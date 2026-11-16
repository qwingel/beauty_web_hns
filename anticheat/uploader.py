"""Durable upload queue; local archives and source journals are retained."""

from datetime import timedelta
import logging

from django.db import connection
from django.utils import timezone

from .models import Archive

LOG = logging.getLogger(__name__)


class Uploader:
    def __init__(self, storage, server_id=None):
        self.storage = storage
        self.server_id = server_id

    def run_once(self):
        queue = Archive.objects.filter(
            upload_next_attempt_at__lte=timezone.now(),
            upload_state__in=[Archive.UploadState.PENDING, Archive.UploadState.UPLOADING, Archive.UploadState.FAILED])
        if self.server_id:
            queue = queue.filter(segment__journal__server_id=self.server_id)
        jobs = list(queue.order_by("upload_next_attempt_at", "id").values_list("id", flat=True)[:100])
        stored = failed = 0
        for archive_id in jobs:
            lock = int.from_bytes(archive_id.bytes[:8], "big", signed=True)
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_lock(%s)", [lock])
                if not cursor.fetchone()[0]:
                    continue
            try:
                archive = queue.filter(pk=archive_id).first()
                if archive is None:
                    continue
                archive.upload_state = Archive.UploadState.UPLOADING
                archive.upload_attempts += 1
                archive.upload_error = ""
                archive.save(update_fields=["upload_state", "upload_attempts", "upload_error"])
                try:
                    destination = (self.storage.bucket, self.storage.endpoint, self.storage.region)
                    bound = (archive.storage_bucket, archive.storage_endpoint, archive.storage_region)
                    if archive.storage_bucket and bound != destination:
                        raise ValueError("archive is bound to a different S3 destination")
                    if not archive.storage_bucket:
                        archive.storage_bucket, archive.storage_endpoint, archive.storage_region = destination
                        archive.save(update_fields=["storage_bucket", "storage_endpoint", "storage_region"])
                    verified = self.storage.store(archive)
                    Archive.objects.filter(pk=archive_id).update(
                        upload_state=Archive.UploadState.STORED, stored_at=timezone.now(), upload_error="",
                        storage_version_id=verified["version_id"], storage_etag=verified["etag"])
                    stored += 1
                    LOG.info("Stored archive %s", archive_id)
                except Exception as error:
                    delay = min(3600, 10 * 2 ** min(archive.upload_attempts - 1, 9))
                    Archive.objects.filter(pk=archive_id).update(
                        upload_state=Archive.UploadState.FAILED,
                        upload_error=f"{type(error).__name__}: {error}"[:4000],
                        upload_next_attempt_at=timezone.now() + timedelta(seconds=delay))
                    failed += 1
                    LOG.exception("Archive %s upload failed; retry in %ss", archive_id, delay)
            finally:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s)", [lock])
        return stored, failed
