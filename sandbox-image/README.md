# Sandbox image (self-hosted Daytona)

This directory holds the Docker image that backs every agent sandbox when
`SANDBOX_TYPE="daytona"`. It is published as the Daytona **snapshot**
`open-swe-sandbox` (the name `agent/integrations/daytona.py` requests by default,
overridable via `DAYTONA_SANDBOX_SNAPSHOT`).

This is a **self-hosted Daytona** stack (docker compose): API on `localhost:3000`,
a local Docker registry container exposed on `localhost:6000`, plus a runner, dex,
etc. The host reaches the registry as `localhost:6000`; the **daytona-runner**
reaches the *same* registry by its in-network name `registry:6000`.

---

## TL;DR — after editing the Dockerfile

Bump the tag and run one command (it builds, pushes, and recreates the snapshot):

```bash
set -a; source .env; set +a   # loads DAYTONA_API_KEY / DAYTONA_API_URL
uv run python scripts/create_daytona_snapshot.py \
  --name open-swe-sandbox \
  --image registry:6000/open-swe-sandbox:<new-tag>
```

Then **restart `make dev` and start a fresh thread** (see step 4 below).

> Always bump `<new-tag>` (e.g. `0.1.2` → `0.1.3`). Snapshots are **immutable** —
> reusing a tag or re-pushing the same one does **not** update a live snapshot.

---

## Why these steps exist

The agent prompts hard-code `GH_TOKEN=dummy gh ...` (a LangSmith-proxy convention,
in ~33 places in `agent/prompt.py`). Daytona has **no proxy**, so a literal `dummy`
token would hit `api.github.com` and 401 (`Bad credentials`). The fix has two halves
that only work together:

- **Image side (this Dockerfile):** a wrapper at `/usr/local/bin/gh` shadows the real
  `/usr/bin/gh` (because `/usr/local/bin` precedes `/usr/bin` in `PATH`). It does
  `unset GH_TOKEN GITHUB_TOKEN; exec /usr/bin/gh "$@"`, so `gh` ignores the dummy
  token and falls back to `~/.config/gh/hosts.yml`.
- **Runtime side (`agent/server.py`):** the `daytona` branch writes the real, rotating
  GitHub App token into `hosts.yml` and adds a `git config url.insteadOf` rewrite for
  plain `git`.

Because the wrapper lives in the **image**, any Dockerfile change requires rebuilding
and re-publishing the snapshot before it takes effect.

---

## What the script does for you

`scripts/create_daytona_snapshot.py`:

1. `docker build` the image from `sandbox-image/open-swe-sandbox.Dockerfile`.
2. `docker push` it to the local registry, then **verifies the tag exists** in the
   registry (catches the "built but never pushed → `manifest unknown`" trap).
3. **Deletes** any existing snapshot of the same name, **waits** for the deletion to
   settle, then **creates** the new one (retrying on conflict).

Registry-name translation is automatic: you pass the **runner-facing** ref
(`registry:6000/...`); the script derives the **host-facing** push ref
(`localhost:6000/...`) for `docker build`/`push`, since the host can't resolve
`registry:6000`. Override with `--push-host` if your setup differs.

Useful flags / env:

| Flag / env | Default | Purpose |
|---|---|---|
| `--image` (required) | — | Runner-facing ref, e.g. `registry:6000/open-swe-sandbox:0.1.3` |
| `--name` / `DAYTONA_SANDBOX_SNAPSHOT` | `open-swe-sandbox` | Snapshot name |
| `--skip-build` | off | Only recreate the snapshot from an already-pushed image |
| `--push-host` / `DAYTONA_PUSH_HOST` | `localhost:6000` | Host-facing alias of the runner registry |
| `--dockerfile` | `sandbox-image/open-swe-sandbox.Dockerfile` | Dockerfile path |
| `--context` | `sandbox-image/` | Build context |
| `--cpu` / `--memory` / `--disk` | `1` / `1` / `3` | Snapshot resources (GB for mem/disk) |

---

## Full manual flow (if you ever bypass the script)

```bash
# 1. Build (host-facing tag)
docker build -t localhost:6000/open-swe-sandbox:<tag> \
  -f sandbox-image/open-swe-sandbox.Dockerfile sandbox-image/

# 2. Push — REQUIRED. Build alone leaves it local; the snapshot errors `manifest unknown`.
docker push localhost:6000/open-swe-sandbox:<tag>
curl -s http://localhost:6000/v2/open-swe-sandbox/tags/list   # confirm <tag> is listed

# 3. Recreate the snapshot from the RUNNER-facing ref
uv run python scripts/create_daytona_snapshot.py \
  --name open-swe-sandbox --image registry:6000/open-swe-sandbox:<tag> --skip-build
```

---

## Step 4 — restart and use a FRESH thread

```bash
make dev
```

A previously-running thread persists its `sandbox_id` in thread metadata and will
**reconnect to the old (pre-change) sandbox**. Start a **new thread/conversation** so a
fresh sandbox is provisioned from the new snapshot.

---

## Verify the image (optional but recommended)

Inspect the built image directly (the base image's `ENTRYPOINT` runs `sleep`, so
override it):

```bash
docker run --rm --entrypoint sh localhost:6000/open-swe-sandbox:<tag> \
  -c 'command -v gh; cat /usr/local/bin/gh; ls -l /usr/bin/gh'
```

Expected: `command -v gh` → `/usr/local/bin/gh`, the wrapper body, and the real ~40 MB
binary still at `/usr/bin/gh`.

To prove the gh-auth behavior in a live sandbox, boot one from the snapshot and run
`GH_TOKEN=dummy gh auth status`: with no creds it should say *"not logged into any
GitHub hosts"* (the dummy token was stripped, not used). At runtime `server.py` writes
the real token into `hosts.yml`, so authenticated commands then succeed.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `HTTP 401: Bad credentials (api.github.com)` in the agent | Snapshot lacks the gh wrapper, or you didn't restart / used an old thread | Rebuild snapshot; restart `make dev`; use a fresh thread |
| Snapshot state `error`: `manifest unknown` | Image was built but **not pushed** to the registry | `docker push`, verify with the `tags/list` curl, recreate |
| `DaytonaAuthorizationError: Access denied` on create/delete | `DAYTONA_API_KEY` lacks snapshot **write** permission for its org | Use an API key with snapshot write scope |
| `DaytonaConflictError: ... already exists` | `delete()` hadn't settled before `create()` | The script already polls + retries; if hand-running, wait and retry |
| Changes don't appear in a sandbox | Reused tag (snapshots are immutable) or reconnected old sandbox | Bump the tag; start a fresh thread |