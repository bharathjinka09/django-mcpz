from __future__ import annotations

from django.db import connections
from django.test import TestCase


class PostgresTests(TestCase):
    databases = {"postgres"}

    def test_connects(self):
        # A smoke test, so a missing PostgreSQL fails here first. Start it
        # with: docker compose up -d --wait
        with connections["postgres"].cursor() as cursor:
            cursor.execute("SELECT 1")
            assert cursor.fetchone() == (1,)
