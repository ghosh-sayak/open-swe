# OpenSandbox Sandbox Backend Integration

**Status:** Approved (design) — implementation not started
**Date:** 2026-07-08
**Deciders:** Sayak Ghosh
**Branch:** `poc/k8s-sandbox`
**Related plan:** `plans/daytona-sandbox-recreate-on-unreachable.md` (on `poc/daytona`) — the ping-guard fix this plan generalizes.

---

## 1. Objective & scope

Add a new sandbox provider so that `SANDBOX_TYPE=opensandbox` gives **behavioral parity with the
existing providers** (same lifecycle, same failure semantics, same tool surface), running against:

- a **local OpenSandbox server in Docker Compose** for dev, and
- a **Kubernetes-deployed OpenSandbox server** for prod,

with **no application code change between the two** — only endpoint/config differences.

Out of scope (deliberately deferred, see §12): per-repo warm snapshots, the OpenSandbox Credential
Vault (kept as a documented future alternative), and any reviewer-graph circuit breaker (absent
upstream even for LangSmith).

---

## 2. Background — the existing sandbox layer (verified against this checkout)

**Provider registry.** `agent/utils/sandbox.py` holds `SANDBOX_FACTORIES: dict[str, tuple[module, fn]]`
(lazy `importlib`). Factory signature is `Callable[[str | None], SandboxBackendProtocol]` — one
positional `sandbox_id`. `create_sandbox()` reads `os.getenv("SANDBOX_TYPE", "langsmith")`. The
`snapshot_id` kwarg is forwarded **only for langsmith**. `validate_sandbox_startup_config()`
(FastAPI lifespan) validates **only langsmith** today.

**deepagents backend contract** (`deepagents.backends`, installed v0.6.8). A provider subclassing
`deepagents.backends.sandbox.BaseSandbox` must implement exactly **four** members:

- `execute(command: str, *, timeout: int | None = None) -> ExecuteResponse`
  (`ExecuteResponse.output` = combined stdout+stderr, `.exit_code: int | None`, `.truncated: bool`)
- `id -> str` (property)
- `upload_files(list[tuple[str, bytes]]) -> list[FileUploadResponse]` — **partial-success** (per-file
  error entries, never raises) and **must create parent directories**
- `download_files(list[str]) -> list[FileDownloadResponse]` — partial-success, never raises

`ls / read / write / edit / grep / glob` are provided **for free** by `BaseSandbox`, implemented by
running small `python3`/`grep` scripts inside the sandbox through `execute()`. `aexecute` defaults to
`asyncio.to_thread(execute, …)`. **Precondition:** the sandbox image must contain `python3` and
`grep`. Our image satisfies this (`python:3.12-slim-bookworm` base + `ripgrep`/system `grep`).

**Provider precedent.** `daytona.py` (~29 lines), `modal.py`, `runloop.py` are thin wrappers around
installable `langchain_*` classes that subclass `BaseSandbox`. `local.py` uses
`deepagents.backends.LocalShellBackend`. All are **synchronous** implementations — `aexecute` runs
them on a worker thread. This is the established shape.

**Sandbox lifecycle** (`agent/server.py`, `agent/utils/sandbox_state.py`). `ensure_sandbox_for_thread`
handles four states (cached→ping, `__creating__` sentinel poll, no-sandbox→create, metadata-id→
reconnect). `SandboxBackendProxy` is a stable per-thread facade; `set_sandbox_backend` swaps the
target in place so all holders (deepagents tools) see a recreated sandbox instantly. `git config
--global user.name/email` is re-applied every run. **This proxy/swap machinery is already fully
provider-agnostic.**

---

## 3. OpenSandbox — capability summary (verified against source @ `e435df40`, 2026-07-06)

- PyPI `opensandbox` **0.1.13** (`requires-python >=3.10`), Apache-2.0, Alibaba-backed, very active
  (repo self-labels "Alpha"). Async-first SDK **with a hand-written blocking sync twin**
  (`opensandbox.sync.SandboxSync`) — zero asyncio in its call path, thread-safe, explicitly designed
  for "call me from a worker thread." A 1:1 API to the async classes.
- Lifecycle: `SandboxSync.create(image=…, timeout=<TTL>, ready_timeout=…, resource=…, env=…,
  entrypoint=…)`, `SandboxSync.connect(sandbox_id, …)` (cross-process reconnect by id), `renew(timedelta)`
  (extends expiry = now+TTL), `kill()` (remote), `close()` (local transport only), `is_healthy()`
  (never raises). Sandbox **IDs are plain UUID4** (no prefix).
- Exec: `sandbox.commands.run(cmd, opts=RunCommandOpts(timeout=timedelta, envs=…, working_directory=…))`
  → `Execution` with `.exit_code` (0 ok / real code / **-1 on timeout-kill** / `None` on spawn failure —
  **no exception on non-zero exit**) and `.logs.stdout` / `.logs.stderr` (timestamped messages).
- Files (native): `files.write_file(path, str|bytes)`, `files.write_files([WriteEntry])`,
  `files.read_bytes(path)`, `read_file`, `create_directories`, etc.
- Exceptions (`opensandbox.exceptions`): `SandboxApiException` (has `.status_code`; 404 = gone/expired,
  401 = auth), `SandboxInternalException` (network unreachable, wraps httpx errors),
  `SandboxReadyTimeoutException`, `InvalidArgumentException` — clean typed hierarchy under
  `SandboxException`.
- Images: **arbitrary OCI, no registration step, no agent baked in** — the `execd` control daemon is
  injected at runtime (Docker: extracted from `execd_image`; k8s: initContainer). Docker runtime is
  **local-first**: `docker images.get(X)` → if present with matching platform, never pulls. So
  `docker build -t open-swe-sandbox:latest .` on the host daemon works **with no registry**.
  Image `ENTRYPOINT`/`CMD` are ignored (OpenSandbox force-overrides with its bootstrap + keepalive).
  `/opt/opensandbox` is a reserved path.
- Networking: control-plane on the server (`:8090` local); exec/file traffic normally goes direct to
  each sandbox's `execd`. `ConnectionConfig(use_server_proxy=True)` routes exec/file through the
  server — required when the client can't reach sandbox IPs (server-in-Docker/client-on-host; or
  open-swe outside a k8s cluster). This flag is what makes docker→k8s config-only.
- TTL: **absolute** (not idle), default **10 min** — too short for agent threads; extend via `renew()`
  or create with `timeout=None`.

---

## 4. Pools vs. Credential Vault — the decisive compatibility facts (verified in server source)

| Fact | Evidence |
|---|---|
| Server-side pools (`extensions.poolRef`) are **Kubernetes-only** | `docker_service.py:614-619` hard-rejects poolRef: *"poolRef is not supported by the Docker provider."* |
| Credential Vault (`credentialProxy`) **cannot combine with** `poolRef` | `api/schema.py:503` raises `ValueError("credentialProxy.enabled cannot be used together with poolRef.")` |
| Vault also **excludes** `snapshotId` + requires `networkPolicy` + `[egress].mode="dns+nft"` + Docker `network_mode="bridge"` | `schema.py:501`, `validators.py:606-623`, `networking.py:145-160` |
| K8s pool mode **honors per-claim env** | `batchsandbox_provider.py:_create_workload_from_pool` builds a `taskTemplate` from `env` (`:363-366`, `_build_task_template :464+`) |
| Pool CRD `Template` is a full `corev1.PodTemplateSpec` (supports `envFrom`/`secretKeyRef`) | `kubernetes/apis/sandbox/v1alpha1/pool_types.go:54` |
| SDK-side pool (`SandboxPoolSync`) is a **client-side eager-create** pool; works against any runtime; `PoolCreationSpec` has `env`/`network_policy`/`extensions` but **no `credential_proxy`** | `sdks/…/sync/pool.py:_build_warmup_sandbox/_direct_create`, `pool_types.py:217` |

**Conclusion:** the Vault and pools are mutually exclusive. Priority is pools ⇒ **Vault is dropped.**

---

## 5. Key decisions

### D1 — Implementation shape: in-repo `BaseSandbox` subclass (not a dependency, not a separate package)

Options considered:

- **(A) Depend on the PyPI `deepagents-opensandbox` package.** Rejected — it is published by an
  unverified third-party author with no linked source repo; unacceptable supply-chain risk for
  credential-adjacent infrastructure. (We may read it as an implementation *reference*; it is
  essentially the same `BaseSandbox` subclass, but has gaps — no `renew`, an extra HTTP round-trip
  for exit codes, loose error mapping.)
- **(B) Separate first-party package** (own PyPI/private registry). Rejected for now — release/version
  overhead with no second consumer.
- **(C, chosen) Self-contained `agent/integrations/opensandbox.py`** — one `OpensandboxBackend`
  subclassing `BaseSandbox`, ~200 lines, versioned and reviewed with the agent. "Maintained by us"
  with no second artifact. If upstream later ships an official `langchain_opensandbox`, migration is
  cheap (same internal shape).

`upload_files`/`download_files` use OpenSandbox's **native file API** (they're abstract; native is the
direct implementation). The six inherited ops (`read/write/edit/ls/grep/glob`) are **not** reimplemented
on the native API — their exact semantics (500 KiB read caps, offset/limit, edit uniqueness) are what
the agent's prompts are tuned against; reimplementing is risk with no measured payoff.

### D2 — Sync SDK via `asyncio.to_thread`

deepagents' contract is a **sync** `execute`; `aexecute` runs it on a worker thread. Use
`opensandbox.sync.SandboxSync` (loop-free blocking, thread-safe). The async SDK would force
`asyncio.run` per call, whose `httpx.AsyncClient` binds its pool to the first loop → "attached to a
different loop" on reuse. `SandboxSync` is exactly the daytona pattern. **Always pass a per-command
timeout** — the sync SSE read has no client-side timeout, so a hung command would pin a worker thread
forever.

### D3 — GitHub auth: `hosts.yml`-via-exec, token from Akeyless into the agent runtime

The prompts hardcode `GH_TOKEN=dummy gh …` in ~33 places. Auth mirrors the **daytona-proven pattern**:
after create/claim, open-swe writes into the sandbox **via the exec channel**:

1. `git config --global url."https://x-access-token:<token>@github.com/".insteadOf "https://github.com/"`
2. `~/.config/gh/hosts.yml` with the installation token (exact commands mirror
   `poc/daytona`'s `_create_sandbox_with_proxy` daytona branch).

The gh-wrapper in the sandbox image (`unset GH_TOKEN GITHUB_TOKEN; exec /usr/bin/gh`) strips the
prompts' `dummy` token so `gh` falls back to `hosts.yml`. GitHub App installation tokens are
**GitHub-capped at 1 h** (not configurable), so `hosts.yml`/`insteadOf` are rewritten at run-start
via `_refresh_github_proxy` (per run) AND mid-run via the before-model
`refresh_github_proxy_before_model` → `maybe_refresh_proxy_token` hook (provider-aware; rewrites
`hosts.yml` for opensandbox before the recorded expiry) so a single run longer than ~1 h keeps
valid credentials. Each rewrite removes any stale `x-access-token` `insteadOf` section first — git
resolves equal-length matches to the first section, so append-only would pin the expired token.

**Why not pod-env injection (the initial Akeyless-as-env idea):** (a) the inline `GH_TOKEN=dummy`
shadows any ambient `GH_TOKEN` per-command, so a pod env var is overridden on every call — the whole
convention is designed to fall through to `hosts.yml`; (b) pod env is static at pod creation, so a
pooled pod warm >1h holds a stale token and rotating the Secret does not update a running pod.
**Correct Akeyless placement:** Akeyless is the *source* of the GitHub App private key/token, injected
into the **open-swe agent runtime** (env var locally; Secret→env on the agent Deployment in k8s).
open-swe mints the installation token and propagates it into each sandbox over exec. This works for
pooled and non-pooled sandboxes identically because it's applied *after* claim.

This auth path is shared by the main agent and the analyzer graph (single helper — see §8).

### D4 — Pools: k8s server-side `poolRef` ON; local SDK-side pool OFF by default

- **K8s prod:** server-side **Pool CRD + `extensions.poolRef`**. Per-thread claim; per-claim env
  honored; `hosts.yml` applied post-claim via exec (rotation-safe). No Vault.
- **Local Docker:** `poolRef` unsupported → the only option is SDK-side `SandboxPoolSync` (eager-create,
  optional Redis for multi-worker). **Off by default** — the sandbox image is heavy (JDK+Kotlin+Gradle+
  LSP; a warmed idle sandbox holds multiple GB), local dev is single-user, and the 2 h sliding TTL
  already keeps a thread's sandbox warm between messages. Behind a config flag so it can be enabled.

### D5 — Error recovery: provider-agnostic (production-grade)

Today recovery is LangSmith-only across three layers (start-of-run ping `server.py:360`; mid-run
`tool_error_handler.py`; circuit breaker `sandbox_circuit_breaker.py`), keyed on
`langsmith.sandbox.SandboxClientError` and an `sb-[A-Za-z0-9-]+` id regex. Generalization:

- **Per-provider recoverability predicate.** Each integration module exports
  `is_recoverable_sandbox_error(exc) -> bool`; a helper in `agent/utils/sandbox.py`
  (`get_recoverable_predicate()`, keyed off `SANDBOX_TYPE` like `SANDBOX_FACTORIES`) resolves the
  active one. Consumers change from `except SandboxClientError` to `except Exception as e:` then
  `if predicate(e): recreate  else: <generic error payload / re-raise>`. A predicate (not a raw
  exception tuple) is used so OpenSandbox can distinguish `SandboxApiException` **404/5xx** (recreate)
  from **401/403** (auth — do not recreate). **Blanket `except Exception`-then-recreate is explicitly
  rejected** (recreation is destructive: the workspace is lost, forcing a re-clone; a plain `TypeError`
  must never trigger it — it goes to the generic path, surfaced to the LLM, no recreation).
  - langsmith: `isinstance(exc, SandboxClientError)` → **zero behavioral change** on that path.
  - opensandbox: `isinstance(exc, SandboxInternalException)` or `SandboxApiException` with
    `status_code in {404, 500, 502, 503, 504}`.
  - daytona (bonus fix): `isinstance(exc, DaytonaError)` excluding auth — closes the poc/daytona gap.
- **Structured circuit-breaker markers.** `tool_error_handler` emits `error_class: "sandbox_unreachable"`
  and an explicit `sandbox_id` field (read from the proxy — no text scraping) in its payloads. The
  circuit breaker matches those JSON fields instead of the `"SandboxClientError"` substring and `sb-`
  regex → provider-agnostic; UUIDs work with no regex. The old substring/regex stays as a legacy
  fallback so existing behavior/tests are preserved.
- **Existing tests unchanged** (the zero-delta proof for the langsmith path); new variants cover UUID
  ids + opensandbox exceptions.

### D6 — TTL: 7200 s sliding, renew-on-touch, mandatory per-command timeout

Create with `timeout=OPEN_SANDBOX_TTL_SECONDS` (default **7200**); call `renew(TTL)` on every
reconnect/ping in `ensure_sandbox_for_thread` so the window slides with activity and abandoned threads
self-expire. Expired-on-reconnect surfaces as the 404-shaped exception → existing recreate path. Every
`execute` gets a server-side command timeout (`OPEN_SANDBOX_COMMAND_TIMEOUT_SECONDS`, default 1800) so
a hung command can't pin a worker thread. (For context: langsmith idle-TTL 2 h / delete-after-stop 24 h;
daytona auto-stop 15 min. 7200 sliding matches langsmith's intent.)

### D7 — Sandbox image: copy the init-swe Dockerfile in, keep the gh-wrapper

Copy `init-swe/infra/opensandbox/local-osb/custom-sandbox-image/open-swe-sandbox.Dockerfile`
→ new `open-swe-k8s/sandbox-image/open-swe-sandbox.Dockerfile` (+ `README.md` with the build command).
**No Dockerfile edits** — it already has `ca-certificates`, `python3`, `grep`/`ripgrep`, `git`, `gh`, and
the gh-wrapper (`unset GH_TOKEN GITHUB_TOKEN; exec /usr/bin/gh`) which is exactly right for the
`hosts.yml` auth path. No secrets are baked in (and none ever will be — the token is written at runtime
over exec, refreshed hourly). `CMD ["bash"]` is harmless dead code (OpenSandbox ignores it).

---

## 6. Parity matrix (protocol behavior → OpenSandbox implementation)

| Behavior | OpenSandbox implementation | Confidence |
|---|---|---|
| `id` | `SandboxSync.id` (UUID4) | solid |
| `execute(cmd, timeout)` | `commands.run(cmd, opts=RunCommandOpts(timeout=timedelta))`; use `Execution.exit_code` directly; merge stdout+stderr into `output` | API solid; interleaving/timeout exit-code (-1) **needs test** |
| `aexecute` | inherited `to_thread(execute)` | solid |
| `upload_files` | native `files.write_file(s)`, per-file partial-success + parent-dir creation | needs test (partial-success is on us) |
| `download_files` | native `files.read_bytes`, partial-success | solid |
| `ls/read/write/edit/grep/glob` | inherited from `BaseSandbox` (image has `python3`+`grep`) | solid |
| Create | `SandboxSync.create(image, timeout=TTL, resource=…)` | solid |
| Reconnect by persisted id | `SandboxSync.connect(id)` + `renew(TTL)` | solid; renew needs test |
| Start-of-run ping recovery | `is_healthy()` / `execute("echo ok")` → predicate-gated recreate | solid |
| GitHub auth | `hosts.yml` + `insteadOf` via exec, refreshed hourly | solid (daytona-proven) |
| Circuit breaker / mid-run recreation | provider-agnostic via D5 | solid after D5 |
| Per-repo snapshot | deferred (langsmith-only today) | gap (intentional) |

---

## 7. Compatibility matrix (deployment)

| Capability | Local Docker | K8s prod |
|---|---|---|
| Server-side pool (`poolRef`) | ❌ rejected by Docker provider | ✅ Pool CRD + `poolRef` (default ON) |
| SDK-side pool (`SandboxPoolSync`) | ✅ (default OFF) | ✅ (unused; server-side preferred) |
| Credential Vault | ✅ but excludes pools → **not used** | ✅ but excludes pools → **not used** |
| GitHub auth (`hosts.yml` via exec) | ✅ | ✅ |
| `use_server_proxy` | `True` (server in Docker, client on host) | `True` when open-swe runs outside cluster |

---

## 8. Detailed design — files

**New:**

- `agent/integrations/opensandbox.py`
  - `class OpensandboxBackend(BaseSandbox)` — `id`, `execute` (`commands.run`, `Execution.exit_code`,
    merged output), `upload_files`/`download_files` (native FS, partial-success, mkdir parents),
    `kill`/`close`. Built on `SandboxSync`.
  - `def create_opensandbox_sandbox(sandbox_id: str | None) -> SandboxBackendProtocol` — fail-fast env
    validation; **create-path seam**:
    - `sandbox_id` set → `SandboxSync.connect(id)` + `renew(TTL)`
    - cold + k8s pooling → `create(..., extensions={"poolRef": OPEN_SANDBOX_POOL_REF})`
    - cold + local SDK-pool → `pool.acquire()`
    - cold + no pool → `SandboxSync.create(...)`
  - `def is_recoverable_sandbox_error(exc) -> bool` (see D5).
- `agent/utils/sandbox_github_auth.py` — `configure_github_auth(backend, token)` writing `hosts.yml` +
  `insteadOf` via `backend.execute(...)`. Shared by `server.py` and `analyzer.py` (D3, Q6.2).
- `sandbox-image/open-swe-sandbox.Dockerfile` (copy of init-swe) + `sandbox-image/README.md`.
- `tests/test_opensandbox_integration.py` (unit) and `tests/integration_tests/test_opensandbox_e2e.py`
  (real container, gated by `OPENSANDBOX_INTEGRATION=1`).

**Modified:**

- `agent/utils/sandbox.py` — registry entry
  `"opensandbox": ("agent.integrations.opensandbox", "create_opensandbox_sandbox")`;
  `get_recoverable_predicate()` helper; opensandbox branch in `validate_sandbox_startup_config`
  (env present + server `/health` reachable at startup).
- `agent/server.py` — opensandbox branch in `_create_sandbox_with_proxy` / `_refresh_github_proxy`
  calling `configure_github_auth`; ping guard + reconnect use the recoverability predicate; `renew`
  on reconnect/ping; wire the pooling create-seam. Both auth branches also `record_proxy_token_expiry`
  so the mid-run before-model refresh fires for long (>1h) single runs (D3).
- `agent/utils/github_proxy.py` — `refresh_proxy_token` is provider-aware: langsmith re-patches the
  proxy API, opensandbox rewrites `hosts.yml` via `configure_github_auth`. `maybe_refresh_proxy_token`
  gate widened from langsmith-only to `{langsmith, opensandbox}`.
- `agent/middleware/tool_error_handler.py` — predicate-gated recreation; structured
  `error_class`/`sandbox_id` payload fields.
- `agent/middleware/sandbox_circuit_breaker.py` — match structured fields (legacy substring/regex as
  fallback).
- `agent/integrations/langsmith.py`, `daytona.py`, `opensandbox.py` — export
  `is_recoverable_sandbox_error`.
- `agent/analyzer.py` — call the shared `configure_github_auth` for non-langsmith providers.
- `pyproject.toml` — add `opensandbox>=0.1.13,<0.2` (and `opensandbox[pool-redis]` only if local
  SDK-pool + Redis is enabled).
- `customization/` — runbook section documenting `SANDBOX_TYPE=opensandbox`, env vars, the init-swe
  compose, and the build/deploy flow (no compose duplicated into this repo — Q4).

**Env vars:**

| Var | Read by | Default |
|---|---|---|
| `OPEN_SANDBOX_DOMAIN` | SDK + our validation | `localhost:8080` (local uses `localhost:8090`) |
| `OPEN_SANDBOX_API_KEY` | SDK + our validation | required (server enforces) |
| `OPEN_SANDBOX_IMAGE` | factory | `open-swe-sandbox:latest` |
| `OPEN_SANDBOX_TTL_SECONDS` | factory (`timeout=` + `renew`) | `7200` |
| `OPEN_SANDBOX_COMMAND_TIMEOUT_SECONDS` | `execute` | `1800` |
| `OPEN_SANDBOX_USE_SERVER_PROXY` | factory → `ConnectionConfig` | `false` (`true` local; `true` out-of-cluster k8s) |
| `OPEN_SANDBOX_CPU` / `OPEN_SANDBOX_MEMORY` | factory → `resource` | `2` / `4Gi` |
| `OPEN_SANDBOX_POOL_ENABLED` | factory | `false` (local) |
| `OPEN_SANDBOX_POOL_REF` | factory (k8s) | unset (set to pool name in prod) |

GitHub App creds (`GITHUB_APP_ID`, installation id, private key) already exist; Akeyless injects the
private key/token into the **agent runtime** env.

---

## 9. Local-dev story

Canonical deployment stays at `init-swe/infra/opensandbox/local-osb/` (Q4) — **unchanged**, keeping its
`local-net` user-defined bridge (no Vault ⇒ no `bridge`/`dns+nft` requirement ⇒ nothing wrong with
`local-net`). Steps a teammate follows (documented in `customization/`):

1. `docker network create local-net` (if absent); `docker pull opensandbox/execd:v1.0.20`.
2. `cd .../local-osb/custom-sandbox-image && docker build -f open-swe-sandbox.Dockerfile -t open-swe-sandbox:latest .`
3. `cd .../local-osb && docker compose up -d`; `curl localhost:8090/health` → `{"status":"healthy"}`.
4. `.env`: `SANDBOX_TYPE=opensandbox`, `OPEN_SANDBOX_DOMAIN=localhost:8090`, `OPEN_SANDBOX_API_KEY=…`,
   `OPEN_SANDBOX_IMAGE=open-swe-sandbox:latest`, `OPEN_SANDBOX_USE_SERVER_PROXY=true`.
5. `make dev`, fresh thread.

Local pooling off by default (D4). No registry required (D3/§3 local-first pull).

---

## 10. K8s story

Deploy the OpenSandbox umbrella Helm chart (server + controller, `runtime.type="kubernetes"`,
`workload_provider="batchsandbox"`). Create a **Pool CRD** for pre-warmed pods; set
`OPEN_SANDBOX_POOL_REF` to its name. Akeyless injects the GitHub App key into the agent Deployment's
env (Secret→env). open-swe changes are **config only**: `OPEN_SANDBOX_DOMAIN` (server svc/ingress),
`OPEN_SANDBOX_API_KEY`, `OPEN_SANDBOX_USE_SERVER_PROXY=true` if open-swe runs outside the cluster,
`OPEN_SANDBOX_POOL_ENABLED=true`, `OPEN_SANDBOX_POOL_REF=<pool>`. Same REST API, same auth header, same
UUID ids as local — genuinely no code change. Raise `kubernetes.sandbox_create_timeout_seconds` and/or
preload the sandbox image on nodes for large first pulls.

---

## 11. Test plan

- **Unit** (`sys.modules`-fake style, no network): env fail-fast; create-vs-connect dispatch on
  `sandbox_id`; `renew` called on reconnect; `ExecuteResponse` mapping (exit code incl. `-1` timeout,
  stdout/stderr merge, timeout→`RunCommandOpts`); upload/download partial-success + mkdir parents;
  `is_recoverable_sandbox_error` truth table (404/5xx vs 401); registry dispatch
  (`SANDBOX_TYPE=opensandbox`). Recovery-middleware: keep the 4 existing langsmith tests unchanged;
  add UUID-id + opensandbox-exception variants. Mid-run refresh: `maybe_refresh_proxy_token` fires
  for opensandbox and `refresh_proxy_token` rewrites `hosts.yml` (via `configure_github_auth`), not
  the proxy API.
- **Integration** (`tests/integration_tests/test_opensandbox_e2e.py`, `OPENSANDBOX_INTEGRATION=1`,
  populates the currently-empty `make integration_tests`): against a real local container —
  `execute("echo ok")` exit code; write→read→edit→grep→glob round-trip; upload/download bytes;
  reconnect-by-id into a second backend; `renew`; kill.
- **Run-and-observe** (per project rule): `docker compose up`, one `SANDBOX_TYPE=opensandbox make dev`
  thread that clones a repo and runs a `gh` call to prove the `hosts.yml` auth path; paste real output.

---

## 12. Risks, open questions, deferred

1. **Alpha maturity.** OpenSandbox is ~7 months old, self-labeled Alpha. The surface we depend on
   (create/connect/run/files/renew) is small and stable-looking; pin `opensandbox>=0.1.13,<0.2`.
   Expect occasional breakage on server-image upgrades.
2. **`local-net` + Vault** — only relevant if the Vault is ever revived: it would require
   `network_mode="bridge"` + `[egress].mode="dns+nft"` and could not coexist with pools.
3. **ADR bootstrapping (open question).** `docs/adr/` does not exist in this repo. The load-bearing
   decisions here (provider contract shape, env-var names, in-repo vs package, auth mechanism) are
   ADR-shaped, but running `/adr-kit:init` is a repo-wide, human-in-the-loop change (edits `CLAUDE.md`,
   installs a pre-commit hook) and is **not** triggered as part of this work. The ADR content lives
   inline in this plan (§5). Decide separately whether to bootstrap adr-kit.
4. **Pre-existing test bug (informational).** `tests/test_daytona_integration.py:49` asserts the old
   default snapshot string and cannot pass on `poc/daytona` as written; unrelated to this branch but
   worth fixing when those branches reconcile.
5. **Deferred — per-repo warm snapshots.** New threads pay image-boot + clone + dependency-warm.
   Mitigation now: the 2 h sliding TTL keeps active threads warm. Future: OpenSandbox native snapshots
   (`create_snapshot`/`snapshot_id`) per repo — the direct analogue of langsmith's per-repo snapshots
   (Vault-incompatible, but the Vault is dropped anyway).
6. **Deferred — reviewer graph circuit breaker.** Absent upstream even for langsmith; unchanged.

---

## 13. Rollback

Fully additive and env-gated. To disable: unset `SANDBOX_TYPE=opensandbox` (defaults back to
langsmith). The D5 recovery generalization is a no-op-equivalent for langsmith (predicate resolves to
exactly `SandboxClientError`; structured markers coexist with the legacy substring/regex fallback), so
it can ship independently. New files can be deleted; modified files revert cleanly (registry entry,
predicate helper, opensandbox branches are isolated additions).

---

## 14. Implementation phases (for execution after final go-ahead)

1. **Image + local server**: copy Dockerfile/README into `sandbox-image/`; verify build + `docker compose
   up` + `verify_sandbox.py`-style smoke against `localhost:8090`.
2. **Backend + factory**: `agent/integrations/opensandbox.py` (backend, factory, predicate); registry +
   startup validation in `agent/utils/sandbox.py`. Unit tests.
3. **Auth helper**: `agent/utils/sandbox_github_auth.py`; wire opensandbox branches in `server.py`
   (`_create_sandbox_with_proxy`/`_refresh_github_proxy`) + `analyzer.py`; `renew` on reconnect.
4. **Error-recovery generalization (D5)**: predicate wiring in `server.py:360` + `tool_error_handler.py`;
   structured markers in the payload + `sandbox_circuit_breaker.py`; per-integration predicate exports.
   Keep existing recovery tests green; add UUID/opensandbox variants.
5. **Pooling seam**: create-path branch (k8s `poolRef`; local SDK-pool flag off by default).
6. **Integration test + run-and-observe**: real container e2e; a live `make dev` thread proving the
   `hosts.yml` auth path (clone + `gh` call). Paste real output.
7. **Docs**: `customization/` runbook; `.env` documentation; `pyproject.toml` dependency.
8. **Lint/format/test gates**: `make lint`, `make format`, `make test` — paste real output.
9. **Decommission sweep (separate branch/PR, after phase 8)**: execute §15 — quarantine Slack/Linear
   and any orphaned code into `bak/`. Kept deliberately separate from the provider work so a broad
   deletion never entangles the feature-add's review.

---

## 15. Decommission unused code (Slack + Linear removal; `bak/` quarantine)

**Motivation.** Slack and Linear are not needed. The agent will be triggered exclusively via the
**GitHub** path (PR comments / auto-review) and the **UI/dashboard** (the Agents chat thread API in
`agent/dashboard/`). Everything Slack/Linear-specific becomes dead weight and should be retired safely,
not hard-deleted.

### 15.1 The `bak/` quarantine method (general rule, reusable for any future dead-code removal)

- Create a repo-root **`bak/`** directory that **mirrors the real tree exactly** (e.g. a quarantined
  `agent/tools/slack_thread_reply.py` lands at `bak/agent/tools/slack_thread_reply.py`).
- **Fully-dead file** → **move** it, preserving its path: `git mv <path> bak/<path>`.
- **Partially-dead file** (only a portion is dead) → **first copy the whole file as-is (dead code
  intact) to `bak/<path>`** as a point-in-time snapshot, **then** strip the dead portion from the live
  file. `bak/` thus always holds a complete, runnable-looking snapshot of what was removed, with full
  surrounding context — recovery is a glance, not a git-archaeology exercise. (Git history remains the
  deeper safety net.)
- **`bak/` is tracked in git** but **excluded from all tooling** so it can never affect the build:
  add `bak` to ruff `extend-exclude` in `pyproject.toml`; it is not collected by pytest
  (`testpaths = tests`); it is not on the import path (nothing imports it — verify with grep); add it
  to `.dockerignore` and any langgraph/packaging excludes.
- Add **`bak/README.md`** recording, per entry: what was quarantined, the date, why (e.g. "Slack
  retired — trigger via dashboard/GitHub only"), and the one-line restore command.

### 15.2 Fully-dead files → whole-file move to `bak/` (Slack/Linear-dedicated)

Confirmed dedicated to Slack/Linear (safe `git mv`):

- Tools: `agent/tools/{linear_comment, linear_create_issue, linear_delete_issue, linear_get_issue,
  linear_get_issue_comments, linear_list_teams, linear_update_issue, slack_read_thread_messages,
  slack_thread_reply}.py`
- Utils: `agent/utils/{linear, linear_team_repo_map, slack, slack_feedback}.py`
- Middleware: `agent/middleware/refresh_slack_status.py` (`SlackAssistantStatusMiddleware`)
- Webhooks: `agent/webhooks/{slack, linear}.py`
- Dashboard OAuth: `agent/dashboard/slack_oauth.py` (and Linear OAuth if present)

### 15.3 Partially-dead files → copy-to-`bak/` then strip in place

These carry Slack/Linear code alongside live code; snapshot to `bak/` first, then remove the
Slack/Linear portions. The executing agent must trace each file's Slack/Linear usage **precisely at
implementation time** (grep + the Python code-intelligence plugin + failing-test-first), not by
guessing — this list is the known footprint, not a line-level spec:

- Wiring: `agent/tools/__init__.py` (imports + `__all__`), `agent/server.py` (tool imports, the
  `tools=[…]` list, `slack_thread` / `linear_issue` `configurable` reads, prompt vars),
  `agent/webapp.py` (Slack/Linear imports, webhook routes, reaction handlers).
- Middleware: `agent/middleware/check_message_queue.py` (mid-run Slack/Linear message injection →
  reduce to dashboard/GitHub sources or remove), `agent/middleware/sandbox_circuit_breaker.py` and
  `agent/middleware/notify_step_limit.py` (notification fan-out Slack→Linear→GitHub → **GitHub +
  dashboard only**).
- Prompts / dispatch: `agent/prompt.py`, `agent/completion.py`, `agent/dispatch.py`.
- Reviewer: `agent/reviewer.py`, `agent/reviewer_findings.py`.
- Utils: `agent/utils/{comments, authorship, auth, multimodal, reviewer_outcomes, thread_ops}.py`.
- Dashboard: `agent/dashboard/{routes, thread_api, user_mappings, oauth, profiles, agent_overrides,
  agent_usage, plan_api, review_api}.py` and `agent/webhooks/github.py` (strip Slack/Linear
  cross-links; keep the GitHub + dashboard trigger surface).

### 15.4 Trigger-mechanism consequences (what stays wired)

- **Middleware stack** (`server.py:get_agent`): drop `SlackAssistantStatusMiddleware`; keep
  `check_message_queue` only for the retained sources (dashboard/GitHub) or remove if it becomes a
  no-op; `notify_step_limit` and the circuit-breaker unrecoverable notice fall back to GitHub +
  dashboard.
- **Webhooks** (`webapp.py`): remove the Slack and Linear routes and signature verifiers; keep the
  GitHub webhook and the dashboard Agents-thread trigger API.
- **Thread-id derivation**: keep GitHub (`get_thread_id_from_branch`) and dashboard thread ids; remove
  the Slack/Linear derivations.
- **Config/env**: `SLACK_*` / `LINEAR_*` env vars and their `configurable` keys become unused —
  document as retired in the `customization/` runbook; leave no dangling required-env checks.
- **No graph is removed** from `langgraph.json` — `agent`, `reviewer`, `analyzer`, and the FastAPI app
  all remain.

### 15.5 General dead-code sweep (beyond Slack/Linear)

After the Slack/Linear strip, run a dead-code pass and quarantine anything now-orphaned by the same
`bak/` method: unused imports/symbols (`ruff --select F401` and a `vulture`-style scan), utils left
with no live caller, and the known leftover `exa-py` reference if still present. Anything ambiguous
stays live and is flagged, not removed.

### 15.6 Sequencing & interaction with the provider work

Run this as its **own branch/PR after phase 8** of §14. Rationale: it is a broad deletion refactor
orthogonal to the OpenSandbox feature; mixing them makes review risky. Sequencing it **after** the D5
error-recovery change also avoids churn on the shared file `sandbox_circuit_breaker.py` (D5 rewrites
its matching logic; the decommission then simplifies its notification fan-out to GitHub + dashboard).

### 15.7 Verification & rollback

- **Verify** (evidence, per project rule): `make lint` + `make test` green (update/remove tests that
  referenced Slack/Linear tools; the langsmith sandbox-recovery tests must still pass); `make dev`
  boots all graphs with **no import errors**; a **dashboard-triggered** agent run completes
  end-to-end (the retained trigger path); `grep -rn "bak/" agent/` and an import check confirm **no
  live code references `bak/`** or any moved module.
- **Rollback**: restore any entry from `bak/` (reverse the `git mv`, or re-apply the snapshot for
  partial strips) or `git revert` the decommission PR wholesale.

