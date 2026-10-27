Installation
============

Requirements
------------

Python 3.11 to 3.15 supported.

Django 5.2 to 6.1 supported.

Installation
------------

1. Install with **pip**:

   .. code-block:: sh

       python -m pip install django-mcpz

2. Add django-mcpz to your |INSTALLED_APPS|__:

   .. |INSTALLED_APPS| replace:: ``INSTALLED_APPS``
   __ https://docs.djangoproject.com/en/stable/ref/settings/#installed-apps

   .. code-block:: python

       INSTALLED_APPS = [
           ...,
           "django_mcpz",
           ...,
       ]

   Depending on your use case, you may want to install one of the optional apps:

   * ``django_mcpz.bearer_tokens``: provides per-client bearer tokens for authenticating MCP clients from developer tools.
     See :ref:`server-bearer-tokens`.

   * ``django_mcpz.oauth``: provides OAuth authentication for hosted assistants like Claude.ai and ChatGPT.
     See :doc:`oauth`.

Now you’re ready to make an MCP server object and route it, as covered in :doc:`servers`.
