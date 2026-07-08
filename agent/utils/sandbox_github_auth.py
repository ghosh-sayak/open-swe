"""Shared hosts.yml GitHub auth for providers without a GitHub proxy (plan D3).

Mirrors the daytona-proven pattern: the prompts always run `GH_TOKEN=dummy gh`,
and the sandbox image ships a gh wrapper that strips that dummy token so gh
falls back to the hosts.yml written here. Real git traffic authenticates via
the insteadOf credential rewrite. Called after create/claim and on every
refresh; because the token is embedded in the insteadOf *section name*, stale
x-access-token sections are removed first — otherwise git keeps resolving to
the first (expired) token after rotation.
"""

from __future__ import annotations

import re

from deepagents.backends.protocol import SandboxBackendProtocol

_TOKEN_RE = re.compile(r"[A-Za-z0-9_.\-]+")

_REMOVE_STALE_SECTIONS = (
    "for s in $(git config --global --name-only --get-regexp "
    "'^url\\.https://x-access-token:.*\\.insteadof$' 2>/dev/null "
    "| sed 's/\\.insteadof$//'); do "
    'git config --global --remove-section "$s" || true; done'
)


def configure_github_auth(sandbox_backend: SandboxBackendProtocol, token: str) -> None:
    """Write git + gh credentials into the sandbox via the exec channel."""
    if not _TOKEN_RE.fullmatch(token):
        # Never interpolate an unexpected value into a shell command / printf format.
        raise ValueError("GitHub token contains unexpected characters; refusing to write it")
    setup_commands = " && ".join(
        [
            _REMOVE_STALE_SECTIONS,
            f"git config --global url.'https://x-access-token:{token}@github.com/'"
            f".insteadOf 'https://github.com/'",
            "mkdir -p /root/.config/gh",
            f"printf 'github.com:\\n  oauth_token: {token}\\n  git_protocol: https\\n"
            f"  user: x-access-token\\n' > /root/.config/gh/hosts.yml",
        ]
    )
    result = sandbox_backend.execute(setup_commands)
    if result.exit_code != 0:
        # Deliberately omits command output: a shell error could echo the token.
        raise RuntimeError(
            f"Failed to configure GitHub auth in sandbox {sandbox_backend.id} "
            f"(exit code {result.exit_code})"
        )
