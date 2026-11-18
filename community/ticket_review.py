from contextlib import contextmanager

import psycopg
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from psycopg.rows import dict_row

from .ban_source import BanSourceUnavailable
from .models import UnbanTicket

TICKET_BAN = """
    SELECT p.id, p.admin_id, p.target_name, p.target_steam_id, p.reason,
           p.is_active, p.expires_at, p.unban_reason,
           s.name AS server_name, a.name AS admin_name, a.is_active AS admin_active
    FROM punishments_punishment AS p
    JOIN servers_server AS s ON s.id = p.server_id
    LEFT JOIN admins_admin AS a ON a.id = p.admin_id
    WHERE p.id = %s AND p.punishment_type = 'ban'
"""


class ReviewForbidden(Exception):
    pass


class ReviewConflict(Exception):
    pass


@contextmanager
def ban_cursor(dsn, *, read_only):
    if not dsn:
        raise BanSourceUnavailable
    options = "-c statement_timeout=4000 -c lock_timeout=3000"
    if read_only:
        options += " -c default_transaction_read_only=on"
    try:
        with psycopg.connect(dsn, connect_timeout=3, options=options, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                yield cursor
    except (psycopg.Error, ValueError) as error:
        raise BanSourceUnavailable from error


def ticket_ban(ban_id):
    with ban_cursor(settings.BEAUTY_BANS_DATABASE_URL, read_only=True) as cursor:
        cursor.execute(TICKET_BAN, (ban_id,))
        ban = cursor.fetchone()
    if ban is not None:
        ban["active"] = ban["is_active"] and not ban_expired(ban)
    return ban


def ban_expired(ban):
    return ban["expires_at"] is not None and ban["expires_at"] <= timezone.now()


def has_review_role(user, ticket):
    return user.is_staff and user.is_active and (
        user.has_perm("community.review_all_unbantickets")
        or (ticket.status == UnbanTicket.Status.OPEN and user.beauty_bans_admin_id is not None)
    )


def can_review(user, ticket, ban):
    if not has_review_role(user, ticket):
        return False
    if user.has_perm("community.review_all_unbantickets"):
        return True
    return ban is not None and ban["admin_active"] and user.beauty_bans_admin_id == ban["admin_id"]


def apply_ban_decision(cursor, ban, ticket, user, status):
    # The marker makes retries safe and limits restoration to this ticket's unban.
    marker = f"Appeal ticket #{ticket.pk}"
    removed_by_ticket = not ban["is_active"] and ban["unban_reason"] == marker
    expired = ban_expired(ban)
    if status == UnbanTicket.Status.APPROVED:
        if not ban["is_active"] or expired:
            return
        admin_id = user.beauty_bans_admin_id
        if admin_id is not None:
            cursor.execute("SELECT id FROM admins_admin WHERE id = %s AND is_active", (admin_id,))
            if cursor.fetchone() is None:
                admin_id = None
        cursor.execute(
            """
            UPDATE punishments_punishment
            SET is_active = false, unban_reason = %s, unbanned_by_id = %s
            WHERE id = %s
            """,
            (marker, admin_id, ban["id"]),
        )
    elif removed_by_ticket and not expired:
        cursor.execute(
            """
            UPDATE punishments_punishment
            SET is_active = true, unban_reason = '', unbanned_by_id = NULL
            WHERE id = %s
            """,
            (ban["id"],),
        )


@transaction.atomic
def review_ticket(ticket_id, user, status, version):
    ticket = UnbanTicket.objects.select_for_update().get(pk=ticket_id)
    if not has_review_role(user, ticket):
        raise ReviewForbidden
    with ban_cursor(settings.BEAUTY_BANS_REVIEW_DATABASE_URL, read_only=False) as cursor:
        cursor.execute(TICKET_BAN + " FOR UPDATE OF p", (ticket.ban_id,))
        ban = cursor.fetchone()
        if not can_review(user, ticket, ban):
            raise ReviewForbidden
        if ticket.review_version != version:
            raise ReviewConflict("Тикет уже изменился. Обновите страницу.")
        if ticket.status == status:
            return
        if ban is None and status == UnbanTicket.Status.APPROVED:
            raise ReviewConflict("Бан не найден в Beauty Bans.")
        if ban is not None:
            apply_ban_decision(cursor, ban, ticket, user, status)
        ticket.status = status
        ticket.reviewed_by = user
        ticket.reviewed_at = timezone.now()
        ticket.review_version += 1
        ticket.save(update_fields=("status", "reviewed_by", "reviewed_at", "review_version"))
