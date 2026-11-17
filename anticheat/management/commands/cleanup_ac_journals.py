import logging
import os
from pathlib import Path
from uuid import UUID

from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from anticheat.archives import load_validator
from anticheat.cleanup import Cleaner
from anticheat.storage import S3Storage


class Command(BaseCommand):
    help = "Remove complete .ready journals after fresh verification of all segment archives in S3."

    def add_arguments(self, parser):
        parser.add_argument("--server-id", type=UUID, default=os.getenv("AC_SERVER_ID"))
        parser.add_argument("--producer-tools", type=Path, default=os.getenv("AC_PRODUCER_TOOLS"))
        parser.add_argument("--spool", type=Path, default=os.getenv("AC_SPOOL"))
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, **options):
        if not all(options[key] for key in ("server_id", "producer_tools", "spool")):
            raise CommandError("Requires --server-id, --producer-tools and --spool (or AC_* env vars)")
        if connection.vendor != "postgresql" or not connection.get_autocommit():
            raise CommandError("Cleaner requires a PostgreSQL session with autocommit")
        try:
            storage = S3Storage.from_settings()
        except ImproperlyConfigured as error:
            raise CommandError(str(error)) from error
        validator = load_validator(options["producer_tools"])
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        removed, failed = Cleaner(storage, validator, options["server_id"], options["spool"]).run_once(options["dry_run"])
        self.stdout.write(f"Cleanup pass complete: {'eligible' if options['dry_run'] else 'removed'}={removed} failed={failed}")
        if failed:
            raise CommandError(f"{failed} cleanup(s) failed; sources retained where present; see database and logs")
