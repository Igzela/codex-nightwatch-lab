"""Optional codex-auth integration and the global OAuth-account lease seam.

The adapter deliberately consumes only codex-auth's versioned JSON contract.
Quota values in that contract are display metadata; selection accepts only a
fresh snapshot supplied by Nightwatch's App Server quota authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fcntl

from .models import QuotaSnapshot
from .storage import control_plane_root, now_iso


SUPPORTED_JSON_SCHEMA = 1
MAX_JSON_BYTES = 1_000_000


class AccountBrokerError(RuntimeError):
    """Base error for account discovery, activation, and lease operations."""


class AccountUnavailable(AccountBrokerError):
    """The optional codex-auth capability is missing or could not be used."""


class AccountSchemaError(AccountBrokerError):
    """A machine-readable document or lease record cannot be trusted."""


class AccountBusy(AccountBrokerError):
    """Another Nightwatch run currently owns the account lease."""


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
    ) -> None:
        self.binary = binary or os.environ.get("NIGHTWATCH_CODEX_AUTH_BIN", "codex-auth")
        self.codex_home = Path(codex_home or os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()
        self.timeout = timeout

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
            if not isinstance(key, str) or not key.strip():
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
        if not isinstance(removed, list) or account_key not in removed:
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
        try:
            result = subprocess.run(
                [binary, *args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self._environment(),
                timeout=self.timeout,
                check=False,
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


@dataclass(frozen=True)
class PoolDecision:
    candidates: list[AccountCandidate]
    selected: AccountCandidate | None
    earliest_reset: int | None


class AccountRuntime(AbstractContextManager["AccountRuntime"]):
    """One safe switch boundary: lease, capsule activation, then caller work."""

    def __init__(self, coordinator: "AccountPoolCoordinator", account_key: str, run_id: str, repo: str | Path, generation: int):
        self.coordinator = coordinator
        self.account_key = account_key
        self.run_id = run_id
        self.repo = repo
        self.generation = generation
        self.lease: AccountLease | None = None
        self.capsule: Any | None = None
        self.codex_home: Path | None = None

    def __enter__(self) -> "AccountRuntime":
        self.lease = self.coordinator.lease_broker.acquire(self.account_key, self.run_id, self.repo, phase="switch_prepared")
        try:
            self.capsule = self.coordinator.capsule_factory(
                self.coordinator.auth,
                self.account_key,
                self.run_id,
                self.generation,
                root=self.coordinator.capsule_root,
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
    ):
        self.auth = auth
        self.lease_broker = lease_broker
        self.capsule_factory = capsule_factory or AccountCapsule.create
        self.capsule_root = Path(capsule_root).expanduser().resolve() if capsule_root else None
        if quota_factory is None:
            from .quota import AppServerQuotaProvider

            quota_factory = lambda home, fingerprint, lease_fd: AppServerQuotaProvider(
                codex_home=home,
                account_fingerprint=fingerprint,
                lease_fd=lease_fd,
            )
        self.quota_factory = quota_factory

    def session(self, account_key: str, run_id: str, repo: str | Path, generation: int) -> AccountRuntime:
        return AccountRuntime(self, account_key, run_id, repo, generation)

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

    def __init__(self, handle: Any, path: Path, metadata: dict[str, Any]):
        self._handle = handle
        self.path = path
        self.metadata = metadata
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
        return self._handle.fileno()

    def release(self) -> None:
        if self._released:
            return
        try:
            current = self._read_metadata(self._handle)
            if current != self.metadata:
                # Never erase a record that no longer proves this owner.
                raise AccountSchemaError("account lease ownership changed unexpectedly")
            self._handle.seek(0)
            self._handle.truncate()
            self._handle.flush()
            os.fsync(self._handle.fileno())
        finally:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._released = True

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.release()

    def _write_metadata(self, value: dict[str, Any]) -> None:
        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
        self._handle.flush()
        os.fsync(self._handle.fileno())

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
        os.chmod(self.root, 0o700)

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
        path = self.lease_path(account_key)
        try:
            existing = os.lstat(path)
            if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
                raise AccountSchemaError("account lease path is not a regular file")
        except FileNotFoundError:
            pass
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
            opened = os.fstat(descriptor)
            if stat.S_ISLNK(opened.st_mode) or not stat.S_ISREG(opened.st_mode):
                os.close(descriptor)
                raise AccountSchemaError("account lease path is unsafe")
            handle = os.fdopen(descriptor, "r+", encoding="utf-8")
        except OSError as exc:
            raise AccountUnavailable("account lease path cannot be opened safely") from exc
        try:
            os.chmod(path, 0o600)
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise AccountBusy(f"account {account_fingerprint(account_key)} is busy") from exc
            previous = AccountLease._read_metadata(handle)
            if previous is not None:
                required = {"schema_version", "account_fingerprint", "run_id", "repo", "pid", "starttime", "executable", "phase"}
                if set(previous) < required or previous.get("schema_version") != 1 or previous.get("account_fingerprint") != account_fingerprint(account_key):
                    raise AccountSchemaError("account lease metadata is invalid")
                if _identity_matches(previous):
                    raise AccountBusy("account lease metadata identifies a live owner")
            identity = supervisor_identity or _linux_process_identity(os.getpid())
            if identity is None:
                raise AccountUnavailable("Linux PID identity is unavailable for account lease")
            metadata = {
                "schema_version": 1,
                "account_fingerprint": account_fingerprint(account_key),
                "run_id": str(run_id),
                "repo": str(Path(repo).resolve()),
                "pid": identity["pid"],
                "starttime": identity["starttime"],
                "executable": identity["executable"],
                "acquired_at": now_iso(),
                "phase": phase,
            }
            lease = AccountLease(handle, path, metadata)
            lease._write_metadata(metadata)
            return lease
        except Exception:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
            raise


class AccountCapsule(AbstractContextManager["AccountCapsule"]):
    """Ephemeral per-account CODEX_HOME synchronized through codex-auth.

    The canonical CODEX_HOME is never switched. A capsule contains exactly one
    selected account after import/remove reconciliation and is deleted only
    after its managed snapshot has been imported back to the canonical source.
    """

    def __init__(self, adapter: CodexAuthAdapter, account_key: str, root: Path, allowed_root: Path | None = None):
        self.adapter = adapter
        self.account_key = account_key
        self.root = root.resolve()
        self.allowed_root = (allowed_root or (control_plane_root() / "account-capsules")).resolve()
        self.codex_home = self.root / "codex-home"
        self.export_root = self.root / "canonical-export"
        self.sync_root = self.root / "synchronized-export"
        self.capsule_adapter = CodexAuthAdapter(adapter.binary, self.codex_home, adapter.timeout)
        self._closed = False

    @classmethod
    def create(
        cls,
        adapter: CodexAuthAdapter,
        account_key: str,
        run_id: str,
        generation: int,
        root: str | Path | None = None,
    ) -> "AccountCapsule":
        base = Path(root or (control_plane_root() / "account-capsules")).expanduser()
        if base.exists() or base.is_symlink():
            base_info = os.lstat(base)
            if stat.S_ISLNK(base_info.st_mode) or not stat.S_ISDIR(base_info.st_mode):
                raise AccountSchemaError("account capsule root must be a real directory")
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        base_info = os.lstat(base)
        if stat.S_ISLNK(base_info.st_mode) or not stat.S_ISDIR(base_info.st_mode):
            raise AccountSchemaError("account capsule root is unsafe")
        base = base.resolve()
        os.chmod(base, 0o700)
        capsule_root = base / f"{str(run_id).replace('/', '_')}-{generation}-{account_fingerprint(account_key)}"
        if capsule_root.exists() or capsule_root.is_symlink():
            cls._recover_existing(adapter, account_key, capsule_root, base, run_id, generation)
        capsule_root.mkdir(mode=0o700)
        os.chmod(capsule_root, 0o700)
        capsule = cls(adapter, account_key, capsule_root, base)
        try:
            capsule._write_manifest(run_id, generation)
            capsule.export_root.mkdir(mode=0o700)
            capsule.codex_home.mkdir(mode=0o700)
            capsule.adapter.export_accounts(capsule.export_root)
            capsule._harden_tree(capsule.export_root)
            capsule.capsule_adapter.import_accounts(capsule.export_root)
            capsule._harden_tree(capsule.codex_home)
            # Keep the capsule single-account. This avoids stale mutable
            # snapshots for other accounts surviving beyond this lease.
            for record in capsule.capsule_adapter.list_accounts():
                if record.account_key != account_key:
                    capsule.capsule_adapter.remove(record.account_key)
            capsule.capsule_adapter.switch(account_key)
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
    ) -> None:
        """Synchronize one exact stale capsule before reusing its name.

        A supervisor crash can leave a capsule directory after the kernel has
        released its account lease. The manifest makes that recovery bounded
        and attributable; an absent/corrupt/live owner is never guessed away.
        """
        info = os.lstat(capsule_root)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise AccountSchemaError("existing account capsule is not a real directory")
        manifest_path = capsule_root / "manifest.json"
        try:
            manifest_info = os.lstat(manifest_path)
        except OSError as exc:
            raise AccountSchemaError("existing account capsule manifest is missing") from exc
        if stat.S_ISLNK(manifest_info.st_mode) or not stat.S_ISREG(manifest_info.st_mode):
            raise AccountSchemaError("existing account capsule manifest is unsafe")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AccountSchemaError("existing account capsule manifest is corrupt") from exc
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or manifest.get("account_fingerprint") != account_fingerprint(account_key) or manifest.get("run_id") != str(run_id) or manifest.get("generation") != generation:
            raise AccountSchemaError("existing account capsule manifest does not match the requested recovery")
        owner = manifest.get("owner")
        if not isinstance(owner, dict) or not isinstance(owner.get("pid"), int) or not isinstance(owner.get("starttime"), str) or not isinstance(owner.get("executable"), str):
            raise AccountSchemaError("existing account capsule owner identity is invalid")
        if _identity_matches(owner):
            raise AccountBusy("existing account capsule still has a live owner")
        capsule = cls(adapter, account_key, capsule_root, allowed_root)
        capsule._harden_tree(capsule.codex_home)
        capsule.synchronize()
        capsule._remove_capsule_root()
        capsule._closed = True

    def _write_manifest(self, run_id: str, generation: int) -> None:
        identity = _linux_process_identity(os.getpid())
        if identity is None:
            raise AccountUnavailable("Linux PID identity is unavailable for account capsule")
        value = {
            "schema_version": 1,
            "account_fingerprint": account_fingerprint(self.account_key),
            "run_id": str(run_id),
            "generation": generation,
            "owner": identity,
            "created_at": now_iso(),
        }
        self.root.joinpath("manifest.json").write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(self.root / "manifest.json", 0o600)

    def synchronize(self) -> None:
        if self._closed:
            raise AccountBrokerError("account capsule is closed")
        # Query-switch first gives codex-auth its documented opportunity to
        # synchronize a refreshed active auth.json into the managed snapshot.
        self.capsule_adapter.switch(self.account_key)
        self._harden_tree(self.codex_home)
        self.sync_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.sync_root, 0o700)
        self.capsule_adapter.export_accounts(self.sync_root)
        self._harden_tree(self.sync_root)
        exported = [path for path in self.sync_root.iterdir() if path.is_file() and path.name.endswith(".auth.json")]
        if len(exported) != 1:
            raise AccountSchemaError("account capsule synchronization did not produce exactly one auth snapshot")
        # Import is the documented registry synchronization path; no auth file
        # contents are parsed or copied by Nightwatch itself.
        self.adapter.import_accounts(self.sync_root)

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

    def close(self, *, discard_unsynced: bool = False) -> None:
        if self._closed:
            return
        if not discard_unsynced:
            self.synchronize()
        self._remove_capsule_root()
        self._closed = True

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()

    def _remove_capsule_root(self) -> None:
        parent = self.root.parent.resolve()
        allowed = self.allowed_root
        if parent != allowed or self.root == allowed or self.root.is_symlink():
            raise AccountSchemaError("refusing to remove an unsafe account capsule path")
        shutil.rmtree(self.root)
