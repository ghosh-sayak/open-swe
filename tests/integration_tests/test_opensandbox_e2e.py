"""End-to-end tests against a real local OpenSandbox server (plan §11).

Gated: set OPENSANDBOX_INTEGRATION=1 with a running server (see the
customization runbook) and OPEN_SANDBOX_API_KEY/OPEN_SANDBOX_DOMAIN exported.
Run: OPENSANDBOX_INTEGRATION=1 make integration_tests
"""

import os
from datetime import timedelta

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("OPENSANDBOX_INTEGRATION") != "1",
    reason="set OPENSANDBOX_INTEGRATION=1 (requires a running OpenSandbox server)",
)

pytest.importorskip("opensandbox")


@pytest.fixture(scope="module")
def backend():
    if not os.environ.get("OPEN_SANDBOX_API_KEY"):
        pytest.skip("OPEN_SANDBOX_API_KEY not set")
    from agent.integrations.opensandbox import create_opensandbox_sandbox

    sandbox_backend = create_opensandbox_sandbox(None)
    yield sandbox_backend
    sandbox_backend.kill()
    sandbox_backend.close()


def test_execute_echo_ok(backend):
    result = backend.execute("echo ok")

    assert result.exit_code == 0
    assert "ok" in result.output


def test_execute_nonzero_exit_code(backend):
    result = backend.execute("exit 3")

    assert result.exit_code == 3


def test_execute_merges_stderr(backend):
    result = backend.execute("echo out && echo err >&2")

    assert result.exit_code == 0
    assert "out" in result.output
    assert "err" in result.output


def test_write_read_edit_grep_glob_roundtrip(backend):
    write = backend.write("/workspace/e2e/roundtrip.txt", "hello opensandbox\nsecond line\n")
    assert write.error is None, write.error

    read = backend.read("/workspace/e2e/roundtrip.txt")
    assert read.error is None, read.error
    assert "hello opensandbox" in read.file_data["content"]

    edit = backend.edit("/workspace/e2e/roundtrip.txt", "hello opensandbox", "hello e2e")
    assert edit.error is None, edit.error

    grep = backend.grep("hello e2e", path="/workspace/e2e")
    assert grep.matches, f"expected a grep match, got {grep!r}"

    glob = backend.glob("*.txt", path="/workspace/e2e")
    assert glob.matches is not None
    assert any(m["path"].endswith("roundtrip.txt") for m in glob.matches)


def test_upload_download_bytes_roundtrip(backend):
    payload = b"\x00binary\xffpayload"

    uploads = backend.upload_files([("/workspace/e2e/deep/nested/blob.bin", payload)])
    assert uploads[0].error is None, uploads[0].error

    downloads = backend.download_files(["/workspace/e2e/deep/nested/blob.bin"])
    assert downloads[0].error is None, downloads[0].error
    assert downloads[0].content == payload


def test_download_missing_file_partial_success(backend):
    downloads = backend.download_files(["/workspace/e2e/does-not-exist.bin"])

    assert downloads[0].content is None
    assert downloads[0].error is not None


def test_github_auth_rotation_resolves_to_newest_token(backend):
    """Refresh with a rotated token must win: git resolves insteadOf to the NEW token."""
    from agent.utils.sandbox_github_auth import INSTEADOF_INCLUDE_PATH, configure_github_auth

    configure_github_auth(backend, "ghs_OLDTOKEN000000")
    configure_github_auth(backend, "ghs_NEWTOKEN111111")

    resolved = backend.execute("git ls-remote --get-url https://github.com/example/repo.git")
    assert resolved.exit_code == 0
    assert "ghs_NEWTOKEN111111" in resolved.output
    assert "ghs_OLDTOKEN000000" not in resolved.output

    # The insteadOf section now lives in the dedicated include file, which is
    # rewritten wholesale on every call, so exactly one section resolves. Note:
    # no --global here — that flag restricts the read to literally ~/.gitconfig
    # and does not follow include.path, so it would never see this section.
    sections = backend.execute(
        "git config --name-only --get-regexp "
        "'^url\\.https://x-access-token:.*\\.insteadof$' | wc -l"
    )
    assert sections.output.strip() == "1", sections.output

    # include.path itself must also stay a single entry across repeated calls;
    # --replace-all in configure_github_auth is what guarantees this.
    includes = backend.execute("git config --global --get-all include.path | wc -l")
    assert includes.output.strip() == "1", includes.output

    hosts = backend.execute("cat /root/.config/gh/hosts.yml")
    assert "ghs_NEWTOKEN111111" in hosts.output
    assert "ghs_OLDTOKEN000000" not in hosts.output

    backend.execute("git config --global --unset-all include.path")
    backend.execute(f"rm -f {INSTEADOF_INCLUDE_PATH}")


def test_reconnect_by_id_and_renew(backend):
    from agent.integrations.opensandbox import create_opensandbox_sandbox

    second = create_opensandbox_sandbox(backend.id)
    try:
        assert second.id == backend.id
        result = second.execute("echo reconnected")
        assert result.exit_code == 0
        assert "reconnected" in result.output
        second.renew_ttl()
    finally:
        second.close()


def test_kill_makes_sandbox_unreachable():
    if not os.environ.get("OPEN_SANDBOX_API_KEY"):
        pytest.skip("OPEN_SANDBOX_API_KEY not set")
    from opensandbox.exceptions import SandboxException

    from agent.integrations.opensandbox import (
        create_opensandbox_sandbox,
        is_recoverable_sandbox_error,
    )

    victim = create_opensandbox_sandbox(None)
    victim.kill()
    victim.close()

    from opensandbox.config.connection_sync import ConnectionConfigSync
    from opensandbox.sync import SandboxSync

    with pytest.raises(SandboxException) as excinfo:
        sandbox = SandboxSync.connect(
            victim.id,
            connection_config=ConnectionConfigSync(
                domain=os.environ.get("OPEN_SANDBOX_DOMAIN", "localhost:8080"),
                api_key=os.environ["OPEN_SANDBOX_API_KEY"],
                use_server_proxy=True,
            ),
            connect_timeout=timedelta(seconds=10),
        )
        sandbox.commands.run("echo should-not-run")

    assert is_recoverable_sandbox_error(excinfo.value), (
        f"killed-sandbox error should be recoverable: {excinfo.value!r}"
    )
