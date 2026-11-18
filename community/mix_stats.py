from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone

from .match_source import MATCH_SELECT, MODES, live_match, stats_cursor
from .steam import steam_id_variants

MOSCOW = ZoneInfo("Europe/Moscow")
STYLES = {"skill": "", "boost": "_boost"}
PAGE_SIZE = 30


def clock_time(seconds):
    minutes, seconds = divmod(round(seconds), 60)
    return f"{minutes:02d}:{seconds:02d}"


def start_of_day(days_ago=0):
    day = timezone.localdate(timezone=MOSCOW) - timedelta(days=days_ago)
    return int(datetime.combine(day, time.min, MOSCOW).timestamp())


def page_number(raw_page):
    try:
        return max(1, min(int(raw_page or 1), 1000000))
    except ValueError:
        return 1


def player_identity(row):
    steamid = row["steamid"]
    variants = steam_id_variants(steamid)
    return {"name": row["name"] or steamid, "steamid": steamid, "profile_id": variants[-1] if variants else ""}


def match_summary(row, captains):
    match = live_match(row)
    finished = match["ended_at"].astimezone(MOSCOW)
    teams = [
        {"captain": captains.get(team) or label, "score": clock_time(row[field]), "won": row["winner"] == team}
        for team, label, field in ((0, "A", "time_a"), (1, "B", "time_b"))
    ]
    return {
        **match, "date": finished.date(), "time": finished.strftime("%H:%M"),
        "duration": clock_time(row["duration"]), "rated": bool(row["rated"]), "teams": teams,
        "winner": next((team["captain"] for team in teams if team["won"]), ""),
    }


def summarize_matches(cursor, rows):
    if not rows:
        return []
    ids = tuple(row["id"] for row in rows)
    cursor.execute(
        """SELECT match_id, team, name FROM beauty_mix_match_players
           WHERE match_id IN %s AND captain = 1 ORDER BY finished DESC, steamid""",
        (ids,),
    )
    captains = {}
    for player in cursor.fetchall():
        captains.setdefault(player["match_id"], {}).setdefault(player["team"], player["name"])
    return [match_summary(row, captains.get(row["id"], {})) for row in rows]


def home_stats():
    with stats_cursor() as cursor:
        cursor.execute(f"SELECT COUNT(*) AS total FROM ({MATCH_SELECT}) AS m WHERE finished_epoch >= %s", (start_of_day(),))
        today = cursor.fetchone()["total"]
        cursor.execute(f"""
            SELECT COUNT(DISTINCT p.steamid) AS total FROM beauty_mix_match_players AS p
            JOIN ({MATCH_SELECT}) AS m ON m.id = p.match_id WHERE m.finished_epoch >= %s
        """, (int(timezone.now().timestamp()) - 86400,))
        daily_players = cursor.fetchone()["total"]
        cursor.execute("""
            SELECT COUNT(DISTINCT player_steamid) AS total
            FROM beauty_player_info
            WHERE player_steamid NOT IN ('', 'BOT', 'HLTV', 'STEAM_ID_LAN', 'STEAM_ID_PENDING',
                                          'VALVE_ID_LAN', 'VALVE_ID_PENDING')
        """)
        total_players = int(cursor.fetchone()["total"])
        cursor.execute(f"{MATCH_SELECT} ORDER BY finished_epoch DESC, id DESC LIMIT 5")
        matches = summarize_matches(cursor, cursor.fetchall())
    return (
        {"label": "Матчей сегодня", "value": today},
        {"label": "Участников миксов за сутки", "value": daily_players},
        {"label": "Игроков всего", "value": total_players},
    ), matches


def match_list(mode, map_name, period, page):
    filters = []
    params = []
    if mode in MODES.values():
        filters.append("style = %s")
        params.append(0 if mode == "Skill" else 1)
    if map_name:
        filters.append("`map` = %s")
        params.append(map_name)
    if period in {"1", "7"}:
        filters.append("finished_epoch >= %s")
        params.append(start_of_day(int(period) - 1))
    where = "WHERE " + " AND ".join(filters) if filters else ""
    with stats_cursor() as cursor:
        cursor.execute("SELECT DISTINCT `map` FROM beauty_mix_matches ORDER BY `map`")
        maps = [row["map"] for row in cursor.fetchall()]
        cursor.execute(
            f"""SELECT * FROM ({MATCH_SELECT}) AS m {where}
                ORDER BY finished_epoch DESC, id DESC LIMIT %s OFFSET %s""",
            (*params, PAGE_SIZE + 1, (page - 1) * PAGE_SIZE),
        )
        rows = cursor.fetchall()
        matches = summarize_matches(cursor, rows[:PAGE_SIZE])
    return matches, maps, len(rows) > PAGE_SIZE


def match_details(match_id):
    with stats_cursor() as cursor:
        cursor.execute(f"{MATCH_SELECT} WHERE id = %s", (match_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        cursor.execute("""
            SELECT steamid, name, team, rating, pts_before, pts_delta, rated, tt_time, last_time,
                   escapes, stabs, ttk, flashed, fall_ct, fall_tt, captain, mvp
            FROM beauty_mix_match_players WHERE match_id = %s
            ORDER BY team, finished DESC, rating DESC, tt_time DESC, steamid
        """, (match_id,))
        roster = cursor.fetchall()
    captains = {}
    for player in roster:
        if player["captain"]:
            captains.setdefault(player["team"], player["name"])
    match = match_summary(row, captains)
    groups = {0: [], 1: []}
    for player in roster:
        entry = {
            **player, **player_identity(player),
            "pts": player["pts_before"] + player["pts_delta"] if player["rated"] else None,
            "tt": clock_time(player["tt_time"]), "last": clock_time(player["last_time"]),
            "ttk": player["ttk"] if player["stabs"] else None,
        }
        if player["team"] in groups:
            groups[player["team"]].append(entry)
        if player["mvp"]:
            match["mvp"] = entry
    for team, group in zip(match["teams"], groups.values()):
        team["roster"] = group
    return match


def winrate(wins, losses):
    return round(wins * 100 / (wins + losses)) if wins + losses else None


def ranking_rows(mode, query, page):
    suffix = STYLES[mode]
    steam_ids = steam_id_variants(query)
    if steam_ids:
        where, params = "WHERE steamid IN %s", (steam_ids,)
    elif query:
        where = "WHERE LOCATE(LOWER(%s), LOWER(name)) > 0 OR LOCATE(LOWER(%s), LOWER(steamid)) > 0"
        params = (query, query)
    else:
        where, params = "", ()
    with stats_cursor() as cursor:
        cursor.execute(f"""
            SELECT * FROM (
                SELECT steamid, name, pts{suffix} AS rating, wins{suffix} AS wins, loss{suffix} AS losses,
                       RANK() OVER (ORDER BY pts{suffix} DESC) AS place
                FROM beauty_mix_players WHERE matches{suffix} >= %s
            ) AS ranked {where}
            ORDER BY place, wins DESC, steamid LIMIT %s OFFSET %s
        """, (settings.MIX_CALIBRATION_MATCHES, *params, PAGE_SIZE + 1, (page - 1) * PAGE_SIZE))
        rows = cursor.fetchall()
    return [{**row, **player_identity(row), "rank": row["place"], "winrate": winrate(row["wins"], row["losses"])}
            for row in rows[:PAGE_SIZE]], len(rows) > PAGE_SIZE


def rank_name(points, rank):
    if not settings.MIX_RANKS_FILE:
        return ""
    try:
        lines = Path(settings.MIX_RANKS_FILE).read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        return ""
    tiers = []
    for line in lines:
        tokens = line.partition("//")[0].split()
        if len(tokens) < 2:
            continue
        minimum, name = tokens[:2]
        if minimum.startswith("#") and minimum[1:].isdigit() and rank <= int(minimum[1:]):
            return name
        if minimum.lstrip("-").isdigit():
            tiers.append((int(minimum), name))
    return next((name for minimum, name in sorted(tiers, reverse=True) if points >= minimum), "")


def mode_rating(cursor, mode, player, totals):
    suffix = STYLES[mode]
    calibration = settings.MIX_CALIBRATION_MATCHES
    points = player[f"pts{suffix}"]
    games, wins, losses = (player[f"{field}{suffix}"] for field in ("matches", "wins", "loss"))
    place, rank_total = None, None
    if games >= calibration:
        cursor.execute(f"""
            SELECT COUNT(*) AS total, COUNT(CASE WHEN pts{suffix} > %s THEN 1 END) + 1 AS place
            FROM beauty_mix_players WHERE matches{suffix} >= %s
        """, (points, calibration))
        rank = cursor.fetchone()
        place, rank_total = rank["place"], rank["total"]
    stats = {"games": games, "wins": wins, "losses": losses}
    if totals:
        samples = totals["samples"]
        stats.update(
            tt=clock_time(totals["tt"] / samples), last=clock_time(totals["last_time"] / samples),
            grade=totals["grade"], mvp=totals["mvp"], stabs=totals["stabs"], ttk=totals["ttk"],
            escapes=totals["escapes"], survived=totals["survived"],
        )
    return {
        "mode": mode.title(), "value": points, "stats": stats, "has_samples": bool(totals),
        "rank": place, "rank_total": rank_total, "tier": rank_name(points, place) if place else "",
        "remaining": max(0, calibration - games), "calibration_matches": calibration,
        "winrate": winrate(wins, losses),
    }


def player_stats(steam_id):
    variants = steam_id_variants(steam_id) if steam_id else ()
    if not variants:
        return {}
    with stats_cursor() as cursor:
        cursor.execute("""
            SELECT * FROM beauty_mix_players WHERE steamid IN %s
            ORDER BY last_match DESC, steamid LIMIT 1
        """, (variants,))
        player = cursor.fetchone()
        cursor.execute(f"""
            SELECT m.*, p.steamid, p.name, p.team, p.rating, p.pts_delta, p.mvp, p.rated AS player_rated
            FROM ({MATCH_SELECT}) AS m JOIN beauty_mix_match_players AS p ON p.match_id = m.id
            WHERE p.steamid IN %s ORDER BY m.finished_epoch DESC, m.id DESC, p.finished DESC, p.steamid
            LIMIT 10
        """, (variants,))
        rows = cursor.fetchall()
        if not player and not rows:
            return {}
        matches = []
        for row in rows:
            match = match_summary(row, {})
            match.update(
                performance=row, mvp=bool(row["mvp"]),
                rated=bool(row["rated"] and row["player_rated"]),
                won=row["team"] == row["winner"] if row["winner"] in (0, 1) else None,
            )
            matches.append(match)
        ratings = []
        if player:
            cursor.execute("""
                SELECT m.style, COUNT(*) AS samples, SUM(p.tt_time) AS tt, SUM(p.last_time) AS last_time,
                       AVG(p.rating) AS grade, SUM(p.mvp) AS mvp, SUM(p.stabs) AS stabs,
                       SUM(p.ttk * p.stabs) / NULLIF(SUM(p.stabs), 0) AS ttk, SUM(p.escapes) AS escapes,
                       ROUND(100 * SUM(p.survived) / NULLIF(SUM(p.tt_rounds), 0)) AS survived
                FROM beauty_mix_match_players AS p JOIN beauty_mix_matches AS m ON m.id = p.match_id
                WHERE p.steamid IN %s AND p.rated = 1 AND m.rated = 1 AND p.finished = 1 AND p.leave_type = 0
                GROUP BY m.style
            """, (variants,))
            totals = {row["style"]: row for row in cursor.fetchall()}
            ratings = [mode_rating(cursor, mode.lower(), player, totals.get(style)) for style, mode in MODES.items()]
    return {
        "player": player_identity(player or rows[0]), "ratings": ratings, "matches": matches,
        "latest_match": matches[0] if matches else None, "captain_count": player["captain"] if player else None,
        "captain_wins": player["captain_wins"] if player else None,
    }
