from functools import wraps
import logging
import math

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, Max, OuterRef, Q, Subquery
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from .archives import read_item, records
from .models import Decision, Player, Segment
from .viewer import archive_path

LOG = logging.getLogger(__name__)
PERMISSIONS = tuple(f"anticheat.view_{model}" for model in ("player", "segment", "summary", "decision", "archive", "journal"))


def ac_access(view):
    @never_cache
    @login_required
    @require_safe
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_active or not request.user.is_staff or not request.user.has_perms(PERMISSIONS):
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapped


def page(request, queryset, size=30):
    return Paginator(queryset, size).get_page(request.GET.get("page"))


def connections():
    return Segment.objects.select_related("journal__server", "player", "archive").annotate(
        detections=Count("decisions", filter=Q(decisions__payload__detect=1)))


@ac_access
def index(request):
    query = request.GET.get("q", "").strip()[:128]
    latest = Segment.objects.filter(player_id=OuterRef("pk")).order_by("-started_at", "-id")
    players = Player.objects.all()
    if query:
        matching_players = Segment.objects.filter(name__icontains=query).values("player_id")
        players = players.filter(Q(steamid__icontains=query) | Q(pk__in=matching_players))
    players = players.annotate(
        latest_name=Subquery(latest.values("name")[:1]), last_seen=Max("segments__started_at"),
        connections=Count("segments", distinct=True)).order_by("-last_seen", "id")
    return render(request, "anticheat/index.html", {"query": query, "players": page(request, players),
                  "recent": connections().order_by("-started_at", "-id")[:10] if not query else []})


@ac_access
def player_detail(request, player_id):
    player = get_object_or_404(Player, pk=player_id)
    history = connections().filter(player=player).order_by("-started_at", "-id")
    return render(request, "anticheat/player.html", {"player": player, "history": page(request, history)})


@ac_access
def segment_detail(request, segment_id):
    segment = get_object_or_404(connections(), pk=segment_id)
    summaries = [{"kind": summary.get_kind_display(), "counters": summary.counters.items()}
                 for summary in segment.summaries.order_by("kind")]
    decisions = segment.decisions.order_by("sequence")
    if request.GET.get("detected") == "1":
        decisions = decisions.filter(payload__detect=1)
    return render(request, "anticheat/segment.html", {"segment": segment, "summaries": summaries,
                  "decisions": page(request, decisions), "detected_only": request.GET.get("detected") == "1"})


def window_info(data):
    center = (data["w"] - data["kind"]) // 2
    return {**data, "center": center, "label": "Bhop" if data["kind"] == 0 else "Gstrafe",
            "partial": data["first"] != center - 15 or data["last"] != center + 15}


@ac_access
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
