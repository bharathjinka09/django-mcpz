from __future__ import annotations

from http import HTTPStatus
from typing import Any
from unittest import mock
from urllib.parse import parse_qs, urlencode, urlsplit

from django.conf import settings
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from unittest_parametrize import ParametrizedTestCase, parametrize

from django_mcpz.oauth import cimd, views
from django_mcpz.oauth.models import AuthorizationCode, Client
from django_mcpz.tokens import sha256_hex
from tests.oauth.utils import (
    ISSUER,
    METADATA_URL,
    REDIRECT_URI,
    RESOURCE,
    make_client,
    mock_metadata_fetch,
    pkce_pair,
)


class AuthorizeTests(ParametrizedTestCase, TestCase):
    user: User
    oauth_client: Client

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", password="pw")
        cls.oauth_client = make_client(name="Claude")

    def setUp(self):
        self.client.force_login(self.user)

    def params(self, **overrides: Any) -> dict[str, Any]:
        _, challenge = pkce_pair()
        params: dict[str, Any] = {
            "response_type": "code",
            "client_id": self.oauth_client.client_id,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": RESOURCE,
            "state": "xyz",
        }
        params.update(overrides)
        return {key: value for key, value in params.items() if value is not None}

    def get(self, **overrides: Any) -> Any:
        return self.client.get("/oauth/authorize", self.params(**overrides))

    def post(self, decision: str, **overrides: Any) -> Any:
        return self.client.post(
            "/oauth/authorize?" + urlencode(self.params(**overrides)),
            {"decision": decision},
        )

    def assert_redirect_error(self, response: Any, error: str) -> None:
        assert response.status_code == HTTPStatus.FOUND
        parts = urlsplit(response["Location"])
        assert parts.scheme == "http"
        assert parts.netloc == "localhost:9999"
        query = parse_qs(parts.query)
        assert query["error"] == [error]
        assert query["state"] == ["xyz"]
        assert query["iss"] == [ISSUER]
        assert "code" not in query

    def test_unknown_client(self):
        response = self.get(client_id="nope")

        assert response.status_code == HTTPStatus.BAD_REQUEST
        self.assertContains(
            response, "invalid_client", status_code=HTTPStatus.BAD_REQUEST
        )
        self.assertContains(
            response, "Unknown client_id.", status_code=HTTPStatus.BAD_REQUEST
        )
        assert response["X-Frame-Options"] == "DENY"
        assert response["Content-Security-Policy"] == "frame-ancestors 'none'"

    def test_unregistered_redirect_uri(self):
        response = self.get(redirect_uri="http://localhost:9999/other")

        self.assertContains(
            response, "Unregistered redirect_uri.", status_code=HTTPStatus.BAD_REQUEST
        )

    @parametrize("host", ["127.0.0.1", "[::1]", "localhost"])
    def test_loopback_redirect_uri_any_port(self, host):
        self.oauth_client.redirect_uris = [f"http://{host}:3000/callback"]
        self.oauth_client.save()

        response = self.get(redirect_uri=f"http://{host}:41234/callback")

        assert response.status_code == HTTPStatus.OK

    @parametrize(
        "uri",
        [
            "http://127.0.0.1:3000/other",
            "https://127.0.0.1:3000/callback",
            "http://localhost:41234/callback",
            "http://127.0.0.1:3000/callback?x=1",
            # Unbalanced brackets make urlsplit() raise, which must not leak.
            "http://[::1",
        ],
    )
    def test_loopback_redirect_uri_other_differences(self, uri):
        self.oauth_client.redirect_uris = ["http://127.0.0.1:3000/callback"]
        self.oauth_client.save()

        response = self.get(redirect_uri=uri)

        self.assertContains(
            response,
            "Unregistered redirect_uri.",
            status_code=HTTPStatus.BAD_REQUEST,
            msg_prefix=uri,
        )

    def test_loopback_redirect_uri_too_long(self):
        # The port allowance could stretch a registered URI past the field
        # size, so the length is checked too.
        registered = "http://127.0.0.1/" + "a" * 483
        assert len(registered) == 500
        self.oauth_client.redirect_uris = [registered]
        self.oauth_client.save()

        response = self.get(redirect_uri=registered.replace("/a", ":65535/a", 1))

        self.assertContains(
            response, "Unregistered redirect_uri.", status_code=HTTPStatus.BAD_REQUEST
        )

    def test_missing_redirect_uri_single_registered(self):
        response = self.get(redirect_uri=None)

        assert response.status_code == HTTPStatus.OK

    def test_missing_redirect_uri_multiple_registered(self):
        self.oauth_client.redirect_uris.append("http://localhost:9999/other")
        self.oauth_client.save()

        response = self.get(redirect_uri=None)

        self.assertContains(
            response, "Missing redirect_uri.", status_code=HTTPStatus.BAD_REQUEST
        )

    def test_wrong_response_type(self):
        response = self.get(response_type="token")

        self.assert_redirect_error(response, "unsupported_response_type")

    def test_missing_code_challenge(self):
        response = self.get(code_challenge=None)

        self.assert_redirect_error(response, "invalid_request")

    @parametrize(
        "challenge",
        # Padding, and non-ASCII, which hmac.compare_digest cannot compare.
        ["a" * 42 + "=", "\u00e9" * 43],
    )
    def test_invalid_code_challenge(self, challenge):
        response = self.get(code_challenge=challenge)

        self.assert_redirect_error(response, "invalid_request")

    def test_scope_too_long(self):
        response = self.get(scope="x" * 501)

        self.assert_redirect_error(response, "invalid_scope")

    def test_plain_code_challenge_method(self):
        response = self.get(code_challenge_method="plain")

        self.assert_redirect_error(response, "invalid_request")

    @parametrize(
        "resource",
        [
            "http://testserver/other",
            "http://testserver/admin/",
            "http://other.example/oauth-mcp",
            # Unbalanced brackets make urlsplit() raise, which must not leak.
            "http://[::1",
            # Longer than the field it would be stored in.
            "http://" + "a" * 500 + "@testserver/oauth-mcp",
        ],
    )
    def test_unknown_resource(self, resource):
        response = self.get(resource=resource)

        self.assert_redirect_error(response, "invalid_target")

    def test_missing_resource(self):
        response = self.get(resource=None)

        self.assert_redirect_error(response, "invalid_target")

    def test_login_required(self):
        self.client.logout()

        response = self.get()

        assert response.status_code == HTTPStatus.FOUND
        location = response["Location"]
        assert location.startswith("/accounts/login/?next=")
        assert "/oauth/authorize" in location

    def test_login_required_before_client_lookup(self):
        self.client.logout()

        with mock.patch.object(cimd, "fetch_document") as fetch:
            response = self.get(client_id=METADATA_URL)

        assert response.status_code == HTTPStatus.FOUND
        assert not fetch.called
        assert not Client.objects.filter(client_id=METADATA_URL).exists()

    def test_resource_case_insensitive(self):
        response = self.post("allow", resource="HTTP://TESTSERVER/oauth-mcp")

        assert response.status_code == HTTPStatus.FOUND
        assert AuthorizationCode.objects.get().resource == RESOURCE

    def test_consent_page(self):
        response = self.get(scope="mcp")

        assert response.status_code == HTTPStatus.OK
        self.assertContains(response, "Claude")
        self.assertContains(response, "<strong>OAuth server</strong>")
        self.assertContains(response, f"<code>{RESOURCE}</code>")
        self.assertContains(
            response, '<img src="http://testserver/static/oauth/icon.png" alt="">'
        )
        self.assertContains(
            response,
            '<source srcset="http://testserver/static/oauth/icon-dark.png"'
            ' media="(prefers-color-scheme: dark)">',
        )
        self.assertContains(response, "alice")
        self.assertContains(response, "running on your computer")
        self.assertContains(response, "<code>mcp</code>")
        assert response["X-Frame-Options"] == "DENY"
        assert response["Content-Security-Policy"] == "frame-ancestors 'none'"

    def test_consent_page_without_auth_context_processor(self):
        templates = [{**settings.TEMPLATES[0], "OPTIONS": {}}]

        with self.settings(TEMPLATES=templates):
            response = self.get()

        self.assertContains(response, "<strong>alice</strong>")

    def test_consent_page_ipv6_local_redirect(self):
        self.oauth_client.redirect_uris = ["http://[::1]:9999/callback"]
        self.oauth_client.save()

        response = self.get(redirect_uri="http://[::1]:9999/callback")

        self.assertContains(response, "running on your computer")

    def test_consent_page_remote_redirect(self):
        self.oauth_client.redirect_uris = ["https://claude.ai/callback"]
        self.oauth_client.save()

        response = self.get(redirect_uri="https://claude.ai/callback")

        self.assertContains(response, "return to <strong>claude.ai</strong>")

    def test_deny(self):
        response = self.post("deny")

        self.assert_redirect_error(response, "access_denied")
        assert not AuthorizationCode.objects.exists()

    def test_allow(self):
        response = self.post("allow", scope="mcp")

        assert response.status_code == HTTPStatus.FOUND
        parts = urlsplit(response["Location"])
        assert parts.netloc == "localhost:9999"
        query = parse_qs(parts.query)
        assert query["state"] == ["xyz"]
        assert query["iss"] == [ISSUER]
        (value,) = query["code"]
        code = AuthorizationCode.objects.get()
        assert code.digest == sha256_hex(value)
        assert code.client == self.oauth_client
        assert code.user == self.user
        assert code.redirect_uri == REDIRECT_URI
        assert code.resource == RESOURCE
        assert code.scope == "mcp"
        assert code.used_at is None
        assert code.expires_at > timezone.now()

    def test_allow_without_state(self):
        response = self.post("allow", state=None)

        query = parse_qs(urlsplit(response["Location"]).query)
        assert "state" not in query
        assert "code" in query

    def test_redirect_uri_with_query(self):
        self.oauth_client.redirect_uris = ["http://localhost:9999/cb?app=1"]
        self.oauth_client.save()

        response = self.post("allow", redirect_uri="http://localhost:9999/cb?app=1")

        location = response["Location"]
        assert location.startswith("http://localhost:9999/cb?app=1&")

    def test_post_authorization_request(self):
        # RFC 6749 section 3.1: the request itself may be POSTed, from the
        # client's site, so without a CSRF token.
        client = self.client_class(enforce_csrf_checks=True)
        client.force_login(self.user)
        params = self.params()

        response = client.post("/oauth/authorize", params)

        assert response.status_code == HTTPStatus.FOUND
        parts = urlsplit(response["Location"])
        assert parts.path == "/oauth/authorize"
        assert parse_qs(parts.query) == {key: [value] for key, value in params.items()}
        response = client.get(response["Location"])
        assert response.status_code == HTTPStatus.OK
        self.assertContains(response, "Allow access?")

    def test_consent_with_csrf(self):
        # As a browser does it: the page sets the cookie and carries the
        # token, without relying on the project's middleware.
        client = self.client_class(enforce_csrf_checks=True)
        client.force_login(self.user)
        params = self.params()
        page = client.get("/oauth/authorize", params)
        assert "csrftoken" in client.cookies

        response = client.post(
            "/oauth/authorize?" + urlencode(params),
            {"decision": "allow", "csrfmiddlewaretoken": page.context["csrf_token"]},
        )

        assert response.status_code == HTTPStatus.FOUND
        assert "code=" in response["Location"]

    def test_post_requires_csrf(self):
        client = self.client_class(enforce_csrf_checks=True)
        client.force_login(self.user)

        response = client.post(
            "/oauth/authorize?" + urlencode(self.params()), {"decision": "allow"}
        )

        assert response.status_code == HTTPStatus.FORBIDDEN

    def test_metadata_document_client(self):
        document = {
            "client_id": METADATA_URL,
            "client_name": "Metadata client",
            "redirect_uris": [REDIRECT_URI],
        }

        with mock_metadata_fetch(document):
            response = self.get(client_id=METADATA_URL)

        assert response.status_code == HTTPStatus.OK
        self.assertContains(response, "Metadata client")
        client = Client.objects.get(client_id=METADATA_URL)
        assert client.kind == Client.Kind.METADATA

    def test_metadata_document_client_invalid(self):
        with mock_metadata_fetch({"client_id": "https://other.example/x"}):
            response = self.get(client_id=METADATA_URL)

        self.assertContains(
            response, "does not match its URL", status_code=HTTPStatus.BAD_REQUEST
        )


class PickIconsTests(SimpleTestCase):
    def test_none(self):
        assert views.pick_icons([]) == (None, None)

    def test_unthemed(self):
        icon = {"src": "http://testserver/icon.png"}

        assert views.pick_icons([icon]) == (icon, None)

    def test_light_and_dark(self):
        light = {"src": "http://testserver/light.png", "theme": "light"}
        dark = {"src": "http://testserver/dark.png", "theme": "dark"}

        assert views.pick_icons([dark, light]) == (light, dark)

    def test_dark_only(self):
        dark = {"src": "http://testserver/dark.png", "theme": "dark"}

        assert views.pick_icons([dark]) == (dark, None)
