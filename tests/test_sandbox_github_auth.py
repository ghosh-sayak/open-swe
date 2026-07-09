"""Tests for the shared hosts.yml GitHub auth helper + its server/analyzer wiring (plan D3)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from deepagents.backends.protocol import ExecuteResponse, FileUploadResponse

from agent.utils.sandbox_github_auth import configure_github_auth


class _FakeBackend:
    """Minimal sandbox backend capturing execute() + upload_files() calls."""

    id = "uuid-fake"

    def __init__(self, exit_code: int = 0):
        self.exit_code = exit_code
        self.commands: list[str] = []
        self.uploads: list[list[tuple[str, bytes]]] = []
        self.upload_error: str | None = None

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        self.commands.append(command)
        return ExecuteResponse(output="", exit_code=self.exit_code)

    def upload_files(self, files):
        self.uploads.append(files)
        return [FileUploadResponse(path=p, error=self.upload_error) for p, _ in files]


class _RenewableFakeBackend(_FakeBackend):
    def __init__(self, exit_code: int = 0):
        super().__init__(exit_code)
        self.renew_calls = 0

    def renew_ttl(self) -> None:
        self.renew_calls += 1


class TestConfigureGithubAuth:
    def test_writes_credentials_via_file_api(self) -> None:
        backend = _FakeBackend()

        configure_github_auth(backend, "ghs_token123")

        # Exactly one upload batch with the two credential files.
        assert len(backend.uploads) == 1
        written = dict(backend.uploads[0])
        assert set(written) == {
            "/root/.config/git/insteadof.gitconfig",
            "/root/.config/gh/hosts.yml",
        }
        insteadof = written["/root/.config/git/insteadof.gitconfig"].decode()
        assert "x-access-token:ghs_token123@github.com" in insteadof
        assert "insteadOf = https://github.com/" in insteadof
        hosts = written["/root/.config/gh/hosts.yml"].decode()
        assert "oauth_token: ghs_token123" in hosts
        assert "user: x-access-token" in hosts

    def test_token_never_appears_on_a_command_line(self) -> None:
        backend = _FakeBackend()

        configure_github_auth(backend, "ghs_token123")

        # The only exec call registers the include file and must be token-free.
        assert all("ghs_token123" not in cmd for cmd in backend.commands)
        assert any("include.path" in cmd for cmd in backend.commands)

    def test_rejects_token_with_unexpected_characters(self) -> None:
        backend = _FakeBackend()

        with pytest.raises(ValueError, match="unexpected characters"):
            configure_github_auth(backend, "bad'token$(reboot)")

        assert backend.commands == []
        assert backend.uploads == []

    def test_raises_on_nonzero_exit(self, monkeypatch) -> None:
        import agent.utils.sandbox_github_auth as auth

        monkeypatch.setattr(auth.time, "sleep", lambda _s: None)
        backend = _FakeBackend(exit_code=1)

        with pytest.raises(RuntimeError, match="Failed to register git include"):
            configure_github_auth(backend, "ghs_token123")

    def test_raises_when_upload_fails(self, monkeypatch) -> None:
        import agent.utils.sandbox_github_auth as auth

        monkeypatch.setattr(auth.time, "sleep", lambda _s: None)
        backend = _FakeBackend()
        backend.upload_error = "disk full"

        with pytest.raises(RuntimeError, match="Failed to write GitHub credential files"):
            configure_github_auth(backend, "ghs_token123")

        # Must not leak the underlying error detail (could echo file content).
        assert backend.commands == []


class TestConfigureGithubAuthRetry:
    def test_retries_then_succeeds(self, monkeypatch) -> None:
        import agent.utils.sandbox_github_auth as auth

        monkeypatch.setattr(auth.time, "sleep", lambda _s: None)
        backend = _FakeBackend()
        # Fail the upload once, then succeed.
        calls = {"n": 0}
        real_upload = backend.upload_files

        def flaky_upload(files):
            calls["n"] += 1
            if calls["n"] == 1:
                backend.upload_error = "boom"
                try:
                    return real_upload(files)
                finally:
                    backend.upload_error = None
            return real_upload(files)

        backend.upload_files = flaky_upload

        configure_github_auth(backend, "ghs_token123")

        assert calls["n"] == 2  # one failure, one success

    def test_raises_after_exhausting_retries(self, monkeypatch) -> None:
        import agent.utils.sandbox_github_auth as auth

        monkeypatch.setattr(auth.time, "sleep", lambda _s: None)
        backend = _FakeBackend()
        backend.upload_error = "always"
        # Count attempts to prove the loop exhausts exactly AUTH_CONFIG_MAX_ATTEMPTS.
        calls = {"n": 0}
        real_upload = backend.upload_files

        def counting_upload(files):
            calls["n"] += 1
            return real_upload(files)

        backend.upload_files = counting_upload

        with pytest.raises(RuntimeError, match="Failed to write GitHub credential files"):
            configure_github_auth(backend, "ghs_token123")

        assert calls["n"] == auth.AUTH_CONFIG_MAX_ATTEMPTS == 3


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


class TestOpensandboxRecordsTokenExpiry:
    @pytest.mark.asyncio
    async def test_create_records_expiry_for_midrun_refresh(self) -> None:
        with (
            patch(
                "agent.server.get_github_app_installation_token_with_expiry",
                new_callable=AsyncMock,
                return_value=("ghs_install", "2025-01-01T13:00:00Z"),
            ),
            patch("agent.server.create_sandbox") as mock_create,
            patch("agent.server.configure_github_auth"),
            patch("agent.server.record_proxy_token_expiry") as mock_record,
            patch.dict("os.environ", {"SANDBOX_TYPE": "opensandbox"}),
        ):
            mock_create.return_value = MagicMock(id="uuid-1")

            from agent.server import _create_sandbox_with_proxy

            await _create_sandbox_with_proxy(thread_id="thread-1")

            mock_record.assert_called_once()
            args, _kwargs = mock_record.call_args
            assert args[0] == "thread-1"
            assert args[1] == "2025-01-01T13:00:00Z"

    @pytest.mark.asyncio
    async def test_refresh_records_expiry_for_midrun_refresh(self) -> None:
        with (
            patch(
                "agent.server.get_github_app_installation_token_with_expiry",
                new_callable=AsyncMock,
                return_value=("ghs_fresh", "2025-01-01T13:00:00Z"),
            ),
            patch("agent.server.configure_github_auth"),
            patch("agent.server.record_proxy_token_expiry") as mock_record,
            patch.dict("os.environ", {"SANDBOX_TYPE": "opensandbox"}),
        ):
            from agent.server import _refresh_github_proxy

            await _refresh_github_proxy(MagicMock(id="uuid-2"), thread_id="thread-2")

            mock_record.assert_called_once()
            args, _kwargs = mock_record.call_args
            assert args[0] == "thread-2"
            assert args[1] == "2025-01-01T13:00:00Z"


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
