"""Restartable journal processing, separate from Django's HTTP processes."""

from datetime import timedelta
import logging
from pathlib import Path
import re

from django.db import connection, transaction
from django.utils import timezone

from .archives import records, split_archive
from .identifiers import stable_id, timestamp
from .models import Archive, Decision, Journal, Player, Segment, Summary

LOG = logging.getLogger(__name__)
STEAM_ID = re.compile(r"STEAM_[0-5]:[01]:[0-9]+\Z")


class Worker:
    def __init__(self, validator, server_id, spool, output):
        self.validator = validator
        self.server_id = server_id
        self.spool = Path(spool).resolve()
        self.output = Path(output).resolve()

    def discover(self):
        if not self.spool.is_dir():
            raise ValueError(f"spool directory does not exist: {self.spool}")
        for source in sorted(self.spool.glob("*.ready")):
            try:
                digest = self.validator.file_hash(source).hex()
                journal_id = stable_id("journal", f"{self.server_id}:{digest}")
                Journal.objects.get_or_create(
                    server_id=self.server_id, source_sha256=digest,
                    defaults={"id": journal_id, "source_path": str(source)})
                Journal.objects.filter(pk=journal_id).exclude(source_path=str(source)).update(source_path=str(source))
            except OSError:
                LOG.exception("Cannot discover %s", source)

    def run_once(self):
        self.discover()
        jobs = list(Journal.objects.filter(server_id=self.server_id, next_attempt_at__lte=timezone.now())
                    .exclude(state=Journal.State.PROCESSED).order_by("next_attempt_at", "id")
                    .values_list("id", flat=True)[:100])
        processed = failed = 0
        for journal_id in jobs:
            # Session locks survive commits and PostgreSQL releases them on disconnect.
            lock = int.from_bytes(journal_id.bytes[:8], "big", signed=True)
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_lock(%s)", [lock])
                if not cursor.fetchone()[0]:
                    continue
            try:
                job = Journal.objects.filter(pk=journal_id, next_attempt_at__lte=timezone.now()).exclude(
                    state=Journal.State.PROCESSED).first()
                if job is None:
                    continue
                job.state = Journal.State.PROCESSING
                job.attempts += 1
                job.last_error = ""
                job.save(update_fields=["state", "attempts", "last_error"])
                try:
                    self.process(job)
                    processed += 1
                    LOG.info("Processed journal %s", journal_id)
                except Exception as error:
                    delay = min(3600, 10 * 2 ** min(job.attempts - 1, 9))
                    Journal.objects.filter(pk=journal_id).update(
                        state=Journal.State.FAILED, last_error=f"{type(error).__name__}: {error}"[:4000],
                        next_attempt_at=timezone.now() + timedelta(seconds=delay))
                    failed += 1
                    LOG.exception("Journal %s failed; retry in %ss", journal_id, delay)
            finally:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s)", [lock])
        return processed, failed

    def process(self, job):
        packed = self.validator.pack(Path(job.source_path), self.output / "journals")
        if packed["report"]["source_sha256"] != job.source_sha256:
            raise ValueError("source journal changed since discovery")
        packed_path = Path(packed["path"])
        header, report, archives = split_archive(
            packed_path, self.output / "segments", job.id, self.validator)
        with transaction.atomic():
            segment_ids = {}
            for item in archives:
                number = item["number"]
                segment_id = item["segment_id"]
                segment_ids[number] = segment_id
                state = report["segments"][str(number)]
                start, end = state["start"] or {}, state["end"] or {}
                steamid = state.get("steamid", start.get("steamid")) or ""
                player = None
                # Pending, LAN, bot and orphan identities must not merge unrelated players.
                if STEAM_ID.fullmatch(steamid):
                    player, _ = Player.objects.get_or_create(
                        steamid=steamid, defaults={"id": stable_id("player", steamid)})
                Segment.objects.create(
                    id=segment_id, journal=job, segment_number=number, player=player, steamid=steamid,
                    name=start.get("name", ""), name_raw_hex=start.get("name_raw_hex", ""),
                    started_at=timestamp(start.get("at")), ended_at=timestamp(end.get("at")), metadata=state)
                for kind, counters in state["latest_totals"].items():
                    Summary.objects.create(segment_id=segment_id, kind=int(kind), counters=counters)
                Archive.objects.create(
                    id=item["id"], segment_id=segment_id, sha256=item["sha256"], size_bytes=item["size"],
                    local_path=str(item["path"]), object_key=f"v1/{self.server_id}/{job.id}/{item['id']}.json.gz")
            for row in records(packed_path):
                data = row["data"]
                if data["t"] == "decision":
                    Decision.objects.create(segment_id=segment_ids[data["s"]], sequence=row["q"],
                                            command_id=data.get("c"), kind=data["kind"], payload=data)
            Journal.objects.filter(pk=job.id).update(
                state=Journal.State.PROCESSED, map_name=header.get("map", ""), started_at=timestamp(header["started"]),
                integrity=report, local_path=str(packed_path), processed_at=timezone.now(), last_error="")
