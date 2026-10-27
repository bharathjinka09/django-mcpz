API
===

Servers
-------

.. currentmodule:: django_mcpz.server

.. class:: MCPServer(*, name, version, title=None, description=None, website_url=None, instructions=None, icons=None, auth, minimum_protocol_version="2025-03-26")

    Represents one MCP server and its registry of tools.
    Like Django’s ``admin.site``, you create one, register things on it, and route it.

    :param name:
        The server name, reported to clients in the ``io.modelcontextprotocol/serverInfo`` metadata of every result, and in |initialize|__ responses to clients on the 2025 versions.

        .. |initialize| replace:: ``initialize``
        __ https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle#initialization

    :param version:
        The server version, reported alongside ``name``.

    :param title:
        Optional human-readable server name for display purposes.
        Also shown on the :doc:`OAuth <oauth>` consent page, and reported in the server’s protected resource metadata, as the name for clients to list the connection under.

    :param description:
        Optional description of the server, for display purposes, reported alongside ``name``.

    :param website_url:
        Optional URL of a website about the server, or its organization, reported alongside ``name`` as ``websiteUrl``.

    :param instructions:
        Optional natural-language guidance for LLMs on how to use this server effectively, returned from |server/discover|__.

        .. |server/discover| replace:: ``server/discover``
        __ https://modelcontextprotocol.io/specification/draft/server/discover

    :param icons:
        Optional list of server icons for display in user interfaces, in the same forms as the ``icons`` parameter of :meth:`tool`.
        Reported alongside ``name`` in the server info, and shown on the :doc:`OAuth <oauth>` consent page.
        See :ref:`server-icons`.

    :param auth:
        Required.
        A callable implementing authentication, run on each request before the request body is touched, such as :func:`public` or :func:`django_mcpz.bearer_tokens.auth.token_auth`.
        It receives the |HttpRequest|__ and should return ``None`` to allow the request, or an |HttpResponse|__ (such as ``HttpResponse(status=HTTPStatus.UNAUTHORIZED)``) to reject it.

        .. |HttpRequest| replace:: ``HttpRequest``
        __ https://docs.djangoproject.com/en/stable/ref/request-response/#django.http.HttpRequest
        .. |HttpResponse| replace:: ``HttpResponse``
        __ https://docs.djangoproject.com/en/stable/ref/request-response/#django.http.HttpResponse

        A non-callable raises ``ImproperlyConfigured``.
        See :ref:`server-authentication`.

    :param minimum_protocol_version:
        The oldest MCP version to serve, as one of two strings.
        ``"2025-03-26"``, the default, also serves clients on the 2025 versions, which open with an ``initialize`` handshake, since many clients have yet to adopt MCP version 2026-07-28.
        ``"2026-07-28"`` serves only that version.
        Any other value raises |ImproperlyConfigured|__.
        See :ref:`server-legacy`.

        .. |ImproperlyConfigured| replace:: ``ImproperlyConfigured``
        __ https://docs.djangoproject.com/en/stable/ref/exceptions/#django.core.exceptions.ImproperlyConfigured

    .. method:: tool(*, description, input_schema=None, name=None, title=None, output_schema=None, read_only=None, destructive=None, idempotent=None, open_world=None, icons=None, permission=None)

        Decorator that registers the decorated function as an MCP tool on this server.

        :param description:
            Human-readable description of the tool’s functionality, for the calling model.

        :param input_schema:
            The tool’s parameters, in one of three forms:

            * ``None``, the default: the input type is read from the type annotation of the function’s params argument, resolved with |get_annotations|__.
              A function taking only the request declares no parameters.
              See :ref:`typed schemas from the params annotation <server-signature-schemas>`.

            * A msgspec-supported type, typically a |msgspec.Struct subclass|__.
              This is the same as annotating the params argument, for when the annotation cannot be resolved at registration time.

              .. |msgspec.Struct subclass| replace:: ``msgspec.Struct`` subclass
              __ https://msgspec.dev/structs

            * A JSON Schema (2020-12) as a plain ``dict``, served as-is, with arguments not validated.
              For a tool with no parameters, use ``{"type": "object", "additionalProperties": False}``.

            In the first two forms, django-mcpz generates the JSON Schema, and validates and converts arguments before each call, rejecting unknown arguments by default.
            See :ref:`server-typed-schemas`.

            .. warning::

                With a ``dict`` ``input_schema``, django-mcpz does not validate ``arguments``.
                Clients are expected to conform, but a hostile client can send anything JSON-decodable.
                Treat ``arguments`` values like any other user input, or use a typed ``input_schema``, which does validate.

            The |x-mcp-header|__ extension, which asks clients to mirror parameter values into HTTP headers for routing by proxies, is not supported.
            Schemas containing it are rejected at registration time with ``ImproperlyConfigured``, since declaring it would oblige the server to validate those headers on every call.

            .. |x-mcp-header| replace:: ``x-mcp-header``
            __ https://modelcontextprotocol.io/specification/2026-07-28/server/tools#x-mcp-header

            .. |get_annotations| replace:: ``inspect.get_annotations()``
            __ https://docs.python.org/3/library/inspect.html#inspect.get_annotations

        :param name:
            The tool name.
            Defaults to the decorated function’s name.
            Per the specification, names are 1 to 128 characters of letters, digits, underscores, hyphens, and dots; anything else raises |ImproperlyConfigured|__.

            __ https://docs.djangoproject.com/en/stable/ref/exceptions/#django.core.exceptions.ImproperlyConfigured

        :param title:
            Optional human-readable tool name for display purposes.

        :param output_schema:
            Optional description of the tool’s structured output: a msgspec-supported type, from which the JSON Schema is generated, or a JSON Schema ``dict``.
            When ``input_schema`` is not given, defaults to the function’s return annotation when that is a ``msgspec.Struct`` subclass.
            Return values are not validated against it.
            Return a matching value, typically an instance of the given type.

        :param read_only:
            Set the ``readOnlyHint`` `tool annotation <https://modelcontextprotocol.io/specification/2026-07-28/schema#toolannotations>`__.
            Reports to clients that the tool does not modify its environment.

        :param destructive:
            Set the ``destructiveHint`` tool annotation.
            Reports to clients that the tool may perform destructive updates to its environment.

        :param idempotent:
            Set the ``idempotentHint`` tool annotation.
            Reports to clients that calling the tool repeatedly with the same arguments will have no additional effect on its environment.

        :param open_world:
            Set the ``openWorldHint`` tool annotation.
            Reports to clients that this tool may interact with an “open world” of external entities.
            If false, the tool’s domain of interaction is closed.
            For example, the world of a web search tool is open, whereas that of a memory tool is not.

        :param icons:
            Optional list of tool icons for display in user interfaces: :class:`Icon` instances, resolved through Django’s static files, and/or plain ``dict``\s in the specification’s `icon format <https://modelcontextprotocol.io/specification/2026-07-28/basic/index#icons>`__, served as-is.
            See :ref:`server-icons`.

        :param permission:
            Optional authorization check restricting the tool to some callers: a callable receiving the ``HttpRequest`` and returning a boolean, or a Django `permission <https://docs.djangoproject.com/en/stable/topics/auth/default/#permissions-and-authorization>`__ codename string like ``"shop.view_order"``, checked with |request.user.has_perm()|__.
            Callers that fail the check do not see the tool in |tools/list|__ and cannot call it.

            .. |tools/list| replace:: ``tools/list``
            __ https://modelcontextprotocol.io/specification/draft/server/tools#listing-tools

            See :ref:`server-permissions`.

            __ https://docs.djangoproject.com/en/stable/ref/contrib/auth/#django.contrib.auth.models.User.has_perm

        Tool functions receive the ``HttpRequest`` first, then their params argument, unless they take only the request.
        With a typed ``input_schema``, that is an instance of your type, already validated.
        With a ``dict`` ``input_schema``, it is the raw ``arguments`` ``dict`` the client sent.
        Django’s request/response cycle applies as usual, so tools can use `the ORM <https://docs.djangoproject.com/en/stable/topics/db/queries/>`__, |request.user|__ (if you use `session or other middleware-based authentication <https://docs.djangoproject.com/en/stable/topics/auth/default/#authentication-in-web-requests>`__), and anything else a view can.

        __ https://docs.djangoproject.com/en/stable/ref/request-response/#django.http.HttpRequest.user

        The return value determines the |tools/call|__ result:

        .. |tools/call| replace:: ``tools/call``
        __ https://modelcontextprotocol.io/specification/draft/server/tools#calling-tools

        * a ``str`` becomes a single text content block.
        * ``None`` becomes an empty content list.
        * A content block, one of :class:`Text`, :class:`Image`, :class:`Audio`, :class:`ResourceLink`, or :class:`EmbeddedResource`, or a list of them, becomes the content list as given.
        * Any other value is serialized to JSON with msgspec, so tools can return anything it supports, including ``dataclasses`` and msgspec ``Struct`` types.
          A value that serializes to a JSON object, such as a ``dict`` or a ``Struct`` instance, becomes the result’s ``structuredContent``, plus its JSON serialization as a text content block for backwards compatibility, per the specification.
          Any other JSON value, such as a list or a number, becomes a text content block only, since the specification requires ``structuredContent`` to be an object and clients reject results where it is not.

        If your tool declares an ``output_schema``, its return value must conform.

    .. method:: __call__(request)

        Servers are their own view functions.
        Route the instance directly, as in :doc:`servers`.
        The view is `CSRF-exempt <https://docs.djangoproject.com/en/stable/ref/csrf/#django.views.decorators.csrf.csrf_exempt>`__, since MCP clients are not browsers and authenticate per-request.

.. exception:: ToolError

    Raise in a tool function to report a *tool execution error*: the message is returned in-band, in a result with ``isError: true``, so the calling model can see it and self-correct.
    In-band means the error travels inside a normal result, where the model reads it, rather than as an HTTP error that the client would surface to the user.

    .. code-block:: python

        import msgspec

        from django_mcpz.server import ToolError


        class GetOrderParams(msgspec.Struct):
            order_id: int


        @server.tool(description="Fetch an order by ID.", read_only=True)
        def get_order(request, params: GetOrderParams):
            try:
                order = Order.objects.get(id=params.order_id)
            except Order.DoesNotExist:
                raise ToolError(f"No order with id {params.order_id}.")
            ...

    Any other exception raised by a tool is logged to the ``django_mcpz`` logger and reported in-band with a generic message, so internal details do not leak to clients.

.. function:: elicit(request, message, schema, *, key=None, expired_message='Your earlier answer expired, so this is asked again.')

    Ask the user a question from inside a tool function, returning their answer.
    See :ref:`server-elicitation`.

    :param message:
        The question, for the client to show the user.
        The specification forbids asking for secrets, such as passwords, API keys, or payment credentials.

    :param schema:
        What to collect, in one of two forms:

        * A msgspec-supported type, typically a `msgspec Struct <https://msgspec.dev/structs>`__, from which the schema is generated and which the answer is converted to and returned as.

        * A JSON Schema as a plain ``dict``, sent as-is, whose answer is returned as a ``dict``, without validation, as for a ``dict`` ``input_schema``.

        Form mode allows a flat object of primitive properties only, so that any client can render it: strings, numbers, integers, booleans, a ``Literal`` or ``Enum`` of strings, and a list of a ``Literal`` or ``Enum`` of strings as a multiple choice.
        A field without a default is required, as for a tool's parameters.
        A type with a nested object, a plain ``list[str]``, or an optional field typed ``| None`` rather than given a default, raises |ImproperlyConfigured|__.

        __ https://docs.djangoproject.com/en/stable/ref/exceptions/#django.core.exceptions.ImproperlyConfigured

    :param key:
        The identifier this question is asked and answered under.
        Defaults to the position of the call, as ``"elicitation-0"``, ``"elicitation-1"``, and so on.
        Pass one where the questions a tool asks vary from run to run, since a position then names a different question each time.
        Asking twice under one key, including a chosen key that matches another call’s position, raises |ImproperlyConfigured|__, since an answer would be given to both.

        __ https://docs.djangoproject.com/en/stable/ref/exceptions/#django.core.exceptions.ImproperlyConfigured

    :param expired_message:
        A note shown above the question when it is asked again because the user took too long to answer.
        Answers expire ten minutes after their question is asked, and then the tool runs again from the top with no answers.
        Pass ``None`` to ask again without a note.

    .. warning::

        A tool that reaches a call to ``elicit()`` runs again from the top for each answer, as covered in :ref:`server-elicitation-repeats`, so do the work that must happen once after the last question.
        To ask the user, ``elicit()`` raises an internal control-flow exception that subclasses ``BaseException``, like ``KeyboardInterrupt``, so ``except Exception`` does not catch it, but a bare ``except:`` or ``except BaseException`` swallows the question.

.. exception:: ElicitationDeclinedError

    Raised by :func:`elicit` when the user declined or dismissed the question.

.. exception:: ElicitationUnavailableError

    Raised by :func:`elicit` when the client cannot answer questions.
    Catch it to fall back to a non-interactive path, such as refusing an action that needed confirmation.

.. class:: Icon(static=None, path=None, mime_type=None, sizes=None, theme=None)

    An icon for a server or a tool, served by this site from Django’s `static files <https://docs.djangoproject.com/en/stable/howto/static-files/>`__ or a URL path, for the ``icons`` parameters of :class:`MCPServer` and :meth:`~MCPServer.tool`.
    See :ref:`server-icons`.

    :param static:
        The static file path, e.g. ``"example/tool-icon.png"``, resolved like the ``{% static %}`` template tag and made absolute against each request.

    :param path:
        Alternatively, a URL path on this site, e.g. ``"/mcp/icon.png"``, made absolute against each request.
        Exactly one of ``static`` and ``path`` is required.

    :param mime_type:
        The image MIME type.
        Guessed from the file extension if not given.

    :param sizes:
        Optional tuple of size strings, e.g. ``("48x48",)``, or ``("any",)`` for scalable formats like SVG.

    :param theme:
        Optionally ``"light"`` or ``"dark"``, if the icon is designed for one background.

.. class:: Text(text)

    A text content block, for returning alongside other blocks.
    On its own, returning a ``str`` does the same.

.. class:: Image(data, mime_type)

    An image content block, from the image’s ``bytes`` and MIME type, such as ``"image/png"``.

.. class:: Audio(data, mime_type)

    An audio content block, from the clip’s ``bytes`` and MIME type, such as ``"audio/wav"``.

.. class:: ResourceLink(uri, name, title=None, description=None, mime_type=None, size=None)

    A link to a resource the client may fetch, such as a file served by your site.
    ``uri`` and ``name`` are required; ``size`` is in bytes.

.. class:: EmbeddedResource(uri, text=None, blob=None, mime_type=None)

    The contents of a resource, included in the result: its ``text``, or its ``bytes`` as ``blob``, exactly one of the two, identified by ``uri``.

.. function:: public(request)

    An ``auth`` callable that allows every request, for a server without authentication.
    See :ref:`server-public`.

.. _api-schemas:

Schema generation
-----------------

For typed input schemas, whether annotated or passed explicitly as ``input_schema``:

* The JSON Schema comes from |msgspec.json.schema()|__.
  Field descriptions and constraints are declared with ``Annotated`` and |msgspec.Meta|__, and defaults appear in the schema.
  django-mcpz inlines the schema’s top-level ``$ref``, so root properties stay statically reachable.
  Nested Structs remain as ``$defs`` references, and a recursive type keeps its top-level ``$ref``.
* Arguments are validated with ``msgspec.convert()`` before the tool runs.
  Validation failures are reported as in-band tool execution errors (``isError: true``), with msgspec’s message, e.g. ``Invalid arguments: Expected `int`, got `str` - at `$.limit```, so the calling model can self-correct, as the specification recommends for input validation errors.
* Unknown arguments are rejected by default: the generated schema gains ``additionalProperties: false``, which the MCP specification recommends, and calls sending undeclared top-level arguments receive an in-band error.
  Tools are called by language models, for which a silently ignored misspelt argument would be an invisible bug.
  This applies to the top level only.
  Extras inside a nested object follow the nested Struct’s own settings, so declare ``forbid_unknown_fields=True`` on nested Structs for depth.
  For a tool that deliberately accepts open-ended arguments, use a ``dict`` schema.
* Any type msgspec can generate a schema for works as ``input_schema``, not just Structs.
  A ``TypedDict``, for example, if you would rather the tool keep receiving a plain (but now validated) ``dict``.
* A matching ``output_schema`` type pairs well: return an instance and msgspec serializes it directly into ``structuredContent``.

  .. |msgspec.json.schema()| replace:: ``msgspec.json.schema()``
  __ https://msgspec.dev/jsonschema
  .. |msgspec.Meta| replace:: ``msgspec.Meta``
  __ https://msgspec.dev/constraints

.. _api-logging:

Log records
-----------

Records logged to the ``django_mcpz.calls`` logger, as covered in :ref:`server-logging`, carry these attributes, for handlers and formatters:

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Attribute
     - Value
   * - ``server``
     - The :class:`MCPServer`.
   * - ``request``
     - The ``HttpRequest``, including whatever the ``auth`` callable attached to it.
   * - ``tool``
     - The tool name.
   * - ``arguments``
     - The raw ``arguments`` ``dict`` from the request, before validation.
       These are not in the message text, since they may contain sensitive data.
       Include them only in handlers that store data appropriately.
   * - ``outcome``
     - One of ``"ok"``, ``"invalid_arguments"`` (rejected before the tool ran), ``"input_required"`` (the tool asked the user a question, as in :ref:`server-elicitation`), ``"tool_error"`` (the tool raised a :class:`BaseToolError`, such as :class:`ToolError`), or ``"exception"`` (the tool raised anything else, also logged with its traceback at ``ERROR`` level to the ``django_mcpz`` logger).
   * - ``duration``
     - Seconds spent validating arguments and running the tool, as a float.

.. _api-bearer-tokens:

Bearer tokens
-------------

.. currentmodule:: django_mcpz.bearer_tokens

The ``django_mcpz.bearer_tokens`` app, as covered in :doc:`bearer_tokens`.

.. function:: auth.token_auth(request)

    Reject requests, with a 401 response, unless their ``Authorization`` header carries an unrevoked, unexpired bearer token, like ``Authorization: Bearer mcp_...``.
    Bearer tokens of inactive users, per |is_active|__, are rejected too, so deactivating a user cuts off their clients.
    The 401 response’s ``WWW-Authenticate`` header names the error, as ``Bearer error="invalid_token"``, when a credential was sent but rejected, per RFC 6750.

    __ https://docs.djangoproject.com/en/stable/ref/contrib/auth/#django.contrib.auth.models.User.is_active

    On success, set |request.user|__ to the token’s user, so :ref:`permissions <server-permissions>` and tool functions can rely on it.
    Also attach the :class:`~models.Token` as ``request.mcp_token``, for tools that want to know which client is calling, and record the use in :attr:`~models.Token.last_used_at`.

    __ https://docs.djangoproject.com/en/stable/ref/request-response/#django.http.HttpRequest.user

.. class:: models.Token

    The model, with these fields and methods.

    .. attribute:: name

        A label for the token, such as the client using it.

    .. attribute:: user

        The user the token acts as.
        Deleting the user deletes their tokens.

    .. attribute:: created_at
    .. attribute:: last_used_at
    .. attribute:: expires_at
    .. attribute:: revoked_at

        Timestamps.
        ``last_used_at`` is ``None`` until the token’s first use, and updated at most once a minute after that, so a busy client does not cost a database write per request.
        It shows in the admin, to help spot tokens that are no longer in use.
        ``expires_at`` is ``None`` for tokens that last until revoked.
        ``revoked_at`` is ``None`` while the token is valid.

    .. classmethod:: create(*, name, user, expires_at=None)

        Create a token, returning the instance and the token value as a tuple.
        The value cannot be recovered later, so pass it on straight away.

    .. property:: is_expired

        Whether ``expires_at`` is set and has passed.

    .. property:: is_revoked

        Whether ``revoked_at`` is set.

    .. method:: revoke()

        Mark the token revoked.

.. describe:: python manage.py mcpz bearer-tokens create NAME --user USERNAME [--expires-in-days DAYS]

    Management command that creates a bearer token and prints its value once.
    The ``bearer-tokens`` sub-command is only available when ``django_mcpz.bearer_tokens`` is in ``INSTALLED_APPS``.

    ``NAME``
        A name for the token, such as the client using it.

    ``--user USERNAME``
        Required.
        The username of the user the token acts as, looked up with the user model’s |get_by_natural_key()|__.

    ``--expires-in-days DAYS``
        Optionally make the token expire that many whole days from now.

    .. |get_by_natural_key()| replace:: ``get_by_natural_key()``
    __ https://docs.djangoproject.com/en/stable/topics/auth/customizing/#django.contrib.auth.models.BaseUserManager.get_by_natural_key

.. describe:: python manage.py mcpz bearer-tokens clear

    Management command that deletes expired and revoked bearer tokens, printing how many.

.. function:: tasks.clear_expired()

    A task for Django’s tasks framework, on Django 6.0 and later, that deletes expired and revoked bearer tokens and returns how many.
    See :ref:`cleanup`.

.. _api-oauth:

OAuth
-----

.. currentmodule:: django_mcpz.oauth

The ``django_mcpz.oauth`` app, as covered in :doc:`oauth`.

.. function:: auth.oauth_auth(request)

    Reject requests, with a 401 response, unless their ``Authorization`` header carries a valid access token as a bearer credential.
    Valid means unexpired, unrevoked, issued for this MCP server’s URL, and belonging to an active user.
    The 401 response’s ``WWW-Authenticate`` header names the server’s metadata document, which is how clients find the authorization server.

    On success, set |request.user|__ to the token’s user, and attach the :class:`~models.AccessToken` as ``request.mcp_token``.

    __ https://docs.djangoproject.com/en/stable/ref/request-response/#django.http.HttpRequest.user

    Tokens are bound to the MCP server’s URL, scheme and host included, so a token issued while the site was reached on one host is rejected on another.

.. class:: models.Client

    An OAuth client: an application that may request tokens.

    .. attribute:: client_id

        The identifier clients present.
        A random string for dynamically registered clients, or the document URL for client ID metadata document clients.

    .. attribute:: kind

        ``"registered"`` or ``"metadata"``, for the two ways a client can arrive.

    .. attribute:: name
    .. attribute:: redirect_uris

        The client’s name, shown on the consent page, and the list of URLs it may be redirected to, compared exactly.

    .. attribute:: created_at
    .. attribute:: fetched_at
    .. attribute:: last_used_at

        Timestamps.
        ``fetched_at`` is when a metadata document was last fetched.
        ``last_used_at`` is when a token was last issued to the client.

.. class:: models.AuthorizationCode

    A single-use code issued by the authorize page, exchanged for tokens at the token endpoint.
    Every token issued from it, including through refreshes, links back to it, so that reuse of the code or of a refresh token revokes the whole family.

.. class:: models.AccessToken

    A bearer token for an MCP server.

    .. attribute:: client
    .. attribute:: user
    .. attribute:: resource
    .. attribute:: scope

        Who the token was issued to, who it acts as, the MCP server URL it is valid for, and the requested scope, if any.

    .. attribute:: created_at
    .. attribute:: expires_at
    .. attribute:: revoked_at

        Timestamps.
        ``revoked_at`` is ``None`` while the token has not been revoked.

    .. property:: is_valid

        Whether the token is unexpired and unrevoked.

    .. method:: revoke()

        Mark the token revoked.

.. class:: models.RefreshToken

    A token for obtaining a fresh access token without the user.
    Has the same fields and methods as :class:`~models.AccessToken`, plus:

    .. attribute:: access_token

        The access token issued alongside it.

    .. attribute:: used_at

        When it was exchanged, since each refresh token may be used once.

.. describe:: python manage.py mcpz oauth clear

    Management command that deletes expired authorization codes, expired and revoked tokens, and dynamically registered clients that never obtained a token within a day of registering.
    The ``oauth`` sub-command is only available when ``django_mcpz.oauth`` is in ``INSTALLED_APPS``.
    An expired access token stays while its refresh token is live, and a code stays while any token issued from it exists, so that reuse of a refresh token can still revoke the family.

.. function:: tasks.clear_expired()

    A task for Django’s tasks framework, on Django 6.0 and later, that does the same as the command and returns the number of rows deleted by kind: ``refresh_tokens``, ``access_tokens``, ``codes``, and ``clients``.
    See :ref:`cleanup`.

.. |request.user.has_perm()| replace:: ``request.user.has_perm()``
.. |request.user| replace:: ``request.user``
.. |is_active| replace:: ``is_active``
