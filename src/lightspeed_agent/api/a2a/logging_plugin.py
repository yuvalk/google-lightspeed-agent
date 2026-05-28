"""Agent execution logging plugin with audit context.

Logs agent lifecycle events (tool calls, LLM invocations, run start/end)
at INFO level for operational visibility. Each log entry includes an
``event_type`` classification and audit context (user_id, org_id,
order_id, request_id) for data lineage tracing.

Controlled by the AGENT_LOGGING_DETAIL setting:
  - "basic": logs tool names, token counts, and lifecycle events
  - "detailed": also logs tool arguments and truncated results
"""

import logging
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from lightspeed_agent.auth.middleware import (
    get_request_id,
    get_request_order_id,
    get_request_org_id,
    get_request_user_id,
)
from lightspeed_agent.config import get_settings

logger = logging.getLogger(__name__)

_MAX_RESULT_LENGTH = 500


def _truncate(value: Any, max_length: int = _MAX_RESULT_LENGTH) -> str:
    """Return a string representation truncated to *max_length* characters."""
    text = str(value)
    if len(text) <= max_length:
        return text
    return text[:max_length] + "...(truncated)"


class AgentLoggingPlugin(BasePlugin):
    """ADK plugin that logs agent execution events with audit context."""

    def __init__(self) -> None:
        super().__init__(name="agent_logging")

    def _is_detailed(self) -> bool:
        return get_settings().agent_logging_detail == "detailed"

    @staticmethod
    def _audit_fields() -> str:
        """Build audit context string for log messages."""
        return (
            f"user_id={get_request_user_id()}, "
            f"org_id={get_request_org_id()}, "
            f"order_id={get_request_order_id()}, "
            f"request_id={get_request_id()}"
        )

    # -- run lifecycle --------------------------------------------------------

    async def before_run_callback(
        self, *, invocation_context: InvocationContext
    ) -> None:
        logger.info(
            "Agent run started "
            "(event_type=agent_run_started, invocation_id=%s, agent=%s, %s)",
            invocation_context.invocation_id,
            invocation_context.agent.name,
            self._audit_fields(),
        )
        return None

    async def after_run_callback(
        self, *, invocation_context: InvocationContext
    ) -> None:
        logger.info(
            "Agent run completed "
            "(event_type=agent_run_completed, invocation_id=%s, %s)",
            invocation_context.invocation_id,
            self._audit_fields(),
        )
        return None

    # -- model callbacks ------------------------------------------------------

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> Any | None:
        logger.info(
            "LLM call started (event_type=llm_call_started, agent=%s, %s)",
            callback_context.agent_name,
            self._audit_fields(),
        )
        return None

    async def after_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_response: LlmResponse,
    ) -> LlmResponse | None:
        input_tokens = 0
        output_tokens = 0
        if llm_response and llm_response.usage_metadata:
            usage = llm_response.usage_metadata
            input_tokens = getattr(usage, "prompt_token_count", 0) or 0
            output_tokens = getattr(usage, "candidates_token_count", 0) or 0

        model_version = llm_response.model_version if llm_response else None

        logger.info(
            "LLM call completed "
            "(event_type=llm_call_completed, input_tokens=%d, output_tokens=%d, "
            "model=%s, %s)",
            input_tokens,
            output_tokens,
            model_version,
            self._audit_fields(),
        )
        return None

    async def on_model_error_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest, error: Exception
    ) -> LlmResponse | None:
        logger.error(
            "LLM call failed (event_type=llm_call_failed, error=%s, %s)",
            error,
            self._audit_fields(),
        )
        return None

    # -- tool callbacks -------------------------------------------------------

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> Any | None:
        tool_name = getattr(tool, "name", type(tool).__name__)
        if self._is_detailed():
            logger.info(
                "Tool call started "
                "(event_type=tool_call_started, tool=%s, args=%s, %s)",
                tool_name,
                _truncate(tool_args),
                self._audit_fields(),
            )
        else:
            logger.info(
                "Tool call started "
                "(event_type=tool_call_started, tool=%s, %s)",
                tool_name,
                self._audit_fields(),
            )
        return None

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict[str, Any],
    ) -> dict[str, Any] | None:
        tool_name = getattr(tool, "name", type(tool).__name__)
        if self._is_detailed():
            logger.info(
                "Tool call completed "
                "(event_type=tool_call_completed, tool=%s, data_source=%s, "
                "result=%s, %s)",
                tool_name,
                tool_name,
                _truncate(result),
                self._audit_fields(),
            )
        else:
            logger.info(
                "Tool call completed "
                "(event_type=tool_call_completed, tool=%s, data_source=%s, %s)",
                tool_name,
                tool_name,
                self._audit_fields(),
            )
        return None

    async def on_tool_error_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        error: Exception,
    ) -> dict[str, Any] | None:
        tool_name = getattr(tool, "name", type(tool).__name__)
        logger.error(
            "Tool call failed "
            "(event_type=tool_call_failed, tool=%s, error=%s, %s)",
            tool_name,
            error,
            self._audit_fields(),
        )
        return None
