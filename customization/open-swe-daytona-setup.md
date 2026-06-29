# Open-SWE × Daytona OSS — Local Setup Runbook

> **Who this is for:** Anyone setting up open-swe locally using Daytona OSS as the sandbox provider, without LangSmith, without Linear, without Slack. This document is self-contained — follow it top to bottom and you will have a working local demo.

---

## Prerequisites

Before starting, ensure the following are installed and running:

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.11–3.13 | `python3 --version` |
| `uv` | latest | `pip install uv` or `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Docker + Docker Compose | latest | `docker --version` |
| Bun | latest | `curl -fsSL https://bun.sh/install \| bash` |
| Node.js | 22.x (LTS) | See Node.js upgrade section below |
| ngrok | latest | `https://ngrok.com/download` — needed for GitHub webhooks |
| Daytona OSS | running locally | Docker Compose stack already up |
| OpenAI API key | — | From `https://platform.openai.com/api-keys` |

### Node.js Upgrade (required for dashboard UI)

Default Ubuntu/Debian Node.js is too old for the Vite-based dashboard. Upgrade to Node 22:

```bash
# Install nvm
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
source ~/.bashrc

# Install and activate Node 22 LTS
nvm install 22
nvm use 22
nvm alias default 22

# Verify
node --version   # must show v22.x.x
```

---

## Step 1 — Clone and install open-swe

```bash
git clone https://github.com/langchain-ai/open-swe.git
cd open-swe
uv venv
source .venv/bin/activate
uv sync --all-extras
```

---

## Step 2 — Start ngrok

GitHub needs a public URL to deliver webhooks to your local machine.

### 2a. Get your permanent dev domain (one-time)

Every ngrok account has a free permanent subdomain — it never changes regardless of restarts.

1. Log in at `https://dashboard.ngrok.com`
2. Go to **Domains**: `https://dashboard.ngrok.com/domains`
3. Copy your assigned domain — looks like `your-name-abc123.ngrok-free.app`

### 2b. Start ngrok

Always use `--url=` with your assigned domain:

```bash
ngrok http --url=your-name-abc123.ngrok-free.app 2024
```

Keep this terminal open for the entire session. When ngrok restarts, just re-run this same command — the URL never changes.

### 2c. If the URL changes (fallback / edge case)

This only happens if you ran plain `ngrok http 2024` without `--url=` and got a random URL. In that case you need to update **one field only** — no App recreation needed:

1. Go to `https://github.com/settings/apps/open-swe-local` → **General**
2. Scroll to **Webhook URL**
3. Replace the old URL with the new one: `https://<new-ngrok-url>/webhooks/github`
4. Click **Save changes**

That's it. All credentials (App ID, private key, client secret, installation ID) are permanent and unaffected.

> **To avoid this entirely:** always use the `--url=` flag with your permanent dev domain from Step 2a.

---

## Step 3 — Create the GitHub App

This is the most involved step. Follow exactly.

### 3a. Navigate to GitHub App creation

Go to: `https://github.com/settings/apps/new`

(This is for a **personal account**. If using an org, go to `https://github.com/organizations/<org>/settings/apps/new` instead.)

### 3b. Fill in the basic settings

| Field | Value |
|---|---|
| **GitHub App name** | `open-swe-local` (or any unique name) |
| **Homepage URL** | `http://localhost:3001` |
| **Webhook URL** | `https://your-name-abc123.ngrok-free.app/webhooks/github` — use your permanent dev domain from Step 2a, not a random URL |
| **Webhook secret** | Generate: `openssl rand -hex 32` — save this value as `GITHUB_WEBHOOK_SECRET` |

### 3c. Set permissions

Under **Repository permissions**, set:

| Permission | Level |
|---|---|
| Contents | Read & write |
| Issues | Read & write |
| Pull requests | Read & write |
| Checks | Read & write |
| Metadata | Read-only (mandatory) |
| Commit statuses | Read & write |

Under **Account permissions**: none needed.

### 3d. Subscribe to events

Check all of the following under **Subscribe to events**:

- ✅ Issue comment
- ✅ Pull request review
- ✅ Pull request review comment
- ✅ Check run
- ✅ Check suite
- ✅ Workflow run

### 3e. Set installation scope

Under **Where can this GitHub App be installed?** → select **Only on this account**.

Click **Create GitHub App**.

### 3f. Collect credentials

After creation, you land on the App settings page. Collect these values:

| Value | Where to find it | Env var |
|---|---|---|
| App ID | Top of the page, labelled "App ID" | `GITHUB_APP_ID` |
| Client ID | Same page, labelled "Client ID" | `GITHUB_APP_CLIENT_ID` |
| Client secret | Click **Generate a new client secret** | `GITHUB_APP_CLIENT_SECRET` |
| Private key | Scroll down → **Private keys** → **Generate a private key** — downloads a `.pem` file | `GITHUB_APP_PRIVATE_KEY` |

**Storing the private key in `.env`:** Open the downloaded `.pem` file and paste its full contents inline, preserving actual newlines (do NOT flatten to `\n`):

```bash
GITHUB_APP_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----
MIIEowIBAAKCAQEA...
...full key content...
-----END RSA PRIVATE KEY-----
"
```

### 3g. Install the App on your repositories

Still on the App settings page, click **Install App** in the left sidebar. Click **Install** next to your account. Choose **Only select repositories** and select the repos you want open-swe to work on (e.g. `ghosh-sayak/open-swe`).

After clicking **Install**, GitHub redirects to:

```
http://localhost:2024/dashboard/api/auth/callback?code=...&installation_id=<NUMBER>&setup_action=install
```

This will show a browser error ("Unable to connect") because the backend isn't running yet — **that's expected**. The `installation_id=<NUMBER>` in the URL is what you need. Save that number as `GITHUB_APP_INSTALLATION_ID`.

---

## Step 4 — Set up Daytona

### 4a. Confirm Daytona is running

```bash
docker ps | grep daytona
```

You should see containers including `daytona-api-1`, `daytona-runner-1`, `daytona-registry-1`, etc.

### 4b. Generate a Daytona API key

```bash
docker exec -it daytona-api-1 sh -c "daytona api-key generate open-swe"
```

Save the output as `DAYTONA_API_KEY`.

### 4c. Build the custom sandbox image

The default Daytona sandbox image (`daytonaio/sandbox:0.5.0-slim`) does **not** include the `gh` CLI, which open-swe requires for all GitHub operations. You must build a custom image.

The canonical Dockerfile lives at `sandbox-image/open-swe-sandbox.Dockerfile` (build
context `sandbox-image/`). It installs `gh` **and** a small `gh` wrapper — see the
[gh-auth fix](#-resolved-gh_tokendummy-override) for why the wrapper is required:

```dockerfile
FROM daytonaio/sandbox:0.5.0-slim

USER root

RUN apt-get update && \
    apt-get install -y curl gpg && \
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | \
    dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && \
    chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | \
    tee /etc/apt/sources.list.d/github-cli.list > /dev/null && \
    apt-get update && \
    apt-get install -y gh git && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# The agent always invokes `GH_TOKEN=dummy gh` (a LangSmith-proxy convention). On
# providers without that proxy, the dummy token would override the real one in
# ~/.config/gh/hosts.yml and 401. /usr/local/bin precedes /usr/bin in PATH, so this
# wrapper shadows the real gh (kept at /usr/bin/gh) and strips the dummy token,
# letting gh fall back to hosts.yml. No recursion, real binary untouched.
RUN printf '#!/bin/sh\nunset GH_TOKEN GITHUB_TOKEN\nexec /usr/bin/gh "$@"\n' > /usr/local/bin/gh && \
    chmod +x /usr/local/bin/gh
```

**Recommended — one command does build + push + snapshot.** `scripts/create_daytona_snapshot.py`
builds the image, pushes it to the local registry (verifying the tag landed), then
deletes/recreates the immutable snapshot. See `sandbox-image/README.md` for the full
runbook.

```bash
set -a; source .env; set +a   # loads DAYTONA_API_KEY / DAYTONA_API_URL
uv run python scripts/create_daytona_snapshot.py \
  --name open-swe-sandbox \
  --image registry:6000/open-swe-sandbox:0.1.0
```

> The `--image` ref uses the runner-facing hostname `registry:6000`; the script
> auto-derives the host-facing `localhost:6000` for build/push (the host can't resolve
> `registry:6000`). **Bump the tag on every change** — snapshots are immutable.

If you'd rather do it by hand, or your API key lacks snapshot-write permission (see
4d), build + push manually and register the snapshot in the dashboard:

```bash
docker build -f sandbox-image/open-swe-sandbox.Dockerfile -t localhost:6000/open-swe-sandbox:0.1.0 sandbox-image/
docker push localhost:6000/open-swe-sandbox:0.1.0

# Verify the TAG is in the registry (build alone is not enough → 'manifest unknown')
curl http://localhost:6000/v2/open-swe-sandbox/tags/list
# Expected: {"name":"open-swe-sandbox","tags":["0.1.0"]}
```

### 4d. Register the snapshot

If you used `scripts/create_daytona_snapshot.py` in 4c, the snapshot is already
created — skip to the verification at the end of this section.

**Snapshot-write permission gotcha:** the script's `daytona.snapshot.create` (and the
dashboard) require an API key with snapshot **write** permission for its organization.
A key minted with `daytona api-key generate` (Step 4b) may be read-only for snapshots
and fail with `DaytonaAuthorizationError: Access denied`. If so, either mint a key with
snapshot permissions, or register the snapshot through the dashboard instead.

**Dashboard alternative** — open `http://localhost:3000` and log in with
`dev@daytona.io` / `password`. Navigate to **Snapshots** → **Create Snapshot**:

| Field | Value |
|---|---|
| **Snapshot Name** | `open-swe-sandbox` |
| **Image** | `registry:6000/open-swe-sandbox:0.1.0` |
| **Region** | `us` (default) |
| **Sandbox Class** | `Container` |
| **Compute (vCPU)** | `1` |
| **Memory (GiB)** | `1` |
| **Storage (GiB)** | `3` |
| **Entrypoint** | leave as `sleep infinity` |

> **Why `registry:6000` not `localhost:6000`:** The runner container resolves the registry by its Docker internal hostname `registry`, not `localhost`. Using `localhost:6000` causes a connection refused error from inside the runner.

Click **Create** and wait for the snapshot state to show **Active** (refresh the page — can take 1–3 minutes).

**Verify (either path):** the snapshot should be `ACTIVE`. A common failure is state
`error: manifest unknown` — that means the image tag was built but never pushed to the
registry. Re-run the push and confirm with the `tags/list` curl from 4c.

> **Updating after a Dockerfile change:** snapshots are immutable, so re-pushing a tag
> does nothing to a live snapshot. Bump the tag and re-run the script (it deletes +
> recreates), or delete/recreate in the dashboard. Then restart the backend and use a
> **fresh thread** — a running thread persists its `sandbox_id` and reconnects to the
> old sandbox.

---

## Step 5 — Set up SearXNG (web search)

open-swe originally uses Exa (commercial API). This setup replaces it with SearXNG (self-hosted, free).

### 5a. Create SearXNG directory and fetch config

```bash
mkdir -p searxng/core-config
cd searxng

curl -fsSL \
    -O https://raw.githubusercontent.com/searxng/searxng/master/container/docker-compose.yml \
    -O https://raw.githubusercontent.com/searxng/searxng/master/container/.env.example

cp .env.example .env
```

### 5b. Edit `searxng/.env`

```bash
SEARXNG_BIND_ADDRESS=0.0.0.0
SEARXNG_PORT=8888
SEARXNG_SECRET="<run: openssl rand -hex 32>"
```

### 5c. Create `searxng/core-config/settings.yml`

```yaml
use_default_settings: true

general:
  debug: false
  instance_name: "open-swe-search"
  enable_metrics: false
  donation_url: false
  privacypolicy_url: false
  contact_url: false

server:
  base_url: "http://localhost:8888"
  bind_address: "0.0.0.0"
  secret_key: "<same value as SEARXNG_SECRET above>"
  limiter: true
  public_instance: false
  image_proxy: false
  method: GET

search:
  safe_search: 0
  default_lang: "en"
  formats:
    - html
    - json

valkey:
  url: valkey://searxng-valkey:6379/0
```

### 5d. Start SearXNG

```bash
cd searxng
docker compose up -d
```

Verify it works:

```bash
curl "http://localhost:8888/search?q=test&format=json" | python3 -m json.tool | head -20
# Expected: JSON with a "results" array containing search hits
```

Go back to project root:
```bash
cd ..
```

---

## Step 6 — Code changes (verify or apply)

These are the changes that distinguish this Daytona-based setup from upstream
open-swe. **If you cloned this repo, they are already applied** — use each subsection
to *verify* the code matches (the snippets are the source of truth for what each change
should look like). **If you cloned the upstream `langchain-ai/open-swe`** (Step 1),
*apply* each one. Either way, each is required for the local Daytona setup to work.

### 6a. `agent/tools/web_search.py` — SearXNG instead of Exa

Upstream uses `exa_py`. Confirm the file matches the SearXNG version below (replace the whole file if it doesn't):

```python
import asyncio
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def web_search(
    query: str,
    num_results: int = 5,
    include_contents: bool = True,
) -> dict[str, Any]:
    """Search the web using SearXNG to find relevant information.

    Use this tool when you need to find documentation, code examples, GitHub repos,
    news, or research papers to help complete a task.

    Args:
        query: The search query
        num_results: Number of results to return (default: 5)
        include_contents: Whether to include full page contents (default: True)

    Returns:
        Dictionary containing:
        - success: Whether the search succeeded
        - results: Search results from SearXNG
        - error: Error message if something failed
    """
    base_url = os.environ.get("SEARXNG_BASE_URL", "http://localhost:8888")

    async def _search() -> dict[str, Any]:
        params = {
            "q": query,
            "format": "json",
            "language": "en",
            "safesearch": "0",
            "pageno": "1",
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(f"{base_url}/search", params=params)
            response.raise_for_status()
            data = response.json()

        results = data.get("results", [])[:num_results]

        formatted = []
        for r in results:
            entry: dict[str, Any] = {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("content", ""),
            }
            if include_contents and r.get("content"):
                entry["content"] = r.get("content", "")
            formatted.append(entry)

        return {"success": True, "results": str(formatted), "error": None}

    try:
        return asyncio.run(_search())
    except Exception as e:
        logger.exception("web_search failed")
        return {"success": False, "results": None, "error": f"{type(e).__name__}: {e}"}
```

### 6b. `pyproject.toml` — remove `exa-py`

Confirm this line is absent from `pyproject.toml` (delete it if present):

```toml
"exa-py>=2.10.1",
```

Then resync:

```bash
uv sync --all-extras
```

> **The gh auth fix has two halves.** The `gh` *wrapper* lives in the **image**
> (Step 4c) because it is static. The code below only writes the **rotating** GitHub
> App token into `~/.config/gh/hosts.yml` (for `gh`'s API calls) plus a
> `git config url.insteadOf` rewrite (for plain `git` and the git sub-step of
> `gh repo clone`). The wrapper strips the hardcoded `GH_TOKEN=dummy` so `gh` falls
> back to this `hosts.yml` token. Do **not** try to replace the `gh` binary at runtime
> with a token baked in — that bakes a token that goes stale on refresh, and the naive
> `cp wrapper $(which gh)` overwrites the real binary and recurses.

### 6c. `agent/server.py` — `_create_sandbox_with_proxy()`

Confirm the `elif sandbox_type == "daytona":` block reads as below (apply if missing):

```python
    elif sandbox_type == "daytona":
        token, _ = await _resolve_proxy_token(github_proxy_token)
        if not token:
            msg = "Cannot configure git auth: GitHub App installation token is unavailable"
            logger.error(msg)
            raise ValueError(msg)
        # No GitHub proxy on this provider, so write real credentials into the sandbox.
        # The agent always runs `GH_TOKEN=dummy gh`; the image ships a gh wrapper that
        # strips that dummy token so gh falls back to the hosts.yml token written here.
        setup_commands = " && ".join([
            # git credential rewrite for plain git (and the git sub-step of `gh repo clone`)
            f"git config --global url.'https://x-access-token:{token}@github.com/'.insteadOf 'https://github.com/'",
            # real token for gh's API calls
            "mkdir -p /root/.config/gh",
            f"printf 'github.com:\\n  oauth_token: {token}\\n  git_protocol: https\\n  user: x-access-token\\n' > /root/.config/gh/hosts.yml",
        ])
        await asyncio.to_thread(sandbox_backend.execute, setup_commands)
        logger.info("Configured git and gh credentials in Daytona sandbox %s", sandbox_backend.id)
```

### 6d. `agent/server.py` — `_refresh_github_proxy()`

Confirm the `elif sandbox_type == "daytona":` block in `_refresh_github_proxy()` reads as below — same credential write, refreshed token (apply if missing):

```python
    elif sandbox_type == "daytona":
        token, _ = await _resolve_proxy_token(github_proxy_token)
        if not token:
            logger.warning(
                "Skipping git credential refresh for Daytona sandbox %s: installation token unavailable",
                sandbox_backend.id,
            )
            return
        setup_commands = " && ".join([
            f"git config --global url.'https://x-access-token:{token}@github.com/'.insteadOf 'https://github.com/'",
            "mkdir -p /root/.config/gh",
            f"printf 'github.com:\\n  oauth_token: {token}\\n  git_protocol: https\\n  user: x-access-token\\n' > /root/.config/gh/hosts.yml",
        ])
        await asyncio.to_thread(sandbox_backend.execute, setup_commands)
        logger.info("Refreshed git and gh credentials in Daytona sandbox %s", sandbox_backend.id)
```

### 6e. `agent/utils/langsmith.py` — sync with upstream

This guards against a bug where `_compose_langsmith_project_url()` raises `ValueError`
when `LANGSMITH_TENANT_ID_PROD` is not set (it returns `None` silently instead).
Confirm the file matches the version below (apply/replace if it doesn't):

```python
"""LangSmith trace URL utilities."""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

from langsmith import Client as LangSmithClient
from langsmith.utils import LangSmithNotFoundError

from .tracing import AGENT_TRACING_PROJECT

logger = logging.getLogger(__name__)

_PROJECT_ID_CACHE: dict[str, str] = {}


def _build_prod_langsmith_client() -> LangSmithClient | None:
    api_key = (
        os.environ.get("LANGSMITH_API_KEY_PROD")
        or os.environ.get("LANGSMITH_API_KEY")
        or os.environ.get("LANGCHAIN_API_KEY")
    )
    if not api_key:
        return None
    api_url = os.environ.get("LANGSMITH_ENDPOINT_PROD") or os.environ.get(
        "LANGSMITH_ENDPOINT", "https://api.smith.langchain.com"
    )
    return LangSmithClient(api_key=api_key, api_url=api_url)


def _resolve_project_id_by_name(project_name: str) -> str | None:
    if project_name in _PROJECT_ID_CACHE:
        return _PROJECT_ID_CACHE[project_name] or None
    client = _build_prod_langsmith_client()
    if client is None:
        return None
    try:
        project = client.read_project(project_name=project_name)
    except LangSmithNotFoundError:
        _PROJECT_ID_CACHE[project_name] = ""
        return None
    except Exception:  # noqa: BLE001
        logger.debug("Could not resolve LangSmith project id for %s", project_name)
        _PROJECT_ID_CACHE[project_name] = ""
        return None
    project_id = getattr(project, "id", None)
    resolved = str(project_id) if project_id else ""
    _PROJECT_ID_CACHE[project_name] = resolved
    return resolved or None


def _compose_langsmith_project_url(project_name: str = AGENT_TRACING_PROJECT) -> str | None:
    """Returns None silently when LangSmith is not configured."""
    tenant_id = os.environ.get("LANGSMITH_TENANT_ID_PROD")
    if not tenant_id:
        return None
    host_url = os.environ.get("LANGSMITH_URL_PROD", "https://smith.langchain.com")
    project_id = _resolve_project_id_by_name(project_name) or os.environ.get(
        "LANGSMITH_TRACING_PROJECT_ID_PROD"
    )
    if not project_id:
        return None
    return f"{host_url}/o/{tenant_id}/projects/p/{project_id}"


def get_langsmith_trace_url(
    thread_id: str, project_name: str = AGENT_TRACING_PROJECT
) -> str | None:
    project_url = _compose_langsmith_project_url(project_name)
    return f"{project_url}/t/{thread_id}" if project_url else None


def _build_langsmith_feedback_clients() -> tuple[LangSmithClient, ...]:
    clients: list[LangSmithClient] = []
    seen: set[tuple[str, str]] = set()
    api_endpoint = os.environ.get("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com")
    client_configs = (
        (
            os.environ.get("LANGSMITH_API_KEY") or os.environ.get("LANGCHAIN_API_KEY"),
            api_endpoint,
        ),
        (
            os.environ.get("LANGSMITH_API_KEY_PROD"),
            os.environ.get("LANGSMITH_ENDPOINT_PROD", api_endpoint),
        ),
    )
    for api_key, api_url in client_configs:
        if not api_key or not api_url:
            continue
        identity = (api_key, api_url)
        if identity in seen:
            continue
        clients.append(LangSmithClient(api_key=api_key, api_url=api_url))
        seen.add(identity)
    return tuple(clients)


def _feedback_id(run_id: str, key: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"langsmith-feedback:{run_id}:{key}")


def create_langsmith_feedback(
    run_id: str,
    key: str,
    *,
    score: float,
    comment: str | None = None,
    source_info: dict[str, Any] | None = None,
) -> bool:
    clients = _build_langsmith_feedback_clients()
    if not clients:
        logger.debug("No LangSmith API key configured, skipping feedback")
        return False
    feedback_id = _feedback_id(run_id, key)
    any_success = False
    for client in clients:
        try:
            client.create_feedback(
                run_id=run_id,
                key=key,
                score=score,
                comment=comment,
                source_info=source_info,
                feedback_source_type="api",
                feedback_id=feedback_id,
            )
            any_success = True
        except Exception:
            try:
                client.update_feedback(feedback_id, score=score, comment=comment)
                any_success = True
            except Exception:
                logger.exception("Failed to create or update LangSmith feedback for run %s", run_id)
    return any_success


def delete_langsmith_feedback(run_id: str, key: str) -> bool:
    clients = _build_langsmith_feedback_clients()
    if not clients:
        logger.debug("No LangSmith API key configured, skipping feedback deletion")
        return False
    feedback_id = _feedback_id(run_id, key)
    any_success = False
    for client in clients:
        try:
            client.delete_feedback(feedback_id)
            any_success = True
        except LangSmithNotFoundError:
            any_success = True
        except Exception:
            logger.exception("Failed to delete LangSmith feedback for run %s", run_id)
    return any_success
```

### 6f. `Makefile` — dev target

Confirm the `dev` target disables tracing (apply if missing):

```makefile
dev:
	LANGCHAIN_TRACING_V2=false LANGCHAIN_TRACING=false uv run langgraph dev --no-browser
```

---

## Step 7 — Create `.env` files

### Project root `.env`

Create `open-swe/.env`:

```bash
# === LLM ===
LLM_MODEL_ID="openai:gpt-4o-mini"
OPENAI_API_KEY="<your OpenAI API key>"

# === GitHub App ===
GITHUB_APP_ID="<from Step 3f>"
GITHUB_APP_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----
<paste full .pem content here — preserve actual newlines>
-----END RSA PRIVATE KEY-----
"
GITHUB_APP_INSTALLATION_ID="<from Step 3g URL>"
GITHUB_APP_CLIENT_ID="<from Step 3f>"
GITHUB_APP_CLIENT_SECRET="<from Step 3f>"

# === GitHub Webhook ===
GITHUB_WEBHOOK_SECRET="<the value you used in Step 3b>"

# === Repo Allowlist ===
# For personal account (no org): leave ALLOWED_GITHUB_ORGS empty
# List repos as owner/repo — comma-separate multiple
ALLOWED_GITHUB_ORGS=""
ALLOWED_GITHUB_REPOS="<your-github-username>/<your-repo>"
DEFAULT_REPO_OWNER="<your-github-username>"
DEFAULT_REPO_NAME="<your-repo>"

# === Sandbox ===
SANDBOX_TYPE="daytona"
DAYTONA_API_KEY="<from Step 4b>"
# NOTE: the /api suffix is REQUIRED. The SDK default is https://app.daytona.io/api,
# and agent/integrations/daytona.py reads this env var. Without /api the SDK 404s.
DAYTONA_API_URL="http://localhost:3000/api"
DAYTONA_SANDBOX_SNAPSHOT="open-swe-sandbox"

# === Dashboard ===
# Port 3001 because Daytona occupies port 3000
DASHBOARD_API_BASE_URL="http://localhost:2024"
DASHBOARD_BASE_URL="http://localhost:3001"
DASHBOARD_ALLOWED_ORIGINS="http://localhost:3001"
DASHBOARD_JWT_SECRET="<run: openssl rand -hex 32>"
LANGGRAPH_URL="http://localhost:2024"
CONFIGURED_ADMINS="<your GitHub email address>"

# === Token Encryption ===
TOKEN_ENCRYPTION_KEY="<run: openssl rand -base64 32>"

# === Web Search (SearXNG) ===
SEARXNG_BASE_URL="http://localhost:8888"

# === Disable LangSmith entirely ===
LANGCHAIN_TRACING_V2=false
LANGCHAIN_TRACING=false
```

Generate the secrets now:

```bash
openssl rand -hex 32      # → paste as DASHBOARD_JWT_SECRET
openssl rand -base64 32   # → paste as TOKEN_ENCRYPTION_KEY
```

### Dashboard UI `.env`

Create `open-swe/ui/.env`:

```bash
VITE_DASHBOARD_API_BASE_URL=http://localhost:2024
```

> **Note:** The variable name is `VITE_DASHBOARD_API_BASE_URL` — not `VITE_API_BASE_URL`. The UI logs an error with the correct name if it's missing.

---

## Step 8 — Upgrade `langgraph-api`

Always use `uv` — never `pip` directly on this project:

```bash
uv add "langgraph-api>=0.10.0"
uv sync --all-extras
```

---

## Step 9 — Start everything

Use five separate terminals. Start them in this order:

```bash
# Terminal 1 — confirm Daytona is running
docker ps | grep daytona-api

# Terminal 2 — ngrok (permanent URL — replace with your domain from Step 2a)
ngrok http --url=your-name-abc123.ngrok-free.app 2024
# The URL never changes — no need to update GitHub App webhook on restarts

# Terminal 3 — SearXNG
cd ~/workspace/open-swe/searxng
docker compose up -d

# Terminal 4 — open-swe backend
cd ~/workspace/open-swe
make dev
# Wait until you see: "Application started up in X.XXXs"
# Verify: curl http://localhost:2024/ok

# Terminal 5 — open-swe dashboard UI
cd ~/workspace/open-swe/ui
bun install
bun run dev -- --port 3001
# Open: http://localhost:3001
```

---

## Step 10 — Verify the full stack

Run these checks before triggering any agent task:

```bash
# Backend health
curl http://localhost:2024/ok
# Expected: {"ok":true}

# All 6 graphs registered
curl http://localhost:2024/assistants/search \
  -H "Content-Type: application/json" \
  -d '{"limit": 10}' | python3 -m json.tool | grep graph_id
# Expected: agent, reviewer, analyzer, chat, scheduler, ci_monitor

# SearXNG working
curl "http://localhost:8888/search?q=python&format=json" | python3 -c \
  "import sys,json; d=json.load(sys.stdin); print('SearXNG OK:', len(d['results']), 'results')"

# Daytona registry accessible from runner
docker exec -it daytona-runner-1 sh -c 'curl -s http://registry:6000/v2/_catalog'
# Expected: {"repositories":["open-swe-sandbox"]}
```

---

## Step 11 — Test via chat UI

1. Open `http://localhost:3001`
2. Click **Select repository** → choose your repo
3. Select a model (GPT-5.5 None is the default — any will work for testing)
4. Type a simple task: `Add a one-line comment to README.md describing what this project does.`
5. Send it

Watch the backend logs in Terminal 3 for:
- `Creating Daytona sandbox` — sandbox is spinning up
- `Configured git and gh credentials in Daytona sandbox` — auth injected
- Cloning your repo
- Agent working

A successful run ends with a draft PR on your GitHub repo.

---

## Port Map — Full Stack

| Port | Service | Notes |
|---|---|---|
| 2024 | open-swe backend | All 6 graphs + GitHub webhook + dashboard API |
| 3000 | Daytona API | ⚠️ Occupied — dashboard shifted to 3001 |
| 3001 | open-swe dashboard UI | Shifted from default 3000 |
| 3003 | Daytona runner | Internal |
| 4000 | Daytona proxy | Internal |
| 5050 | PgAdmin | Internal |
| 5100 | Registry UI | Internal |
| 5556 | Dex auth | Internal |
| 6000 | Docker registry | Push custom sandbox image here |
| 8888 | SearXNG | Web search |
| 9091 | MinIO console | Internal |

---

## What to Skip from the Official INSTALLATION.md

| Section | Reason |
|---|---|
| Step 4a — LangSmith API key & tenant IDs | Not using LangSmith |
| Step 4b — GitHub OAuth via LangSmith | Not using LangSmith per-user OAuth |
| Step 4c — LangSmith sandbox snapshot | Replaced by Daytona |
| Step 5b — Linear trigger | Not using Linear |
| Step 5c — Slack trigger | Not using Slack |
| All `LANGSMITH_*` env vars | Not using LangSmith |
| `DEFAULT_SANDBOX_SNAPSHOT_ID` and sizing vars | LangSmith sandbox only |
| `GITHUB_OAUTH_PROVIDER_ID`, `X_SERVICE_AUTH_JWT_SECRET` | LangSmith per-user OAuth only |
| `EXA_API_KEY` | Replaced by SearXNG |
| Step 10 — Production deployment | Local dev only |

---

## Known Issues

### ✅ Resolved: `GH_TOKEN=dummy` override

**Status:** Resolved. Validated end-to-end (live sandbox: `GH_TOKEN=dummy gh auth status`
with no creds reports "not logged in" — the dummy token is stripped — and once
`hosts.yml` is seeded, `gh` reads that token instead).

**Root cause:** open-swe hardcodes `GH_TOKEN=dummy gh <command>` everywhere (a LangSmith
proxy convention). The inline `GH_TOKEN=dummy` **takes precedence over `hosts.yml`**, so
simply writing the real token to `~/.config/gh/hosts.yml` does nothing — `gh` keeps
using `dummy` and 401s on `api.github.com`.

**Fix (two halves — see Step 4c and 6c/6d):**

1. **Image wrapper (static):** the Dockerfile installs `/usr/local/bin/gh`, which
   shadows the real `/usr/bin/gh` (because `/usr/local/bin` precedes `/usr/bin` in
   `PATH`). It runs `unset GH_TOKEN GITHUB_TOKEN; exec /usr/bin/gh "$@"`, so the dummy
   token is dropped and `gh` falls back to `hosts.yml`. No token is baked in, the real
   binary is untouched, and there is no recursion.
2. **Runtime credentials (rotating):** `server.py` writes the real GitHub App
   installation token into `hosts.yml` and adds a `git config url.insteadOf` rewrite on
   each sandbox create/refresh.

> **Anti-pattern — do not do this.** An earlier attempt replaced the `gh` binary at
> runtime via `GH_REAL=$(which gh); printf '…exec "$GH_REAL"…' > wrapper; cp wrapper
> $(which gh)`. It is broken twice over: `$GH_REAL` is written literally and is empty
> when the wrapper runs, and `cp wrapper $(which gh)` overwrites the real binary so the
> wrapper execs itself (infinite recursion). It also bakes a token that goes stale on
> the hourly refresh. The image-wrapper + `hosts.yml` split above avoids all of this.

---

### 🟡 Medium: Slow graph load warning

```
Slow graph load. Accessing graph 'agent' took ~7892ms.
```

Not a blocker. Sandbox creation at startup is the cause. Cosmetic warning from `langgraph-api`.

---

### 🟡 Medium: `watchfiles` polling noise

```
3 changes detected  (every 10 seconds)
```

Hot-reload watcher hitting `.pyc` files. If distracting, add `--no-reload` to `Makefile`:

```makefile
dev:
	LANGCHAIN_TRACING_V2=false LANGCHAIN_TRACING=false uv run langgraph dev --no-browser --no-reload
```

---

### 🟢 Minor: LangSmith Studio banner in logs

```
🎨 Studio UI: https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024
For production use, please use LangSmith Deployment.
```

Hardcoded in `langgraph-api` library. Cannot be removed. No data flows to LangSmith — purely cosmetic.

---

## Daytona OSS Notes

- **Version:** v0.187.0 (visible in dashboard footer at `http://localhost:3000`)
- **As of June 2026:** Daytona OSS repo is frozen — development moved private. No further upstream fixes.
- **Snapshot form:** Only accepts a Docker image reference (no Dockerfile upload). Build images locally and push to the local registry first. `scripts/create_daytona_snapshot.py` automates build + push + recreate; snapshots are **immutable**, so bump the tag and delete/recreate on every change.
- **Snapshot create needs write permission:** the SDK `daytona.snapshot.create` / `delete` fail with `DaytonaAuthorizationError: Access denied` if the API key lacks snapshot-write scope for its org (a `daytona api-key generate` key may be read-only). Listing still works. Use a write-capable key or the dashboard.
- **Delete is async:** `snapshot.delete()` returns before deletion settles, so an immediate `create` of the same name hits `DaytonaConflictError`. The script polls until the old one is gone, then retries.
- **Registry internal hostname:** The runner container's `daemon.json` has `"insecure-registries": ["registry:6000"]`. Always use `registry:6000/<image>:<tag>` in the Daytona dashboard — not `localhost:6000` or `daytona-registry-1:6000`.
- **Default image:** `daytonaio/sandbox:0.5.0-slim` — no `gh` CLI included. Custom snapshot required.
- **Daytona dashboard login:** `dev@daytona.io` / `password`

---

## Graphs Loaded on Backend Startup (expected)

| Graph | Module | Purpose |
|---|---|---|
| `agent` | `agent.server` | Main coding agent |
| `reviewer` | `agent.reviewer` | Code review loop |
| `analyzer` | `agent.analyzer` | Repo context analysis |
| `chat` | `agent.chat` | Dashboard chat UI |
| `scheduler` | `agent.scheduler` | Task scheduling |
| `ci_monitor` | `agent.ci_monitor` | CI status monitoring |

---

## Quick Commands Reference

```bash
# Generate secrets
openssl rand -hex 32        # DASHBOARD_JWT_SECRET, GITHUB_WEBHOOK_SECRET, SEARXNG_SECRET
openssl rand -base64 32     # TOKEN_ENCRYPTION_KEY

# ngrok — always use your permanent dev domain (find it at https://dashboard.ngrok.com/domains)
ngrok http --url=your-name-abc123.ngrok-free.app 2024

# Dependencies
uv sync --all-extras                  # after any pyproject.toml change
uv add "langgraph-api>=0.10.0"        # upgrade langgraph-api (never use pip directly)

# Custom sandbox image — build + push + (re)create snapshot in one command.
# Bump the tag on every change (snapshots are immutable). See sandbox-image/README.md.
set -a; source .env; set +a
uv run python scripts/create_daytona_snapshot.py \
  --name open-swe-sandbox --image registry:6000/open-swe-sandbox:0.1.0
# --skip-build to only recreate from an already-pushed image
curl http://localhost:6000/v2/open-swe-sandbox/tags/list   # confirm the tag is pushed

# Daytona API key
docker exec -it daytona-api-1 sh -c "daytona api-key generate open-swe"

# Health checks
curl http://localhost:2024/ok
curl "http://localhost:8888/search?q=test&format=json" | python3 -m json.tool | head -10
docker exec -it daytona-runner-1 sh -c 'curl -s http://registry:6000/v2/_catalog'

# Logs
docker compose -f searxng/docker-compose.yml logs -f core   # SearXNG logs
docker logs daytona-runner-1 -f                              # Daytona runner logs
```
---

## Staying Up to Date with Upstream open-swe

> **Current sync status:** see [upstream-sync-status.md](upstream-sync-status.md) — updated each time the daily workflow is run.

### The strategy

```
main              → always mirrors langchain-ai/open-swe exactly
local/self-hosted → your branch with all customizations on top
```

You never commit your changes to `main`. Your customizations stay on `local/self-hosted` and get rebased onto `main` whenever upstream ships new commits.

---

### One-time setup (already done — kept here for reference)

```bash
# Add the original repo as "upstream"
git remote add upstream https://github.com/langchain-ai/open-swe.git

# Point origin to your fork
git remote set-url origin https://github.com/ghosh-sayak/open-swe.git

# Verify
git remote -v
# origin    https://github.com/ghosh-sayak/open-swe.git (fetch)
# upstream  https://github.com/langchain-ai/open-swe.git (fetch)

# Create your customization branch from main
git checkout -b local/self-hosted

# Commit all your changes
git add Makefile agent/integrations/daytona.py agent/server.py \
  agent/tools/web_search.py agent/utils/langsmith.py \
  pyproject.toml uv.lock .gitignore \
  customization/ sandbox-image/ scripts/create_daytona_snapshot.py

git commit -m "chore: self-hosted setup (Daytona + SearXNG, no LangSmith)"

# Push both branches to your fork
git push origin main
git push origin local/self-hosted
```

---

### Daily workflow — pulling upstream updates

**Step 1: Check if upstream has new commits in your files**

```bash
git fetch upstream

git log main..upstream/main --oneline -- \
  agent/server.py \
  agent/tools/web_search.py \
  agent/utils/langsmith.py \
  agent/integrations/daytona.py \
  pyproject.toml \
  Makefile
```

If the output is empty — nothing to do. If commits appear, continue to Step 2.

**Step 2: Preview what upstream changed in your files**

```bash
git diff main upstream/main -- \
  agent/server.py \
  agent/tools/web_search.py \
  agent/utils/langsmith.py \
  agent/integrations/daytona.py \
  pyproject.toml \
  Makefile
```

Read the diff carefully before proceeding — especially changes to `agent/server.py` and `agent/integrations/daytona.py`.

**Step 3: Sync main with upstream**

```bash
git checkout main
git merge upstream/main --ff-only
```

`--ff-only` ensures `main` never gets your own commits. If it fails, something went wrong — stop and investigate before continuing.

**Step 4: Rebase your customizations onto the new main**

```bash
git checkout local/self-hosted
git rebase main
```

**Step 5: Resolve conflicts if any**

Git pauses on each conflict. For each conflicted file:

```bash
# See which files have conflicts
git status

# For each conflicted file — open it, find conflict markers (<<<, ===, >>>)
# Decide what to keep, edit the file, remove the markers

# Mark resolved
git add <resolved-file>

# Continue
git rebase --continue

# If something goes badly wrong and you want to start over
git rebase --abort
```

**Known conflict patterns for this setup:**

| File | What to do |
|---|---|
| `agent/utils/langsmith.py` | Accept upstream version — it already has our fix |
| `agent/tools/web_search.py` | Keep your SearXNG version entirely — discard upstream's Exa code |
| `agent/server.py` | Keep your Daytona `elif` blocks, accept upstream's new additions around them |
| `pyproject.toml` | Accept upstream's version bumps AND keep your `exa-py` removal |
| `Makefile` | Keep your changes — upstream rarely touches this |

**Step 6: Push updated branches**

```bash
git push origin main
git push origin local/self-hosted
```

---

### Branch naming for future work

| What you're doing | Branch to use |
|---|---|
| Infrastructure customization (sandbox, search, auth) | Commit directly to `local/self-hosted` |
| New feature or experiment | Create `feature/xxx` off `local/self-hosted`, merge back when done |
| Fix to contribute back to upstream | Create `fix/xxx` off `main`, open PR to `langchain-ai/open-swe` |

---

### Your fork on GitHub

| Branch | Purpose |
|---|---|
| `main` | Clean mirror of `langchain-ai/open-swe` — never commit here directly |
| `local/self-hosted` | All your customizations — this is your working branch |

> **Ignore** the GitHub prompt to open a pull request when you push `local/self-hosted` — that is GitHub suggesting you PR your changes into `langchain-ai/open-swe`. You do not want to do that.
