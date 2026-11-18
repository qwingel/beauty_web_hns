from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.paginator import Paginator
from django.db.models import Q

from community.demo import PLAYERS, demo_matches

MOSCOW = ZoneInfo("Europe/Moscow")
# The UID carries the finish epoch before the producer queues the SQL insert.
MATCH_SELECT = """
    SELECT id, `map`, style, duration,
           CASE WHEN uid REGEXP '^[0-9]{9,11}-'
                THEN CAST(SUBSTRING_INDEX(uid, '-', 1) AS UNSIGNED)
                ELSE UNIX_TIMESTAMP(finished)
           END AS finished_epoch
    FROM beauty_mix_matches
"""


class MatchSourceUnavailable(Exception):
    pass


@contextmanager
def stats_cursor():
    try:
        import pymysql
    except ImportError as error:
        raise MatchSourceUnavailable from error

    try:
        url = urlsplit(settings.AC_MATCH_DATABASE_URL)
        if url.scheme != "mysql" or not url.hostname or not url.path.strip("/"):
            raise ValueError("Invalid match database URL")
        connection = pymysql.connect(
            host=url.hostname, port=url.port or 3306, user=unquote(url.username or ""),
            password=unquote(url.password or ""), database=unquote(url.path[1:]),
            charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=3, read_timeout=5,
        )
        try:
            with connection.cursor() as cursor:
                yield cursor
        finally:
            connection.close()
    except (pymysql.Error, ValueError) as error:
        raise MatchSourceUnavailable from error


def demo_history():
    matches = demo_matches()
    by_name = {player["name"]: player["steamid"] for player in PLAYERS}
    for match in matches:
        match["started_at"] = datetime.combine(match["date"], datetime.strptime(match["time"], "%H:%M").time(), MOSCOW)
        match["ended_at"] = match["started_at"] + timedelta(minutes=match["duration"])
        match["participants"] = [
            {"name": player.name, "steamid": by_name[player.name]}
            for _, _, roster in match["teams"] for player in roster
        ]
    return sorted(matches, key=lambda match: (match["ended_at"], match["id"]), reverse=True)


def live_match(row):
    ended_at = datetime.fromtimestamp(int(row["finished_epoch"]), timezone.utc)
    return {
        "id": row["id"], "map": row["map"], "mode": {0: "Skill", 1: "Boost"}.get(row["style"], "Микс"),
        "ended_at": ended_at, "started_at": ended_at - timedelta(seconds=row["duration"]),
    }


def history(page_number, size=30):
    if not settings.AC_MATCH_DATABASE_URL:
        entries = Paginator(demo_history(), size).get_page(page_number)
        previous = entries.previous_page_number() if entries.has_previous() else None
        following = entries.next_page_number() if entries.has_next() else None
        return entries.object_list, previous, following
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
        return next((match for match in demo_history() if match["id"] == match_id), None)
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
        matches = demo_history()
        result = {}
        for pk, segment in candidates.items():
            ids = [
                match["id"] for match in matches
                if match["map"].casefold() == segment.journal.map_name.casefold()
                and segment.started_at < match["ended_at"]
                and (segment.ended_at > match["started_at"] if segment.ended_at
                     else segment.started_at >= match["started_at"])
                and any(player["steamid"] == segment.steamid for player in match["participants"])
            ]
            if len(ids) == 1:
                result[pk] = ids[0]
        return result

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
