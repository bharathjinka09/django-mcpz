"""
Form mode elicitation: a tool asking the user a question.

MCP version 2026-07-28 has no server-to-client requests, so a tool that needs
input returns an input_required result, and the client answers by retrying the
whole call. The answers so far travel in requestState, which arrives through
the client, so it is signed and bound to the caller, the tool, and the call's
arguments.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Literal

import msgspec
import msgspec.json
from django.core import signing
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest

STATE_SALT = "django_mcpz.elicitation"

# Seconds a requestState stays valid, keeping the replay window short, as the
# specification asks, while leaving a person time to answer.
STATE_MAX_AGE = 600


class Answer(msgspec.Struct):
    """One client response to an elicitation request."""

    action: Literal["accept", "decline", "cancel"]
    content: dict[str, Any] | None = None


class State(msgspec.Struct):
    """
    The contents of a requestState: the call it belongs to, the answers so far,
    and the key of the question awaiting an answer.
    """

    principal: str
    tool: str
    arguments: str
    answers: dict[str, Answer]
    pending: str


class StateSerializer:
    """Serialize a signed State with msgspec, as elsewhere in the package."""

    def dumps(self, obj: State) -> bytes:
        return msgspec.json.encode(obj)

    def loads(self, data: bytes) -> State:
        return msgspec.json.decode(data, type=State)


class BadStateError(Exception):
    """An untrustworthy requestState, or an unparsable inputResponses."""


class InputRequired(BaseException):
    """
    An elicit() call with no answer yet, carrying the request to ask for one.

    A BaseException, like KeyboardInterrupt, since it is control flow rather
    than an error, so a tool's ``except Exception`` cannot swallow it.
    """

    def __init__(
        self, key: str, message: str, requested_schema: dict[str, Any]
    ) -> None:
        super().__init__(message)
        self.key = key
        self.message = message
        self.requested_schema = requested_schema


@dataclass
class Elicitation:
    """
    The elicitation state of one tools/call, put on the request for elicit().
    """

    server: str
    principal: str
    tool: str
    arguments: dict[str, Any]
    answers: dict[str, Answer] = field(default_factory=dict)
    unavailable: str | None = None
    expired: bool = False
    issued: list[str] = field(default_factory=list)

    @classmethod
    def for_call(
        cls,
        request: HttpRequest,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        *,
        input_responses: object,
        request_state: object,
        unavailable: str | None,
    ) -> Elicitation:
        """
        Build the elicitation state for a call, from the answers it carries.

        Only an answer to the question the requestState says is pending is
        taken from inputResponses, so a client cannot answer a question before
        it is asked. Raise BadStateError for a requestState that fails
        verification, or an unparsable answer to the pending question.

        An expired requestState is not an error, since a person may simply
        have taken a while to answer. Its answers are dropped, and the call
        runs as though new, so the tool asks again, marked as expired.
        """
        elicitation = cls(
            server=server,
            principal=principal_of(request),
            tool=tool,
            arguments=arguments,
            unavailable=unavailable,
        )
        if request_state is None:
            return elicitation
        state = elicitation._verified_state(request_state)
        if state is None:
            elicitation.expired = True
            return elicitation
        elicitation.answers.update(state.answers)
        answer = _pending_answer(input_responses, state.pending)
        if answer is not None:
            elicitation.answers[state.pending] = answer
        return elicitation

    def claim_key(self, key: str | None) -> str:
        """
        The key one elicit() call asks and answers under, by position when it
        was given none. Two questions sharing a key would both be given the
        first answer, so a reused key is an error.
        """
        if key is None:
            key = f"elicitation-{len(self.issued)}"
        if key in self.issued:
            raise ImproperlyConfigured(
                f"Two elicit() calls in the tool {self.tool!r} asked under"
                f" the key {key!r}. Each question needs its own key, since"
                " its answer comes back under it."
            )
        self.issued.append(key)
        return key

    @property
    def salt(self) -> str:
        return f"{STATE_SALT}:{self.server}"

    def request_state(self, pending: str) -> str:
        """The signed state to send with a question asked under pending."""
        state = State(
            principal=self.principal,
            tool=self.tool,
            arguments=digest(self.arguments),
            answers=self.answers,
            pending=pending,
        )
        return signing.dumps(state, salt=self.salt, serializer=StateSerializer)

    def _verified_state(self, value: object) -> State | None:
        """
        A requestState, once its signature is checked and it proves to belong
        to this call, by this caller, or None if it has expired.
        """
        if not isinstance(value, str):
            raise BadStateError("requestState must be a string")
        expired = False
        try:
            state: State = signing.loads(
                value,
                salt=self.salt,
                serializer=StateSerializer,
                max_age=STATE_MAX_AGE,
            )
        except signing.SignatureExpired:
            # Raised only once the signature checks out, so the state is
            # genuine, and still checked as belonging to this call below.
            expired = True
            state = signing.loads(value, salt=self.salt, serializer=StateSerializer)
        except signing.BadSignature:
            raise BadStateError("Invalid requestState") from None
        issued_for = (state.principal, state.tool, state.arguments)
        if issued_for != (self.principal, self.tool, digest(self.arguments)):
            raise BadStateError("Invalid requestState")
        if expired:
            return None
        return state


def digest(arguments: dict[str, Any]) -> str:
    """
    A digest of a call's arguments, binding a state to the call that issued
    it, taken in a fixed key order so that a reordered retry still matches.
    """
    encoded = msgspec.json.encode(arguments, order="deterministic")
    return hashlib.sha256(encoded).hexdigest()


def principal_of(request: HttpRequest) -> str:
    """
    Who the request is authenticated as, to bind a state to one caller: the
    user, plus the OAuth client, which outlives a token refresh, or else the
    bearer token itself. Empty for a caller the auth callable left
    unidentified.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return ""
    token = getattr(request, "mcp_token", None)
    if token is None:
        return f"user:{user.pk}"
    # Not the OAuth client_id string, which lives on the client row itself.
    client = getattr(token, "client_id", None)
    if client is None:
        return f"user:{user.pk}:token:{token.pk}"
    return f"user:{user.pk}:client:{client}"


def _pending_answer(input_responses: object, pending: str) -> Answer | None:
    """
    The answer to the pending question, ignoring any other keys, as the
    specification asks.
    """
    if input_responses is None:
        return None
    if not isinstance(input_responses, dict):
        raise BadStateError("inputResponses must be an object")
    value = input_responses.get(pending)
    if value is None:
        return None
    try:
        return msgspec.convert(value, Answer)
    except msgspec.ValidationError as exc:
        raise BadStateError(f"Invalid inputResponses: {exc}") from None
