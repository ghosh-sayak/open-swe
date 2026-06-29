# Plan: Recreate Daytona sandbox when unreachable

## Problem

When a cached Daytona sandbox container stops (loses its IP / is not started),
`check_or_recreate_sandbox` pings it with `echo ok`, which raises
`DaytonaValidationError`. The `except SandboxClientError` guard does not catch
this (it is a LangSmith-specific exception type), so the error propagates
unhandled and the entire agent run crashes.

Root cause: the except block was written for LangSmith only. Any other provider
(Daytona, Modal, RunLoop, local) that raises a different exception type will
bypass the guard entirely.

## Why restart-before-recreate was ruled out

A restart-first path (`sandbox.start(timeout)`) would preserve the container
and its in-progress work. However:

- **Low confidence (~45%)**: Daytona's `sandbox.start()` exists on the Python
  object but is undocumented. It also requires unwrapping the `DaytonaSandbox`
  wrapper to access `_sandbox.start` — a private attribute access (`# noqa: SLF001`).
- **Requires extra customization**: the `_try_start_daytona_sandbox` helper
  needs `SANDBOX_TYPE`-gated logic, a new import from `langchain_daytona`, and
  access to `unwrap_sandbox_backend` — all changes that are Daytona-specific and
  not present in stock open-swe.
- **Not worth the fragility**: the restart path adds ~60 lines of provider-specific
  code for an uncertain benefit. The sandbox already gets a fresh clone when
  recreated; the lost work is the intermediate build/install state, which the
  agent can re-run.

The simpler fix is strictly better here: catch any exception from the ping and
recreate, consistent with the existing pattern everywhere else in the file.

## Fix applied

In `agent/server.py`, `check_or_recreate_sandbox` — changed `except SandboxClientError`
to `except Exception` with `exc_info=True`:

```python
try:
    await asyncio.to_thread(sandbox_backend.execute, "echo ok")
except Exception:  # noqa: BLE001
    logger.warning(
        "Cached sandbox is no longer reachable for thread %s, recreating",
        thread_id,
        exc_info=True,
    )
    sandbox_backend = await _recreate_sandbox(
        thread_id,
        github_proxy_token=github_proxy_token,
        github_proxy_repositories=github_proxy_repositories,
        repo=repo,
    )
return sandbox_backend
```

## Why `except Exception` (not `SandboxClientError`)

The ping is a binary liveness check. Any exception — LangSmith's
`SandboxClientError`, Daytona's `DaytonaValidationError`, a network timeout —
means the sandbox is unreachable. `except Exception` with `# noqa: BLE001`
is the established pattern in this file (lines 139, 154, 205, 275, 323, etc.).
Adding `exc_info=True` ensures the actual exception type and message appear
in logs so failures are diagnosable.

## Alignment with stock open-swe

The stock `main` branch uses `except SandboxClientError` — written for LangSmith
only. This change extends the same intent (recreate on unreachable) to all
providers. No new imports, no provider-specific branches, no private attributes.

## Roll-back

```python
try:
    await asyncio.to_thread(sandbox_backend.execute, "echo ok")
except SandboxClientError:
    logger.warning(
        "Cached sandbox is no longer reachable for thread %s, recreating",
        thread_id,
    )
    sandbox_backend = await _recreate_sandbox(
        thread_id,
        github_proxy_token=github_proxy_token,
        github_proxy_repositories=github_proxy_repositories,
        repo=repo,
    )
return sandbox_backend
```
