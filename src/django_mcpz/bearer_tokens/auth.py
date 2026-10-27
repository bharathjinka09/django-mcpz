from __future__ import annotations

from http import HTTPStatus

from django.db.models import Q
from django.http import HttpRequest, HttpResponse
from django.utils import timezone

from django_mcpz import tokens
from django_mcpz.bearer_tokens.models import Token


def token_auth(request: HttpRequest) -> HttpResponse | None:
    """
    Authenticate with a bearer token from the django_mcpz.bearer_tokens app.

    Requires the Authorization header to carry an unrevoked, unexpired token,
    for an active user, as a bearer credential. On success, attaches the Token as
    request.mcp_token and sets request.user to the token's user. Otherwise
    responds 401 with a bearer challenge, naming the error when a credential
    was sent, per RFC 6750.
    """
    header = request.headers.get("Authorization", "")
    # The auth scheme is case-insensitive (RFC 9110 §11.1).
    scheme, _, credential = header.partition(" ")
    credential = credential.strip()
    if scheme.lower() != "bearer" or not credential:
        return _rejection()
    try:
        token = Token.objects.select_related("user").get(
            Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()),
            digest=tokens.sha256_hex(credential),
            revoked_at__isnull=True,
        )
    except Token.DoesNotExist:
        return _rejection(error="invalid_token")
    # Like Django's ModelBackend, treat a user model without is_active as
    # always active.
    if not getattr(token.user, "is_active", True):
        return _rejection(error="invalid_token")
    token.record_use()
    request.mcp_token = token  # type: ignore[attr-defined]
    request.user = token.user
    return None


def _rejection(error: str | None = None) -> HttpResponse:
    challenge = "Bearer"
    if error is not None:
        challenge += f' error="{error}"'
    return HttpResponse(
        status=HTTPStatus.UNAUTHORIZED, headers={"WWW-Authenticate": challenge}
    )
