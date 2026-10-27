from __future__ import annotations

import datetime as dt
from io import StringIO

import pytest
from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.test import TestCase, modify_settings
from django.utils import timezone

from django_mcpz import tokens
from django_mcpz.bearer_tokens.models import Token


class MCPTokensCreateCommandTests(TestCase):
    def test_not_installed(self):
        with (
            modify_settings(INSTALLED_APPS={"remove": "django_mcpz.bearer_tokens"}),
            pytest.raises(CommandError, match="invalid choice: 'bearer-tokens'"),
        ):
            call_command("mcpz", "bearer-tokens", "create", "Laptop", "--user", "alice")

    def test_create(self):
        user = User.objects.create_user("alice")
        out = StringIO()

        call_command(
            "mcpz", "bearer-tokens", "create", "Laptop", "--user", "alice", stdout=out
        )

        value = out.getvalue().strip()
        assert value.startswith(tokens.PREFIX)
        token = Token.objects.get()
        assert token.name == "Laptop"
        assert token.user == user
        assert token.digest == tokens.sha256_hex(value)

    def test_expires_in_days(self):
        User.objects.create_user("alice")

        call_command(
            "mcpz",
            "bearer-tokens",
            "create",
            "Laptop",
            "--user",
            "alice",
            "--expires-in-days",
            "30",
            stdout=StringIO(),
        )

        token = Token.objects.get()
        assert token.expires_at is not None
        remaining = token.expires_at - timezone.now()
        assert dt.timedelta(days=29, hours=23) < remaining <= dt.timedelta(days=30)

    def test_expires_in_days_not_positive(self):
        User.objects.create_user("alice")

        with pytest.raises(CommandError, match="must be positive"):
            call_command(
                "mcpz",
                "bearer-tokens",
                "create",
                "Laptop",
                "--user",
                "alice",
                "--expires-in-days",
                "0",
            )

    def test_name_too_long(self):
        # Longer than the field it would be stored in, which most databases
        # reject rather than truncate.
        User.objects.create_user("alice")

        with pytest.raises(CommandError, match="at most 200 characters"):
            call_command(
                "mcpz", "bearer-tokens", "create", "x" * 201, "--user", "alice"
            )

        assert not Token.objects.exists()

    def test_user_required(self):
        with pytest.raises(CommandError, match="--user"):
            call_command("mcpz", "bearer-tokens", "create", "Laptop")

    def test_unknown_user(self):
        with pytest.raises(CommandError, match="No user named 'nobody'"):
            call_command(
                "mcpz", "bearer-tokens", "create", "Laptop", "--user", "nobody"
            )


class MCPTokensClearCommandTests(TestCase):
    def test_not_installed(self):
        with (
            modify_settings(INSTALLED_APPS={"remove": "django_mcpz.bearer_tokens"}),
            pytest.raises(CommandError, match="invalid choice: 'bearer-tokens'"),
        ):
            call_command("mcpz", "bearer-tokens", "clear")

    def test_clears(self):
        user = User.objects.create_user("alice")
        live, _ = Token.create(name="live", user=user)
        Token.create(
            name="expired",
            user=user,
            expires_at=timezone.now() - dt.timedelta(seconds=1),
        )
        revoked, _ = Token.create(name="revoked", user=user)
        revoked.revoke()
        out = StringIO()

        call_command("mcpz", "bearer-tokens", "clear", stdout=out)

        assert out.getvalue() == "Deleted 2 bearer tokens.\n"
        assert list(Token.objects.all()) == [live]
