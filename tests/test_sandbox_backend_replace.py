"""Backend-replacement lifecycle: local transports must be closed on swap."""

from agent.utils.sandbox_state import clear_sandbox_backend, set_sandbox_backend


class _ClosableBackend:
    owns_local_transport = True

    def __init__(self, backend_id: str):
        self.id = backend_id
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _PlainBackend:
    def __init__(self, backend_id: str):
        self.id = backend_id
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_replace_closes_old_backend_that_owns_transport():
    old = _ClosableBackend("uuid-old")
    set_sandbox_backend("t-close-1", old)
    try:
        set_sandbox_backend("t-close-1", _ClosableBackend("uuid-new"))
        assert old.closed is True
    finally:
        clear_sandbox_backend("t-close-1")


def test_replace_does_not_close_backends_without_marker():
    """langsmith-and-friends zero-delta: no close call on unmarked backends."""
    old = _PlainBackend("sb-old")
    set_sandbox_backend("t-close-2", old)
    try:
        set_sandbox_backend("t-close-2", _PlainBackend("sb-new"))
        assert old.closed is False
    finally:
        clear_sandbox_backend("t-close-2")


def test_replace_with_same_backend_does_not_close_it():
    backend = _ClosableBackend("uuid-same")
    set_sandbox_backend("t-close-3", backend)
    try:
        set_sandbox_backend("t-close-3", backend)
        assert backend.closed is False
    finally:
        clear_sandbox_backend("t-close-3")


def test_replace_close_failure_is_swallowed():
    old = _ClosableBackend("uuid-boom")

    def boom() -> None:
        raise RuntimeError("transport already closed")

    old.close = boom
    set_sandbox_backend("t-close-4", old)
    try:
        proxy = set_sandbox_backend("t-close-4", _ClosableBackend("uuid-new"))
        assert proxy.id == "uuid-new"
    finally:
        clear_sandbox_backend("t-close-4")
