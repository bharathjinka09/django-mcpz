"""
Background tasks, for Django's tasks framework (Django 6.0+).

Enqueue them from a scheduler, or run the equivalent management command
from cron on earlier Django versions.
"""

from __future__ import annotations

import django

from django_mcpz.oauth import cleanup

if django.VERSION >= (6, 0):
    # Keep module importable on Django < 6.0, where Celery autodiscovery or
    # other things might try to import it.
    from django.tasks import task

    @task
    def clear_expired() -> dict[str, int]:
        """
        Delete expired and revoked codes and tokens, and unused clients.

        Returns the number of rows deleted, by kind.
        """
        return cleanup.clear_expired()
