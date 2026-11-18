from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .ban_access import lookup_access
from .ban_source import ban_cursor
from .models import UnbanTicket

TICKET_BAN = """
    SELECT p.id, p.admin_id, p.target_name, p.target_steam_id, p.reason,
           p.is_active, p.expires_at, p.unban_reason,
           s.name AS server_name, a.name AS admin_name
    FROM punishments_punishment AS p
    JOIN servers_server AS s ON s.id = p.server_id
    LEFT JOIN admins_admin AS a ON a.id = p.admin_id
    WHERE p.id = %s AND p.punishment_type = 'ban'
"""


class ReviewForbidden(Exception):
    pass


class ReviewConflict(Exception):
    pass


def ticket_ban(ban_id):
    with ban_cursor(settings.BEAUTY_BANS_DATABASE_URL, read_only=True) as cursor:
        cursor.execute(TICKET_BAN, (ban_id,))
        ban = cursor.fetchone()
    if ban is not None:
        ban["active"] = ban["is_active"] and not ban_expired(ban)
    return ban


def ban_expired(ban):
    return ban["expires_at"] is not None and ban["expires_at"] <= timezone.now()


def can_review(access, ticket, ban):
    if not access or not access.anticheat:
        return False
    return access.review_all or (
        ticket.status == UnbanTicket.Status.OPEN and ban is not None and access.admin_id == ban["admin_id"]
    )


def apply_ban_decision(cursor, ban, ticket, admin_id, status):
    # The marker makes retries safe and limits restoration to this ticket's unban.
    marker = f"Appeal ticket #{ticket.pk}"
    removed_by_ticket = not ban["is_active"] and ban["unban_reason"] == marker
    expired = ban_expired(ban)
    if status == UnbanTicket.Status.APPROVED:
        if not ban["is_active"] or expired:
            return
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
    if not user.is_active or not user.steam_id:
        raise ReviewForbidden
    ticket = UnbanTicket.objects.select_for_update().get(pk=ticket_id)
    with ban_cursor(settings.BEAUTY_BANS_REVIEW_DATABASE_URL, read_only=False) as cursor:
        access = lookup_access(user, cursor)
        if not access or not access.anticheat:
            raise ReviewForbidden
        cursor.execute(TICKET_BAN + " FOR UPDATE OF p", (ticket.ban_id,))
        ban = cursor.fetchone()
        if not can_review(access, ticket, ban):
            raise ReviewForbidden
        if ticket.review_version != version:
            raise ReviewConflict("Тикет уже изменился. Обновите страницу.")
        if ticket.status == status:
            return
        if ban is None and status == UnbanTicket.Status.APPROVED:
            raise ReviewConflict("Бан не найден в Beauty Bans.")
        if ban is not None:
            apply_ban_decision(cursor, ban, ticket, access.admin_id, status)
        ticket.status = status
        ticket.reviewed_by = user
        ticket.reviewed_at = timezone.now()
        ticket.review_version += 1
        ticket.save(update_fields=("status", "reviewed_by", "reviewed_at", "review_version"))
