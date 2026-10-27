from __future__ import annotations

import datetime as dt
from http import HTTPStatus
from typing import Any
from unittest import mock

from django.contrib.auth.models import User
from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone

from django_mcpz.oauth import cimd
from django_mcpz.oauth.models import (
    AccessToken,
    AuthorizationCode,
    Client,
    RefreshToken,
)
from django_mcpz.tokens import sha256_hex
from tests.oauth.utils import (
    METADATA_URL,
    REDIRECT_URI,
    RESOURCE,
    TokenTestCase,
    make_access_token,
    make_client,
    make_code,
)


class TokenCodeTests(TokenTestCase):
    def test_success(self):
        code, value, verifier = self.make_code(scope="mcp")

        response = self.exchange(value, verifier, redirect_uri=REDIRECT_URI)

        body = self.assert_tokens(response)
        assert body["scope"] == "mcp"
        code.refresh_from_db()
        assert code.used_at is not None
        access = AccessToken.objects.get()
        assert access.user == self.user
        assert access.client == self.oauth_client
        assert access.code == code
        assert access.resource == RESOURCE
        assert access.scope == "mcp"
        self.oauth_client.refresh_from_db()
        assert self.oauth_client.last_used_at is not None

    def test_success_without_scope(self):
        _, value, verifier = self.make_code()

        body = self.assert_tokens(self.exchange(value, verifier))

        assert "scope" not in body

    def test_get_not_allowed(self):
        response = self.client.get("/oauth/token")

        assert response.status_code == HTTPStatus.METHOD_NOT_ALLOWED

    def test_unknown_client(self):
        _, value, verifier = self.make_code()

        response = self.exchange(value, verifier, client_id="nope")

        self.assert_token_error(response, "invalid_client", HTTPStatus.UNAUTHORIZED)

    def test_unsupported_grant_type(self):
        response = self.post_token(grant_type="password")

        self.assert_token_error(response, "unsupported_grant_type")

    def test_metadata_client_known(self):
        client = make_client(client_id=METADATA_URL, kind=Client.Kind.METADATA)
        _, value, verifier = self.make_code(client=client)

        with mock.patch.object(cimd, "fetch_document") as fetch:
            response = self.exchange(value, verifier, client_id=METADATA_URL)

        self.assert_tokens(response)
        assert not fetch.called

    def test_metadata_client_unknown_not_fetched(self):
        _, value, verifier = self.make_code()

        with mock.patch.object(cimd, "fetch_document") as fetch:
            response = self.exchange(value, verifier, client_id=METADATA_URL)

        self.assert_token_error(response, "invalid_client", HTTPStatus.UNAUTHORIZED)
        assert not fetch.called
        assert not Client.objects.filter(client_id=METADATA_URL).exists()

    def test_unknown_code(self):
        response = self.exchange("mcp_nope", "v" * 43)

        self.assert_token_error(response, "invalid_grant")

    def test_code_for_other_client(self):
        other = make_client()
        _, value, verifier = self.make_code(client=other)

        response = self.exchange(value, verifier)

        self.assert_token_error(response, "invalid_grant")

    def test_code_reuse_revokes_family(self):
        code, value, verifier = self.make_code()
        first = self.assert_tokens(self.exchange(value, verifier))

        response = self.exchange(value, verifier)

        body = self.assert_token_error(response, "invalid_grant")
        assert body["error_description"] == "Authorization code already used."
        access = AccessToken.objects.get(digest=sha256_hex(first["access_token"]))
        refresh = RefreshToken.objects.get(digest=sha256_hex(first["refresh_token"]))
        assert not access.is_valid
        assert not refresh.is_valid

    def test_code_expired(self):
        _, value, verifier = self.make_code(
            expires_at=timezone.now() - dt.timedelta(seconds=1)
        )

        response = self.exchange(value, verifier)

        body = self.assert_token_error(response, "invalid_grant")
        assert body["error_description"] == "Authorization code expired."

    def test_redirect_uri_mismatch(self):
        _, value, verifier = self.make_code()

        response = self.exchange(
            value, verifier, redirect_uri="http://localhost:9999/other"
        )

        self.assert_token_error(response, "invalid_grant")

    def test_resource_mismatch(self):
        _, value, verifier = self.make_code()

        response = self.exchange(value, verifier, resource="http://testserver/other")

        self.assert_token_error(response, "invalid_target")

    def test_resource_match(self):
        _, value, verifier = self.make_code()

        response = self.exchange(value, verifier, resource=RESOURCE)

        self.assert_tokens(response)

    def test_resource_match_case_insensitive(self):
        # The authorize endpoint accepts the scheme and host in any case, so
        # the same value must match here too.
        _, value, verifier = self.make_code()

        response = self.exchange(
            value, verifier, resource="HTTP://TESTSERVER/oauth-mcp"
        )

        self.assert_tokens(response)

    def test_resource_malformed(self):
        # Unbalanced brackets make urlsplit() raise, which must not leak.
        _, value, verifier = self.make_code()

        response = self.exchange(value, verifier, resource="http://[::1")

        self.assert_token_error(response, "invalid_target")

    def test_wrong_verifier(self):
        _, value, _ = self.make_code()

        response = self.exchange(value, "w" * 43)

        self.assert_token_error(response, "invalid_grant")

    def test_short_verifier(self):
        _, value, _ = self.make_code()

        response = self.exchange(value, "short")

        self.assert_token_error(response, "invalid_grant")

    def test_inactive_user(self):
        _, value, verifier = self.make_code()
        self.user.is_active = False
        self.user.save()

        response = self.exchange(value, verifier)

        self.assert_token_error(response, "invalid_grant")


class TokenRefreshTests(TokenTestCase):
    def issue(self, **fields: Any) -> tuple[dict[str, Any], AuthorizationCode]:
        code, value, verifier = self.make_code(**fields)
        return self.assert_tokens(self.exchange(value, verifier)), code

    def refresh(self, refresh_token: str, **data: Any) -> Any:
        data.setdefault("grant_type", "refresh_token")
        return self.post_token(refresh_token=refresh_token, **data)

    def test_success(self):
        first, code = self.issue(scope="mcp")

        response = self.refresh(first["refresh_token"])

        second = self.assert_tokens(response)
        assert second["scope"] == "mcp"
        assert second["access_token"] != first["access_token"]
        assert second["refresh_token"] != first["refresh_token"]
        old_access = AccessToken.objects.get(digest=sha256_hex(first["access_token"]))
        old_refresh = RefreshToken.objects.get(
            digest=sha256_hex(first["refresh_token"])
        )
        assert old_access.is_valid, "Left to expire, for requests in flight."
        assert old_refresh.used_at is not None
        new_access = AccessToken.objects.get(digest=sha256_hex(second["access_token"]))
        assert new_access.code == code

    def test_unknown(self):
        response = self.refresh("mcp_nope")

        self.assert_token_error(response, "invalid_grant")

    def test_other_client(self):
        first, _ = self.issue()
        other = make_client()

        response = self.refresh(first["refresh_token"], client_id=other.client_id)

        self.assert_token_error(response, "invalid_grant")

    def test_reuse_revokes_family(self):
        first, _ = self.issue()
        second = self.assert_tokens(self.refresh(first["refresh_token"]))

        response = self.refresh(first["refresh_token"])

        self.assert_token_error(response, "invalid_grant")
        access = AccessToken.objects.get(digest=sha256_hex(second["access_token"]))
        refresh = RefreshToken.objects.get(digest=sha256_hex(second["refresh_token"]))
        assert not access.is_valid
        assert not refresh.is_valid

    def test_reuse_without_code_revokes_pair(self):
        first, code = self.issue()
        second = self.assert_tokens(self.refresh(first["refresh_token"]))
        code.delete()

        response = self.refresh(first["refresh_token"])

        self.assert_token_error(response, "invalid_grant")
        access = AccessToken.objects.get(digest=sha256_hex(second["access_token"]))
        assert access.is_valid, "Only the reused token's own pair is revoked."
        old = RefreshToken.objects.get(digest=sha256_hex(first["refresh_token"]))
        assert not old.is_valid

    def test_expired(self):
        first, _ = self.issue()
        RefreshToken.objects.update(expires_at=timezone.now() - dt.timedelta(days=1))

        response = self.refresh(first["refresh_token"])

        self.assert_token_error(response, "invalid_grant")

    def test_resource_mismatch(self):
        first, _ = self.issue()

        response = self.refresh(
            first["refresh_token"], resource="http://testserver/other"
        )

        self.assert_token_error(response, "invalid_target")

    def test_resource_match_case_insensitive(self):
        first, _ = self.issue()

        response = self.refresh(
            first["refresh_token"], resource="HTTP://TESTSERVER/oauth-mcp"
        )

        self.assert_tokens(response)

    def test_resource_malformed(self):
        first, _ = self.issue()

        response = self.refresh(first["refresh_token"], resource="http://[::1")

        self.assert_token_error(response, "invalid_target")

    def test_inactive_user(self):
        first, _ = self.issue()
        self.user.is_active = False
        self.user.save()

        response = self.refresh(first["refresh_token"])

        self.assert_token_error(response, "invalid_grant")


class AtomicRequestsTests(TransactionTestCase):
    """
    Under ATOMIC_REQUESTS, the writes behind an error response still commit,
    since the view returns rather than raises: a replayed code revokes its
    family for good.
    """

    def test_reuse_revocation_committed(self):
        user = User.objects.create_user("alice")
        oauth_client = make_client(name="Claude")
        code, value, verifier = make_code(client=oauth_client, user=user)
        access, _ = make_access_token(user=user, client=oauth_client, code=code)
        code.used_at = timezone.now()
        code.save()

        with mock.patch.dict(connection.settings_dict, {"ATOMIC_REQUESTS": True}):
            response = self.client.post(
                "/oauth/token",
                {
                    "grant_type": "authorization_code",
                    "client_id": oauth_client.client_id,
                    "code": value,
                    "code_verifier": verifier,
                },
            )

        assert response.status_code == HTTPStatus.BAD_REQUEST
        assert response.json()["error"] == "invalid_grant"
        access.refresh_from_db()
        assert access.revoked_at is not None
