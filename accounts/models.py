from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Site account, independent of telemetry player identities."""

    beauty_bans_admin_id = models.PositiveBigIntegerField(null=True, blank=True, unique=True)
