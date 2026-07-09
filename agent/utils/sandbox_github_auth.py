"""Shared GitHub auth for sandboxes without a GitHub proxy (plan D3).

Mirrors the daytona-proven pattern: the prompts always run `GH_TOKEN=dummy gh`,
and the sandbox image ships a gh wrapper that strips that dummy token so gh
falls back to the hosts.yml written here. Real git traffic authenticates via
the insteadOf credential rewrite. Called after create/claim and on every
refresh; the token travels only in file *content*, written via the sandbox's
file-upload API. The insteadOf rule lives in a dedicated git include file that
is rewritten wholesale on every call, so a rotated token never leaves a stale
section behind and the bot identity in ~/.gitconfig is never touched.
"""

from __future__ import annotations

import logging
import re
import time

from deepagents.backends.protocol import SandboxBackendProtocol

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[A-Za-z0-9_.\-]+")

INSTEADOF_INCLUDE_PATH = "/root/.config/git/insteadof.gitconfig"
GH_HOSTS_PATH = "/root/.config/gh/hosts.yml"

AUTH_CONFIG_MAX_ATTEMPTS = 3
AUTH_CONFIG_RETRY_DELAYS = (0.5, 1.0)


def _credential_files(token: str) -> list[tuple[str, bytes]]:
    """Build the (path, content) pairs written into the sandbox.

    The token lives only in file *content* (never on a command line). The
    insteadOf rule goes in a dedicated include file that is rewritten wholesale
    on every call, so a rotated token cannot leave a stale section behind and
    the bot identity in ~/.gitconfig is never touched.
    """
    insteadof = (
        f'[url "https://x-access-token:{token}@github.com/"]\n\tinsteadOf = https://github.com/\n'
    )
    hosts = f"github.com:\n  oauth_token: {token}\n  git_protocol: https\n  user: x-access-token\n"
    return [
        (INSTEADOF_INCLUDE_PATH, insteadof.encode()),
        (GH_HOSTS_PATH, hosts.encode()),
    ]


def _write_credentials_once(sandbox_backend: SandboxBackendProtocol, token: str) -> None:
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


def configure_github_auth(sandbox_backend: SandboxBackendProtocol, token: str) -> None:
    """Write git + gh credentials into the sandbox via the file API, with retry."""
    if not _TOKEN_RE.fullmatch(token):
        # Defense in depth: never let an unexpected value reach a file/command.
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
