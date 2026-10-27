Deployment notes
================

.. _server-security:

Security
--------

An MCP server lets a language model run code in your project on behalf of whoever holds the credentials.
Some things to keep in mind:

* Serve over HTTPS only.
  Bearer tokens travel in a header on every request, so a plain HTTP server exposes them to anyone on the network path.
  Django’s |SECURE_SSL_REDIRECT|__ setting covers the basics.

* A token is a password for its user.
  Anyone holding it can call every tool that user can, from any machine, until it is revoked.
  Treat token values like passwords when creating and sharing them, and revoke tokens for lost devices and departed users, or deactivate the user.

* Tool functions run with the caller’s ``request.user`` and no further checks.
  Anything a tool reads or changes is available to every authenticated client, unless the tool checks permissions itself or is restricted with ``permission``, see :ref:`server-permissions`.
  Model tools on what the user should be able to do, not on what the model asks for.

* Tool annotations such as ``read_only`` and ``destructive`` are hints for the client’s user interface, not enforcement.
  A tool marked read-only is only read-only if its code is.

* Whatever a tool returns goes into the model’s context, where it competes with the user’s instructions.
  Data your application stores, such as free text from other users, can contain instructions that try to steer the model.
  Keep tools narrow, return only the fields the model needs, and prefer structured output over pasting whole documents.

* Each |tools/call|__ runs in the request like a normal view, so |ATOMIC_REQUESTS|__ applies to it: with that setting on, a tool that raises has its writes rolled back, and the error result is still returned.
  Without it, a tool that makes several writes should wrap them in |transaction.atomic()|__ itself.

* The state an :func:`~django_mcpz.server.elicit` question travels with is bound to ``request.user``, so set it in the ``auth`` callable wherever a tool asks questions, as covered in :ref:`server-elicitation`.

* Requests carrying an ``Origin`` header from a host outside |ALLOWED_HOSTS|__ are rejected, so a web page cannot make a browser call your server with its cookies, as covered in :ref:`server-protocol-support`.

* OAuth clients identify themselves, through registration or a metadata document, so any application can present itself under any name.
  The consent page is where the user checks that the name and the redirect destination match the application they are connecting, which is why the :doc:`oauth <oauth>` app shows it on every authorization.

.. |SECURE_SSL_REDIRECT| replace:: ``SECURE_SSL_REDIRECT``
__ https://docs.djangoproject.com/en/stable/ref/settings/#secure-ssl-redirect

.. |tools/call| replace:: ``tools/call``
__ https://modelcontextprotocol.io/specification/draft/server/tools#calling-tools

.. |ATOMIC_REQUESTS| replace:: ``ATOMIC_REQUESTS``
__ https://docs.djangoproject.com/en/stable/ref/settings/#atomic-requests

.. |transaction.atomic()| replace:: ``transaction.atomic()``
__ https://docs.djangoproject.com/en/stable/topics/db/transactions/#django.db.transaction.atomic

.. |ALLOWED_HOSTS| replace:: ``ALLOWED_HOSTS``
__ https://docs.djangoproject.com/en/stable/ref/settings/#allowed-hosts

.. _cleanup:

Cleanup
-------

Both the bearer tokens app and the OAuth app provide ways to clean up expired and revoked tokens from your database, similar to Django’s |clearsessions|__ management command for its session model.
Pick your flavour: either as a task for Django’s |tasks framework|__, or as a management command for running from cron (or whatever).

.. |clearsessions| replace:: ``clearsessions``
__ https://docs.djangoproject.com/en/stable/ref/django-admin/#clearsessions
.. |tasks framework| replace:: tasks framework
__ https://docs.djangoproject.com/en/stable/topics/tasks/

Tasks
^^^^^

You can import and run the tasks like so:

.. code-block:: python

    from django_mcpz.oauth.tasks import clear_expired as clear_expired_oauth
    from django_mcpz.bearer_tokens.tasks import clear_expired as clear_expired_bearer_tokens

    clear_expired_bearer_tokens.enqueue()
    clear_expired_oauth.enqueue()

Only import and run (or schedule) the tasks for the auth apps you’re using!

For regular cleanup, schedule the tasks to run periodically.
Django’s tasks framework does not currently provide a way to schedule tasks, so you’ll need a scheduler, such as |django-scheduled-tasks|__, which you can use like so:

.. |django-scheduled-tasks| replace:: ``django-scheduled-tasks``
__ https://github.com/lode-braced/django-scheduled-tasks

.. code-block:: python

    from django_scheduled_tasks import cron_task

    from django_mcpz.oauth.tasks import clear_expired as clear_expired_oauth
    from django_mcpz.bearer_tokens.tasks import clear_expired as clear_expired_bearer_tokens

    cron_task(cron_schedule="0 4 * * *")(clear_expired_bearer_tokens)
    cron_task(cron_schedule="0 4 * * *")(clear_expired_oauth)

Put that in a module your project imports at startup, such as an app’s ``tasks.py``, so the schedules are registered.
Daily execution as above should be fine, unless you’re at a hugemongous scale.

Management commands
^^^^^^^^^^^^^^^^^^^

Use these management commands to clear expired and revoked tokens from the database:

.. code-block:: sh

    python manage.py mcpz bearer-tokens clear
    python manage.py mcpz oauth clear

Again, it’s best to schedule the relevant command(s) to run daily, such as with cron, Systemd timers, or whatever your deployment uses for scheduled tasks.
