Servers
=======

.. currentmodule:: django_mcpz.server

An MCP server is what AI clients connect to: a set of tools, served at a single URL.
In django-mcpz a server is a plain, POST-only, synchronous Django view: each request stands alone, like a call to any HTTP API, and gets one JSON response.
django-mcpz speaks the current protocol, MCP version 2026-07-28, and the “legacy” 2025 versions too.
See :ref:`server-protocol-support` and :ref:`server-legacy` for the details.

Requests are `JSON-RPC <https://www.jsonrpc.org/specification>`__, a small convention for JSON POST bodies carrying a ``method`` name and ``params``.
Clients send three kinds: |server/discover|__ asks what the server is and can do, |tools/list|__ asks which tools exist and how to call them, and |tools/call|__ runs one.

.. |server/discover| replace:: ``server/discover``
__ https://modelcontextprotocol.io/specification/draft/server/discover

.. |tools/list| replace:: ``tools/list``
__ https://modelcontextprotocol.io/specification/draft/server/tools#listing-tools

.. |tools/call| replace:: ``tools/call``
__ https://modelcontextprotocol.io/specification/draft/server/tools#calling-tools

Define a server by creating an :class:`MCPServer` instance, attaching tool functions with its :meth:`~MCPServer.tool` decorator, and routing the server itself as the view, at whatever URL you like.

Put each server in an ``mcp`` module of one of your apps, alongside its ``models.py`` and ``admin.py``: an ``mcp.py`` file, or an ``mcp`` package for a server with many tools.
That keeps servers easy to find, and is the convention these docs and the example project follow.

For example, in ``example/mcp.py``:

.. code-block:: python

    from typing import Literal

    import msgspec

    from django_mcpz.server import MCPServer
    from django_mcpz.bearer_tokens.auth import token_auth
    from example.models import Order

    server = MCPServer(
        name="shop",
        version="1.0.0",
        instructions="Query the shop’s order database.",
        auth=token_auth,
    )


    class CountOrdersParams(msgspec.Struct):
        status: Literal["pending", "shipped", "cancelled"] | None = None


    class CountOrdersResult(msgspec.Struct):
        count: int


    @server.tool(
        description="Count Order rows, optionally filtered by status.",
        read_only=True,
    )
    def count_orders(request, params: CountOrdersParams) -> CountOrdersResult:
        qs = Order.objects.all()
        if params.status is not None:
            qs = qs.filter(status=params.status)
        return CountOrdersResult(count=qs.count())

The tool’s JSON Schemas are generated from the params argument’s type annotation and the return annotation, and arguments are validated before the tool runs.
See :ref:`server-typed-schemas`.
Plain ``dict`` JSON Schemas work as ``input_schema`` too, wherever you need exact control.

Route the server in ``urls.py``:

.. code-block:: python

    from django.urls import path

    from example.mcp import server

    urlpatterns = [
        path("mcp", server),
    ]

.. tip::

    Avoid a trailing slash in your MCP server’s URL, routing it with ``path("mcp", server)`` rather than ``path("mcp/", server)``.
    Some clients strip the trailing slash from configured server URLs, such as the Claude desktop app, so requests to a server at ``/mcp/`` arrive at ``/mcp`` and fail.

Every server needs an ``auth`` argument saying how callers are authenticated.
The example uses the optional ``django_mcpz.bearer_tokens`` app, which authenticates clients with per-client bearer tokens.
See :ref:`server-authentication`.

.. _server-typed-schemas:

Typed schemas
-------------

django-mcpz turns typed Python into JSON Schemas, so schemas rarely need writing by hand.
It validates each call’s arguments against them, rejecting mistyped and unknown arguments with in-band errors that the calling model can correct from.

.. _server-signature-schemas:

Tool parameters are defined by a msgspec-supported type, typically a `msgspec Struct <https://msgspec.dev/structs>`__, read by default from the type annotation of the function’s params argument:

.. code-block:: python

    from typing import Annotated

    import msgspec


    class SearchParams(msgspec.Struct):
        query: Annotated[str, msgspec.Meta(description="Search terms.", min_length=1)]
        limit: Annotated[int, msgspec.Meta(ge=1, le=100)] = 10


    @server.tool(description="Search products.", read_only=True)
    def search(
        request, params: SearchParams
    ) -> SearchResult: ...  # params is a validated SearchParams instance

The return annotation, when it is a ``msgspec.Struct`` type like ``SearchResult`` here, doubles as the ``output_schema``.
Tools can also return images, audio, and resources, as content blocks such as :class:`Image`, covered under :meth:`MCPServer.tool`.
A tool with no parameters is a function taking only the request, declaring an empty schema:

.. code-block:: python

    @server.tool(description="Overall statistics.", read_only=True)
    def stats(request): ...

Annotations are resolved with ``inspect.get_annotations()``, which on Python 3.14+ uses |annotationlib|__, so ``from __future__ import annotations`` works, as long as the annotations reference module-level names.
In short, define your parameter types at module level, not inside functions.
An unannotated params argument, or extra parameters beyond the request and params argument, raise ``ImproperlyConfigured`` at registration time.
Pass the type as ``input_schema`` instead where the annotation cannot be resolved.

.. |annotationlib| replace:: ``annotationlib``
__ https://docs.python.org/3/library/annotationlib.html

The full rules of schema generation and validation are in :ref:`api-schemas`.

.. _server-icons:

Icons
-----

Servers and tools can carry icons for display in client user interfaces.
The specification directs clients to reject icons from origins other than the MCP server’s own, so icons have to be served by your site.

If your site serves its own static files, with a relative |STATIC_URL|__ such as ``"/static/"``, as with |WhiteNoise|__, pass :class:`Icon` instances with ``static`` set.
django-mcpz resolves them to absolute URLs on the current host, per request, in the server info and ``tools/list`` responses:

.. |STATIC_URL| replace:: ``STATIC_URL``
__ https://docs.djangoproject.com/en/stable/ref/settings/#static-url
.. |WhiteNoise| replace:: WhiteNoise
__ https://whitenoise.readthedocs.io/

.. code-block:: python

    import msgspec

    from django_mcpz.server import Icon, MCPServer

    server = MCPServer(
        name="shop",
        version="1.0.0",
        icons=[Icon(static="shop/icon.png", sizes=("48x48",))],
        auth=...,
    )


    class SearchParams(msgspec.Struct):
        query: str


    @server.tool(
        description="Search products.",
        read_only=True,
        icons=[
            Icon(static="shop/search.png", sizes=("48x48",)),
            Icon(static="shop/search.svg", sizes=("any",)),
        ],
    )
    def search(request, params: SearchParams): ...

A server’s icons also appear on the :doc:`OAuth <oauth>` consent page, beside its title.

If ``STATIC_URL`` points at a CDN on another origin, static files will not do, since clients would reject them.
Instead, serve the icon from a view on your site, such as one returning |FileResponse|__:

.. |FileResponse| replace:: ``FileResponse``
__ https://docs.djangoproject.com/en/stable/ref/request-response/#django.http.FileResponse

.. code-block:: python

    from django.conf import settings
    from django.http import FileResponse
    from django.urls import path

    from example.mcp import server


    def search_icon(request):
        return FileResponse(open(settings.BASE_DIR / "icons" / "search.png", "rb"))


    urlpatterns = [
        path("mcp", server),
        path("mcp/icons/search.png", search_icon),
    ]

…and point the icon at that URL path with ``path`` instead of ``static``:

.. code-block:: python

    Icon(path="/mcp/icons/search.png", sizes=("48x48",))

Plain ``dict``\s in the specification’s `icon format <https://modelcontextprotocol.io/specification/2026-07-28/basic/index#icons>`__ pass through unresolved, for ``data:`` URIs or externally hosted images, which clients may reject as cross-origin.

.. _server-authentication:

Authentication
--------------

MCP servers expose application internals to network callers, so every server must say how it authenticates them, through the required ``auth`` argument.
This is separate to Django’s own authentication, which is browser-oriented and session-based.

Two optional apps cover the common cases.
Developer tools such as Claude Code accept a bearer token in their configuration, which the :doc:`bearer tokens <bearer_tokens>` app provides.
Hosted assistants such as Claude.ai and ChatGPT connect through OAuth, which the :doc:`oauth <oauth>` app provides, implementing the `MCP authorization specification <https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization>`__.
Any other scheme plugs in as a callable.

.. _server-public:

Public servers
~~~~~~~~~~~~~~

Pass :func:`public`, which allows every request, if the server is protected some other way, such as an authenticating reverse proxy or other middleware, or is genuinely public:

.. code-block:: python

    from django_mcpz.server import MCPServer, public

    server = MCPServer(name="shop", version="1.0.0", auth=public)

Be very sure you want to do this!

Bearer tokens
~~~~~~~~~~~~~

For developer tools, which send a pasted credential, use bearer tokens from the optional ``django_mcpz.bearer_tokens`` app: one token per client, each acting as a user, revocable one at a time.
Pass its authentication callable to your server:

.. code-block:: python

    from django_mcpz.server import MCPServer
    from django_mcpz.bearer_tokens.auth import token_auth

    server = MCPServer(name="shop", version="1.0.0", auth=token_auth)

See :doc:`bearer_tokens` for setting up the app and creating bearer tokens.

OAuth
~~~~~

For hosted assistants, which take the user through a login and consent flow rather than accepting a pasted token, use the optional ``django_mcpz.oauth`` app’s callable:

.. code-block:: python

    from django_mcpz.oauth.auth import oauth_auth
    from django_mcpz.server import MCPServer

    server = MCPServer(name="shop", version="1.0.0", auth=oauth_auth)

See :doc:`oauth` for setting up the app.

Custom authentication
~~~~~~~~~~~~~~~~~~~~~

Any callable works as ``auth``, so other schemes plug in the same way: check the request and return an ``HttpResponse`` to reject it or ``None`` to accept it, attaching attributes to ``request``, like ``request.user``, as needed for downstream usage.
For example, trusting an authenticating reverse proxy that sets a header:

.. code-block:: python

    from http import HTTPStatus

    from django.contrib.auth import get_user_model
    from django.http import HttpResponse

    from django_mcpz.server import MCPServer


    def proxy_auth(request):
        username = request.headers.get("X-Authenticated-User")
        if not username:
            return HttpResponse(status=HTTPStatus.UNAUTHORIZED)
        request.user = get_user_model().objects.get(username=username)
        return None


    server = MCPServer(name="shop", version="1.0.0", auth=proxy_auth)

Tokens from an external OAuth authorization server plug in the same way: validate the access token, set ``request.user``, done.

.. _server-permissions:

Per-tool permissions
--------------------

Authentication establishes *who* is calling.
The ``permission`` parameter of :meth:`~MCPServer.tool` controls *which tools* they may use.
Pass a callable receiving the ``HttpRequest`` and returning a boolean, or a Django `permission codename <https://docs.djangoproject.com/en/stable/topics/auth/default/#permissions-and-authorization>`__ string, checked with |request.user.has_perm()|__:

.. |request.user.has_perm()| replace:: ``request.user.has_perm()``
__ https://docs.djangoproject.com/en/stable/ref/contrib/auth/#django.contrib.auth.models.User.has_perm

.. code-block:: python

    @server.tool(
        description="List refunds issued in a date range.",
        read_only=True,
        permission="shop.view_refund",
    )
    def list_refunds(request, params: ListRefundsParams) -> ListRefundsResult: ...


    @server.tool(
        description="Issue a refund for an order.",
        destructive=True,
        permission=lambda request: request.user.is_staff,
    )
    def issue_refund(request, params: IssueRefundParams) -> IssueRefundResult: ...

Callers that fail the check do not see the tool in ``tools/list``, so the calling model never tries it, and calling it anyway is rejected identically to calling a nonexistent tool.
Nothing about restricted tools is revealed to callers lacking permission.

Both forms need the ``auth`` callable, or middleware, to identify the caller first.
String permissions require |request.user|__ to be set, which :func:`~django_mcpz.bearer_tokens.auth.token_auth` does from the token’s user.
If nothing set it, the check raises ``ImproperlyConfigured`` rather than failing quietly.
Callable permissions that read ``request.user`` need the same, and should guard for its absence themselves.
Callable permissions can check whatever the ``auth`` callable attached, such as ``request.mcp_token`` from the bearer tokens app.

.. |request.user| replace:: ``request.user``
__ https://docs.djangoproject.com/en/stable/ref/request-response/#django.http.HttpRequest.user

.. _server-elicitation:

Asking the user a question
--------------------------

A tool can ask the user for something mid-call with :func:`elicit`, such as a confirmation before a consequential action, or a detail the model did not supply.
Describe what to collect as a ``msgspec.Struct``, and the answer comes back as an instance of it:

.. code-block:: python

    from typing import Annotated

    import msgspec

    from django_mcpz.server import ToolError, elicit


    class Confirmation(msgspec.Struct):
        confirmed: Annotated[bool, msgspec.Meta(description="Yes, issue it.")]


    @server.tool(description="Issue a refund for an order.", destructive=True)
    def issue_refund(request, params: IssueRefundParams) -> IssueRefundResult:
        order = Order.objects.get(id=params.order_id)
        confirmation = elicit(
            request, f"Refund £{order.total} to {order.email}?", Confirmation
        )
        if not confirmation.confirmed:
            raise ToolError("No refund issued.")
        ...

If the user declines, or the client cannot ask at all, :func:`elicit` raises :class:`ElicitationDeclinedError` or :class:`ElicitationUnavailableError`, which report the reason in-band unless the tool catches them.
Form mode collects a flat object of primitive fields, so that any client can render it, and the schema is restricted to match.
See :func:`elicit`.

.. _server-elicitation-repeats:

Repeated runs
~~~~~~~~~~~~~

MCP version 2026-07-28 gives the server no channel to send a question over.
Instead the server *returns* the question, and the client answers it by repeating the whole tool call.
A tool that asks therefore runs again from the top for each answer, so everything above an ``elicit()`` call runs more than once:

.. code-block:: python

    @server.tool(description="Refund an order.", destructive=True)
    def refund(request, params: RefundParams) -> str:
        order = Order.objects.get(id=params.order_id)  # runs twice
        confirmation = elicit(request, "Refund it?", Confirmation)  # asks, then answers
        order.refund()  # runs once
        return "Refunded."

Basic data usage guidance:

* Read freely before and during questions.
* Make writes only after all questions are answered, so that the tool’s writes happen at most once.

Under |ATOMIC_REQUESTS|__ a write made before a question is rolled back when the question is asked, at least.

.. |ATOMIC_REQUESTS| replace:: ``ATOMIC_REQUESTS``
__ https://docs.djangoproject.com/en/stable/ref/settings/#atomic-requests

The answers already given travel in an opaque ``requestState`` that the client echoes back unread, as covered in :ref:`server-protocol-support`.
It is bound to the caller it was issued to, which needs an ``auth`` callable that identifies one—see :ref:`server-security`.
Answers expire ten minutes after their question is asked, after which the tool asks again, as covered under :func:`elicit`.
Answers are not guaranteed to be used once, so a tool that must not repeat an action should check it has not already happened.

.. _server-logging:

Logging tool calls
------------------

Every ``tools/call`` request that reaches a tool is logged at ``INFO`` level to the ``django_mcpz.calls`` logger, with a message like ``Tool 'count_orders': ok in 3.2ms``.
Tools rejected as unknown, including those the caller lacks permission for, and protocol-level failures are not logged there.

To see the messages, route the logger in your |LOGGING|__ setting, for example:

.. |LOGGING| replace:: ``LOGGING``
__ https://docs.djangoproject.com/en/stable/ref/settings/#logging

.. code-block:: python

    LOGGING = {
        "version": 1,
        "disable_existing_loggers": False,
        "handlers": {
            "console": {"class": "logging.StreamHandler"},
        },
        "loggers": {
            "django_mcpz.calls": {"handlers": ["console"], "level": "INFO"},
        },
    }

For an audit trail or metrics, attach a custom handler that reads the record attributes:

.. code-block:: python

    import logging


    class ToolCallAuditHandler(logging.Handler):
        def emit(self, record):
            from example.models import ToolCall

            ToolCall.objects.create(
                token=getattr(record.request, "mcp_token", None),
                tool=record.tool,
                arguments=record.arguments,
                outcome=record.outcome,
                duration=record.duration,
            )

Reference the handler class in ``LOGGING`` under ``"handlers"``, with a ``"()"`` or ``"class"`` key, per Django’s `logging documentation <https://docs.djangoproject.com/en/stable/topics/logging/>`__.

Each record’s attributes are listed in :ref:`api-logging`.

.. _server-legacy:

Clients on earlier MCP versions
-------------------------------

Adoption of MCP version 2026-07-28 is uneven: some clients probe with ``server/discover`` and speak it, while others still open with the |initialize|__ handshake of the 2025 versions.
By default, servers serve both.

.. |initialize| replace:: ``initialize``
__ https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle#initialization

The 2025 versions allow a stateless server mode, with no session ID and no server-to-client stream, that maps onto the same synchronous view.
A request without an ``MCP-Protocol-Version`` header, or with one naming a 2025 version, is handled that way, as detailed in :ref:`protocol-legacy`.

Authentication and permissions apply identically.
Pass ``minimum_protocol_version="2026-07-28"`` to serve only that version, rejecting requests without its ``MCP-Protocol-Version`` header, if you want to be certain every caller is on the current version.
