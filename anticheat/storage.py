"""Conditional S3 publication and verification of the actual stored bytes."""

import hashlib
import os
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class S3Storage:
    def __init__(self, client, bucket, endpoint="", region="us-east-1"):
        self.client = client
        self.bucket = bucket
        self.endpoint = endpoint.rstrip("/")
        self.region = region

    @classmethod
    def from_settings(cls):
        if not settings.AC_S3_BUCKET:
            raise ImproperlyConfigured("AC_S3_BUCKET is required for uploads")
        endpoint = settings.AC_S3_ENDPOINT_URL.rstrip("/")
        if endpoint:
            url = urlsplit(endpoint)
            if (url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password
                    or url.query or url.fragment):
                raise ImproperlyConfigured("AC_S3_ENDPOINT_URL must be an HTTP(S) URL without credentials or query")
            if url.scheme != "https" and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise ImproperlyConfigured("S3 endpoint requires HTTPS outside localhost")
        if settings.AC_S3_ADDRESSING_STYLE not in {"auto", "path", "virtual"}:
            raise ImproperlyConfigured("AC_S3_ADDRESSING_STYLE must be auto, path or virtual")
        client = boto3.client(
            "s3", endpoint_url=endpoint or None, region_name=settings.AC_S3_REGION,
            config=Config(signature_version="s3v4", connect_timeout=10, read_timeout=60,
                          retries={"mode": "standard", "total_max_attempts": 3},
                          request_checksum_calculation="when_required", response_checksum_validation="when_required",
                          s3={"addressing_style": settings.AC_S3_ADDRESSING_STYLE}))
        return cls(client, settings.AC_S3_BUCKET, endpoint, settings.AC_S3_REGION)

    def verify(self, archive):
        """Metadata and ETag are not trusted as content checksums."""
        response = self.client.get_object(Bucket=self.bucket, Key=archive.object_key)
        body = response["Body"]
        try:
            if response["ContentLength"] != archive.size_bytes:
                raise ValueError("stored archive size mismatch")
            digest = hashlib.sha256()
            size = 0
            while block := body.read(1024 * 1024):
                size += len(block)
                if size > archive.size_bytes:
                    raise ValueError("stored archive exceeds expected size")
                digest.update(block)
            if size != archive.size_bytes or digest.hexdigest() != archive.sha256:
                raise ValueError("stored archive SHA-256 mismatch")
        finally:
            body.close()
        return {"version_id": response.get("VersionId", ""), "etag": response.get("ETag", "")}

    def store(self, archive):
        try:
            return self.verify(archive)
        except ClientError as error:
            # AccessDenied and transport failures must not be mistaken for missing objects.
            if error.response["Error"]["Code"] not in {"NoSuchKey", "404", "NotFound"}:
                raise
        # Single PUT is atomic; multipart uploads are intentionally not introduced here.
        if archive.size_bytes > 5 * 1024**3:
            raise ValueError("archive exceeds the 5 GiB single-PUT limit")
        with open(archive.local_path, "rb") as incoming:
            before = os.fstat(incoming.fileno())
            digest = hashlib.sha256()
            while block := incoming.read(1024 * 1024):
                digest.update(block)
            if before.st_size != archive.size_bytes or digest.hexdigest() != archive.sha256:
                raise ValueError("local archive size or SHA-256 mismatch")
            incoming.seek(0)
            try:
                self.client.put_object(
                    Bucket=self.bucket, Key=archive.object_key, Body=incoming, ContentLength=archive.size_bytes,
                    ContentType="application/gzip", Metadata={"sha256": archive.sha256, "archive-id": str(archive.id)},
                    IfNoneMatch="*")
            except ClientError as error:
                # A competing or timed-out PUT may already have published the same object.
                if error.response["Error"]["Code"] not in {"PreconditionFailed", "412"}:
                    raise
            after = os.fstat(incoming.fileno())
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError("local archive changed during upload")
        return self.verify(archive)
