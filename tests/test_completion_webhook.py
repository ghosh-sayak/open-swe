from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from agent import completion


class _FakeThreads:
    def __init__(self, metadata: dict[str, Any]) -> None:
        self._metadata = metadata
        self.updates: list[dict[str, Any]] = []

    async def get(self, thread_id: str) -> dict[str, Any]:
        return {"thread_id": thread_id, "metadata": self._metadata}

    async def update(self, *, thread_id: str, metadata: dict[str, Any]) -> None:
        self.updates.append(metadata)


class _FakeClient:
    def __init__(self, metadata: dict[str, Any]) -> None:
        self.threads = _FakeThreads(metadata)


def _github_metadata() -> dict[str, Any]:
    return {
        "source": "github",
        "repo": {"owner": "langchain-ai", "name": "open-swe"},
        "source_context": {"pr_number": 7},
    }


def _patch_github(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    monkeypatch.setattr(
        completion,
        "get_github_app_installation_token",
        AsyncMock(return_value="ghs_tok"),
    )
    reply = AsyncMock(return_value=True)
    monkeypatch.setattr(completion, "post_github_comment", reply)
    return reply


@pytest.mark.asyncio
async def test_error_status_posts_github_failure_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(_github_metadata())
    monkeypatch.setattr(completion, "langgraph_client", lambda: client)
    reply = _patch_github(monkeypatch)

    result = await completion.handle_run_completion({"thread_id": "t1", "status": "error"})

    assert result["status"] == "ok"
    reply.assert_awaited_once()
    args = reply.await_args.args
    assert args[0] == {"owner": "langchain-ai", "name": "open-swe"}
    assert args[1] == 7
    assert client.threads.updates == [{"failure_reply_posted": True}]


@pytest.mark.asyncio
async def test_success_status_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(_github_metadata())
    monkeypatch.setattr(completion, "langgraph_client", lambda: client)
    reply = _patch_github(monkeypatch)

    result = await completion.handle_run_completion({"thread_id": "t1", "status": "success"})

    assert result["status"] == "ignored"
    reply.assert_not_called()


@pytest.mark.asyncio
async def test_idempotent_when_already_replied(monkeypatch: pytest.MonkeyPatch) -> None:
    metadata = _github_metadata()
    metadata["failure_reply_posted"] = True
    client = _FakeClient(metadata)
    monkeypatch.setattr(completion, "langgraph_client", lambda: client)
    reply = _patch_github(monkeypatch)

    result = await completion.handle_run_completion({"thread_id": "t1", "status": "timeout"})

    assert result["status"] == "ignored"
    reply.assert_not_called()
    assert client.threads.updates == []


@pytest.mark.asyncio
async def test_missing_thread_id_is_ignored() -> None:
    result = await completion.handle_run_completion({"status": "error"})
    assert result["status"] == "ignored"


@pytest.mark.asyncio
async def test_no_reply_channel_does_not_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient({"source": "schedule"})
    monkeypatch.setattr(completion, "langgraph_client", lambda: client)

    result = await completion.handle_run_completion({"thread_id": "t1", "status": "error"})

    assert result["status"] == "ignored"
    assert client.threads.updates == []


@pytest.mark.asyncio
async def test_interrupted_status_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    # Follow-ups use multitask_strategy="interrupt", so an interrupted run is a
    # healthy hand-off, not a failure to report.
    client = _FakeClient(_github_metadata())
    monkeypatch.setattr(completion, "langgraph_client", lambda: client)
    reply = _patch_github(monkeypatch)

    result = await completion.handle_run_completion({"thread_id": "t1", "status": "interrupted"})

    assert result["status"] == "ignored"
    reply.assert_not_called()
    assert client.threads.updates == []


def test_verify_run_complete_token(monkeypatch: pytest.MonkeyPatch) -> None:
    # No secret configured: fail closed (reject everything).
    monkeypatch.setattr(completion, "RUN_COMPLETE_WEBHOOK_SECRET", None)
    assert completion.verify_run_complete_token(None) is False
    assert completion.verify_run_complete_token("whatever") is False

    # Secret configured: require an exact match.
    monkeypatch.setattr(completion, "RUN_COMPLETE_WEBHOOK_SECRET", "s3cret")
    assert completion.verify_run_complete_token("s3cret") is True
    assert completion.verify_run_complete_token("wrong") is False
    assert completion.verify_run_complete_token(None) is False
