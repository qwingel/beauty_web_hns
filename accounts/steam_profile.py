from http.client import HTTPSConnection, HTTPException
from urllib.parse import urlsplit
from xml.etree import ElementTree

from django.core.cache import cache

PROFILE_CACHE_TTL = 15 * 60
FAILURE_CACHE_TTL = 60
MAX_PROFILE_BYTES = 256 * 1024
AVATAR_HOSTS = {
    "avatars.steamstatic.com",
    "avatars.akamai.steamstatic.com",
    "avatars.cloudflare.steamstatic.com",
    "avatars.fastly.steamstatic.com",
    "steamcdn-a.akamaihd.net",
}


def fetch_steam_profile(steam_id):
    connection = HTTPSConnection("steamcommunity.com", timeout=3)
    try:
        connection.request("GET", f"/profiles/{steam_id}/?xml=1")
        response = connection.getresponse()
        if response.status != 200:
            return {}
        body = response.read(MAX_PROFILE_BYTES + 1)
        if len(body) > MAX_PROFILE_BYTES:
            return {}
        profile = ElementTree.fromstring(body)
        if profile.tag != "profile" or profile.findtext("steamID64") != steam_id:
            return {}
        nickname = profile.findtext("steamID", "").strip()
        avatar_url = profile.findtext("avatarFull", "").strip()
        avatar = urlsplit(avatar_url)
        if avatar.scheme != "https" or avatar.netloc not in AVATAR_HOSTS:
            avatar_url = ""
        return {"nickname": nickname, "avatar_url": avatar_url} if nickname or avatar_url else {}
    except (OSError, HTTPException, ElementTree.ParseError, ValueError):
        return {}
    finally:
        connection.close()


def get_steam_profile(steam_id):
    if not steam_id or len(steam_id) != 17 or not steam_id.isascii() or not steam_id.isdecimal():
        return {}
    key = f"steam-profile:{steam_id}"
    profile = cache.get(key)
    if profile is None:
        profile = fetch_steam_profile(steam_id)
        cache.set(key, profile, PROFILE_CACHE_TTL if profile else FAILURE_CACHE_TTL)
    return profile
