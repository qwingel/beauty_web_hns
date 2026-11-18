from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.functional import cached_property

from .steam_profile import get_steam_profile


class User(AbstractUser):
    """Site account, independent of telemetry player identities."""

    steam_id = models.CharField(max_length=17, null=True, blank=True, unique=True, editable=False)

    @property
    def public_name(self):
        return self.steam_id or self.get_username()

    @cached_property
    def steam_profile(self):
        return get_steam_profile(self.steam_id)
