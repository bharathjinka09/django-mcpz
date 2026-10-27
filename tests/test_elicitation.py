from __future__ import annotations

import datetime as dt
import enum
from http import HTTPStatus
from typing import Annotated, Any, Literal
from unittest import mock

import msgspec
import pytest
import time_machine
from django.contrib.auth.models import AnonymousUser, User
from django.core import signing
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest
from django.test import (
    RequestFactory,
    SimpleTestCase,
    TestCase,
    TransactionTestCase,
)
from unittest_parametrize import ParametrizedTestCase, param, parametrize

from django_mcpz.bearer_tokens.models import Token
from django_mcpz.elicitation import (
    STATE_MAX_AGE,
    STATE_SALT,
    Answer,
    Elicitation,
    State,
    StateSerializer,
    digest,
    principal_of,
)
from django_mcpz.jsonrpc import INVALID_PARAMS
from django_mcpz.schemas import elicitation_schema
from django_mcpz.server import PROTOCOL_VERSION, elicit
from tests.models import Widget
from tests.oauth.utils import make_access_token, make_client
from tests.test_server import ServerTestCase, make_message

FORM: dict[str, Any] = {"elicitation": {"form": {}}}

SALT = f"{STATE_SALT}:elicitation-server"

ISSUED = dt.datetime(2026, 10, 1, 12, 0, tzinfo=dt.UTC)


def time_travel(seconds: int) -> time_machine.travel:
    """Freeze the clock some seconds after a fixed moment."""
    return time_machine.travel(ISSUED + dt.timedelta(seconds=seconds), tick=False)


# Module level, since msgspec resolves deferred annotations in module scope.


class Size(enum.Enum):
    small = "small"
    large = "large"


class Sized(msgspec.Struct):
    size: Size


class Nested(msgspec.Struct):
    inner: Sized


class Noted(msgspec.Struct):
    note: str | None = None


class Tally(msgspec.Struct):
    counts: list[int] = []


class Choices(msgspec.Struct):
    picked: list[Literal["red", "green"]] = []


class Number(enum.Enum):
    one = 1
    two = 2


class Numbered(msgspec.Struct):
    number: Number


class Documented(enum.Enum):
    """A docstring describing the type, not any one field."""

    red = "red"
    green = "green"


class Single(msgspec.Struct):
    choice: Literal["a", "b"]
    other: Literal["a", "b"] = "a"


class NumericLiteral(msgspec.Struct):
    number: Literal[1, 2]


class MixedLiteral(msgspec.Struct):
    value: Literal["a", 1]


class Defaulted(msgspec.Struct):
    colour: Documented = Documented.green
    size: Annotated[Size, msgspec.Meta(description="Which size?")] = Size.large


class Bare(msgspec.Struct):
    anything: list = []  # type: ignore[type-arg]


class Empty(msgspec.Struct):
    pass


class Plain(msgspec.Struct):
    name: str
    ratio: float
    flag: bool
    count: int


class Constrained(msgspec.Struct):
    count: Annotated[int, msgspec.Meta(ge=1, le=3)] = 1
    name: Annotated[
        str, msgspec.Meta(description="Name", min_length=1, max_length=5)
    ] = "x"


class ElicitationTestCase(ServerTestCase):
    """Calls that carry the elicitation capability, and answers to questions."""

    url = "/elicitation-mcp"

    def call(
        self,
        name: str,
        *,
        answers: object = None,
        request_state: object = None,
        arguments: dict[str, Any] | None = None,
        capabilities: object = FORM,
        headers: dict[str, str | None] | None = None,
    ) -> Any:
        params: dict[str, Any] = {"name": name, "arguments": arguments or {}}
        if answers is not None:
            params["inputResponses"] = answers
        if request_state is not None:
            params["requestState"] = request_state
        message = make_message("tools/call", params, client_capabilities=capabilities)
        return self.post(message, headers=headers)

    def assert_asked(self, response: Any, key: str = "elicitation-0") -> dict[str, Any]:
        """The result asks one question, and carries the state to resume with."""
        assert response.status_code == HTTPStatus.OK
        assert response.headers["Content-Type"] == "application/json"
        data = response.json()
        assert data["jsonrpc"] == "2.0"
        assert data["id"] == 1
        result: dict[str, Any] = data["result"]
        assert result["resultType"] == "input_required"
        assert list(result["inputRequests"]) == [key]
        assert result["inputRequests"][key]["method"] == "elicitation/create"
        assert result["requestState"]
        return result

    def assert_completed(self, response: Any) -> dict[str, Any]:
        """The result is a finished tool result, not another question."""
        assert response.status_code == HTTPStatus.OK
        result: dict[str, Any] = response.json()["result"]
        assert result["resultType"] == "complete"
        return result

    def answer(
        self,
        name: str,
        asked: dict[str, Any],
        content: dict[str, Any] | None,
        *,
        key: str = "elicitation-0",
        action: str = "accept",
        arguments: dict[str, Any] | None = None,
        headers: dict[str, str | None] | None = None,
    ) -> Any:
        """Retry the call with an answer, as a client does."""
        answer: dict[str, Any] = {"action": action}
        if content is not None:
            answer["content"] = content
        return self.call(
            name,
            answers={key: answer},
            request_state=asked["requestState"],
            arguments=arguments,
            headers=headers,
        )


class AskingTests(ElicitationTestCase, ParametrizedTestCase):
    def test_asks_on_the_first_call(self):
        response = self.call("confirm")

        asked = self.assert_asked(response)
        params = asked["inputRequests"]["elicitation-0"]["params"]
        assert params["mode"] == "form"
        assert params["message"] == "Really do the thing?"
        assert params["requestedSchema"] == {
            "type": "object",
            "properties": {"confirmed": {"type": "boolean", "default": False}},
        }

    def test_not_swallowed_by_except_exception(self):
        asked = self.assert_asked(self.call("confirm_catching"))

        response = self.answer("confirm_catching", asked, {"confirmed": True})

        result = self.assert_completed(response)
        assert result["content"] == [{"type": "text", "text": "Confirmed: True"}]

    def test_server_info_on_the_question(self):
        response = self.call("confirm")

        asked = self.assert_asked(response)
        server_info = asked["_meta"]["io.modelcontextprotocol/serverInfo"]
        assert server_info["name"] == "elicitation-server"

    def test_answer_completes_the_call(self):
        asked = self.assert_asked(self.call("confirm"))

        response = self.answer("confirm", asked, {"confirmed": True})

        result = self.assert_completed(response)
        assert result["isError"] is False
        assert result["content"] == [{"type": "text", "text": "Confirmed: True"}]

    def test_answer_without_content(self):
        # An approval-only question, where every field has a default.
        asked = self.assert_asked(self.call("confirm"))

        response = self.answer("confirm", asked, None)

        result = self.assert_completed(response)
        assert result["content"] == [{"type": "text", "text": "Confirmed: False"}]

    @parametrize(
        "action,wording",
        [
            param("decline", "declined", id="declined"),
            param("cancel", "dismissed", id="cancelled"),
        ],
    )
    def test_refused(self, action, wording):
        asked = self.assert_asked(self.call("confirm"))

        response = self.answer("confirm", asked, None, action=action)

        result = self.assert_completed(response)
        assert result["isError"] is True
        assert result["content"] == [
            {
                "type": "text",
                "text": f"The user {wording} the question: Really do the thing?",
            }
        ]

    def test_answer_returned_to_a_tool_that_catches_refusals(self):
        asked = self.assert_asked(self.call("confirm_or_carry_on"))

        response = self.answer("confirm_or_carry_on", asked, {"confirmed": True})

        result = self.assert_completed(response)
        assert result["content"] == [{"type": "text", "text": "Confirmed: True"}]

    @parametrize(
        "action",
        [
            param("decline", id="declined"),
            param("cancel", id="cancelled"),
        ],
    )
    def test_refusal_caught_by_the_tool(self, action):
        asked = self.assert_asked(self.call("confirm_or_carry_on"))

        response = self.answer("confirm_or_carry_on", asked, None, action=action)

        result = self.assert_completed(response)
        assert result["isError"] is False
        assert result["content"] == [{"type": "text", "text": "Carried on without it."}]

    def test_answer_returned_to_a_tool_catching_tool_error(self):
        asked = self.assert_asked(self.call("confirm_catching_tool_error"))

        response = self.answer(
            "confirm_catching_tool_error", asked, {"confirmed": True}
        )

        result = self.assert_completed(response)
        assert result["content"] == [{"type": "text", "text": "Confirmed: True"}]

    def test_refusal_not_a_tool_error(self):
        asked = self.assert_asked(self.call("confirm_catching_tool_error"))

        response = self.answer(
            "confirm_catching_tool_error", asked, None, action="decline"
        )

        result = self.assert_completed(response)
        assert result["isError"] is True
        assert result["content"] == [
            {
                "type": "text",
                "text": "The user declined the question: Really do the thing?",
            }
        ]

    def test_invalid_answer_asked_again(self):
        # The specification asks servers to ask again, rather than error.
        asked = self.assert_asked(self.call("confirm"))

        response = self.answer("confirm", asked, {"confirmed": "yes"})

        self.assert_asked(response)

    def test_invalid_answer_not_carried_on(self):
        asked = self.assert_asked(self.call("confirm"))

        again = self.assert_asked(self.answer("confirm", asked, {"confirmed": "yes"}))

        state: State = signing.loads(
            again["requestState"], salt=SALT, serializer=StateSerializer
        )
        assert state.answers == {}
        assert state.pending == "elicitation-0"

    def test_plain_schema_sent_as_given(self):
        response = self.call("ask_name")

        asked = self.assert_asked(response, key="name")
        assert asked["inputRequests"]["name"]["params"]["requestedSchema"] == {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }

    def test_plain_schema_answer_returned_as_a_dict(self):
        asked = self.assert_asked(self.call("ask_name"), key="name")

        response = self.answer("ask_name", asked, {"name": "Alice"}, key="name")

        result = self.assert_completed(response)
        assert result["content"] == [{"type": "text", "text": "Hello, Alice!"}]

    def test_unanswerable_schema_reported_as_a_failure(self):
        with self.assertLogs("django_mcpz", level="ERROR"):
            response = self.call("ask_impossible")

        result = self.assert_completed(response)
        assert result["isError"] is True
        assert result["content"][0]["text"] == (
            "Tool 'ask_impossible' failed unexpectedly."
        )

    def call_record(self, name: str) -> Any:
        with self.assertLogs("django_mcpz.calls", level="INFO") as logs:
            self.call(name)
        (record,) = logs.records
        return record

    def test_logged_outcome(self):
        record = self.call_record("confirm")

        assert record.outcome == "input_required"

    def test_outside_a_tool(self):
        request = RequestFactory().post(self.url)

        with pytest.raises(ImproperlyConfigured) as excinfo:
            elicit(request, "Really?", Sized)

        assert excinfo.value.args[0] == (
            "elicit() works only inside a tool function, called by an MCP"
            " server, which puts the state it needs on the request."
        )


class KeyTests(ElicitationTestCase):
    """An answer comes back under a key alone, so each question needs one."""

    def test_reused_key_rejected(self):
        asked = self.assert_asked(self.call("ask_twice_under_one_key"), key="same")

        with self.assertLogs("django_mcpz", level="ERROR") as logs:
            response = self.answer(
                "ask_twice_under_one_key", asked, {"confirmed": True}, key="same"
            )

        result = self.assert_completed(response)
        assert result["isError"] is True
        assert (
            logs.records[0].getMessage()
            == "Tool 'ask_twice_under_one_key' raised an exception"
        )
        exc_info = logs.records[0].exc_info
        assert exc_info is not None
        assert str(exc_info[1]) == (
            "Two elicit() calls in the tool 'ask_twice_under_one_key' asked"
            " under the key 'same'. Each question needs its own key, since"
            " its answer comes back under it."
        )


class SeveralQuestionsTests(ElicitationTestCase):
    """
    A tool asking more than one question runs again for each, with the
    answers already given carried in the state rather than resent.
    """

    def test_one_question_at_a_time(self):
        first = self.assert_asked(self.call("order_dessert"))
        assert first["inputRequests"]["elicitation-0"]["params"]["message"] == (
            "What would you like?"
        )

        second = self.answer(
            "order_dessert", first, {"flavour": "chocolate", "scoops": 2}
        )

        asked = self.assert_asked(second, key="elicitation-1")
        assert asked["inputRequests"]["elicitation-1"]["params"]["message"] == (
            "Add a tip?"
        )
        response = self.answer(
            "order_dessert", asked, {"confirmed": True}, key="elicitation-1"
        )

        result = self.assert_completed(response)
        assert result["structuredContent"] == {
            "flavour": "chocolate",
            "scoops": 2,
            "tip": True,
        }

    def test_enum_schema_carries_its_type(self):
        response = self.call("order_dessert")

        asked = self.assert_asked(response)
        schema = asked["inputRequests"]["elicitation-0"]["params"]["requestedSchema"]
        assert schema["properties"]["flavour"] == {
            "type": "string",
            "enum": ["chocolate", "vanilla"],
        }
        assert schema["required"] == ["flavour"]

    def test_constraints_carried(self):
        response = self.call("order_dessert")

        asked = self.assert_asked(response)
        schema = asked["inputRequests"]["elicitation-0"]["params"]["requestedSchema"]
        assert schema["properties"]["scoops"] == {
            "type": "integer",
            "minimum": 1,
            "maximum": 3,
            "default": 1,
        }


class CapabilityTests(ElicitationTestCase, ParametrizedTestCase):
    """
    The 2026-07-28 revision carries the client's capabilities on every
    request, so a tool knows per call whether it can ask anything.
    """

    @parametrize(
        "capabilities",
        [
            param({}, id="none_declared"),
            param("all of them", id="not_an_object"),
            param({"elicitation": True}, id="elicitation_not_object"),
            param({"elicitation": {"url": {}}}, id="url_only"),
        ],
    )
    def test_cannot_ask(self, capabilities):
        response = self.call("confirm", capabilities=capabilities)

        result = self.assert_completed(response)
        assert result["isError"] is True
        assert result["content"] == [
            {
                "type": "text",
                "text": (
                    "This tool needs to ask you a question, which this client"
                    " does not support (form mode MCP elicitation)."
                ),
            }
        ]

    def test_no_mode_means_form_mode(self):
        # For compatibility, per the specification.
        response = self.call("confirm", capabilities={"elicitation": {}})

        self.assert_asked(response)

    def test_tool_that_falls_back_asks_when_it_can(self):
        asked = self.assert_asked(self.call("confirm_or_refuse"))

        response = self.answer("confirm_or_refuse", asked, {"confirmed": True})

        result = self.assert_completed(response)
        assert result["content"] == [{"type": "text", "text": "Done."}]

    def test_unavailable_caught_by_the_tool(self):
        response = self.call("confirm_or_refuse", capabilities={})

        result = self.assert_completed(response)
        assert result["isError"] is False
        assert result["content"] == [
            {"type": "text", "text": "Refused without confirmation."}
        ]

    def test_tool_that_never_asks_unaffected(self):
        response = self.call("no_question", capabilities={})

        result = self.assert_completed(response)
        assert result["isError"] is False
        assert result["content"] == [{"type": "text", "text": "No question."}]


class LegacyProtocolTests(ElicitationTestCase):
    """The 2025 revisions cannot carry a question to the client at all."""

    def call_legacy(self, name: str) -> Any:
        message = make_message(
            "tools/call",
            {"name": name, "arguments": {}},
            protocol_version=None,
            client_capabilities=None,
        )
        return self.post(
            message,
            headers={
                "MCP-Protocol-Version": None,
                "Mcp-Method": None,
                "Mcp-Name": None,
            },
        )

    def test_unavailable_caught_by_the_tool(self):
        response = self.call_legacy("confirm_or_refuse")

        result = response.json()["result"]
        assert result["isError"] is False
        assert result["content"] == [
            {"type": "text", "text": "Refused without confirmation."}
        ]

    def test_tool_that_never_asks_unaffected(self):
        response = self.call_legacy("no_question")

        result = response.json()["result"]
        assert result["isError"] is False
        assert result["content"] == [{"type": "text", "text": "No question."}]

    def test_cannot_ask(self):
        response = self.call_legacy("confirm")

        result = response.json()["result"]
        assert result["isError"] is True
        assert result["content"] == [
            {
                "type": "text",
                "text": (
                    "This tool needs to ask you a question, which needs MCP"
                    f" version {PROTOCOL_VERSION}. This client is using an"
                    " earlier version."
                ),
            }
        ]


class RequestStateTests(ElicitationTestCase, ParametrizedTestCase):
    """
    requestState reaches the server through the client, so it is signed, and
    bound to the caller, the tool, and the arguments it was issued for.
    """

    def state(self, **overrides: Any) -> str:
        fields: dict[str, Any] = {
            "principal": "",
            "tool": "confirm",
            "arguments": digest({}),
            "answers": {"elicitation-0": Answer(action="accept")},
            "pending": "elicitation-0",
        }
        fields.update(overrides)
        return signing.dumps(State(**fields), salt=SALT, serializer=StateSerializer)

    def test_own_state_accepted(self):
        response = self.call("confirm", request_state=self.state())

        result = self.assert_completed(response)
        assert result["content"] == [{"type": "text", "text": "Confirmed: False"}]

    def test_not_a_string(self):
        response = self.call("confirm", request_state=12)

        error = self.assert_error(response, INVALID_PARAMS)
        assert error["message"] == "requestState must be a string"

    def test_tampered(self):
        asked = self.assert_asked(self.call("confirm"))

        response = self.call("confirm", request_state=asked["requestState"] + "x")

        error = self.assert_error(response, INVALID_PARAMS)
        assert error["message"] == "Invalid requestState"

    @parametrize(
        "overrides",
        [
            param({"tool": "ask_name"}, id="another_tool"),
            param({"arguments": digest({"a": 1})}, id="other_arguments"),
            param({"principal": "user:1:client:1"}, id="another_principal"),
        ],
    )
    def test_issued_for_something_else(self, overrides):
        response = self.call("confirm", request_state=self.state(**overrides))

        self.assert_error(response, INVALID_PARAMS)

    def call_after(self, seconds: int, name: str, **overrides: Any) -> Any:
        """Call with a genuine state, some seconds after it was issued."""
        with time_travel(0):
            state = self.state(tool=name, **overrides)
        with time_travel(seconds):
            return self.call(
                name,
                request_state=state,
                answers={"elicitation-0": {"action": "accept"}},
            )

    def call_expired(self, name: str, **overrides: Any) -> Any:
        return self.call_after(3600, name, **overrides)

    def test_valid_just_inside_the_max_age(self):
        response = self.call_after(STATE_MAX_AGE - 1, "confirm")

        result = self.assert_completed(response)
        assert result["content"] == [{"type": "text", "text": "Confirmed: False"}]

    def test_expired_just_outside_the_max_age(self):
        response = self.call_after(STATE_MAX_AGE + 1, "confirm")

        self.assert_asked(response)

    def test_max_age_is_ten_minutes(self):
        assert STATE_MAX_AGE == 600

    def test_expired_asked_again(self):
        asked = self.assert_asked(self.call_expired("confirm"))

        params = asked["inputRequests"]["elicitation-0"]["params"]
        assert params["message"] == (
            "Your earlier answer expired, so this is asked again.\n\n"
            "Really do the thing?"
        )

    def test_expired_then_answered(self):
        asked = self.assert_asked(self.call_expired("confirm"))

        with time_travel(3600):
            response = self.answer("confirm", asked, {"confirmed": True})

        result = self.assert_completed(response)
        assert result["content"] == [{"type": "text", "text": "Confirmed: True"}]

    @parametrize(
        "name,message",
        [
            param(
                "confirm_custom_expiry",
                "Too slow!\n\nReally do the thing?",
                id="custom",
            ),
            param("confirm_no_expiry_note", "Really do the thing?", id="none"),
        ],
    )
    def test_expired_message(self, name, message):
        asked = self.assert_asked(self.call_expired(name))

        params = asked["inputRequests"]["elicitation-0"]["params"]
        assert params["message"] == message
        with time_travel(3600):
            response = self.answer(name, asked, {"confirmed": True})
        result = self.assert_completed(response)
        assert result["content"] == [{"type": "text", "text": "Confirmed: True"}]

    def test_expired_issued_for_something_else(self):
        response = self.call_expired("confirm", principal="user:1:client:1")

        error = self.assert_error(response, INVALID_PARAMS)
        assert error["message"] == "Invalid requestState"

    def test_expired_tampered(self):
        with time_travel(0):
            state = self.state() + "x"

        with time_travel(3600):
            response = self.call("confirm", request_state=state)

        error = self.assert_error(response, INVALID_PARAMS)
        assert error["message"] == "Invalid requestState"

    def test_expired_midway_asks_from_the_start(self):
        # The first answer is carried in the state, so it expires with it, and
        # the tool asks its first question again, dropping the stale answer.
        with time_travel(0):
            first = self.assert_asked(self.call("order_dessert"))
        with time_travel(10):
            second = self.assert_asked(
                self.answer("order_dessert", first, {"flavour": "vanilla"}),
                key="elicitation-1",
            )

        with time_travel(10 + STATE_MAX_AGE + 1):
            response = self.answer(
                "order_dessert", second, {"confirmed": True}, key="elicitation-1"
            )

        again = self.assert_asked(response)
        params = again["inputRequests"]["elicitation-0"]["params"]
        assert params["message"] == (
            "Your earlier answer expired, so this is asked again.\n\n"
            "What would you like?"
        )
        state: State = signing.loads(
            again["requestState"], salt=SALT, serializer=StateSerializer
        )
        assert state.answers == {}
        assert state.pending == "elicitation-0"

    def test_expired_midway_then_answered_through(self):
        with time_travel(0):
            first = self.assert_asked(self.call("order_dessert"))
            second = self.assert_asked(
                self.answer("order_dessert", first, {"flavour": "vanilla"}),
                key="elicitation-1",
            )
        with time_travel(STATE_MAX_AGE + 1):
            again = self.assert_asked(
                self.answer(
                    "order_dessert", second, {"confirmed": True}, key="elicitation-1"
                )
            )
            third = self.assert_asked(
                self.answer("order_dessert", again, {"flavour": "chocolate"}),
                key="elicitation-1",
            )
            response = self.answer(
                "order_dessert", third, {"confirmed": False}, key="elicitation-1"
            )

        result = self.assert_completed(response)
        assert result["structuredContent"] == {
            "flavour": "chocolate",
            "scoops": 1,
            "tip": False,
        }

    def test_another_servers_state(self):
        state = signing.dumps(
            State(
                principal="",
                tool="confirm",
                arguments=digest({}),
                answers={},
                pending="elicitation-0",
            ),
            salt=f"{STATE_SALT}:another-server",
            serializer=StateSerializer,
        )

        response = self.call("confirm", request_state=state)

        self.assert_error(response, INVALID_PARAMS)

    @parametrize(
        "answer",
        [
            param({"action": "maybe"}, id="unknown_action"),
            param({"content": {}}, id="no_action"),
            param({"action": "accept", "content": "yes"}, id="content_a_string"),
            param({"action": "accept", "content": [1]}, id="content_a_list"),
            param("accept", id="answer_a_string"),
            param([], id="answer_a_list"),
        ],
    )
    def test_malformed_answer(self, answer):
        asked = self.assert_asked(self.call("confirm"))

        response = self.call(
            "confirm",
            answers={"elicitation-0": answer},
            request_state=asked["requestState"],
        )

        error = self.assert_error(response, INVALID_PARAMS)
        assert error["message"].startswith("Invalid inputResponses:")

    def test_null_answer_is_no_answer(self):
        asked = self.assert_asked(self.call("confirm"))

        response = self.call(
            "confirm",
            answers={"elicitation-0": None},
            request_state=asked["requestState"],
        )

        self.assert_asked(response)

    def test_empty_answers_is_no_answer(self):
        asked = self.assert_asked(self.call("confirm"))

        response = self.call("confirm", answers={}, request_state=asked["requestState"])

        self.assert_asked(response)

    def test_answers_not_an_object(self):
        asked = self.assert_asked(self.call("confirm"))

        response = self.call("confirm", answers=[], request_state=asked["requestState"])

        error = self.assert_error(response, INVALID_PARAMS)
        assert error["message"] == "inputResponses must be an object"

    def test_reordered_arguments_still_match(self):
        # A client is free to re-serialize the arguments on its retry.
        asked = self.assert_asked(
            self.call("confirm_arguments", arguments={"a": 1, "b": 2})
        )

        response = self.answer(
            "confirm_arguments",
            asked,
            {"confirmed": True},
            arguments={"b": 2, "a": 1},
        )

        result = self.assert_completed(response)
        assert result["content"] == [{"type": "text", "text": "Confirmed: True"}]


class PendingAnswerTests(ElicitationTestCase):
    """
    Only the question awaiting an answer takes one from inputResponses, so a
    client cannot answer a question before it is asked, or skip the state.
    """

    def test_answer_without_state_ignored(self):
        response = self.call(
            "confirm",
            answers={"elicitation-0": {"action": "accept", "content": {}}},
        )

        self.assert_asked(response)

    def test_unknown_key_ignored(self):
        asked = self.assert_asked(self.call("confirm"))

        response = self.call(
            "confirm",
            answers={
                "elicitation-0": {"action": "accept", "content": {"confirmed": True}},
                "something-else": "not an answer",
            },
            request_state=asked["requestState"],
        )

        result = self.assert_completed(response)
        assert result["content"] == [{"type": "text", "text": "Confirmed: True"}]

    def test_answer_under_another_key_ignored(self):
        asked = self.assert_asked(self.call("confirm"))

        response = self.call(
            "confirm",
            answers={"elicitation-1": {"action": "accept", "content": {}}},
            request_state=asked["requestState"],
        )

        self.assert_asked(response)

    def test_answer_to_a_question_not_yet_asked_ignored(self):
        first = self.assert_asked(self.call("order_dessert"))

        response = self.call(
            "order_dessert",
            answers={
                "elicitation-0": {
                    "action": "accept",
                    "content": {"flavour": "vanilla"},
                },
                "elicitation-1": {"action": "accept", "content": {"confirmed": True}},
            },
            request_state=first["requestState"],
        )

        asked = self.assert_asked(response, key="elicitation-1")
        assert asked["inputRequests"]["elicitation-1"]["params"]["message"] == (
            "Add a tip?"
        )


class PrincipalBindingTests(ElicitationTestCase, TestCase):
    """A question is answerable only by the caller it was asked of."""

    @classmethod
    def setUpTestData(cls):
        User.objects.create_user("alice")
        User.objects.create_user("bob")

    def test_answer_from_the_same_user(self):
        asked = self.assert_asked(
            self.call("confirm_as_user", headers={"X-User": "alice"})
        )

        response = self.answer(
            "confirm_as_user",
            asked,
            {"confirmed": True},
            headers={"X-User": "alice"},
        )

        result = self.assert_completed(response)
        assert result["content"] == [{"type": "text", "text": "alice confirmed: True"}]

    def test_answer_from_another_user(self):
        asked = self.assert_asked(
            self.call("confirm_as_user", headers={"X-User": "alice"})
        )

        response = self.answer(
            "confirm_as_user",
            asked,
            {"confirmed": True},
            headers={"X-User": "bob"},
        )

        self.assert_error(response, INVALID_PARAMS)

    def test_anonymous_caller(self):
        response = self.call("confirm_as_user")

        self.assert_asked(response)


class OAuthPrincipalBindingTests(ElicitationTestCase, TestCase):
    """
    A question asked of an OAuth client is answerable by that client for that
    user, even with a fresh token, and by nobody else.
    """

    url = "/oauth-elicitation-mcp"
    resource = "http://testserver/oauth-elicitation-mcp"
    alice: User
    bob: User

    @classmethod
    def setUpTestData(cls):
        cls.alice = User.objects.create_user("alice")
        cls.bob = User.objects.create_user("bob")

    def headers(self, user: User, client: Any) -> dict[str, str | None]:
        _, value = make_access_token(user=user, client=client, resource=self.resource)
        return {"Authorization": f"Bearer {value}"}

    def test_answered_with_a_fresh_token_for_the_same_client(self):
        # Tokens are short-lived, so the answer may arrive on a refreshed one.
        claude = make_client(name="Claude")
        asked = self.assert_asked(
            self.call("confirm_as_user", headers=self.headers(self.alice, claude))
        )

        response = self.answer(
            "confirm_as_user",
            asked,
            {"confirmed": True},
            headers=self.headers(self.alice, claude),
        )

        result = self.assert_completed(response)
        assert result["content"] == [{"type": "text", "text": "alice confirmed: True"}]

    def test_answered_with_the_same_token(self):
        claude = make_client(name="Claude")
        headers = self.headers(self.alice, claude)
        asked = self.assert_asked(self.call("confirm_as_user", headers=headers))

        response = self.answer(
            "confirm_as_user", asked, {"confirmed": True}, headers=headers
        )

        self.assert_completed(response)

    def test_answer_from_another_client_for_the_same_user(self):
        asked = self.assert_asked(
            self.call(
                "confirm_as_user",
                headers=self.headers(self.alice, make_client(name="Claude")),
            )
        )

        response = self.answer(
            "confirm_as_user",
            asked,
            {"confirmed": True},
            headers=self.headers(self.alice, make_client(name="Other")),
        )

        error = self.assert_error(response, INVALID_PARAMS)
        assert error["message"] == "Invalid requestState"

    def test_answer_from_another_user_through_the_same_client(self):
        claude = make_client(name="Claude")
        asked = self.assert_asked(
            self.call("confirm_as_user", headers=self.headers(self.alice, claude))
        )

        response = self.answer(
            "confirm_as_user",
            asked,
            {"confirmed": True},
            headers=self.headers(self.bob, claude),
        )

        self.assert_error(response, INVALID_PARAMS)


class BearerPrincipalBindingTests(ElicitationTestCase, TestCase):
    """
    A bearer token names no client, since it is one, so a question is
    answerable only with the token it was asked of.
    """

    url = "/bearer-elicitation-mcp"
    alice: User

    @classmethod
    def setUpTestData(cls):
        cls.alice = User.objects.create_user("alice")

    def headers(self, token_value: str) -> dict[str, str | None]:
        return {"Authorization": f"Bearer {token_value}"}

    def test_answered_with_the_same_token(self):
        _, value = Token.create(name="laptop", user=self.alice)
        asked = self.assert_asked(
            self.call("confirm_as_user", headers=self.headers(value))
        )

        response = self.answer(
            "confirm_as_user",
            asked,
            {"confirmed": True},
            headers=self.headers(value),
        )

        result = self.assert_completed(response)
        assert result["content"] == [{"type": "text", "text": "alice confirmed: True"}]

    def test_answer_with_another_token_for_the_same_user(self):
        _, laptop = Token.create(name="laptop", user=self.alice)
        _, phone = Token.create(name="phone", user=self.alice)
        asked = self.assert_asked(
            self.call("confirm_as_user", headers=self.headers(laptop))
        )

        response = self.answer(
            "confirm_as_user",
            asked,
            {"confirmed": True},
            headers=self.headers(phone),
        )

        error = self.assert_error(response, INVALID_PARAMS)
        assert error["message"] == "Invalid requestState"


class PrincipalTests(SimpleTestCase, ParametrizedTestCase):
    """
    The specification asks for state to name the client as well as the user.
    """

    def request(self, **attributes: object) -> HttpRequest:
        request = RequestFactory().post("/elicitation-mcp")
        for name, value in attributes.items():
            setattr(request, name, value)
        return request

    def test_nothing_set(self):
        assert principal_of(self.request()) == ""

    def test_anonymous(self):
        assert principal_of(self.request(user=AnonymousUser())) == ""

    def test_user_alone(self):
        assert principal_of(self.request(user=User(pk=1))) == "user:1"

    @parametrize(
        "token,expected",
        [
            # A bearer token names no client, since it is one.
            param(mock.Mock(pk=7, client_id=None), "user:1:token:7", id="bearer"),
            # An OAuth token names the client row it was issued to, which
            # outlives the token being refreshed.
            param(mock.Mock(client_id=3), "user:1:client:3", id="oauth"),
        ],
    )
    def test_user_and_client(self, token, expected):
        request = self.request(user=User(pk=1), mcp_token=token)

        assert principal_of(request) == expected


class ElicitationTransactionTests(ElicitationTestCase, TestCase):
    """
    A question is raised past the savepoints, so a tool that writes before
    asking leaves nothing behind for the call that answers to repeat.
    """

    def test_writes_rolled_back_when_asking(self):
        response = self.call("confirm_widget")

        self.assert_asked(response)
        assert Widget.objects.count() == 0

    def test_writes_kept_once_answered(self):
        asked = self.assert_asked(self.call("confirm_widget"))

        response = self.answer("confirm_widget", asked, {"confirmed": True})

        result = self.assert_completed(response)
        assert result["content"] == [{"type": "text", "text": "Kept."}]
        assert Widget.objects.count() == 1


class ElicitationAutocommitTests(ElicitationTestCase, TransactionTestCase):
    """Outside a transaction, a write before a question is committed."""

    def test_writes_kept_without_atomic_requests(self):
        response = self.call("confirm_widget")

        self.assert_asked(response)
        assert Widget.objects.count() == 1


class ElicitationSchemaTests(SimpleTestCase, ParametrizedTestCase):
    """
    Form mode allows a flat object of primitives only, so that any client can
    render it.
    """

    def test_enum_resolved_and_typed(self):
        assert elicitation_schema(Sized) == {
            "type": "object",
            "properties": {
                "size": {
                    "type": "string",
                    "enum": ["large", "small"],
                }
            },
            "required": ["size"],
        }

    def test_multiple_choice(self):
        assert elicitation_schema(Choices) == {
            "type": "object",
            "properties": {
                "picked": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["green", "red"]},
                    "default": [],
                }
            },
        }

    def test_literal(self):
        assert elicitation_schema(Single) == {
            "type": "object",
            "properties": {
                "choice": {"type": "string", "enum": ["a", "b"]},
                "other": {"type": "string", "enum": ["a", "b"], "default": "a"},
            },
            "required": ["choice"],
        }

    def test_enum_keeps_the_fields_default_and_description(self):
        # The $ref is replaced by the definition, with the field's own keys
        # kept, and the definition's title and docstring dropped.
        assert elicitation_schema(Defaulted) == {
            "type": "object",
            "properties": {
                "colour": {
                    "type": "string",
                    "enum": ["green", "red"],
                    "default": "green",
                },
                "size": {
                    "type": "string",
                    "enum": ["large", "small"],
                    "description": "Which size?",
                    "default": "large",
                },
            },
        }

    def test_primitives(self):
        assert elicitation_schema(Plain) == {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "ratio": {"type": "number"},
                "flag": {"type": "boolean"},
                "count": {"type": "integer"},
            },
            "required": ["name", "ratio", "flag", "count"],
        }

    def test_constraints_survive(self):
        assert elicitation_schema(Constrained) == {
            "type": "object",
            "properties": {
                "count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 3,
                    "default": 1,
                },
                "name": {
                    "type": "string",
                    "description": "Name",
                    "minLength": 1,
                    "maxLength": 5,
                    "default": "x",
                },
            },
        }

    def test_no_fields(self):
        assert elicitation_schema(Empty) == {"type": "object", "properties": {}}

    @parametrize(
        "type_,expected",
        [
            param(
                Bare,
                "Elicitation field 'anything' is a list of values that are not"
                " a fixed set of options. Form mode allows a list only as a"
                " multiple choice, so annotate its items with a Literal or an"
                " Enum.",
                id="bare_list",
            ),
            param(
                NumericLiteral,
                "Elicitation field 'number' offers options that are not all"
                " strings. Form mode allows a choice between strings only, so"
                " use a str-valued Enum or a Literal of strings.",
                id="numeric_literal",
            ),
            param(
                MixedLiteral,
                "Elicitation field 'value' offers options that are not all"
                " strings. Form mode allows a choice between strings only, so"
                " use a str-valued Enum or a Literal of strings.",
                id="mixed_literal",
            ),
            param(
                int,
                "An elicitation schema must be an object with properties,"
                " such as a msgspec.Struct subclass, not <class 'int'>.",
                id="not_an_object",
            ),
            param(
                Nested,
                "Elicitation field 'inner' is not one of the types form mode"
                " allows: a string, number, integer, boolean, a Literal or"
                " Enum of strings, or a list of a Literal or Enum of"
                " strings, as a multiple choice.",
                id="nested_object",
            ),
            param(
                Noted,
                "Elicitation field 'note' is a union of types, such as an"
                " optional field typed with None. Form mode allows one"
                " primitive type per field, so give the field a default"
                " instead.",
                id="optional_field",
            ),
            param(
                Tally,
                "Elicitation field 'counts' is a list of values that are not"
                " a fixed set of options. Form mode allows a list only as a"
                " multiple choice, so annotate its items with a Literal or an"
                " Enum.",
                id="free_list",
            ),
            param(
                Numbered,
                "Elicitation field 'number' offers options that are not all"
                " strings. Form mode allows a choice between strings only, so"
                " use a str-valued Enum or a Literal of strings.",
                id="non_string_enum",
            ),
        ],
    )
    def test_rejected(self, type_, expected):
        with pytest.raises(ImproperlyConfigured) as excinfo:
            elicitation_schema(type_)

        assert str(excinfo.value) == expected


class ElicitationStateTests(SimpleTestCase):
    def elicitation(self) -> Elicitation:
        return Elicitation(server="s", principal="", tool="confirm", arguments={})

    def test_keys_by_position(self):
        elicitation = self.elicitation()

        assert elicitation.claim_key(None) == "elicitation-0"
        assert elicitation.claim_key(None) == "elicitation-1"

    def test_chosen_key_kept(self):
        elicitation = self.elicitation()

        assert elicitation.claim_key("name") == "name"

    def test_position_counts_chosen_keys(self):
        elicitation = self.elicitation()
        elicitation.claim_key("name")

        assert elicitation.claim_key(None) == "elicitation-1"

    def test_chosen_key_reused(self):
        elicitation = self.elicitation()
        elicitation.claim_key("name")

        with pytest.raises(ImproperlyConfigured) as excinfo:
            elicitation.claim_key("name")

        assert str(excinfo.value) == (
            "Two elicit() calls in the tool 'confirm' asked under the key"
            " 'name'. Each question needs its own key, since its answer"
            " comes back under it."
        )

    def test_state_round_trip(self):
        elicitation = Elicitation(
            server="s",
            principal="user:1",
            tool="confirm",
            arguments={},
            answers={"elicitation-0": Answer(action="accept", content={"ok": True})},
        )

        state: State = signing.loads(
            elicitation.request_state("elicitation-1"),
            salt=f"{STATE_SALT}:s",
            serializer=StateSerializer,
        )

        assert state.principal == "user:1"
        assert state.pending == "elicitation-1"
        assert state.answers["elicitation-0"].content == {"ok": True}

    def test_arguments_digest_ignores_key_order(self):
        assert digest({"a": 1, "b": {"c": 2, "d": 3}}) == digest(
            {"b": {"d": 3, "c": 2}, "a": 1}
        )
