from __future__ import annotations

import enum
from http import HTTPStatus
from typing import Annotated, Any

import msgspec
from django.contrib.auth.models import AnonymousUser, User
from django.http import HttpRequest, HttpResponse

from django_mcpz.bearer_tokens.auth import token_auth
from django_mcpz.oauth.auth import oauth_auth
from django_mcpz.server import (
    ElicitationDeclinedError,
    ElicitationUnavailableError,
    Icon,
    Image,
    MCPServer,
    ResourceLink,
    Text,
    ToolError,
    elicit,
    public,
)
from tests.models import Widget

server = MCPServer(
    name="example-server",
    version="1.2.3",
    title="Example Server",
    description="Exercises every feature of django-mcpz.",
    website_url="https://github.com/adamchainz/django-mcpz",
    instructions="Example MCP server used in the django-mcpz test suite.",
    icons=[
        Icon(static="diner/icon.png", sizes=("48x48",)),
        {"src": "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'/%3E"},
    ],
    auth=public,
)


@server.tool(
    description="Add two integers.",
    input_schema={
        "type": "object",
        "properties": {
            "a": {"type": "integer"},
            "b": {"type": "integer"},
        },
        "required": ["a", "b"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {"sum": {"type": "integer"}},
        "required": ["sum"],
    },
)
def add(request: HttpRequest, arguments: dict[str, Any]) -> dict[str, int]:
    return {"sum": arguments["a"] + arguments["b"]}


@server.tool(
    name="greet",
    title="Greeter",
    description="Greet someone by name.",
    input_schema={
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "additionalProperties": False,
    },
    read_only=True,
)
def greet_tool(request: HttpRequest, arguments: dict[str, Any]) -> str:
    return f"Hello, {arguments.get('name', 'world')}!"


@server.tool(
    description="Report an in-band tool execution error.",
    input_schema={"type": "object", "additionalProperties": False},
)
def unavailable(request: HttpRequest, arguments: dict[str, Any]) -> None:
    raise ToolError("The flux capacitor is offline. Try the DeLorean instead.")


@server.tool(
    description="Crash with an unexpected exception.",
    input_schema={"type": "object", "additionalProperties": False},
    destructive=True,
    open_world=False,
)
def crash(request: HttpRequest, arguments: dict[str, Any]) -> None:
    raise ValueError("secret internal details")


@server.tool(
    description="Create a widget, then finish as asked, for transaction tests.",
    input_schema={
        "type": "object",
        "properties": {"then": {"type": "string", "enum": ["ok", "error", "crash"]}},
        "required": ["then"],
        "additionalProperties": False,
    },
)
def create_widget(request: HttpRequest, arguments: dict[str, Any]) -> str:
    Widget.objects.create(name="pending", price=1)
    if arguments["then"] == "error":
        raise ToolError("Changed my mind.")
    if arguments["then"] == "crash":
        raise ValueError("boom")
    return "Created."


@server.tool(
    description="Do nothing.",
    input_schema={"type": "object", "additionalProperties": False},
)
def noop(request: HttpRequest, arguments: dict[str, Any]) -> None:
    return None


@server.tool(
    description="Return something JSON cannot represent.",
    input_schema={"type": "object", "additionalProperties": False},
)
def unencodable(request: HttpRequest, arguments: dict[str, Any]) -> object:
    return object()


@server.tool(
    description="Echo the region.",
    input_schema={
        "type": "object",
        "properties": {
            "region": {"type": "string"},
            "shard": {"type": "integer"},
            "fast": {"type": "boolean"},
            "config": {
                "type": "object",
                "properties": {
                    "zone": {"type": "string"},
                },
            },
        },
        "additionalProperties": False,
    },
    icons=[{"src": "https://example.com/regional.png", "mimeType": "image/png"}],
)
def regional(request: HttpRequest, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"region": arguments.get("region")}


def bearer_auth(request: HttpRequest) -> HttpResponse | None:
    if request.headers.get("Authorization") != "Bearer test-token":
        return HttpResponse(status=HTTPStatus.UNAUTHORIZED)
    return None


secure_server = MCPServer(
    name="secure-server",
    version="1.0.0",
    auth=bearer_auth,
)


@secure_server.tool(
    description="Return the secret word.",
    input_schema={"type": "object", "additionalProperties": False},
)
def secret_word(request: HttpRequest, arguments: dict[str, Any]) -> str:
    return "xyzzy"


# Typed schemas: msgspec Structs generate the JSON Schemas and validate
# arguments before the tool runs.


class MultiplyParams(msgspec.Struct):
    a: Annotated[int, msgspec.Meta(description="The first factor.")]
    b: int = 2


class MultiplyResult(msgspec.Struct):
    product: int


@server.tool(
    description="Multiply two integers.",
    input_schema=MultiplyParams,
    output_schema=MultiplyResult,
)
def multiply(request: HttpRequest, params: MultiplyParams) -> MultiplyResult:
    return MultiplyResult(product=params.a * params.b)


class AddTypedParams(msgspec.Struct, forbid_unknown_fields=True):
    a: int
    b: int


@server.tool(
    description="Add two integers, with typed parameters.",
    input_schema=AddTypedParams,
)
def add_typed(request: HttpRequest, params: AddTypedParams) -> dict[str, int]:
    return {"sum": params.a + params.b}


class Point(msgspec.Struct):
    x: int
    y: int


class SegmentParams(msgspec.Struct):
    start: Point
    end: Point
    label: str = ""


@server.tool(
    description="Measure the Manhattan length of a line segment.",
    input_schema=SegmentParams,
)
def segment_length(request: HttpRequest, params: SegmentParams) -> dict[str, Any]:
    length = abs(params.end.x - params.start.x) + abs(params.end.y - params.start.y)
    return {"label": params.label, "length": length}


class ShoutParams(msgspec.Struct):
    message: Annotated[str, msgspec.Meta(description="What to shout.")]
    times: int = 1


class ShoutResult(msgspec.Struct):
    text: str


@server.tool(
    description="Shout a message.",
    read_only=True,
    idempotent=True,
    icons=[
        Icon(static="diner/shout.png", sizes=("48x48",)),
        Icon(static="diner/shout-dark.svg", mime_type="image/svg+xml", theme="dark"),
        {"src": "data:image/png;base64,AAAA", "mimeType": "image/png"},
    ],
)
def shout(request: HttpRequest, params: ShoutParams) -> ShoutResult:
    return ShoutResult(text=" ".join([params.message.upper() + "!"] * params.times))


@server.tool(description="A picture, with a caption.", read_only=True)
def picture(request: HttpRequest) -> list[Text | Image]:
    return [Text("A single grey pixel."), Image(b"\x89PNG\r\n\x1a\n", "image/png")]


@server.tool(description="A link to a report.", read_only=True)
def report_link(request: HttpRequest) -> ResourceLink:
    return ResourceLink(
        uri="https://example.com/reports/1.pdf",
        name="Report 1",
        mime_type="application/pdf",
    )


@server.tool(description="Content blocks mixed with other values.")
def mixed_content(request: HttpRequest) -> list[Any]:
    return [Text("Half a result."), 1]


@server.tool(description="Do nothing, with a generated empty schema.")
def sig_noop(request: HttpRequest) -> None:
    return None


@server.tool(description="A JSON value that is not an object.", read_only=True)
def evens(request: HttpRequest) -> list[int]:
    return [2, 4, 6]


# Speaks only 2026-07-28: clients on earlier revisions are rejected.
strict_server = MCPServer(
    name="strict-server",
    version="1.0.0",
    auth=public,
    minimum_protocol_version="2026-07-28",
)


@strict_server.tool(description="Do nothing, strictly.")
def strict_noop(request: HttpRequest) -> None:
    return None


# Per-tool permissions. The auth callable identifies the user from a header,
# standing in for a real token lookup.


def header_user_auth(request: HttpRequest) -> HttpResponse | None:
    username = request.headers.get("X-User")
    if username is None:
        request.user = AnonymousUser()
    else:
        request.user = User.objects.get(username=username)
    return None


perms_server = MCPServer(
    name="perms-server",
    version="1.0.0",
    auth=header_user_auth,
)


@perms_server.tool(
    description="Available to everyone.",
    input_schema={"type": "object", "additionalProperties": False},
)
def public_tool(request: HttpRequest, arguments: dict[str, Any]) -> str:
    return "public"


@perms_server.tool(
    description="Available to staff.",
    input_schema={"type": "object", "additionalProperties": False},
    permission=lambda request: request.user.is_staff,
)
def staff_tool(request: HttpRequest, arguments: dict[str, Any]) -> str:
    return "staff"


@perms_server.tool(
    description="Available to users with the tests.view_widget permission.",
    input_schema={"type": "object", "additionalProperties": False},
    permission="tests.view_widget",
)
def widget_tool(request: HttpRequest, arguments: dict[str, Any]) -> str:
    return "widgets"


# Authenticated with bearer tokens from the django_mcpz.bearer_tokens app.
bearer_tokens_server = MCPServer(
    name="bearer-tokens-server",
    version="1.0.0",
    auth=token_auth,
)


@bearer_tokens_server.tool(description="Who is calling.", read_only=True)
def whoami(request: HttpRequest) -> dict[str, Any]:
    return {
        "token": request.mcp_token.name,  # type: ignore[attr-defined]
        "user": request.user.get_username(),
    }


@bearer_tokens_server.tool(
    description="Available to users with the tests.view_widget permission.",
    permission="tests.view_widget",
)
def token_widget_tool(request: HttpRequest) -> str:
    return "widgets"


# OAuth

oauth_server = MCPServer(
    name="oauth-server",
    version="1.0.0",
    title="OAuth server",
    icons=[
        Icon(static="oauth/icon.png", sizes=("48x48",)),
        Icon(static="oauth/icon-dark.png", sizes=("48x48",), theme="dark"),
    ],
    auth=oauth_auth,
)


@oauth_server.tool(description="Who is calling.", read_only=True)
def oauth_whoami(request: HttpRequest) -> dict[str, Any]:
    return {
        "client": request.mcp_token.client.name,  # type: ignore[attr-defined]
        "user": request.user.get_username(),
    }


# Elicitation: the tool asks the user a question, and the client answers it by
# calling the tool again, so the tool runs once per question. The user comes
# from a header, as for perms_server, so that state binding can be exercised.

elicitation_server = MCPServer(
    name="elicitation-server",
    version="1.0.0",
    auth=header_user_auth,
)


class Confirmation(msgspec.Struct):
    confirmed: bool = False


class Flavour(enum.Enum):
    vanilla = "vanilla"
    chocolate = "chocolate"


class Dessert(msgspec.Struct):
    flavour: Flavour
    scoops: Annotated[int, msgspec.Meta(ge=1, le=3)] = 1


@elicitation_server.tool(description="Ask for confirmation, then report it.")
def confirm(request: HttpRequest) -> str:
    answer = elicit(request, "Really do the thing?", Confirmation)
    return f"Confirmed: {answer.confirmed}"


@elicitation_server.tool(description="Ask two questions, one after the other.")
def order_dessert(request: HttpRequest) -> dict[str, Any]:
    dessert = elicit(request, "What would you like?", Dessert)
    tip = elicit(request, "Add a tip?", Confirmation)
    return {
        "flavour": dessert.flavour.value,
        "scoops": dessert.scoops,
        "tip": tip.confirmed,
    }


@elicitation_server.tool(description="Ask, with a custom note on expiry.")
def confirm_custom_expiry(request: HttpRequest) -> str:
    answer = elicit(
        request, "Really do the thing?", Confirmation, expired_message="Too slow!"
    )
    return f"Confirmed: {answer.confirmed}"


@elicitation_server.tool(description="Ask, with no note on expiry.")
def confirm_no_expiry_note(request: HttpRequest) -> str:
    answer = elicit(request, "Really do the thing?", Confirmation, expired_message=None)
    return f"Confirmed: {answer.confirmed}"


@elicitation_server.tool(description="Ask inside a ToolError handler.")
def confirm_catching_tool_error(request: HttpRequest) -> str:
    try:
        answer = elicit(request, "Really do the thing?", Confirmation)
    except ToolError:  # pragma: no cover
        return "Caught."
    return f"Confirmed: {answer.confirmed}"


@elicitation_server.tool(description="Ask inside a broad exception handler.")
def confirm_catching(request: HttpRequest) -> str:
    try:
        answer = elicit(request, "Really do the thing?", Confirmation)
    except Exception:  # pragma: no cover
        return "Swallowed."
    return f"Confirmed: {answer.confirmed}"


@elicitation_server.tool(description="Ask with a plain schema, under a fixed key.")
def ask_name(request: HttpRequest) -> str:
    answer = elicit(
        request,
        "What is your name?",
        {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        key="name",
    )
    return f"Hello, {answer['name']}!"


@elicitation_server.tool(description="Ask twice under one key, which is a mistake.")
def ask_twice_under_one_key(request: HttpRequest) -> str:
    elicit(request, "First?", Confirmation, key="same")
    elicit(request, "Second?", Confirmation, key="same")
    return "unreachable"  # pragma: no cover


class Target(msgspec.Struct):
    a: int
    b: int


@elicitation_server.tool(description="Ask about the arguments it was given.")
def confirm_arguments(request: HttpRequest, params: Target) -> str:
    answer = elicit(request, f"Proceed with {params.a} and {params.b}?", Confirmation)
    return f"Confirmed: {answer.confirmed}"


@elicitation_server.tool(description="Create a widget, then ask whether to keep it.")
def confirm_widget(request: HttpRequest) -> str:
    Widget.objects.create(name="pending", price=1)
    elicit(request, "Keep the widget?", Confirmation)
    return "Kept."


@elicitation_server.tool(description="Ask for something form mode cannot collect.")
def ask_impossible(request: HttpRequest) -> str:
    elicit(request, "Where to?", SegmentParams)
    return "unreachable"  # pragma: no cover


@elicitation_server.tool(description="Ask for confirmation, as the calling user.")
def confirm_as_user(request: HttpRequest) -> str:
    answer = elicit(request, "Really do the thing?", Confirmation)
    return f"{request.user} confirmed: {answer.confirmed}"


@elicitation_server.tool(description="Refuse when it cannot ask.")
def confirm_or_refuse(request: HttpRequest) -> str:
    try:
        elicit(request, "Really do the thing?", Confirmation)
    except ElicitationUnavailableError:
        return "Refused without confirmation."
    return "Done."


@elicitation_server.tool(description="Carry on when the user declines.")
def confirm_or_carry_on(request: HttpRequest) -> str:
    try:
        answer = elicit(request, "Really do the thing?", Confirmation)
    except ElicitationDeclinedError:
        return "Carried on without it."
    return f"Confirmed: {answer.confirmed}"


@elicitation_server.tool(description="Never asks anything.")
def no_question(request: HttpRequest) -> str:
    return "No question."


# Servers that ask the same question under each kind of authentication, to
# test that a question is answerable only by the caller it was asked of.


def header_identity_auth(request: HttpRequest) -> HttpResponse | None:
    """Identify a user from a header, without a database lookup."""
    username = request.headers.get("X-User")
    if username is None:
        request.user = AnonymousUser()
    else:
        request.user = User(pk=1, username=username)
    return None


identified_elicitation_server = MCPServer(
    name="identified-elicitation-server",
    version="1.0.0",
    auth=header_identity_auth,
)
identified_elicitation_server.tool(
    description="Ask for confirmation, as the calling user."
)(confirm_as_user)

oauth_elicitation_server = MCPServer(
    name="oauth-elicitation-server",
    version="1.0.0",
    auth=oauth_auth,
)
oauth_elicitation_server.tool(description="Ask for confirmation, as the calling user.")(
    confirm_as_user
)

bearer_elicitation_server = MCPServer(
    name="bearer-elicitation-server",
    version="1.0.0",
    auth=token_auth,
)
bearer_elicitation_server.tool(
    description="Ask for confirmation, as the calling user."
)(confirm_as_user)
