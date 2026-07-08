"""Provider-agnostic sandbox error recovery (plan D5).

Covers the per-provider recoverability predicate, the predicate-gated
recreation in the ping guard and ToolErrorMiddleware, and the structured
error_class/sandbox_id circuit-breaker markers with UUID sandbox ids.

The four pre-existing langsmith tests in test_sandbox_recovery.py are the
zero-delta proof for the langsmith path and must stay green unmodified.
"""

from __future__ import annotations

import json
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langsmith.sandbox import SandboxClientError

from agent.middleware.sandbox_circuit_breaker import SandboxCircuitBreakerMiddleware
from agent.middleware.tool_error_handler import ToolErrorMiddleware
from agent.utils.sandbox import get_recoverable_predicate
from agent.utils.sandbox_state import clear_sandbox_backend, set_sandbox_backend

UUID_OLD = "0f0e0d0c-1111-4222-8333-444455556666"
UUID_NEW = "9a9b9c9d-7777-4888-9999-000011112222"


# --------------------------------------------------------------------------- #
# Fake opensandbox SDK tree (only what agent.integrations.opensandbox imports) #
# --------------------------------------------------------------------------- #
class _FakeSandboxException(Exception):
    pass


class _FakeSandboxApiException(_FakeSandboxException):
    def __init__(self, message="", status_code=None):
        super().__init__(message)
        self.status_code = status_code


class _FakeSandboxInternalException(_FakeSandboxException):
    pass


def _install_opensandbox_fakes(monkeypatch):
    modules = {
        "opensandbox": types.ModuleType("opensandbox"),
        "opensandbox.sync": types.ModuleType("opensandbox.sync"),
        "opensandbox.models": types.ModuleType("opensandbox.models"),
        "opensandbox.models.execd": types.ModuleType("opensandbox.models.execd"),
        "opensandbox.models.filesystem": types.ModuleType("opensandbox.models.filesystem"),
        "opensandbox.config": types.ModuleType("opensandbox.config"),
        "opensandbox.config.connection_sync": types.ModuleType(
            "opensandbox.config.connection_sync"
        ),
        "opensandbox.exceptions": types.ModuleType("opensandbox.exceptions"),
    }
    modules["opensandbox.sync"].SandboxSync = type("SandboxSync", (), {})
    modules["opensandbox.models.execd"].RunCommandOpts = type("RunCommandOpts", (), {})
    modules["opensandbox.models.filesystem"].WriteEntry = type("WriteEntry", (), {})
    modules["opensandbox.config.connection_sync"].ConnectionConfigSync = type(
        "ConnectionConfigSync", (), {}
    )
    modules["opensandbox.exceptions"].SandboxException = _FakeSandboxException
    modules["opensandbox.exceptions"].SandboxApiException = _FakeSandboxApiException
    modules["opensandbox.exceptions"].SandboxInternalException = _FakeSandboxInternalException
    modules["opensandbox.exceptions"].SandboxReadyTimeoutException = type(
        "SandboxReadyTimeoutException", (_FakeSandboxException,), {}
    )
    modules["opensandbox.exceptions"].SandboxUnhealthyException = type(
        "SandboxUnhealthyException", (_FakeSandboxException,), {}
    )
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)


@pytest.fixture
def opensandbox_env(monkeypatch):
    """SANDBOX_TYPE=opensandbox with a freshly imported integration module."""
    _install_opensandbox_fakes(monkeypatch)
    monkeypatch.setenv("SANDBOX_TYPE", "opensandbox")
    sys.modules.pop("agent.integrations.opensandbox", None)
    yield
    sys.modules.pop("agent.integrations.opensandbox", None)


def _tool_request(thread_id: str = "thread-1") -> ToolCallRequest:
    runtime = MagicMock(config={"configurable": {"thread_id": thread_id}})
    return ToolCallRequest(
        tool_call={"name": "ls", "args": {"path": "/"}, "id": "tc1"},
        tool=MagicMock(),
        state={},
        runtime=runtime,
    )


# --------------------------------------------------------------------------- #
# get_recoverable_predicate dispatch                                          #
# --------------------------------------------------------------------------- #
def test_predicate_langsmith_matches_client_error(monkeypatch):
    monkeypatch.delenv("SANDBOX_TYPE", raising=False)

    predicate = get_recoverable_predicate()

    assert predicate(SandboxClientError("timeout")) is True
    assert predicate(TypeError("bug")) is False


def test_predicate_opensandbox_status_codes(opensandbox_env):
    predicate = get_recoverable_predicate()

    assert predicate(_FakeSandboxApiException("gone", status_code=404)) is True
    assert predicate(_FakeSandboxApiException("boom", status_code=503)) is True
    assert predicate(_FakeSandboxInternalException("net down")) is True
    assert predicate(_FakeSandboxApiException("auth", status_code=401)) is False
    assert predicate(TypeError("bug")) is False


def test_predicate_unknown_provider_never_recreates(monkeypatch):
    monkeypatch.setenv("SANDBOX_TYPE", "modal")

    predicate = get_recoverable_predicate()

    assert predicate(SandboxClientError("timeout")) is False
    assert predicate(TypeError("bug")) is False


# --------------------------------------------------------------------------- #
# ToolErrorMiddleware: predicate-gated recreation + structured payloads        #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_opensandbox_recoverable_error_recreates_sandbox(opensandbox_env) -> None:
    middleware = ToolErrorMiddleware()
    request = _tool_request()
    old_backend = MagicMock(id=UUID_OLD)
    new_backend = MagicMock(id=UUID_NEW)
    set_sandbox_backend("thread-1", old_backend)

    async def handler(_request: ToolCallRequest) -> ToolMessage:
        raise _FakeSandboxApiException("sandbox gone", status_code=404)

    try:
        with (
            patch("agent.server._recreate_sandbox", new_callable=AsyncMock) as mock_recreate,
            patch("agent.server.client") as mock_client,
        ):
            mock_recreate.return_value = new_backend
            mock_client.threads.update = AsyncMock()

            result = await middleware.awrap_tool_call(request, handler)

        assert isinstance(result, ToolMessage)
        mock_recreate.assert_awaited_once_with("thread-1")
        payload = json.loads(result.content)
        assert payload["status"] == "error"
        assert payload["recovery"] == "sandbox_recreated_after_client_error"
        assert payload["error_class"] == "sandbox_unreachable"
        assert payload["sandbox_id"] == UUID_NEW
        assert payload["error_type"] == "_FakeSandboxApiException"
    finally:
        clear_sandbox_backend("thread-1")


@pytest.mark.asyncio
async def test_opensandbox_auth_error_does_not_recreate(opensandbox_env) -> None:
    middleware = ToolErrorMiddleware()
    request = _tool_request()

    async def handler(_request: ToolCallRequest) -> ToolMessage:
        raise _FakeSandboxApiException("bad credentials", status_code=401)

    with patch("agent.server._recreate_sandbox", new_callable=AsyncMock) as mock_recreate:
        result = await middleware.awrap_tool_call(request, handler)

    mock_recreate.assert_not_awaited()
    payload = json.loads(result.content)
    assert payload["status"] == "error"
    assert "recovery" not in payload
    assert "error_class" not in payload


@pytest.mark.asyncio
async def test_type_error_never_recreates(opensandbox_env) -> None:
    middleware = ToolErrorMiddleware()
    request = _tool_request()

    async def handler(_request: ToolCallRequest) -> ToolMessage:
        raise TypeError("plain bug")

    with patch("agent.server._recreate_sandbox", new_callable=AsyncMock) as mock_recreate:
        result = await middleware.awrap_tool_call(request, handler)

    mock_recreate.assert_not_awaited()
    payload = json.loads(result.content)
    assert payload["error_type"] == "TypeError"
    assert "recovery" not in payload
    assert "error_class" not in payload


@pytest.mark.asyncio
async def test_recreate_failure_emits_structured_unreachable_payload(monkeypatch) -> None:
    monkeypatch.delenv("SANDBOX_TYPE", raising=False)
    middleware = ToolErrorMiddleware()
    request = _tool_request()
    dead_backend = MagicMock(id="sb-dead")
    set_sandbox_backend("thread-1", dead_backend)

    async def handler(_request: ToolCallRequest) -> ToolMessage:
        raise SandboxClientError("Sandbox request timed out: sb-dead")

    try:
        with patch(
            "agent.server._recreate_sandbox",
            new_callable=AsyncMock,
            side_effect=RuntimeError("provider down"),
        ):
            result = await middleware.awrap_tool_call(request, handler)

        payload = json.loads(result.content)
        assert payload["status"] == "error"
        assert payload["error_class"] == "sandbox_unreachable"
        assert payload["sandbox_id"] == "sb-dead"
        assert "recovery" not in payload
    finally:
        clear_sandbox_backend("thread-1")


# --------------------------------------------------------------------------- #
# Ping guard (check_or_recreate_sandbox)                                       #
# --------------------------------------------------------------------------- #
class _PingBackend:
    id = UUID_OLD

    def __init__(self, error: Exception | None = None):
        self._error = error

    def execute(self, command: str, *, timeout: int | None = None):
        if self._error is not None:
            raise self._error
        return MagicMock(output="ok", exit_code=0)


@pytest.mark.asyncio
async def test_ping_guard_recreates_on_opensandbox_error(opensandbox_env) -> None:
    replacement = MagicMock(id=UUID_NEW)
    backend = _PingBackend(_FakeSandboxApiException("gone", status_code=404))

    with patch(
        "agent.server._recreate_sandbox", new_callable=AsyncMock, return_value=replacement
    ) as mock_recreate:
        from agent.server import check_or_recreate_sandbox

        result = await check_or_recreate_sandbox(backend, "thread-1")

    assert result is replacement
    mock_recreate.assert_awaited_once()


@pytest.mark.asyncio
async def test_ping_guard_propagates_non_sandbox_error(opensandbox_env) -> None:
    backend = _PingBackend(TypeError("plain bug"))

    with patch("agent.server._recreate_sandbox", new_callable=AsyncMock) as mock_recreate:
        from agent.server import check_or_recreate_sandbox

        with pytest.raises(TypeError, match="plain bug"):
            await check_or_recreate_sandbox(backend, "thread-1")

    mock_recreate.assert_not_awaited()


@pytest.mark.asyncio
async def test_ping_guard_langsmith_client_error_still_recreates(monkeypatch) -> None:
    monkeypatch.delenv("SANDBOX_TYPE", raising=False)
    replacement = MagicMock(id="sb-new")
    backend = _PingBackend(SandboxClientError("unreachable"))

    with patch(
        "agent.server._recreate_sandbox", new_callable=AsyncMock, return_value=replacement
    ) as mock_recreate:
        from agent.server import check_or_recreate_sandbox

        result = await check_or_recreate_sandbox(backend, "thread-1")

    assert result is replacement
    mock_recreate.assert_awaited_once()


# --------------------------------------------------------------------------- #
# Circuit breaker: structured markers with UUID sandbox ids                    #
# --------------------------------------------------------------------------- #
def _structured_unreachable_message(tool_call_id: str, sandbox_id: str) -> ToolMessage:
    return ToolMessage(
        content=json.dumps(
            {
                "error": "Sandbox request failed",
                "error_type": "SandboxApiException",
                "error_class": "sandbox_unreachable",
                "sandbox_id": sandbox_id,
                "status": "error",
            }
        ),
        tool_call_id=tool_call_id,
        status="error",
    )


def test_circuit_breaker_trips_on_structured_uuid_failures() -> None:
    middleware = SandboxCircuitBreakerMiddleware(threshold=2)
    messages = [
        HumanMessage(content="please fix this"),
        AIMessage(content="", tool_calls=[{"name": "ls", "args": {}, "id": "tc1"}]),
        _structured_unreachable_message("tc1", UUID_OLD),
        AIMessage(content="", tool_calls=[{"name": "grep", "args": {}, "id": "tc2"}]),
        _structured_unreachable_message("tc2", UUID_OLD),
        AIMessage(content="", tool_calls=[{"name": "execute", "args": {}, "id": "tc3"}]),
        _structured_unreachable_message("tc3", UUID_OLD),
    ]

    result = middleware.before_model({"messages": messages}, MagicMock())

    assert result is not None
    assert result["jump_to"] == "end"
    assert "Sandbox circuit breaker triggered" in result["messages"][0].content
    assert UUID_OLD in result["messages"][0].content


def test_circuit_breaker_below_threshold_does_not_trip() -> None:
    middleware = SandboxCircuitBreakerMiddleware(threshold=2)
    messages = [
        HumanMessage(content="please fix this"),
        AIMessage(content="", tool_calls=[{"name": "ls", "args": {}, "id": "tc1"}]),
        _structured_unreachable_message("tc1", UUID_OLD),
    ]

    result = middleware.before_model({"messages": messages}, MagicMock())

    assert result is None


def test_circuit_breaker_trips_on_structured_uuid_recreations() -> None:
    middleware = SandboxCircuitBreakerMiddleware(threshold=2)

    def recreated(tool_call_id: str, sandbox_id: str) -> ToolMessage:
        return ToolMessage(
            content=json.dumps(
                {
                    "error_type": "SandboxInternalException",
                    "error_class": "sandbox_unreachable",
                    "previous_error": "net down",
                    "recovery": "sandbox_recreated_after_client_error",
                    "sandbox_id": sandbox_id,
                    "status": "error",
                }
            ),
            tool_call_id=tool_call_id,
            status="error",
        )

    messages = [
        HumanMessage(content="please fix this"),
        AIMessage(content="", tool_calls=[{"name": "ls", "args": {}, "id": "tc1"}]),
        recreated("tc1", "11111111-2222-4333-8444-555566667777"),
        AIMessage(content="", tool_calls=[{"name": "grep", "args": {}, "id": "tc2"}]),
        recreated("tc2", "22222222-3333-4444-8555-666677778888"),
        AIMessage(content="", tool_calls=[{"name": "execute", "args": {}, "id": "tc3"}]),
        recreated("tc3", "33333333-4444-4555-8666-777788889999"),
    ]

    result = middleware.before_model({"messages": messages}, MagicMock())

    assert result is not None
    assert result["jump_to"] == "end"
    assert "consecutive sandbox recreations" in result["messages"][0].content


# --------------------------------------------------------------------------- #
# Review fixes: refresh/reconnect gating, predicate hardening, JSON safety     #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_refresh_failure_nonrecoverable_keeps_sandbox(opensandbox_env) -> None:
    """An auth-refresh failure on a healthy sandbox must not destroy the workspace."""
    backend = MagicMock(id=UUID_OLD)

    with (
        patch(
            "agent.server._refresh_github_proxy",
            new_callable=AsyncMock,
            side_effect=RuntimeError("hosts.yml write failed (exit code 1)"),
        ),
        patch("agent.server._recreate_sandbox", new_callable=AsyncMock) as mock_recreate,
    ):
        from agent.server import _refresh_github_proxy_or_recreate

        result = await _refresh_github_proxy_or_recreate(backend, "thread-1")

    assert result is backend
    mock_recreate.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_failure_recoverable_recreates(opensandbox_env) -> None:
    backend = MagicMock(id=UUID_OLD)
    replacement = MagicMock(id=UUID_NEW)

    with (
        patch(
            "agent.server._refresh_github_proxy",
            new_callable=AsyncMock,
            side_effect=_FakeSandboxApiException("gone", status_code=503),
        ),
        patch(
            "agent.server._recreate_sandbox", new_callable=AsyncMock, return_value=replacement
        ) as mock_recreate,
    ):
        from agent.server import _refresh_github_proxy_or_recreate

        result = await _refresh_github_proxy_or_recreate(backend, "thread-1")

    assert result is replacement
    mock_recreate.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconnect_failure_nonrecoverable_reraises_without_metadata_reset(
    opensandbox_env,
) -> None:
    """A config bug on reconnect must not orphan a live sandbox's metadata."""
    with (
        patch(
            "agent.server.get_sandbox_id_from_metadata",
            new_callable=AsyncMock,
            return_value="uuid-existing",
        ),
        patch(
            "agent.server.create_sandbox",
            side_effect=ValueError("OPEN_SANDBOX_TTL_SECONDS must be an integer"),
        ),
        patch(
            "agent.server._create_sandbox_with_proxy", new_callable=AsyncMock
        ) as mock_create_proxy,
        patch("agent.server.client") as mock_client,
        patch.dict("agent.server.SANDBOX_BACKENDS", {}, clear=True),
    ):
        mock_client.threads.update = AsyncMock()

        from agent.server import ensure_sandbox_for_thread

        with pytest.raises(ValueError, match="OPEN_SANDBOX_TTL_SECONDS"):
            await ensure_sandbox_for_thread("thread-1")

        mock_create_proxy.assert_not_awaited()
        mock_client.threads.update.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconnect_failure_recoverable_creates_replacement(opensandbox_env) -> None:
    replacement = MagicMock(id=UUID_NEW)

    with (
        patch(
            "agent.server.get_sandbox_id_from_metadata",
            new_callable=AsyncMock,
            return_value="uuid-existing",
        ),
        patch(
            "agent.server.create_sandbox",
            side_effect=_FakeSandboxApiException("expired", status_code=404),
        ),
        patch(
            "agent.server._create_sandbox_with_proxy",
            new_callable=AsyncMock,
            return_value=replacement,
        ) as mock_create_proxy,
        patch("agent.server._configure_git_identity", new_callable=AsyncMock),
        patch("agent.server.client") as mock_client,
        patch.dict("agent.server.SANDBOX_BACKENDS", {}, clear=True),
    ):
        mock_client.threads.update = AsyncMock()

        from agent.server import ensure_sandbox_for_thread

        result = await ensure_sandbox_for_thread("thread-1")

    assert result.id == UUID_NEW
    mock_create_proxy.assert_awaited_once()


@pytest.mark.asyncio
async def test_broken_predicate_falls_back_to_generic_error() -> None:
    """A predicate that itself raises must fail closed to the generic path."""
    middleware = ToolErrorMiddleware()
    request = _tool_request()

    async def handler(_request: ToolCallRequest) -> ToolMessage:
        raise RuntimeError("tool blew up")

    with (
        patch(
            "agent.middleware.tool_error_handler.get_recoverable_predicate",
            side_effect=RuntimeError("predicate import failed"),
        ),
        patch("agent.server._recreate_sandbox", new_callable=AsyncMock) as mock_recreate,
    ):
        result = await middleware.awrap_tool_call(request, handler)

    mock_recreate.assert_not_awaited()
    payload = json.loads(result.content)
    assert payload["error_type"] == "RuntimeError"
    assert payload["error"] == "tool blew up"
    assert "recovery" not in payload


@pytest.mark.asyncio
async def test_ping_guard_broken_predicate_reraises_original_error() -> None:
    backend = _PingBackend(SandboxClientError("unreachable"))

    with (
        patch(
            "agent.server.get_recoverable_predicate",
            side_effect=RuntimeError("predicate import failed"),
        ),
        patch("agent.server._recreate_sandbox", new_callable=AsyncMock) as mock_recreate,
    ):
        from agent.server import check_or_recreate_sandbox

        with pytest.raises(SandboxClientError, match="unreachable"):
            await check_or_recreate_sandbox(backend, "thread-1")

    mock_recreate.assert_not_awaited()


def test_circuit_breaker_survives_pathological_json() -> None:
    deep = "[" * 100000 + "]" * 100000
    middleware = SandboxCircuitBreakerMiddleware(threshold=2)
    messages = [
        HumanMessage(content="please fix this"),
        AIMessage(content="", tool_calls=[{"name": "ls", "args": {}, "id": "tc1"}]),
        ToolMessage(content=deep, tool_call_id="tc1", status="error"),
    ]

    assert middleware.before_model({"messages": messages}, MagicMock()) is None


@pytest.mark.asyncio
async def test_reconnect_failure_legacy_provider_still_self_heals(monkeypatch) -> None:
    """Providers without a recoverability predicate keep baseline reconnect self-healing."""
    monkeypatch.setenv("SANDBOX_TYPE", "modal")
    replacement = MagicMock(id="modal-new")

    with (
        patch(
            "agent.server.get_sandbox_id_from_metadata",
            new_callable=AsyncMock,
            return_value="modal-existing",
        ),
        patch("agent.server.create_sandbox", side_effect=RuntimeError("sandbox deleted")),
        patch(
            "agent.server._create_sandbox_with_proxy",
            new_callable=AsyncMock,
            return_value=replacement,
        ) as mock_create_proxy,
        patch("agent.server._configure_git_identity", new_callable=AsyncMock),
        patch("agent.server.client") as mock_client,
        patch.dict("agent.server.SANDBOX_BACKENDS", {}, clear=True),
    ):
        mock_client.threads.update = AsyncMock()

        from agent.server import ensure_sandbox_for_thread

        result = await ensure_sandbox_for_thread("thread-1")

    assert result.id == "modal-new"
    mock_create_proxy.assert_awaited_once()


def test_circuit_breaker_trips_on_structured_failures_without_id() -> None:
    """Process-restart scenarios can leave no sandbox_id; the breaker must still trip."""
    middleware = SandboxCircuitBreakerMiddleware(threshold=2)

    def unreachable_without_id(tool_call_id: str) -> ToolMessage:
        return ToolMessage(
            content=json.dumps(
                {
                    "error": "Sandbox request failed",
                    "error_type": "SandboxInternalException",
                    "error_class": "sandbox_unreachable",
                    "status": "error",
                }
            ),
            tool_call_id=tool_call_id,
            status="error",
        )

    messages = [
        HumanMessage(content="please fix this"),
        AIMessage(content="", tool_calls=[{"name": "ls", "args": {}, "id": "tc1"}]),
        unreachable_without_id("tc1"),
        AIMessage(content="", tool_calls=[{"name": "grep", "args": {}, "id": "tc2"}]),
        unreachable_without_id("tc2"),
        AIMessage(content="", tool_calls=[{"name": "execute", "args": {}, "id": "tc3"}]),
        unreachable_without_id("tc3"),
    ]

    result = middleware.before_model({"messages": messages}, MagicMock())

    assert result is not None
    assert result["jump_to"] == "end"
    assert "Sandbox circuit breaker triggered" in result["messages"][0].content
