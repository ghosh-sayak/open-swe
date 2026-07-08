# open-swe sandbox image (OpenSandbox provider)

The OCI image every `SANDBOX_TYPE=opensandbox` thread runs in. This is the **target-workload**
image — JDK + Kotlin + Gradle + the JetBrains Kotlin LSP, plus git/gh/ripgrep — not the agent's
Python runtime. OpenSandbox injects its `execd` control daemon at runtime, so nothing
OpenSandbox-specific is baked in, and the image's `CMD` is ignored.

The Dockerfile is a verbatim copy of
`init-swe/infra/opensandbox/local-osb/custom-sandbox-image/open-swe-sandbox.Dockerfile`.
It includes the `gh` wrapper (`unset GH_TOKEN GITHUB_TOKEN; exec /usr/bin/gh`) that the
`hosts.yml` GitHub-auth path depends on — see `plans/opensandbox-backend-integration.md` (D3, D7).
No secrets are baked in; the GitHub token is written into the running sandbox over exec and
refreshed hourly.

## Build

```bash
cd sandbox-image
docker build -f open-swe-sandbox.Dockerfile -t open-swe-sandbox:latest .
```

OpenSandbox's Docker runtime is local-first: if the tag exists on the host daemon with a matching
platform it is never pulled, so **no registry is required** for local dev. For Kubernetes, push the
image to a registry your nodes can pull from and set `OPEN_SANDBOX_IMAGE` accordingly.

Toolchain versions are build ARGs (`JAVA_VERSION`, `GRADLE_VERSION`, `KOTLIN_VERSION`, …) — see
the Dockerfile header to retarget the image per project.

## Local OpenSandbox server

The canonical local deployment (Docker Compose) lives in the sibling repo at
`init-swe/infra/opensandbox/local-osb/` — see the `customization/` runbook for the full
`SANDBOX_TYPE=opensandbox` setup (env vars, health check, smoke test).