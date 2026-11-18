from contextlib import contextmanager
from math import ceil

import psycopg
from django.conf import settings
from django.utils import timezone
from psycopg import sql
from psycopg.rows import dict_row

from .steam import steam_id_variants

PAGE_SIZE = 30


class BanSourceUnavailable(Exception):
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


def load_bans(raw_page, *, query="", steam_ids=()):
    try:
        requested_page = max(1, int(raw_page or 1))
    except ValueError:
        requested_page = 1
    filters = [sql.SQL("p.punishment_type = 'ban'")]
    params = []
    steam_ids = steam_ids or steam_id_variants(query)
    if steam_ids:
        filters.append(sql.SQL("UPPER(p.target_steam_id) = ANY(%s)"))
        params.append(list(steam_ids))
    elif query:
        filters.append(sql.SQL(
            "(STRPOS(LOWER(p.target_name), LOWER(%s)) > 0 OR STRPOS(LOWER(p.target_steam_id), LOWER(%s)) > 0)"
        ))
        params.extend((query, query))
    conditions = sql.SQL(" AND ").join(filters)

    try:
        with psycopg.connect(
            settings.BEAUTY_BANS_DATABASE_URL,
            connect_timeout=3,
            options="-c default_transaction_read_only=on -c statement_timeout=4000",
            row_factory=dict_row,
        ) as connection:
            connection.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("SELECT COUNT(*) AS total FROM punishments_punishment AS p WHERE {}").format(conditions),
                    params,
                )
                total = cursor.fetchone()["total"]
                number = min(requested_page, max(1, ceil(total / PAGE_SIZE)))
                cursor.execute(
                    sql.SQL("""
                    SELECT p.id, p.target_name, p.target_steam_id, p.reason,
                           p.issued_at, p.expires_at, p.duration, p.is_active,
                           s.name AS server_name, a.name AS admin_name
                    FROM punishments_punishment AS p
                    JOIN servers_server AS s ON s.id = p.server_id
                    LEFT JOIN admins_admin AS a ON a.id = p.admin_id
                    WHERE {}
                    ORDER BY p.issued_at DESC, p.id DESC
                    LIMIT %s OFFSET %s
                    """).format(conditions),
                    [*params, PAGE_SIZE, (number - 1) * PAGE_SIZE],
                )
                bans = cursor.fetchall()
    except (psycopg.Error, ValueError) as error:
        raise BanSourceUnavailable from error

    now = timezone.now()
    for ban in bans:
        expires_at = ban["expires_at"]
        ban["active"] = ban["is_active"] and (expires_at is None or expires_at > now)
    return {
        "bans": bans,
        "number": number,
        "previous": number - 1 if number > 1 else None,
        "following": number + 1 if number * PAGE_SIZE < total else None,
    }
