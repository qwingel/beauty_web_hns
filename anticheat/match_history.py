from uuid import UUID

from django.conf import settings
from django.db.models import Q

from community.match_source import MATCH_SELECT, MatchSourceUnavailable, live_match, stats_cursor


def history(page_number, size=30):
    if not settings.AC_MATCH_DATABASE_URL:
        return [], None, None
    try:
        number = max(1, min(int(page_number or 1), 1000000))
    except ValueError:
        number = 1
    with stats_cursor() as cursor:
        cursor.execute(
            f"{MATCH_SELECT} ORDER BY finished_epoch DESC, id DESC LIMIT %s OFFSET %s",
            (size + 1, (number - 1) * size),
        )
        rows = cursor.fetchall()
    return [live_match(row) for row in rows[:size]], number - 1 if number > 1 else None, number + 1 if len(rows) > size else None


def match_by_id(match_id):
    if not settings.AC_MATCH_DATABASE_URL:
        return None
    with stats_cursor() as cursor:
        cursor.execute(f"{MATCH_SELECT} WHERE id = %s", (match_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        match = live_match(row)
        cursor.execute(
            "SELECT steamid, name FROM beauty_mix_match_players WHERE match_id = %s ORDER BY team, name, steamid",
            (match_id,),
        )
        match["participants"] = cursor.fetchall()
    return match


def match_sessions(queryset, match):
    if not settings.AC_MATCH_SERVER_ID:
        return {}
    try:
        server_id = UUID(settings.AC_MATCH_SERVER_ID)
    except ValueError as error:
        raise MatchSourceUnavailable from error

    steamids = {participant["steamid"] for participant in match["participants"] if participant["steamid"]}
    sessions = queryset.filter(
        journal__server_id=server_id, journal__map_name__iexact=match["map"],
        started_at__lt=match["ended_at"], steamid__in=steamids,
    ).filter(
        Q(ended_at__gt=match["started_at"]) |
        Q(ended_at__isnull=True, started_at__gte=match["started_at"])
    ).order_by("-started_at", "-pk")
    by_steamid = {}
    for session in sessions:
        by_steamid.setdefault(session.steamid, []).append(session)
    return by_steamid


def detection_match_ids(segments):
    # A session may span several matches, and a decision has no wall-clock timestamp.
    if not settings.AC_MATCH_SERVER_ID:
        return {}
    try:
        server_id = UUID(settings.AC_MATCH_SERVER_ID)
    except ValueError as error:
        raise MatchSourceUnavailable from error
    candidates = {
        segment.pk: segment for segment in segments
        if segment.started_at and segment.steamid and segment.journal.server_id == server_id
    }
    if not candidates:
        return {}

    if not settings.AC_MATCH_DATABASE_URL:
        return {}

    result = {}
    with stats_cursor() as cursor:
        for pk, segment in candidates.items():
            started = segment.started_at.timestamp()
            ended = segment.ended_at.timestamp() if segment.ended_at else started
            operator = "<" if segment.ended_at else "<="
            cursor.execute(
                f"""SELECT matches.id FROM ({MATCH_SELECT}) AS matches
                INNER JOIN beauty_mix_match_players AS roster ON roster.match_id = matches.id
                WHERE roster.steamid = %s AND LOWER(matches.`map`) = LOWER(%s)
                  AND matches.finished_epoch > %s
                  AND matches.finished_epoch - matches.duration {operator} %s
                LIMIT 2""",
                (segment.steamid, segment.journal.map_name, started, ended),
            )
            rows = cursor.fetchall()
            if len(rows) == 1:
                result[pk] = rows[0]["id"]
    return result
