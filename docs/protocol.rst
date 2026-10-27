.. _server-protocol-support:

Protocol support
================

django-mcpz implements the server side of MCP version 2026-07-28 over the `Streamable HTTP transport <https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http>`__, plus the stateless server mode of the 2025 versions (see :ref:`protocol-legacy`).
MCP version 2026-07-28 made the protocol stateless, with no initialization handshake, no sessions, and no server-sent event streams, which is what lets a plain synchronous view serve it in full, under WSGI, with no need for ASGI.
JSON serialization and deserialization use `msgspec <https://msgspec.dev/>`__, via `django-msgspec <https://django-msgspec.readthedocs.io/>`__, for speed.

Implemented:

* |server/discover|__, reporting supported versions, the ``tools`` capability, and any ``instructions``.

  .. |server/discover| replace:: ``server/discover``
  __ https://modelcontextprotocol.io/specification/draft/server/discover

* |tools/list|__, returning all registered tools in registration order (deterministic, per the specification’s caching recommendation).
  The required ``ttlMs`` and ``cacheScope`` fields, which tell clients how long they may cache the tool list and whether shared caches may hold it, are fixed at ``300_000`` (five minutes, suiting the mostly static tool lists typical of this package) and ``"private"`` (never shared between callers, as permission-filtered tool lists require).

  .. |tools/list| replace:: ``tools/list``
  __ https://modelcontextprotocol.io/specification/draft/server/tools#listing-tools

* |tools/call|__, dispatching to your tool functions.

  .. |tools/call| replace:: ``tools/call``
  __ https://modelcontextprotocol.io/specification/draft/server/tools#calling-tools

* Per-request protocol version negotiation: unsupported versions receive an ``UnsupportedProtocolVersionError`` (code ``-32022``) listing supported versions.
* Request metadata validation: the required ``_meta`` fields, and the ``MCP-Protocol-Version``, ``Mcp-Method``, and ``Mcp-Name`` headers are checked against the request body, with mismatches rejected as ``HeaderMismatch`` errors (code ``-32020``), including support for the Base64 sentinel value encoding.
* ``Origin`` header validation against |the ALLOWED_HOSTS setting|__, to prevent DNS rebinding attacks, rejecting invalid origins with HTTP 403.

  .. |the ALLOWED_HOSTS setting| replace:: the ``ALLOWED_HOSTS`` setting
  __ https://docs.djangoproject.com/en/stable/ref/settings/#allowed-hosts

* Notifications are acknowledged with HTTP 202, and non-POST requests rejected with HTTP 405, as the transport requires.
* |Form mode elicitation|__, over the |multi round-trip|__ flow: a tool needing input returns an ``input_required`` result carrying an ``elicitation/create`` request, and the client answers it by repeating the call.
  The answers so far travel in a signed ``requestState``, bound to the caller, the tool, and the call’s arguments, so no server-side session is needed and any instance can take the answer.
  See :ref:`server-elicitation`.

  .. |Form mode elicitation| replace:: Form mode elicitation
  __ https://modelcontextprotocol.io/specification/2026-07-28/client/elicitation
  .. |multi round-trip| replace:: multi round-trip
  __ https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/mrtr

Not implemented, by design or not yet.
These are features for interactive or long-running conversations, which most Django tools do not need:

* Per-request SSE streaming responses, which the specification makes optional.
  Responses are always single JSON objects, which suits quick, synchronous tools.
* |subscriptions/listen|__ long-lived notification streams, which need a streaming (and realistically ASGI) response.

  .. |subscriptions/listen| replace:: ``subscriptions/listen``
  __ https://modelcontextprotocol.io/specification/draft/basic/patterns/subscriptions

* Resources, prompts, and completions.
* Sampling and roots.
  MCP version 2026-07-28 embeds these in an ``input_required`` tool result, like elicitation above, so they fit a synchronous view and may come in a future version.
* URL mode elicitation, for interactions that must happen out of band, such as collecting a credential or taking a payment.
* The ``x-mcp-header`` schema extension, for routing by proxies on parameter values.
  Schemas using it are rejected at registration time.

.. _protocol-legacy:

Earlier MCP versions
--------------------

Servers with the default ``minimum_protocol_version`` also serve the 2025 versions, as covered in :ref:`server-legacy`.
A request without an ``MCP-Protocol-Version`` header, or with one naming a 2025 version, is handled in those versions’ stateless server mode:

* |initialize|__ is answered with the requested protocol version (2025-11-25, 2025-06-18, or 2025-03-26), or the latest of those if the request named another, and the same capabilities and ``instructions`` as ``server/discover``.
  No ``Mcp-Session-Id`` header is issued, so clients treat the server as stateless.

  .. |initialize| replace:: ``initialize``
  __ https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle#initialization

* |notifications/initialized|__ is acknowledged with HTTP 202, and |ping|__ answered with an empty result.

  .. |notifications/initialized| replace:: ``notifications/initialized``
  __ https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle#initialization

  .. |ping| replace:: ``ping``
  __ https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/ping

* ``tools/list`` and ``tools/call`` work as for MCP version 2026-07-28, without the ``_meta`` fields and ``Mcp-*`` header requirements that version added.
* Other methods receive a JSON-RPC method-not-found error in an HTTP 200 response, as those transports expect.
* ``GET`` requests receive HTTP 405, which tells such clients the server offers no notification stream.
