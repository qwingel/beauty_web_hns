import logging

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import render
from django.views.decorators.http import require_safe

from .ban_access import player_roles, request_access
from .match_source import MatchSourceUnavailable
from .mix_stats import home_stats, match_details, match_list, page_number, player_stats, ranking_rows

LOG = logging.getLogger(__name__)


@require_safe
def home(request):
    context = {"server_address": settings.GAME_SERVER_ADDRESS}
    if settings.AC_MATCH_DATABASE_URL:
        try:
            context["metrics"], context["matches"] = home_stats()
        except MatchSourceUnavailable:
            LOG.warning("Home stats source unavailable")
    context["stats_unavailable"] = "metrics" not in context
    return render(request, "community/home.html", context)


@login_required(login_url="profile-login")
@require_safe
def profile(request):
    steam_profile = request.user.steam_profile
    context = {
        "nickname": steam_profile.get("nickname") or request.user.public_name,
        "avatar_url": steam_profile.get("avatar_url"),
        "own_profile": True,
        "steam_profile_id": request.user.steam_id,
    }
    if access := request_access(request):
        context["player_role"] = access.role
    if request.user.steam_id:
        try:
            context.update(player_stats(request.user.steam_id))
        except MatchSourceUnavailable:
            context["stats_unavailable"] = True
    return render(request, "community/profile.html", context)


@require_safe
def player_profile(request, steam_id):
    try:
        stats = player_stats(steam_id)
    except MatchSourceUnavailable:
        return render(request, "community/profile.html", {"stats_unavailable": True}, status=503)
    if not stats:
        raise Http404
    player = stats["player"]
    roles = player_roles([player["steamid"]])
    return render(request, "community/profile.html", {
        **stats, "nickname": player["name"], "steam_profile_id": player["profile_id"],
        "player_role": roles.get(player["steamid"]),
    })


@require_safe
def ranking(request):
    mode = request.GET.get("mode", "skill")
    if mode not in {"skill", "boost"}:
        mode = "skill"
    query = request.GET.get("q", "").strip()[:128]
    page = page_number(request.GET.get("page"))
    context = {"mode": mode, "query": query}
    try:
        players, next_page = ranking_rows(mode, query, page)
        roles = player_roles(player["steamid"] for player in players)
        for player in players:
            player["role"] = roles.get(player["steamid"])
        context["players"] = players
        context.update(previous=page - 1 if page > 1 else None, following=page + 1 if next_page else None)
    except MatchSourceUnavailable:
        context["stats_unavailable"] = True
    return render(request, "community/ranking.html", context)


@require_safe
def matches(request):
    mode = request.GET.get("mode", "")
    map_name = request.GET.get("map", "")[:32]
    period = request.GET.get("period", "")
    page = page_number(request.GET.get("page"))
    context = {"mode": mode, "map_name": map_name, "period": period}
    try:
        context["matches"], context["maps"], next_page = match_list(mode, map_name, period, page)
        context.update(previous=page - 1 if page > 1 else None, following=page + 1 if next_page else None)
    except MatchSourceUnavailable:
        context["stats_unavailable"] = True
    return render(request, "community/matches.html", context)


@require_safe
def match_detail(request, match_id):
    try:
        match = match_details(match_id)
    except MatchSourceUnavailable:
        return render(request, "community/match.html", {"stats_unavailable": True}, status=503)
    if match is None:
        raise Http404
    players = [player for team in match["teams"] for player in team["roster"]]
    roles = player_roles(player["steamid"] for player in players)
    for player in players:
        player["role"] = roles.get(player["steamid"])
    return render(request, "community/match.html", {"match": match})
