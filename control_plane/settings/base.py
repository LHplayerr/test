"""Shared Django settings.

Reads every value from the environment so the same code runs in dev,
test, and production without changes. The only files that differ
between environments are dev.py and prod.py, and each overrides just a
handful of variables.
"""

from __future__ import annotations

import os
from pathlib import Path

import dj_database_url

from shared.logging import JSONFormatter


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_list(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

REDIS_URL = _env("REDIS_URL", "redis://localhost:6379/0")
GATEWAY_PUBLIC_HOST = _env("GATEWAY_PUBLIC_HOST", "")
SECRET_KEY = _env("DJANGO_SECRET_KEY", "dev-insecure-change-me")
DEBUG = _env_bool("DJANGO_DEBUG", False)
ALLOWED_HOSTS = _env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")

ROOT_URLCONF = "control_plane.urls"
WSGI_APPLICATION = "control_plane.wsgi.application"
ASGI_APPLICATION = "control_plane.asgi.application"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "channels",
    "rest_framework",
    "control_plane.apps.sessions_mgr",
    "control_plane.apps.dashboard",
]

# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "control_plane" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
            ],
        },
    },
]


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

DATABASES = {
    "default": dj_database_url.parse(
        _env("DATABASE_URL", f"sqlite:///{BASE_DIR / 'db.sqlite3'}"),
        conn_max_age=600,
    ),
}


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------

_redis_url = _env("REDIS_URL", "redis://localhost:6379/0")

# channels_redis waits for messages with a 5 s blocking pop, and redis-py
# 8 defaults socket_timeout to 5 s as well. Left at the default, an idle
# WebSocket dies with a TimeoutError after about five seconds, so the
# socket timeout must be longer than the blocking pop.
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {"hosts": [{"address": _redis_url, "socket_timeout": 15}]},
    },
}


# ---------------------------------------------------------------------------
# REST framework
# ---------------------------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
    ],
    "DEFAULT_AUTHENTICATION_CLASSES": [],      # no accounts
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.AllowAny", # capability URLs are the gate
    ],
    "UNAUTHENTICATED_USER": None,
}


# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"


# ---------------------------------------------------------------------------
# Logging — JSON, structured, correlated by session_id
# ---------------------------------------------------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {"()": JSONFormatter},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": os.environ.get("LOG_LEVEL", "INFO"),
    },
    "loggers": {
        "django.request": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "uvicorn.access": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}