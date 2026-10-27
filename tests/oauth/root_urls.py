"""A URLconf with an MCP server routed at the site root, for discovery tests."""

from __future__ import annotations

from django.urls import include, path

from tests.mcp import oauth_server

urlpatterns = [
    path("", oauth_server),
    path("oauth/", include("django_mcpz.oauth.urls")),
    path("", include("django_mcpz.oauth.wellknown")),
]
