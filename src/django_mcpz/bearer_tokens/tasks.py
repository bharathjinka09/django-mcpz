"""
Background tasks, for Django's tasks framework (Django 6.0+).

Enqueue them from a scheduler, or run the equivalent management command
from cron on earlier Django versions.
"""

from __future__ import annotations

import django

from django_mcpz.bearer_tokens import cleanup

if django.VERSION >= (6, 0):
    # Keep module importable on Django < 6.0, where Celery autodiscovery or
    # other things might try to import it.
    from django.tasks import task

    @task
    def clear_expired() -> int:
        """
        Delete expired and revoked bearer tokens.

        Returns the number deleted.
        """
        return cleanup.clear_expired()
