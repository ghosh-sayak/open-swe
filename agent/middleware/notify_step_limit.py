"""After-agent middleware that notifies users when the step limit is reached."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from langchain.agents.middleware import AgentState, after_agent
from langgraph.config import get_config
from langgraph.runtime import Runtime

from ..utils.source_notify import post_source_notification

logger = logging.getLogger(__name__)

_LIMIT_MARKER = "Model call limits exceeded"


def _content_to_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)

    parts: list[str] = []
    for block in content:
        if isinstance(block, Mapping):
            text = block.get("text", "")
            parts.append(text if isinstance(text, str) else str(text))
        else:
            parts.append(str(block))
    return " ".join(parts)


@after_agent
async def notify_step_limit_reached(
    state: AgentState,
    runtime: Runtime,
) -> dict[str, Any] | None:
    """Notify the user on the GitHub source when the agent hits its step limit.

    Runs after the agent exits. Checks whether the last AI message contains
    the ``ModelCallLimitMiddleware`` marker text; if so, posts a comment on the
    triggering GitHub PR/issue so the user is not left wondering what happened.
    Dashboard-triggered runs surface this in the dashboard.
    """
    messages = state.get("messages", [])
    if not messages:
        return None

    last_msg = messages[-1]
    content = _content_to_text(getattr(last_msg, "content", "") or "")

    if _LIMIT_MARKER not in content:
        return None

    message = (
        "I've reached my maximum step limit and had to stop. "
        "The task may be incomplete. You can retry with a more focused request, "
        "or ask me to continue from where I left off."
    )

    try:
        await post_source_notification(get_config(), message)
    except Exception:
        logger.exception("Failed to send step-limit notification")

    return None
