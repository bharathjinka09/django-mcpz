from __future__ import annotations

import datetime as dt
from http import HTTPStatus

from django.contrib.auth.models import User
from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory, TestCase, override_settings

from django_mcpz.oauth.auth import oauth_auth
from tests.oauth.utils import make_access_token, make_client
from tests.test_server import ServerTestCase, make_message


class OAuthAuthTests(TestCase):
    user: User

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice")

    def request(
        self, authorization: str | None, path: str = "/oauth-mcp"
    ) -> HttpRequest:
        headers = {} if authorization is None else {"Authorization": authorization}
        return RequestFactory().post(path, headers=headers)

    def assert_challenge(
        self, response: HttpResponse | None, error: str | None = None
    ) -> None:
        assert response is not None
        assert response.status_code == HTTPStatus.UNAUTHORIZED
        expected = (
            'Bearer resource_metadata="http://testserver/.well-known/'
            'oauth-protected-resource/oauth-mcp"'
        )
        if error is not None:
            expected = expected.replace("Bearer ", f'Bearer error="{error}", ')
        assert response.headers["WWW-Authenticate"] == expected

    def test_missing_header(self):
        self.assert_challenge(oauth_auth(self.request(None)))

    def test_wrong_scheme(self):
        self.assert_challenge(oauth_auth(self.request("Basic abc")))

    def test_empty_credential(self):
        self.assert_challenge(oauth_auth(self.request("Bearer ")))

    def test_unknown_token(self):
        self.assert_challenge(
            oauth_auth(self.request("Bearer mcp_nope")), "invalid_token"
        )

    def test_expired_token(self):
        _, value = make_access_token(user=self.user, lifetime=dt.timedelta(seconds=-1))

        self.assert_challenge(
            oauth_auth(self.request(f"Bearer {value}")), "invalid_token"
        )

    def test_revoked_token(self):
        token, value = make_access_token(user=self.user)
        token.revoke()

        self.assert_challenge(
            oauth_auth(self.request(f"Bearer {value}")), "invalid_token"
        )

    def test_wrong_resource(self):
        _, value = make_access_token(user=self.user, resource="http://testserver/other")

        self.assert_challenge(
            oauth_auth(self.request(f"Bearer {value}")), "invalid_token"
        )

    def test_inactive_user(self):
        _, value = make_access_token(user=self.user)
        self.user.is_active = False
        self.user.save()

        self.assert_challenge(
            oauth_auth(self.request(f"Bearer {value}")), "invalid_token"
        )

    def test_valid(self):
        token, value = make_access_token(user=self.user)
        request = self.request(f"bearer {value}")

        assert oauth_auth(request) is None
        assert request.mcp_token == token  # type: ignore[attr-defined]
        assert request.user == self.user

    def test_host_case_insensitive(self):
        token, value = make_access_token(user=self.user)
        request = RequestFactory().post(
            "/oauth-mcp",
            headers={"Authorization": f"Bearer {value}"},
            SERVER_NAME="TESTSERVER",
        )

        assert oauth_auth(request) is None
        assert request.mcp_token == token  # type: ignore[attr-defined]


class OAuthServerTests(ServerTestCase, TestCase):
    """The oauth app authenticating a server, end to end."""

    url = "/oauth-mcp"
    user: User

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice")

    def test_unauthenticated(self):
        response = self.post(make_message("tools/list"))

        assert response.status_code == HTTPStatus.UNAUTHORIZED
        assert "resource_metadata=" in response.headers["WWW-Authenticate"]

    @override_settings(ROOT_URLCONF="tests.oauth.root_urls")
    def test_unauthenticated_server_at_root(self):
        # The challenge names a metadata URL without a trailing slash, which
        # is where the document is served for a server at the site root.
        response = self.client.post(
            "/",
            data=b"{}",
            content_type="application/json",
        )

        assert response.status_code == HTTPStatus.UNAUTHORIZED
        assert response.headers["WWW-Authenticate"] == (
            'Bearer resource_metadata="http://testserver/.well-known/'
            'oauth-protected-resource"'
        )

        response = self.client.get("/.well-known/oauth-protected-resource")

        assert response.status_code == HTTPStatus.OK
        assert response.json()["resource"] == "http://testserver/"

    def test_whoami(self):
        _, value = make_access_token(user=self.user, client=make_client(name="Claude"))

        response = self.post(
            make_message("tools/call", {"name": "oauth_whoami", "arguments": {}}),
            headers={"Authorization": f"Bearer {value}"},
        )

        assert response.json()["result"]["structuredContent"] == {
            "client": "Claude",
            "user": "alice",
        }
