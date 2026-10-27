from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from http import HTTPStatus
from typing import Any
from urllib.parse import urlencode, urlsplit

from django.contrib.auth.decorators import login_not_required
from django.contrib.auth.views import redirect_to_login
from django.db import transaction
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseRedirect,
    JsonResponse,
)
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_deny
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_http_methods, require_POST

from django_mcpz import tokens
from django_mcpz.oauth import cimd, discovery
from django_mcpz.oauth.conf import (
    LOCAL_HOSTS,
    is_secure_url,
    oauth_settings,
)
from django_mcpz.oauth.csp import frame_ancestors_none
from django_mcpz.oauth.discovery import (
    canonical,
    issuer_url,
    resolve_mcp_server,
    validate_resource,
)
from django_mcpz.oauth.models import (
    NAME_MAX_LENGTH,
    SCOPE_MAX_LENGTH,
    URL_MAX_LENGTH,
    AccessToken,
    AuthorizationCode,
    Client,
    RefreshToken,
)

# Metadata documents


@login_not_required
def protected_resource_metadata(
    request: HttpRequest, resource_path: str = ""
) -> HttpResponse:
    """RFC 9728: describe an MCP server and name its authorization server."""
    path = f"/{resource_path}"
    server = resolve_mcp_server(path)
    if server is None:
        raise Http404
    info = server.server_info
    return JsonResponse(
        {
            "resource": canonical(request.build_absolute_uri(path)),
            "authorization_servers": [issuer_url(request)],
            "bearer_methods_supported": ["header"],
            # Shown by clients when listing the connection, so the server's
            # display name where it has one.
            "resource_name": info.get("title", info["name"]),
        }
    )


@login_not_required
def authorization_server_metadata(
    request: HttpRequest, issuer_path: str = ""
) -> HttpResponse:
    """RFC 8414: describe the authorization server's endpoints and features."""
    if f"/{issuer_path}".rstrip("/") != discovery.issuer_path():
        raise Http404
    issuer = issuer_url(request)
    document = {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/authorize",
        "token_endpoint": f"{issuer}/token",
        "revocation_endpoint": f"{issuer}/revoke",
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
        "client_id_metadata_document_supported": True,
        "authorization_response_iss_parameter_supported": True,
    }
    if oauth_settings.dynamic_registration:
        document["registration_endpoint"] = f"{issuer}/register"
    return JsonResponse(document)


# Authorization endpoint


# RFC 7636: a challenge is base64url, and an S256 one is 43 characters. The
# looser bound keeps the stored value comparable with hmac.compare_digest,
# which rejects non-ASCII strings.
CODE_CHALLENGE_RE = re.compile(r"[A-Za-z0-9_-]{43,128}")


class AuthorizeError(Exception):
    """A problem with the authorization request."""

    def __init__(self, error: str, description: str) -> None:
        super().__init__(description)
        self.error = error
        self.description = description


def load_client(client_id: str, *, fetch: bool) -> Client:
    """
    Find the client for a request.

    Metadata documents are fetched only for the authorize page, where the
    user has logged in, so that anyone on the internet cannot make the
    server fetch URLs or fill the client table. The token and revocation
    endpoints see only known clients, which suffices, since codes and tokens
    exist only for clients that have been through the authorize page.
    """
    if fetch and cimd.is_metadata_url(client_id):
        try:
            return cimd.get_metadata_client(client_id)
        except cimd.MetadataError as exc:
            raise AuthorizeError("invalid_client", str(exc)) from exc
    client = Client.objects.filter(client_id=client_id).first()
    if client is None:
        raise AuthorizeError("invalid_client", "Unknown client_id.")
    return client


def resolve_redirect_uri(client: Client, redirect_uri: str | None) -> str:
    if redirect_uri is None:
        if len(client.redirect_uris) == 1:
            return str(client.redirect_uris[0])
        raise AuthorizeError("invalid_request", "Missing redirect_uri.")
    # The loopback port allowance means a matching URI can be longer than
    # the registered one, so the length is checked as well.
    if len(redirect_uri) > URL_MAX_LENGTH or not client.allows_redirect_uri(
        redirect_uri
    ):
        raise AuthorizeError("invalid_request", "Unregistered redirect_uri.")
    return redirect_uri


def pick_icons(
    icons: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """
    The server icon to show on the consent page, and a dark-scheme alternative.

    The page follows the user's colour scheme from a light default, so an icon
    without a theme, or for the light theme, comes first.
    """
    light = next((icon for icon in icons if icon.get("theme") != "dark"), None)
    dark = next((icon for icon in icons if icon.get("theme") == "dark"), None)
    if light is None:
        return dark, None
    return light, dark


def resolve_resource(resource: str | None) -> str:
    if resource is None:
        raise AuthorizeError("invalid_target", "Missing resource.")
    if len(resource) > URL_MAX_LENGTH:
        raise AuthorizeError("invalid_target", "resource is too long.")
    validated = validate_resource(resource)
    if validated is None:
        raise AuthorizeError(
            "invalid_target", f"Not an MCP server on this site: {resource!r}."
        )
    return validated


# OAuth 2.1 section 7.10: the consent page must not be framed, so a hidden
# page cannot trick the user into clicking Allow. Both the older header and
# the CSP directive, for every response.
@login_not_required
@csrf_exempt
@xframe_options_deny
@frame_ancestors_none
@require_http_methods(["GET", "POST"])
def authorize(request: HttpRequest) -> HttpResponse:
    """
    Show the consent page, then redirect back to the client with a code.

    GET renders the page. POST records the decision. Both carry the
    authorization request in the query string, revalidated each time.
    """
    if request.method == "POST" and "decision" not in request.POST:
        # RFC 6749 section 3.1 lets clients POST the authorization request
        # itself, from their own site, so without a CSRF token. Carry on as
        # a GET of the same request, so the consent form has one shape.
        return HttpResponseRedirect(f"{request.path}?{request.POST.urlencode()}")
    return _authorize(request)


# CSRF protection is applied here rather than relying on the middleware,
# since a forged consent would let an attacker's client obtain a code.
@csrf_protect
def _authorize(request: HttpRequest) -> HttpResponse:
    params = request.GET
    # Login comes first, so that only users of this site can make the server
    # fetch client metadata documents, or learn which clients it knows.
    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    # Until the client and redirect URI are known good, errors are shown to
    # the user rather than redirected, per OAuth 2.1 section 4.1.2.1.
    try:
        client = load_client(params.get("client_id", ""), fetch=True)
        redirect_uri = resolve_redirect_uri(client, params.get("redirect_uri"))
    except AuthorizeError as exc:
        return render(
            request,
            "django_mcpz/oauth/error.html",
            {"error": exc.error, "description": exc.description},
            status=HTTPStatus.BAD_REQUEST,
        )

    state = params.get("state")

    def redirect(**fields: str) -> HttpResponse:
        query: dict[str, str] = {}
        if state is not None:
            query["state"] = state
        query.update(fields)
        query["iss"] = issuer_url(request)
        separator = "&" if urlsplit(redirect_uri).query else "?"
        return HttpResponseRedirect(redirect_uri + separator + urlencode(query))

    try:
        if params.get("response_type") != "code":
            raise AuthorizeError("unsupported_response_type", "Use response_type=code.")
        code_challenge = params.get("code_challenge", "")
        if not CODE_CHALLENGE_RE.fullmatch(code_challenge):
            raise AuthorizeError(
                "invalid_request", "Missing or invalid code_challenge (PKCE)."
            )
        if params.get("code_challenge_method") != "S256":
            raise AuthorizeError("invalid_request", "Use code_challenge_method=S256.")
        resource = resolve_resource(params.get("resource"))
        scope = params.get("scope", "")
        if len(scope) > SCOPE_MAX_LENGTH:
            raise AuthorizeError("invalid_scope", "scope is too long.")
    except AuthorizeError as exc:
        return redirect(error=exc.error, error_description=exc.description)

    server = discovery.resolve_mcp_server(urlsplit(resource).path)
    assert server is not None  # validate_resource() checked
    info = server.server_info_for(request)
    server_icon, server_icon_dark = pick_icons(info.get("icons", []))
    redirect_host = urlsplit(redirect_uri).hostname or ""
    context = {
        # Passed explicitly, so the template works without the auth context
        # processor.
        "user": request.user,
        "client": client,
        "redirect_uri": redirect_uri,
        "redirect_host": redirect_host,
        "redirect_is_local": redirect_host in LOCAL_HOSTS,
        "resource": resource,
        "server_title": info.get("title", info["name"]),
        "server_icon": server_icon,
        "server_icon_dark": server_icon_dark,
        "scope": scope,
    }
    if request.method == "GET":
        return render(request, "django_mcpz/oauth/authorize.html", context)

    if request.POST.get("decision") != "allow":
        return redirect(
            error="access_denied", error_description="The user denied access."
        )

    value = tokens.generate()
    AuthorizationCode.objects.create(
        digest=tokens.sha256_hex(value),
        client=client,
        user=request.user,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        resource=resource,
        scope=scope,
        expires_at=timezone.now() + oauth_settings.code_lifetime,
    )
    return redirect(code=value)


# Token endpoint


def token_error(
    error: str, description: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST
) -> HttpResponse:
    return JsonResponse(
        {"error": error, "error_description": description},
        status=status,
        headers={"Cache-Control": "no-store"},
    )


def resource_matches(resource: str | None, expected: str) -> bool:
    """
    Whether a token request's resource parameter names the expected resource.

    Compared in canonical form, with the scheme and host lowercased, since
    that is how the authorize endpoint stored it, and clients send the same
    value to both endpoints. Absent means no check, per RFC 8707.
    """
    if resource is None:
        return True
    try:
        return canonical(resource) == expected
    except ValueError:
        # Unbalanced brackets in the host, so not a URL at all.
        return False


def verify_pkce(code_verifier: str, code_challenge: str) -> bool:
    if not (43 <= len(code_verifier) <= 128):
        return False
    digest = hashlib.sha256(code_verifier.encode("ascii", "ignore")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return hmac.compare_digest(expected, code_challenge)


@login_not_required
@csrf_exempt
@require_POST
def token(request: HttpRequest) -> HttpResponse:
    """Exchange an authorization code, or a refresh token, for tokens."""
    params = request.POST
    grant_type = params.get("grant_type")
    try:
        client = load_client(params.get("client_id", ""), fetch=False)
    except AuthorizeError as exc:
        return token_error("invalid_client", exc.description, HTTPStatus.UNAUTHORIZED)

    if grant_type == "authorization_code":
        return exchange_code(client, params)
    elif grant_type == "refresh_token":
        return exchange_refresh_token(client, params)
    return token_error(
        "unsupported_grant_type",
        "Supported grant types: authorization_code, refresh_token.",
    )


def exchange_code(client: Client, params: Any) -> HttpResponse:
    # The row is locked for the whole exchange, so that two requests
    # presenting the same code cannot both succeed: the second waits, then
    # finds the code used.
    with transaction.atomic():
        code = (
            AuthorizationCode.objects.select_for_update()
            .filter(digest=tokens.sha256_hex(params.get("code", "")))
            .first()
        )
        return _exchange_code(client, params, code)


def _exchange_code(
    client: Client,
    params: Any,
    code: AuthorizationCode | None,
) -> HttpResponse:
    if code is None or code.client_id != client.pk:
        return token_error("invalid_grant", "Unknown authorization code.")
    if code.used_at is not None:
        # A replayed code means it leaked: revoke everything issued from it.
        code.revoke_family()
        return token_error("invalid_grant", "Authorization code already used.")
    if code.expires_at <= timezone.now():
        return token_error("invalid_grant", "Authorization code expired.")
    redirect_uri = params.get("redirect_uri")
    if redirect_uri is not None and redirect_uri != code.redirect_uri:
        return token_error("invalid_grant", "redirect_uri does not match.")
    if not resource_matches(params.get("resource"), code.resource):
        return token_error("invalid_target", "resource does not match.")
    if not verify_pkce(params.get("code_verifier", ""), code.code_challenge):
        return token_error("invalid_grant", "code_verifier does not match.")
    if not getattr(code.user, "is_active", True):
        return token_error("invalid_grant", "User is inactive.")

    code.used_at = timezone.now()
    code.save(update_fields=["used_at"])
    return issue_tokens(client, code)


def exchange_refresh_token(client: Client, params: Any) -> HttpResponse:
    # Locked as in exchange_code, so concurrent refreshes cannot both
    # rotate the same token.
    with transaction.atomic():
        refresh = (
            RefreshToken.objects.select_for_update()
            .filter(digest=tokens.sha256_hex(params.get("refresh_token", "")))
            .first()
        )
        return _exchange_refresh_token(client, params, refresh)


def _exchange_refresh_token(
    client: Client,
    params: Any,
    refresh: RefreshToken | None,
) -> HttpResponse:
    if refresh is None or refresh.client_id != client.pk:
        return token_error("invalid_grant", "Unknown refresh token.")
    if refresh.used_at is not None:
        # Refresh tokens rotate, so a second use means the old one leaked.
        if refresh.code is not None:
            refresh.code.revoke_family()
        else:
            refresh.revoke()
            refresh.access_token.revoke()
        return token_error("invalid_grant", "Refresh token already used.")
    if not refresh.is_valid:
        return token_error("invalid_grant", "Refresh token expired or revoked.")
    if not resource_matches(params.get("resource"), refresh.resource):
        return token_error("invalid_target", "resource does not match.")
    if not getattr(refresh.user, "is_active", True):
        return token_error("invalid_grant", "User is inactive.")

    refresh.used_at = timezone.now()
    refresh.save(update_fields=["used_at"])
    # The old access token is left to expire, since a client that refreshes
    # early may still have requests in flight with it. Reuse of this refresh
    # token revokes it along with the rest of the family.
    return issue_tokens(client, refresh)


def issue_tokens(
    client: Client,
    source: AuthorizationCode | RefreshToken,
) -> HttpResponse:
    """Create an access and refresh token pair from a code or refresh token."""
    code = source if isinstance(source, AuthorizationCode) else source.code
    access, access_value = AccessToken.create(
        lifetime=oauth_settings.access_token_lifetime,
        client=client,
        user=source.user,
        code=code,
        resource=source.resource,
        scope=source.scope,
    )
    _, refresh_value = RefreshToken.create(
        lifetime=oauth_settings.refresh_token_lifetime,
        client=client,
        user=source.user,
        code=code,
        resource=source.resource,
        scope=source.scope,
        access_token=access,
    )
    client.record_use()
    body = {
        "access_token": access_value,
        "token_type": "Bearer",
        "expires_in": int(oauth_settings.access_token_lifetime.total_seconds()),
        "refresh_token": refresh_value,
    }
    if source.scope:
        body["scope"] = source.scope
    return JsonResponse(body, headers={"Cache-Control": "no-store"})


# Dynamic client registration


class RegistrationError(Exception):
    def __init__(self, error: str, description: str) -> None:
        super().__init__(description)
        self.error = error
        self.description = description


def validate_registration(metadata: object) -> tuple[str, list[str]]:
    if not isinstance(metadata, dict):
        raise RegistrationError("invalid_client_metadata", "Expected a JSON object.")
    uris = metadata.get("redirect_uris")
    if not isinstance(uris, list) or not uris:
        raise RegistrationError(
            "invalid_redirect_uri", "redirect_uris must be a non-empty list."
        )
    for uri in uris:
        if (
            not isinstance(uri, str)
            or len(uri) > URL_MAX_LENGTH
            or not is_secure_url(uri)
            or urlsplit(uri).fragment
        ):
            raise RegistrationError(
                "invalid_redirect_uri",
                f"Redirect URIs must be HTTPS, or HTTP on localhost: {uri!r}.",
            )
    name = metadata.get("client_name", "")
    if not isinstance(name, str):
        raise RegistrationError(
            "invalid_client_metadata", "client_name must be a string."
        )
    method = metadata.get("token_endpoint_auth_method", "none")
    if method not in ("none", "client_secret_post", "client_secret_basic"):
        raise RegistrationError(
            "invalid_client_metadata",
            f"Unsupported token_endpoint_auth_method {method!r}.",
        )
    return name[:NAME_MAX_LENGTH], uris


@login_not_required
@csrf_exempt
@require_POST
def register(request: HttpRequest) -> HttpResponse:
    """RFC 7591: register a public client, returning its new client_id."""
    if not oauth_settings.dynamic_registration:
        return HttpResponse(status=HTTPStatus.NOT_FOUND)
    try:
        metadata = json.loads(request.body)
    except ValueError:
        metadata = None
    try:
        name, redirect_uris = validate_registration(metadata)
    except RegistrationError as exc:
        return JsonResponse(
            {"error": exc.error, "error_description": exc.description},
            status=HTTPStatus.BAD_REQUEST,
        )
    client = Client.objects.create(
        client_id=secrets.token_urlsafe(24),
        kind=Client.Kind.REGISTERED,
        name=name,
        redirect_uris=redirect_uris,
    )
    # Only public clients are supported, so the response corrects any
    # requested client authentication method, as RFC 7591 allows.
    return JsonResponse(
        {
            "client_id": client.client_id,
            "client_id_issued_at": int(time.time()),
            "client_name": client.name,
            "redirect_uris": client.redirect_uris,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
        status=HTTPStatus.CREATED,
    )


# Revocation


@login_not_required
@csrf_exempt
@require_POST
def revoke(request: HttpRequest) -> HttpResponse:
    """RFC 7009: revoke an access or refresh token."""
    params = request.POST
    try:
        client = load_client(params.get("client_id", ""), fetch=False)
    except AuthorizeError as exc:
        return token_error("invalid_client", exc.description, HTTPStatus.UNAUTHORIZED)
    digest = tokens.sha256_hex(params.get("token", ""))
    # Look for a refresh token first, then an access token, regardless of
    # token_type_hint, which is only a hint.
    refresh = (
        RefreshToken.objects.select_related("access_token", "code")
        .filter(digest=digest)
        .first()
    )
    if refresh is not None and refresh.client_id == client.pk:
        # RFC 7009 section 2.1: revoking a refresh token should revoke the
        # access tokens issued on the same grant too.
        if refresh.code is not None:
            refresh.code.revoke_family()
        else:
            with transaction.atomic():
                refresh.revoke()
                refresh.access_token.revoke()
    else:
        access = AccessToken.objects.filter(digest=digest).first()
        if access is not None and access.client_id == client.pk:
            access.revoke()
    # Unknown tokens are reported as success, so the endpoint reveals nothing.
    return HttpResponse(status=HTTPStatus.OK)
