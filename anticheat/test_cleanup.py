from datetime import timedelta
import io
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

import boto3
from django.core.management import call_command
from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone
from moto import mock_aws

from .archives import load_validator
from .cleanup import Cleaner
from .models import Archive, Journal, Segment, Server
from .storage import S3Storage
from .uploader import Uploader
from .worker import Worker


class CleanupTests(TransactionTestCase):
    def setUp(self):
        emulator = mock_aws()
        emulator.start()
        self.addCleanup(emulator.stop)
        scratch = Path.home() / ".codex" / "scratchpad"
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.spool = self.root / "spool"
        self.spool.mkdir()
        self.source = self.spool / "sample.ready"
        self.source_open = self.spool / "active.open"
        self.source_open.write_bytes(b"active writer")
        tools = os.getenv("AC_PRODUCER_TOOLS", str(Path(__file__).resolve().parents[2] / "beauty-server" / "tools"))
        self.validator = load_validator(tools)
        data = [{"t": "archive", "schema": 1, "started": 1700000000, "nonce": 123, "map": "test"}]
        for number in (1, 2):
            data.extend([
                {"t": "start", "s": number, "steamid": "STEAM_0:1:123"},
                {"t": "stats", "s": number, "kind": 0, "scope": 1, "v": [0] * len(self.validator.BHOP_FIELDS)},
                {"t": "end", "s": number},
            ])
        data.append({"t": "archive_end"})
        self.source.write_bytes(b"".join(self.validator.encode({"q": i, "data": row}) + b"\n" for i, row in enumerate(data, 1)))
        self.server = Server.objects.create(name="Test server")
        self.worker = Worker(self.validator, self.server.id, self.spool, self.root / "archives")
        self.worker.run_once()
        self.s3 = boto3.client("s3", region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing")
        self.s3.create_bucket(Bucket="cleanup-test")
        self.storage = S3Storage(self.s3, "cleanup-test")
        Uploader(self.storage).run_once()
        self.cleaner = Cleaner(self.storage, self.validator, self.server.id, self.spool)

    def assert_cleanup_rejected(self):
        with self.assertLogs("anticheat.cleanup", level="ERROR"):
            self.assertEqual(self.cleaner.run_once(), (0, 1))
        self.assertTrue(self.source.exists())
        self.assertIsNone(Journal.objects.get().source_deleted_at)

    def test_removes_only_source_and_retains_local_archives_and_active_writer(self):
        self.assertEqual(self.cleaner.run_once(), (1, 0))
        self.assertFalse(self.source.exists())
        self.assertEqual(self.source_open.read_bytes(), b"active writer")
        job = Journal.objects.get()
        self.assertIsNotNone(job.source_deleted_at)
        self.assertIsNotNone(job.cleanup_verified_at)
        self.assertTrue(Path(job.local_path).exists())
        for archive in Archive.objects.all():
            self.assertTrue(Path(archive.local_path).exists())
            self.storage.verify(archive)
        self.assertEqual(self.cleaner.run_once(), (0, 0))
        self.assertEqual(self.worker.run_once(), (0, 0))

    def test_dry_run_verifies_without_mutating_database_or_source(self):
        self.assertEqual(self.cleaner.run_once(dry_run=True), (1, 0))
        self.assertTrue(self.source.exists())
        job = Journal.objects.get()
        self.assertEqual(job.cleanup_attempts, 0)
        self.assertIsNone(job.cleanup_verified_at)
        self.assertIsNone(job.source_deleted_at)

    def test_pending_archive_prevents_cleanup(self):
        Archive.objects.filter(pk=Archive.objects.first().pk).update(upload_state="pending")
        self.assert_cleanup_rejected()

    def test_missing_segment_archive_prevents_cleanup(self):
        Archive.objects.first().delete()
        self.assert_cleanup_rejected()

    def test_segment_inventory_must_match_validated_report(self):
        segment = Segment.objects.first()
        segment.segment_number = 7
        segment.save()
        self.assert_cleanup_rejected()

    def test_remote_object_deleted_after_upload_prevents_cleanup(self):
        self.s3.delete_object(Bucket=self.storage.bucket, Key=Archive.objects.first().object_key)
        self.assert_cleanup_rejected()

    def test_remote_object_corruption_prevents_cleanup(self):
        archive = Archive.objects.first()
        self.s3.put_object(Bucket=self.storage.bucket, Key=archive.object_key, Body=b"x" * archive.size_bytes,
                           Metadata={"sha256": archive.sha256})
        self.assert_cleanup_rejected()

    def test_wrong_destination_prevents_cleanup(self):
        Archive.objects.update(storage_bucket="different-bucket")
        self.assert_cleanup_rejected()

    def test_source_hash_mismatch_prevents_cleanup(self):
        self.source.write_bytes(b"changed")
        self.assert_cleanup_rejected()

    def test_source_change_during_s3_verification_prevents_cleanup(self):
        original = self.storage.verify

        def mutate(archive):
            result = original(archive)
            self.source.write_bytes(self.source.read_bytes() + b" ")
            return result

        with patch.object(self.storage, "verify", side_effect=mutate):
            self.assert_cleanup_rejected()

    def test_source_outside_spool_is_rejected(self):
        outside = self.root / "outside.ready"
        outside.write_bytes(self.source.read_bytes())
        Journal.objects.update(source_path=str(outside))
        self.assert_cleanup_rejected()
        self.assertTrue(outside.exists())

    def test_open_source_path_is_rejected(self):
        Journal.objects.update(source_path=str(self.source_open))
        self.assert_cleanup_rejected()
        self.assertEqual(self.source_open.read_bytes(), b"active writer")

    def test_ready_directory_is_not_deleted(self):
        self.source.unlink()
        self.source.mkdir()
        self.assert_cleanup_rejected()
        self.assertTrue(self.source.is_dir())

    def test_incomplete_and_unprocessed_journals_are_retained(self):
        job = Journal.objects.get()
        report = job.integrity
        report["complete"] = False
        Journal.objects.update(integrity=report)
        self.assertEqual(self.cleaner.run_once(), (0, 0))
        report["complete"] = True
        Journal.objects.update(integrity=report, state="failed")
        self.assertEqual(self.cleaner.run_once(), (0, 0))
        self.assertTrue(self.source.exists())

    def test_empty_journal_is_retained(self):
        Archive.objects.all().delete()
        from .models import Summary
        Summary.objects.all().delete()
        Segment.objects.all().delete()
        job = Journal.objects.get()
        job.integrity["segments"] = {}
        job.save()
        self.assertEqual(self.cleaner.run_once(), (0, 0))
        self.assertTrue(self.source.exists())

    def test_crash_after_unlink_is_reconciled_from_verified_intent(self):
        with patch.object(self.cleaner, "finish", side_effect=OSError("simulated crash after unlink")):
            with self.assertLogs("anticheat.cleanup", level="ERROR"):
                self.assertEqual(self.cleaner.run_once(), (0, 1))
        self.assertFalse(self.source.exists())
        self.assertIsNotNone(Journal.objects.get().cleanup_verified_at)
        self.assertIsNone(Journal.objects.get().source_deleted_at)
        Journal.objects.update(cleanup_next_attempt_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.cleaner.run_once(), (1, 0))
        self.assertIsNotNone(Journal.objects.get().source_deleted_at)

    def test_missing_source_without_intent_is_not_marked_deleted(self):
        self.source.unlink()
        with self.assertLogs("anticheat.cleanup", level="ERROR"):
            self.assertEqual(self.cleaner.run_once(), (0, 1))
        self.assertIsNone(Journal.objects.get().source_deleted_at)
        self.assertIsNone(Journal.objects.get().cleanup_verified_at)

    def test_permission_error_retries_without_losing_source(self):
        with patch.object(Path, "unlink", side_effect=PermissionError("read-only spool")):
            self.assert_cleanup_rejected()
        self.assertEqual(self.cleaner.run_once(), (0, 0))
        Journal.objects.update(cleanup_next_attempt_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.cleaner.run_once(), (1, 0))

    def test_other_session_holds_journal_lock(self):
        job = Journal.objects.get()
        lock = int.from_bytes(job.id.bytes[:8], "big", signed=True)
        other = connection.copy(alias="cleanup_lock_test")
        try:
            with other.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock(%s)", [lock])
            self.assertEqual(self.cleaner.run_once(), (0, 0))
            self.assertTrue(self.source.exists())
        finally:
            other.close()
        self.assertEqual(self.cleaner.run_once(), (1, 0))

    def test_management_command_dry_run(self):
        with patch("anticheat.management.commands.cleanup_ac_journals.S3Storage.from_settings", return_value=self.storage):
            call_command("cleanup_ac_journals", server_id=self.server.id,
                         producer_tools=Path(self.validator.__file__).parent, spool=self.spool, dry_run=True, stdout=io.StringIO())
        self.assertTrue(self.source.exists())
