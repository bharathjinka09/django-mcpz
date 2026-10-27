from __future__ import annotations

from typing import Any

SECRET_KEY = "NOTASECRET"

ALLOWED_HOSTS: list[str] = []

DATABASES: dict[str, dict[str, Any]] = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    },
    # PostgreSQL is used only by the tests that need it. It matches
    # compose.yaml, so start it with: docker compose up -d --wait
    "postgres": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "mcpz",
        "HOST": "localhost",
        "PORT": "55432",
        "USER": "postgres",
        "PASSWORD": "postgres",
        # Set up without the default database, for tests that use only this.
        "TEST": {"DEPENDENCIES": []},
    },
}

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.messages",
    "django.contrib.sessions",
    "django_mcpz",
    "django_mcpz.oauth",
    "django_mcpz.bearer_tokens",
    "tests",
]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

TEMPLATES: list[dict[str, Any]] = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "django.template.context_processors.request",
            ],
        },
    }
]

ROOT_URLCONF = "tests.urls"

USE_TZ = True
STATIC_URL = "static/"
