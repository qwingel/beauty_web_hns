from functools import wraps
import logging
import math

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, F, Max, OuterRef, Q, Subquery
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_safe

from community.ban_access import player_roles, request_access

from .archives import read_item, records
from .forms import SessionCommentForm
from .match_history import MatchSourceUnavailable, detection_match_ids, history, match_by_id, match_sessions
from .models import Decision, MovementKind, Player, Segment
from .session_stats import session_statistics
from .viewer import archive_path

LOG = logging.getLogger(__name__)


def ac_access(view):
    @never_cache
    @login_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        access = request_access(request)
        if not access or not access.anticheat:
            return render(request, "anticheat/forbidden.html", status=403)
        return view(request, *args, **kwargs)
    return wrapped


def page(request, queryset, size=30):
    return Paginator(queryset, size).get_page(request.GET.get("page"))


def connections():
    return Segment.objects.select_related("journal__server", "player", "archive").annotate(
        detections=Count("decisions", filter=Q(decisions__payload__detect=1)))


@ac_access
@require_safe
def index(request):
    query = request.GET.get("q", "").strip()[:128]
    if not query:
        detections = list(Decision.objects.filter(payload__detect=1).select_related("segment__journal").order_by(
            "-segment__started_at", "-sequence", "-pk",
        )[:10])
        roles = player_roles(decision.segment.steamid for decision in detections)
        for decision in detections:
            decision.player_role = roles.get(decision.segment.steamid)
        context = {"detections": detections}
        try:
            context["matches"], context["previous"], context["following"] = history(request.GET.get("page"))
        except MatchSourceUnavailable:
            LOG.warning("Match history source unavailable")
            context["match_error"] = True
        if not context.get("match_error"):
            try:
                match_ids = detection_match_ids(decision.segment for decision in detections)
            except MatchSourceUnavailable:
                LOG.warning("Detection match IDs unavailable")
            else:
                for decision in detections:
                    decision.match_id = match_ids.get(decision.segment_id)
        return render(request, "anticheat/index.html", context)

    latest = Segment.objects.filter(player_id=OuterRef("pk")).order_by(F("started_at").desc(nulls_last=True), "-id")
    players = Player.objects.annotate(
        latest_name=Subquery(latest.values("name")[:1]),
    )
    exact = players.filter(Q(steamid__iexact=query) | Q(latest_name__iexact=query))
    if exact.exists():
        players = exact
    else:
        matching_players = Segment.objects.filter(name__icontains=query).values("player_id")
        players = players.filter(Q(steamid__icontains=query) | Q(pk__in=matching_players))
    players = players.annotate(
        last_seen=Max("segments__started_at"),
        connections=Count("segments", distinct=True)).order_by("-last_seen", "id")
    entries = page(request, players)
    roles = player_roles(player.steamid for player in entries)
    for player in entries:
        player.role = roles.get(player.steamid)
    return render(request, "anticheat/index.html", {"query": query, "players": entries})


@ac_access
@require_safe
def match_detail(request, match_id):
    try:
        match = match_by_id(match_id)
        if match is None:
            raise Http404
        sessions = match_sessions(connections(), match)
    except MatchSourceUnavailable:
        LOG.warning("Match source unavailable for %s", match_id)
        return render(request, "anticheat/match.html", {"match_error": True}, status=503)
    participants = [
        {**player, "sessions": sessions.get(player["steamid"], ())}
        for player in match["participants"]
    ]
    return render(request, "anticheat/match.html", {
        "match": match, "participants": participants,
        "has_match_server": bool(settings.AC_MATCH_SERVER_ID),
    })


@ac_access
@require_safe
def player_detail(request, player_id):
    player = get_object_or_404(Player, pk=player_id)
    history = connections().filter(player=player).order_by("-started_at", "-id")
    return render(request, "anticheat/player.html", {"player": player, "history": page(request, history),
                  "latest": history.first(), "detection_count": Decision.objects.filter(
                      segment__player=player, payload__detect=1).count()})


@ac_access
@require_http_methods(["GET", "HEAD", "POST"])
def segment_detail(request, segment_id):
    segment = get_object_or_404(connections(), pk=segment_id)
    kind = MovementKind.GSTRAFE if request.GET.get("stats") == "gstrafe" else MovementKind.BHOP
    if request.method in {"GET", "HEAD"} and request.headers.get("X-Session-Statistics") == "fragment":
        return render(request, "anticheat/session_statistics.html", {"summary": session_statistics(segment, kind)})

    comment_form = SessionCommentForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and comment_form.is_valid():
        segment.comments.create(author=request.user, body=comment_form.cleaned_data["body"])
        params = request.GET.copy()
        params.pop("comments_page", None)
        query = f"?{params.urlencode()}" if params else ""
        return redirect(f"{request.path}{query}#session-comments")

    match_id = request.GET.get("match", "")
    comments = Paginator(segment.comments.select_related("author"), 30).get_page(request.GET.get("comments_page"))
    context = {"segment": segment, "comment_form": comment_form, "comments": comments}
    status = 400 if comment_form.is_bound else 200
    if match_id.isascii() and match_id.isdecimal() and len(match_id) <= 10:
        context.update(match_id=match_id, match_query=f"&match={match_id}")
    if request.GET.get("tab") == "patterns":
        return render_patterns(request, context, status=status)
    decisions = segment.decisions.order_by("sequence")
    if request.GET.get("detected") == "1":
        decisions = decisions.filter(payload__detect=1)
    return render(request, "anticheat/segment.html", {**context, "summary": session_statistics(segment, kind),
                  "decisions": page(request, decisions), "detected_only": request.GET.get("detected") == "1"}, status=status)


def window_info(data):
    center = (data["w"] - data["kind"]) // 2
    return {**data, "center": center, "label": "Bhop" if data["kind"] == 0 else "Gstrafe",
            "partial": data["first"] != center - 15 or data["last"] != center + 15}


def pattern_cells(center, buttons, event):
    cells = []
    for offset in range(-15, 16):
        value = buttons.get(center + offset)
        state = "missing" if value is None else "command" if int(value) & (2 | 4) else "idle"
        fog = None
        if offset == 0 and event is not None:
            ideal = event.get("ideal")
            state = "perfect" if ideal == 1 else "fail" if ideal == 0 else "event"
            if type(event.get("fog")) is int:
                fog = event["fog"]
        cells.append({"offset": offset, "state": state, "fog": fog})
    return cells


def render_patterns(request, context, status=200):
    segment = context["segment"]
    kind = request.GET.get("kind", "")
    if kind not in {"", "0", "1"}:
        raise Http404
    context.update(show_patterns=True, kind=kind)
    if not hasattr(segment, "archive"):
        return render(request, "anticheat/segment.html", {**context, "pattern_error": "Архив ещё не создан."}, status=503)
    try:
        number = max(1, min(int(request.GET.get("page", "1")), 1000000))
    except ValueError:
        number = 1
    size = 40
    start = (number - 1) * size
    count, selected = 0, {}
    try:
        with archive_path(segment.archive) as path:
            fields = read_item(path, "fields")["commands"]
            cmd_index, button_index = fields.index("cmd"), fields.index("buttons")
            for row in records(path):
                data = row["data"]
                if data.get("s") != segment.segment_number:
                    continue
                if data["t"] == "window" and (not kind or data["kind"] == int(kind)):
                    if start <= count < start + size:
                        selected[data["w"]] = {**window_info(data), "buttons": {}}
                    count += 1
                elif data["t"] == "cmd" and data["w"] in selected:
                    selected[data["w"]]["buttons"][data["v"][cmd_index]] = data["v"][button_index]
            events = {}
            if selected:
                for row in records(path):
                    data = row["data"]
                    if data.get("s") != segment.segment_number:
                        continue
                    if data["t"] == "bhop":
                        command = data.get("anchor", data.get("c"))
                        event_kind = 0
                    elif data["t"] == "gstrafe":
                        command = data.get("c")
                        event_kind = 1
                    else:
                        continue
                    if type(command) is not int:
                        continue
                    window_id = command * 2 + event_kind
                    if window_id in selected:
                        events[window_id] = data
    except Exception:
        LOG.exception("Cannot read patterns for segment %s", segment.id)
        context["pattern_error"] = "Архив недоступен или не прошёл проверку целостности."
        return render(request, "anticheat/segment.html", context, status=503)
    for window_id, pattern in selected.items():
        pattern["cells"] = pattern_cells(pattern["center"], pattern.pop("buttons"), events.get(window_id))
    context.update(patterns=selected.values(), page_number=number,
                   previous=number - 1 if number > 1 else None, following=number + 1 if start + size < count else None)
    return render(request, "anticheat/segment.html", context, status=status)


@ac_access
@require_safe
def windows(request, segment_id, window_id=None):
    segment = get_object_or_404(connections(), pk=segment_id)
    if not hasattr(segment, "archive"):
        return render(request, "anticheat/windows.html", {"segment": segment, "error": "Архив ещё не создан."}, status=503)
    kind = request.GET.get("kind", "")
    if kind not in {"", "0", "1"}:
        raise Http404
    context = {"segment": segment, "kind": kind}
    try:
        with archive_path(segment.archive) as path:
            if window_id is None:
                # Keep only one page of window metadata, never the command array.
                try:
                    number = max(1, int(request.GET.get("page", "1")))
                except ValueError:
                    number = 1
                number = min(number, 1000000)
                start, count, items = (number - 1) * 40, 0, []
                for row in records(path):
                    data = row["data"]
                    if data["t"] != "window" or data["s"] != segment.segment_number:
                        continue
                    if kind and data["kind"] != int(kind):
                        continue
                    if start <= count < start + 40:
                        items.append(window_info(data))
                    count += 1
                context.update(windows=items, page_number=number, total=count,
                               previous=number - 1 if number > 1 else None, following=number + 1 if start + 40 < count else None)
            else:
                fields = read_item(path, "fields")["commands"]
                selected, commands = None, []
                for row in records(path):
                    data = row["data"]
                    if data.get("s") != segment.segment_number or data.get("w") != window_id:
                        continue
                    if data["t"] == "window":
                        selected = window_info(data)
                    elif data["t"] == "cmd":
                        if len(commands) >= 31 or len(data["v"]) != len(fields):
                            raise ValueError("Неверный формат командного окна.")
                        command = dict(zip(fields, data["v"]))
                        command["raw_values"] = data["v"]
                        command["speed"] = math.hypot(command["velocity_x"], command["velocity_y"])
                        command["post_speed"] = math.hypot(command["end_velocity_x"], command["end_velocity_y"])
                        command["keys"] = " + ".join(label for mask, label in ((2, "JUMP"), (4, "DUCK"), (8, "FORWARD"),
                            (16, "BACK"), (512, "LEFT"), (1024, "RIGHT")) if command["buttons"] & mask) or "—"
                        commands.append(command)
                if selected is None:
                    raise Http404
                for command in commands:
                    command["offset"] = command["cmd"] - selected["center"]
                context.update(selected=selected, commands=commands, fields=fields,
                               missing=len(commands) != selected["last"] - selected["first"] + 1)
    except Http404:
        raise
    except Exception:
        LOG.exception("Cannot read archive for segment %s", segment.id)
        context = {"segment": segment, "error": "Архив недоступен или не прошёл проверку целостности. Попробуйте позже."}
        return render(request, "anticheat/windows.html", context, status=503)
    return render(request, "anticheat/windows.html", context)
