# OpenSandbox Lifecycle & Auth Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the `SANDBOX_TYPE=opensandbox` provider to the `langsmith` provider's robustness bar on six portable lifecycle/auth gaps, with no behavior change for any other provider.

**Architecture:** Six independent changes across the opensandbox backend (`agent/integrations/opensandbox.py`), the shared GitHub-auth helper (`agent/utils/sandbox_github_auth.py`), and the mid-run token-refresh path (`agent/utils/github_proxy.py`). Each is gated on `SANDBOX_TYPE == "opensandbox"` or lives in an opensandbox-only code path. Tests are unit-only, using the existing `sys.modules`-fake style for the SDK (no network).

**Tech Stack:** Python 3.11, `opensandbox` sync SDK (`SandboxSync`, `CommandsSync`, `FilesystemSync`, `ConnectionConfigSync`), deepagents `BaseSandbox` / `SandboxBackendProtocol`, pytest (`asyncio_mode = "auto"`), ruff (line-length 100).

## Global Constraints

- No behavior change for `langsmith` or any other provider — every edit is gated on `SANDBOX_TYPE == "opensandbox"` or lives in an opensandbox-only module/branch.
- Match the small, curated provider style; do not re-implement anything the `opensandbox` SDK or deepagents `BaseSandbox` already provides.
- Verify every SDK API shape against the installed package before use; do not guess signatures. (Signatures used here were confirmed against the installed `opensandbox` package.)
- Ruff line-length 100, target py311. Run `make lint` before each commit.
- Tests are unit-only under `tests/`; the opensandbox SDK is faked via `sys.modules` (see `tests/test_opensandbox_integration.py`). `asyncio_mode = "auto"` — `async def test_*` needs no decorator, but existing files use explicit `@pytest.mark.asyncio`; follow the file you are editing.
- Run a single test: `uv run pytest -vvv tests/<file>::<test>`.

## Confirmed SDK signatures (reference for all tasks)

```
SandboxSync.renew(timeout: datetime.timedelta) -> SandboxRenewResponse
CommandsSync.run(command: str, *, opts: RunCommandOpts | None = None, handlers=None) -> Execution
CommandsSync.interrupt(execution_id: str) -> None          # requires background mode to get an id; NOT used in this plan
FilesystemSync.write_file(path, data, *, encoding='utf-8', mode=755, ...) -> None
SandboxBackendProtocol.upload_files(files: list[tuple[str, bytes]]) -> list[FileUploadResponse]   # provider-agnostic; used by item G
ConnectionConfigSync(...).protocol -> str   # default 'http'
```

## File structure

| File | Responsibility | Touched by |
|---|---|---|
| `agent/integrations/opensandbox.py` | Backend + factory + predicate + startup validation | H (health scheme), A+B (execute deadline/async) |
| `agent/utils/sandbox_github_auth.py` | In-sandbox GitHub credential write | G (file-API write), C (retry) |
| `agent/utils/github_proxy.py` | Mid-run credential/token refresh | F (TTL renewal) |
| `tests/test_opensandbox_integration.py` | Backend/factory/startup unit tests | H, A+B |
| `tests/test_sandbox_github_auth.py` | Auth-helper unit tests | G, C |
| `tests/test_github_proxy_refresh.py` | Mid-run refresh unit tests | F |

## Task order & rationale

1. **Task 1 (H)** — isolated startup-validation change; smallest, no dependencies.
2. **Task 2 (A+B)** — backend execute path; self-contained.
3. **Task 3 (G)** — rewrite the auth write to the file API (changes the mechanism C wraps).
4. **Task 4 (C)** — add retry around the now-final auth write.
5. **Task 5 (F)** — TTL renewal in the mid-run refresh path.

Each task ends with a passing test suite and a commit.

---

### Task 1: HTTPS-aware startup health probe (item H)

The startup health probe hardcodes `http://` and so breaks any TLS deployment. Derive the scheme from an `OPEN_SANDBOX_PROTOCOL` env var (default `http`, preserving current local-dev behavior) and thread it into both the health-probe URL and the SDK connection config so the probe matches how the SDK actually connects.

> **Health path note:** the probe path stays `/health` (current behavior). The OpenSandbox execd API documents `GET /ping` as its health endpoint, but that is the in-sandbox agent, not necessarily the control-plane the startup probe hits. Do **not** change the path in this task; verifying `/ping` vs `/health` against a running control-plane is a separate runtime check.

**Files:**
- Modify: `agent/integrations/opensandbox.py` (add `DEFAULT_PROTOCOL`, `_get_protocol`, use in `_connection_config` and `validate_startup_config`)
- Test: `tests/test_opensandbox_integration.py`

**Interfaces:**
- Consumes: `_get_domain()`, `_connection_config()`, `_probe_health(url)`, `validate_startup_config()` (existing).
- Produces: `_get_protocol() -> str`; `validate_startup_config` builds the probe URL as `f"{_get_protocol()}://{_get_domain()}/health"`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_opensandbox_integration.py` (near `test_validate_startup_probes_health`):

```python
def test_validate_startup_probes_health_https(osb, monkeypatch):
    monkeypatch.setenv("OPEN_SANDBOX_PROTOCOL", "https")
    probed = {}

    def fake_probe(url):
        probed["url"] = url

    monkeypatch.setattr(osb, "_probe_health", fake_probe)

    osb.validate_startup_config()

    assert probed["url"] == "https://localhost:8090/health"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest -vvv tests/test_opensandbox_integration.py::test_validate_startup_probes_health_https`
Expected: FAIL — probed URL is `http://localhost:8090/health` (scheme hardcoded).

- [ ] **Step 3: Write minimal implementation**

In `agent/integrations/opensandbox.py`, add a constant near the other defaults:

```python
DEFAULT_PROTOCOL = "http"
```

Add a helper next to `_get_domain`:

```python
def _get_protocol() -> str:
    return os.environ.get("OPEN_SANDBOX_PROTOCOL", DEFAULT_PROTOCOL)
```

Thread it into the connection config so real SDK traffic uses the same scheme:

```python
def _connection_config() -> ConnectionConfigSync:
    return ConnectionConfigSync(
        domain=_get_domain(),
        api_key=_require_api_key(),
        protocol=_get_protocol(),
        use_server_proxy=_parse_bool_env("OPEN_SANDBOX_USE_SERVER_PROXY"),
    )
```

In `validate_startup_config`, replace the hardcoded scheme:

```python
    url = f"{_get_protocol()}://{_get_domain()}/health"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -vvv tests/test_opensandbox_integration.py -k "validate_startup"`
Expected: PASS — both `test_validate_startup_probes_health` (still `http://...`) and the new `_https` test pass. (`_FakeConnectionConfigSync` already swallows extra kwargs via `**kwargs`, so the added `protocol=` argument is harmless.)

- [ ] **Step 5: Lint and commit**

```bash
make lint
git add agent/integrations/opensandbox.py tests/test_opensandbox_integration.py
git commit -m "feat(opensandbox): derive health-probe scheme from OPEN_SANDBOX_PROTOCOL (item H)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Execute client-side deadline + async offload (items A + B)

`OpensandboxBackend.execute` runs the blocking sync SSE read with only a server-side timeout, and neither `execute` nor `aexecute` is overridden — so the inherited async path can block the event loop and a wedged command can pin a worker forever. Fix both: factor the blocking run into a shared helper, and override `aexecute` to run it via `asyncio.wait_for(asyncio.to_thread(...), deadline)` where `deadline = server_timeout + grace`. On deadline overrun, return exit code 124 and abandon the wedged thread (no join) — the same realized behavior as langsmith's `TimeoutLangSmithSandbox` (`pool.shutdown(wait=False)`). The server-side `RunCommandOpts` timeout remains the real enforcement; the client deadline is the backstop for when it fails to return.

> **Why not `commands.interrupt`?** It needs an `execution_id`, which the blocking `run()` only returns after completion. Obtaining one requires background mode (`create_session`/`run_in_session`) — a larger change deliberately out of scope. Abandon-thread + 124 is the spec-approved fallback.

**Files:**
- Modify: `agent/integrations/opensandbox.py` (add `import asyncio`; `DEFAULT_EXECUTE_CLIENT_GRACE_SECONDS`; `OpensandboxBackend._run_blocking`, `_effective_timeout`; grace in `__init__`; override `aexecute`; simplify `execute`)
- Test: `tests/test_opensandbox_integration.py`

**Interfaces:**
- Consumes: `self._sandbox.commands.run`, `RunCommandOpts`, `self._command_timeout_seconds` (existing).
- Produces:
  - `OpensandboxBackend._effective_timeout(self, timeout: int | None) -> int`
  - `OpensandboxBackend._run_blocking(self, command: str, effective: int) -> ExecuteResponse`
  - `OpensandboxBackend.execute(self, command, *, timeout=None) -> ExecuteResponse` (unchanged signature)
  - `OpensandboxBackend.aexecute(self, command, *, timeout=None) -> ExecuteResponse` (new override; on deadline overrun returns `ExecuteResponse(exit_code=124, ...)`)
  - `self._execute_client_grace_seconds: int` (from `OPEN_SANDBOX_EXECUTE_CLIENT_GRACE_SECONDS`, default 30, must be >= 0)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_opensandbox_integration.py` (add `import threading` at the top of the file if absent):

```python
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
```

Add this small helper near the other module-loading helpers in the test file (it builds a fake sandbox with a preset execution):

```python
def _make_backend_sandbox(osb, *, exit_code=0, stdout=None, stderr=None):
    sandbox = _FakeSandboxSync(sandbox_id="uuid-exec")
    sandbox.next_execution = _FakeExecution(exit_code, stdout=stdout, stderr=stderr)
    return sandbox
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest -vvv tests/test_opensandbox_integration.py -k "aexecute"`
Expected: FAIL — `test_aexecute_returns_124_on_client_deadline` fails because `aexecute` is inherited (no client deadline) and hangs / does not return 124.

- [ ] **Step 3: Write minimal implementation**

In `agent/integrations/opensandbox.py`:

Add `import asyncio` to the imports, and a constant near the other defaults:

```python
DEFAULT_EXECUTE_CLIENT_GRACE_SECONDS = 30
```

In `OpensandboxBackend.__init__`, capture the grace window (accept 0):

```python
        grace = _parse_int_env(
            "OPEN_SANDBOX_EXECUTE_CLIENT_GRACE_SECONDS",
            DEFAULT_EXECUTE_CLIENT_GRACE_SECONDS,
        )
        if grace < 0:
            raise ValueError("OPEN_SANDBOX_EXECUTE_CLIENT_GRACE_SECONDS must be >= 0")
        self._execute_client_grace_seconds = grace
```

Replace the existing `execute` method with the factored form plus the async override:

```python
    def _effective_timeout(self, timeout: int | None) -> int:
        # timeout=0/None means "no client timeout" to deepagents; we still cap
        # it to the configured command timeout so a command can't run unbounded.
        return timeout if timeout else self._command_timeout_seconds

    def _run_blocking(self, command: str, effective: int) -> ExecuteResponse:
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

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        return self._run_blocking(command, self._effective_timeout(timeout))

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        effective = self._effective_timeout(timeout)
        deadline = effective + self._execute_client_grace_seconds
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._run_blocking, command, effective),
                timeout=deadline,
            )
        except TimeoutError:
            # The sync SSE read has no client deadline; the worker thread is
            # abandoned (not joined), exactly like langsmith's wedged-command
            # handling. The server-side RunCommandOpts timeout still reaps it.
            logger.warning(
                "OpenSandbox command exceeded client deadline of %ss; abandoning worker",
                deadline,
            )
            return ExecuteResponse(
                output=f"Command exceeded the client-side deadline of {deadline}s and was abandoned.",
                exit_code=124,
                truncated=False,
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -vvv tests/test_opensandbox_integration.py -k "execute"`
Expected: PASS — the new `aexecute` tests pass and all existing `test_execute_*` tests still pass (they call `execute`, whose behavior is unchanged because `_run_blocking` is the old body verbatim).

- [ ] **Step 5: Lint and commit**

```bash
make lint
git add agent/integrations/opensandbox.py tests/test_opensandbox_integration.py
git commit -m "feat(opensandbox): client-side execute deadline + async offload (items A/B)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Write GitHub credentials via the file API (item G)

`configure_github_auth` interpolates the token into a `git config` / `printf` shell command, exposing it in the sandbox process list. Rewrite it to write the credential files via the provider-agnostic `upload_files(...)` protocol method, so the token only ever travels in a file body. Put the `insteadOf` rewrite in a **dedicated git include file** (rewritten wholesale each call — no stale-section shell dance) and register that file with a **token-free** `git config` call, so the bot's `user.name`/`user.email` in `~/.gitconfig` are never clobbered (the mid-run refresh path does not re-apply identity).

**Files:**
- Modify: `agent/utils/sandbox_github_auth.py` (replace the shell-command body with file-API writes)
- Test: `tests/test_sandbox_github_auth.py`

**Interfaces:**
- Consumes: `SandboxBackendProtocol.upload_files(list[tuple[str, bytes]]) -> list[FileUploadResponse]`, `.execute(...)`, `.id`; `_TOKEN_RE` (existing).
- Produces:
  - Module constants `INSTEADOF_INCLUDE_PATH = "/root/.config/git/insteadof.gitconfig"`, `GH_HOSTS_PATH = "/root/.config/gh/hosts.yml"`
  - `_credential_files(token: str) -> list[tuple[str, bytes]]`
  - `configure_github_auth(sandbox_backend, token)` — same signature; now writes via `upload_files` + a token-free `git config --global --replace-all include.path`.

- [ ] **Step 1: Rewrite the failing test**

Replace the `_FakeBackend` and `TestConfigureGithubAuth.test_writes_insteadof_and_hosts_yml` in `tests/test_sandbox_github_auth.py`. First extend the fake to record uploads:

```python
from deepagents.backends.protocol import ExecuteResponse, FileUploadResponse


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
```

Then the new assertions:

```python
class TestConfigureGithubAuth:
    def test_writes_credentials_via_file_api(self) -> None:
        backend = _FakeBackend()

        configure_github_auth(backend, "ghs_token123")

        # Exactly one upload batch with the two credential files.
        assert len(backend.uploads) == 1
        written = {path: data for path, data in backend.uploads[0]}
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest -vvv tests/test_sandbox_github_auth.py::TestConfigureGithubAuth`
Expected: FAIL — current implementation writes via a single `execute` shell command; `backend.uploads` is empty and the token appears in `backend.commands`.

- [ ] **Step 3: Write minimal implementation**

Replace the body of `agent/utils/sandbox_github_auth.py` below the imports and `_TOKEN_RE` with:

```python
INSTEADOF_INCLUDE_PATH = "/root/.config/git/insteadof.gitconfig"
GH_HOSTS_PATH = "/root/.config/gh/hosts.yml"


def _credential_files(token: str) -> list[tuple[str, bytes]]:
    """Build the (path, content) pairs written into the sandbox.

    The token lives only in file *content* (never on a command line). The
    insteadOf rule goes in a dedicated include file that is rewritten wholesale
    on every call, so a rotated token cannot leave a stale section behind and
    the bot identity in ~/.gitconfig is never touched.
    """
    insteadof = (
        f'[url "https://x-access-token:{token}@github.com/"]\n'
        "\tinsteadOf = https://github.com/\n"
    )
    hosts = (
        "github.com:\n"
        f"  oauth_token: {token}\n"
        "  git_protocol: https\n"
        "  user: x-access-token\n"
    )
    return [
        (INSTEADOF_INCLUDE_PATH, insteadof.encode()),
        (GH_HOSTS_PATH, hosts.encode()),
    ]


def configure_github_auth(sandbox_backend: SandboxBackendProtocol, token: str) -> None:
    """Write git + gh credentials into the sandbox via the file API."""
    if not _TOKEN_RE.fullmatch(token):
        # Defense in depth: never let an unexpected value reach a file/command.
        raise ValueError("GitHub token contains unexpected characters; refusing to write it")

    responses = sandbox_backend.upload_files(_credential_files(token))
    failed = [r.path for r in responses if r.error]
    if failed:
        # Deliberately omits error detail: it could echo file content.
        raise RuntimeError(
            f"Failed to write GitHub credential files in sandbox {sandbox_backend.id}: {failed}"
        )

    # Register the include file. Token-free and identity-safe: --replace-all sets
    # this single include.path without rewriting the rest of ~/.gitconfig.
    result = sandbox_backend.execute(
        f'git config --global --replace-all include.path "{INSTEADOF_INCLUDE_PATH}"'
    )
    if result.exit_code != 0:
        raise RuntimeError(
            f"Failed to register git include in sandbox {sandbox_backend.id} "
            f"(exit code {result.exit_code})"
        )
```

Remove the now-unused `_REMOVE_STALE_SECTIONS` constant. Update the module docstring's first paragraph to describe the file-API write (drop the "gh wrapper strips the dummy token" detail only if inaccurate — keep the parts that are still true).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -vvv tests/test_sandbox_github_auth.py`
Expected: PASS. If other tests in the file assert the old 4-part `&&` command shape, update them to the new upload/include shape (they exercise the same function).

- [ ] **Step 5: Lint and commit**

```bash
make lint
git add agent/utils/sandbox_github_auth.py tests/test_sandbox_github_auth.py
git commit -m "feat(opensandbox): write GitHub creds via file API, off the command line (item G)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Retry the auth-config write (item C)

A single transient failure writing credentials currently fails the run. langsmith's `_configure_github_proxy` retries with backoff; give `configure_github_auth` the same treatment. Retry the whole write on `RuntimeError` (the transient infra failure raised in Task 3), a small fixed number of times with backoff; the `ValueError` token-validation check stays outside the loop (never retried).

**Files:**
- Modify: `agent/utils/sandbox_github_auth.py` (wrap the write in a retry loop)
- Test: `tests/test_sandbox_github_auth.py`

**Interfaces:**
- Consumes: `configure_github_auth` internals from Task 3.
- Produces:
  - Module constants `AUTH_CONFIG_MAX_ATTEMPTS = 3`, `AUTH_CONFIG_RETRY_DELAYS = (0.5, 1.0)`
  - `_write_credentials_once(sandbox_backend, token) -> None` (the Task-3 write body)
  - `configure_github_auth` retries `_write_credentials_once` on `RuntimeError`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_sandbox_github_auth.py`:

```python
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

        with pytest.raises(RuntimeError, match="Failed to write GitHub credential files"):
            configure_github_auth(backend, "ghs_token123")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest -vvv tests/test_sandbox_github_auth.py::TestConfigureGithubAuthRetry`
Expected: FAIL — no retry yet (`test_retries_then_succeeds` raises on the first failure; and `auth.time` may not exist).

- [ ] **Step 3: Write minimal implementation**

In `agent/utils/sandbox_github_auth.py`, add `import logging` and `import time` at the top (and `logger = logging.getLogger(__name__)` if not present). Add constants near the paths:

```python
AUTH_CONFIG_MAX_ATTEMPTS = 3
AUTH_CONFIG_RETRY_DELAYS = (0.5, 1.0)
```

Rename the Task-3 write body to `_write_credentials_once` and make `configure_github_auth` the retry wrapper:

```python
def _write_credentials_once(sandbox_backend: SandboxBackendProtocol, token: str) -> None:
    responses = sandbox_backend.upload_files(_credential_files(token))
    failed = [r.path for r in responses if r.error]
    if failed:
        raise RuntimeError(
            f"Failed to write GitHub credential files in sandbox {sandbox_backend.id}: {failed}"
        )
    result = sandbox_backend.execute(
        f'git config --global --replace-all include.path "{INSTEADOF_INCLUDE_PATH}"'
    )
    if result.exit_code != 0:
        raise RuntimeError(
            f"Failed to register git include in sandbox {sandbox_backend.id} "
            f"(exit code {result.exit_code})"
        )


def configure_github_auth(sandbox_backend: SandboxBackendProtocol, token: str) -> None:
    """Write git + gh credentials into the sandbox via the file API, with retry."""
    if not _TOKEN_RE.fullmatch(token):
        raise ValueError("GitHub token contains unexpected characters; refusing to write it")

    for attempt in range(AUTH_CONFIG_MAX_ATTEMPTS):
        try:
            _write_credentials_once(sandbox_backend, token)
            return
        except RuntimeError:
            if attempt == AUTH_CONFIG_MAX_ATTEMPTS - 1:
                raise
            delay = AUTH_CONFIG_RETRY_DELAYS[min(attempt, len(AUTH_CONFIG_RETRY_DELAYS) - 1)]
            logger.warning(
                "GitHub auth write failed for sandbox %s (attempt %d/%d); retrying in %.1fs",
                sandbox_backend.id,
                attempt + 1,
                AUTH_CONFIG_MAX_ATTEMPTS,
                delay,
            )
            time.sleep(delay)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -vvv tests/test_sandbox_github_auth.py`
Expected: PASS — retry tests pass; Task-3 tests still pass (happy path takes the first attempt).

- [ ] **Step 5: Lint and commit**

```bash
make lint
git add agent/utils/sandbox_github_auth.py tests/test_sandbox_github_auth.py
git commit -m "feat(opensandbox): retry the GitHub auth-config write with backoff (item C)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Renew the sandbox TTL on mid-run refresh (item F)

opensandbox uses an absolute TTL renewed only at run boundaries, so a run longer than the default TTL gets its GitHub token kept fresh while the sandbox is reaped underneath it. Tie TTL renewal to the (near-expiry, ~hourly) token rotation in `refresh_proxy_token`: whenever the opensandbox credentials are rewritten mid-run, also slide the sandbox TTL. This keeps the sandbox alive as long as the run keeps calling the model — the same "alive while active" behavior langsmith gets from its idle TTL — with no separate expiry tracking.

**Files:**
- Modify: `agent/utils/github_proxy.py` (`refresh_proxy_token`, opensandbox branch)
- Test: `tests/test_github_proxy_refresh.py`

**Interfaces:**
- Consumes: `unwrap_sandbox_backend`, `SANDBOX_BACKENDS`, `configure_github_auth`, `get_github_app_installation_token_with_expiry` (existing); `OpensandboxBackend.renew_ttl()` (existing, discovered via `getattr`).
- Produces: after the opensandbox credential rewrite, `refresh_proxy_token` calls `renew_ttl()` on the unwrapped backend (best-effort; a failed renew is logged, not raised).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_github_proxy_refresh.py` (follow the file's existing mocking style):

```python
class TestMidRunTtlRenewal:
    @pytest.mark.asyncio
    async def test_opensandbox_refresh_renews_ttl(self, monkeypatch) -> None:
        now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        record_proxy_token_expiry("thread-1", now + timedelta(minutes=1))

        backend = MagicMock()
        backend.id = "sbx-1"
        backend.renew_ttl = MagicMock()
        # SANDBOX_BACKENDS stores the proxy; unwrap returns it as-is for a MagicMock.
        monkeypatch.setitem(github_proxy.SANDBOX_BACKENDS, "thread-1", backend)
        monkeypatch.setattr(github_proxy, "unwrap_sandbox_backend", lambda b: b)

        async def fake_token(**kwargs):
            return "ghs_fresh", now + timedelta(hours=1)

        monkeypatch.setattr(
            github_proxy, "get_github_app_installation_token_with_expiry", fake_token
        )
        monkeypatch.setattr(github_proxy, "configure_github_auth", MagicMock())

        with patch.dict("os.environ", {"SANDBOX_TYPE": "opensandbox"}):
            # configure_github_auth is imported lazily inside the function; patch there too.
            with patch(
                "agent.utils.sandbox_github_auth.configure_github_auth", MagicMock()
            ):
                refreshed = await github_proxy.refresh_proxy_token("thread-1")

        assert refreshed is True
        backend.renew_ttl.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest -vvv tests/test_github_proxy_refresh.py::TestMidRunTtlRenewal`
Expected: FAIL — `renew_ttl` is never called (`refresh_proxy_token` only rewrites credentials).

- [ ] **Step 3: Write minimal implementation**

In `agent/utils/github_proxy.py`, in `refresh_proxy_token`, extend the opensandbox branch. Current:

```python
    current_backend = unwrap_sandbox_backend(sandbox_backend)
    if sandbox_type == "opensandbox":
        from .sandbox_github_auth import configure_github_auth

        await asyncio.to_thread(configure_github_auth, current_backend, token)
    else:
        from ..integrations.langsmith import _configure_github_proxy

        await asyncio.to_thread(_configure_github_proxy, current_backend.id, token)
```

Replace the opensandbox branch body with:

```python
    if sandbox_type == "opensandbox":
        from .sandbox_github_auth import configure_github_auth

        await asyncio.to_thread(configure_github_auth, current_backend, token)
        # Absolute TTL: slide it on the same ~hourly cadence as token rotation so
        # a long single run isn't reaped mid-run (langsmith gets this free via its
        # idle TTL). Best-effort: the credentials already wrote, so a failed renew
        # is logged, not fatal.
        renew = getattr(current_backend, "renew_ttl", None)
        if renew is not None:
            try:
                await asyncio.to_thread(renew)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Failed to renew OpenSandbox TTL during mid-run refresh for thread %s",
                    thread_id,
                    exc_info=True,
                )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -vvv tests/test_github_proxy_refresh.py`
Expected: PASS — the new renewal test passes and existing refresh tests are unaffected (langsmith backends have no `renew_ttl`, so the `getattr` guard is a no-op for them).

- [ ] **Step 5: Lint and commit**

```bash
make lint
git add agent/utils/github_proxy.py tests/test_github_proxy_refresh.py
git commit -m "feat(opensandbox): renew sandbox TTL on mid-run credential refresh (item F)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Final verification

- [ ] **Run the full suite:** `make test`
      Expected: all tests pass. Pay attention to `tests/test_opensandbox_integration.py`, `tests/test_sandbox_github_auth.py`, `tests/test_github_proxy_refresh.py`, and anything importing `configure_github_auth` (e.g. analyzer/server wiring tests).
- [ ] **Lint clean:** `make lint`
- [ ] **Confirm no other-provider drift:** grep the diff for any change outside an `opensandbox` branch or opensandbox-only module; there should be none (item F's `getattr` guard is the only shared-file edit and is a no-op for other providers).

## Self-review notes

- **Spec coverage:** A+B → Task 2; C → Task 4; F → Task 5; G → Task 3; H → Task 1. D and E are explicitly out of scope (Spec 2). All six in-scope items are covered.
- **Design refinements vs spec (intentional):** (1) Item G writes a dedicated git *include file* rather than rewriting `~/.gitconfig` wholesale, to avoid clobbering the bot identity the mid-run refresh path does not re-apply. (2) Item A uses abandon-thread + 124 rather than `commands.interrupt`, because interrupt needs an execution id that only background mode provides (out of scope). (3) Item H fixes the scheme (the real bug) and leaves the `/health` path unchanged pending a live control-plane check, rather than switching to `/ping` on uncertain docs.
- **Type consistency:** `configure_github_auth(sandbox_backend, token)` signature is unchanged across Tasks 3–4; `_write_credentials_once` is the shared body; `INSTEADOF_INCLUDE_PATH`/`GH_HOSTS_PATH` are referenced consistently. `_effective_timeout`/`_run_blocking` names match between `execute` and `aexecute`.