# OpenSandbox provider runbook (`SANDBOX_TYPE=opensandbox`)

Runs each agent thread in an [OpenSandbox](https://github.com/alibaba/OpenSandbox) sandbox —
a local Docker Compose server for dev, a Kubernetes-deployed server for prod, with **no
application code change between the two** (endpoint/config only). Design record:
`plans/opensandbox-backend-integration.md`.

- Integration: `agent/integrations/opensandbox.py` (`OpensandboxBackend` on the sync SDK)
- GitHub auth: `agent/utils/sandbox_github_auth.py` — hosts.yml + `insteadOf` written into the
  sandbox over exec after create/claim and rewritten on every reuse (rotation-safe). The prompts'
  `GH_TOKEN=dummy gh` convention works because the sandbox image ships a gh wrapper that strips
  the dummy token so gh falls back to hosts.yml. **No GitHub proxy and no secrets baked into the
  image**; the GitHub App private key lives only in the agent runtime's env (locally via `.env`;
  in k8s injected by Akeyless as Secret→env on the agent Deployment).
- Sandbox image: `sandbox-image/` (see its README; verbatim copy of the init-swe Dockerfile).

## Environment variables

| Var | Meaning | Default |
|---|---|---|
| `SANDBOX_TYPE` | select the provider | `langsmith` (set to `opensandbox`) |
| `OPEN_SANDBOX_DOMAIN` | server `host:port` (no scheme) | `localhost:8080` (local compose uses `localhost:8090`) |
| `OPEN_SANDBOX_API_KEY` | server API key | required (validated at startup + `/health` probe) |
| `OPEN_SANDBOX_IMAGE` | sandbox OCI image | `open-swe-sandbox:latest` |
| `OPEN_SANDBOX_TTL_SECONDS` | absolute TTL, renewed on every reconnect/ping (sliding window) | `7200` |
| `OPEN_SANDBOX_COMMAND_TIMEOUT_SECONDS` | server-side per-command timeout | `1800` |
| `OPEN_SANDBOX_USE_SERVER_PROXY` | route exec/file traffic via the server (needed when the client can't reach sandbox IPs: server-in-Docker/client-on-host, or open-swe outside the k8s cluster) | `false` (`true` for local dev) |
| `OPEN_SANDBOX_CPU` / `OPEN_SANDBOX_MEMORY` | per-sandbox resources | `2` / `4Gi` |
| `OPEN_SANDBOX_POOL_ENABLED` | enable pre-warmed pooling | `false` |
| `OPEN_SANDBOX_POOL_REF` | k8s Pool CRD name; with pooling enabled, claims via `extensions.poolRef` | unset |

Pooling matrix (plan D4): k8s prod = server-side Pool CRD + `OPEN_SANDBOX_POOL_ENABLED=true` +
`OPEN_SANDBOX_POOL_REF=<pool>`; local Docker rejects `poolRef`, so enabling pooling without a
ref uses a client-side `SandboxPoolSync` eager-create pool — off by default (the image is heavy;
the sliding TTL already keeps a thread's sandbox warm between messages).

## Local dev

The canonical local deployment lives in the **sibling repo** at
`init-swe/infra/opensandbox/local-osb/` (compose file + config; deliberately not duplicated here).

1. `docker network create local-net` (if absent); `docker pull opensandbox/execd:v1.0.20`.
2. Build the sandbox image from this repo: `cd sandbox-image && docker build -f open-swe-sandbox.Dockerfile -t open-swe-sandbox:latest .` (no registry needed — the Docker runtime is local-first).
3. `cd ~/workspace/init-swe/infra/opensandbox/local-osb && docker compose up -d`, then `curl localhost:8090/health` → `{"status":"healthy"}`.
4. `.env` in this repo:
   ```bash
   SANDBOX_TYPE=opensandbox
   OPEN_SANDBOX_DOMAIN=localhost:8090
   OPEN_SANDBOX_API_KEY=...       # matches OPENSANDBOX_API_KEY in local-osb/.env
   OPEN_SANDBOX_IMAGE=open-swe-sandbox:latest
   OPEN_SANDBOX_USE_SERVER_PROXY=true
   # GitHub App creds (GITHUB_APP_ID, GITHUB_APP_INSTALLATION_ID, GITHUB_APP_PRIVATE_KEY)
   # stay as for the other providers — the token is minted at runtime and written into
   # the sandbox over exec, never baked into the image.
   ```
5. `make dev`, start a fresh thread.

## Kubernetes

Deploy the OpenSandbox umbrella Helm chart (server + controller, `runtime.type="kubernetes"`,
`workload_provider="batchsandbox"`), create a Pool CRD for pre-warmed pods, and point open-swe at
it — config only: `OPEN_SANDBOX_DOMAIN=<server svc/ingress>`, `OPEN_SANDBOX_API_KEY`,
`OPEN_SANDBOX_USE_SERVER_PROXY=true` if open-swe runs outside the cluster,
`OPEN_SANDBOX_POOL_ENABLED=true`, `OPEN_SANDBOX_POOL_REF=<pool>`. Push `OPEN_SANDBOX_IMAGE`
to a registry the nodes can pull, and raise `kubernetes.sandbox_create_timeout_seconds` (or
preload the image on nodes) for large first pulls.

## Failure semantics (plan D5/D6)

Dead/unreachable sandboxes (connection errors, API 404/5xx — e.g. TTL expiry) are recreated
automatically at the start-of-run ping, on reconnect, and mid-run via `ToolErrorMiddleware`;
auth/config errors (401/403, bad env) are surfaced instead — recreation is destructive and is
never triggered by a non-sandbox bug. The circuit breaker recognizes the structured
`error_class`/`sandbox_id` payload fields (UUID ids). TTL is absolute but renewed on every
reconnect/ping, so active threads stay warm and abandoned ones self-expire.

## Tests

- Unit: `make test` (no server needed; the SDK is faked).
- Integration (real server): `OPENSANDBOX_INTEGRATION=1 OPEN_SANDBOX_DOMAIN=localhost:8090 OPEN_SANDBOX_API_KEY=... OPEN_SANDBOX_USE_SERVER_PROXY=true make integration_tests`

## Rollback

Fully additive and env-gated: unset `SANDBOX_TYPE` (or set it back to `langsmith`) and the
langsmith path behaves exactly as before (the D5 recovery generalization is a no-op for it).