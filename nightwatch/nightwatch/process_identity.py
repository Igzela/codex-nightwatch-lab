from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


def sys_platform_linux() -> bool:
    return sys.platform.startswith("linux")


def pid_alive(pid: int | None) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        if sys_platform_linux():
            try:
                stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
                state = stat.rsplit(")", 1)[-1].strip().split(maxsplit=1)[0]
                if state == "Z":
                    return False
            except OSError:
                return False
        return True
    except OSError:
        return False


def linux_process_identity(pid: int) -> dict[str, Any] | None:
    """Linux PID identity resistant to PID reuse."""
    if not sys_platform_linux() or not pid_alive(pid):
        return None
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields = stat_text.rsplit(")", 1)[-1].strip().split()
        starttime = fields[19]
        executable = os.readlink(f"/proc/{pid}/exe")
    except (OSError, IndexError):
        return None
    return {"pid": pid, "starttime": starttime, "executable": executable}


def process_identity_matches(record: dict[str, Any]) -> bool:
    pid = record.get("pid")
    if not isinstance(pid, int):
        return False
    observed = linux_process_identity(pid)
    return bool(
        observed
        and observed.get("starttime") == record.get("starttime")
        and observed.get("executable") == record.get("executable")
    )
