from __future__ import annotations

import base64
import contextlib
import logging
import mimetypes
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Literal, TypeVar, overload
from urllib.parse import urlsplit

import msgspec
import msgspec.json
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import connections, transaction
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed
from django.http.request import validate_host
from django.templatetags.static import static as static_url
from django_msgspec import enc_hook
from msgspec import UnsetType

from django_mcpz.elicitation import BadStateError, Elicitation, InputRequired
from django_mcpz.headers import decode_header_value
from django_mcpz.jsonrpc import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    MessageError,
    decode_message,
    error_response,
    result_response,
)
from django_mcpz.legacy import (
    LEGACY_PROTOCOL_VERSIONS,
    is_legacy_request,
    legacy_dispatch,
)
from django_mcpz.schemas import (
    elicitation_schema,
    inspect_types,
    is_struct_type,
    reject_header_annotations,
    type_schema,
)

logger = logging.getLogger("django_mcpz")
# One record per tools/call, with structured attributes for handlers.
call_logger = logging.getLogger("django_mcpz.calls")

PROTOCOL_VERSION = "2026-07-28"
SUPPORTED_PROTOCOL_VERSIONS = [PROTOCOL_VERSION]
MINIMUM_PROTOCOL_VERSIONS = (LEGACY_PROTOCOL_VERSIONS[-1], PROTOCOL_VERSION)

# MCP-defined error codes (reserved sub-range -32020 to -32099)
HEADER_MISMATCH = -32020
UNSUPPORTED_PROTOCOL_VERSION = -32022

META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

# Required on tools/list and server/discover results. Five minutes suits the
# mostly static tool lists typical of this package. Private scope keeps
# results within the caller's authorization context, which
# permission-filtered tool lists depend on.
_CACHE_FIELDS = {"ttlMs": 300_000, "cacheScope": "private"}

# The specification's constraints on tool names.
TOOL_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,128}")


class BaseToolError(Exception):
    """
    The base of the exceptions reported in-band, as a tool execution error
    carrying the exception's message, rather than as a failure.
    """


class ToolError(BaseToolError):
    """
    Raise in a tool function to report a tool execution error in-band, so the
    calling language model can see the message and self-correct.
    """


class ElicitationUnavailableError(BaseToolError):
    """
    Raised by elicit() when the client cannot answer questions from a tool.
    """


class ElicitationDeclinedError(BaseToolError):
    """
    Raised by elicit() when the user declined or dismissed the question.
    """


StructT = TypeVar("StructT", bound=msgspec.Struct)

EXPIRED_MESSAGE = "Your earlier answer expired, so this is asked again."

_NO_ELICITATION = (
    "This tool needs to ask you a question, which this client does not"
    " support (form mode MCP elicitation)."
)
_NO_ELICITATION_PROTOCOL = (
    "This tool needs to ask you a question, which needs MCP version"
    f" {PROTOCOL_VERSION}. This client is using an earlier version."
)


@overload
def elicit(
    request: HttpRequest,
    message: str,
    schema: type[StructT],
    *,
    key: str | None = None,
    expired_message: str | None = EXPIRED_MESSAGE,
) -> StructT: ...


@overload
def elicit(
    request: HttpRequest,
    message: str,
    schema: dict[str, Any],
    *,
    key: str | None = None,
    expired_message: str | None = EXPIRED_MESSAGE,
) -> dict[str, Any]: ...


def elicit(
    request: HttpRequest,
    message: str,
    schema: type[StructT] | dict[str, Any],
    *,
    key: str | None = None,
    expired_message: str | None = EXPIRED_MESSAGE,
) -> StructT | dict[str, Any]:
    """
    Ask the user a question from inside a tool function, returning the answer.

    The protocol has no way to pause a call, so the first time a tool reaches
    this it raises, the server asks the client for the answer, and the client
    answers by calling the tool again. This call then returns the answer and
    the tool carries on, which means everything above it runs once per
    question, and a tool must not depend on running only once.

    When the user took too long to answer, the answers so far are dropped and
    the question asked again, with expired_message shown above it, unless it
    is None.
    """
    elicitation = getattr(request, "mcp_elicitation", None)
    if elicitation is None:
        raise ImproperlyConfigured(
            "elicit() works only inside a tool function, called by an MCP"
            " server, which puts the state it needs on the request."
        )
    if elicitation.unavailable is not None:
        raise ElicitationUnavailableError(elicitation.unavailable)
    key = elicitation.claim_key(key)
    answer = elicitation.answers.get(key)
    if answer is None:
        if elicitation.expired and expired_message is not None:
            message = f"{expired_message}\n\n{message}"
        raise InputRequired(key, message, _requested_schema(schema))
    if answer.action != "accept":
        wording = "declined" if answer.action == "decline" else "dismissed"
        raise ElicitationDeclinedError(f"The user {wording} the question: {message}")
    content = answer.content or {}
    if isinstance(schema, dict):
        return content
    try:
        return msgspec.convert(content, schema)
    except msgspec.ValidationError:
        # The specification asks servers to ask again, rather than error. The
        # answer that did not fit is dropped, so it does not travel on in the
        # state as though it had been accepted.
        del elicitation.answers[key]
        raise InputRequired(key, message, _requested_schema(schema)) from None


def _requested_schema(schema: type[msgspec.Struct] | dict[str, Any]) -> dict[str, Any]:
    """
    The requestedSchema to ask with, generated from a type, or a plain schema
    served as given, as for a tool's input_schema.
    """
    if isinstance(schema, dict):
        return schema
    return elicitation_schema(schema)


@dataclass(frozen=True)
class Icon:
    """
    An icon for a server or a tool, served by this site and resolved to an
    absolute URL per request.
    Clients are told to reject icons from other origins, so same-origin
    files are the natural home for them: a static file, or a URL path served
    by a view.
    """

    static: str | None = None
    path: str | None = None
    mime_type: str | None = None
    sizes: tuple[str, ...] | None = None
    theme: Literal["light", "dark"] | None = None

    def __post_init__(self) -> None:
        if (self.static is None) == (self.path is None):
            raise ImproperlyConfigured("Icon needs exactly one of static or path.")

    def resolve(self, request: HttpRequest) -> dict[str, Any]:
        if self.static is not None:
            location = static_url(self.static)
        else:
            assert self.path is not None  # __post_init__ requires one of the two
            location = self.path
        icon: dict[str, Any] = {"src": request.build_absolute_uri(location)}
        mime_type = self.mime_type
        if mime_type is None:
            # From the path alone, since some static files storages append
            # a query string, for cache busting or signing.
            mime_type = mimetypes.guess_type(urlsplit(location).path)[0]
        if mime_type is not None:
            icon["mimeType"] = mime_type
        if self.sizes is not None:
            icon["sizes"] = list(self.sizes)
        if self.theme is not None:
            icon["theme"] = self.theme
        return icon


class Content:
    """
    A content block for a tool result. Return one, or a list of them, from a
    tool function to send content other than text or structured output.
    """

    def to_dict(self) -> dict[str, Any]:
        raise NotImplementedError  # pragma: no cover


@dataclass(frozen=True)
class Text(Content):
    """A text block, for mixing with other content."""

    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"type": "text", "text": self.text}


@dataclass(frozen=True)
class Image(Content):
    """An image, from its bytes."""

    data: bytes
    mime_type: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "image",
            "data": base64.b64encode(self.data).decode(),
            "mimeType": self.mime_type,
        }


@dataclass(frozen=True)
class Audio(Content):
    """An audio clip, from its bytes."""

    data: bytes
    mime_type: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "audio",
            "data": base64.b64encode(self.data).decode(),
            "mimeType": self.mime_type,
        }


@dataclass(frozen=True)
class ResourceLink(Content):
    """A link to a resource the client may fetch, such as a URL on this site."""

    uri: str
    name: str
    title: str | None = None
    description: str | None = None
    mime_type: str | None = None
    size: int | None = None

    def to_dict(self) -> dict[str, Any]:
        block: dict[str, Any] = {
            "type": "resource_link",
            "uri": self.uri,
            "name": self.name,
        }
        if self.title is not None:
            block["title"] = self.title
        if self.description is not None:
            block["description"] = self.description
        if self.mime_type is not None:
            block["mimeType"] = self.mime_type
        if self.size is not None:
            block["size"] = self.size
        return block


@dataclass(frozen=True)
class EmbeddedResource(Content):
    """The contents of a resource, as text or bytes, identified by a URI."""

    uri: str
    text: str | None = None
    blob: bytes | None = None
    mime_type: str | None = None

    def __post_init__(self) -> None:
        if (self.text is None) == (self.blob is None):
            raise ValueError("EmbeddedResource needs exactly one of text or blob.")

    def to_dict(self) -> dict[str, Any]:
        resource: dict[str, Any] = {"uri": self.uri}
        if self.mime_type is not None:
            resource["mimeType"] = self.mime_type
        if self.text is not None:
            resource["text"] = self.text
        else:
            assert self.blob is not None  # __post_init__ requires one of the two
            resource["blob"] = base64.b64encode(self.blob).decode()
        return {"type": "resource", "resource": resource}


@dataclass(frozen=True)
class Tool:
    name: str
    func: Callable[..., Any]
    definition: dict[str, Any]
    input_type: Any
    known_keys: frozenset[str] | None
    takes_params: bool
    icons: tuple[Icon | dict[str, Any], ...] | None
    permission: Callable[[HttpRequest], bool] | None

    def permitted(self, request: HttpRequest) -> bool:
        return self.permission is None or self.permission(request)


def _has_perm(codename: str) -> Callable[[HttpRequest], bool]:
    def check(request: HttpRequest) -> bool:
        try:
            user = request.user
        except AttributeError:
            raise ImproperlyConfigured(
                f"The permission {codename!r} needs request.user, but nothing set"
                " it. Set it in the server's auth callable, or use Django's"
                " AuthenticationMiddleware."
            ) from None
        return user.has_perm(codename)

    return check


def _resolve_icons(
    icons: tuple[Icon | dict[str, Any], ...], request: HttpRequest
) -> list[dict[str, Any]]:
    return [icon.resolve(request) if isinstance(icon, Icon) else icon for icon in icons]


def _tool_definition(tool: Tool, request: HttpRequest) -> dict[str, Any]:
    if tool.icons is None:
        return tool.definition
    return {**tool.definition, "icons": _resolve_icons(tool.icons, request)}


@contextlib.contextmanager
def _savepoints() -> Iterator[None]:
    """
    Run a tool in a savepoint on each open transaction, as under
    ATOMIC_REQUESTS, so that a tool that raises leaves no writes behind while
    the request's transaction stays usable for the error result. Without an
    open transaction, autocommit applies, as for any view.
    """
    with contextlib.ExitStack() as stack:
        for connection in connections.all(initialized_only=True):
            if connection.in_atomic_block:
                stack.enter_context(transaction.atomic(using=connection.alias))
        yield


def _tool_result(output: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"isError": False}
    if output is None:
        result["content"] = []
    elif isinstance(output, str):
        result["content"] = [{"type": "text", "text": output}]
    elif isinstance(output, Content):
        result["content"] = [output.to_dict()]
    elif isinstance(output, (list, tuple)) and any(
        isinstance(item, Content) for item in output
    ):
        if not all(isinstance(item, Content) for item in output):
            raise TypeError(
                "A tool returning content blocks must return only content blocks."
            )
        result["content"] = [item.to_dict() for item in output]
    else:
        encoded = msgspec.json.encode(output, enc_hook=enc_hook)
        result["content"] = [{"type": "text", "text": encoded.decode()}]
        # structuredContent must be a JSON object, so other values, such as
        # lists and numbers, travel as text only. Clients validate this, and
        # some reject the whole result otherwise.
        if encoded.startswith(b"{"):
            # Reuse the encoding: msgspec inlines Raw values when serializing
            # the response, avoiding encoding the output twice.
            result["structuredContent"] = msgspec.Raw(encoded)
    return result


def host_allowed(host: str) -> bool:
    """
    Whether the host is in ALLOWED_HOSTS, with Django's DEBUG allowance.

    The host is a lowercase name or IP address without a port, as from
    urlsplit().hostname, which strips the brackets from IPv6 addresses.
    ALLOWED_HOSTS entries keep them, so they are put back for comparison.
    """
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    allowed_hosts = settings.ALLOWED_HOSTS
    if settings.DEBUG and not allowed_hosts:
        allowed_hosts = [".localhost", "127.0.0.1", "[::1]"]
    return validate_host(host, allowed_hosts)


def public(request: HttpRequest) -> HttpResponse | None:
    """
    Allow every request, for a server without authentication.
    """
    return None


class MCPServer:
    """
    A Model Context Protocol (MCP) server, speaking MCP protocol revision
    2026-07-28 over the Streamable HTTP transport. Attach tool functions with
    the tool() decorator and route the server itself as the view.
    """

    def __init__(
        self,
        *,
        name: str,
        version: str,
        title: str | None = None,
        description: str | None = None,
        website_url: str | None = None,
        instructions: str | None = None,
        icons: list[Icon | dict[str, Any]] | None = None,
        auth: Callable[[HttpRequest], HttpResponse | None],
        minimum_protocol_version: Literal["2025-03-26", "2026-07-28"] = "2025-03-26",
    ) -> None:
        if minimum_protocol_version not in MINIMUM_PROTOCOL_VERSIONS:
            raise ImproperlyConfigured(
                "minimum_protocol_version must be one of"
                f" {', '.join(MINIMUM_PROTOCOL_VERSIONS)}, not"
                f" {minimum_protocol_version!r}."
            )
        if not callable(auth):
            raise ImproperlyConfigured(
                "auth must be a callable, such as django_mcpz.server.public or"
                f" django_mcpz.bearer_tokens.auth.token_auth, not {auth!r}."
            )
        self.server_info: dict[str, str] = {"name": name, "version": version}
        if title is not None:
            self.server_info["title"] = title
        if description is not None:
            self.server_info["description"] = description
        if website_url is not None:
            self.server_info["websiteUrl"] = website_url
        self.instructions = instructions
        self.icons = tuple(icons) if icons is not None else None
        self.auth = auth
        self.minimum_protocol_version = minimum_protocol_version
        self._serve_legacy = minimum_protocol_version != PROTOCOL_VERSION
        self.supported_versions = list(SUPPORTED_PROTOCOL_VERSIONS)
        if self._serve_legacy:
            self.supported_versions.extend(LEGACY_PROTOCOL_VERSIONS)
        self._tools: dict[str, Tool] = {}

    def server_info_for(self, request: HttpRequest) -> dict[str, Any]:
        """The server's name, version, title, and icons, for the request."""
        if self.icons is None:
            return self.server_info
        return {**self.server_info, "icons": _resolve_icons(self.icons, request)}

    # Tool registration

    def tool(
        self,
        *,
        description: str,
        input_schema: dict[str, Any] | type[Any] | None = None,
        name: str | None = None,
        title: str | None = None,
        output_schema: dict[str, Any] | type[Any] | None = None,
        read_only: bool | None = None,
        destructive: bool | None = None,
        idempotent: bool | None = None,
        open_world: bool | None = None,
        icons: list[Icon | dict[str, Any]] | None = None,
        permission: Callable[[HttpRequest], bool] | str | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register the decorated function as an MCP tool on this server."""

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            tool_name = name if name is not None else func.__name__
            if TOOL_NAME_RE.fullmatch(tool_name) is None:
                raise ImproperlyConfigured(
                    f"Tool name {tool_name!r} is invalid: names are 1 to 128"
                    " characters of letters, digits, '_', '-', and '.'."
                )
            if tool_name in self._tools:
                raise ImproperlyConfigured(
                    f"A tool named {tool_name!r} is already registered."
                )
            permission_check = (
                _has_perm(permission) if isinstance(permission, str) else permission
            )
            known_keys: frozenset[str] | None = None
            return_type = None
            takes_params = True
            if isinstance(input_schema, dict):
                input_type = None
                input_schema_dict = input_schema
            else:
                if input_schema is None:
                    input_type, return_type = inspect_types(func, tool_name)
                    takes_params = input_type is not None
                else:
                    input_type = input_schema
                if input_type is None:
                    # A function taking only the request has no parameters.
                    input_schema_dict = {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    }
                    known_keys = frozenset()
                else:
                    input_schema_dict = type_schema(input_type)
                    properties = input_schema_dict.get("properties")
                    if (
                        isinstance(properties, dict)
                        and "additionalProperties" not in input_schema_dict
                    ):
                        # Unknown arguments are rejected by default: tools are
                        # called by language models, for which a silently
                        # ignored misspelt argument is an invisible bug, where
                        # an in-band error allows self-correction.
                        input_schema_dict["additionalProperties"] = False
                        known_keys = frozenset(properties)
            definition: dict[str, Any] = {
                "name": tool_name,
                "description": description,
                "inputSchema": input_schema_dict,
            }
            if title is not None:
                definition["title"] = title
            if output_schema is None:
                if is_struct_type(return_type):
                    definition["outputSchema"] = type_schema(return_type)
            elif isinstance(output_schema, dict):
                definition["outputSchema"] = output_schema
            else:
                definition["outputSchema"] = type_schema(output_schema)
            annotations: dict[str, Any] = {}
            if read_only is not None:
                annotations["readOnlyHint"] = read_only
            if destructive is not None:
                annotations["destructiveHint"] = destructive
            if idempotent is not None:
                annotations["idempotentHint"] = idempotent
            if open_world is not None:
                annotations["openWorldHint"] = open_world
            if annotations:
                definition["annotations"] = annotations
            reject_header_annotations(input_schema_dict)
            self._tools[tool_name] = Tool(
                name=tool_name,
                func=func,
                definition=definition,
                input_type=input_type,
                known_keys=known_keys,
                takes_params=takes_params,
                icons=tuple(icons) if icons is not None else None,
                permission=permission_check,
            )
            return func

        return decorator

    # The view

    # MCP clients are not browsers and authenticate per-request, so the
    # server is exempt from CSRF checks and from LoginRequiredMiddleware.
    csrf_exempt = True
    login_required = False

    def __call__(self, request: HttpRequest) -> HttpResponse:
        # The 2026-07-28 revision removed the GET/SSE stream and
        # DELETE-terminated sessions: the server accepts POST only.
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])

        if not self._origin_allowed(request):
            return error_response(
                None,
                INVALID_REQUEST,
                "Invalid Origin header",
                status=HTTPStatus.FORBIDDEN,
            )

        auth_response = self.auth(request)
        if auth_response is not None:
            return auth_response

        try:
            message = decode_message(request.body)
        except MessageError as exc:
            return error_response(
                exc.request_id,
                exc.code,
                exc.error_message,
                status=HTTPStatus.BAD_REQUEST,
            )

        method = message.method
        request_id = message.id
        if isinstance(request_id, UnsetType):
            # A notification. This protocol revision defines no client-to-
            # server notifications, and no header requirements for
            # notification POSTs: acknowledge and do nothing.
            return HttpResponse(status=HTTPStatus.ACCEPTED)

        params = message.params
        if isinstance(params, UnsetType):
            params = {}

        if self._serve_legacy and is_legacy_request(request):
            return legacy_dispatch(self, request, request_id, method, params)

        error = self._validate_metadata(request, request_id, method, params)
        if error is not None:
            return error

        if method == "server/discover":
            return self._discover(request, request_id)
        elif method == "tools/list":
            return self._tools_list(request, request_id, params)
        elif method == "tools/call":
            return self._tools_call(request, request_id, params)
        elif method == "initialize":
            # Legacy (pre-2026-07-28) clients open with an initialize
            # handshake. Name the supported versions, since this error may be
            # the only diagnostic such clients can surface.
            return error_response(
                request_id,
                METHOD_NOT_FOUND,
                (
                    "Method not found: 'initialize'. This server only supports"
                    " stateless MCP protocol versions:"
                    f" {', '.join(SUPPORTED_PROTOCOL_VERSIONS)}."
                ),
                status=HTTPStatus.NOT_FOUND,
            )
        else:
            return error_response(
                request_id,
                METHOD_NOT_FOUND,
                f"Method not found: {method!r}",
                status=HTTPStatus.NOT_FOUND,
            )

    def _origin_allowed(self, request: HttpRequest) -> bool:
        origin = request.headers.get("Origin")
        if origin is None:
            return True
        try:
            host = urlsplit(origin).hostname
        except ValueError:
            return False
        if host is None:
            return False
        return host_allowed(host)

    def _validate_metadata(
        self,
        request: HttpRequest,
        request_id: str | int,
        method: str,
        params: dict[str, Any],
    ) -> HttpResponse | None:
        header_version = request.headers.get("MCP-Protocol-Version")
        if header_version is None:
            return error_response(
                request_id,
                HEADER_MISMATCH,
                "Missing required MCP-Protocol-Version header",
                status=HTTPStatus.BAD_REQUEST,
            )
        if header_version not in SUPPORTED_PROTOCOL_VERSIONS:
            return error_response(
                request_id,
                UNSUPPORTED_PROTOCOL_VERSION,
                "Unsupported protocol version",
                data={
                    "supported": self.supported_versions,
                    "requested": header_version,
                },
                status=HTTPStatus.BAD_REQUEST,
            )

        meta = params.get("_meta")
        if not isinstance(meta, dict):
            meta = {}
        meta_version = meta.get(META_PROTOCOL_VERSION)
        if meta_version is None:
            return error_response(
                request_id,
                INVALID_PARAMS,
                f"Missing required _meta field: {META_PROTOCOL_VERSION!r}",
                status=HTTPStatus.BAD_REQUEST,
            )
        if META_CLIENT_CAPABILITIES not in meta:
            return error_response(
                request_id,
                INVALID_PARAMS,
                f"Missing required _meta field: {META_CLIENT_CAPABILITIES!r}",
                status=HTTPStatus.BAD_REQUEST,
            )
        if meta_version != header_version:
            return error_response(
                request_id,
                HEADER_MISMATCH,
                (
                    "Header mismatch: MCP-Protocol-Version header value"
                    f" {header_version!r} does not match body value"
                    f" {meta_version!r}"
                ),
                status=HTTPStatus.BAD_REQUEST,
            )

        header_method = request.headers.get("Mcp-Method")
        if header_method is None:
            return error_response(
                request_id,
                HEADER_MISMATCH,
                "Missing required Mcp-Method header",
                status=HTTPStatus.BAD_REQUEST,
            )
        if header_method != method:
            return error_response(
                request_id,
                HEADER_MISMATCH,
                (
                    f"Header mismatch: Mcp-Method header value {header_method!r}"
                    f" does not match body value {method!r}"
                ),
                status=HTTPStatus.BAD_REQUEST,
            )
        return None

    # Method handlers

    def _discover(self, request: HttpRequest, request_id: str | int) -> HttpResponse:
        result: dict[str, Any] = {
            "supportedVersions": self.supported_versions,
            "capabilities": {"tools": {}},
            **_CACHE_FIELDS,
        }
        if self.instructions is not None:
            result["instructions"] = self.instructions
        return self._result(request, request_id, result)

    def _tools_list(
        self, request: HttpRequest, request_id: str | int, params: dict[str, Any]
    ) -> HttpResponse:
        if params.get("cursor") is not None:
            # All tools are returned in one page, so no cursor is ever valid.
            # The pagination specification asks for an invalid params error,
            # at the JSON-RPC level, so HTTP 200 like any other response.
            return error_response(request_id, INVALID_PARAMS, "Invalid cursor")
        return self._result(
            request,
            request_id,
            {
                "tools": [
                    _tool_definition(tool, request)
                    for tool in self._tools.values()
                    if tool.permitted(request)
                ],
                **_CACHE_FIELDS,
            },
        )

    def _tools_call(
        self,
        request: HttpRequest,
        request_id: str | int,
        params: dict[str, Any],
        *,
        legacy: bool = False,
    ) -> HttpResponse:
        name = params.get("name")
        if not isinstance(name, str):
            return error_response(request_id, INVALID_PARAMS, "Missing tool name")

        if not legacy:
            error = _validate_name_header(request, request_id, name)
            if error is not None:
                return error

        tool = self._tools.get(name)
        if tool is None or not tool.permitted(request):
            # A tool the caller may not use is omitted from tools/list, so
            # treat calling it the same as calling a nonexistent one.
            return error_response(request_id, INVALID_PARAMS, f"Unknown tool: {name!r}")

        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        elif not isinstance(arguments, dict):
            return error_response(
                request_id, INVALID_PARAMS, "arguments must be an object"
            )

        try:
            elicitation = Elicitation.for_call(
                request,
                self.server_info["name"],
                name,
                arguments,
                input_responses=params.get("inputResponses"),
                request_state=params.get("requestState"),
                unavailable=_elicitation_unavailable(params, legacy=legacy),
            )
        except BadStateError as exc:
            return error_response(request_id, INVALID_PARAMS, str(exc))

        started = time.perf_counter()
        response, outcome = self._run_tool(
            request, request_id, tool, arguments, elicitation
        )
        duration = time.perf_counter() - started
        call_logger.info(
            "Tool %r: %s in %.1fms",
            name,
            outcome,
            duration * 1000,
            extra={
                "server": self,
                "request": request,
                "tool": name,
                "arguments": arguments,
                "outcome": outcome,
                "duration": duration,
            },
        )
        return response

    def _run_tool(
        self,
        request: HttpRequest,
        request_id: str | int,
        tool: Tool,
        arguments: dict[str, Any],
        elicitation: Elicitation,
    ) -> tuple[HttpResponse, str]:
        """Validate the arguments and run the tool, returning the outcome too."""
        # Input validation errors are tool execution errors, reported in-band
        # so the calling model can self-correct.
        if tool.known_keys is not None:
            unknown = sorted(set(arguments) - tool.known_keys)
            if unknown:
                names = ", ".join(f"`{key}`" for key in unknown)
                plural = "s" if len(unknown) > 1 else ""
                return (
                    self._tool_error(
                        request,
                        request_id,
                        f"Invalid arguments: unknown field{plural} {names}",
                    ),
                    "invalid_arguments",
                )
        tool_arguments: Any = arguments
        if tool.input_type is not None:
            try:
                tool_arguments = msgspec.convert(arguments, tool.input_type)
            except msgspec.ValidationError as exc:
                return (
                    self._tool_error(request, request_id, f"Invalid arguments: {exc}"),
                    "invalid_arguments",
                )

        request.mcp_elicitation = elicitation  # type: ignore[attr-defined]
        try:
            with _savepoints():
                if tool.takes_params:
                    output = tool.func(request, tool_arguments)
                else:
                    output = tool.func(request)
                result = _tool_result(output)
        except InputRequired as exc:
            # Raised past the savepoints, so under ATOMIC_REQUESTS anything
            # the tool wrote before asking is rolled back, ready for it to run
            # again.
            return (
                self._input_required(request, request_id, elicitation, exc),
                "input_required",
            )
        except BaseToolError as exc:
            return self._tool_error(request, request_id, str(exc)), "tool_error"
        except Exception:
            logger.exception("Tool %r raised an exception", tool.name)
            return (
                self._tool_error(
                    request, request_id, f"Tool {tool.name!r} failed unexpectedly."
                ),
                "exception",
            )

        return self._result(request, request_id, result), "ok"

    def _tool_error(
        self, request: HttpRequest, request_id: str | int, text: str
    ) -> HttpResponse:
        # Tool execution failures are reported in-band, not as JSON-RPC
        # errors, so the calling model can see them and self-correct.
        return self._result(
            request,
            request_id,
            {"content": [{"type": "text", "text": text}], "isError": True},
        )

    def _input_required(
        self,
        request: HttpRequest,
        request_id: str | int,
        elicitation: Elicitation,
        required: InputRequired,
    ) -> HttpResponse:
        """
        Ask the client for the input a tool needs, with the state that lets
        any instance of this server resume the call when the answer arrives.
        """
        return self._result(
            request,
            request_id,
            {
                "inputRequests": {
                    required.key: {
                        "method": "elicitation/create",
                        "params": {
                            "mode": "form",
                            "message": required.message,
                            "requestedSchema": required.requested_schema,
                        },
                    }
                },
                "requestState": elicitation.request_state(required.key),
            },
            result_type="input_required",
        )

    def _result(
        self,
        request: HttpRequest,
        request_id: str | int,
        result: dict[str, Any],
        *,
        result_type: str = "complete",
    ) -> HttpResponse:
        return result_response(
            request_id,
            {
                "resultType": result_type,
                **result,
                "_meta": {META_SERVER_INFO: self.server_info_for(request)},
            },
        )


def _elicitation_unavailable(params: dict[str, Any], *, legacy: bool) -> str | None:
    """
    Why a tool cannot ask this caller a question, or None when it can.

    The 2026-07-28 revision carries the client's capabilities on every
    request, already required by the metadata validation, so support is known
    per call rather than negotiated once. An elicitation object with no mode
    in it means form mode, for clients that name none.
    """
    if legacy:
        return _NO_ELICITATION_PROTOCOL
    capabilities = params["_meta"][META_CLIENT_CAPABILITIES]
    if not isinstance(capabilities, dict):
        return _NO_ELICITATION
    elicitation = capabilities.get("elicitation")
    if isinstance(elicitation, dict) and (not elicitation or "form" in elicitation):
        return None
    return _NO_ELICITATION


def _validate_name_header(
    request: HttpRequest, request_id: str | int, name: str
) -> HttpResponse | None:
    header_name = request.headers.get("Mcp-Name")
    if header_name is None:
        return error_response(
            request_id,
            HEADER_MISMATCH,
            "Missing required Mcp-Name header",
            status=HTTPStatus.BAD_REQUEST,
        )
    try:
        decoded = decode_header_value(header_name)
    except ValueError:
        return error_response(
            request_id,
            HEADER_MISMATCH,
            "Malformed Mcp-Name header",
            status=HTTPStatus.BAD_REQUEST,
        )
    if decoded != name:
        return error_response(
            request_id,
            HEADER_MISMATCH,
            (
                f"Header mismatch: Mcp-Name header value {decoded!r} does not"
                f" match body value {name!r}"
            ),
            status=HTTPStatus.BAD_REQUEST,
        )
    return None
