# OpenSandbox lifecycle & auth hardening (Spec 1)

**Date:** 2026-07-09
**Branch context:** `poc/k8s-sandbox`
**Status:** Approved design — ready for implementation planning

## Summary

The `SANDBOX_TYPE=opensandbox` provider is implemented and merged, but a design
review benchmarked it against the mature `langsmith` provider and found a set of
lifecycle- and auth-robustness gaps that langsmith already solves. This spec
covers the **portable parity fixes** — the ones where langsmith has a mechanism
opensandbox lacks (or avoids a problem opensandbox is still exposed to) and the
fix fits the small, curated provider style.

Two larger, more architectural gaps are explicitly **out of scope** and deferred
to Spec 2 (see Non-goals): the provider-class refactor and repo-scoped snapshot
support.

## Scope

Six items, labelled A/B/C/F/G/H to match the review findings they close.

| Item | Gap | Review finding |
|---|---|---|
| A | `execute` has no client-side deadline; a wedged command can pin a worker thread forever | #4 |
| B | Neither `execute` nor `aexecute` is overridden, so the async path is inherited unmodified and can block the event loop | #7 (async) |
| C | `configure_github_auth` has no retry; a single transient failure fails the run | #10 |
| F | Absolute TTL is renewed only at run boundaries; a long single run is reaped mid-run | #1 |
| G | GitHub token is interpolated into a shell command (visible in the sandbox process list) | #11 / #12 |
| H | Startup health probe hardcodes `http://` and the `/health` path | #13 |

## Context / grounding

Verified against the OpenSandbox Python SDK and `deepagents_opensandbox`
documentation (via context7):

- **Command interruption exists:** the execd API exposes `DELETE /command`
  ("Interrupt the currently executing command"), plus foreground/background
  modes and `GET /command/status/{session}`. A client-side deadline can
  therefore interrupt a wedged command rather than only abandoning the thread.
- **TTL renewal exists:** `renew(timeout)` sets `expiresAt = now + timeout`;
  `timeoutSeconds = null` selects a non-expiring / manual-cleanup mode.
- **File-write API exists:** `files.write_files(WriteEntry(path, data, mode))`,
  already used by `OpensandboxBackend.upload_files`.
- **Health endpoint:** the documented service health check is `GET /ping`, not
  `/health`.
- The sibling `langsmith` provider's `TimeoutLangSmithSandbox` and
  `_configure_github_proxy` are the reference implementations for items A/B and C.

## Design

### A + B — Execute client-side deadline and proper async path

These are a single change to `agent/integrations/opensandbox.py`.

Today `OpensandboxBackend.execute` calls the blocking sync
`self._sandbox.commands.run(...)` (a synchronous SSE read) with only a
server-side timeout. If the server-side timeout does not fire, the read blocks
its worker thread indefinitely and wedges the run. `aexecute` is not overridden,
so opensandbox inherits `BaseSandbox`'s base async behavior unmodified.

Fix, mirroring langsmith's `TimeoutLangSmithSandbox`:

- Introduce a client-side deadline of `effective_server_timeout + grace`, where
  `grace` comes from a new env var `OPEN_SANDBOX_EXECUTE_CLIENT_GRACE_SECONDS`
  (default `30`).
- Override `aexecute` to run the blocking call via
  `asyncio.wait_for(asyncio.to_thread(<blocking run>), timeout=deadline)`. This
  simultaneously (a) guarantees the sync SDK call never blocks the event loop
  (item B) and (b) gives the client-side deadline a place to live (item A).
- On `asyncio.TimeoutError`: issue a best-effort command interrupt via the SDK's
  command-interrupt primitive (`DELETE /command`) if the sync SDK exposes one;
  otherwise abandon the wedged thread without joining it — exactly as langsmith
  does with `pool.shutdown(wait=False)`. Either way, return
  `ExecuteResponse(output=<timeout message>, exit_code=124, truncated=False)`.
- Keep the synchronous `execute` correct as well (it is part of the protocol);
  the deadline logic is shared so both paths behave consistently.

**Assumption to confirm during planning:** whether the installed `opensandbox`
sync SDK exposes a per-command interrupt/kill method. If it does not, the
timeout path degrades gracefully to abandon-thread + `124` (no worse than
langsmith's wedged-worker handling, and the sandbox's own server-side timeout
eventually reaps the command).

### C — Auth-config retry

`configure_github_auth` (in `agent/utils/sandbox_github_auth.py`) performs the
credential write once. langsmith's `_configure_github_proxy` retries up to 3×
with backoff on retryable transient errors.

Fix: wrap the credential write in the same retry shape — reuse langsmith's
constants (max attempts, retry delays, retryable conditions) so the two auth
paths behave consistently. On exhaustion, raise as today (the lifecycle layer
already decides recreate-vs-keep via the recoverability predicate).

### F — Mid-run TTL renewal

`renew_ttl` currently fires only at run boundaries (the ping path and on
reconnect). The before-model hook refreshes the GitHub token every model call
but never slides the TTL, so a single continuous run longer than
`DEFAULT_TTL_SECONDS` (2h) has its token kept fresh while the sandbox is reaped
underneath it.

Fix (decision: renew-in-hook, keeping auto-cleanup as a safety net): renew the
TTL inside the same before-model refresh path that already handles the token
(`agent/utils/github_proxy.py` / `refresh_github_proxy_before_model`), gated on
a near-expiry window so it is not a network call on every turn. Track TTL expiry
analogously to how proxy-token expiry is already tracked, and slide it when
within the window. The sandbox still expires on its own if the process dies, so
a crashed run cannot leak a sandbox indefinitely.

### G — Token hygiene

`configure_github_auth` interpolates the token into `git config` / `printf`
shell commands passed to `execute`, so the token is briefly visible in the
sandbox process list (`ps`) and to anything logging the exec channel. The
injection-safety regex is retained; this is about visibility, not injection.

Fix: write the credential files via the file API (`files.write_files` /
`WriteEntry`) instead of the shell:

- Write `~/.config/gh/hosts.yml` directly as file content.
- Write `~/.gitconfig` directly as file content, including the
  `url."https://x-access-token:<token>@github.com/".insteadOf` section.

Because we now own the whole `~/.gitconfig` file, the fragile "remove stale
insteadOf sections first" shell dance is no longer needed — each refresh
rewrites the file wholesale, so a rotated token cannot leave a stale section
behind. The token never appears on a command line.

The deeper concern — a pooled/reused sandbox retaining a prior token or crossing
an installation boundary — is bound up with pooling and is deferred to Spec 2,
where pooling reuse is designed.

### H — HTTPS-aware health probe

`validate_startup_config` builds `http://{domain}/health` with a hardcoded
scheme and path. In any TLS deployment this both transmits credentials in
plaintext (if the scheme were used for real traffic) and fails the probe
outright (scheme/path mismatch with how the SDK connects).

Fix:

- Derive the scheme from configuration (the `protocol` / `use_server_proxy`
  signal the SDK already uses) rather than hardcoding `http`.
- Use the documented health path (`/ping`) — confirm against the running server
  during planning, since `/health` may be a control-plane alias.
- Prefer reusing the SDK's connection config to reach the health endpoint over
  hand-building a URL, so the probe and real traffic stay in sync.

## Testing

Unit tests extend the existing seams in `tests/test_opensandbox_integration.py`
(and `tests/test_sandbox_github_auth.py` for item G):

- A/B: a command that overruns its deadline returns `exit_code=124`; `aexecute`
  offloads the blocking call (does not block the loop); the interrupt/abandon
  path is exercised.
- C: a transient failure followed by success results in a successful configure;
  exhausted retries raise.
- F: TTL renew fires only when within the near-expiry window, not on every turn.
- G: credentials are written via the file API; assert no token appears in any
  `execute` command string; wholesale gitconfig rewrite leaves no stale section.
- H: probe scheme is derived from config; correct path is used.

End-to-end coverage stays in `tests/integration_tests/test_opensandbox_e2e.py`.

## Non-goals (deferred to Spec 2)

- **D — Provider-class refactor:** giving opensandbox a `SandboxProvider`
  subclass to match `LangSmithProvider`, and/or adopting the published
  `deepagents_opensandbox` `OpensandboxProvider` (which offers native async
  `aget_or_create`/`adelete`).
- **E — Repo-scoped snapshots / warm images:** the langsmith snapshot model so
  runs do not re-clone + reinstall every time.
- **Pooling reuse boundary:** ensuring a pooled sandbox never crosses
  installation/user boundaries with a stale token (depends on the pooling design).

## Constraints

- No behavior change for `langsmith` or any other provider — all edits are
  gated on `SANDBOX_TYPE == "opensandbox"` or live in opensandbox-only modules.
- Match the small, curated provider style; do not re-implement anything the
  `opensandbox` SDK or `deepagents` `BaseSandbox` already provides.
- Verify every SDK API shape against the installed package / context7 before
  use; do not guess signatures.
