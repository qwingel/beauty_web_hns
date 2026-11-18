from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import render
from django.views.decorators.http import require_safe

from .demo import CALIBRATION_MATCHES, DEMO_RANKS, PLAYERS, demo_matches, demo_mode_stats, player_match_history


@require_safe
def home(request):
    return render(request, "community/home.html", {
        "server": {
            "map": "hns_floppytown",
            "image": "community/hns_floppytown.jpg",
            "image_source": "https://gamebanana.com/mods/93209",
            "players": 12,
            "capacity": 20,
            "mode": "Микс · Skill",
            "address": settings.GAME_SERVER_ADDRESS,
        },
        "metrics": (
            {"label": "Матчей сегодня", "value": "24"},
            {"label": "Игроков за сутки", "value": "86"},
            {"label": "Игроков всего", "value": "1 248"},
        ),
        "matches": demo_matches(),
    })


@login_required(login_url="profile-login")
@require_safe
def profile(request):
    return render(request, "community/profile.html", {"nickname": request.user.get_username()})


@require_safe
def player_profile(request, nickname):
    player = next((player for player in PLAYERS if player["name"] == nickname), None)
    if player is None:
        raise Http404
    histories = {entry["name"]: player_match_history(entry["name"]) for entry in PLAYERS}
    history = histories[nickname]
    ratings = []
    for mode in ("skill", "boost"):
        mode_stats = {name: demo_mode_stats(name, mode, matches) for name, matches in histories.items()}
        stats = mode_stats[nickname]
        rated = [
            entry for entry in PLAYERS
            if mode_stats[entry["name"]]["games"] >= CALIBRATION_MATCHES
        ]
        rank = 1 + sum(entry[mode] > player[mode] for entry in rated) if stats["games"] >= CALIBRATION_MATCHES else None
        decided = stats["wins"] + stats["losses"]
        ratings.append({
            "mode": mode.title(), "value": player[mode], "stats": stats,
            "rank": rank, "rank_total": len(rated),
            "tier": next((name for minimum, name in DEMO_RANKS if player[mode] >= minimum), "") if rank else "",
            "remaining": max(0, CALIBRATION_MATCHES - stats["games"]), "calibration_matches": CALIBRATION_MATCHES,
            "winrate": round(100 * stats["wins"] / decided) if decided else None,
        })
    captain_matches = [match for match in history if match["captain"] == nickname]
    return render(request, "community/profile.html", {
        "nickname": nickname, "player": player, "ratings": ratings, "matches": history,
        "latest_match": history[0] if history else None,
        "captain_count": len(captain_matches),
        "captain_wins": sum(match["winner"] == nickname for match in captain_matches),
    })


@require_safe
def ranking(request):
    mode = request.GET.get("mode", "skill")
    if mode not in {"skill", "boost"}:
        mode = "skill"
    query = request.GET.get("q", "").strip()[:128]
    players = [
        {**player, "rank": rank, "rating": player[mode]}
        for rank, player in enumerate(sorted(PLAYERS, key=lambda entry: entry[mode], reverse=True), 1)
        if query.casefold() in player["name"].casefold() or query.casefold() in player["steamid"].casefold()
    ]
    return render(request, "community/ranking.html", {"players": players, "mode": mode, "query": query})


@require_safe
def matches(request):
    entries = demo_matches()
    maps = sorted({match["map"] for match in entries})
    mode = request.GET.get("mode", "")
    map_name = request.GET.get("map", "")
    period = request.GET.get("period", "")
    if mode in {"Skill", "Boost"}:
        entries = [match for match in entries if match["mode"] == mode]
    if map_name in maps:
        entries = [match for match in entries if match["map"] == map_name]
    if period in {"1", "7"}:
        entries = [match for match in entries if match["days_ago"] < int(period)]
    return render(request, "community/matches.html", {
        "matches": entries, "maps": maps, "mode": mode, "map_name": map_name, "period": period,
    })


@require_safe
def match_detail(request, match_id):
    match = next((match for match in demo_matches() if match["id"] == match_id), None)
    if match is None:
        raise Http404
    match["mvp_stats"] = next(
        (player for _, _, roster in match["teams"] for player in roster if player.name == match["mvp"]),
        None,
    )
    return render(request, "community/match.html", {"match": match})
