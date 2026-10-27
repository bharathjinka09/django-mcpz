from __future__ import annotations

import datetime as dt

from django.test import SimpleTestCase, override_settings
from unittest_parametrize import ParametrizedTestCase, parametrize

from django_mcpz.oauth.conf import is_secure_url, oauth_settings


class SettingsTests(ParametrizedTestCase, SimpleTestCase):
    def test_defaults(self):
        assert oauth_settings.access_token_lifetime == dt.timedelta(hours=1)
        assert oauth_settings.refresh_token_lifetime == dt.timedelta(days=30)
        assert oauth_settings.code_lifetime == dt.timedelta(minutes=5)
        assert oauth_settings.dynamic_registration

    @override_settings(
        MCPZ_OAUTH_ACCESS_TOKEN_LIFETIME=dt.timedelta(minutes=10),
        MCPZ_OAUTH_REFRESH_TOKEN_LIFETIME=dt.timedelta(days=1),
        MCPZ_OAUTH_DYNAMIC_REGISTRATION=False,
    )
    def test_overrides(self):
        assert oauth_settings.access_token_lifetime == dt.timedelta(minutes=10)
        assert oauth_settings.refresh_token_lifetime == dt.timedelta(days=1)
        assert not oauth_settings.dynamic_registration

    @parametrize(
        "url",
        [
            "https://example.com/cb",
            "http://localhost:3000/cb",
            "http://127.0.0.1:3000/cb",
            "http://[::1]:3000/cb",
        ],
    )
    def test_is_secure_url(self, url):
        assert is_secure_url(url)

    @parametrize(
        "url",
        [
            "http://example.com/cb",
            "myapp://callback",
            # Unbalanced brackets make urlsplit() raise, which must not leak.
            "http://[::1",
        ],
    )
    def test_is_secure_url_invalid(self, url):
        assert not is_secure_url(url)
