from __future__ import annotations

import os
import signal


def crash_hook(point: str) -> None:
    """Test-only deterministic crash injection; never active without both guards."""
    if os.environ.get("NIGHTWATCH_ENABLE_TEST_CRASH_HOOKS") != "1" or os.environ.get("NIGHTWATCH_TEST_CRASH_POINT") != point:
        return
    once = os.environ.get("NIGHTWATCH_TEST_CRASH_ONCE_FILE")
    if once:
        try:
            fd = os.open(once, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError:
            return
    os.kill(os.getpid(), signal.SIGKILL)
