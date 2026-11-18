from datetime import datetime, timezone
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from .steam_login import OPENID_NAMESPACE, REQUIRED_SIGNED_FIELDS, STEAM_ENDPOINT


@override_settings(STEAM_LOGIN_ORIGIN="https://testserver")
class SteamLoginTests(TestCase):
    def assertion(self, target="/bans/tickets/"):
        start = self.client.get(reverse("steam-login"), {"next": target}, secure=True)
        self.assertEqual(start.status_code, 302)
        endpoint = urlsplit(start.url)
        self.assertEqual(endpoint.geturl().split("?")[0], STEAM_ENDPOINT)
        return_to = parse_qs(endpoint.query)["openid.return_to"][0]
        state = parse_qs(urlsplit(return_to).query)["state"][0]
        claimed_id = "https://steamcommunity.com/openid/id/76561197960265975"
        return {
            "state": state,
            "openid.ns": OPENID_NAMESPACE,
            "openid.mode": "id_res",
            "openid.op_endpoint": STEAM_ENDPOINT,
            "openid.return_to": return_to,
            "openid.response_nonce": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") + "test",
            "openid.assoc_handle": "test",
            "openid.claimed_id": claimed_id,
            "openid.identity": claimed_id,
            "openid.signed": ",".join(sorted(REQUIRED_SIGNED_FIELDS)),
            "openid.sig": "test",
        }

    def test_login_creates_an_unprivileged_account_and_preserves_destination(self):
        fields = self.assertion()
        with patch("accounts.steam_login.verify_signature", return_value=True):
            response = self.client.get(reverse("steam-return"), fields, secure=True)
        self.assertRedirects(response, "/bans/tickets/", fetch_redirect_response=False)
        user = get_user_model().objects.get(steam_id="76561197960265975")
        self.assertEqual(self.client.session["_auth_user_id"], str(user.pk))
        self.assertFalse(user.has_usable_password())
        self.assertFalse(user.is_staff)
        self.assertEqual(self.client.get(reverse("steam-return"), fields, secure=True).status_code, 400)

    def test_rejects_mismatched_state_unsigned_identity_and_invalid_signature(self):
        return_path = reverse("steam-return")
        fields = self.assertion()
        with patch("accounts.steam_login.verify_signature", return_value=False) as verify:
            self.assertEqual(self.client.get(return_path, {**fields, "state": "wrong"}, secure=True).status_code, 400)
            unsigned = {**fields, "openid.signed": "op_endpoint,return_to,response_nonce,assoc_handle"}
            self.assertEqual(self.client.get(return_path, unsigned, secure=True).status_code, 400)
            verify.assert_not_called()
            self.assertEqual(self.client.get(return_path, self.assertion(), secure=True).status_code, 400)
            verify.assert_called_once()
        self.assertFalse(get_user_model().objects.exists())

    def test_configured_site_login_uses_steam_and_blocks_external_redirects(self):
        response = self.client.get(reverse("profile-login"), {"next": "https://example.org"}, secure=True)
        self.assertIn(reverse("steam-login"), response.url)
        self.assertNotIn("example.org", response.url)

    def test_existing_session_displays_steam_profile_and_caches_it(self):
        steam_id = "76561197960265975"
        avatar_url = "https://avatars.fastly.steamstatic.com/avatar_full.jpg"
        user = get_user_model().objects.create_user(username="steam_account", steam_id=steam_id)
        self.client.force_login(user)
        self.addCleanup(cache.delete, f"steam-profile:{steam_id}")

        with patch("accounts.steam_profile.HTTPSConnection") as connection:
            steam_response = connection.return_value.getresponse.return_value
            steam_response.status = 200
            steam_response.read.return_value = (
                f"<profile><steamID64>{steam_id}</steamID64><steamID><![CDATA[Player & Co]]></steamID>"
                f"<avatarFull>{avatar_url}</avatarFull></profile>"
            ).encode()
            response = self.client.get(reverse("profile"), secure=True)
            self.assertContains(response, "Player &amp; Co", count=2)
            self.assertContains(response, avatar_url, count=2)
            self.assertContains(response, f'href="https://steamcommunity.com/profiles/{steam_id}/"')
            self.assertContains(self.client.get(reverse("home"), secure=True), avatar_url)
            connection.return_value.request.assert_called_once_with("GET", f"/profiles/{steam_id}/?xml=1")

    def test_steam_profile_failure_keeps_profile_accessible(self):
        steam_id = "76561197960265975"
        user = get_user_model().objects.create_user(username="steam_account", steam_id=steam_id)
        self.client.force_login(user)
        self.addCleanup(cache.delete, f"steam-profile:{steam_id}")

        with patch("accounts.steam_profile.HTTPSConnection") as connection:
            connection.return_value.getresponse.side_effect = TimeoutError
            response = self.client.get(reverse("profile"), secure=True)
            self.assertContains(response, f'<h1 class="subpage-title">{steam_id}</h1>', html=True)
            self.assertNotContains(response, '<img class="avatar"')
