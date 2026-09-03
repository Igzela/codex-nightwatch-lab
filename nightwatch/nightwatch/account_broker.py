"""Optional codex-auth integration and the global OAuth-account lease seam.

The adapter deliberately consumes only codex-auth's versioned JSON contract.
Quota values in that contract are display metadata; selection accepts only a
fresh snapshot supplied by Nightwatch's App Server quota authority.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fcntl

from .account_errors import AccountBrokerError, AccountBusy, AccountSchemaError, AccountUnavailable
from .account_locks import AccountRegistryLockBroker
from .models import QuotaSnapshot
from .storage import control_plane_root, now_iso
from .testing import crash_hook


SUPPORTED_JSON_SCHEMA = 1
MAX_JSON_BYTES = 1_000_000


def _looks_like_auth_failure(value: BaseException) -> bool:
    text = str(value).casefold()
    return any(marker in text for marker in ("unauthorized", "authentication", "login required", "invalid api", "401", "403"))


def account_fingerprint(account_key: str) -> str:
    if not isinstance(account_key, str) or not account_key.strip():
        raise ValueError("account_key must be a non-empty string")
    return "acct-" + hashlib.sha256(account_key.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class AccountRecord:
    """Safe account metadata from codex-auth, excluding auth and usage blobs."""

    account_key: str
    alias: str | None = None
    account_name: str | None = None
    plan: str | None = None
    auth_mode: str | None = None
    active: bool = False

    @property
    def fingerprint(self) -> str:
        return account_fingerprint(self.account_key)

    @property
    def display_name(self) -> str:
        for value in (self.alias, self.account_name):
            if isinstance(value, str) and value.strip():
                return " ".join(value.split())[:80]
        return self.fingerprint


@dataclass(frozen=True)
class AccountCandidate:
    """Selection input; quota must be fresh App Server evidence."""

    account_key: str
    quota: QuotaSnapshot | None
    leased: bool = False
    auth_error: bool = False

    @property
    def fingerprint(self) -> str:
        return account_fingerprint(self.account_key)


class CodexAuthAdapter:
    """Thin, timeout-bounded adapter for the optional codex-auth CLI."""

    def __init__(
        self,
        binary: str | None = None,
        codex_home: str | Path | None = None,
        timeout: float = 10.0,
        registry_lock: AccountRegistryLockBroker | None = None,
        canonical_registry: bool = True,
    ) -> None:
        self.binary = binary or os.environ.get("NIGHTWATCH_CODEX_AUTH_BIN", "codex-auth")
        self.codex_home = Path(codex_home or os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()
        self.timeout = timeout
        self.canonical_registry = canonical_registry
        self.registry_lock = registry_lock
        self._registry_context = ContextVar(f"nightwatch-registry-{id(self)}", default=None)

    def _resolved_binary(self) -> str | None:
        if os.sep in self.binary:
            path = Path(self.binary)
            return str(path) if path.is_file() and os.access(path, os.X_OK) else None
        return shutil.which(self.binary)

    def _environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        environment["CODEX_HOME"] = str(self.codex_home)
        return environment

    def available(self) -> bool:
        """Return whether discovery can prove the JSON capability exists."""
        if self._resolved_binary() is None:
            return False
        try:
            self.list_accounts()
        except AccountBrokerError:
            return False
        return True

    def list_accounts(self, *, active_only: bool = False) -> list[AccountRecord]:
        argv = ["list"]
        if active_only:
            argv.append("--active")
        argv.extend(["--skip-api", "--json"])
        document = self._run_json(argv)
        accounts = document.get("accounts")
        if not isinstance(accounts, list):
            raise AccountSchemaError("codex-auth list JSON has no accounts array")
        result: list[AccountRecord] = []
        for item in accounts:
            if not isinstance(item, dict):
                raise AccountSchemaError("codex-auth account entry is not an object")
            key = item.get("account_key")
            if not self._valid_account_key(key):
                raise AccountSchemaError("codex-auth account entry has no stable account_key")
            result.append(
                AccountRecord(
                    account_key=key,
                    alias=self._optional_text(item.get("alias")),
                    account_name=self._optional_text(item.get("account_name")),
                    plan=self._optional_text(item.get("plan")),
                    auth_mode=self._optional_text(item.get("auth_mode")),
                    active=item.get("active") is True,
                )
            )
        return result

    def active_account(self) -> AccountRecord | None:
        accounts = self.list_accounts(active_only=True)
        active = [item for item in accounts if item.active]
        if len(active) > 1:
            raise AccountSchemaError("codex-auth reported multiple active accounts")
        return active[0] if active else (accounts[0] if len(accounts) == 1 else None)

    def switch(self, account_key: str) -> AccountRecord:
        """Activate exactly one previously discovered stable account key."""
        if not isinstance(account_key, str) or not account_key.strip():
            raise ValueError("account_key must be a non-empty string")
        document = self._run_json(["switch", account_key, "--json"])
        switched = document.get("switched_to")
        if not isinstance(switched, dict):
            raise AccountSchemaError("codex-auth switch JSON has no switched_to account")
        if switched.get("account_key") != account_key:
            raise AccountSchemaError("codex-auth switched to an unexpected account")
        return AccountRecord(
            account_key=account_key,
            alias=self._optional_text(switched.get("alias")),
            account_name=self._optional_text(switched.get("account_name")),
            plan=self._optional_text(switched.get("plan")),
            auth_mode=self._optional_text(switched.get("auth_mode")),
            active=True,
        )

    def export_accounts(self, destination: str | Path) -> None:
        """Export snapshots through codex-auth; stdout/stderr are never logic."""
        self._run_command(["export", str(Path(destination).resolve())], require_json=False)

    def import_accounts(self, source: str | Path) -> None:
        """Import snapshots through codex-auth; auth files never enter Git."""
        self._run_command(["import", str(Path(source).resolve())], require_json=False)

    def remove(self, account_key: str) -> None:
        """Remove one account from a temporary capsule only."""
        if not isinstance(account_key, str) or not account_key.strip():
            raise ValueError("account_key must be a non-empty string")
        document = self._run_json(["remove", account_key, "--json"])
        removed = document.get("removed")
        if not isinstance(removed, list):
            raise AccountSchemaError("codex-auth remove result is not an account-object array")
        matched = False
        for entry in removed:
            if not isinstance(entry, dict):
                raise AccountSchemaError("codex-auth remove entry is not an account object")
            removed_key = entry.get("account_key")
            if not self._valid_account_key(removed_key):
                raise AccountSchemaError("codex-auth remove entry has no stable account_key")
            matched = matched or removed_key == account_key
        if not matched:
            raise AccountSchemaError("codex-auth remove did not confirm the requested account")

    def _run_json(self, args: list[str]) -> dict[str, Any]:
        value = self._run_command(args, require_json=True)
        if not isinstance(value, dict):
            raise AccountSchemaError("codex-auth JSON document is not an object")
        schema = value.get("schema_version")
        if schema != SUPPORTED_JSON_SCHEMA:
            raise AccountSchemaError(f"unsupported codex-auth JSON schema: {schema!r}")
        if value.get("error") is not None:
            error = value.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            raise AccountUnavailable(f"codex-auth operation failed: {code or 'unknown_error'}")
        return value

    def _run_command(self, args: list[str], *, require_json: bool) -> dict[str, Any] | None:
        binary = self._resolved_binary()
        if binary is None:
            raise AccountUnavailable("codex-auth binary is unavailable")
        with self._registry_guard(args) as registry_lock:
            try:
                pass_fds = (registry_lock.fileno(),) if registry_lock is not None else ()
                result = subprocess.run(
                    [binary, *args],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=self._environment(),
                    timeout=self.timeout,
                    check=False,
                    pass_fds=pass_fds,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise AccountUnavailable(f"codex-auth command unavailable: {type(exc).__name__}") from exc
        stdout = result.stdout
        if not require_json:
            if result.returncode != 0:
                raise AccountUnavailable("codex-auth command failed")
            return None
        if len(stdout.encode("utf-8", errors="replace")) > MAX_JSON_BYTES:
            raise AccountSchemaError("codex-auth JSON document is too large")
        try:
            value = json.loads(stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            # stderr is intentionally not inspected: it is diagnostics only.
            raise AccountSchemaError("codex-auth stdout is not valid JSON") from exc
        if result.returncode not in (0, 1, 2):
            raise AccountUnavailable("codex-auth returned an unexpected exit status")
        return value

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        return " ".join(value.split())[:160] if isinstance(value, str) and value.strip() else None

    @staticmethod
    def _valid_account_key(value: Any) -> bool:
        return isinstance(value, str) and bool(value.strip()) and all(ord(char) >= 32 for char in value)

    @contextmanager
    def registry_transaction(self, *, operation: str = "registry"):
        """Hold the canonical lock across one logically atomic reconciliation.

        This is deliberately narrower than a provider lease: callers use it
        only for canonical active-account reads/imports/restores. Individual
        adapter commands reuse the held lock through a context-local token.
        """
        if not self.canonical_registry:
            yield None
            return
        current = self._registry_context.get()
        if current is not None:
            yield current
            return
        with self._registry_guard([operation]) as registry_lock:
            token = self._registry_context.set(registry_lock)
            try:
                yield registry_lock
            finally:
                self._registry_context.reset(token)

    def _registry_guard(self, args: list[str]):
        if not self.canonical_registry:
            return nullcontext()
        current = self._registry_context.get()
        if current is not None:
            return nullcontext(current)
        if self.registry_lock is None:
            self.registry_lock = AccountRegistryLockBroker()
        operation = args[0] if args and isinstance(args[0], str) else "registry"
        return self.registry_lock.acquire(operation=operation)


@dataclass(frozen=True)
class PoolDecision:
    candidates: list[AccountCandidate]
    selected: AccountCandidate | None
    earliest_reset: int | None


class AccountRuntime(AbstractContextManager["AccountRuntime"]):
    """One safe switch boundary: lease, capsule activation, then caller work."""

    def __init__(
        self,
        coordinator: "AccountPoolCoordinator",
        account_key: str,
        run_id: str,
        repo: str | Path,
        generation: int,
        codex_home: Path | None = None,
    ):
        self.coordinator = coordinator
        self.account_key = account_key
        self.run_id = run_id
        self.repo = repo
        self.generation = generation
        self.lease: AccountLease | None = None
        self.capsule: Any | None = None
        self.codex_home: Path | None = Path(codex_home).resolve() if codex_home else None

    def __enter__(self) -> "AccountRuntime":
        self.lease = self.coordinator.lease_broker.acquire(self.account_key, self.run_id, self.repo, phase="switch_prepared")
        try:
            target_home = self.codex_home or self.coordinator.run_codex_home
            self.capsule = self.coordinator.capsule_factory(
                self.coordinator.auth,
                self.account_key,
                self.run_id,
                self.generation,
                root=self.coordinator.capsule_root,
                codex_home=target_home,
            )
            opened = self.capsule.__enter__() if hasattr(self.capsule, "__enter__") else self.capsule
            self.capsule = opened
            self.codex_home = Path(opened.codex_home).resolve()
            self.lease.set_phase("switched")
            return self
        except Exception:
            self._close_resources(discard=True)
            raise

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self._close_resources(discard=False)

    def _close_resources(self, *, discard: bool) -> None:
        error: BaseException | None = None
        if self.capsule is not None:
            try:
                if hasattr(self.capsule, "close"):
                    self.capsule.close(discard_unsynced=discard)
                elif hasattr(self.capsule, "__exit__"):
                    self.capsule.__exit__(None, None, None)
            except BaseException as exc:  # release still must not strand a lock
                error = exc
            self.capsule = None
        if self.lease is not None:
            try:
                self.lease.release()
            except BaseException as exc:
                error = error or exc
            self.lease = None
        if error is not None and not discard:
            raise error


class AccountPoolCoordinator:
    """Deep account-pool interface shared by Supervisor and future UI callers."""

    def __init__(
        self,
        auth: CodexAuthAdapter,
        lease_broker: "AccountLeaseBroker",
        *,
        quota_factory: Any | None = None,
        capsule_factory: Any | None = None,
        capsule_root: str | Path | None = None,
        run_codex_home: str | Path | None = None,
    ):
        self.auth = auth
        self.lease_broker = lease_broker
        self.capsule_factory = capsule_factory or AccountCapsule.create
        self.capsule_root = Path(capsule_root).expanduser().resolve() if capsule_root else None
        self.run_codex_home = Path(run_codex_home).expanduser().resolve() if run_codex_home else None
        if quota_factory is None:
            from .quota import AppServerQuotaProvider

            quota_factory = lambda home, fingerprint, lease_fd: AppServerQuotaProvider(
                codex_home=home,
                account_fingerprint=fingerprint,
                lease_fd=lease_fd,
            )
        self.quota_factory = quota_factory

    def session(
        self,
        account_key: str,
        run_id: str,
        repo: str | Path,
        generation: int,
        codex_home: str | Path | None = None,
    ) -> AccountRuntime:
        target_home = Path(codex_home).resolve() if codex_home else self.run_codex_home
        return AccountRuntime(self, account_key, run_id, repo, generation, codex_home=target_home)

    def active_account(self) -> AccountRecord | None:
        method = getattr(self.auth, "active_account", None)
        if not callable(method):
            return None
        return method()

    def probe(self, account_keys: list[str], run_id: str, repo: str | Path, generation: int, excluded: set[str] | None = None) -> PoolDecision:
        records = self.auth.list_accounts()
        by_key = {record.account_key: record for record in records}
        missing = [key for key in account_keys if key not in by_key]
        if missing:
            raise AccountSchemaError("authorized account is no longer present in codex-auth registry")
        candidates: list[AccountCandidate] = []
        excluded = excluded or set()
        for key in account_keys:
            if key in excluded:
                candidates.append(AccountCandidate(key, None, auth_error=True))
                continue
            try:
                with self.session(key, run_id, repo, generation) as runtime:
                    assert runtime.lease is not None and runtime.codex_home is not None
                    quota = self.quota_factory(runtime.codex_home, account_fingerprint(key), runtime.lease.fd).read()
                    runtime.lease.set_phase("quota_verified")
                    candidates.append(AccountCandidate(key, quota))
            except AccountBusy:
                candidates.append(AccountCandidate(key, None, leased=True))
            except AccountUnavailable:
                candidates.append(AccountCandidate(key, None, auth_error=True))
            except AccountBrokerError:
                candidates.append(AccountCandidate(key, None))
            except Exception as exc:
                # A single account's App Server failure must not make another
                # account look usable. The supervisor will fail closed if no
                # authoritative candidate remains.
                candidates.append(AccountCandidate(key, None, auth_error=_looks_like_auth_failure(exc)))
        return PoolDecision(candidates, select_best_account(candidates), earliest_relevant_reset(candidates))


def _window(snapshot: QuotaSnapshot | None, name: str):
    if snapshot is None:
        return None
    return next((item for item in snapshot.windows() if item.name == name), None)


def _remaining(window: Any) -> float | None:
    if window is None or window.used_percent is None:
        return None
    return max(0.0, min(100.0, 100.0 - float(window.used_percent)))


def _authoritative(snapshot: QuotaSnapshot | None) -> bool:
    return snapshot is not None and snapshot.source in {"live_app_server", "fake_file"} and not snapshot.error


def _capacity(candidate: AccountCandidate) -> tuple[float, float, float, int, str] | None:
    if candidate.leased or candidate.auth_error or not _authoritative(candidate.quota):
        return None
    short = _remaining(_window(candidate.quota, "5h"))
    weekly = _remaining(_window(candidate.quota, "weekly"))
    if short is None or weekly is None or short <= 0 or weekly <= 0:
        return None
    resets = [item.resets_at for item in candidate.quota.windows() if item.resets_at is not None]
    next_reset = min(resets) if resets else 2**63 - 1
    return (min(short, weekly), short, weekly, next_reset, candidate.fingerprint)


def select_best_account(candidates: list[AccountCandidate]) -> AccountCandidate | None:
    """Select the greatest usable capacity with deterministic tie breakers."""
    ranked = [(key, candidate) for candidate in candidates if (key := _capacity(candidate)) is not None]
    if not ranked:
        return None
    # Capacity fields descend; a reset is perishable and therefore ascends;
    # the fingerprint is a stable final tie breaker.
    ranked.sort(key=lambda item: (-item[0][0], -item[0][1], -item[0][2], item[0][3], item[0][4]))
    return ranked[0][1]


def earliest_relevant_reset(candidates: list[AccountCandidate]) -> int | None:
    resets: list[int] = []
    for candidate in candidates:
        if not _authoritative(candidate.quota):
            continue
        for window in candidate.quota.exhausted_windows():
            if window.resets_at is not None:
                resets.append(window.resets_at)
    return min(resets) if resets else None


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


class AccountLease(AbstractContextManager["AccountLease"]):
    """A lifetime-held, metadata-audited lease for one OAuth account."""

    def __init__(self, handle: Any, lock_fd: int, path: Path, metadata: dict[str, Any], path_identity: tuple[int, int]):
        self._handle = handle
        self._lock_fd = lock_fd
        self.path = path
        self.metadata = metadata
        self._path_identity = path_identity
        self._released = False

    def set_phase(self, phase: str) -> None:
        if self._released or not isinstance(phase, str) or not phase.strip():
            raise AccountBrokerError("account lease is not active")
        self.metadata["phase"] = phase
        self._write_metadata(self.metadata)

    @property
    def fd(self) -> int:
        if self._released:
            raise AccountBrokerError("account lease is not active")
        return self._lock_fd

    def release(self) -> None:
        if self._released:
            return
        try:
            self._assert_metadata_path()
            current = self._read_metadata(self._handle)
            if current != self.metadata:
                # Never erase a record that no longer proves this owner.
                raise AccountSchemaError("account lease ownership changed unexpectedly")
            self._handle.seek(0)
            self._handle.truncate()
            self._handle.flush()
            os.fsync(self._handle.fileno())
        finally:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            finally:
                try:
                    self._handle.close()
                finally:
                    os.close(self._lock_fd)
                    self._released = True

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.release()

    def _write_metadata(self, value: dict[str, Any]) -> None:
        self._assert_metadata_path()
        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
        self._handle.flush()
        os.fsync(self._handle.fileno())

    def _assert_metadata_path(self) -> None:
        try:
            info = os.lstat(self.path)
        except OSError as exc:
            raise AccountSchemaError("account lease metadata path disappeared") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != self._path_identity:
            raise AccountSchemaError("account lease metadata path was replaced")

    @staticmethod
    def _read_metadata(handle: Any) -> dict[str, Any] | None:
        handle.seek(0)
        text = handle.read()
        if not text.strip():
            return None
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AccountSchemaError("account lease metadata is corrupt") from exc
        if not isinstance(value, dict):
            raise AccountSchemaError("account lease metadata is not an object")
        return value


class AccountLeaseBroker:
    """Filesystem-backed global account coordination without a daemon."""

    def __init__(self, root: str | Path | None = None):
        candidate = Path(root or (control_plane_root() / "account-leases")).expanduser()
        if candidate.exists() or candidate.is_symlink():
            info = os.lstat(candidate)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise AccountSchemaError("account lease root must be a real directory")
        candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = os.lstat(candidate)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise AccountSchemaError("account lease root is unsafe")
        self.root = candidate.resolve()
        self._root_identity = (info.st_dev, info.st_ino)
        self._lock_root = self.root.parent / f".{self.root.name}.account-locks"
        if self._lock_root.exists() or self._lock_root.is_symlink():
            lock_root_info = os.lstat(self._lock_root)
            if stat.S_ISLNK(lock_root_info.st_mode) or not stat.S_ISDIR(lock_root_info.st_mode):
                raise AccountSchemaError("account lease lock root must be a real directory")
        self._lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_root_info = os.lstat(self._lock_root)
        if stat.S_ISLNK(lock_root_info.st_mode) or not stat.S_ISDIR(lock_root_info.st_mode):
            raise AccountSchemaError("account lease lock root is unsafe")
        self._lock_root_identity = (lock_root_info.st_dev, lock_root_info.st_ino)
        root_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        root_descriptor: int | None = None
        lock_root_descriptor: int | None = None
        try:
            root_descriptor = os.open(self.root, root_flags)
            root_info = os.fstat(root_descriptor)
            if not stat.S_ISDIR(root_info.st_mode) or (root_info.st_dev, root_info.st_ino) != self._root_identity:
                raise AccountSchemaError("account lease root is unsafe")
            os.fchmod(root_descriptor, 0o700)
            lock_root_descriptor = os.open(self._lock_root, root_flags)
            lock_root_info = os.fstat(lock_root_descriptor)
            if not stat.S_ISDIR(lock_root_info.st_mode) or (lock_root_info.st_dev, lock_root_info.st_ino) != self._lock_root_identity:
                raise AccountSchemaError("account lease lock root is unsafe")
            os.fchmod(lock_root_descriptor, 0o700)
        except OSError as exc:
            raise AccountSchemaError("account lease root cannot be opened safely") from exc
        finally:
            if root_descriptor is not None:
                os.close(root_descriptor)
            if lock_root_descriptor is not None:
                os.close(lock_root_descriptor)

    def _assert_root(self) -> None:
        try:
            info = os.lstat(self.root)
        except OSError as exc:
            raise AccountSchemaError("account lease root disappeared") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != self._root_identity:
            raise AccountSchemaError("account lease root was replaced")

    def _assert_lock_root(self) -> None:
        try:
            info = os.lstat(self._lock_root)
        except OSError as exc:
            raise AccountSchemaError("account lease lock root disappeared") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != self._lock_root_identity:
            raise AccountSchemaError("account lease lock root was replaced")

    def lease_path(self, account_key: str) -> Path:
        return self.root / f"{account_fingerprint(account_key)}.lock"

    def acquire(
        self,
        account_key: str,
        run_id: str,
        repo: str | Path,
        *,
        supervisor_identity: dict[str, Any] | None = None,
        phase: str = "selected",
    ) -> AccountLease:
        fingerprint = account_fingerprint(account_key)
        path = self.lease_path(account_key)
        self._assert_lock_root()
        self._assert_root()
        lock_root_descriptor: int | None = None
        root_descriptor: int | None = None
        lock_fd: int | None = None
        handle: Any | None = None
        try:
            root_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            lock_root_descriptor = os.open(self._lock_root, root_flags)
            lock_root_info = os.fstat(lock_root_descriptor)
            if not stat.S_ISDIR(lock_root_info.st_mode) or (lock_root_info.st_dev, lock_root_info.st_ino) != self._lock_root_identity:
                raise AccountSchemaError("account lease lock root is unsafe")
            self._assert_lock_root()
            lock_dir_name = fingerprint
            try:
                os.mkdir(lock_dir_name, mode=0o700, dir_fd=lock_root_descriptor)
            except FileExistsError:
                pass
            lock_dir_info = os.stat(lock_dir_name, dir_fd=lock_root_descriptor, follow_symlinks=False)
            if stat.S_ISLNK(lock_dir_info.st_mode) or not stat.S_ISDIR(lock_dir_info.st_mode):
                raise AccountSchemaError("account lease lock directory is unsafe")
            lock_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            lock_fd = os.open(lock_dir_name, lock_flags, dir_fd=lock_root_descriptor)
            opened_lock_dir = os.fstat(lock_fd)
            if not stat.S_ISDIR(opened_lock_dir.st_mode) or (opened_lock_dir.st_dev, opened_lock_dir.st_ino) != (lock_dir_info.st_dev, lock_dir_info.st_ino):
                raise AccountSchemaError("account lease lock directory was replaced")
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise AccountBusy(f"account {fingerprint} is busy") from exc
            self._assert_lock_root()
            self._assert_root()
            root_descriptor = os.open(self.root, root_flags)
            root_info = os.fstat(root_descriptor)
            if not stat.S_ISDIR(root_info.st_mode) or (root_info.st_dev, root_info.st_ino) != self._root_identity:
                raise AccountSchemaError("account lease root is unsafe")
            self._assert_root()
            lease_name = path.name
            try:
                existing = os.stat(lease_name, dir_fd=root_descriptor, follow_symlinks=False)
                if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
                    raise AccountSchemaError("account lease path is not a regular file")
            except FileNotFoundError:
                pass
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(lease_name, flags, 0o600, dir_fd=root_descriptor)
                opened = os.fstat(descriptor)
                named = os.stat(lease_name, dir_fd=root_descriptor, follow_symlinks=False)
                if stat.S_ISLNK(opened.st_mode) or not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
                    os.close(descriptor)
                    raise AccountSchemaError("account lease path is unsafe")
                handle = os.fdopen(descriptor, "r+", encoding="utf-8")
                os.fchmod(handle.fileno(), 0o600)
            except OSError as exc:
                raise AccountUnavailable("account lease path cannot be opened safely") from exc
            previous = AccountLease._read_metadata(handle)
            if previous is not None:
                required = {"schema_version", "account_fingerprint", "lock_root", "run_id", "repo", "pid", "starttime", "executable", "phase"}
                lock_root = previous.get("lock_root")
                if (
                    not required.issubset(previous)
                    or previous.get("schema_version") != 1
                    or previous.get("account_fingerprint") != fingerprint
                    or not isinstance(lock_root, dict)
                    or type(lock_root.get("device")) is not int
                    or type(lock_root.get("inode")) is not int
                ):
                    raise AccountSchemaError("account lease metadata is invalid")
                if (lock_root["device"], lock_root["inode"]) != self._lock_root_identity:
                    # A stale-looking owner may still have a provider child
                    # holding the old directory lock FD. Never migrate live
                    # metadata across lock-domain replacement automatically.
                    raise AccountSchemaError("account lease lock domain changed unexpectedly")
                if _identity_matches(previous):
                    raise AccountBusy("account lease metadata identifies a live owner")
            identity = supervisor_identity or _linux_process_identity(os.getpid())
            if identity is None:
                raise AccountUnavailable("Linux PID identity is unavailable for account lease")
            metadata = {
                "schema_version": 1,
                "account_fingerprint": fingerprint,
                "lock_root": {
                    "device": self._lock_root_identity[0],
                    "inode": self._lock_root_identity[1],
                },
                "run_id": str(run_id),
                "repo": str(Path(repo).resolve()),
                "pid": identity["pid"],
                "starttime": identity["starttime"],
                "executable": identity["executable"],
                "acquired_at": now_iso(),
                "phase": phase,
            }
            lease = AccountLease(handle, lock_fd, path, metadata, (opened.st_dev, opened.st_ino))
            lease._write_metadata(metadata)
            os.close(lock_root_descriptor)
            lock_root_descriptor = None
            root_descriptor = None
            lock_fd = None
            handle = None
            return lease
        except Exception:
            try:
                if lock_fd is not None:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                if handle is not None:
                    handle.close()
                if lock_fd is not None:
                    os.close(lock_fd)
                if root_descriptor is not None:
                    os.close(root_descriptor)
                if lock_root_descriptor is not None:
                    os.close(lock_root_descriptor)
            raise


class AccountCapsule(AbstractContextManager["AccountCapsule"]):
    """Persistent per-run CODEX_HOME with ephemeral leased authentication.

    The local Codex Thread Store (sessions/rollouts, local database) persists
    across provider turns, supervisor restarts, and account handoffs within
    the run. Credential-bearing authentication material (auth.json, accounts/)
    is injected only while the account lease is held and is scrubbed immediately
    upon session exit or failure.
    """

    def __init__(
        self,
        adapter: CodexAuthAdapter,
        account_key: str,
        root: Path,
        allowed_root: Path | None = None,
        codex_home: Path | None = None,
    ):
        self.adapter = adapter
        self.account_key = account_key
        self.root = root.resolve()
        self.allowed_root = (allowed_root or (control_plane_root() / "account-capsules")).resolve()
        self.codex_home = (codex_home or (self.root / "codex-home")).resolve()
        self.manifest_path = self.root / "manifest.json"
        self.export_root = self.root / "canonical-export"
        self.sync_root = self.root / "synchronized-export"
        self.capsule_adapter = CodexAuthAdapter(adapter.binary, self.codex_home, adapter.timeout, canonical_registry=False)
        self._closed = False
        self._validate_paths()

    def _validate_paths(self) -> None:
        if self.root.is_symlink():
            raise AccountSchemaError("account capsule root must not be a symlink")
        if self.codex_home.is_symlink():
            raise AccountSchemaError("account capsule codex-home must not be a symlink")

    @staticmethod
    def _matches_account_snapshot(filename: str, account_key: str, data: dict[str, Any]) -> bool:
        if data.get("account_key") == account_key:
            return True
        stem = filename.removesuffix(".auth.json")
        if stem == account_key or stem == account_key.replace("::", "--"):
            return True
        for padding in (b"", b"=", b"=="):
            try:
                if base64.b64decode(stem.encode("ascii") + padding).decode("utf-8") == account_key:
                    return True
            except Exception:
                pass
            try:
                if base64.urlsafe_b64decode(stem.encode("ascii") + padding).decode("utf-8") == account_key:
                    return True
            except Exception:
                pass
        return False

    @classmethod
    def create(
        cls,
        adapter: CodexAuthAdapter,
        account_key: str,
        run_id: str,
        generation: int,
        root: str | Path | None = None,
        codex_home: str | Path | None = None,
    ) -> "AccountCapsule":
        if codex_home is not None:
            target_codex_home = Path(codex_home).expanduser().resolve()
            capsule_root = target_codex_home.parent.resolve()
            allowed_base = capsule_root.parent.resolve()
        else:
            base = Path(root or (control_plane_root() / "account-capsules")).expanduser()
            if base.exists() or base.is_symlink():
                base_info = os.lstat(base)
                if stat.S_ISLNK(base_info.st_mode) or not stat.S_ISDIR(base_info.st_mode):
                    raise AccountSchemaError("account capsule root must be a real directory")
            base.mkdir(parents=True, exist_ok=True, mode=0o700)
            base_info = os.lstat(base)
            if stat.S_ISLNK(base_info.st_mode) or not stat.S_ISDIR(base_info.st_mode):
                raise AccountSchemaError("account capsule root is unsafe")
            allowed_base = base.resolve()
            os.chmod(allowed_base, 0o700)
            capsule_root = (allowed_base / str(run_id).replace('/', '_')).resolve()
            target_codex_home = (capsule_root / "codex-home").resolve()

        if capsule_root.exists() or capsule_root.is_symlink():
            info = os.lstat(capsule_root)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise AccountSchemaError("account capsule root must be a real directory")
        capsule_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(capsule_root, 0o700)

        if target_codex_home.exists() or target_codex_home.is_symlink():
            info = os.lstat(target_codex_home)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise AccountSchemaError("account capsule codex-home must be a real directory")
        target_codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(target_codex_home, 0o700)

        manifest_path = capsule_root / "manifest.json"
        auth_file = target_codex_home / "auth.json"
        accounts_dir = target_codex_home / "accounts"
        if manifest_path.exists() or auth_file.exists() or accounts_dir.exists():
            cls._recover_existing(adapter, account_key, capsule_root, allowed_base, run_id, generation, codex_home=target_codex_home)

        capsule = cls(adapter, account_key, capsule_root, allowed_base, codex_home=target_codex_home)
        try:
            capsule._write_manifest(run_id, generation)
            capsule.export_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(capsule.export_root, 0o700)
            capsule.adapter.export_accounts(capsule.export_root)
            capsule._harden_tree(capsule.export_root)
            crash_hook("AFTER_CANONICAL_EXPORT")

            # Prune all other accounts from export staging so only account_key is imported
            for file_path in list(capsule.export_root.iterdir()):
                if file_path.is_file() and file_path.name.endswith(".auth.json"):
                    try:
                        record_data = json.loads(file_path.read_text(encoding="utf-8"))
                    except Exception:
                        record_data = {}
                    if not cls._matches_account_snapshot(file_path.name, account_key, record_data):
                        file_path.unlink()

            capsule.capsule_adapter.import_accounts(capsule.export_root)
            capsule._clean_runtime_tmp()
            capsule._harden_tree(capsule.codex_home)
            crash_hook("AFTER_CAPSULE_IMPORT")

            for record in capsule.capsule_adapter.list_accounts():
                if record.account_key != account_key:
                    capsule.capsule_adapter.remove(record.account_key)
            selected = capsule.capsule_adapter.switch(account_key)
            records = capsule.capsule_adapter.list_accounts()
            if len(records) != 1 or records[0].account_key != account_key or not selected.active or not records[0].active:
                raise AccountSchemaError("account capsule did not reconcile to the selected active account")

            capsule._remove_staging(capsule.export_root)
            crash_hook("AFTER_CAPSULE_PRUNE")
            return capsule
        except Exception:
            capsule.close(discard_unsynced=True)
            raise

    @classmethod
    def _recover_existing(
        cls,
        adapter: CodexAuthAdapter,
        account_key: str,
        capsule_root: Path,
        allowed_root: Path,
        run_id: str,
        generation: int,
        codex_home: Path | None = None,
    ) -> None:
        info = os.lstat(capsule_root)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise AccountSchemaError("existing account capsule is not a real directory")
        manifest_path = capsule_root / "manifest.json"
        if manifest_path.exists():
            manifest_info = os.lstat(manifest_path)
            if stat.S_ISLNK(manifest_info.st_mode) or not stat.S_ISREG(manifest_info.st_mode):
                raise AccountSchemaError("existing account capsule manifest is unsafe")
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise AccountSchemaError("existing account capsule manifest is corrupt") from exc
            if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
                raise AccountSchemaError("existing account capsule manifest is invalid")
            owner = manifest.get("owner")
            if not isinstance(owner, dict) or not isinstance(owner.get("pid"), int) or not isinstance(owner.get("starttime"), str) or not isinstance(owner.get("executable"), str):
                raise AccountSchemaError("existing account capsule owner identity is invalid")
            if _identity_matches(owner) and owner.get("pid") != os.getpid():
                raise AccountBusy("existing account capsule still has a live owner")

        capsule = cls(adapter, account_key, capsule_root, allowed_root, codex_home=codex_home)
        capsule._clean_runtime_tmp()
        if capsule.codex_home.exists():
            capsule._harden_tree(capsule.codex_home)
        try:
            capsule.synchronize()
        except Exception:
            pass
        capsule.scrub_credentials()
        capsule._closed = True

    def _clean_runtime_tmp(self) -> None:
        """Clean ephemeral runtime tmp created by official Codex processes."""
        runtime_tmp = self.codex_home / "tmp"
        try:
            info = os.lstat(runtime_tmp)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode):
            raise AccountSchemaError("account capsule runtime tmp is a symlink")
        if stat.S_ISDIR(info.st_mode):
            shutil.rmtree(runtime_tmp)

    def _write_manifest(self, run_id: str, generation: int) -> None:
        identity = _linux_process_identity(os.getpid())
        if identity is None:
            raise AccountUnavailable("Linux PID identity is unavailable for account capsule")
        value = {
            "schema_version": 1,
            "account_fingerprint": account_fingerprint(self.account_key),
            "account_key": self.account_key,
            "run_id": str(run_id),
            "generation": generation,
            "owner": identity,
            "created_at": now_iso(),
        }
        self.manifest_path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(self.manifest_path, 0o600)

    def synchronize(self) -> None:
        if self._closed:
            raise AccountBrokerError("account capsule is closed")
        auth_file = self.codex_home / "auth.json"
        accounts_dir = self.codex_home / "accounts"
        registry_file = self.codex_home / "registry.json"
        if not auth_file.exists() and not accounts_dir.exists() and not registry_file.exists() and not any(self.codex_home.glob("*.auth.json")):
            return
        canonical_active = self.adapter.active_account()
        self.capsule_adapter.switch(self.account_key)
        self._clean_runtime_tmp()
        self._harden_tree(self.codex_home)
        self.sync_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.sync_root, 0o700)
        crash_hook("BEFORE_CAPSULE_EXPORT")
        self.capsule_adapter.export_accounts(self.sync_root)
        self._harden_tree(self.sync_root)
        crash_hook("AFTER_CAPSULE_EXPORT")
        exported = [path for path in self.sync_root.iterdir() if path.is_file() and path.name.endswith(".auth.json")]
        if len(exported) != 1:
            raise AccountSchemaError("account capsule synchronization did not produce exactly one auth snapshot")
        with self.adapter.registry_transaction(operation="capsule-sync"):
            canonical_active = self.adapter.active_account()
            crash_hook("BEFORE_CANONICAL_IMPORT")
            self.adapter.import_accounts(self.sync_root)
            crash_hook("AFTER_CANONICAL_IMPORT")
            if canonical_active is not None and canonical_active.account_key != self.account_key:
                self.adapter.switch(canonical_active.account_key)
        self._remove_staging(self.sync_root)

    def scrub_credentials(self) -> None:
        """Scrub credential-bearing files while preserving durable Thread/session store."""
        auth_file = self.codex_home / "auth.json"
        if auth_file.exists() or auth_file.is_symlink():
            auth_file.unlink(missing_ok=True)
        registry_file = self.codex_home / "registry.json"
        if registry_file.exists() or registry_file.is_symlink():
            registry_file.unlink(missing_ok=True)
        accounts_dir = self.codex_home / "accounts"
        if accounts_dir.exists() or accounts_dir.is_symlink():
            if accounts_dir.is_symlink():
                raise AccountSchemaError("account capsule accounts directory is a symlink")
            shutil.rmtree(accounts_dir)
        for auth_path in list(self.codex_home.glob("*.auth.json")):
            auth_path.unlink(missing_ok=True)
        if self.manifest_path.exists() or self.manifest_path.is_symlink():
            self.manifest_path.unlink(missing_ok=True)
        self._remove_staging(self.export_root)
        self._remove_staging(self.sync_root)
        self._clean_runtime_tmp()
        if self.codex_home.exists():
            self._harden_tree(self.codex_home)

    @staticmethod
    def _harden_tree(root: Path) -> None:
        for path in (root, *root.rglob("*")):
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                raise AccountSchemaError("account capsule contains a symlink")
            if stat.S_ISDIR(info.st_mode):
                os.chmod(path, 0o700)
            elif stat.S_ISREG(info.st_mode):
                os.chmod(path, 0o600)
            else:
                raise AccountSchemaError("account capsule contains an unsupported file")

    @staticmethod
    def _remove_staging(root: Path) -> None:
        """Remove one validated temporary export without following links."""
        try:
            info = os.lstat(root)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise AccountSchemaError("account capsule staging path is unsafe")
        AccountCapsule._harden_tree(root)
        shutil.rmtree(root)

    def close(self, *, discard_unsynced: bool = False) -> None:
        if self._closed:
            return
        try:
            if not discard_unsynced:
                self.synchronize()
        finally:
            self.scrub_credentials()
            has_thread_store = (self.codex_home / "sessions").exists() or any((self.codex_home / "sessions").rglob("*.jsonl"))
            if discard_unsynced and not has_thread_store:
                self._remove_capsule_root()
            self._closed = True

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()

    def _remove_capsule_root(self) -> None:
        parent = self.root.parent.resolve()
        allowed = self.allowed_root
        if parent != allowed and self.root != allowed and not self.root.is_relative_to(allowed):
            raise AccountSchemaError("refusing to remove an unsafe account capsule path")
        self._clean_runtime_tmp()
        self._harden_tree(self.root)
        shutil.rmtree(self.root)
