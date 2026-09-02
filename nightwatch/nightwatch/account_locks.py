"""Short-lived locks for canonical codex-auth registry synchronization.

Account lease acquisition must happen before this lock.  The registry lock is
held only while one codex-auth command reads or mutates the canonical registry;
it is never held across a provider or App Server operation.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import fcntl

from .account_errors import AccountBusy, AccountSchemaError
from .storage import control_plane_root, now_iso
from .testing import crash_hook


class AccountRegistryLock(AbstractContextManager["AccountRegistryLock"]):
    """One kernel-held canonical registry lock with auditable ownership."""

    def __init__(self, handle: Any, path: Path, metadata: dict[str, Any]):
        self._handle = handle
        self.path = path
        self.metadata = metadata
        self._released = False

    def __enter__(self) -> "AccountRegistryLock":
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.release()

    def release(self) -> None:
        if self._released:
            return
        try:
            current = AccountRegistryLockBroker._read_metadata(self._handle)
            if current != self.metadata:
                raise AccountSchemaError("canonical registry lock ownership changed unexpectedly")
            self._handle.seek(0)
            self._handle.truncate()
            self._handle.flush()
            os.fsync(self._handle.fileno())
        finally:
            crash_hook("BEFORE_REGISTRY_UNLOCK")
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._released = True

    def _write_metadata(self, value: dict[str, Any]) -> None:
        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
        self._handle.flush()
        os.fsync(self._handle.fileno())


def _linux_process_identity(pid: int) -> dict[str, Any] | None:
    if not sys.platform.startswith("linux") or pid <= 0:
        return None
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields = stat_text.rsplit(")", 1)[-1].strip().split()
        return {"pid": pid, "starttime": fields[19], "executable": os.readlink(f"/proc/{pid}/exe")}
    except (OSError, IndexError):
        return None


def _identity_matches(record: dict[str, Any]) -> bool:
    pid = record.get("pid")
    if not isinstance(pid, int):
        return False
    observed = _linux_process_identity(pid)
    return bool(observed and observed.get("starttime") == record.get("starttime") and observed.get("executable") == record.get("executable"))


class AccountRegistryLockBroker:
    """Fail-closed filesystem lock for canonical codex-auth registry work."""

    def __init__(self, root: str | Path | None = None, timeout: float = 10.0):
        candidate = Path(root or control_plane_root()).expanduser()
        if candidate.exists() or candidate.is_symlink():
            info = os.lstat(candidate)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise AccountSchemaError("canonical registry lock root must be a real directory")
        candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = os.lstat(candidate)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise AccountSchemaError("canonical registry lock root is unsafe")
        self.root = candidate.resolve()
        self._root_identity = (info.st_dev, info.st_ino)
        self.path = self.root / "account-registry.lock"
        self.timeout = max(0.0, float(timeout))
        os.chmod(self.root, 0o700)

    def _assert_root(self) -> None:
        try:
            info = os.lstat(self.root)
        except OSError as exc:
            raise AccountSchemaError("canonical registry lock root disappeared") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != self._root_identity:
            raise AccountSchemaError("canonical registry lock root was replaced")

    def acquire(self, *, operation: str = "registry", timeout: float | None = None) -> AccountRegistryLock:
        crash_hook("BEFORE_REGISTRY_LOCK")
        self._assert_root()
        path = self.path
        try:
            existing = os.lstat(path)
            if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
                raise AccountSchemaError("canonical registry lock path is not a regular file")
        except FileNotFoundError:
            pass
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
            opened = os.fstat(descriptor)
            if stat.S_ISLNK(opened.st_mode) or not stat.S_ISREG(opened.st_mode):
                os.close(descriptor)
                raise AccountSchemaError("canonical registry lock path is unsafe")
            handle = os.fdopen(descriptor, "r+", encoding="utf-8")
        except OSError as exc:
            raise AccountSchemaError("canonical registry lock path cannot be opened safely") from exc
        try:
            # Re-check the directory inode after open so a rename/replacement
            # race cannot silently move this lock operation to an old root.
            self._assert_root()
            os.fchmod(handle.fileno(), 0o600)
            limit = self.timeout if timeout is None else max(0.0, float(timeout))
            deadline = time.monotonic() + limit
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise AccountBusy("canonical registry lock is busy") from exc
                    time.sleep(min(0.01, max(0.001, deadline - time.monotonic())))
            previous = self._read_metadata(handle)
            if previous is not None:
                required = {"schema_version", "lock_kind", "pid", "starttime", "executable", "operation", "acquired_at"}
                if set(previous) < required or previous.get("schema_version") != 1 or previous.get("lock_kind") != "canonical_registry":
                    raise AccountSchemaError("canonical registry lock metadata is invalid")
                if _identity_matches(previous):
                    raise AccountBusy("canonical registry lock metadata identifies a live owner")
            identity = _linux_process_identity(os.getpid())
            if identity is None:
                raise AccountSchemaError("Linux PID identity is unavailable for canonical registry lock")
            metadata = {
                "schema_version": 1,
                "lock_kind": "canonical_registry",
                "pid": identity["pid"],
                "starttime": identity["starttime"],
                "executable": identity["executable"],
                "operation": str(operation)[:80],
                "acquired_at": now_iso(),
            }
            lock = AccountRegistryLock(handle, path, metadata)
            lock._write_metadata(metadata)
            crash_hook("AFTER_REGISTRY_LOCK")
            return lock
        except Exception:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
            raise

    @staticmethod
    def _read_metadata(handle: Any) -> dict[str, Any] | None:
        handle.seek(0)
        text = handle.read()
        if len(text.encode("utf-8", errors="replace")) > 64 * 1024:
            raise AccountSchemaError("canonical registry lock metadata is too large")
        if not text.strip():
            return None
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AccountSchemaError("canonical registry lock metadata is corrupt") from exc
        if not isinstance(value, dict):
            raise AccountSchemaError("canonical registry lock metadata is not an object")
        return value
