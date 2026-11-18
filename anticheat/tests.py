from datetime import timedelta
import gzip
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection, IntegrityError, transaction
from django.test import TransactionTestCase
from django.utils import timezone

from community.ban_access import BanAccess

from .archives import load_validator
from .identifiers import stable_id
from .models import Archive, Decision, Journal, Player, Segment, Server, Summary
from .worker import Worker


class WorkerTests(TransactionTestCase):
    def setUp(self):
        scratch = Path.home() / ".codex" / "scratchpad"
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.spool = self.root / "spool"
        self.spool.mkdir()
        self.source = self.spool / "sample.ready"
        tools = os.getenv("AC_PRODUCER_TOOLS", str(Path(__file__).resolve().parents[2] / "beauty-server" / "tools"))
        self.validator = load_validator(tools)
        self.server = Server.objects.create(name="Test server")
        self.worker = Worker(self.validator, self.server.id, self.spool, self.root / "output")

    def fixture(self, steamid="STEAM_0:1:123", second=True):
        data = [
            {"t": "archive", "schema": 1, "started": 1700000000, "nonce": 123, "map": "test"},
            {"t": "start", "s": 1, "steamid": steamid, "name": "Test", "at": 1700000001},
        ]
        for kind in (0, 1):
            window = 32 + kind
            data.append({"t": "window", "s": 1, "w": window, "kind": kind, "first": 1, "last": 31})
            for command in range(1, 32):
                values = [command] + [0] * (len(self.validator.COMMAND_FIELDS) - 1)
                values[7] = -123.4567890123
                data.append({"t": "cmd", "s": 1, "w": window, "v": values})
            fields = (self.validator.BHOP_FIELDS, self.validator.GSTRAFE_FIELDS)[kind]
            data.append({"t": "stats", "s": 1, "kind": kind, "scope": 1, "v": list(range(len(fields)))})
        data.extend([
            {"t": "decision", "s": 1, "kind": 0, "c": 16, "detect": 1},
            {"t": "end", "s": 1, "at": 1700000100},
        ])
        if second:
            data.extend([
                {"t": "start", "s": 2, "steamid": steamid, "name": "Reconnect"},
                {"t": "end", "s": 2},
            ])
        data.append({"t": "archive_end", "dropped": 0, "write_errors": 0})
        return [{"q": index, "data": row} for index, row in enumerate(data, 1)]

    def write(self, rows=None):
        self.source.write_bytes(b"".join(self.validator.encode(row) + b"\n" for row in (rows or self.fixture())))

    def test_ingestion_and_segment_isolation(self):
        self.write()
        self.assertEqual(self.worker.run_once(), (1, 0))
        self.assertTrue(self.source.exists())
        self.assertEqual(Player.objects.count(), 1)
        self.assertEqual(Segment.objects.count(), 2)
        self.assertEqual(Summary.objects.count(), 2)
        self.assertEqual(Decision.objects.get().command_id, 16)
        self.assertTrue(Journal.objects.get().integrity["complete"])
        for archive in Archive.objects.select_related("segment"):
            self.assertEqual(archive.upload_state, "pending")
            self.assertIsNone(archive.stored_at)
            self.assertEqual(self.validator.file_hash(Path(archive.local_path)).hex(), archive.sha256)
            with gzip.open(archive.local_path, "rt") as incoming:
                document = json.load(incoming)
            self.assertEqual({row["data"]["s"] for row in document["records"]}, {archive.segment.segment_number})
            if archive.segment.segment_number == 1:
                commands = [row for row in document["records"] if row["data"]["t"] == "cmd"]
                self.assertEqual(len(commands), 62)
                self.assertEqual(commands[0]["data"]["v"][7], -123.4567890123)

    def test_repeated_discovery_is_idempotent(self):
        self.write()
        self.worker.run_once()
        before = list(Archive.objects.values_list("id", "sha256"))
        self.assertEqual(self.worker.run_once(), (0, 0))
        self.assertEqual(Journal.objects.count(), 1)
        self.assertEqual(Journal.objects.get().attempts, 1)
        self.assertEqual(list(Archive.objects.values_list("id", "sha256")), before)

    def test_corruption_retries_without_deleting_source(self):
        self.source.write_bytes(b"broken\n")
        with self.assertLogs("anticheat.worker", level="ERROR"):
            self.assertEqual(self.worker.run_once(), (0, 1))
        job = Journal.objects.get()
        self.assertEqual(job.state, "failed")
        self.assertGreater(job.next_attempt_at, timezone.now())
        self.assertIn("ValueError", job.last_error)
        self.assertTrue(self.source.exists())
        self.assertEqual(self.worker.run_once(), (0, 0))

    def test_rollback_and_retry_reuses_existing_files(self):
        self.write()
        with patch.object(Archive.objects, "create", side_effect=RuntimeError("injected database failure")):
            with self.assertLogs("anticheat.worker", level="ERROR"):
                self.assertEqual(self.worker.run_once(), (0, 1))
        self.assertEqual(Segment.objects.count(), 0)
        self.assertEqual(Player.objects.count(), 0)
        paths = list((self.root / "output" / "segments").glob("*.gz"))
        hashes = {path: self.validator.file_hash(path) for path in paths}
        self.assertEqual(len(paths), 2)
        Journal.objects.update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.worker.run_once(), (1, 0))
        self.assertEqual(hashes, {path: self.validator.file_hash(path) for path in paths})
        self.assertEqual(Journal.objects.get().attempts, 2)

    def test_crashed_processing_job_is_reclaimed(self):
        self.write()
        self.worker.discover()
        Journal.objects.update(state="processing", attempts=1)
        self.assertEqual(self.worker.run_once(), (1, 0))
        self.assertEqual(Journal.objects.get().attempts, 2)

    def test_other_worker_session_holds_lock(self):
        self.write()
        self.worker.discover()
        job = Journal.objects.get()
        lock = int.from_bytes(job.id.bytes[:8], "big", signed=True)
        other = connection.copy(alias="worker_lock_test")
        try:
            with other.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock(%s)", [lock])
            self.assertEqual(self.worker.run_once(), (0, 0))
            self.assertEqual(Journal.objects.get().attempts, 0)
        finally:
            other.close()
        self.assertEqual(self.worker.run_once(), (1, 0))

    def test_open_journals_are_ignored(self):
        self.write()
        self.source.rename(self.source.with_suffix(".open"))
        self.assertEqual(self.worker.run_once(), (0, 0))
        self.assertEqual(Journal.objects.count(), 0)

    def test_gaps_remain_visible(self):
        rows = self.fixture()
        for row in rows[2:]:
            row["q"] += 2
        self.write(rows)
        self.assertEqual(self.worker.run_once(), (1, 0))
        self.assertFalse(Journal.objects.get().integrity["complete"])
        for archive in Archive.objects.all():
            with gzip.open(archive.local_path, "rt") as incoming:
                self.assertEqual(json.load(incoming)["report"]["missing_records"], 2)

    def test_pending_identity_does_not_create_shared_player(self):
        self.write(self.fixture(steamid="STEAM_ID_PENDING"))
        self.worker.run_once()
        self.assertEqual(Player.objects.count(), 0)
        self.assertEqual(Segment.objects.filter(player__isnull=True).count(), 2)

    def test_late_identity_resolves_player(self):
        rows = self.fixture(steamid="STEAM_ID_PENDING", second=False)
        rows.insert(2, {"data": {"t": "identity", "s": 1, "steamid": "STEAM_0:1:456"}})
        for index, row in enumerate(rows, 1):
            row["q"] = index
        self.write(rows)
        self.worker.run_once()
        self.assertEqual(Segment.objects.get().player.steamid, "STEAM_0:1:456")

    def test_corrupt_existing_segment_archive_is_not_overwritten(self):
        self.write()
        with patch.object(Archive.objects, "create", side_effect=RuntimeError("failure")):
            with self.assertLogs("anticheat.worker", level="ERROR"):
                self.worker.run_once()
        path = next((self.root / "output" / "segments").glob("*.gz"))
        path.write_bytes(b"corrupt")
        Journal.objects.update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        with self.assertLogs("anticheat.worker", level="ERROR"):
            self.assertEqual(self.worker.run_once(), (0, 1))
        self.assertEqual(path.read_bytes(), b"corrupt")
        self.assertEqual(Segment.objects.count(), 0)

    def test_identical_journals_on_different_servers_have_distinct_ids(self):
        self.write()
        self.worker.run_once()
        server = Server.objects.create(name="Second")
        other = Worker(self.validator, server.id, self.spool, self.root / "output")
        self.assertEqual(other.run_once(), (1, 0))
        self.assertEqual(Journal.objects.count(), 2)
        self.assertEqual(Archive.objects.count(), 4)
        self.assertEqual(Player.objects.count(), 1)

    def test_source_changed_after_discovery(self):
        self.write()
        self.worker.discover()
        job = Journal.objects.get()
        self.source.write_bytes(self.source.read_bytes().replace(b'"map":"test"', b'"map":"changed"'))
        with self.assertRaisesRegex(ValueError, "changed since discovery"):
            self.worker.process(job)
        self.assertEqual(Segment.objects.count(), 0)

    def test_stored_state_requires_storage_reference(self):
        self.write()
        self.worker.run_once()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Archive.objects.update(upload_state="stored")

    def test_management_command_once(self):
        self.write()
        call_command("process_ac_archives", producer_tools=Path(self.validator.__file__).parent,
                     server_id=self.server.id, spool=self.spool, output=self.root / "output", once=True, verbosity=0)
        self.assertEqual(Journal.objects.get().state, "processed")


class AccessTests(TransactionTestCase):
    def test_telemetry_requires_admin_access(self):
        url = "/admin/anticheat/journal/"
        self.assertEqual(self.client.get(url).status_code, 302)
        user = get_user_model().objects.create_user(username="regular")
        self.client.force_login(user)
        self.assertEqual(self.client.get(url).status_code, 302)
        user.is_staff = True
        user.save()
        self.assertEqual(self.client.get(url).status_code, 403)
        user.is_superuser = True
        user.save()
        self.assertEqual(self.client.get(url).status_code, 403)
        with patch("community.ban_access.lookup_access", return_value=BanAccess(1, "владелец")):
            self.assertEqual(self.client.get(url).status_code, 200)
