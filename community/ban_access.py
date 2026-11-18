from collections import defaultdict
from dataclasses import dataclass

from django.conf import settings

from .ban_source import BanSourceUnavailable, ban_cursor
from .steam import steam_id_variants

REVIEW_GROUPS = {"владелец", "гл. админ", "модератор"}
ANTICHEAT_GROUPS = REVIEW_GROUPS | {"админ"}
ROLE_TONES = {
    "владелец": "owner",
    "гл. админ": "head-admin",
    "админ": "admin",
    "модератор": "moderator",
    "full watcher": "watcher",
}

ACTIVE_ASSIGNMENTS = """
    SELECT a.id, a.steam_id, g.name
    FROM admins_admin AS a
    JOIN admins_adminserver AS p ON p.admin_id = a.id
    JOIN servers_server AS s ON s.id = p.server_id AND s.is_active
    LEFT JOIN admins_admingroup AS g ON g.id = p.group_id
    WHERE a.is_active AND UPPER(a.steam_id) = ANY(%s) AND p.server_id = %s
      AND (p.expires_at IS NULL OR p.expires_at > CURRENT_TIMESTAMP)
"""


def role_badge(label):
    label = (label or "").strip()
    if not label:
        return None
    return {"label": label, "tone": ROLE_TONES.get(label.casefold(), "staff")}


def configured_server_id():
    server_id = str(settings.BEAUTY_BANS_SERVER_ID)
    if server_id.isascii() and server_id.isdecimal() and len(server_id) <= 18:
        return int(server_id)
    return None


@dataclass(frozen=True)
class BanAccess:
    admin_id: int
    group: str
    label: str = ""

    @property
    def role(self):
        return role_badge(self.label or self.group)

    @property
    def anticheat(self):
        return self.group in ANTICHEAT_GROUPS

    @property
    def review_all(self):
        return self.group in REVIEW_GROUPS


def lookup_access(user, cursor=None):
    if not user.is_authenticated or not user.is_active or not user.steam_id:
        return None
    server_id = configured_server_id()
    if server_id is None:
        return None
    steam_ids = steam_id_variants(user.steam_id)
    if not steam_ids:
        return None
    if cursor is None:
        with ban_cursor(settings.BEAUTY_BANS_DATABASE_URL, read_only=True) as cursor:
            return lookup_access(user, cursor)
    cursor.execute(f"{ACTIVE_ASSIGNMENTS} LIMIT 2", (list(steam_ids), server_id))
    rows = cursor.fetchall()
    if len(rows) != 1 or not rows[0]["name"]:
        return None
    label = rows[0]["name"].strip()
    return BanAccess(rows[0]["id"], label.casefold(), label)


def player_roles(steam_ids):
    server_id = configured_server_id()
    if server_id is None or not settings.BEAUTY_BANS_DATABASE_URL:
        return {}
    aliases = {}
    for steam_id in steam_ids:
        if variants := steam_id_variants(steam_id or ""):
            aliases[steam_id] = variants
    if not aliases:
        return {}
    ids = {alias for variants in aliases.values() for alias in variants}
    try:
        with ban_cursor(settings.BEAUTY_BANS_DATABASE_URL, read_only=True) as cursor:
            cursor.execute(ACTIVE_ASSIGNMENTS, (sorted(ids), server_id))
            assignments = cursor.fetchall()
    except BanSourceUnavailable:
        return {}
    groups = defaultdict(list)
    for row in assignments:
        if variants := steam_id_variants(row["steam_id"]):
            groups[variants[-1]].append(row["name"])
    return {
        steam_id: role_badge(groups[variants[-1]][0])
        for steam_id, variants in aliases.items()
        if len(groups[variants[-1]]) == 1
    }


def request_access(request):
    if not hasattr(request, "beauty_bans_access"):
        try:
            request.beauty_bans_access = lookup_access(request.user)
        except BanSourceUnavailable:
            request.beauty_bans_access = None
    return request.beauty_bans_access


def navigation_access(request):
    return {"show_anticheat": bool((access := request_access(request)) and access.anticheat)}
