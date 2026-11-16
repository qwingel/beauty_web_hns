import logging
import os
from pathlib import Path
import time
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ImproperlyConfigured
from django.db import connection

from anticheat.archives import load_validator
from anticheat.models import Server
from anticheat.storage import S3Storage
from anticheat.uploader import Uploader
from anticheat.worker import Worker


class Command(BaseCommand):
    help = "Watch finalized AC journals and create verified local segment archives."

    def add_arguments(self, parser):
        parser.add_argument("--producer-tools", type=Path, default=os.getenv("AC_PRODUCER_TOOLS"))
        parser.add_argument("--server-id", type=UUID, default=os.getenv("AC_SERVER_ID"))
        parser.add_argument("--server-name", default=os.getenv("AC_SERVER_NAME", "Beauty HNS"))
        parser.add_argument("--spool", type=Path, default=os.getenv("AC_SPOOL"))
        parser.add_argument("--output", type=Path, default=os.getenv("AC_OUTPUT"))
        parser.add_argument("--poll-seconds", type=float, default=10)
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--upload", action="store_true", help="Upload pending archives to configured private S3 storage")

    def handle(self, **options):
        if not all(options[key] for key in ("producer_tools", "server_id", "spool", "output")):
            raise CommandError("Requires --producer-tools, --server-id, --spool and --output (or AC_* env vars)")
        if options["poll_seconds"] <= 0:
            raise CommandError("--poll-seconds must be positive")
        if connection.vendor != "postgresql" or not connection.get_autocommit():
            raise CommandError("Worker requires a PostgreSQL session with autocommit")
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        uploader = None
        if options["upload"]:
            try:
                uploader = Uploader(S3Storage.from_settings(), options["server_id"])
            except ImproperlyConfigured as error:
                raise CommandError(str(error)) from error
        validator = load_validator(options["producer_tools"])
        server, _ = Server.objects.update_or_create(pk=options["server_id"], defaults={"name": options["server_name"]})
        worker = Worker(validator, server.id, options["spool"], options["output"])
        try:
            while True:
                processed, failed = worker.run_once()
                self.stdout.write(f"Pass complete: processed={processed} failed={failed}")
                upload_failed = 0
                if uploader:
                    stored, upload_failed = uploader.run_once()
                    self.stdout.write(f"Upload pass complete: stored={stored} failed={upload_failed}")
                if options["once"]:
                    if failed or upload_failed:
                        raise CommandError(f"{failed} journal(s) and {upload_failed} upload(s) failed; see database and logs")
                    return
                time.sleep(options["poll_seconds"])
        except KeyboardInterrupt:
            return
