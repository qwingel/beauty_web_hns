import logging

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from .ban_source import BanSourceUnavailable, load_bans
from .forms import BanLookupForm, UnbanTicketForm
from .models import UnbanTicket
from .steam import steam_id_variants
from .ticket_review import ReviewConflict, ReviewForbidden, can_review, review_ticket, ticket_ban

LOG = logging.getLogger(__name__)


@require_safe
def banlist(request):
    query = request.GET.get("q", "").strip()[:128]
    if not settings.BEAUTY_BANS_DATABASE_URL:
        return render(request, "community/banlist.html", {"source_disabled": True, "query": query})
    try:
        context = load_bans(request.GET.get("page"), query=query)
    except BanSourceUnavailable:
        LOG.warning("Beauty Bans source unavailable")
        return render(request, "community/banlist.html", {"source_unavailable": True, "query": query}, status=503)
    context["query"] = query
    return render(request, "community/banlist.html", context)


@never_cache
@login_required(login_url="profile-login")
@require_safe
def unban_tickets(request):
    tickets = Paginator(UnbanTicket.objects.select_related("author"), 30).get_page(request.GET.get("page"))
    return render(request, "community/unban_tickets.html", {"tickets": tickets})


@never_cache
@login_required(login_url="profile-login")
@require_safe
def unban_ticket(request, ticket_id):
    return render_ticket(request, ticket_id)


def render_ticket(request, ticket_id, *, error="", status=200):
    ticket = get_object_or_404(UnbanTicket.objects.select_related("author", "reviewed_by"), pk=ticket_id)
    try:
        ban = ticket_ban(ticket.ban_id)
    except BanSourceUnavailable:
        ban = None
    allowed = can_review(request.user, ticket, ban)
    writable = bool(settings.BEAUTY_BANS_REVIEW_DATABASE_URL)
    return render(request, "community/unban_ticket.html", {
        "ticket": ticket, "ban": ban, "review_error": error,
        "review_disabled": allowed and not writable, "can_review": allowed and writable,
    }, status=status)


@never_cache
@login_required(login_url="profile-login")
@require_POST
def review_unban_ticket(request, ticket_id):
    status = request.POST.get("status")
    try:
        version = int(request.POST.get("version", ""))
    except ValueError:
        version = -1
    if version < 0 or status not in (UnbanTicket.Status.APPROVED, UnbanTicket.Status.REJECTED):
        return HttpResponseBadRequest("Некорректное решение.")
    try:
        review_ticket(ticket_id, request.user, status, version)
    except UnbanTicket.DoesNotExist as error:
        raise Http404 from error
    except ReviewForbidden as error:
        raise PermissionDenied from error
    except ReviewConflict as error:
        return render_ticket(request, ticket_id, error=str(error), status=409)
    except BanSourceUnavailable:
        LOG.warning("Beauty Bans review source unavailable")
        return render_ticket(request, ticket_id, error="Не удалось рассмотреть тикет. Beauty Bans недоступен.", status=503)
    return redirect("unban-ticket", ticket_id=ticket_id)


@never_cache
@login_required(login_url="profile-login")
@require_http_methods(["GET", "HEAD", "POST"])
def create_unban_ticket(request):
    submitted = request.method == "POST"
    params = request.POST if submitted else request.GET
    lookup = BanLookupForm(params if submitted or "steam_id" in params else None)
    form = UnbanTicketForm(request.POST if submitted else None)
    context = {"lookup": lookup, "form": form}
    status = 400 if submitted else 200
    if not settings.BEAUTY_BANS_DATABASE_URL:
        context["source_disabled"] = True
    elif lookup.is_valid():
        try:
            result = load_bans(request.GET.get("page"), steam_ids=steam_id_variants(lookup.cleaned_data["steam_id"]))
        except BanSourceUnavailable:
            LOG.warning("Beauty Bans ticket lookup unavailable")
            context["source_unavailable"] = True
            status = 503
        else:
            bans = {ban["id"]: ban for ban in result["bans"]}
            form.fields["ban_id"].choices = [("", "Выберите бан")] + [
                (ban["id"], f'#{ban["id"]} · {ban["server_name"]} · {" ".join(ban["reason"].split())[:100]}')
                for ban in bans.values()
            ]
            if submitted and form.is_valid():
                ban = bans[form.cleaned_data["ban_id"]]
                ticket = UnbanTicket.objects.create(
                    author=request.user, ban_id=ban["id"],
                    subject=f'Апелляция: {ban["target_name"] or ban["target_steam_id"]}',
                    message=form.cleaned_data["message"],
                )
                return redirect("unban-ticket", ticket_id=ticket.pk)
            context.update(result, steam_id=lookup.cleaned_data["steam_id"])
    return render(request, "community/unban_ticket_create.html", context, status=status)
