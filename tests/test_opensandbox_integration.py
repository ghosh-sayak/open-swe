"""Unit tests for the OpenSandbox backend + factory + predicate (plan §11).

sys.modules-fake style, no network: the opensandbox SDK is faked and the
integration module is loaded in isolation, mirroring test_daytona_integration.py.
"""

import importlib.util
import sys
import threading
import types
from datetime import timedelta
from pathlib import Path

import pytest
from deepagents.backends.protocol import (
    FILE_NOT_FOUND,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# Fakes for the opensandbox SDK                                               #
# --------------------------------------------------------------------------- #
class _FakeConnectionConfigSync:
    def __init__(self, *, domain=None, api_key=None, use_server_proxy=False, **kwargs):
        self.domain = domain
        self.api_key = api_key
        self.use_server_proxy = use_server_proxy
        self.kwargs = kwargs


class _FakeRunCommandOpts:
    def __init__(
        self, *, timeout=None, background=False, working_directory=None, envs=None, **kwargs
    ):
        self.timeout = timeout
        self.background = background
        self.working_directory = working_directory
        self.envs = envs


class _FakeWriteEntry:
    def __init__(self, *, path, data=None, mode=755):
        self.path = path
        self.data = data
        self.mode = mode


class _FakeOutputMessage:
    def __init__(self, text, is_error=False):
        self.text = text
        self.is_error = is_error


class _FakeExecutionLogs:
    def __init__(self, stdout=None, stderr=None):
        self.stdout = [_FakeOutputMessage(t) for t in (stdout or [])]
        self.stderr = [_FakeOutputMessage(t, is_error=True) for t in (stderr or [])]


class _FakeExecution:
    def __init__(self, exit_code=0, stdout=None, stderr=None):
        self.exit_code = exit_code
        self.logs = _FakeExecutionLogs(stdout=stdout, stderr=stderr)


class _FakeCommands:
    def __init__(self, sandbox):
        self._sandbox = sandbox

    def run(self, command, *, opts=None, handlers=None):
        self._sandbox.run_calls.append((command, opts))
        return self._sandbox.next_execution


class _FakeFilesystem:
    def __init__(self, sandbox):
        self._sandbox = sandbox

    def create_directories(self, entries):
        if self._sandbox.mkdir_error is not None:
            raise self._sandbox.mkdir_error
        self._sandbox.created_dirs.extend(e.path for e in entries)

    def write_file(self, path, data):
        err = self._sandbox.write_errors.get(path)
        if err is not None:
            raise err
        self._sandbox.written[path] = data

    def read_bytes(self, path):
        err = self._sandbox.read_errors.get(path)
        if err is not None:
            raise err
        return self._sandbox.files_content[path]


class _FakeSandboxSync:
    def __init__(self, sandbox_id):
        self._id = sandbox_id
        self.commands = _FakeCommands(self)
        self.files = _FakeFilesystem(self)
        self.run_calls = []
        self.renew_calls = []
        self.killed = False
        self.closed = False
        self.origin = None
        self.create_kwargs = None
        self.connect_kwargs = None
        self.next_execution = _FakeExecution(0, stdout=["ok"])
        self.created_dirs = []
        self.written = {}
        self.files_content = {}
        self.write_errors = {}
        self.read_errors = {}
        self.mkdir_error = None

    @property
    def id(self):
        return self._id

    @classmethod
    def create(cls, image=None, **kwargs):
        inst = cls(sandbox_id="uuid-created")
        inst.origin = "create"
        inst.create_kwargs = {"image": image, **kwargs}
        return inst

    @classmethod
    def connect(cls, sandbox_id, **kwargs):
        inst = cls(sandbox_id=sandbox_id)
        inst.origin = "connect"
        inst.connect_kwargs = {"sandbox_id": sandbox_id, **kwargs}
        return inst

    def renew(self, timeout):
        self.renew_calls.append(timeout)

    def kill(self):
        self.killed = True

    def close(self):
        self.closed = True


class _FakePoolCreationSpec:
    def __init__(self, *, image, resource=None, env=None, extensions=None, **kwargs):
        self.image = image
        self.resource = resource
        self.env = env
        self.extensions = extensions


class _FakeAcquirePolicy:
    FAIL_FAST = "FAIL_FAST"
    DIRECT_CREATE = "DIRECT_CREATE"


class _FakeInMemoryPoolStateStore:
    pass


class _FakeSandboxPoolSync:
    instances: list["_FakeSandboxPoolSync"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = False
        self.acquire_calls = []
        _FakeSandboxPoolSync.instances.append(self)

    def start(self):
        self.started = True

    def acquire(self, sandbox_timeout=None, **kwargs):
        self.acquire_calls.append(sandbox_timeout)
        return _FakeSandboxSync(sandbox_id="uuid-pooled")


class _FakeSandboxException(Exception):
    pass


class _FakeSandboxApiException(_FakeSandboxException):
    def __init__(self, message="", status_code=None):
        super().__init__(message)
        self.status_code = status_code


class _FakeSandboxInternalException(_FakeSandboxException):
    pass


class _FakeSandboxReadyTimeoutException(_FakeSandboxException):
    pass


class _FakeSandboxUnhealthyException(_FakeSandboxException):
    pass


def _install_opensandbox_fakes(monkeypatch):
    """Register a fake opensandbox package tree in sys.modules."""
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
    modules["opensandbox.sync"].SandboxSync = _FakeSandboxSync
    modules["opensandbox"].SandboxPoolSync = _FakeSandboxPoolSync
    modules["opensandbox"].PoolCreationSpec = _FakePoolCreationSpec
    modules["opensandbox"].AcquirePolicy = _FakeAcquirePolicy
    modules["opensandbox"].InMemoryPoolStateStore = _FakeInMemoryPoolStateStore
    modules["opensandbox.models.execd"].RunCommandOpts = _FakeRunCommandOpts
    modules["opensandbox.models.filesystem"].WriteEntry = _FakeWriteEntry
    modules["opensandbox.config.connection_sync"].ConnectionConfigSync = _FakeConnectionConfigSync
    modules["opensandbox.exceptions"].SandboxException = _FakeSandboxException
    modules["opensandbox.exceptions"].SandboxApiException = _FakeSandboxApiException
    modules["opensandbox.exceptions"].SandboxInternalException = _FakeSandboxInternalException
    modules[
        "opensandbox.exceptions"
    ].SandboxReadyTimeoutException = _FakeSandboxReadyTimeoutException
    modules["opensandbox.exceptions"].SandboxUnhealthyException = _FakeSandboxUnhealthyException
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)


def _load_opensandbox_module(monkeypatch):
    _install_opensandbox_fakes(monkeypatch)
    module_path = ROOT / "agent" / "integrations" / "opensandbox.py"
    spec = importlib.util.spec_from_file_location("opensandbox_under_test", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_backend_sandbox(osb, *, exit_code=0, stdout=None, stderr=None):
    sandbox = _FakeSandboxSync(sandbox_id="uuid-exec")
    sandbox.next_execution = _FakeExecution(exit_code, stdout=stdout, stderr=stderr)
    return sandbox


@pytest.fixture
def osb(monkeypatch):
    monkeypatch.setenv("OPEN_SANDBOX_API_KEY", "test-key")
    monkeypatch.setenv("OPEN_SANDBOX_DOMAIN", "localhost:8090")
    monkeypatch.delenv("OPEN_SANDBOX_TTL_SECONDS", raising=False)
    monkeypatch.delenv("OPEN_SANDBOX_COMMAND_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("OPEN_SANDBOX_USE_SERVER_PROXY", raising=False)
    monkeypatch.delenv("OPEN_SANDBOX_IMAGE", raising=False)
    monkeypatch.delenv("OPEN_SANDBOX_CPU", raising=False)
    monkeypatch.delenv("OPEN_SANDBOX_MEMORY", raising=False)
    monkeypatch.delenv("OPEN_SANDBOX_POOL_ENABLED", raising=False)
    monkeypatch.delenv("OPEN_SANDBOX_POOL_REF", raising=False)
    monkeypatch.delenv("OPEN_SANDBOX_PROTOCOL", raising=False)
    monkeypatch.delenv("OPEN_SANDBOX_EXECUTE_CLIENT_GRACE_SECONDS", raising=False)
    _FakeSandboxPoolSync.instances.clear()
    return _load_opensandbox_module(monkeypatch)


# --------------------------------------------------------------------------- #
# Factory: env fail-fast + create/connect dispatch + renew                    #
# --------------------------------------------------------------------------- #
def test_factory_requires_api_key(monkeypatch):
    monkeypatch.delenv("OPEN_SANDBOX_API_KEY", raising=False)
    module = _load_opensandbox_module(monkeypatch)

    with pytest.raises(ValueError, match="OPEN_SANDBOX_API_KEY"):
        module.create_opensandbox_sandbox(None)


def test_cold_start_calls_create_with_defaults(osb):
    backend = osb.create_opensandbox_sandbox(None)

    sandbox = backend._sandbox
    assert sandbox.origin == "create"
    assert sandbox.create_kwargs["image"] == "open-swe-sandbox:latest"
    assert sandbox.create_kwargs["timeout"] == timedelta(seconds=7200)
    assert sandbox.create_kwargs["resource"] == {"cpu": "2", "memory": "4Gi"}
    conn = sandbox.create_kwargs["connection_config"]
    assert conn.domain == "localhost:8090"
    assert conn.api_key == "test-key"
    assert conn.use_server_proxy is False
    assert sandbox.renew_calls == []


def test_cold_start_honors_env_overrides(osb, monkeypatch):
    monkeypatch.setenv("OPEN_SANDBOX_IMAGE", "custom-image:1.2")
    monkeypatch.setenv("OPEN_SANDBOX_CPU", "4")
    monkeypatch.setenv("OPEN_SANDBOX_MEMORY", "8Gi")
    monkeypatch.setenv("OPEN_SANDBOX_USE_SERVER_PROXY", "true")

    backend = osb.create_opensandbox_sandbox(None)

    kwargs = backend._sandbox.create_kwargs
    assert kwargs["image"] == "custom-image:1.2"
    assert kwargs["resource"] == {"cpu": "4", "memory": "8Gi"}
    assert kwargs["connection_config"].use_server_proxy is True


def test_reconnect_calls_connect_and_renews(osb):
    backend = osb.create_opensandbox_sandbox("abc-123-uuid")

    sandbox = backend._sandbox
    assert sandbox.origin == "connect"
    assert sandbox.connect_kwargs["sandbox_id"] == "abc-123-uuid"
    assert backend.id == "abc-123-uuid"
    assert sandbox.renew_calls == [timedelta(seconds=7200)]


def test_reconnect_renews_with_configured_ttl(osb, monkeypatch):
    monkeypatch.setenv("OPEN_SANDBOX_TTL_SECONDS", "300")

    backend = osb.create_opensandbox_sandbox("abc-123-uuid")

    assert backend._sandbox.renew_calls == [timedelta(seconds=300)]


def test_invalid_ttl_raises(osb, monkeypatch):
    monkeypatch.setenv("OPEN_SANDBOX_TTL_SECONDS", "not-an-int")

    with pytest.raises(ValueError, match="OPEN_SANDBOX_TTL_SECONDS"):
        osb.create_opensandbox_sandbox(None)


@pytest.mark.parametrize("value", ["0", "-100"])
def test_non_positive_ttl_raises(osb, monkeypatch, value):
    monkeypatch.setenv("OPEN_SANDBOX_TTL_SECONDS", value)

    with pytest.raises(ValueError, match="OPEN_SANDBOX_TTL_SECONDS"):
        osb.create_opensandbox_sandbox(None)


def test_backend_declares_local_transport_ownership(osb):
    backend = osb.create_opensandbox_sandbox(None)

    assert backend.owns_local_transport is True


# --------------------------------------------------------------------------- #
# execute: exit-code passthrough, stdout/stderr merge, command timeout        #
# --------------------------------------------------------------------------- #
def test_execute_merges_stdout_and_stderr(osb):
    backend = osb.create_opensandbox_sandbox(None)
    backend._sandbox.next_execution = _FakeExecution(
        exit_code=0, stdout=["line1", "line2"], stderr=["err1"]
    )

    result = backend.execute("echo hi")

    assert isinstance(result, ExecuteResponse)
    assert result.output == "line1\nline2\nerr1"
    assert result.exit_code == 0
    assert result.truncated is False


def test_execute_stderr_only(osb):
    backend = osb.create_opensandbox_sandbox(None)
    backend._sandbox.next_execution = _FakeExecution(exit_code=1, stdout=[], stderr=["boom"])

    result = backend.execute("false")

    assert result.output == "boom"
    assert result.exit_code == 1


def test_execute_passes_timeout_exit_code_through(osb):
    backend = osb.create_opensandbox_sandbox(None)
    backend._sandbox.next_execution = _FakeExecution(exit_code=-1, stdout=["partial"])

    result = backend.execute("sleep 999")

    assert result.exit_code == -1


def test_execute_uses_default_command_timeout(osb):
    backend = osb.create_opensandbox_sandbox(None)

    backend.execute("echo hi")

    _command, opts = backend._sandbox.run_calls[-1]
    assert opts.timeout == timedelta(seconds=1800)


def test_execute_honors_explicit_timeout(osb):
    backend = osb.create_opensandbox_sandbox(None)

    backend.execute("echo hi", timeout=45)

    _command, opts = backend._sandbox.run_calls[-1]
    assert opts.timeout == timedelta(seconds=45)


def test_execute_timeout_zero_maps_to_default_cap(osb):
    """deepagents' execute tool passes 0 for "no timeout"; a server-side cap must still apply."""
    backend = osb.create_opensandbox_sandbox(None)

    backend.execute("sleep 999", timeout=0)

    _command, opts = backend._sandbox.run_calls[-1]
    assert opts.timeout == timedelta(seconds=1800)


def test_execute_command_timeout_env_override(osb, monkeypatch):
    monkeypatch.setenv("OPEN_SANDBOX_COMMAND_TIMEOUT_SECONDS", "600")
    backend = osb.create_opensandbox_sandbox(None)

    backend.execute("echo hi")

    _command, opts = backend._sandbox.run_calls[-1]
    assert opts.timeout == timedelta(seconds=600)


# --------------------------------------------------------------------------- #
# aexecute: client-side deadline + async offload (items A/B)                  #
# --------------------------------------------------------------------------- #
def test_aexecute_returns_output(osb):
    import asyncio

    backend_sandbox = _make_backend_sandbox(osb, stdout=["hello"])
    backend = osb.OpensandboxBackend(backend_sandbox)

    result = asyncio.run(backend.aexecute("echo hello"))

    assert result.exit_code == 0
    assert result.output == "hello"


def test_aexecute_returns_124_on_client_deadline(osb, monkeypatch):
    import asyncio

    monkeypatch.setenv("OPEN_SANDBOX_EXECUTE_CLIENT_GRACE_SECONDS", "0")
    backend_sandbox = _make_backend_sandbox(osb, stdout=["never"])
    backend = osb.OpensandboxBackend(backend_sandbox)

    blocked = threading.Event()

    def _hang(command, effective):
        blocked.wait()  # blocks forever; the deadline must fire

    monkeypatch.setattr(backend, "_run_blocking", _hang)

    # timeout=1 -> effective=1, grace=0 -> client deadline ~1s
    result = asyncio.run(backend.aexecute("sleep 999", timeout=1))
    blocked.set()  # release the abandoned worker so the test process can exit

    assert result.exit_code == 124
    assert "deadline" in result.output.lower()


# --------------------------------------------------------------------------- #
# upload_files / download_files: partial success + parent-dir creation        #
# --------------------------------------------------------------------------- #
def test_upload_creates_parent_dirs_and_writes(osb):
    backend = osb.create_opensandbox_sandbox(None)

    responses = backend.upload_files([("/workspace/sub/dir/file.txt", b"data")])

    assert responses == [FileUploadResponse(path="/workspace/sub/dir/file.txt", error=None)]
    assert "/workspace/sub/dir" in backend._sandbox.created_dirs
    assert backend._sandbox.written["/workspace/sub/dir/file.txt"] == b"data"


def test_upload_partial_success_does_not_raise(osb):
    backend = osb.create_opensandbox_sandbox(None)
    backend._sandbox.write_errors["/bad.txt"] = RuntimeError("disk full")

    responses = backend.upload_files([("/good.txt", b"ok"), ("/bad.txt", b"nope")])

    assert responses[0].path == "/good.txt"
    assert responses[0].error is None
    assert responses[1].path == "/bad.txt"
    assert responses[1].error is not None
    assert backend._sandbox.written["/good.txt"] == b"ok"


def test_download_returns_bytes(osb):
    backend = osb.create_opensandbox_sandbox(None)
    backend._sandbox.files_content["/workspace/a.txt"] = b"hello"

    responses = backend.download_files(["/workspace/a.txt"])

    assert responses == [
        FileDownloadResponse(path="/workspace/a.txt", content=b"hello", error=None)
    ]


def test_download_partial_success_maps_not_found(osb):
    backend = osb.create_opensandbox_sandbox(None)
    backend._sandbox.files_content["/workspace/a.txt"] = b"hello"
    backend._sandbox.read_errors["/workspace/missing.txt"] = osb.SandboxApiException(
        "gone", status_code=404
    )

    responses = backend.download_files(["/workspace/a.txt", "/workspace/missing.txt"])

    assert responses[0].content == b"hello"
    assert responses[0].error is None
    assert responses[1].content is None
    assert responses[1].error == FILE_NOT_FOUND


def test_renew_ttl_renews_with_configured_ttl(osb, monkeypatch):
    backend = osb.create_opensandbox_sandbox(None)
    monkeypatch.setenv("OPEN_SANDBOX_TTL_SECONDS", "900")

    backend.renew_ttl()

    assert backend._sandbox.renew_calls == [timedelta(seconds=900)]


# --------------------------------------------------------------------------- #
# id / kill / close                                                           #
# --------------------------------------------------------------------------- #
def test_kill_and_close_forwarded(osb):
    backend = osb.create_opensandbox_sandbox(None)

    backend.kill()
    backend.close()

    assert backend._sandbox.killed is True
    assert backend._sandbox.closed is True


# --------------------------------------------------------------------------- #
# is_recoverable_sandbox_error truth table (D5)                               #
# --------------------------------------------------------------------------- #
def test_recoverable_internal_exception(osb):
    assert osb.is_recoverable_sandbox_error(osb.SandboxInternalException("net down")) is True


@pytest.mark.parametrize("status", [404, 500, 502, 503, 504])
def test_recoverable_api_status_codes(osb, status):
    assert (
        osb.is_recoverable_sandbox_error(osb.SandboxApiException("x", status_code=status)) is True
    )


@pytest.mark.parametrize("status", [401, 403, 400, 422])
def test_non_recoverable_api_status_codes(osb, status):
    assert (
        osb.is_recoverable_sandbox_error(osb.SandboxApiException("x", status_code=status)) is False
    )


def test_recoverable_ready_timeout_and_unhealthy(osb):
    """A dead pod behind a live API record surfaces as ready-timeout/unhealthy on connect."""
    assert osb.is_recoverable_sandbox_error(osb.SandboxReadyTimeoutException("t")) is True
    assert osb.is_recoverable_sandbox_error(osb.SandboxUnhealthyException("u")) is True


def test_non_sandbox_exception_not_recoverable(osb):
    assert osb.is_recoverable_sandbox_error(TypeError("bug")) is False
    assert osb.is_recoverable_sandbox_error(ValueError("bug")) is False


# --------------------------------------------------------------------------- #
# startup validation                                                          #
# --------------------------------------------------------------------------- #
def test_validate_startup_requires_api_key(monkeypatch):
    monkeypatch.delenv("OPEN_SANDBOX_API_KEY", raising=False)
    module = _load_opensandbox_module(monkeypatch)

    with pytest.raises(ValueError, match="OPEN_SANDBOX_API_KEY"):
        module.validate_startup_config()


def test_validate_startup_probes_health(osb, monkeypatch):
    probed = {}

    def fake_probe(url):
        probed["url"] = url

    monkeypatch.setattr(osb, "_probe_health", fake_probe)

    osb.validate_startup_config()

    assert probed["url"] == "http://localhost:8090/health"


def test_validate_startup_probes_health_https(osb, monkeypatch):
    monkeypatch.setenv("OPEN_SANDBOX_PROTOCOL", "https")
    probed = {}

    def fake_probe(url):
        probed["url"] = url

    monkeypatch.setattr(osb, "_probe_health", fake_probe)

    osb.validate_startup_config()

    assert probed["url"] == "https://localhost:8090/health"


def test_validate_startup_raises_on_unreachable(osb, monkeypatch):
    def fake_probe(url):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(osb, "_probe_health", fake_probe)

    with pytest.raises(ValueError, match="health check"):
        osb.validate_startup_config()


# --------------------------------------------------------------------------- #
# Registry + startup dispatch in agent/utils/sandbox.py                        #
# --------------------------------------------------------------------------- #
def test_registry_has_opensandbox_entry():
    from agent.utils.sandbox import SANDBOX_FACTORIES

    assert SANDBOX_FACTORIES["opensandbox"] == (
        "agent.integrations.opensandbox",
        "create_opensandbox_sandbox",
    )


def test_validate_sandbox_startup_config_dispatches_opensandbox(monkeypatch):
    _install_opensandbox_fakes(monkeypatch)
    monkeypatch.setenv("SANDBOX_TYPE", "opensandbox")
    monkeypatch.setenv("OPEN_SANDBOX_API_KEY", "test-key")
    monkeypatch.setenv("OPEN_SANDBOX_DOMAIN", "localhost:8090")
    monkeypatch.delitem(sys.modules, "agent.integrations.opensandbox", raising=False)

    probed = {}
    try:
        import agent.integrations.opensandbox as osb_module

        monkeypatch.setattr(osb_module, "_probe_health", lambda url: probed.setdefault("url", url))

        from agent.utils.sandbox import validate_sandbox_startup_config

        validate_sandbox_startup_config()
        assert probed["url"] == "http://localhost:8090/health"
    finally:
        sys.modules.pop("agent.integrations.opensandbox", None)


# --------------------------------------------------------------------------- #
# Pooling create-seam (D4)                                                    #
# --------------------------------------------------------------------------- #
def test_cold_start_without_pool_passes_no_extensions(osb):
    backend = osb.create_opensandbox_sandbox(None)

    assert backend._sandbox.create_kwargs.get("extensions") is None
    assert _FakeSandboxPoolSync.instances == []


def test_k8s_pool_ref_claims_via_extensions(osb, monkeypatch):
    monkeypatch.setenv("OPEN_SANDBOX_POOL_ENABLED", "true")
    monkeypatch.setenv("OPEN_SANDBOX_POOL_REF", "open-swe-pool")

    backend = osb.create_opensandbox_sandbox(None)

    sandbox = backend._sandbox
    assert sandbox.origin == "create"
    assert sandbox.create_kwargs["extensions"] == {"poolRef": "open-swe-pool"}
    assert _FakeSandboxPoolSync.instances == []


def test_pool_ref_without_enabled_flag_is_ignored(osb, monkeypatch):
    monkeypatch.setenv("OPEN_SANDBOX_POOL_REF", "open-swe-pool")

    backend = osb.create_opensandbox_sandbox(None)

    assert backend._sandbox.create_kwargs.get("extensions") is None


def test_local_sdk_pool_acquires_when_enabled_without_ref(osb, monkeypatch):
    monkeypatch.setenv("OPEN_SANDBOX_POOL_ENABLED", "true")

    backend = osb.create_opensandbox_sandbox(None)

    assert backend.id == "uuid-pooled"
    assert len(_FakeSandboxPoolSync.instances) == 1
    pool = _FakeSandboxPoolSync.instances[0]
    assert pool.started is True
    assert pool.acquire_calls == [timedelta(seconds=7200)]
    spec = pool.kwargs["creation_spec"]
    assert spec.image == "open-swe-sandbox:latest"
    assert spec.resource == {"cpu": "2", "memory": "4Gi"}


def test_local_sdk_pool_is_reused_across_calls(osb, monkeypatch):
    monkeypatch.setenv("OPEN_SANDBOX_POOL_ENABLED", "true")

    osb.create_opensandbox_sandbox(None)
    osb.create_opensandbox_sandbox(None)

    assert len(_FakeSandboxPoolSync.instances) == 1
    assert len(_FakeSandboxPoolSync.instances[0].acquire_calls) == 2


def test_reconnect_ignores_pooling(osb, monkeypatch):
    monkeypatch.setenv("OPEN_SANDBOX_POOL_ENABLED", "true")
    monkeypatch.setenv("OPEN_SANDBOX_POOL_REF", "open-swe-pool")

    backend = osb.create_opensandbox_sandbox("abc-123-uuid")

    assert backend._sandbox.origin == "connect"
    assert _FakeSandboxPoolSync.instances == []


def test_local_sdk_pool_init_is_thread_safe(osb, monkeypatch):
    """Two concurrent cold-starts must not each start (and leak) a live pool."""
    import time
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setenv("OPEN_SANDBOX_POOL_ENABLED", "true")
    original_init = _FakeSandboxPoolSync.__init__

    def slow_init(self, **kwargs):
        time.sleep(0.05)
        original_init(self, **kwargs)

    monkeypatch.setattr(_FakeSandboxPoolSync, "__init__", slow_init)

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda _: osb.create_opensandbox_sandbox(None), range(2)))

    assert len(_FakeSandboxPoolSync.instances) == 1
