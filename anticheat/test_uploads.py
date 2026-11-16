from datetime import timedelta
import gzip
import hashlib
import io
from pathlib import Path
import tempfile
from unittest.mock import patch
from uuid import uuid4

import boto3
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command, CommandError
from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from moto import mock_aws

from .models import Archive, Journal, Segment, Server
from .storage import S3Storage
from .uploader import Uploader


class UploadTests(TransactionTestCase):
    def setUp(self):
        emulator = mock_aws()
        emulator.start()
        self.addCleanup(emulator.stop)
        self.client_s3 = boto3.client("s3", region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing")
        self.bucket = "beauty-ac-test"
        self.client_s3.create_bucket(Bucket=self.bucket)
        self.storage = S3Storage(self.client_s3, self.bucket)
        self.uploader = Uploader(self.storage)
        scratch = Path.home() / ".codex" / "scratchpad"
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.payload = gzip.compress(b'{"schema":1,"records":[]}', mtime=0)
        path = self.root / "archive.json.gz"
        path.write_bytes(self.payload)
        self.server = Server.objects.create(name="Test")
        journal = Journal.objects.create(id=uuid4(), server=self.server, source_sha256="0" * 64,
                                         source_path=str(self.root / "source.ready"), state="processed")
        segment = Segment.objects.create(id=uuid4(), journal=journal, segment_number=1, metadata={})
        self.archive = Archive.objects.create(
            id=uuid4(), segment=segment, sha256=hashlib.sha256(self.payload).hexdigest(),
            size_bytes=len(self.payload), local_path=str(path), object_key="v1/test/archive.json.gz")

    def upload_existing(self, payload=None, **options):
        return self.client_s3.put_object(Bucket=self.bucket, Key=self.archive.object_key,
                                         Body=self.payload if payload is None else payload, **options)

    def test_upload_verifies_remote_bytes_and_retains_local_file(self):
        with patch.object(self.client_s3, "put_object", wraps=self.client_s3.put_object) as put:
            self.assertEqual(self.uploader.run_once(), (1, 0))
            self.assertEqual(put.call_args.kwargs["IfNoneMatch"], "*")
            self.assertNotIn("ACL", put.call_args.kwargs)
        self.archive.refresh_from_db()
        self.assertEqual(self.archive.upload_state, "stored")
        self.assertEqual(self.archive.storage_bucket, self.bucket)
        self.assertEqual(self.archive.storage_region, "us-east-1")
        self.assertTrue(self.archive.storage_etag)
        self.assertIsNotNone(self.archive.stored_at)
        self.assertTrue(Path(self.archive.local_path).exists())
        self.assertEqual(self.uploader.run_once(), (0, 0))

    def test_existing_matching_object_is_reused_without_local_file(self):
        self.upload_existing()
        Path(self.archive.local_path).unlink()
        with patch.object(self.client_s3, "put_object", side_effect=AssertionError("must not upload")):
            self.assertEqual(self.uploader.run_once(), (1, 0))

    def test_existing_wrong_bytes_are_not_trusted_even_with_correct_metadata(self):
        corrupt = b"x" * len(self.payload)
        self.upload_existing(corrupt, Metadata={"sha256": self.archive.sha256})
        with self.assertLogs("anticheat.uploader", level="ERROR"):
            self.assertEqual(self.uploader.run_once(), (0, 1))
        self.archive.refresh_from_db()
        self.assertIsNone(self.archive.stored_at)
        self.assertIn("SHA-256 mismatch", self.archive.upload_error)
        response = self.client_s3.get_object(Bucket=self.bucket, Key=self.archive.object_key)
        with response["Body"] as body:
            self.assertEqual(body.read(), corrupt)

    def test_wrong_remote_size_fails(self):
        self.upload_existing(b"short")
        with self.assertLogs("anticheat.uploader", level="ERROR"):
            self.assertEqual(self.uploader.run_once(), (0, 1))
        self.archive.refresh_from_db()
        self.assertIn("size mismatch", self.archive.upload_error)

    def test_truncated_remote_stream_never_becomes_stored(self):
        incoming = io.BytesIO(self.payload[:-1])
        body = StreamingBody(incoming, len(self.payload))
        response = {"Body": body, "ContentLength": len(self.payload)}
        with patch.object(self.client_s3, "get_object", return_value=response):
            with self.assertLogs("anticheat.uploader", level="ERROR"):
                self.assertEqual(self.uploader.run_once(), (0, 1))
        self.archive.refresh_from_db()
        self.assertEqual(self.archive.upload_state, "failed")
        self.assertIsNone(self.archive.stored_at)
        self.assertTrue(incoming.closed)

    def test_changed_local_file_is_not_uploaded(self):
        Path(self.archive.local_path).write_bytes(b"corrupt")
        with self.assertLogs("anticheat.uploader", level="ERROR"):
            self.assertEqual(self.uploader.run_once(), (0, 1))
        self.archive.refresh_from_db()
        self.assertIn("local archive", self.archive.upload_error)
        with self.assertRaises(ClientError):
            self.client_s3.get_object(Bucket=self.bucket, Key=self.archive.object_key)

    def test_access_denied_is_not_treated_as_missing_object(self):
        denied = ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "GetObject")
        with patch.object(self.client_s3, "get_object", side_effect=denied):
            with patch.object(self.client_s3, "put_object", side_effect=AssertionError("must not upload")):
                with self.assertLogs("anticheat.uploader", level="ERROR"):
                    self.assertEqual(self.uploader.run_once(), (0, 1))
        self.archive.refresh_from_db()
        self.assertGreater(self.archive.upload_next_attempt_at, timezone.now())
        self.assertEqual(self.uploader.run_once(), (0, 0))

    def test_interrupted_upload_is_reclaimed_and_verified(self):
        self.upload_existing()
        Archive.objects.update(upload_state="uploading", upload_attempts=1)
        self.assertEqual(self.uploader.run_once(), (1, 0))
        self.archive.refresh_from_db()
        self.assertEqual(self.archive.upload_attempts, 2)

    def test_failure_after_put_retries_without_overwrite(self):
        original = self.storage.verify
        calls = 0

        def fail_verification(archive):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("read interrupted")
            return original(archive)

        with patch.object(self.storage, "verify", side_effect=fail_verification):
            with self.assertLogs("anticheat.uploader", level="ERROR"):
                self.assertEqual(self.uploader.run_once(), (0, 1))
        Archive.objects.update(upload_next_attempt_at=timezone.now() - timedelta(seconds=1))
        with patch.object(self.client_s3, "put_object", side_effect=AssertionError("must reuse object")):
            self.assertEqual(self.uploader.run_once(), (1, 0))

    def test_conditional_put_race_verifies_winner(self):
        original = self.client_s3.put_object

        def competing_put(**kwargs):
            original(Bucket=self.bucket, Key=self.archive.object_key, Body=self.payload)
            return original(**kwargs)

        with patch.object(self.client_s3, "put_object", side_effect=competing_put):
            self.assertEqual(self.uploader.run_once(), (1, 0))

    def test_conditional_put_race_rejects_different_winner(self):
        original = self.client_s3.put_object

        def competing_put(**kwargs):
            original(Bucket=self.bucket, Key=self.archive.object_key, Body=b"wrong")
            return original(**kwargs)

        with patch.object(self.client_s3, "put_object", side_effect=competing_put):
            with self.assertLogs("anticheat.uploader", level="ERROR"):
                self.assertEqual(self.uploader.run_once(), (0, 1))

    def test_versioned_bucket_records_verified_version(self):
        self.client_s3.put_bucket_versioning(Bucket=self.bucket, VersioningConfiguration={"Status": "Enabled"})
        self.assertEqual(self.uploader.run_once(), (1, 0))
        self.archive.refresh_from_db()
        self.assertTrue(self.archive.storage_version_id)

    def test_destination_is_bound_before_upload_and_cannot_silently_change(self):
        with patch.object(self.storage, "store", side_effect=OSError("timeout")):
            with self.assertLogs("anticheat.uploader", level="ERROR"):
                self.assertEqual(self.uploader.run_once(), (0, 1))
        Archive.objects.update(upload_next_attempt_at=timezone.now() - timedelta(seconds=1))
        other = Uploader(S3Storage(self.client_s3, "different-bucket"))
        with self.assertLogs("anticheat.uploader", level="ERROR"):
            self.assertEqual(other.run_once(), (0, 1))
        self.archive.refresh_from_db()
        self.assertEqual(self.archive.storage_bucket, self.bucket)
        self.assertIn("different S3 destination", self.archive.upload_error)

    def test_other_session_holds_upload_lock(self):
        lock = int.from_bytes(self.archive.id.bytes[:8], "big", signed=True)
        other = connection.copy(alias="upload_lock_test")
        try:
            with other.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock(%s)", [lock])
            self.assertEqual(self.uploader.run_once(), (0, 0))
        finally:
            other.close()
        self.assertEqual(self.uploader.run_once(), (1, 0))

    def test_server_filter_excludes_other_servers(self):
        self.assertEqual(Uploader(self.storage, uuid4()).run_once(), (0, 0))
        self.assertEqual(Uploader(self.storage, self.server.id).run_once(), (1, 0))

    def test_management_command_once(self):
        with patch("anticheat.management.commands.upload_ac_archives.S3Storage.from_settings", return_value=self.storage):
            call_command("upload_ac_archives", once=True, stdout=io.StringIO())
        self.archive.refresh_from_db()
        self.assertEqual(self.archive.upload_state, "stored")

    def test_processing_command_can_upload_without_new_journals(self):
        with patch("anticheat.management.commands.process_ac_archives.S3Storage.from_settings", return_value=self.storage):
            with patch("anticheat.management.commands.process_ac_archives.load_validator"):
                with patch("anticheat.management.commands.process_ac_archives.Worker.run_once", return_value=(0, 0)):
                    call_command("process_ac_archives", server_id=self.server.id, producer_tools=self.root,
                                 spool=self.root, output=self.root, upload=True, once=True, stdout=io.StringIO())
        self.archive.refresh_from_db()
        self.assertEqual(self.archive.upload_state, "stored")

    def test_management_command_failure_exits_nonzero(self):
        with patch("anticheat.management.commands.upload_ac_archives.S3Storage.from_settings", return_value=self.storage):
            with patch.object(self.storage, "store", side_effect=OSError("timeout")):
                with self.assertLogs("anticheat.uploader", level="ERROR"):
                    with self.assertRaises(CommandError):
                        call_command("upload_ac_archives", once=True, stdout=io.StringIO())

    @override_settings(AC_S3_BUCKET="")
    def test_configuration_requires_bucket(self):
        with self.assertRaises(ImproperlyConfigured):
            S3Storage.from_settings()

    @override_settings(AC_S3_BUCKET="timeweb-test", AC_S3_ENDPOINT_URL="https://s3.twcstorage.ru/",
                       AC_S3_REGION="ru-1", AC_S3_ADDRESSING_STYLE="path")
    def test_timeweb_client_configuration(self):
        storage = S3Storage.from_settings()
        self.assertEqual(storage.endpoint, "https://s3.twcstorage.ru")
        self.assertEqual(storage.region, "ru-1")
        self.assertEqual(storage.client.meta.config.s3["addressing_style"], "path")
        self.assertEqual(storage.client.meta.config.signature_version, "s3v4")

    @override_settings(AC_S3_BUCKET="test", AC_S3_ENDPOINT_URL="http://storage.example.com")
    def test_configuration_requires_https_for_remote_endpoint(self):
        with self.assertRaises(ImproperlyConfigured):
            S3Storage.from_settings()
