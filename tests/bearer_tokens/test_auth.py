from __future__ import annotations

import datetime as dt
from http import HTTPStatus

from django.contrib.auth.models import Permission, User
from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory, TestCase
from django.utils import timezone

from django_mcpz.bearer_tokens.auth import token_auth
from django_mcpz.bearer_tokens.models import Token
from django_mcpz.jsonrpc import INVALID_PARAMS
from tests.test_server import ServerTestCase, make_message


class TokenAuthTests(TestCase):
    user: User

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice")

    def request(self, authorization: str | None) -> HttpRequest:
        headers = {} if authorization is None else {"Authorization": authorization}
        return RequestFactory().post("/mcp", headers=headers)

    def assert_challenge(
        self, response: HttpResponse | None, error: str | None = None
    ) -> None:
        # RFC 6750 section 3.1: the error is named when a credential was
        # sent, and left out when the request carried none.
        assert response is not None
        assert response.status_code == HTTPStatus.UNAUTHORIZED
        expected = "Bearer"
        if error is not None:
            expected += f' error="{error}"'
        assert response.headers["WWW-Authenticate"] == expected

    def test_missing_header(self):
        self.assert_challenge(token_auth(self.request(None)))

    def test_wrong_scheme(self):
        _, value = Token.create(name="t", user=self.user)

        self.assert_challenge(token_auth(self.request(f"Basic {value}")))

    def test_empty_credential(self):
        self.assert_challenge(token_auth(self.request("Bearer ")))

    def test_unknown_token(self):
        self.assert_challenge(
            token_auth(self.request("Bearer mcp_nope")), "invalid_token"
        )

    def test_revoked_token(self):
        token, value = Token.create(name="t", user=self.user)
        token.revoke()

        self.assert_challenge(
            token_auth(self.request(f"Bearer {value}")), "invalid_token"
        )

    def test_expired_token(self):
        _, value = Token.create(
            name="t",
            user=self.user,
            expires_at=timezone.now() - dt.timedelta(seconds=1),
        )

        self.assert_challenge(
            token_auth(self.request(f"Bearer {value}")), "invalid_token"
        )

    def test_unexpired_token(self):
        _, value = Token.create(
            name="t",
            user=self.user,
            expires_at=timezone.now() + dt.timedelta(days=1),
        )

        assert token_auth(self.request(f"Bearer {value}")) is None

    def test_inactive_user(self):
        _, value = Token.create(name="t", user=self.user)
        self.user.is_active = False
        self.user.save()

        self.assert_challenge(
            token_auth(self.request(f"Bearer {value}")), "invalid_token"
        )

    def test_valid_token(self):
        token, value = Token.create(name="t", user=self.user)
        # The auth scheme is case-insensitive (RFC 9110 §11.1).
        request = self.request(f"bearer {value}")

        assert token_auth(request) is None
        assert request.mcp_token == token  # type: ignore[attr-defined]
        assert request.user == self.user
        token.refresh_from_db()
        assert token.last_used_at is not None


class TokensServerTests(ServerTestCase, TestCase):
    """The tokens app authenticating a server, end to end."""

    url = "/bearer-tokens-mcp"
    viewer: User
    nobody: User

    @classmethod
    def setUpTestData(cls):
        cls.viewer = User.objects.create_user("viewer")
        cls.viewer.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="tests", codename="view_widget"
            )
        )
        cls.nobody = User.objects.create_user("nobody")

    def test_unauthenticated(self):
        response = self.post(make_message("tools/list"))

        assert response.status_code == HTTPStatus.UNAUTHORIZED

    def test_whoami(self):
        _, value = Token.create(name="Viewer’s client", user=self.viewer)

        response = self.post(
            make_message("tools/call", {"name": "whoami", "arguments": {}}),
            headers={"Authorization": f"Bearer {value}"},
        )

        assert response.json()["result"]["structuredContent"] == {
            "token": "Viewer’s client",
            "user": "viewer",
        }

    def test_permission_granted(self):
        _, value = Token.create(name="t", user=self.viewer)

        response = self.post(
            make_message("tools/list"), headers={"Authorization": f"Bearer {value}"}
        )
        names = [tool["name"] for tool in response.json()["result"]["tools"]]
        assert names == ["whoami", "token_widget_tool"]

        response = self.post(
            make_message("tools/call", {"name": "token_widget_tool"}),
            headers={"Authorization": f"Bearer {value}"},
        )
        result = response.json()["result"]
        assert result["content"] == [{"type": "text", "text": "widgets"}]

    def test_permission_denied(self):
        _, value = Token.create(name="t", user=self.nobody)

        response = self.post(
            make_message("tools/list"), headers={"Authorization": f"Bearer {value}"}
        )
        names = [tool["name"] for tool in response.json()["result"]["tools"]]
        assert names == ["whoami"]

        response = self.post(
            make_message("tools/call", {"name": "token_widget_tool"}),
            headers={"Authorization": f"Bearer {value}"},
        )
        self.assert_error(response, INVALID_PARAMS)
