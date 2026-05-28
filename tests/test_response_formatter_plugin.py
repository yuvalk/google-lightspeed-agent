"""Tests for the response formatter plugin."""

from unittest.mock import MagicMock

import pytest
from google.genai import types

from lightspeed_agent.api.a2a.response_formatter_plugin import (
    FIRST_RESPONSE_NOTICE,
    RESPONSE_FOOTER,
    ResponseFormatterPlugin,
)


def _make_event(*, text: str = "Hello", is_final: bool = True, author: str = "agent"):
    """Build a minimal mock Event with text content."""
    part = types.Part(text=text)
    content = types.Content(parts=[part], role="model")

    event = MagicMock()
    event.content = content
    event.author = author
    event.is_final_response.return_value = is_final
    return event


def _make_invocation_context(session_events: list | None = None):
    """Build a mock InvocationContext with a session containing the given events."""
    ctx = MagicMock()
    ctx.session.events = session_events or []
    return ctx


class TestResponseFormatterPluginFirstNotice:
    """Tests for first-response notice injection."""

    @pytest.mark.asyncio
    async def test_prepends_notice_on_first_response(self):
        """The notice should be prepended when no prior agent text exists."""
        plugin = ResponseFormatterPlugin()
        event = _make_event(text="Here is your answer.")
        ctx = _make_invocation_context(session_events=[])

        result = await plugin.on_event_callback(
            invocation_context=ctx, event=event
        )

        assert result is event
        assert result.content.parts[0].text.startswith(FIRST_RESPONSE_NOTICE)
        assert "Here is your answer." in result.content.parts[0].text

    @pytest.mark.asyncio
    async def test_skips_notice_on_subsequent_response(self):
        """The notice should NOT be prepended when prior agent text exists."""
        plugin = ResponseFormatterPlugin()
        event = _make_event(text="Follow-up answer.")

        prior_event = _make_event(text="Previous answer.", author="agent")
        ctx = _make_invocation_context(session_events=[prior_event])

        result = await plugin.on_event_callback(
            invocation_context=ctx, event=event
        )

        assert result is event
        assert not result.content.parts[0].text.startswith(FIRST_RESPONSE_NOTICE)

    @pytest.mark.asyncio
    async def test_skips_non_final_events(self):
        """Non-final events (function calls, partials) should not be modified."""
        plugin = ResponseFormatterPlugin()
        event = _make_event(text="partial text", is_final=False)
        ctx = _make_invocation_context()

        result = await plugin.on_event_callback(
            invocation_context=ctx, event=event
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_skips_events_without_content(self):
        """Events with no content should not be modified."""
        plugin = ResponseFormatterPlugin()
        event = MagicMock()
        event.is_final_response.return_value = True
        event.content = None
        ctx = _make_invocation_context()

        result = await plugin.on_event_callback(
            invocation_context=ctx, event=event
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_skips_events_without_text_parts(self):
        """Events with parts but no text (e.g., function calls) should pass through."""
        plugin = ResponseFormatterPlugin()
        part = types.Part(function_call=types.FunctionCall(name="tool", args={}))
        content = types.Content(parts=[part], role="model")

        event = MagicMock()
        event.is_final_response.return_value = True
        event.content = content
        ctx = _make_invocation_context()

        result = await plugin.on_event_callback(
            invocation_context=ctx, event=event
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_prior_user_events_do_not_count(self):
        """User-authored events should not prevent the first-response notice."""
        plugin = ResponseFormatterPlugin()
        event = _make_event(text="Agent response.")

        user_event = _make_event(text="User question.", author="user")
        ctx = _make_invocation_context(session_events=[user_event])

        result = await plugin.on_event_callback(
            invocation_context=ctx, event=event
        )

        assert result is event
        assert result.content.parts[0].text.startswith(FIRST_RESPONSE_NOTICE)

    @pytest.mark.asyncio
    async def test_prior_agent_events_without_text_do_not_count(self):
        """Agent events without text content should not prevent the notice."""
        plugin = ResponseFormatterPlugin()
        event = _make_event(text="Agent response.")

        # Prior agent event with function call only (no text)
        prior_event = MagicMock()
        prior_event.author = "agent"
        prior_event.content = types.Content(
            parts=[types.Part(function_call=types.FunctionCall(name="t", args={}))],
            role="model",
        )
        ctx = _make_invocation_context(session_events=[prior_event])

        result = await plugin.on_event_callback(
            invocation_context=ctx, event=event
        )

        assert result is event
        assert result.content.parts[0].text.startswith(FIRST_RESPONSE_NOTICE)

    @pytest.mark.asyncio
    async def test_multi_part_event_prepends_to_first_text(self):
        """When an event has multiple parts, notice goes before the first text part."""
        plugin = ResponseFormatterPlugin()
        parts = [
            types.Part(function_call=types.FunctionCall(name="tool", args={})),
            types.Part(text="Actual answer."),
        ]
        content = types.Content(parts=parts, role="model")

        event = MagicMock()
        event.is_final_response.return_value = True
        event.content = content
        ctx = _make_invocation_context(session_events=[])

        result = await plugin.on_event_callback(
            invocation_context=ctx, event=event
        )

        assert result is event
        # First part (function call) should be untouched
        assert result.content.parts[0].function_call is not None
        # Second part (text) should have the notice prepended
        assert result.content.parts[1].text.startswith(FIRST_RESPONSE_NOTICE)
        assert "Actual answer." in result.content.parts[1].text


class TestResponseFormatterPluginFooter:
    """Tests for disclaimer footer injection."""

    @pytest.mark.asyncio
    async def test_appends_footer_on_every_final_response(self):
        """The footer should be appended to every final text response."""
        plugin = ResponseFormatterPlugin()
        event = _make_event(text="Some answer.")

        # Simulate a subsequent response (not first)
        prior_event = _make_event(text="Earlier answer.", author="agent")
        ctx = _make_invocation_context(session_events=[prior_event])

        result = await plugin.on_event_callback(
            invocation_context=ctx, event=event
        )

        assert result is event
        assert result.content.parts[0].text.endswith(RESPONSE_FOOTER)

    @pytest.mark.asyncio
    async def test_footer_present_on_first_response_too(self):
        """The first response should have both the notice and the footer."""
        plugin = ResponseFormatterPlugin()
        event = _make_event(text="Welcome answer.")
        ctx = _make_invocation_context(session_events=[])

        result = await plugin.on_event_callback(
            invocation_context=ctx, event=event
        )

        assert result is event
        text = result.content.parts[0].text
        assert text.startswith(FIRST_RESPONSE_NOTICE)
        assert text.endswith(RESPONSE_FOOTER)

    @pytest.mark.asyncio
    async def test_footer_not_added_to_non_final_events(self):
        """Non-final events should not get the footer."""
        plugin = ResponseFormatterPlugin()
        event = _make_event(text="streaming chunk", is_final=False)
        ctx = _make_invocation_context()

        result = await plugin.on_event_callback(
            invocation_context=ctx, event=event
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_footer_appended_to_last_text_part(self):
        """In multi-part events, the footer goes on the last text part."""
        plugin = ResponseFormatterPlugin()
        parts = [
            types.Part(text="First paragraph."),
            types.Part(text="Second paragraph."),
        ]
        content = types.Content(parts=parts, role="model")

        event = MagicMock()
        event.is_final_response.return_value = True
        event.content = content

        prior_event = _make_event(text="Earlier.", author="agent")
        ctx = _make_invocation_context(session_events=[prior_event])

        result = await plugin.on_event_callback(
            invocation_context=ctx, event=event
        )

        assert result is event
        # First text part should NOT have the footer
        assert not result.content.parts[0].text.endswith(RESPONSE_FOOTER)
        # Last text part should have the footer
        assert result.content.parts[1].text.endswith(RESPONSE_FOOTER)

    @pytest.mark.asyncio
    async def test_footer_contains_disclaimer_text(self):
        """Verify the footer contains the expected disclaimer wording."""
        assert "Always review AI-generated content" in RESPONSE_FOOTER
        assert "---" in RESPONSE_FOOTER
