import logging
import os
import time
from uuid import UUID

from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from anticheat.storage import S3Storage
from anticheat.uploader import Uploader


class Command(BaseCommand):
    help = "Upload and verify pending AC archives independently of journal collection."

    def add_arguments(self, parser):
        parser.add_argument("--server-id", type=UUID, default=os.getenv("AC_SERVER_ID"))
        parser.add_argument("--poll-seconds", type=float, default=10)
        parser.add_argument("--once", action="store_true")

    def handle(self, **options):
        if options["poll_seconds"] <= 0:
            raise CommandError("--poll-seconds must be positive")
        if connection.vendor != "postgresql" or not connection.get_autocommit():
            raise CommandError("Uploader requires a PostgreSQL session with autocommit")
        try:
            storage = S3Storage.from_settings()
        except ImproperlyConfigured as error:
            raise CommandError(str(error)) from error
        uploader = Uploader(storage, options["server_id"])
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        try:
            while True:
                stored, failed = uploader.run_once()
                self.stdout.write(f"Upload pass complete: stored={stored} failed={failed}")
                if options["once"]:
                    if failed:
                        raise CommandError(f"{failed} upload(s) failed; see database and logs")
                    return
                time.sleep(options["poll_seconds"])
        except KeyboardInterrupt:
            return
