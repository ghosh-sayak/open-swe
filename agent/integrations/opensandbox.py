"""OpenSandbox sandbox backend integration (SANDBOX_TYPE=opensandbox).

Self-contained BaseSandbox subclass over the blocking sync SDK
(opensandbox.sync.SandboxSync), per plan D1/D2. The six inherited ops
(read/write/edit/ls/grep/glob) come from BaseSandbox and run python3/grep
inside the sandbox image.
"""

from __future__ import annotations

import logging
import os
import posixpath
import threading
from datetime import timedelta

import httpx
from deepagents.backends.protocol import (
    FILE_NOT_FOUND,
    PERMISSION_DENIED,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
    SandboxBackendProtocol,
)
from deepagents.backends.sandbox import BaseSandbox
from opensandbox.config.connection_sync import ConnectionConfigSync
from opensandbox.exceptions import (
    SandboxApiException,
    SandboxInternalException,
    SandboxReadyTimeoutException,
    SandboxUnhealthyException,
)
from opensandbox.models.execd import RunCommandOpts
from opensandbox.models.filesystem import WriteEntry
from opensandbox.sync import SandboxSync

logger = logging.getLogger(__name__)

DEFAULT_DOMAIN = "localhost:8080"
DEFAULT_IMAGE = "open-swe-sandbox:latest"
DEFAULT_TTL_SECONDS = 7200
DEFAULT_COMMAND_TIMEOUT_SECONDS = 1800
DEFAULT_CPU = "2"
DEFAULT_MEMORY = "4Gi"
HEALTH_PROBE_TIMEOUT_SECONDS = 5.0

RECOVERABLE_API_STATUS_CODES = frozenset({404, 500, 502, 503, 504})


def _parse_int_env(name: str, default: int, *, positive: bool = False) -> int:
    raw = os.environ.get(name)
    if not raw:
        value = default
    else:
        try:
            value = int(raw)
        except ValueError as e:
            raise ValueError(f"{name} must be an integer, got {raw!r}") from e
    if positive and value <= 0:
        raise ValueError(f"{name} must be a positive integer, got {value}")
    return value


def _parse_bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _require_api_key() -> str:
    api_key = os.environ.get("OPEN_SANDBOX_API_KEY")
    if not api_key:
        raise ValueError("OPEN_SANDBOX_API_KEY environment variable is required")
    return api_key


def _get_domain() -> str:
    return os.environ.get("OPEN_SANDBOX_DOMAIN", DEFAULT_DOMAIN)


def _connection_config() -> ConnectionConfigSync:
    return ConnectionConfigSync(
        domain=_get_domain(),
        api_key=_require_api_key(),
        use_server_proxy=_parse_bool_env("OPEN_SANDBOX_USE_SERVER_PROXY"),
    )


def _ttl() -> timedelta:
    return timedelta(
        seconds=_parse_int_env("OPEN_SANDBOX_TTL_SECONDS", DEFAULT_TTL_SECONDS, positive=True)
    )


def _image() -> str:
    return os.environ.get("OPEN_SANDBOX_IMAGE", DEFAULT_IMAGE)


def _resource() -> dict[str, str]:
    return {
        "cpu": os.environ.get("OPEN_SANDBOX_CPU", DEFAULT_CPU),
        "memory": os.environ.get("OPEN_SANDBOX_MEMORY", DEFAULT_MEMORY),
    }


def _map_file_error(exc: Exception) -> str:
    if isinstance(exc, SandboxApiException):
        if exc.status_code == 404:
            return FILE_NOT_FOUND
        if exc.status_code == 403:
            return PERMISSION_DENIED
    return f"{type(exc).__name__}: {exc}"


class OpensandboxBackend(BaseSandbox):
    """deepagents sandbox backend over an OpenSandbox SandboxSync instance."""

    # Each SandboxSync owns a local httpx transport; set_sandbox_backend closes
    # replaced backends that declare this so recreation doesn't leak connections.
    owns_local_transport = True

    def __init__(self, sandbox: SandboxSync) -> None:
        self._sandbox = sandbox
        self._command_timeout_seconds = _parse_int_env(
            "OPEN_SANDBOX_COMMAND_TIMEOUT_SECONDS", DEFAULT_COMMAND_TIMEOUT_SECONDS, positive=True
        )

    @property
    def id(self) -> str:
        return self._sandbox.id

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        # Always enforce a server-side command timeout: the sync SSE read has no
        # client-side deadline, so an unbounded command would pin a worker thread.
        # timeout=0 means "no timeout" to deepagents' execute tool — cap it too.
        effective = timeout if timeout else self._command_timeout_seconds
        execution = self._sandbox.commands.run(
            command,
            opts=RunCommandOpts(timeout=timedelta(seconds=effective)),
        )
        chunks = [m.text.rstrip("\n") for m in execution.logs.stdout]
        chunks.extend(m.text.rstrip("\n") for m in execution.logs.stderr)
        return ExecuteResponse(
            output="\n".join(chunks),
            exit_code=execution.exit_code,
            truncated=False,
        )

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        responses: list[FileUploadResponse] = []
        for path, content in files:
            try:
                parent = posixpath.dirname(path)
                if parent and parent != "/":
                    self._sandbox.files.create_directories([WriteEntry(path=parent)])
                self._sandbox.files.write_file(path, content)
                responses.append(FileUploadResponse(path=path, error=None))
            except Exception as exc:
                responses.append(FileUploadResponse(path=path, error=_map_file_error(exc)))
        return responses

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        responses: list[FileDownloadResponse] = []
        for path in paths:
            try:
                content = self._sandbox.files.read_bytes(path)
                responses.append(FileDownloadResponse(path=path, content=content, error=None))
            except Exception as exc:
                responses.append(
                    FileDownloadResponse(path=path, content=None, error=_map_file_error(exc))
                )
        return responses

    def renew_ttl(self) -> None:
        """Slide the absolute TTL window (D6); called on every reconnect/ping."""
        self._sandbox.renew(_ttl())

    def kill(self) -> None:
        self._sandbox.kill()

    def close(self) -> None:
        self._sandbox.close()


_sdk_pool = None
_sdk_pool_lock = threading.Lock()

SDK_POOL_NAME = "open-swe-local"
SDK_POOL_MAX_IDLE = 1


def _acquire_from_sdk_pool(connection_config: ConnectionConfigSync, ttl: timedelta):
    """Claim from the client-side eager-create pool (D4 local option, off by default)."""
    global _sdk_pool
    if _sdk_pool is None:
        # The factory runs on asyncio.to_thread workers; without the lock two
        # concurrent cold-starts would each start() a pool and leak one
        # reconciler thread + its warm sandbox.
        with _sdk_pool_lock:
            if _sdk_pool is None:
                from opensandbox import InMemoryPoolStateStore, PoolCreationSpec, SandboxPoolSync

                pool = SandboxPoolSync(
                    pool_name=SDK_POOL_NAME,
                    max_idle=SDK_POOL_MAX_IDLE,
                    state_store=InMemoryPoolStateStore(),
                    connection_config=connection_config,
                    creation_spec=PoolCreationSpec(image=_image(), resource=_resource()),
                )
                pool.start()
                _sdk_pool = pool
    return _sdk_pool.acquire(sandbox_timeout=ttl)


def create_opensandbox_sandbox(sandbox_id: str | None = None) -> SandboxBackendProtocol:
    """Create or reconnect to an OpenSandbox sandbox.

    Reconnects (and renews the TTL) when sandbox_id is given. Cold-start seam
    (D4): k8s server-side pool claim via extensions.poolRef when pooling is
    enabled and OPEN_SANDBOX_POOL_REF is set; local SDK-side pool when enabled
    without a poolRef; plain create otherwise (the default).
    """
    connection_config = _connection_config()
    ttl = _ttl()

    if sandbox_id:
        sandbox = SandboxSync.connect(sandbox_id, connection_config=connection_config)
        sandbox.renew(ttl)
        return OpensandboxBackend(sandbox)

    pool_enabled = _parse_bool_env("OPEN_SANDBOX_POOL_ENABLED")
    pool_ref = os.environ.get("OPEN_SANDBOX_POOL_REF")

    if pool_enabled and pool_ref:
        sandbox = SandboxSync.create(
            image=_image(),
            timeout=ttl,
            resource=_resource(),
            extensions={"poolRef": pool_ref},
            connection_config=connection_config,
        )
        logger.info("Claimed OpenSandbox sandbox %s from pool %s", sandbox.id, pool_ref)
    elif pool_enabled:
        sandbox = _acquire_from_sdk_pool(connection_config, ttl)
        logger.info("Acquired OpenSandbox sandbox %s from local SDK pool", sandbox.id)
    else:
        sandbox = SandboxSync.create(
            image=_image(),
            timeout=ttl,
            resource=_resource(),
            connection_config=connection_config,
        )
        logger.info("Created OpenSandbox sandbox %s", sandbox.id)
    return OpensandboxBackend(sandbox)


def is_recoverable_sandbox_error(exc: BaseException) -> bool:
    """Whether exc indicates a dead/unreachable sandbox that recreation can fix.

    Ready-timeout/unhealthy cover a dead pod behind a still-live API record
    (connect polls health and raises these instead of a 404). Auth/validation
    failures (401/403/4xx other than 404) are NOT recoverable: recreation is
    destructive (the workspace is lost) and would not fix them.
    """
    if isinstance(
        exc,
        (SandboxInternalException, SandboxReadyTimeoutException, SandboxUnhealthyException),
    ):
        return True
    if isinstance(exc, SandboxApiException):
        return exc.status_code in RECOVERABLE_API_STATUS_CODES
    return False


def _probe_health(url: str) -> None:
    response = httpx.get(url, timeout=HEALTH_PROBE_TIMEOUT_SECONDS)
    response.raise_for_status()


def validate_startup_config() -> None:
    """Fail fast at server startup: env present + OpenSandbox server reachable."""
    _require_api_key()
    _parse_int_env("OPEN_SANDBOX_TTL_SECONDS", DEFAULT_TTL_SECONDS, positive=True)
    _parse_int_env(
        "OPEN_SANDBOX_COMMAND_TIMEOUT_SECONDS", DEFAULT_COMMAND_TIMEOUT_SECONDS, positive=True
    )
    url = f"http://{_get_domain()}/health"
    try:
        _probe_health(url)
    except Exception as e:
        raise ValueError(f"OpenSandbox server health check failed at {url}: {e}") from e
