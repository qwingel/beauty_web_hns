import re
import secrets
from datetime import datetime, timezone
from http.client import HTTPSConnection, HTTPException
from time import time
from urllib.parse import urlencode, urlsplit

from django.conf import settings
from django.contrib.auth import get_user_model, login
from django.contrib.auth.hashers import make_password
from django.contrib.auth.views import LoginView
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from community.steam import steam_id_variants

STEAM_HOST = "steamcommunity.com"
STEAM_PATH = "/openid/login"
STEAM_ENDPOINT = f"https://{STEAM_HOST}{STEAM_PATH}"
OPENID_NAMESPACE = "http://specs.openid.net/auth/2.0"
IDENTIFIER_SELECT = f"{OPENID_NAMESPACE}/identifier_select"
STATE_KEY = "steam_login"
MAX_AGE = 600
REQUIRED_SIGNED_FIELDS = {"op_endpoint", "return_to", "response_nonce", "assoc_handle", "claimed_id", "identity"}


class SteamUnavailable(Exception):
    pass


def login_origin():
    origin = settings.STEAM_LOGIN_ORIGIN
    parsed = urlsplit(origin)
    if not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username is not None:
        return ""
    if parsed.scheme == "https" or (
        settings.DEBUG and parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        return origin
    return ""


class SiteLoginView(LoginView):
    def dispatch(self, request, *args, **kwargs):
        if login_origin():
            target = self.get_success_url()
            if request.user.is_authenticated:
                return redirect(target)
            response = redirect(f'{reverse("steam-login")}?{urlencode({"next": target})}')
            response["Cache-Control"] = "no-store"
            return response
        return super().dispatch(request, *args, **kwargs)


def next_page(request):
    target = request.GET.get("next", "")
    if target.startswith("/") and url_has_allowed_host_and_scheme(target, allowed_hosts=set(), require_https=True):
        return target
    return reverse("profile")


def callback_url(origin, state):
    return f'{origin}{reverse("steam-return")}?{urlencode({"state": state})}'


def login_page(request, message, *, status=200, target=None):
    return render(request, "community/steam.html", {
        "message": message, "steam_enabled": bool(login_origin()),
        "next": target or reverse("profile"),
    }, status=status)


@never_cache
@require_safe
def start(request):
    target = next_page(request)
    if request.user.is_authenticated:
        return redirect(target)
    origin = login_origin()
    if not origin:
        return login_page(request, "Вход через Steam пока недоступен.")
    if request.method != "GET" or request.build_absolute_uri(request.path) != origin + request.path:
        return login_page(request, "Откройте вход на основном адресе сайта.", status=400, target=target)
    state = secrets.token_urlsafe(32)
    request.session[STATE_KEY] = {"state": state, "next": target, "created": time()}
    params = {
        "openid.ns": OPENID_NAMESPACE,
        "openid.mode": "checkid_setup",
        "openid.return_to": callback_url(origin, state),
        "openid.realm": f"{origin}/",
        "openid.claimed_id": IDENTIFIER_SELECT,
        "openid.identity": IDENTIFIER_SELECT,
    }
    return redirect(f"{STEAM_ENDPOINT}?{urlencode(params)}")


def verify_signature(fields):
    connection = HTTPSConnection(STEAM_HOST, timeout=5)
    payload = urlencode({**fields, "openid.mode": "check_authentication"})
    try:
        connection.request("POST", STEAM_PATH, payload, {"Content-Type": "application/x-www-form-urlencoded"})
        response = connection.getresponse()
        if response.status != 200:
            raise SteamUnavailable
        return b"is_valid:true" in response.read(4096).splitlines()
    except (OSError, HTTPException) as error:
        raise SteamUnavailable from error
    finally:
        connection.close()


def verified_steam_id(query, return_to):
    fields = {}
    for key, values in query.lists():
        if key.startswith("openid."):
            if len(values) != 1:
                return None
            fields[key] = values[0]
    if (
        fields.get("openid.ns") != OPENID_NAMESPACE or fields.get("openid.mode") != "id_res"
        or fields.get("openid.op_endpoint") != STEAM_ENDPOINT or fields.get("openid.return_to") != return_to
        or not REQUIRED_SIGNED_FIELDS.issubset(fields.get("openid.signed", "").split(","))
        or not fields.get("openid.assoc_handle") or not fields.get("openid.sig")
    ):
        return None
    claimed = fields.get("openid.claimed_id", "")
    match = re.fullmatch(r"https?://steamcommunity\.com/openid/id/([0-9]{17})", claimed)
    if not match or claimed != fields.get("openid.identity") or not steam_id_variants(match[1]):
        return None
    try:
        nonce_time = datetime.strptime(
            fields.get("openid.response_nonce", "")[:20], "%Y-%m-%dT%H:%M:%SZ",
        ).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None
    if abs(time() - nonce_time) > MAX_AGE or not verify_signature(fields):
        return None
    return match[1]


@never_cache
@require_safe
def complete(request):
    origin = login_origin()
    flow = request.session.get(STATE_KEY)
    state = request.GET.getlist("state")
    if (
        request.method != "GET" or not origin or request.build_absolute_uri(request.path) != origin + request.path
        or not flow or len(state) != 1 or not secrets.compare_digest(flow["state"], state[0])
        or time() - flow["created"] > MAX_AGE
    ):
        return login_page(request, "Срок входа истёк. Попробуйте ещё раз.", status=400)
    del request.session[STATE_KEY]
    target = flow["next"]
    if request.GET.get("openid.mode") == "cancel":
        return login_page(request, "Вход через Steam отменён.", target=target)
    try:
        steam_id = verified_steam_id(request.GET, callback_url(origin, flow["state"]))
    except SteamUnavailable:
        return login_page(request, "Steam временно недоступен.", status=503, target=target)
    if not steam_id:
        return login_page(request, "Steam не подтвердил вход.", status=400, target=target)
    user, _ = get_user_model().objects.get_or_create(
        steam_id=steam_id, defaults={"username": f"steam_{steam_id}_{secrets.token_hex(6)}", "password": make_password(None)},
    )
    if not user.is_active:
        return login_page(request, "Вход для этого аккаунта недоступен.", status=403, target=target)
    login(request, user)
    return redirect(target)
