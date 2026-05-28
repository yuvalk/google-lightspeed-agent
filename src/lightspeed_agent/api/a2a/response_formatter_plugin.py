"""Response formatter plugin.

Injects the first-response legal notice and the AI-content disclaimer
footer at the application layer so the LLM does not need to track
conversation state or remember to include verbatim boilerplate.

- The notice is prepended to the first final text response in each session.
- The footer is appended to every final text response.
"""

import logging

from google.adk.agents.invocation_context import InvocationContext
from google.adk.events.event import Event
from google.adk.plugins.base_plugin import BasePlugin

logger = logging.getLogger(__name__)

FIRST_RESPONSE_NOTICE = (
    "You are interacting with the Red Hat Lightspeed Agent, which can answer questions "
    "about your Red Hat account, subscription, system configuration, and related details. "
    "This feature uses AI technology. Interactions may be used to improve Red Hat's "
    "products or services.\n\n"
    "Always review AI-generated content prior to use.\n\n"
)

RESPONSE_FOOTER = "\n\n---\n*Always review AI-generated content prior to use.*"


class ResponseFormatterPlugin(BasePlugin):
    """ADK plugin that injects the first-response notice and disclaimer footer."""

    def __init__(self) -> None:
        super().__init__(name="response_formatter")

    async def on_event_callback(
        self, *, invocation_context: InvocationContext, event: Event
    ) -> Event | None:
        """Inject the first-response notice and disclaimer footer."""
        if not event.is_final_response():
            return None

        if not event.content or not event.content.parts:
            return None

        # Locate the first and last text parts
        first_text_idx: int | None = None
        last_text_idx: int | None = None
        for i, part in enumerate(event.content.parts):
            if part.text:
                if first_text_idx is None:
                    first_text_idx = i
                last_text_idx = i

        if first_text_idx is None or last_text_idx is None:
            return None

        # Prepend first-response notice when this is a new session
        if self._is_first_agent_response(invocation_context.session.events):
            first_text = event.content.parts[first_text_idx].text or ""
            event.content.parts[first_text_idx].text = (
                FIRST_RESPONSE_NOTICE + first_text
            )
            logger.debug("Prepended first-response notice to agent response")

        # Append disclaimer footer to every final response
        last_text = event.content.parts[last_text_idx].text or ""
        event.content.parts[last_text_idx].text = last_text + RESPONSE_FOOTER

        return event

    @staticmethod
    def _is_first_agent_response(session_events: list[Event]) -> bool:
        """Return True when no prior agent event in the session contains text."""
        for ev in session_events:
            if ev.author == "user":
                continue
            if ev.content and ev.content.parts:
                for part in ev.content.parts:
                    if part.text:
                        return False
        return True
