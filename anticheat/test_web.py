import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.test import TransactionTestCase, override_settings
from django.urls import reverse

from .archives import load_validator
from .models import Archive, Segment, Server
from .views import PERMISSIONS
from .worker import Worker


class WebTests(TransactionTestCase):
    def setUp(self):
        scratch = Path.home() / ".codex/scratchpad"
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        tools = os.getenv("AC_PRODUCER_TOOLS", str(Path(__file__).resolve().parents[2] / "beauty-server/tools"))
        self.validator = load_validator(tools)
        spool = self.root / "spool"
        spool.mkdir()
        data = [{"t": "archive", "schema": 1, "started": 1700000000, "nonce": 123, "map": "hns_test"},
                {"t": "start", "s": 1, "steamid": "STEAM_0:1:123", "name": "<script>nick</script>", "at": 1700000000}]
        for kind in (0, 1):
            first, last = (1, 31) if kind == 0 else (14, 18)
            data.append({"t": "window", "s": 1, "w": 32 + kind, "kind": kind, "first": first, "last": last})
            for command in range(first, last + 1):
                values = [command] + [0] * (len(self.validator.COMMAND_FIELDS) - 1)
                values[2] = 6
                values[13] = 250.25
                data.append({"t": "cmd", "s": 1, "w": 32 + kind, "v": values})
            data.append({"t": "stats", "s": 1, "kind": kind, "scope": 1,
                         "v": [0] * len((self.validator.BHOP_FIELDS, self.validator.GSTRAFE_FIELDS)[kind])})
        data.extend([{"t": "decision", "s": 1, "c": 16, "kind": 0, "phase": 2, "reason": 3, "detect": 1},
                     {"t": "end", "s": 1, "at": 1700000100}, {"t": "archive_end"}])
        (spool / "sample.ready").write_bytes(b"".join(self.validator.encode({"q": i, "data": row}) + b"\n"
                                                     for i, row in enumerate(data, 1)))
        server = Server.objects.create(name="Test server")
        Worker(self.validator, server.id, spool, self.root / "archives").run_once()
        self.segment = Segment.objects.get()
        self.player = self.segment.player
        self.user = get_user_model().objects.create_user(username="reviewer", password="example-password", is_staff=True)
        codenames = [permission.split(".")[1] for permission in PERMISSIONS]
        self.user.user_permissions.set(Permission.objects.filter(content_type__app_label="anticheat", codename__in=codenames))
        self.urls = [reverse("anticheat:index"), reverse("anticheat:player", args=[self.player.id]),
                     reverse("anticheat:segment", args=[self.segment.id]), reverse("anticheat:windows", args=[self.segment.id]),
                     reverse("anticheat:window", args=[self.segment.id, 32])]

    def test_anonymous_cannot_read_any_private_page(self):
        for url in self.urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.url.startswith("/anticheat/login/"))

    def test_staff_without_permissions_and_regular_users_are_forbidden(self):
        other = get_user_model().objects.create_user(username="other", is_staff=True)
        self.client.force_login(other)
        for url in self.urls:
            self.assertEqual(self.client.get(url).status_code, 403)
        self.user.is_staff = False
        self.user.save()
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.urls[0]).status_code, 403)

    def test_all_pages_render_and_are_not_cacheable(self):
        self.client.force_login(self.user)
        for url in self.urls:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertIn("no-store", response["Cache-Control"])
            self.assertNotContains(response, "<script>nick</script>")

    def test_search_by_steamid_and_name(self):
        self.client.force_login(self.user)
        for query in ("STEAM_0:1:123", "nick"):
            self.assertContains(self.client.get(self.urls[0], {"q": query}), "STEAM_0:1:123")
        self.assertContains(self.client.get(self.urls[0], {"q": "unknown"}), "Ничего не найдено")

    def test_historical_nickname_search_keeps_full_connection_count(self):
        from uuid import uuid4
        Segment.objects.create(id=uuid4(), journal=self.segment.journal, segment_number=2, player=self.player,
                               name="Other nickname", metadata={})
        self.client.force_login(self.user)
        response = self.client.get(self.urls[0], {"q": "<script>nick"})
        self.assertEqual(response.context["players"][0].connections, 2)

    def test_partial_window_and_recorded_buttons_are_visible(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("anticheat:window", args=[self.segment.id, 33]))
        self.assertContains(response, "Неполное окно")
        self.assertContains(response, "JUMP + DUCK")
        self.assertEqual(len(response.context["commands"]), 5)
        self.assertEqual(response.context["commands"][2]["offset"], 0)
        self.assertEqual(response.context["commands"][0]["speed"], 250.25)

    def test_window_filters_and_invalid_selection(self):
        self.client.force_login(self.user)
        response = self.client.get(self.urls[3], {"kind": "1"})
        self.assertEqual(len(response.context["windows"]), 1)
        self.assertEqual(response.context["windows"][0]["kind"], 1)
        self.assertEqual(self.client.get(reverse("anticheat:window", args=[self.segment.id, 999])).status_code, 404)
        self.assertEqual(self.client.get(self.urls[3], {"kind": "invalid"}).status_code, 404)

    def test_corrupt_archive_is_rejected_without_exposing_path(self):
        self.client.force_login(self.user)
        path = Path(self.segment.archive.local_path)
        path.write_bytes(b"corrupt")
        with self.assertLogs("anticheat.views", level="ERROR"):
            response = self.client.get(self.urls[3])
        self.assertEqual(response.status_code, 503)
        self.assertNotContains(response, str(path), status_code=503)

    def test_verified_s3_fallback_when_local_file_is_missing(self):
        import boto3
        from moto import mock_aws
        from .storage import S3Storage
        from .uploader import Uploader

        self.client.force_login(self.user)
        with mock_aws():
            client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing")
            client.create_bucket(Bucket="web-test")
            storage = S3Storage(client, "web-test")
            Uploader(storage).run_once()
            Path(self.segment.archive.local_path).unlink()
            with patch("anticheat.viewer.S3Storage.from_settings", return_value=storage):
                self.assertEqual(self.client.get(self.urls[-1]).status_code, 200)

    def test_login_redirect_cannot_leave_site_and_logout_requires_post(self):
        response = self.client.post(reverse("login"), {"username": "reviewer", "password": "example-password", "next": "https://example.com"})
        self.assertRedirects(response, self.urls[0])
        self.assertEqual(self.client.get(reverse("logout")).status_code, 405)
        self.assertEqual(self.client.post(reverse("logout")).status_code, 302)

    def test_viewer_limit_is_enforced(self):
        self.client.force_login(self.user)
        with override_settings(AC_VIEWER_MAX_ARCHIVE_BYTES=1), self.assertLogs("anticheat.views", level="ERROR"):
            self.assertEqual(self.client.get(self.urls[3]).status_code, 503)
