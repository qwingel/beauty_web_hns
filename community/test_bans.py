from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import connection
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from psycopg.conninfo import make_conninfo

from .ban_source import BanSourceUnavailable, load_bans
from .models import UnbanTicket


class BanPagesTests(TestCase):
    @override_settings(BEAUTY_BANS_DATABASE_URL="")
    def test_missing_source_is_explicit(self):
        self.assertContains(self.client.get(reverse("banlist")), "Источник Beauty Bans не подключён.")

    @override_settings(BEAUTY_BANS_DATABASE_URL="postgresql://beautybans")
    def test_source_error_does_not_show_connection_details(self):
        with patch("community.ban_views.load_bans", side_effect=BanSourceUnavailable("private connection details")):
            response = self.client.get(reverse("banlist"))
        self.assertContains(response, "Банлист временно недоступен.", status_code=503)
        self.assertNotContains(response, "private connection details", status_code=503)

    def test_tickets_are_visible_to_any_signed_in_user(self):
        users = get_user_model().objects
        owner = users.create_user(username="owner")
        reader = users.create_user(username="reader")
        ticket = UnbanTicket.objects.create(author=owner, ban_id=7, subject="Review request", message="Appeal comment")
        listing = reverse("unban-tickets")
        detail = reverse("unban-ticket", args=[ticket.pk])

        self.assertEqual(self.client.get(detail).status_code, 302)
        self.assertEqual(self.client.post(reverse("unban-ticket-create")).status_code, 302)
        self.client.force_login(owner)
        self.assertContains(self.client.get(listing), ticket.subject)
        response = self.client.get(detail)
        self.assertContains(response, ticket.message)
        self.assertIn("no-store", response["Cache-Control"])
        self.client.force_login(reader)
        self.assertContains(self.client.get(listing), ticket.subject)
        self.assertContains(self.client.get(detail), ticket.message)


class BeautyBansSourceTests(TransactionTestCase):
    def setUp(self):
        options = connection.settings_dict
        self.dsn = make_conninfo(
            dbname=options["NAME"], host=options["HOST"], port=options["PORT"],
            user=options["USER"], password=options["PASSWORD"],
        )
        with connection.cursor() as cursor:
            cursor.execute("CREATE TABLE servers_server (id integer PRIMARY KEY, name text)")
            cursor.execute("CREATE TABLE admins_admin (id integer PRIMARY KEY, name text, is_active boolean)")
            cursor.execute("""
                CREATE TABLE punishments_punishment (
                    id integer PRIMARY KEY, punishment_type text, target_name text, target_steam_id text,
                    reason text, issued_at timestamptz, expires_at timestamptz, duration integer,
                    is_active boolean, server_id integer, admin_id integer,
                    unban_reason text DEFAULT '', unbanned_by_id integer
                )
            """)
            cursor.execute("INSERT INTO servers_server VALUES (1, 'HNS')")
            cursor.execute("INSERT INTO admins_admin VALUES (1, 'Reviewer', true), (2, 'Other admin', true)")
        self.addCleanup(self.drop_source)

    def drop_source(self):
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE punishments_punishment, admins_admin, servers_server")

    def test_reads_beauty_bans_tables_and_excludes_other_punishments(self):
        with connection.cursor() as cursor:
            cursor.execute("""
                INSERT INTO punishments_punishment
                SELECT n, 'ban', 'Player', 'STEAM_0:1:123', 'Review',
                       now() - interval '1 day', now() - interval '1 hour', 60, true, 1, 1
                FROM generate_series(1, 31) AS n
            """)
            cursor.execute("""
                INSERT INTO punishments_punishment VALUES
                    (32, 'ban', 'Permanent', 'STEAM_0:1:321', 'Permanent', now(), NULL, 0, true, 1, NULL),
                    (33, 'mute', 'Hidden mute', 'STEAM_0:1:322', 'Mute', now(), NULL, 0, true, 1, 1)
            """)
        with override_settings(BEAUTY_BANS_DATABASE_URL=self.dsn):
            first = load_bans("1")
            second = load_bans("999")
            response = self.client.get(reverse("banlist"))
            matched = self.client.get(reverse("banlist"), {"q": "perMAN"})
            aliases = self.client.get(reverse("banlist"), {"q": "76561197960266371"})
            self.assertContains(matched, "Permanent")
            self.assertNotContains(matched, "STEAM_0:1:123")
            self.assertContains(aliases, "STEAM_0:1:321")
            page = self.client.get(reverse("banlist"), {"q": "Player"})
            self.assertContains(page, "q=Player&amp;page=2")

            user = get_user_model().objects.create_user(username="appealing-player")
            self.client.force_login(user)
            create = reverse("unban-ticket-create")
            form = self.client.get(create, {"steam_id": "STEAM_1:1:321"})
            self.assertContains(form, "Почему нужно разбанить?")
            self.assertContains(form, "#32")
            self.assertNotContains(form, "Hidden mute")
            forged = self.client.post(create, {"steam_id": "STEAM_0:1:321", "ban_id": 31, "message": "Appeal"})
            self.assertEqual(forged.status_code, 400)
            self.assertFalse(UnbanTicket.objects.exists())
            csrf_client = Client(enforce_csrf_checks=True)
            csrf_client.force_login(user)
            self.assertEqual(csrf_client.post(create, {"steam_id": "[U:1:643]", "ban_id": 32, "message": "Appeal"}).status_code, 403)

            submitted = self.client.post(create, {"steam_id": "[U:1:643]", "ban_id": 32, "message": '<script>Appeal</script>'})
            ticket = UnbanTicket.objects.get()
            self.assertEqual((ticket.ban_id, ticket.author_id, ticket.message), (32, user.pk, "<script>Appeal</script>"))
            self.assertRedirects(submitted, reverse("unban-ticket", args=[ticket.pk]))
            self.assertContains(self.client.get(submitted.url), "&lt;script&gt;Appeal&lt;/script&gt;")
        self.assertEqual([ban["id"] for ban in first["bans"]], [32, *range(31, 2, -1)])
        self.assertEqual([ban["id"] for ban in second["bans"]], [2, 1])
        self.assertTrue(first["bans"][0]["active"])
        self.assertFalse(first["bans"][1]["active"])
        self.assertContains(response, "Permanent")
        self.assertNotContains(response, "Hidden mute")

    def test_issuer_reviews_once_and_head_can_reverse_the_exact_ban(self):
        with connection.cursor() as cursor:
            cursor.execute("""
                INSERT INTO punishments_punishment VALUES
                    (1, 'ban', 'Player', 'STEAM_0:1:123', 'Review', now(), NULL, 0, true, 1, 1),
                    (2, 'ban', 'Player', 'STEAM_0:1:123', 'Separate ban', now(), NULL, 0, true, 1, 2)
            """)
        users = get_user_model().objects
        player = users.create_user(username="player")
        issuer = users.create_user(username="issuer", is_staff=True, beauty_bans_admin_id=1)
        other = users.create_user(username="other", is_staff=True, beauty_bans_admin_id=2)
        head = users.create_user(username="head", is_staff=True)
        head.user_permissions.add(Permission.objects.get(
            content_type__app_label="community", codename="review_all_unbantickets",
        ))
        ticket = UnbanTicket.objects.create(author=player, ban_id=1, subject="Appeal", message="Please review")
        detail = reverse("unban-ticket", args=[ticket.pk])
        review = reverse("unban-ticket-review", args=[ticket.pk])
        approval = {"status": "approved", "version": 0}
        with override_settings(BEAUTY_BANS_DATABASE_URL=self.dsn, BEAUTY_BANS_REVIEW_DATABASE_URL=self.dsn):
            for user in (player, other):
                self.client.force_login(user)
                self.assertNotContains(self.client.get(detail), "Разбанить")
                self.assertEqual(self.client.post(review, approval).status_code, 403)

            self.client.force_login(issuer)
            self.assertContains(self.client.get(detail), "Разбанить")
            self.assertContains(self.client.get(detail), "Отклонить")
            self.assertEqual(self.client.get(review).status_code, 405)
            csrf_client = Client(enforce_csrf_checks=True)
            csrf_client.force_login(issuer)
            self.assertEqual(csrf_client.post(review, approval).status_code, 403)
            self.assertRedirects(self.client.post(review, approval), detail)
            ticket.refresh_from_db()
            self.assertEqual((ticket.status, ticket.reviewed_by, ticket.review_version), ("approved", issuer, 1))
            self.assertIsNotNone(ticket.reviewed_at)
            self.assertNotContains(self.client.get(detail), "Отклонить")
            self.assertEqual(self.client.post(review, {"status": "rejected", "version": 1}).status_code, 403)
            with connection.cursor() as cursor:
                cursor.execute("SELECT is_active, unbanned_by_id FROM punishments_punishment ORDER BY id LIMIT 2")
                self.assertEqual(cursor.fetchall(), [(False, 1), (True, None)])

            self.client.force_login(head)
            self.assertContains(self.client.get(detail), "Отклонить")
            stale = self.client.post(review, {"status": "rejected", "version": 0})
            self.assertContains(stale, "Тикет уже изменился", status_code=409)
            self.assertRedirects(self.client.post(review, {"status": "rejected", "version": 1}), detail)
            ticket.refresh_from_db()
            self.assertEqual((ticket.status, ticket.reviewed_by, ticket.review_version), ("rejected", head, 2))
            with connection.cursor() as cursor:
                cursor.execute("SELECT is_active, unban_reason FROM punishments_punishment WHERE id = 1")
                self.assertEqual(cursor.fetchone(), (True, ""))

            self.assertRedirects(self.client.post(review, {"status": "approved", "version": 2}), detail)
            with connection.cursor() as cursor:
                cursor.execute("UPDATE punishments_punishment SET expires_at = now() WHERE id = 1")
            self.assertRedirects(self.client.post(review, {"status": "rejected", "version": 3}), detail)
            with connection.cursor() as cursor:
                cursor.execute("SELECT is_active FROM punishments_punishment WHERE id = 1")
                self.assertEqual(cursor.fetchone(), (False,))
