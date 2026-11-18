import logging

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_safe

from .ban_source import BanSourceUnavailable, load_bans
from .forms import BanLookupForm, UnbanTicketForm
from .models import UnbanTicket
from .steam import steam_id_variants

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
    ticket = get_object_or_404(UnbanTicket.objects.select_related("author"), pk=ticket_id)
    return render(request, "community/unban_ticket.html", {"ticket": ticket})


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
