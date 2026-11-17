"""Remove finalized source journals only after fresh durable archive verification."""

from datetime import timedelta
import logging
import os
from pathlib import Path
import stat

from django.db import connection
from django.utils import timezone

from .models import Archive, Journal

LOG = logging.getLogger(__name__)


class Cleaner:
    def __init__(self, storage, validator, server_id, spool):
        self.storage = storage
        self.validator = validator
        self.server_id = server_id
        self.spool = Path(spool).resolve()

    def run_once(self, dry_run=False):
        if not self.spool.is_dir():
            raise ValueError(f"spool directory does not exist: {self.spool}")
        # Empty journals have no durable segment archive, so they are retained.
        queue = Journal.objects.filter(
            server_id=self.server_id, state=Journal.State.PROCESSED, integrity__complete=True,
            source_deleted_at__isnull=True, cleanup_next_attempt_at__lte=timezone.now(), segments__isnull=False)
        jobs = list(queue.order_by("cleanup_next_attempt_at", "id").values_list("id", flat=True).distinct()[:100])
        deleted = failed = 0
        for journal_id in jobs:
            lock = int.from_bytes(journal_id.bytes[:8], "big", signed=True)
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_lock(%s)", [lock])
                if not cursor.fetchone()[0]:
                    continue
            try:
                job = queue.filter(pk=journal_id).first()
                if job is None:
                    continue
                if not dry_run:
                    Journal.objects.filter(pk=job.id).update(cleanup_attempts=job.cleanup_attempts + 1)
                try:
                    self.clean(job, dry_run)
                    deleted += 1
                    LOG.info("%s journal %s", "Would remove source for" if dry_run else "Removed source for", journal_id)
                except Exception as error:
                    if not dry_run:
                        delay = min(3600, 10 * 2 ** min(job.cleanup_attempts, 9))
                        Journal.objects.filter(pk=job.id).update(
                            cleanup_error=f"{type(error).__name__}: {error}"[:4000],
                            cleanup_next_attempt_at=timezone.now() + timedelta(seconds=delay))
                    failed += 1
                    LOG.exception("Source cleanup failed for journal %s", journal_id)
            finally:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s)", [lock])
        return deleted, failed

    def clean(self, job, dry_run=False):
        if job.state != Journal.State.PROCESSED or not job.integrity or job.integrity.get("complete") is not True:
            raise ValueError("journal is not completely validated and processed")
        source = Path(job.source_path)
        # Check the resolved parent but never follow a symlink at the final component.
        if source.suffix != ".ready" or source.parent.resolve() != self.spool:
            raise ValueError("source must be a .ready file directly inside the configured spool")
        try:
            before = source.lstat()
        except FileNotFoundError:
            if job.cleanup_verified_at is None:
                raise ValueError("source missing without a verified cleanup intent")
            if not dry_run:
                self.finish(job)
            return
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("source must be a regular file, not a symlink")
        if self.validator.file_hash(source).hex() != job.source_sha256:
            raise ValueError("source SHA-256 mismatch")
        segments = list(job.segments.select_related("archive"))
        expected = {int(number) for number in job.integrity["segments"]}
        if not expected or {segment.segment_number for segment in segments} != expected:
            raise ValueError("segment inventory does not match the validated journal")
        for segment in segments:
            try:
                archive = segment.archive
            except Archive.DoesNotExist:
                raise ValueError("segment archive is missing") from None
            if archive.upload_state != Archive.UploadState.STORED or archive.stored_at is None:
                raise ValueError("not all segment archives are durably stored")
            if (archive.storage_bucket, archive.storage_endpoint, archive.storage_region) != (
                    self.storage.bucket, self.storage.endpoint, self.storage.region):
                raise ValueError("archive belongs to a different S3 destination")
            self.storage.verify(archive)
        after = source.lstat()
        signature = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_mode)
        if signature(before) != signature(after):
            raise ValueError("source changed during cleanup verification")
        if dry_run:
            return
        # Commit intent before unlink: a crash after unlink can then be reconciled safely.
        Journal.objects.filter(pk=job.id).update(cleanup_verified_at=timezone.now(), cleanup_error="")
        if signature(source.lstat()) != signature(after):
            raise ValueError("source changed before deletion")
        source.unlink()
        if os.name != "nt":
            descriptor = os.open(self.spool, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        self.finish(job)

    def finish(self, job):
        Journal.objects.filter(pk=job.id).update(source_deleted_at=timezone.now(), cleanup_error="")
