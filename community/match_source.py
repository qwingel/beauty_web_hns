from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlsplit

from django.conf import settings

MODES = {0: "Skill", 1: "Boost"}

# The UID carries the finish epoch before the producer queues the SQL insert.
MATCH_SELECT = """
    SELECT id, `map`, style, duration, rated, winner, time_a, time_b,
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


def live_match(row):
    ended_at = datetime.fromtimestamp(int(row["finished_epoch"]), timezone.utc)
    return {
        "id": row["id"], "map": row["map"], "mode": MODES.get(row["style"], "Микс"),
        "ended_at": ended_at, "started_at": ended_at - timedelta(seconds=row["duration"]),
    }
