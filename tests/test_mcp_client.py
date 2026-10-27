from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, Literal

import anyio.to_thread
import httpx2
from django.core.handlers.wsgi import WSGIHandler
from django.test import SimpleTestCase
from mcp.client.client import Client
from mcp.client.session import ClientRequestContext, ElicitationFnT
from mcp.client.streamable_http import streamable_http_client
from mcp_types import CallToolResult, ElicitRequestParams, ElicitResult


class WSGITransport(httpx2.AsyncBaseTransport):
    """Serve httpx requests with Django's WSGI application, in a thread."""

    def __init__(self) -> None:
        self._transport = httpx2.WSGITransport(app=WSGIHandler())

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        await request.aread()
        return await anyio.to_thread.run_sync(self._handle, request)

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        response = self._transport.handle_request(request)
        try:
            response.read()
        finally:
            response.close()
        headers = [
            (name, value)
            for name, value in response.headers.items()
            if name.lower() not in ("content-length", "transfer-encoding")
        ]
        return httpx2.Response(
            status_code=response.status_code,
            headers=headers,
            content=response.content,
        )


@asynccontextmanager
async def mcp_client(
    path: str = "/mcp",
    headers: dict[str, str] | None = None,
    mode: Literal["auto", "legacy"] = "auto",
    elicitation_callback: ElicitationFnT | None = None,
) -> AsyncGenerator[Client]:
    async with httpx2.AsyncClient(
        transport=WSGITransport(), headers=headers
    ) as http_client:
        transport = streamable_http_client(
            f"http://testserver{path}", http_client=http_client
        )
        async with Client(
            transport, mode=mode, elicitation_callback=elicitation_callback
        ) as client:
            yield client


class MCPClientTests(SimpleTestCase):
    """
    Test the server with the official MCP Python SDK client, over its
    Streamable HTTP transport, routed to the Django WSGI application.
    """

    def test_list_tools(self):
        async def run():
            async with mcp_client() as client:
                return await client.list_tools()

        result = asyncio.run(run())

        names = [tool.name for tool in result.tools]
        assert names == [
            "add",
            "greet",
            "unavailable",
            "crash",
            "create_widget",
            "noop",
            "unencodable",
            "regional",
            "multiply",
            "add_typed",
            "segment_length",
            "shout",
            "picture",
            "report_link",
            "mixed_content",
            "sig_noop",
            "evens",
        ]
        add = result.tools[0]
        assert add.description == "Add two integers."
        assert add.input_schema["required"] == ["a", "b"]
        assert add.output_schema is not None
        assert add.output_schema["required"] == ["sum"]

    def test_call_tool_structured(self):
        async def run():
            async with mcp_client() as client:
                return await client.call_tool("add", {"a": 20, "b": 22})

        result = asyncio.run(run())

        assert result.is_error is False
        assert result.structured_content == {"sum": 42}

    def test_call_tool_typed_schema(self):
        async def run():
            async with mcp_client() as client:
                return await client.call_tool("multiply", {"a": 6, "b": 7})

        result = asyncio.run(run())

        assert result.is_error is False
        assert result.structured_content == {"product": 42}

    def test_call_tool_text(self):
        async def run():
            async with mcp_client() as client:
                return await client.call_tool("greet", {"name": "Alice"})

        result = asyncio.run(run())

        assert result.is_error is False
        assert result.content[0].text == "Hello, Alice!"

    def test_call_tool_error(self):
        async def run():
            async with mcp_client() as client:
                return await client.call_tool("unavailable")

        result = asyncio.run(run())

        assert result.is_error is True
        assert (
            result.content[0].text
            == "The flux capacitor is offline. Try the DeLorean instead."
        )

    def test_legacy_client(self):
        # The SDK's legacy mode opens with the 2025 initialize handshake.
        async def run():
            async with mcp_client(mode="legacy") as client:
                tools = await client.list_tools()
                result = await client.call_tool("add", {"a": 20, "b": 22})
                return tools, result

        tools, result = asyncio.run(run())

        assert [tool.name for tool in tools.tools][:2] == ["add", "greet"]
        assert result.is_error is False
        assert result.structured_content == {"sum": 42}

    def test_auth(self):
        async def run():
            async with mcp_client(
                "/secure-mcp", headers={"Authorization": "Bearer test-token"}
            ) as client:
                return await client.call_tool("secret_word")

        result = asyncio.run(run())

        assert result.content[0].text == "xyzzy"


class ElicitationClientTests(SimpleTestCase):
    """
    The SDK client answers a tool's questions and retries the call itself,
    which is the whole flow the multi round-trip pattern describes.
    """

    def call(
        self,
        name: str,
        answer: ElicitationFnT | None = None,
        *,
        path: str = "/elicitation-mcp",
        headers: dict[str, str] | None = None,
    ) -> CallToolResult:
        async def run():
            async with mcp_client(
                path, headers=headers, elicitation_callback=answer
            ) as client:
                return await client.call_tool(name)

        return asyncio.run(run())

    def test_question_answered(self):
        asked = []

        async def answer(
            context: ClientRequestContext, params: ElicitRequestParams
        ) -> ElicitResult:
            asked.append(params.message)
            return ElicitResult(action="accept", content={"confirmed": True})

        result = self.call("confirm", answer)

        assert asked == ["Really do the thing?"]
        assert result.is_error is False
        assert result.content[0].text == "Confirmed: True"

    def test_two_questions_answered(self):
        asked = []

        async def answer(
            context: ClientRequestContext, params: ElicitRequestParams
        ) -> ElicitResult:
            asked.append(params.message)
            content: dict[str, Any] = {"confirmed": True}
            if "like" in params.message:
                content = {"flavour": "vanilla", "scoops": 3}
            return ElicitResult(action="accept", content=content)

        result = self.call("order_dessert", answer)

        assert asked == ["What would you like?", "Add a tip?"]
        assert result.structured_content == {
            "flavour": "vanilla",
            "scoops": 3,
            "tip": True,
        }

    def test_question_declined(self):
        async def answer(
            context: ClientRequestContext, params: ElicitRequestParams
        ) -> ElicitResult:
            return ElicitResult(action="decline")

        result = self.call("confirm", answer)

        assert result.is_error is True
        assert result.content[0].text == (
            "The user declined the question: Really do the thing?"
        )

    def test_question_cancelled(self):
        async def answer(
            context: ClientRequestContext, params: ElicitRequestParams
        ) -> ElicitResult:
            return ElicitResult(action="cancel")

        result = self.call("confirm", answer)

        assert result.is_error is True
        assert result.content[0].text == (
            "The user dismissed the question: Really do the thing?"
        )

    def test_invalid_answer_asked_again(self):
        # The server asks again rather than failing, so the SDK handles a
        # second input_required result for the same question.
        asked = []

        async def answer(
            context: ClientRequestContext, params: ElicitRequestParams
        ) -> ElicitResult:
            asked.append(params.message)
            content: dict[str, Any] = {"confirmed": "yes"}
            if len(asked) > 1:
                content = {"confirmed": True}
            return ElicitResult(action="accept", content=content)

        result = self.call("confirm", answer)

        assert asked == ["Really do the thing?", "Really do the thing?"]
        assert result.is_error is False
        assert result.content[0].text == "Confirmed: True"

    def test_question_answered_under_auth(self):
        # The state is bound to the caller, so the SDK must echo it back, and
        # keep sending the caller's headers, for the answer to be accepted.
        asked = []

        async def answer(
            context: ClientRequestContext, params: ElicitRequestParams
        ) -> ElicitResult:
            asked.append(params.message)
            return ElicitResult(action="accept", content={"confirmed": True})

        result = self.call(
            "confirm_as_user",
            answer,
            path="/identified-elicitation-mcp",
            headers={"X-User": "alice"},
        )

        assert asked == ["Really do the thing?"]
        assert result.is_error is False
        assert result.content[0].text == "alice confirmed: True"

    def test_question_answered_without_auth(self):
        async def answer(
            context: ClientRequestContext, params: ElicitRequestParams
        ) -> ElicitResult:
            return ElicitResult(action="accept", content={"confirmed": True})

        result = self.call(
            "confirm_as_user", answer, path="/identified-elicitation-mcp"
        )

        assert result.is_error is False
        assert result.content[0].text == "AnonymousUser confirmed: True"

    def test_client_that_cannot_answer(self):
        # Without a callback the client declares no elicitation capability.
        result = self.call("confirm")

        assert result.is_error is True
        assert result.content[0].text == (
            "This tool needs to ask you a question, which this client does"
            " not support (form mode MCP elicitation)."
        )
