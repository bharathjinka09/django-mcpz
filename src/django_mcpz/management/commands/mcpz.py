from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Any

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone


class Command(BaseCommand):
    help = "Manage MCP bearer tokens and OAuth data."

    def add_arguments(self, parser: CommandParser) -> None:
        subparsers = parser.add_subparsers(
            title="sub-commands",
            required=True,
        )

        if apps.is_installed("django_mcpz.bearer_tokens"):
            bearer_tokens_parser = subparsers.add_parser(
                "bearer-tokens",
                help="Manage MCP bearer tokens.",
            )
            bearer_tokens_subparsers = bearer_tokens_parser.add_subparsers(
                title="sub-commands",
                required=True,
            )

            bearer_tokens_create_parser = bearer_tokens_subparsers.add_parser(
                "create",
                help="Create an MCP bearer token, printing its value once.",
            )
            bearer_tokens_create_parser.set_defaults(method=self.bearer_tokens_create)
            bearer_tokens_create_parser.add_argument(
                "name", help="A name for the token, e.g. the client using it."
            )
            bearer_tokens_create_parser.add_argument(
                "--user",
                required=True,
                help="Username of the user the token acts as.",
            )
            bearer_tokens_create_parser.add_argument(
                "--expires-in-days",
                type=int,
                help="Make the token expire this many days from now.",
            )

            bearer_tokens_clear_parser = bearer_tokens_subparsers.add_parser(
                "clear",
                help="Delete expired and revoked MCP bearer tokens.",
            )
            bearer_tokens_clear_parser.set_defaults(method=self.bearer_tokens_clear)

        if apps.is_installed("django_mcpz.oauth"):
            oauth_parser = subparsers.add_parser(
                "oauth",
                help="Manage MCP OAuth data.",
            )
            oauth_subparsers = oauth_parser.add_subparsers(
                title="sub-commands",
                required=True,
            )

            oauth_clear_parser = oauth_subparsers.add_parser(
                "clear",
                help=(
                    "Delete expired and revoked OAuth codes and tokens, and"
                    " registered clients that never obtained a token."
                ),
            )
            oauth_clear_parser.set_defaults(method=self.oauth_clear)

    def handle(
        self,
        *args: Any,
        method: Callable[..., None],
        **options: Any,
    ) -> None:
        method(*args, **options)

    def bearer_tokens_create(self, *args: Any, **options: Any) -> None:
        from django_mcpz.bearer_tokens.models import Token

        # Checked before saving, since most databases reject longer strings
        # rather than truncating them.
        max_length = Token._meta.get_field("name").max_length
        assert max_length is not None
        if len(options["name"]) > max_length:
            raise CommandError(f"name must be at most {max_length} characters.")
        User = get_user_model()
        try:
            user = User._default_manager.get_by_natural_key(options["user"])
        except User.DoesNotExist:
            raise CommandError(f"No user named {options['user']!r}.") from None
        expires_at = None
        if options["expires_in_days"] is not None:
            if options["expires_in_days"] <= 0:
                raise CommandError("--expires-in-days must be positive.")
            expires_at = timezone.now() + dt.timedelta(days=options["expires_in_days"])
        _, value = Token.create(name=options["name"], user=user, expires_at=expires_at)
        self.stdout.write(value)

    def bearer_tokens_clear(self, *args: Any, **options: Any) -> None:
        from django_mcpz.bearer_tokens.cleanup import clear_expired

        count = clear_expired()
        self.stdout.write(f"Deleted {count} bearer tokens.")

    def oauth_clear(self, *args: Any, **options: Any) -> None:
        from django_mcpz.oauth.cleanup import clear_expired

        for name, count in clear_expired().items():
            self.stdout.write(f"Deleted {count} {name.replace('_', ' ')}.")
