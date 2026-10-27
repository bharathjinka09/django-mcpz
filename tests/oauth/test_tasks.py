from __future__ import annotations

import datetime as dt

import django
import pytest
from django.contrib.auth.models import User
from django.test import TestCase

from django_mcpz.oauth import tasks
from tests.oauth.utils import make_access_token


@pytest.mark.skipif(django.VERSION >= (6, 0), reason="Before Django 6.0")
class NoTasksTests(TestCase):
    def test_no_tasks_before_django_6(self):
        assert not hasattr(tasks, "clear_expired")


@pytest.mark.skipif(django.VERSION < (6, 0), reason="Django tasks framework, 6.0+")
class TasksTests(TestCase):
    """The cleanup task, run through the default immediate backend."""

    def test_clear_expired(self):
        user = User.objects.create_user("alice")
        live, _ = make_access_token(user=user)
        make_access_token(user=user, lifetime=dt.timedelta(-1))

        result = tasks.clear_expired.enqueue()

        assert result.return_value == {
            "refresh_tokens": 0,
            "access_tokens": 1,
            "codes": 0,
            "clients": 0,
        }
        assert list(type(live).objects.all()) == [live]
