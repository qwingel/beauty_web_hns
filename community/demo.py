from datetime import timedelta
from typing import NamedTuple
from zoneinfo import ZoneInfo

from django.utils import timezone


PLAYERS = (
    {"name": "shiver", "steamid": "STEAM_0:1:23456781", "skill": 2184, "boost": 1472, "winrate": 68},
    {"name": "motion", "steamid": "STEAM_0:1:23456782", "skill": 2108, "boost": 1762, "winrate": 67},
    {"name": "velocity", "steamid": "STEAM_0:1:23456783", "skill": 1884, "boost": 1628, "winrate": 62},
    {"name": "qwingel", "steamid": "STEAM_0:1:23456789", "skill": 1576, "boost": 1394, "winrate": 60},
)

PROFILE_STATS = {
    "qwingel": {
        "skill": {"games": 96, "wins": 57, "losses": 38, "tt": "01:54", "last": "00:21",
                  "grade": 7.2, "mvp": 14, "stabs": 613, "ttk": 30.8, "escapes": 82, "survived": 34},
        "boost": {"games": 48, "wins": 25, "losses": 20, "tt": "01:38", "last": "00:16",
                  "grade": 6.9, "mvp": 6, "stabs": 304, "ttk": 34.1, "escapes": 36, "survived": 28},
    },
}
CALIBRATION_MATCHES = 10
DEMO_RANKS = (
    # Beauty Mix rank thresholds for the preview.
    (1800, "Crown"), (1550, "Alexandrite"), (1400, "Ruby"), (1250, "Sapphire"),
    (1150, "Opal"), (1050, "Amethyst"), (950, "Garnet"), (850, "Topaz"),
    (750, "Jade"), (650, "Amber"), (0, "Quartz"),
)

class MatchPlayer(NamedTuple):
    name: str
    rating: float
    pts: int
    pts_delta: int
    tt: str
    last: str
    escapes: int
    stabs: int
    ttk: float | None
    flashed: int
    fall_ct: int
    fall_tt: int


MATCHES = (
    {"id": 1042, "map": "hns_floppytown", "mode": "Skill", "time": "18:42", "days_ago": 0, "duration": 18,
     "teams": (
         ("shiver", "08:42", (
             MatchPlayer("shiver", rating=8.7, pts=2184, pts_delta=24, tt="02:43", last="00:31",
                         escapes=4, stabs=11, ttk=23.6, flashed=8, fall_ct=1, fall_tt=0),
             MatchPlayer("motion", rating=7.9, pts=2108, pts_delta=18, tt="02:18", last="00:21",
                         escapes=3, stabs=8, ttk=29.1, flashed=5, fall_ct=0, fall_tt=2),
         )),
         ("velocity", "07:51", (
             MatchPlayer("velocity", rating=7.5, pts=1884, pts_delta=-12, tt="02:26", last="00:28",
                         escapes=2, stabs=9, ttk=25.3, flashed=6, fall_ct=2, fall_tt=1),
             MatchPlayer("qwingel", rating=6.8, pts=1576, pts_delta=-16, tt="01:54", last="00:10",
                         escapes=1, stabs=6, ttk=34.2, flashed=3, fall_ct=1, fall_tt=2),
         )),
     ), "mvp": "shiver", "winner": "shiver"},
    {"id": 1041, "map": "hns_mie", "mode": "Boost", "time": "18:10", "days_ago": 2, "duration": 16,
     "teams": (
         ("velocity", "06:18", (
             MatchPlayer("velocity", rating=7.1, pts=1628, pts_delta=-12, tt="02:18", last="00:17",
                         escapes=2, stabs=7, ttk=27.4, flashed=4, fall_ct=0, fall_tt=2),
             MatchPlayer("shiver", rating=6.4, pts=1472, pts_delta=-10, tt="01:40", last="00:09",
                         escapes=1, stabs=5, ttk=32.8, flashed=2, fall_ct=1, fall_tt=1),
         )),
         ("motion", "06:45", (
             MatchPlayer("motion", rating=8.3, pts=1762, pts_delta=22, tt="02:35", last="00:37",
                         escapes=3, stabs=9, ttk=22.5, flashed=5, fall_ct=0, fall_tt=1),
             MatchPlayer("qwingel", rating=7.2, pts=1394, pts_delta=17, tt="01:58", last="00:24",
                         escapes=2, stabs=7, ttk=28.1, flashed=3, fall_ct=1, fall_tt=0),
         )),
     ), "mvp": "motion", "winner": "motion"},
    {"id": 1040, "map": "hns_tyo", "mode": "Skill", "time": "17:36", "days_ago": 9, "duration": 21,
     "teams": (
         ("qwingel", "09:12", (
             MatchPlayer("qwingel", rating=9.0, pts=1592, pts_delta=26, tt="03:14", last="00:54",
                         escapes=5, stabs=12, ttk=21.8, flashed=9, fall_ct=0, fall_tt=1),
             MatchPlayer("velocity", rating=7.6, pts=1896, pts_delta=17, tt="02:10", last="00:22",
                         escapes=3, stabs=8, ttk=26.7, flashed=4, fall_ct=1, fall_tt=2),
         )),
         ("shiver", "08:36", (
             MatchPlayer("shiver", rating=7.3, pts=2160, pts_delta=-13, tt="02:43", last="00:32",
                         escapes=2, stabs=9, ttk=28.2, flashed=5, fall_ct=1, fall_tt=1),
             MatchPlayer("motion", rating=6.9, pts=2090, pts_delta=-16, tt="02:03", last="00:14",
                         escapes=1, stabs=7, ttk=31.9, flashed=3, fall_ct=2, fall_tt=0),
         )),
     ), "mvp": "qwingel", "winner": "qwingel"},
)


def demo_matches():
    today = timezone.localdate(timezone=ZoneInfo("Europe/Moscow"))
    return [
        {"rated": True, **match, "date": today - timedelta(days=match["days_ago"])}
        for match in MATCHES
    ]


def player_match_history(nickname):
    history = [
        {**match, "captain": captain, "performance": player}
        for match in demo_matches()
        for captain, _, roster in match["teams"]
        for player in roster
        if player.name == nickname
    ]
    return sorted(history, key=lambda match: (match["date"], match["time"], match["id"]), reverse=True)


def average_time(values):
    seconds = [int(minutes) * 60 + int(seconds) for minutes, seconds in (value.split(":") for value in values)]
    minutes, remainder = divmod(round(sum(seconds) / len(seconds)), 60)
    return f"{minutes:02d}:{remainder:02d}"


def demo_mode_stats(nickname, mode, history):
    if stored := PROFILE_STATS.get(nickname, {}).get(mode):
        return stored
    entries = [match for match in history if match["rated"] and match["mode"].lower() == mode]
    stats = {
        "games": len(entries),
        "wins": sum(match["winner"] == match["captain"] for match in entries),
        "losses": sum(bool(match["winner"]) and match["winner"] != match["captain"] for match in entries),
    }
    if entries:
        players = [match["performance"] for match in entries]
        measured_stabs = sum(player.stabs for player in players if player.ttk is not None)
        stats.update(
            tt=average_time(player.tt for player in players),
            last=average_time(player.last for player in players),
            grade=sum(player.rating for player in players) / len(players),
            mvp=sum(match["mvp"] == nickname for match in entries),
            stabs=sum(player.stabs for player in players),
            ttk=sum(player.ttk * player.stabs for player in players if player.ttk is not None) / measured_stabs
            if measured_stabs else None,
            escapes=sum(player.escapes for player in players),
            survived=None,
        )
    return stats
