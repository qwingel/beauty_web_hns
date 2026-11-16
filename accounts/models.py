from django.contrib.auth.models import AbstractUser


class User(AbstractUser):
    """Site account, independent of telemetry player identities."""
