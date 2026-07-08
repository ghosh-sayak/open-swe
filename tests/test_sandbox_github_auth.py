"""Tests for the shared hosts.yml GitHub auth helper + its server/analyzer wiring (plan D3)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from deepagents.backends.protocol import ExecuteResponse

from agent.utils.sandbox_github_auth import configure_github_auth


class _FakeBackend:
    """Minimal sandbox backend capturing execute() calls; no renew_ttl."""

    id = "uuid-fake"

    def __init__(self, exit_code: int = 0):
        self.exit_code = exit_code
        self.commands: list[str] = []

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        self.commands.append(command)
        return ExecuteResponse(output="", exit_code=self.exit_code)


class _RenewableFakeBackend(_FakeBackend):
    def __init__(self, exit_code: int = 0):
        super().__init__(exit_code)
        self.renew_calls = 0

    def renew_ttl(self) -> None:
        self.renew_calls += 1


class TestConfigureGithubAuth:
    def test_writes_insteadof_and_hosts_yml(self) -> None:
        backend = _FakeBackend()

        configure_github_auth(backend, "ghs_token123")

        assert len(backend.commands) == 1
        command = backend.commands[0]
        parts = command.split(" && ")
        assert len(parts) == 4
        # Rotation safety: stale x-access-token url sections are removed first,
        # otherwise git resolves insteadOf to the FIRST (expired) section forever.
        assert "--get-regexp" in parts[0]
        assert "x-access-token" in parts[0]
        assert "--remove-section" in parts[0]
        assert (
            "git config --global "
            "url.'https://x-access-token:ghs_token123@github.com/'.insteadOf "
            "'https://github.com/'"
        ) == parts[1]
        assert parts[2] == "mkdir -p /root/.config/gh"
        assert "oauth_token: ghs_token123" in parts[3]
        assert "user: x-access-token" in parts[3]
        assert "git_protocol: https" in parts[3]
        assert "> /root/.config/gh/hosts.yml" in parts[3]

    def test_rejects_token_with_unexpected_characters(self) -> None:
        backend = _FakeBackend()

        with pytest.raises(ValueError, match="unexpected characters"):
            configure_github_auth(backend, "bad'token$(reboot)")

        assert backend.commands == []

    def test_raises_on_nonzero_exit(self) -> None:
        backend = _FakeBackend(exit_code=1)

        with pytest.raises(RuntimeError, match="GitHub auth"):
            configure_github_auth(backend, "ghs_token123")


class TestCreateSandboxWithProxyOpensandbox:
    @pytest.mark.asyncio
    async def test_configures_hosts_yml_auth(self) -> None:
        with (
            patch(
                "agent.server.get_github_app_installation_token_with_expiry",
                new_callable=AsyncMock,
                return_value=("ghs_install", None),
            ),
            patch("agent.server.create_sandbox") as mock_create,
            patch("agent.server.configure_github_auth") as mock_auth,
            patch.dict("os.environ", {"SANDBOX_TYPE": "opensandbox"}),
        ):
            backend = MagicMock(id="uuid-1")
            mock_create.return_value = backend

            from agent.server import _create_sandbox_with_proxy

            await _create_sandbox_with_proxy()

            mock_create.assert_called_once_with(snapshot_id=None)
            mock_auth.assert_called_once_with(backend, "ghs_install")

    @pytest.mark.asyncio
    async def test_raises_when_no_installation_token(self) -> None:
        with (
            patch(
                "agent.server.get_github_app_installation_token_with_expiry",
                new_callable=AsyncMock,
                return_value=(None, None),
            ),
            patch("agent.server.create_sandbox") as mock_create,
            patch("agent.server.configure_github_auth") as mock_auth,
            patch.dict("os.environ", {"SANDBOX_TYPE": "opensandbox"}),
        ):
            mock_create.return_value = MagicMock(id="uuid-2")

            from agent.server import _create_sandbox_with_proxy

            with pytest.raises(ValueError, match="installation token is unavailable"):
                await _create_sandbox_with_proxy()

            mock_auth.assert_not_called()

    @pytest.mark.asyncio
    async def test_kills_orphaned_sandbox_when_no_token(self) -> None:
        """The just-created sandbox must not burn resources when auth setup aborts."""
        with (
            patch(
                "agent.server.get_github_app_installation_token_with_expiry",
                new_callable=AsyncMock,
                return_value=(None, None),
            ),
            patch("agent.server.create_sandbox") as mock_create,
            patch("agent.server.configure_github_auth"),
            patch.dict("os.environ", {"SANDBOX_TYPE": "opensandbox"}),
        ):
            backend = MagicMock(id="uuid-orphan")
            mock_create.return_value = backend

            from agent.server import _create_sandbox_with_proxy

            with pytest.raises(ValueError, match="installation token is unavailable"):
                await _create_sandbox_with_proxy()

            backend.kill.assert_called_once()


class TestRefreshGithubProxyOpensandbox:
    @pytest.mark.asyncio
    async def test_rewrites_hosts_yml_on_reuse(self) -> None:
        with (
            patch(
                "agent.server.get_github_app_installation_token_with_expiry",
                new_callable=AsyncMock,
                return_value=("ghs_fresh", None),
            ),
            patch("agent.server.configure_github_auth") as mock_auth,
            patch.dict("os.environ", {"SANDBOX_TYPE": "opensandbox"}),
        ):
            backend = MagicMock(id="uuid-3")

            from agent.server import _refresh_github_proxy

            await _refresh_github_proxy(backend)

            mock_auth.assert_called_once_with(backend, "ghs_fresh")

    @pytest.mark.asyncio
    async def test_skips_quietly_when_no_token(self) -> None:
        with (
            patch(
                "agent.server.get_github_app_installation_token_with_expiry",
                new_callable=AsyncMock,
                return_value=(None, None),
            ),
            patch("agent.server.configure_github_auth") as mock_auth,
            patch.dict("os.environ", {"SANDBOX_TYPE": "opensandbox"}),
        ):
            from agent.server import _refresh_github_proxy

            await _refresh_github_proxy(MagicMock(id="uuid-4"))

            mock_auth.assert_not_called()

    @pytest.mark.asyncio
    async def test_other_providers_still_skipped(self) -> None:
        with (
            patch("agent.server.configure_github_auth") as mock_auth,
            patch("agent.server._configure_github_proxy") as mock_proxy,
            patch.dict("os.environ", {"SANDBOX_TYPE": "daytona"}),
        ):
            from agent.server import _refresh_github_proxy

            await _refresh_github_proxy(MagicMock(id="dt-1"))

            mock_auth.assert_not_called()
            mock_proxy.assert_not_called()


class TestRenewTtlOnPing:
    @pytest.mark.asyncio
    async def test_ping_renews_ttl_when_backend_supports_it(self) -> None:
        backend = _RenewableFakeBackend()

        from agent.server import check_or_recreate_sandbox

        result = await check_or_recreate_sandbox(backend, "thread-1")

        assert result is backend
        assert backend.renew_calls == 1

    @pytest.mark.asyncio
    async def test_ping_without_renew_support_is_noop(self) -> None:
        backend = _FakeBackend()

        from agent.server import check_or_recreate_sandbox

        result = await check_or_recreate_sandbox(backend, "thread-1")

        assert result is backend
        assert backend.commands == ["echo ok"]

    @pytest.mark.asyncio
    async def test_renew_failure_is_nonfatal(self) -> None:
        backend = _RenewableFakeBackend()

        def boom() -> None:
            raise RuntimeError("renew failed")

        backend.renew_ttl = boom

        from agent.server import check_or_recreate_sandbox

        result = await check_or_recreate_sandbox(backend, "thread-1")

        assert result is backend


class TestAnalyzerGithubAuth:
    @pytest.mark.asyncio
    async def test_opensandbox_uses_hosts_yml_helper(self) -> None:
        with (
            patch("agent.analyzer.configure_github_auth") as mock_auth,
            patch.dict("os.environ", {"SANDBOX_TYPE": "opensandbox"}),
        ):
            backend = MagicMock(id="uuid-5")

            from agent.analyzer import _configure_sandbox_github_proxy

            await _configure_sandbox_github_proxy(backend, "ghs_tok")

            mock_auth.assert_called_once_with(backend, "ghs_tok")

    @pytest.mark.asyncio
    async def test_langsmith_still_uses_proxy(self) -> None:
        with (
            patch("agent.analyzer.configure_github_auth") as mock_auth,
            patch("agent.analyzer._configure_github_proxy") as mock_proxy,
            patch.dict("os.environ", {"SANDBOX_TYPE": "langsmith"}),
        ):
            backend = MagicMock(id="sb-6")

            from agent.analyzer import _configure_sandbox_github_proxy

            await _configure_sandbox_github_proxy(backend, "ghs_tok")

            mock_proxy.assert_called_once_with("sb-6", "ghs_tok")
            mock_auth.assert_not_called()
