from __future__ import annotations

import datetime as dt
from typing import Any, TypeVar
from urllib.parse import urlsplit

from django.conf import settings
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone

from django_mcpz import tokens
from django_mcpz.oauth.conf import LOCAL_HOSTS
from django_mcpz.tokens import sha256_hex

# Field sizes, checked before saving values that clients supply, since most
# databases reject longer strings rather than truncating them.
URL_MAX_LENGTH = 500
NAME_MAX_LENGTH = 200
SCOPE_MAX_LENGTH = 500


class Client(models.Model):
    """
    An OAuth client: an application that may request tokens.

    Registered through dynamic registration, in which case client_id is a
    random string, or through a client ID metadata document, in which case
    client_id is the document's HTTPS URL and the row caches the document.
    """

    class Kind(models.TextChoices):
        REGISTERED = "registered", "Dynamically registered"
        METADATA = "metadata", "Client ID metadata document"

    client_id = models.CharField(max_length=URL_MAX_LENGTH, unique=True)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    name = models.CharField(max_length=NAME_MAX_LENGTH, blank=True)
    redirect_uris = models.JSONField(default=list)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    fetched_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        help_text="When the metadata document was last fetched, for metadata clients.",
    )
    last_used_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        help_text="When a token was last issued to the client.",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.name or self.client_id

    def allows_redirect_uri(self, uri: str) -> bool:
        # Exact string comparison, per OAuth 2.1: no prefixes, no wildcards.
        # The one allowance, from OAuth 2.1 section 8.4.2, is the port of a
        # loopback address, since native clients listen on whatever port is
        # free. The spec names only IP literals, but localhost is included
        # too, since clients such as Claude Code register it.
        return any(
            uri == registered or _loopback_match(uri, registered)
            for registered in self.redirect_uris
        )

    def record_use(self) -> None:
        Client.objects.filter(pk=self.pk).update(last_used_at=timezone.now())


def _loopback_match(uri: str, registered: str) -> bool:
    """Whether the URIs differ only by port, on a loopback address."""
    try:
        a, b = urlsplit(uri), urlsplit(registered)
    except ValueError:
        # Unbalanced brackets in a host, so not a URL at all.
        return False
    return (
        a.hostname in LOCAL_HOSTS
        and a.hostname == b.hostname
        and a.scheme == b.scheme
        and (a.path, a.query, a.fragment) == (b.path, b.query, b.fragment)
    )


class AuthorizationCode(models.Model):
    """
    A single-use code, exchanged for tokens at the token endpoint.

    Every token issued from a code, and from refreshes of those tokens, links
    back to it, so that reuse of the code or of a refresh token can revoke
    the whole family, per OAuth 2.1.
    """

    digest = models.CharField(max_length=64, unique=True, editable=False)
    client = models.ForeignKey(Client, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    redirect_uri = models.CharField(max_length=URL_MAX_LENGTH)
    code_challenge = models.CharField(max_length=128)
    resource = models.CharField(max_length=URL_MAX_LENGTH)
    scope = models.CharField(max_length=SCOPE_MAX_LENGTH, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"Code for {self.client} by {self.user}"

    def revoke_family(self) -> None:
        """Revoke every token issued from this code."""
        now = timezone.now()
        with transaction.atomic():
            AccessToken.objects.filter(code=self, revoked_at__isnull=True).update(
                revoked_at=now
            )
            RefreshToken.objects.filter(code=self, revoked_at__isnull=True).update(
                revoked_at=now
            )


TokenT = TypeVar("TokenT", bound="Token")


class Token(models.Model):
    """Fields shared by access and refresh tokens."""

    digest = models.CharField(max_length=64, unique=True, editable=False)
    client = models.ForeignKey(Client, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    code = models.ForeignKey(
        AuthorizationCode, on_delete=models.SET_NULL, null=True, editable=False
    )
    resource = models.CharField(max_length=URL_MAX_LENGTH)
    scope = models.CharField(max_length=SCOPE_MAX_LENGTH, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.client} as {self.user}"

    @property
    def is_valid(self) -> bool:
        return self.revoked_at is None and self.expires_at > timezone.now()

    def revoke(self) -> None:
        if self.revoked_at is None:
            self.revoked_at = timezone.now()
            self.save(update_fields=["revoked_at"])

    @classmethod
    def create(
        cls: type[TokenT], *, lifetime: dt.timedelta, **fields: Any
    ) -> tuple[TokenT, str]:
        """Create a token, returning the instance and its secret value."""
        value = tokens.generate()
        token = cls._default_manager.create(
            digest=sha256_hex(value),
            expires_at=timezone.now() + lifetime,
            **fields,
        )
        return token, value


class AccessToken(Token):
    """A bearer token for an MCP server, sent on every request."""


class RefreshToken(Token):
    """
    A token for obtaining a fresh access token without the user.

    Single use: each refresh issues a new pair and marks this one used.
    Reuse revokes the whole family, since it means the token leaked.
    """

    access_token = models.OneToOneField(
        AccessToken, on_delete=models.CASCADE, related_name="refresh_token"
    )
    used_at = models.DateTimeField(null=True, blank=True)


def expired_or_revoked() -> Q:
    """A filter for token and code rows that can be deleted."""
    return Q(expires_at__lt=timezone.now()) | Q(revoked_at__isnull=False)
