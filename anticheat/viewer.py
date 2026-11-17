"""Bounded, verified reads of private segment archives for the web viewer."""

from contextlib import contextmanager
import hashlib
from pathlib import Path
import tempfile

from django.conf import settings

from .storage import S3Storage


@contextmanager
def archive_path(archive):
    if archive.size_bytes > settings.AC_VIEWER_MAX_ARCHIVE_BYTES:
        raise ValueError("Архив превышает лимит веб-просмотра.")
    local = Path(archive.local_path)
    if local.is_file():
        digest = hashlib.sha256()
        with local.open("rb") as incoming:
            while block := incoming.read(1024 * 1024):
                digest.update(block)
        if local.stat().st_size != archive.size_bytes or digest.hexdigest() != archive.sha256:
            raise ValueError("Контрольная сумма локального архива не совпадает.")
        yield local
        return
    storage = S3Storage.from_settings()
    if archive.upload_state != "stored" or (archive.storage_bucket, archive.storage_endpoint, archive.storage_region) != (
            storage.bucket, storage.endpoint, storage.region):
        raise ValueError("Подтверждённая копия архива недоступна.")
    options = {"Bucket": storage.bucket, "Key": archive.object_key}
    if archive.storage_version_id:
        options["VersionId"] = archive.storage_version_id
    response = storage.client.get_object(**options)
    with tempfile.TemporaryDirectory(prefix="ac-view-") as temporary:
        path = Path(temporary) / "segment.json.gz"
        body = response["Body"]
        try:
            if response["ContentLength"] != archive.size_bytes:
                raise ValueError("Размер архива в хранилище не совпадает.")
            digest, size = hashlib.sha256(), 0
            with path.open("wb") as outgoing:
                while block := body.read(1024 * 1024):
                    size += len(block)
                    if size > archive.size_bytes:
                        raise ValueError("Архив превышает ожидаемый размер.")
                    digest.update(block)
                    outgoing.write(block)
            if size != archive.size_bytes or digest.hexdigest() != archive.sha256:
                raise ValueError("Контрольная сумма архива в хранилище не совпадает.")
        finally:
            body.close()
        yield path
