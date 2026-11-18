"""Shared website settings; feature-specific code belongs in Django apps."""

import os
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
DEBUG = os.getenv("DJANGO_DEBUG", "0") == "1"
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    raise ImproperlyConfigured("DJANGO_SECRET_KEY is required")
ALLOWED_HOSTS = [host.strip() for host in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if host.strip()]
CSRF_TRUSTED_ORIGINS = [origin.strip() for origin in os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if origin.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "accounts",
    "anticheat",
    "community",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

database_url = urlsplit(os.getenv("DATABASE_URL", ""))
if database_url.scheme not in {"postgres", "postgresql"} or not database_url.path.strip("/"):
    raise ImproperlyConfigured("DATABASE_URL must name a PostgreSQL database")
DATABASES = {"default": {
    "ENGINE": "django.db.backends.postgresql",
    "NAME": unquote(database_url.path[1:]),
    "USER": unquote(database_url.username or ""),
    "PASSWORD": unquote(database_url.password or ""),
    "HOST": database_url.hostname or "localhost",
    "PORT": database_url.port or 5432,
    "OPTIONS": {key: values[-1] for key, values in parse_qs(database_url.query).items()},
}}
AUTH_USER_MODEL = "accounts.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LANGUAGE_CODE = "ru"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
STATIC_URL = "/anticheat/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
GAME_SERVER_ADDRESS = os.getenv("GAME_SERVER_ADDRESS", "")
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
SESSION_COOKIE_SECURE = os.getenv("DJANGO_SECURE_COOKIES", "0" if DEBUG else "1") == "1"
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
LOGIN_URL = "/anticheat/login/"
LOGIN_REDIRECT_URL = "/anticheat/"
LOGOUT_REDIRECT_URL = "/anticheat/login/"
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

AC_S3_BUCKET = os.getenv("AC_S3_BUCKET", "")
AC_S3_ENDPOINT_URL = os.getenv("AC_S3_ENDPOINT_URL", "")
AC_S3_REGION = os.getenv("AC_S3_REGION", "us-east-1")
AC_S3_ADDRESSING_STYLE = os.getenv("AC_S3_ADDRESSING_STYLE", "auto")
AC_VIEWER_MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
