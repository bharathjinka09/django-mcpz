=========
Changelog
=========

Unreleased
----------

* Document the convention of defining MCP servers in an ``mcp`` module of an app, an ``mcp.py`` file or ``mcp`` package, alongside its ``models.py`` and ``admin.py``.
  See :doc:`servers`.

  `PR #38 <https://github.com/adamchainz/django-mcpz/pull/38>`__.

1.2.0 (2026-10-26)
------------------

* Drop Python 3.10 support.

* Make the ``tasks`` modules importable on Django 5.2, where they now define nothing.
  Previously, tools that import every app’s ``tasks`` module, such as Celery’s autodiscovery, crashed because ``django.tasks`` does not exist before Django 6.0.

  `PR #37 <https://github.com/adamchainz/django-mcpz/pull/37>`__.
  Thanks to Andrew Miller for the report in `Issue #36 <https://github.com/adamchainz/django-mcpz/issues/36>`__.

1.1.0 (2026-10-02)
------------------

* Add form mode elicitation, so a tool can ask the user a question mid-call with the new ``elicit()`` function, such as a confirmation before a consequential action.
  The new ``ElicitationDeclinedError`` and ``ElicitationUnavailableError`` exceptions report a refusal, and the call log gains an ``"input_required"`` outcome.
  Clients on the 2025 versions cannot answer questions, since those versions have no way to carry one.

  Thanks to  Ben Atkinson in `PR #24 <https://github.com/adamchainz/django-mcpz/pull/24>`__.

1.0.5 (2026-10-01)
------------------

* Treat ``localhost`` as a loopback host for OAuth redirect URIs, like ``127.0.0.1`` and ``::1`` already were.
  Previously, clients that register a ``localhost`` redirect URI, such as Claude Code with ``http://localhost/callback``, failed with “Unregistered redirect_uri.” and could not sign in.

  `PR #31 <https://github.com/adamchainz/django-mcpz/pull/31>`__.

1.0.4 (2026-09-18)
------------------

* Compare the OAuth token endpoint’s ``resource`` parameter with the scheme and host lowercased, as the authorize endpoint does.
  Previously, a client sending a resource URL with an uppercase scheme or host, such as ``https://Example.com/mcp``, could obtain an authorization code but not exchange or refresh it, since the token endpoint compared the value as given against the stored lowercase form.

  `PR #25 <https://github.com/adamchainz/django-mcpz/pull/25>`__.

* Point the OAuth challenge for an MCP server routed at the site root, with ``path("", server)``, at ``/.well-known/oauth-protected-resource`` rather than the same URL with a trailing slash, which was not served.
  Clients following the challenge received a 404 response and could not discover the authorization server.

  `PR #26 <https://github.com/adamchainz/django-mcpz/pull/26>`__.

* Make ``mcpz bearer-tokens create`` reject a too-long name with a clear error, rather than crashing with a database error.

  `PR #28 <https://github.com/adamchainz/django-mcpz/pull/28>`__.

* Name the error in the bearer tokens app’s 401 challenge when a credential was sent but rejected, as ``WWW-Authenticate: Bearer error="invalid_token"``, per RFC 6750, matching the OAuth app.
  Requests carrying no bearer credential still receive the bare ``Bearer`` challenge.

  `PR #29 <https://github.com/adamchainz/django-mcpz/pull/29>`__.

1.0.3 (2026-09-16)
------------------

* Fix OAuth compatibility with ChatGPT.
  Correctly parse the ``token_endpoint_auth_methods_supported`` field from client ID metadata documents, allowing its discovery document to be used for OAuth client registration.

  Thanks to Jamie Matthews in `PR #22 <https://github.com/adamchainz/django-mcpz/pull/22>`__.

1.0.2 (2026-09-16)
------------------

* Exempt the MCP server view and the OAuth views from Django’s |LoginRequiredMiddleware|__.
  Previously, projects using that middleware had it redirect unauthenticated requests to the login page, before the server or its OAuth views could apply their own authentication.

  .. |LoginRequiredMiddleware| replace:: ``LoginRequiredMiddleware``
  __ https://docs.djangoproject.com/en/stable/ref/middleware/#django.contrib.auth.middleware.LoginRequiredMiddleware

  `PR #20 <https://github.com/adamchainz/django-mcpz/pull/20>`__.

1.0.1 (2026-09-15)
------------------

* Allow ``Origin`` headers and OAuth resource URLs with IPv6 hosts, such as ``http://[::1]:8000``, to match their ``ALLOWED_HOSTS`` entries.
  Previously they were always rejected, since ``urlsplit()`` strips the brackets that ``ALLOWED_HOSTS`` entries keep.

  `PR #13 <https://github.com/adamchainz/django-mcpz/pull/13>`__.

* Send tool return values that serialize to JSON values other than objects, such as lists and numbers, as text content only, without ``structuredContent``.
  The specification requires ``structuredContent`` to be an object, and clients reject results where it is not.

  `PR #14 <https://github.com/adamchainz/django-mcpz/pull/14>`__.

* Reject malformed URLs with unbalanced brackets, such as ``http://[::1``, in OAuth redirect URIs, resource URLs, and client ID metadata document URLs, rather than failing with a 500 response.

  `PR #15 <https://github.com/adamchainz/django-mcpz/pull/15>`__.

* Reject OAuth ``resource`` and ``redirect_uri`` parameters longer than the fields they are stored in, rather than failing with a database error on databases that enforce field sizes.

  `PR #16 <https://github.com/adamchainz/django-mcpz/pull/16>`__.

* Support OAuth on sites deployed under a path prefix, with ``SCRIPT_NAME`` or ``FORCE_SCRIPT_NAME``.
  Resource URLs and the discovery document paths include the prefix, which is now stripped before matching them against the URLconf.

  `PR #17 <https://github.com/adamchainz/django-mcpz/pull/17>`__.

* Guess an ``Icon``’s MIME type from the path of its URL alone, ignoring any query string a static files storage appends.

  `PR #18 <https://github.com/adamchainz/django-mcpz/pull/18>`__.

1.0.0 (2026-09-15)
------------------

* Initial release.
