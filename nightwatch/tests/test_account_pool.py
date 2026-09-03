from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

PRODUCT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PRODUCT))

from nightwatch.account_broker import (  # noqa: E402
    AccountBusy,
    AccountCandidate,
    AccountCapsule,
    AccountLeaseBroker,
    AccountPoolCoordinator,
    AccountRecord,
    AccountRegistryLockBroker,
    AccountSchemaError,
    AccountUnavailable,
    CodexAuthAdapter,
    account_fingerprint,
    earliest_relevant_reset,
    select_best_account,
)
from nightwatch import cli  # noqa: E402
from nightwatch.models import ErrorKind, ProviderResult, QuotaSnapshot, QuotaWindow, State  # noqa: E402
from nightwatch.quota import AppServerQuotaProvider  # noqa: E402
from nightwatch.storage import NightwatchStore  # noqa: E402
from nightwatch.supervisor import Supervisor  # noqa: E402


def snapshot(short: float | None, weekly: float | None, *, source: str = "live_app_server", now: int | None = None) -> QuotaSnapshot:
    now = now or int(time.time())
    return QuotaSnapshot(
        source,
        "now",
        QuotaWindow("5h", short, 300, now + 300),
        QuotaWindow("weekly", weekly, 10080, now + 1000),
    )


def auth_payload(*, schema_version: int = 1, stderr: str | None = None) -> dict:
    return {
        "schema_version": schema_version,
        "command": "list",
        "active_account_key": "user::a",
        "accounts": [
            {
                "number": 1,
                "account_key": "user::a",
                "alias": "personal",
                "account_name": "Personal",
                "email": "private@example.invalid",
                "plan": "plus",
                "auth_mode": "chatgpt",
                "active": True,
                "usage": {"primary": {"used_percent": 99}, "access_token": "must-not-be-retained"},
                "unknown_future_field": {"ignored": True},
            },
            {"number": 2, "account_key": "user::b", "alias": "backup", "active": False},
        ],
    }


class AuthBinary:
    def __init__(self, payload: dict, *, sleep: float = 0, stderr: str = "", malformed: bool = False, argv_path: Path | None = None, remove_result: object | None = None):
        self.temporary = tempfile.TemporaryDirectory(prefix="nightwatch-auth-fake-")
        self.path = Path(self.temporary.name) / "codex-auth"
        argv_capture = f"open({str(argv_path)!r}, 'w').write(json.dumps(sys.argv[1:]))\n" if argv_path else ""
        explicit_remove = remove_result is not None
        code = (
            "#!/usr/bin/env python3\n"
            "import json, sys, time\n"
            f"time.sleep({sleep!r})\n"
            f"print({json.dumps(stderr)!r}, file=sys.stderr, end='')\n"
            f"{argv_capture}"
            f"if {malformed!r}: print('not-json'); sys.exit(0)\n"
            f"payload = {payload!r}\n"
            f"remove_result = {remove_result!r}\n"
            "if sys.argv[1:2] == ['remove']:\n"
            f"    removed_payload = remove_result if {explicit_remove!r} else [{{'account_key': sys.argv[2]}}]\n"
            "    payload = {'schema_version': 1, 'command': 'remove', 'removed': removed_payload}\n"
            "elif sys.argv[1:2] == ['switch']:\n"
            "    payload = {'schema_version': 1, 'command': 'switch', 'switched_to': {'account_key': sys.argv[2]}}\n"
            "print(json.dumps(payload))\n"
        )
        self.path.write_text(code, encoding="utf-8")
        self.path.chmod(0o700)

    def close(self) -> None:
        self.temporary.cleanup()


class AccountBrokerContractTests(unittest.TestCase):
    def test_cli_can_authorize_an_explicit_auto_pool_without_enrolling_all_accounts(self):
        args = cli._parser().parse_args([
            "run", "goal", "--account-mode", "auto-pool", "--account", "personal", "--account", "backup",
        ])
        self.assertEqual(args.account_mode, "auto-pool")
        self.assertEqual(args.accounts, ["personal", "backup"])

    def test_schema_v1_accepts_unknown_optional_fields_and_ignores_row_numbers(self):
        fake = AuthBinary(auth_payload(), stderr="warning mentions a token but is not logic")
        try:
            adapter = CodexAuthAdapter(binary=str(fake.path), codex_home=Path(tempfile.mkdtemp()))
            accounts = adapter.list_accounts()
            self.assertEqual([account.account_key for account in accounts], ["user::a", "user::b"])
            self.assertEqual(accounts[0].alias, "personal")
            self.assertEqual(accounts[0].fingerprint, account_fingerprint("user::a"))
            self.assertNotIn("must-not-be-retained", repr(accounts[0]))
        finally:
            fake.close()

    def test_future_schema_is_rejected(self):
        fake = AuthBinary(auth_payload(schema_version=2))
        try:
            with self.assertRaises(AccountSchemaError):
                CodexAuthAdapter(binary=str(fake.path)).list_accounts()
        finally:
            fake.close()

    def test_malformed_json_is_rejected_and_stderr_does_not_drive_logic(self):
        fake = AuthBinary(auth_payload(), stderr="authentication failed; token expired")
        try:
            adapter = CodexAuthAdapter(binary=str(fake.path))
            self.assertEqual(len(adapter.list_accounts()), 2)
        finally:
            fake.close()
        fake = AuthBinary(auth_payload(), malformed=True)
        try:
            with self.assertRaises(AccountSchemaError):
                CodexAuthAdapter(binary=str(fake.path)).list_accounts()
        finally:
            fake.close()

    def test_timeout_fails_safely(self):
        fake = AuthBinary(auth_payload(), sleep=0.2)
        try:
            with self.assertRaises(AccountUnavailable):
                CodexAuthAdapter(binary=str(fake.path), timeout=0.01).list_accounts()
        finally:
            fake.close()

    def test_missing_binary_disables_optional_pool(self):
        adapter = CodexAuthAdapter(binary="/definitely/missing/codex-auth")
        self.assertFalse(adapter.available())
        with self.assertRaises(AccountUnavailable):
            adapter.list_accounts()

    def test_switch_uses_stable_account_key(self):
        fake = AuthBinary(auth_payload())
        try:
            adapter = CodexAuthAdapter(binary=str(fake.path))
            switched = adapter.switch("user::b")
            self.assertEqual(switched.account_key, "user::b")
        finally:
            fake.close()

    def test_remove_accepts_real_schema_v1_account_array(self):
        fake = AuthBinary(auth_payload(), remove_result=[{"account_key": "user::a", "unknown_optional": {"ignored": True}}])
        try:
            CodexAuthAdapter(binary=str(fake.path)).remove("user::a")
        finally:
            fake.close()

    def test_remove_requires_requested_account_key(self):
        fake = AuthBinary(auth_payload(), remove_result=[{"account_key": "user::b"}])
        try:
            with self.assertRaises(AccountSchemaError):
                CodexAuthAdapter(binary=str(fake.path)).remove("user::a")
        finally:
            fake.close()

    def test_remove_rejects_string_array_legacy_fake_shape(self):
        fake = AuthBinary(auth_payload(), remove_result=["user::a"])
        try:
            with self.assertRaises(AccountSchemaError):
                CodexAuthAdapter(binary=str(fake.path)).remove("user::a")
        finally:
            fake.close()

    def test_remove_rejects_malformed_account_entries(self):
        fake = AuthBinary(auth_payload(), remove_result=[{"alias": "missing stable key"}])
        try:
            with self.assertRaises(AccountSchemaError):
                CodexAuthAdapter(binary=str(fake.path)).remove("user::a")
        finally:
            fake.close()

    def test_remove_ignores_unknown_optional_fields(self):
        fake = AuthBinary(auth_payload(), remove_result=[{"account_key": "user::a", "usage": {"access_token": "never retained"}, "future": [1, 2, 3]}])
        try:
            CodexAuthAdapter(binary=str(fake.path)).remove("user::a")
        finally:
            fake.close()

    def test_discovery_uses_local_skip_api_json_contract(self):
        with tempfile.TemporaryDirectory(prefix="nightwatch-auth-argv-") as temporary:
            argv_path = Path(temporary) / "argv.json"
            fake = AuthBinary(auth_payload(), argv_path=argv_path)
            try:
                CodexAuthAdapter(binary=str(fake.path)).list_accounts()
                argv = json.loads(argv_path.read_text(encoding="utf-8"))
                self.assertEqual(argv, ["list", "--skip-api", "--json"])
                self.assertNotIn("--api", argv)
            finally:
                fake.close()

    def test_pre_pool_state_loads_as_current_only(self):
        with tempfile.TemporaryDirectory(prefix="nightwatch-current-only-") as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=root, check=True, stdout=subprocess.DEVNULL)
            store = NightwatchStore(root, state_home=Path(temporary) / "state")
            store.initialize("legacy", "legacy goal", str(root))
            state = json.loads(store.state_path.read_text(encoding="utf-8"))
            for key in ("account_mode", "authorized_accounts", "current_account_key", "current_account_fingerprint", "active_account_fingerprint", "account_generation", "account_lease", "account_claim", "account_reselect", "account_snapshots", "account_reset_times", "account_errors", "last_switch_reason", "cross_account_thread_mode", "thread_handoff"):
                state.pop(key, None)
            store.state_path.write_text(json.dumps(state), encoding="utf-8")
            loaded = store.load_state()
            self.assertEqual(loaded.get("account_mode", "CURRENT_ONLY"), "CURRENT_ONLY")
            self.assertEqual(loaded.get("authorized_accounts", []), [])

    def test_all_account_auth_failures_block_the_mission(self):
        with tempfile.TemporaryDirectory(prefix="nightwatch-auth-failover-") as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=root, check=True, stdout=subprocess.DEVNULL)
            from nightwatch.account_broker import AccountRecord

            class Auth:
                def list_accounts(self):
                    return [AccountRecord("user::a"), AccountRecord("user::b")]

            def unavailable(_home, _fingerprint, _fd):
                raise AccountUnavailable("authentication required")

            coordinator = AccountPoolCoordinator(
                Auth(),
                AccountLeaseBroker(Path(temporary) / "leases"),
                quota_factory=unavailable,
                capsule_factory=lambda _adapter, _key, _run_id, _generation, **_kwargs: _FakeCapsule(Path(temporary)),
            )
            store = NightwatchStore(root, state_home=Path(temporary) / "state")
            store.initialize("auth-fail", "auth failover", str(root), account_mode="AUTO_POOL", authorized_accounts=["user::a", "user::b"])
            self.assertFalse(Supervisor(store, account_pool=coordinator)._prepare_pool_account(reselect=True))
            self.assertEqual(store.load_state()["state"], "BLOCKED")

    def test_app_server_probe_accepts_explicit_codex_home_and_tags_account(self):
        fake_app = PRODUCT.parent / "test-artifacts" / "fake-app-server" / "fake_app_server.py"
        with tempfile.TemporaryDirectory(prefix="nightwatch-account-home-") as temporary:
            environment_file = Path(temporary) / "observed-codex-home"
            with patch.dict(os.environ, {"FAKE_APP_SERVER_ENV_FILE": str(environment_file)}):
                quota = AppServerQuotaProvider(binary=str(fake_app), codex_home=temporary, account_fingerprint="acct-test").read()
            self.assertEqual(environment_file.read_text(encoding="utf-8"), str(Path(temporary).resolve()))
        self.assertEqual(quota.source, "live_app_server")
        self.assertEqual(quota.account_fingerprint, "acct-test")

    def test_capsule_sync_preserves_a_refreshed_snapshot_across_switches(self):
        with tempfile.TemporaryDirectory(prefix="nightwatch-auth-capsule-") as temporary:
            root = Path(temporary)
            canonical = root / "canonical"
            canonical.mkdir(mode=0o700)
            (canonical / "registry.json").write_text(json.dumps({
                "accounts": {
                    "user::a": {"account_key": "user::a", "alias": "a", "token": "a-old"},
                    "user::b": {"account_key": "user::b", "alias": "b", "token": "b-old"},
                },
                "active": "user::a",
            }), encoding="utf-8")
            binary = root / "codex-auth"
            binary.write_text(
                """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
home = Path(os.environ['CODEX_HOME'])
registry = home / 'registry.json'
value = json.loads(registry.read_text()) if registry.exists() else {'accounts': {}, 'active': None}
command = sys.argv[1]
if command == 'list':
    print(json.dumps({'schema_version': 1, 'command': 'list', 'accounts': [dict(row, active=key == value.get('active')) for key, row in value['accounts'].items()]}))
elif command == 'switch':
    value['active'] = sys.argv[2]
    registry.write_text(json.dumps(value))
    row = dict(value['accounts'][sys.argv[2]])
    print(json.dumps({'schema_version': 1, 'command': 'switch', 'switched_to': row}))
elif command == 'remove':
    key = sys.argv[2]
    value['accounts'].pop(key, None)
    if value.get('active') == key:
        value['active'] = next(iter(value['accounts']), None)
    registry.write_text(json.dumps(value))
    print(json.dumps({'schema_version': 1, 'command': 'remove', 'removed': [{'account_key': key, 'unknown_optional': True}]}))
elif command == 'export':
    destination = Path(sys.argv[2])
    destination.mkdir(parents=True, exist_ok=True)
    for key, row in value['accounts'].items():
        (destination / (key.replace('::', '--') + '.auth.json')).write_text(json.dumps(row))
elif command == 'import':
    for path in Path(sys.argv[2]).glob('*.auth.json'):
        row = json.loads(path.read_text())
        value['accounts'][row['account_key']] = row
    registry.write_text(json.dumps(value))
""",
                encoding="utf-8",
            )
            binary.chmod(0o700)
            adapter = CodexAuthAdapter(binary=str(binary), codex_home=canonical)
            capsule_root = root / "capsules"
            capsule = AccountCapsule.create(adapter, "user::a", "run", 1, capsule_root)
            self.assertFalse(capsule.export_root.exists())
            for path in capsule.root.rglob("*.auth.json"):
                self.assertTrue(path.is_relative_to(capsule.codex_home))
            self.assertEqual(
                [(record.account_key, record.active) for record in capsule.capsule_adapter.list_accounts()],
                [("user::a", True)],
            )
            capsule_registry = capsule.codex_home / "registry.json"
            refreshed = json.loads(capsule_registry.read_text())
            refreshed["accounts"]["user::a"]["token"] = "a-refreshed"
            capsule_registry.write_text(json.dumps(refreshed), encoding="utf-8")
            capsule.close()

            canonical_value = json.loads((canonical / "registry.json").read_text())
            self.assertEqual(canonical_value["accounts"]["user::a"]["token"], "a-refreshed")
            self.assertEqual(canonical_value["accounts"]["user::b"]["token"], "b-old")

            capsule_b = AccountCapsule.create(adapter, "user::b", "run", 2, capsule_root)
            capsule_b.close()
            self.assertEqual(json.loads((canonical / "registry.json").read_text())["active"], "user::a")
            capsule_a = AccountCapsule.create(adapter, "user::a", "run", 3, capsule_root)
            self.assertEqual(json.loads((capsule_a.codex_home / "registry.json").read_text())["accounts"]["user::a"]["token"], "a-refreshed")
            capsule_a.close()
            stale = AccountCapsule.create(adapter, "user::a", "run", 4, capsule_root)
            manifest_path = stale.root / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["owner"] = {"pid": os.getpid(), "starttime": "stale", "executable": "/not-the-current-process"}
            manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
            recovered = AccountCapsule.create(adapter, "user::a", "run", 4, capsule_root)
            recovered.close()

    def test_capsule_prune_failure_cleans_temporary_credentials(self):
        fake = AuthBinary(auth_payload(), remove_result=["user::b"])
        try:
            with tempfile.TemporaryDirectory(prefix="nightwatch-auth-capsule-failure-") as temporary:
                root = Path(temporary) / "capsules"
                with self.assertRaises(AccountSchemaError):
                    AccountCapsule.create(CodexAuthAdapter(binary=str(fake.path)), "user::a", "run", 1, root)
                self.assertEqual(list(root.iterdir()), [])
        finally:
            fake.close()

    def test_capsule_crash_seams_leave_parseable_registry_and_attributable_capsule(self):
        create_points = ("AFTER_CANONICAL_EXPORT", "AFTER_CAPSULE_IMPORT", "AFTER_CAPSULE_PRUNE")
        sync_points = ("BEFORE_CAPSULE_EXPORT", "AFTER_CAPSULE_EXPORT", "BEFORE_CANONICAL_IMPORT", "AFTER_CANONICAL_IMPORT")
        fake_code = """#!/usr/bin/env python3
import json, sys
from pathlib import Path
home = Path(__import__('os').environ['CODEX_HOME'])
registry = home / 'registry.json'
value = json.loads(registry.read_text()) if registry.exists() else {'accounts': {}, 'active': None}
command = sys.argv[1]
if command == 'list':
    rows = [dict(row, active=key == value.get('active')) for key, row in value['accounts'].items()]
    if '--active' in sys.argv:
        rows = [row for row in rows if row['active']]
    print(json.dumps({'schema_version': 1, 'command': 'list', 'accounts': rows}))
elif command == 'switch':
    value['active'] = sys.argv[2]
    registry.write_text(json.dumps(value))
    print(json.dumps({'schema_version': 1, 'command': 'switch', 'switched_to': {'account_key': sys.argv[2]}}))
elif command == 'remove':
    key = sys.argv[2]
    value['accounts'].pop(key, None)
    if value.get('active') == key:
        value['active'] = next(iter(value['accounts']), None)
    registry.write_text(json.dumps(value))
    print(json.dumps({'schema_version': 1, 'command': 'remove', 'removed': [{'account_key': key}]}))
elif command == 'export':
    destination = Path(sys.argv[2])
    destination.mkdir(parents=True, exist_ok=True)
    for key, row in value['accounts'].items():
        (destination / (key.replace('::', '--') + '.auth.json')).write_text(json.dumps(row))
elif command == 'import':
    for path in Path(sys.argv[2]).glob('*.auth.json'):
        row = json.loads(path.read_text())
        value['accounts'][row['account_key']] = row
    registry.write_text(json.dumps(value))
"""
        for point in (*create_points, *sync_points):
            with self.subTest(point=point):
                with tempfile.TemporaryDirectory(prefix="nightwatch-capsule-crash-") as temporary:
                    root = Path(temporary)
                    canonical = root / "canonical"
                    canonical.mkdir(mode=0o700)
                    (canonical / "registry.json").write_text(json.dumps({
                        "accounts": {
                            "user::a": {"account_key": "user::a", "token": "a-old"},
                            "user::b": {"account_key": "user::b", "token": "b-old"},
                        },
                        "active": "user::a",
                    }), encoding="utf-8")
                    binary = root / "codex-auth"
                    binary.write_text(fake_code, encoding="utf-8")
                    binary.chmod(0o700)
                    child_code = (
                        "import sys; "
                        "from nightwatch.account_broker import AccountCapsule, AccountRegistryLockBroker, CodexAuthAdapter; "
                        "adapter = CodexAuthAdapter(binary=sys.argv[1], codex_home=sys.argv[2], "
                        "registry_lock=AccountRegistryLockBroker(sys.argv[4])); "
                        "capsule = AccountCapsule.create(adapter, 'user::a', 'crash-run', 1, sys.argv[3]); "
                        "capsule.synchronize() if sys.argv[5] == 'sync' else None"
                    )
                    environment = dict(os.environ)
                    environment.update({
                        "PYTHONPATH": str(PRODUCT),
                        "NIGHTWATCH_ENABLE_TEST_CRASH_HOOKS": "1",
                        "NIGHTWATCH_TEST_CRASH_POINT": point,
                    })
                    mode = "sync" if point in sync_points else "create"
                    child = subprocess.Popen(
                        [sys.executable, "-c", child_code, str(binary), str(canonical), str(root / "capsules"), str(root / "control"), mode],
                        env=environment,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    try:
                        return_code = child.wait(timeout=5)
                        diagnostics = child.stderr.read() if child.stderr is not None else ""
                        self.assertEqual(return_code, -9, f"{point}: {diagnostics}")
                        registry = json.loads((canonical / "registry.json").read_text(encoding="utf-8"))
                        self.assertEqual(registry["accounts"]["user::b"]["token"], "b-old")
                        capsule_dirs = [path for path in (root / "capsules").iterdir() if path.is_dir()]
                        self.assertEqual(len(capsule_dirs), 1)
                        manifest = json.loads((capsule_dirs[0] / "manifest.json").read_text(encoding="utf-8"))
                        self.assertEqual(manifest["account_fingerprint"], account_fingerprint("user::a"))
                    finally:
                        if child.poll() is None:
                            child.kill()
                            child.wait(timeout=5)
                        if child.stdout is not None:
                            child.stdout.close()
                        if child.stderr is not None:
                            child.stderr.close()


class AccountSelectionTests(unittest.TestCase):
    def candidate(self, key: str, short: float | None, weekly: float | None, **kwargs) -> AccountCandidate:
        return AccountCandidate(key, snapshot(short, weekly, now=1_000), **kwargs)

    def test_max_usable_capacity_uses_both_windows(self):
        best = select_best_account([
            self.candidate("weekly-exhausted", 0, 100),
            self.candidate("balanced", 60, 40),
        ])
        self.assertIsNotNone(best)
        self.assertEqual(best.account_key, "balanced")

    def test_selection_tie_breakers_are_deterministic(self):
        candidates = [
            self.candidate("z", 20, 20),
            self.candidate("a", 20, 20),
        ]
        expected = min(candidates, key=lambda item: item.fingerprint).account_key
        self.assertEqual(select_best_account(candidates).account_key, expected)

    def test_exhausted_leased_auth_error_and_unknown_candidates_are_rejected(self):
        candidates = [
            self.candidate("short", 100, 0),
            self.candidate("leased", 0, 0, leased=True),
            self.candidate("auth", 0, 0, auth_error=True),
            self.candidate("unknown", None, 0),
        ]
        self.assertIsNone(select_best_account(candidates))

    def test_earliest_reset_considers_all_authorized_exhausted_windows(self):
        candidates = [self.candidate("a", 100, 20), self.candidate("b", 20, 100)]
        self.assertEqual(earliest_relevant_reset(candidates), 1_300)


class _FakeCapsule:
    def __init__(self, home: Path):
        self.codex_home = home

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class _FakeAuth:
    def __init__(self):
        from nightwatch.account_broker import AccountRecord
        self.accounts = [AccountRecord("user::a", alias="a"), AccountRecord("user::b", alias="b")]

    def list_accounts(self):
        return self.accounts


class _StaticQuota:
    def __init__(self, value):
        self.value = value

    def read(self):
        return self.value


class AccountProbeTests(unittest.TestCase):
    def test_probe_holds_lease_for_app_server_and_selects_recovered_account(self):
        with tempfile.TemporaryDirectory(prefix="nightwatch-pool-probe-") as temporary:
            root = Path(temporary)
            lease_broker = AccountLeaseBroker(root / "leases")
            calls: list[tuple[str, int]] = []

            def quota_factory(home: Path, fingerprint: str, lease_fd: int):
                key = "user::a" if fingerprint == account_fingerprint("user::a") else "user::b"
                calls.append((key, lease_fd))
                with self.assertRaises(AccountBusy):
                    lease_broker.acquire(key, "other-run", "/other")
                return _StaticQuota(snapshot(100 if key == "user::a" else 40, 100 if key == "user::a" else 60))

            def capsule_factory(_adapter, key, _run_id, _generation, **_kwargs):
                home = root / account_fingerprint(key)
                home.mkdir(exist_ok=True)
                return _FakeCapsule(home)

            coordinator = AccountPoolCoordinator(
                _FakeAuth(),
                lease_broker,
                quota_factory=quota_factory,
                capsule_factory=capsule_factory,
            )
            decision = coordinator.probe(["user::a", "user::b"], "run", "/repo", 1)
            self.assertEqual(decision.selected.account_key, "user::b")
            self.assertEqual([key for key, _fd in calls], ["user::a", "user::b"])


class AccountPoolFakeE2ETests(unittest.TestCase):
    def test_after_provider_exit_crash_releases_account_lease_and_reconciles(self):
        fake_codex = PRODUCT.parent / "test-artifacts" / "fake-codex" / "fake_codex.py"
        with tempfile.TemporaryDirectory(prefix="nightwatch-pool-provider-exit-") as temporary:
            root = Path(temporary)
            repo = root / "repo"
            repo.mkdir()
            for args in (
                ("init", "-q"),
                ("config", "user.email", "nightwatch@example.invalid"),
                ("config", "user.name", "Nightwatch"),
            ):
                subprocess.run(["git", *args], cwd=repo, check=True, stdout=subprocess.DEVNULL)
            (repo / "README.md").write_text("fixture\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
            plan = root / "plan.json"
            progress = root / "progress.json"
            plan.write_text(json.dumps({"milestones": [{"id": "M1", "title": "crash recovery", "weight": 1}]}), encoding="utf-8")
            progress.write_text(json.dumps({"milestones": [{"id": "M1", "status": "implemented"}]}), encoding="utf-8")
            state_home = root / "state"
            lease_root = root / "leases"
            capsule_root = root / "capsules"
            once_file = root / "crash-once"
            store = NightwatchStore(repo, state_home=state_home)
            store.initialize(
                "pool-provider-exit",
                "recover after provider exit",
                str(repo),
                verify_commands=["test -f fake-implemented.txt", "git diff --check"],
                account_mode="AUTO_POOL",
                authorized_accounts=["user::a", "user::b"],
            )
            child_code = (
                "import sys; from pathlib import Path; "
                "from nightwatch.account_broker import AccountLeaseBroker, AccountPoolCoordinator, account_fingerprint; "
                "from nightwatch.models import QuotaSnapshot, QuotaWindow; "
                "from nightwatch.storage import NightwatchStore; from nightwatch.supervisor import Supervisor; "
                "from test_account_pool import _FakeAuth, _FakeCapsule, _StaticQuota; "
                "quota = QuotaSnapshot('live_app_server', 'now', QuotaWindow('5h', 10, 300, None), QuotaWindow('weekly', 20, 10080, None)); "
                "coordinator = AccountPoolCoordinator(_FakeAuth(), AccountLeaseBroker(sys.argv[3]), "
                "quota_factory=lambda _home, _fingerprint, _fd: _StaticQuota(quota), "
                "capsule_factory=lambda _adapter, key, _run, _generation, **_kwargs: _FakeCapsule(Path(sys.argv[4]) / account_fingerprint(key))); "
                "Supervisor(NightwatchStore(sys.argv[1], state_home=sys.argv[2]), account_pool=coordinator).execute(start=True)"
            )
            environment = dict(os.environ)
            environment.update({
                "PYTHONPATH": os.pathsep.join((str(PRODUCT), str(PRODUCT / "tests"))),
                "NIGHTWATCH_CODEX_BIN": str(fake_codex),
                "NIGHTWATCH_SKIP_AUTH_CHECK": "1",
                "FAKE_CODEX_SCENARIO": "normal",
                "FAKE_CODEX_RESUME_SCENARIO": "normal",
                "FAKE_CODEX_PLAN_FILE": str(plan),
                "FAKE_CODEX_PROGRESS_FILE": str(progress),
                "NIGHTWATCH_ENABLE_TEST_CRASH_HOOKS": "1",
                "NIGHTWATCH_TEST_CRASH_POINT": "AFTER_PROVIDER_EXIT",
                "NIGHTWATCH_TEST_CRASH_ONCE_FILE": str(once_file),
            })
            child = subprocess.run(
                [sys.executable, "-c", child_code, str(repo), str(state_home), str(lease_root), str(capsule_root)],
                cwd=repo,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(child.returncode, -9, child.stderr)
            crashed = store.load_state()
            self.assertIsNone(crashed["active_process"])
            self.assertIsNone(crashed["account_lease"])
            self.assertEqual(crashed["account_claim"]["phase"], "provider_exited")
            selected_key = crashed["current_account_key"]
            available = AccountLeaseBroker(lease_root).acquire(selected_key, "reconciler", repo)
            available.release()

            coordinator = AccountPoolCoordinator(
                _FakeAuth(),
                AccountLeaseBroker(lease_root),
                quota_factory=lambda _home, _fingerprint, _fd: _StaticQuota(snapshot(10, 20)),
                capsule_factory=lambda _adapter, key, _run, _generation, **_kwargs: _FakeCapsule(capsule_root / account_fingerprint(key)),
            )
            with patch.dict(os.environ, environment, clear=False):
                final = Supervisor(store, account_pool=coordinator).execute(start=False)
            self.assertEqual(final["state"], State.DONE.value)
            self.assertIsNone(final["account_lease"])

    def test_a_to_b_wait_and_return_to_a_uses_controlled_handoffs(self):
        fake_codex = PRODUCT.parent / "test-artifacts" / "fake-codex" / "fake_codex.py"
        with tempfile.TemporaryDirectory(prefix="nightwatch-pool-e2e-") as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            def git(*args: str) -> None:
                subprocess.run(["git", *args], cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)

            git("init", "-q")
            git("config", "user.email", "nightwatch@example.invalid")
            git("config", "user.name", "Nightwatch")
            (root / "README.md").write_text("fixture\n", encoding="utf-8")
            git("add", "README.md")
            git("commit", "-qm", "fixture")
            plan = root / "plan-source.json"
            plan.write_text(json.dumps({"milestones": [{"id": "M1", "title": "pool mission", "weight": 1}]}), encoding="utf-8")
            progress = root / "progress-source.json"
            progress.write_text(json.dumps({"milestones": [{"id": "M1", "status": "implemented"}]}), encoding="utf-8")
            state_home = Path(temporary) / "trusted-state"
            leases = Path(temporary) / "leases"
            a, b = "user::a", "user::b"
            a_fp, b_fp = account_fingerprint(a), account_fingerprint(b)

            from nightwatch.account_broker import AccountRecord

            class PoolAuth:
                def list_accounts(self):
                    return [AccountRecord(a, alias="personal"), AccountRecord(b, alias="backup")]

            class Sequence:
                def __init__(self):
                    self.calls = 0

                def __call__(self, _home, fingerprint, _fd):
                    self.calls += 1
                    index = (self.calls - 1) // 2
                    key = a if fingerprint == a_fp else b
                    if index == 0:
                        used = (0, 20) if key == a else (30, 40)
                    elif index == 1:
                        used = (100, 100) if key == a else (30, 40)
                    elif index == 2:
                        used = (100, 100)
                    else:
                        used = (0, 20) if key == a else (100, 100)
                    reset = int(time.time())
                    value = QuotaSnapshot("live_app_server", "now", QuotaWindow("5h", used[0], 300, reset), QuotaWindow("weekly", used[1], 10080, reset))
                    return _StaticQuota(value)

            sequence = Sequence()
            lease_broker = AccountLeaseBroker(leases)

            def capsule_factory(_adapter, key, _run_id, _generation, **_kwargs):
                home = Path(temporary) / "capsules" / account_fingerprint(key)
                home.mkdir(parents=True, exist_ok=True)
                return _FakeCapsule(home)

            coordinator = AccountPoolCoordinator(
                PoolAuth(),
                lease_broker,
                quota_factory=sequence,
                capsule_factory=capsule_factory,
            )
            store = NightwatchStore(root, state_home=state_home)
            store.initialize(
                "pool-e2e",
                "continue the pool mission",
                str(root),
                verify_commands=["test -f fake-implemented.txt", "git diff --check"],
                account_mode="AUTO_POOL",
                authorized_accounts=[a, b],
            )
            with patch.dict(os.environ, {
                "NIGHTWATCH_CODEX_BIN": str(fake_codex),
                "NIGHTWATCH_SKIP_AUTH_CHECK": "1",
                "NIGHTWATCH_QUOTA_BUFFER_SECONDS": "0",
                "NIGHTWATCH_WAIT_POLL_SECONDS": "0.01",
                "FAKE_CODEX_SCENARIO": "pool",
                "FAKE_CODEX_ACCOUNT_A": a_fp,
                "FAKE_CODEX_ACCOUNT_B": b_fp,
                "FAKE_CODEX_PLAN_FILE": str(plan),
                "FAKE_CODEX_PROGRESS_FILE": str(progress),
            }, clear=False):
                final = Supervisor(store, account_pool=coordinator).execute(start=True)

            events = [json.loads(line) for line in store.events_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(
                final["state"],
                "DONE",
                f"state={final['state']} calls={sequence.calls} "
                f"events={[event.get('event') for event in events]}",
            )
            self.assertEqual(final["authorized_accounts"], [a, b])
            self.assertEqual(final["current_account_key"], a)
            self.assertEqual(final["account_generation"], 3)
            self.assertEqual(final["cross_account_thread_mode"], "INCONCLUSIVE")
            self.assertEqual(final["thread_handoff"]["mode"], "CONTROLLED_THREAD_HANDOFF")
            self.assertEqual(final["thread_handoff"]["status"], "captured")
            self.assertIsNone(final["account_lease"])
            self.assertEqual(sequence.calls, 8)
            events = [json.loads(line) for line in store.events_path.read_text(encoding="utf-8").splitlines()]
            names = [event["event"] for event in events]
            self.assertEqual(names.count("provider_started"), 3)
            self.assertEqual(names.count("provider_finished"), 3)
            self.assertEqual(names.count("account_quota_exhausted"), 2)
            self.assertIn("account_pool_wait", names)
            self.assertIn("account_pool_recovered", names)
            self.assertLess(names.index("provider_finished"), names.index("account_quota_exhausted"))
            self.assertEqual(json.loads((root / ".fake-codex-state.json").read_text(encoding="utf-8")), {
                "starts": 3,
                "resumes": 0,
                "thread_id": "POOL-3",
                "pool_counts": {a_fp: 2, b_fp: 1},
            })

    def test_weekly_exhaustion_governs_wait_until_weekly_recovery(self):
        fake_codex = PRODUCT.parent / "test-artifacts" / "fake-codex" / "fake_codex.py"
        with tempfile.TemporaryDirectory(prefix="nightwatch-pool-weekly-") as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()

            def git(*args: str) -> None:
                subprocess.run(["git", *args], cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)

            git("init", "-q")
            git("config", "user.email", "nightwatch@example.invalid")
            git("config", "user.name", "Nightwatch")
            (root / "README.md").write_text("fixture\n", encoding="utf-8")
            git("add", "README.md")
            git("commit", "-qm", "fixture")
            plan = root / "plan-source.json"
            plan.write_text(json.dumps({"milestones": [{"id": "M1", "title": "weekly pool mission", "weight": 1}]}), encoding="utf-8")
            progress = root / "progress-source.json"
            progress.write_text(json.dumps({"milestones": [{"id": "M1", "status": "implemented"}]}), encoding="utf-8")
            a, b = "user::weekly-a", "user::weekly-b"
            a_fp, b_fp = account_fingerprint(a), account_fingerprint(b)
            now = int(time.time())

            class PoolAuth:
                def list_accounts(self):
                    from nightwatch.account_broker import AccountRecord
                    return [AccountRecord(a, alias="personal"), AccountRecord(b, alias="backup")]

            class Sequence:
                def __init__(self):
                    self.calls = 0

                def __call__(self, _home, fingerprint, _fd):
                    self.calls += 1
                    phase = (self.calls - 1) // 2
                    key = a if fingerprint == a_fp else b
                    if phase == 0:
                        used = (0, 100) if key == a else (65, 60)
                    elif phase in {1, 2}:
                        used = (0, 100) if key == a else (65, 100)
                    else:
                        used = (0, 20) if key == a else (65, 100)
                    short_reset = now if phase == 1 else now + 900
                    weekly_reset = now + 1800
                    return _StaticQuota(QuotaSnapshot(
                        "live_app_server",
                        "now",
                        QuotaWindow("5h", used[0], 300, short_reset),
                        QuotaWindow("weekly", used[1], 10080, weekly_reset),
                    ))

            sequence = Sequence()
            coordinator = AccountPoolCoordinator(
                PoolAuth(),
                AccountLeaseBroker(Path(temporary) / "leases"),
                quota_factory=sequence,
                capsule_factory=lambda _adapter, key, _run_id, _generation, **_kwargs: _FakeCapsule(Path(temporary) / account_fingerprint(key)),
            )
            store = NightwatchStore(root, state_home=Path(temporary) / "state")
            store.initialize(
                "pool-weekly-e2e",
                "continue weekly pool mission",
                str(root),
                verify_commands=["test -f fake-implemented.txt", "git diff --check"],
                account_mode="AUTO_POOL",
                authorized_accounts=[a, b],
            )
            with patch.dict(os.environ, {
                "NIGHTWATCH_CODEX_BIN": str(fake_codex),
                "NIGHTWATCH_SKIP_AUTH_CHECK": "1",
                "NIGHTWATCH_QUOTA_BUFFER_SECONDS": "0",
                "NIGHTWATCH_WAIT_POLL_SECONDS": "0.01",
                "FAKE_CODEX_SCENARIO": "pool_weekly",
                "FAKE_CODEX_ACCOUNT_A": a_fp,
                "FAKE_CODEX_ACCOUNT_B": b_fp,
                "FAKE_CODEX_PLAN_FILE": str(plan),
                "FAKE_CODEX_PROGRESS_FILE": str(progress),
            }, clear=False), patch("nightwatch.supervisor._sleep_until") as sleep_until:
                final = Supervisor(store, account_pool=coordinator).execute(start=True)

            self.assertEqual(final["state"], "DONE")
            self.assertEqual(final["current_account_key"], a)
            self.assertEqual(final["account_generation"], 2)
            self.assertEqual(sequence.calls, 8)
            self.assertEqual(sleep_until.call_count, 2)
            events = store.load_events()
            self.assertEqual([event["event"] for event in events].count("account_pool_wait"), 2)
            self.assertEqual(json.loads((root / ".fake-codex-state.json").read_text(encoding="utf-8")), {"starts": 2, "resumes": 0, "thread_id": "POOL-2", "pool_counts": {b_fp: 1, a_fp: 1}})

    def test_auto_pool_can_complete_more_than_20_normal_quota_cycles(self):
        fake_codex = PRODUCT.parent / "test-artifacts" / "fake-codex" / "fake_codex.py"
        with tempfile.TemporaryDirectory(prefix="nightwatch-pool-long-") as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()

            def git(*args: str) -> None:
                subprocess.run(["git", *args], cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)

            git("init", "-q")
            git("config", "user.email", "nightwatch@example.invalid")
            git("config", "user.name", "Nightwatch")
            (root / "README.md").write_text("fixture\n", encoding="utf-8")
            git("add", "README.md")
            git("commit", "-qm", "fixture")
            plan = root / "plan-source.json"
            plan.write_text(json.dumps({"milestones": [{"id": "M1", "title": "long pool mission", "weight": 1}]}), encoding="utf-8")
            progress = root / "progress-source.json"
            progress.write_text(json.dumps({"milestones": [{"id": "M1", "status": "implemented"}]}), encoding="utf-8")
            a, b = "user::long-a", "user::long-b"
            a_fp, b_fp = account_fingerprint(a), account_fingerprint(b)

            class PoolAuth:
                def list_accounts(self):
                    from nightwatch.account_broker import AccountRecord
                    return [AccountRecord(a, alias="personal"), AccountRecord(b, alias="backup")]

            class LongSequence:
                def __init__(self):
                    self.calls = 0

                def __call__(self, _home, fingerprint, _fd):
                    self.calls += 1
                    probe = (self.calls - 1) // 2
                    if probe == 0:
                        used = (0, 20) if fingerprint == a_fp else (30, 40)
                    elif probe % 4 in {0, 1}:
                        used = (100, 30) if fingerprint == a_fp else (30, 40)
                    elif probe % 4 == 2:
                        used = (100, 100)
                    else:
                        used = (0, 20) if fingerprint == a_fp else (100, 100)
                    now = int(time.time())
                    return _StaticQuota(QuotaSnapshot(
                        "live_app_server",
                        "now",
                        QuotaWindow("5h", used[0], 300, now),
                        QuotaWindow("weekly", used[1], 10080, now + 1),
                    ))

            sequence = LongSequence()
            coordinator = AccountPoolCoordinator(
                PoolAuth(),
                AccountLeaseBroker(Path(temporary) / "leases"),
                quota_factory=sequence,
                capsule_factory=lambda _adapter, key, _run_id, _generation, **_kwargs: _FakeCapsule(Path(temporary) / account_fingerprint(key)),
            )
            store = NightwatchStore(root, state_home=Path(temporary) / "state")
            store.initialize(
                "pool-long-e2e",
                "continue long pool mission",
                str(root),
                verify_commands=["test -f fake-implemented.txt", "git diff --check"],
                account_mode="AUTO_POOL",
                authorized_accounts=[a, b],
            )
            with patch.dict(os.environ, {
                "NIGHTWATCH_CODEX_BIN": str(fake_codex),
                "NIGHTWATCH_SKIP_AUTH_CHECK": "1",
                "NIGHTWATCH_QUOTA_BUFFER_SECONDS": "0",
                "FAKE_CODEX_SCENARIO": "pool_long",
                "FAKE_CODEX_LONG_CYCLES": "30",
                "FAKE_CODEX_ACCOUNT_A": a_fp,
                "FAKE_CODEX_ACCOUNT_B": b_fp,
                "FAKE_CODEX_PLAN_FILE": str(plan),
                "FAKE_CODEX_PROGRESS_FILE": str(progress),
            }, clear=False), patch("nightwatch.supervisor._sleep_until"):
                final = Supervisor(store, account_pool=coordinator).execute(start=True)

            self.assertEqual(final["state"], State.DONE.value)
            self.assertEqual(final["quota_cycles"], 30)
            self.assertEqual(final["recoveries"], 0)
            self.assertEqual(final["recovery_failures"], 0)
            self.assertEqual(final["account_generation"], 31)
            codex_state = json.loads((root / ".fake-codex-state.json").read_text(encoding="utf-8"))
            self.assertEqual(codex_state["starts"] + codex_state.get("resumes", 0), 31)
            self.assertEqual(codex_state["quota_events"], 30)
            self.assertGreaterEqual(sequence.calls, 60)

    def test_repeated_real_recovery_failures_still_trip_circuit_breaker(self):
        with tempfile.TemporaryDirectory(prefix="nightwatch-pool-failure-budget-") as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=root, check=True, stdout=subprocess.DEVNULL)

            class BrokenAuth:
                def list_accounts(self):
                    raise AccountSchemaError("registry authority unavailable")

            store = NightwatchStore(root, state_home=Path(temporary) / "state")
            store.initialize("pool-failure", "failure budget", str(root), account_mode="AUTO_POOL", authorized_accounts=["user::a"])
            store.transition(State.PREFLIGHT, "preflight_started", "test")
            store.transition(State.RUNNING, "provider_launch_ready", "test")
            store.transition(
                State.WAIT_QUOTA,
                "account_pool_wait",
                "test wait",
                {"next_resume_at": "2000-01-01T00:00:00Z", "account_reselect": True},
            )
            coordinator = AccountPoolCoordinator(BrokenAuth(), AccountLeaseBroker(Path(temporary) / "leases"))
            supervisor = Supervisor(store, account_pool=coordinator)
            with patch("nightwatch.supervisor._sleep_until"):
                self.assertTrue(supervisor._wait_and_revalidate_pool())
                self.assertTrue(supervisor._wait_and_revalidate_pool())
                self.assertFalse(supervisor._wait_and_revalidate_pool())
            state = store.load_state()
            self.assertEqual(state["state"], State.BLOCKED.value)
            self.assertEqual(state["recovery_failures"], 3)

    def test_quota_cycles_are_reported_separately_from_failures(self):
        with tempfile.TemporaryDirectory(prefix="nightwatch-pool-counters-") as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=root, check=True, stdout=subprocess.DEVNULL)
            store = NightwatchStore(root, state_home=Path(temporary) / "state")
            store.initialize("pool-counters", "counter test", str(root), account_mode="AUTO_POOL", authorized_accounts=["user::a"])
            store.transition(State.PREFLIGHT, "preflight_started", "test")
            store.transition(State.RUNNING, "provider_launch_ready", "test")
            final = Supervisor(store, account_pool=object())._rotate_pool_after_quota(
                ProviderResult(1, None, "THREAD", 1, 0, error_kind=ErrorKind.QUOTA_5H, reset_at=int(time.time()))
            )
            self.assertEqual(final["quota_cycles"], 1)
            self.assertEqual(final["recovery_failures"], 0)
            self.assertEqual(final["recoveries"], 0)

    def test_current_only_existing_recovery_safety_is_preserved(self):
        with tempfile.TemporaryDirectory(prefix="nightwatch-current-recovery-") as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=root, check=True, stdout=subprocess.DEVNULL)
            store = NightwatchStore(root, state_home=Path(temporary) / "state")
            store.initialize("current-recovery", "current recovery", str(root))
            store.transition(State.PREFLIGHT, "preflight_started", "test")
            store.transition(State.RUNNING, "provider_launch_ready", "test")
            store.mutate("set_recovery_budget", "test current-only budget", lambda item: {**item, "recoveries": 20})
            final = Supervisor(store)._enter_quota_wait(
                ProviderResult(1, None, "THREAD", 1, 0, error_kind=ErrorKind.QUOTA_5H, reset_at=int(time.time()))
            )
            self.assertEqual(final["state"], State.FAILED.value)


class AccountLeaseTests(unittest.TestCase):
    def test_same_account_is_exclusive_and_different_account_is_independent(self):
        with tempfile.TemporaryDirectory(prefix="nightwatch-leases-") as temporary:
            broker = AccountLeaseBroker(Path(temporary))
            first = broker.acquire("user::a", "run-a", "/repo-a")
            try:
                with self.assertRaises(AccountBusy):
                    broker.acquire("user::a", "run-b", "/repo-b")
                second = broker.acquire("user::b", "run-b", "/repo-b")
                second.release()
            finally:
                first.release()
            reusable = broker.acquire("user::a", "run-c", "/repo-c")
            reusable.release()

    def test_corrupt_lease_fails_closed_without_deleting_state(self):
        with tempfile.TemporaryDirectory(prefix="nightwatch-leases-") as temporary:
            broker = AccountLeaseBroker(Path(temporary))
            path = broker.lease_path("user::a")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("not-json\n", encoding="utf-8")
            path.chmod(0o600)
            with self.assertRaises(AccountSchemaError):
                broker.acquire("user::a", "run-a", "/repo-a")
            self.assertEqual(path.read_text(encoding="utf-8"), "not-json\n")

    def test_stale_pid_identity_is_reconciled_without_touching_other_state(self):
        with tempfile.TemporaryDirectory(prefix="nightwatch-leases-") as temporary:
            broker = AccountLeaseBroker(Path(temporary))
            path = broker.lease_path("user::a")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "schema_version": 1,
                "account_fingerprint": account_fingerprint("user::a"),
                "lock_root": {
                    "device": broker._lock_root_identity[0],
                    "inode": broker._lock_root_identity[1],
                },
                "run_id": "old",
                "repo": "/old",
                "pid": os.getpid(),
                "starttime": "not-this-process",
                "executable": "/not-this-executable",
                "phase": "provider",
            }) + "\n", encoding="utf-8")
            path.chmod(0o600)
            lease = broker.acquire("user::a", "new", "/new")
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(value["run_id"], "new")
            finally:
                lease.release()

    def test_process_crash_releases_kernel_lease_and_allows_reconciliation(self):
        with tempfile.TemporaryDirectory(prefix="nightwatch-leases-") as temporary:
            root = Path(temporary)
            code = (
                "import time, sys; "
                "from nightwatch.account_broker import AccountLeaseBroker; "
                "lease = AccountLeaseBroker(sys.argv[1]).acquire('user::a', 'crashed', '/repo'); "
                "print('owned', flush=True); time.sleep(30)"
            )
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(PRODUCT)
            child = subprocess.Popen([sys.executable, "-c", code, str(root)], env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), "owned")
                broker = AccountLeaseBroker(root)
                with self.assertRaises(AccountBusy):
                    broker.acquire("user::a", "blocked", "/other")
                child.kill()
                child.wait(timeout=5)
                recovered = broker.acquire("user::a", "recovered", "/repo")
                try:
                    self.assertEqual(json.loads(broker.lease_path("user::a").read_text(encoding="utf-8"))["run_id"], "recovered")
                finally:
                    recovered.release()
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
                if child.stdout is not None:
                    child.stdout.close()
                if child.stderr is not None:
                    child.stderr.close()

    def test_symlinked_account_lease_paths_fail_closed(self):
        with tempfile.TemporaryDirectory(prefix="nightwatch-leases-") as temporary:
            root = Path(temporary)
            target = root / "target"
            target.mkdir()
            root_link = root / "linked-root"
            root_link.symlink_to(target, target_is_directory=True)
            with self.assertRaises(AccountSchemaError):
                AccountLeaseBroker(root_link)

            broker = AccountLeaseBroker(root / "leases")
            outside = root / "outside"
            outside.write_text("preserve\n", encoding="utf-8")
            lease_path = broker.lease_path("user::a")
            lease_path.symlink_to(outside)
            with self.assertRaises(AccountSchemaError):
                broker.acquire("user::a", "run", "/repo")
            self.assertEqual(outside.read_text(encoding="utf-8"), "preserve\n")

    def test_replacing_held_account_lease_path_does_not_bypass_exclusion(self):
        with tempfile.TemporaryDirectory(prefix="nightwatch-lease-path-replace-") as temporary:
            root = Path(temporary)
            broker = AccountLeaseBroker(root / "leases")
            lease = broker.acquire("user::a", "run-a", "/repo-a")
            replacement = root / "replacement.lock"
            try:
                path = broker.lease_path("user::a")
                path.rename(replacement)
                path.write_text("{}\n", encoding="utf-8")
                with self.assertRaises(AccountBusy):
                    broker.acquire("user::a", "run-b", "/repo-b")
                with self.assertRaises(AccountSchemaError):
                    lease.release()
            finally:
                if not lease._released:
                    lease.release()
            self.assertTrue(replacement.exists())

    def test_replacing_held_account_lease_root_does_not_split_exclusion(self):
        with tempfile.TemporaryDirectory(prefix="nightwatch-lease-root-replace-") as temporary:
            root = Path(temporary)
            lease_root = root / "leases"
            broker = AccountLeaseBroker(lease_root)
            lease = broker.acquire("user::a", "run-a", "/repo-a")
            old_root = root / "old-leases"
            try:
                lease_root.rename(old_root)
                lease_root.mkdir(mode=0o700)
                replacement_broker = AccountLeaseBroker(lease_root)
                with self.assertRaises(AccountBusy):
                    replacement_broker.acquire("user::a", "run-b", "/repo-b")
                with self.assertRaises(AccountSchemaError):
                    lease.release()
            finally:
                if not lease._released:
                    lease.release()

    def test_replacing_hidden_lease_lock_root_fails_closed_on_live_metadata(self):
        with tempfile.TemporaryDirectory(prefix="nightwatch-hidden-lease-root-") as temporary:
            root = Path(temporary)
            lease_root = root / "leases"
            broker = AccountLeaseBroker(lease_root)
            lease = broker.acquire("user::a", "run-a", "/repo-a")
            old_lock_root = root / "old-account-locks"
            try:
                broker._lock_root.rename(old_lock_root)
                broker._lock_root.mkdir(mode=0o700)
                replacement_broker = AccountLeaseBroker(lease_root)
                with self.assertRaises(AccountSchemaError):
                    replacement_broker.acquire("user::a", "run-b", "/repo-b")
            finally:
                lease.release()

    def test_replacing_hidden_lease_lock_root_fails_closed_while_descendant_holds_fd(self):
        with tempfile.TemporaryDirectory(prefix="nightwatch-hidden-lease-descendant-") as temporary:
            root = Path(temporary)
            lease_root = root / "leases"
            child_code = (
                "import os, subprocess, sys; "
                "from nightwatch.account_broker import AccountLeaseBroker; "
                "lease = AccountLeaseBroker(sys.argv[1]).acquire('user::a', 'run-a', '/repo-a'); "
                "provider = subprocess.Popen(['/bin/sleep', '30'], stdin=subprocess.DEVNULL, "
                "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, pass_fds=(lease.fd,)); "
                "print(provider.pid, flush=True); os._exit(0)"
            )
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(PRODUCT)
            supervisor = subprocess.Popen(
                [sys.executable, "-c", child_code, str(lease_root)],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            provider_pid: int | None = None
            try:
                assert supervisor.stdout is not None
                provider_pid = int(supervisor.stdout.readline().strip())
                supervisor.wait(timeout=5)
                self.assertEqual(supervisor.returncode, 0)
                hidden_root = root / ".leases.account-locks"
                hidden_root.rename(root / "old-account-locks")
                hidden_root.mkdir(mode=0o700)
                replacement_broker = AccountLeaseBroker(lease_root)
                with self.assertRaises(AccountSchemaError):
                    replacement_broker.acquire("user::a", "run-b", "/repo-b")
            finally:
                if supervisor.poll() is None:
                    supervisor.kill()
                    supervisor.wait(timeout=5)
                if provider_pid is not None:
                    try:
                        os.kill(provider_pid, 9)
                    except ProcessLookupError:
                        pass
                if supervisor.stdout is not None:
                    supervisor.stdout.close()
                if supervisor.stderr is not None:
                    supervisor.stderr.close()


class RegistryLockTests(unittest.TestCase):
    def test_registry_transaction_serializes_active_restore(self):
        with tempfile.TemporaryDirectory(prefix="nightwatch-registry-active-") as temporary:
            root = Path(temporary)
            canonical = root / "canonical"
            canonical.mkdir(mode=0o700)
            (canonical / "registry.json").write_text(json.dumps({
                "accounts": {
                    "user::a": {"account_key": "user::a", "token": "a"},
                    "user::b": {"account_key": "user::b", "token": "b"},
                },
                "active": "user::a",
            }), encoding="utf-8")
            source = root / "source"
            source.mkdir(mode=0o700)
            (source / "a.auth.json").write_text(json.dumps({"account_key": "user::a", "token": "a-refreshed"}), encoding="utf-8")
            binary = root / "codex-auth"
            binary.write_text(
                """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
home = Path(os.environ['CODEX_HOME'])
registry = home / 'registry.json'
value = json.loads(registry.read_text())
command = sys.argv[1]
if command == 'list':
    rows = [dict(row, active=key == value.get('active')) for key, row in value['accounts'].items()]
    if '--active' in sys.argv:
        rows = [row for row in rows if row['active']]
    print(json.dumps({'schema_version': 1, 'command': 'list', 'accounts': rows}))
elif command == 'switch':
    value['active'] = sys.argv[2]
    registry.write_text(json.dumps(value))
    print(json.dumps({'schema_version': 1, 'command': 'switch', 'switched_to': {'account_key': sys.argv[2]}}))
elif command == 'import':
    for path in Path(sys.argv[2]).glob('*.auth.json'):
        row = json.loads(path.read_text())
        value['accounts'][row['account_key']] = row
    registry.write_text(json.dumps(value))
""",
                encoding="utf-8",
            )
            binary.chmod(0o700)
            adapter = CodexAuthAdapter(
                binary=str(binary),
                codex_home=canonical,
                registry_lock=AccountRegistryLockBroker(root / "control"),
            )
            ready = threading.Event()
            switched = threading.Event()

            def external_switch() -> None:
                ready.wait(timeout=2)
                adapter.switch("user::b")
                switched.set()

            thread = threading.Thread(target=external_switch)
            thread.start()
            with adapter.registry_transaction(operation="capsule-sync"):
                active = adapter.active_account()
                self.assertEqual(active.account_key, "user::a")
                ready.set()
                self.assertFalse(switched.wait(0.05))
                adapter.import_accounts(source)
                adapter.switch(active.account_key)
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertTrue(switched.is_set())
            self.assertEqual(json.loads((canonical / "registry.json").read_text())['active'], "user::b")

    def test_concurrent_canonical_refreshes_are_serialized_and_preserved(self):
        with tempfile.TemporaryDirectory(prefix="nightwatch-registry-lock-") as temporary:
            root = Path(temporary)
            canonical = root / "canonical"
            canonical.mkdir(mode=0o700)
            (canonical / "registry.json").write_text(json.dumps({
                "accounts": {
                    "user::a": {"account_key": "user::a", "token": "a-old"},
                    "user::b": {"account_key": "user::b", "token": "b-old"},
                },
                "active": "user::a",
            }), encoding="utf-8")
            source_a = root / "source-a"
            source_b = root / "source-b"
            source_a.mkdir()
            source_b.mkdir()
            (source_a / "a.auth.json").write_text(json.dumps({"account_key": "user::a", "token": "a-new"}), encoding="utf-8")
            (source_b / "b.auth.json").write_text(json.dumps({"account_key": "user::b", "token": "b-new"}), encoding="utf-8")
            marker = root / "active-import"
            binary = root / "codex-auth"
            binary.write_text(
                """#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
home = Path(os.environ['CODEX_HOME'])
if sys.argv[1] != 'import':
    print(json.dumps({'schema_version': 1, 'accounts': []}))
    raise SystemExit(0)
marker = Path(os.environ['FAKE_IMPORT_MARKER'])
try:
    fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
except FileExistsError:
    raise SystemExit('overlapping canonical import')
try:
    value = json.loads((home / 'registry.json').read_text())
    time.sleep(0.08)
    for path in Path(sys.argv[2]).glob('*.auth.json'):
        row = json.loads(path.read_text())
        value['accounts'][row['account_key']] = row
    (home / 'registry.json').write_text(json.dumps(value))
finally:
    marker.unlink(missing_ok=True)
""",
                encoding="utf-8",
            )
            binary.chmod(0o700)
            lock = AccountRegistryLockBroker(root / "control")
            adapter = CodexAuthAdapter(binary=str(binary), codex_home=canonical, registry_lock=lock)
            errors: list[BaseException] = []
            barrier = threading.Barrier(2)

            def sync(source: Path) -> None:
                try:
                    barrier.wait(timeout=2)
                    adapter.import_accounts(source)
                except BaseException as exc:
                    errors.append(exc)

            with patch.dict(os.environ, {"FAKE_IMPORT_MARKER": str(marker)}):
                threads = [threading.Thread(target=sync, args=(source,)) for source in (source_a, source_b)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=3)
            self.assertFalse(errors, errors)
            final = json.loads((canonical / "registry.json").read_text(encoding="utf-8"))
            self.assertEqual(final["accounts"]["user::a"]["token"], "a-new")
            self.assertEqual(final["accounts"]["user::b"]["token"], "b-new")

    def test_registry_lock_is_not_held_during_provider_scope(self):
        with tempfile.TemporaryDirectory(prefix="nightwatch-registry-provider-") as temporary:
            root = Path(temporary)
            registry = AccountRegistryLockBroker(root / "control")
            lease_broker = AccountLeaseBroker(root / "leases")
            observed: list[bool] = []

            def quota_factory(_home, _fingerprint, _fd):
                lock = registry.acquire(operation="provider-scope", timeout=0.2)
                try:
                    observed.append(True)
                finally:
                    lock.release()
                return _StaticQuota(snapshot(10, 20))

            coordinator = AccountPoolCoordinator(
                _FakeAuth(),
                lease_broker,
                quota_factory=quota_factory,
                capsule_factory=lambda _adapter, key, _run_id, _generation, **_kwargs: _FakeCapsule(root / account_fingerprint(key)),
            )
            decision = coordinator.probe(["user::a"], "run", root, 1)
            self.assertIsNotNone(decision.selected)
            self.assertEqual(observed, [True])

    def test_account_lease_precedes_registry_lock_without_deadlock(self):
        with tempfile.TemporaryDirectory(prefix="nightwatch-lock-order-") as temporary:
            root = Path(temporary)
            events: list[str] = []

            class TracedLeaseBroker(AccountLeaseBroker):
                def acquire(self, *args, **kwargs):
                    events.append("account lease")
                    return super().acquire(*args, **kwargs)

            class TracedRegistryLockBroker(AccountRegistryLockBroker):
                def acquire(self, *args, **kwargs):
                    events.append("registry lock")
                    return super().acquire(*args, **kwargs)

            auth = AuthBinary(auth_payload())
            try:
                registry = TracedRegistryLockBroker(root / "control")
                adapter = CodexAuthAdapter(binary=str(auth.path), codex_home=root / "canonical", registry_lock=registry)
                lease_broker = TracedLeaseBroker(root / "leases")
                home = root / "capsule"

                def capsule_factory(current_adapter, _key, _run_id, _generation, **_kwargs):
                    current_adapter.export_accounts(root / "export")
                    home.mkdir(exist_ok=True)
                    return _FakeCapsule(home)

                coordinator = AccountPoolCoordinator(adapter, lease_broker, capsule_factory=capsule_factory)
                with coordinator.session("user::a", "run", root, 1):
                    pass
                self.assertEqual(events[:2], ["account lease", "registry lock"])
            finally:
                auth.close()

    def test_registry_lock_crash_releases_kernel_lock(self):
        with tempfile.TemporaryDirectory(prefix="nightwatch-registry-crash-") as temporary:
            root = Path(temporary)
            code = (
                "import sys, time; "
                "from nightwatch.account_broker import AccountRegistryLockBroker; "
                "lock = AccountRegistryLockBroker(sys.argv[1]).acquire(); "
                "print('owned', flush=True); time.sleep(30)"
            )
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(PRODUCT)
            child = subprocess.Popen([sys.executable, "-c", code, str(root / "control")], env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), "owned")
                broker = AccountRegistryLockBroker(root / "control")
                with self.assertRaises(AccountBusy):
                    broker.acquire(timeout=0.05)
                child.kill()
                child.wait(timeout=5)
                lock = broker.acquire(timeout=0.2)
                lock.release()
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
                if child.stdout is not None:
                    child.stdout.close()
                if child.stderr is not None:
                    child.stderr.close()

    def test_registry_crash_hooks_cover_canonical_import_boundaries(self):
        points = ("BEFORE_REGISTRY_LOCK", "AFTER_REGISTRY_LOCK", "BEFORE_REGISTRY_UNLOCK")
        fake_code = """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
home = Path(os.environ['CODEX_HOME'])
registry = home / 'registry.json'
if sys.argv[1] == 'import':
    value = json.loads(registry.read_text())
    for path in Path(sys.argv[2]).glob('*.auth.json'):
        row = json.loads(path.read_text())
        value['accounts'][row['account_key']] = row
    registry.write_text(json.dumps(value))
"""
        for point in points:
            with self.subTest(point=point):
                with tempfile.TemporaryDirectory(prefix="nightwatch-registry-hook-") as temporary:
                    root = Path(temporary)
                    canonical = root / "canonical"
                    canonical.mkdir(mode=0o700)
                    (canonical / "registry.json").write_text(json.dumps({
                        "accounts": {
                            "user::a": {"account_key": "user::a", "token": "a-old"},
                            "user::b": {"account_key": "user::b", "token": "b-old"},
                        },
                        "active": "user::a",
                    }), encoding="utf-8")
                    source = root / "source"
                    source.mkdir(mode=0o700)
                    (source / "a.auth.json").write_text(json.dumps({"account_key": "user::a", "token": "a-new"}), encoding="utf-8")
                    binary = root / "codex-auth"
                    binary.write_text(fake_code, encoding="utf-8")
                    binary.chmod(0o700)
                    child_code = (
                        "import sys; "
                        "from nightwatch.account_broker import AccountRegistryLockBroker, CodexAuthAdapter; "
                        "CodexAuthAdapter(binary=sys.argv[1], codex_home=sys.argv[2], "
                        "registry_lock=AccountRegistryLockBroker(sys.argv[4])).import_accounts(sys.argv[3])"
                    )
                    environment = dict(os.environ)
                    environment.update({
                        "PYTHONPATH": str(PRODUCT),
                        "NIGHTWATCH_ENABLE_TEST_CRASH_HOOKS": "1",
                        "NIGHTWATCH_TEST_CRASH_POINT": point,
                    })
                    child = subprocess.Popen(
                        [sys.executable, "-c", child_code, str(binary), str(canonical), str(source), str(root / "control")],
                        env=environment,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    try:
                        self.assertEqual(child.wait(timeout=5), -9, point)
                        registry = json.loads((canonical / "registry.json").read_text(encoding="utf-8"))
                        expected = "a-new" if point == "BEFORE_REGISTRY_UNLOCK" else "a-old"
                        self.assertEqual(registry["accounts"]["user::a"]["token"], expected)
                        self.assertEqual(registry["accounts"]["user::b"]["token"], "b-old")
                        broker = AccountRegistryLockBroker(root / "control")
                        lock = broker.acquire(timeout=0.2)
                        lock.release()
                    finally:
                        if child.poll() is None:
                            child.kill()
                            child.wait(timeout=5)
                        if child.stdout is not None:
                            child.stdout.close()
                        if child.stderr is not None:
                            child.stderr.close()

    def test_codex_auth_child_keeps_registry_lock_after_parent_crash(self):
        with tempfile.TemporaryDirectory(prefix="nightwatch-registry-child-crash-") as temporary:
            root = Path(temporary)
            started = root / "started"
            binary = root / "codex-auth"
            binary.write_text(
                """#!/usr/bin/env python3
import json, os, time
from pathlib import Path
Path(os.environ['FAKE_AUTH_STARTED']).write_text(str(os.getpid()))
time.sleep(1.5)
print(json.dumps({'schema_version': 1, 'command': 'list', 'accounts': []}), flush=True)
""",
                encoding="utf-8",
            )
            binary.chmod(0o700)
            canonical = root / "canonical"
            canonical.mkdir(mode=0o700)
            code = (
                "import sys; "
                "from nightwatch.account_broker import CodexAuthAdapter, AccountRegistryLockBroker; "
                "CodexAuthAdapter(binary=sys.argv[1], codex_home=sys.argv[2], "
                "registry_lock=AccountRegistryLockBroker(sys.argv[3])).list_accounts()"
            )
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(PRODUCT)
            environment["FAKE_AUTH_STARTED"] = str(started)
            parent = subprocess.Popen(
                [sys.executable, "-c", code, str(binary), str(canonical), str(root / "control")],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + 2
                while not started.exists() and time.monotonic() < deadline:
                    self.assertIsNone(parent.poll())
                    time.sleep(0.01)
                self.assertTrue(started.exists())
                parent.kill()
                parent.wait(timeout=5)
                broker = AccountRegistryLockBroker(root / "control")
                with self.assertRaises(AccountBusy):
                    broker.acquire(timeout=0.1)
                deadline = time.monotonic() + 3
                while True:
                    try:
                        lock = broker.acquire(timeout=0.1)
                        break
                    except AccountBusy:
                        if time.monotonic() >= deadline:
                            raise
                lock.release()
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.wait(timeout=5)
                if parent.stdout is not None:
                    parent.stdout.close()
                if parent.stderr is not None:
                    parent.stderr.close()

    def test_registry_lock_rejects_symlink_corrupt_and_replaced_roots(self):
        with tempfile.TemporaryDirectory(prefix="nightwatch-registry-safety-") as temporary:
            root = Path(temporary)
            target = root / "target"
            target.mkdir()
            linked = root / "linked"
            linked.symlink_to(target, target_is_directory=True)
            with self.assertRaises(AccountSchemaError):
                AccountRegistryLockBroker(linked)

            broker = AccountRegistryLockBroker(root / "control")
            broker.path.write_text("not-json\n", encoding="utf-8")
            broker.path.chmod(0o600)
            with self.assertRaises(AccountSchemaError):
                broker.acquire(timeout=0.2)

            broker.path.unlink()
            broker.root.rename(root / "old-control")
            (root / "control").mkdir()
            with self.assertRaises(AccountSchemaError):
                broker.acquire(timeout=0.2)

    def test_registry_lock_rejects_replaced_metadata_path(self):
        with tempfile.TemporaryDirectory(prefix="nightwatch-registry-path-replace-") as temporary:
            root = Path(temporary)
            broker = AccountRegistryLockBroker(root / "control")
            lock = broker.acquire(timeout=0.2)
            lock.release()
            original = root / "original-lock"
            broker.path.rename(original)
            broker.path.write_text("{}\n", encoding="utf-8")
            with self.assertRaises(AccountSchemaError):
                broker.acquire(timeout=0.2)
            self.assertTrue(original.exists())

    def test_replacing_held_registry_root_does_not_split_lock_domain(self):
        with tempfile.TemporaryDirectory(prefix="nightwatch-registry-root-replace-") as temporary:
            root = Path(temporary)
            registry_root = root / "control"
            broker = AccountRegistryLockBroker(registry_root)
            lock = broker.acquire(timeout=0.2)
            old_root = root / "old-control"
            try:
                registry_root.rename(old_root)
                registry_root.mkdir(mode=0o700)
                replacement_broker = AccountRegistryLockBroker(registry_root)
                with self.assertRaises(AccountBusy):
                    replacement_broker.acquire(timeout=0.05)
            finally:
                lock.release()

    def test_registry_lock_rejects_symlinked_lock_path(self):
        with tempfile.TemporaryDirectory(prefix="nightwatch-registry-symlink-") as temporary:
            root = Path(temporary)
            broker = AccountRegistryLockBroker(root / "control")
            outside = root / "outside"
            outside.write_text("preserve\n", encoding="utf-8")
            broker.path.symlink_to(outside)
            with self.assertRaises(AccountSchemaError):
                broker.acquire(timeout=0.2)
    def test_capsule_cleans_runtime_tmp_containing_symlinks(self):
        with tempfile.TemporaryDirectory(prefix="nightwatch-capsule-tmp-") as temporary:
            root = Path(temporary)
            binary = root / "codex-auth"
            binary.write_text(
                """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
home = Path(os.environ['CODEX_HOME'])
registry = home / 'registry.json'
value = json.loads(registry.read_text()) if registry.exists() else {'accounts': {'user::a': {'account_key': 'user::a', 'active': True}}, 'active': 'user::a'}
command = sys.argv[1]
if command == 'list':
    print(json.dumps({'schema_version': 1, 'command': 'list', 'accounts': [dict(row, active=key == value.get('active')) for key, row in value['accounts'].items()]}))
elif command == 'switch':
    value['active'] = sys.argv[2]
    registry.write_text(json.dumps(value))
    row = dict(value['accounts'][sys.argv[2]], active=True)
    print(json.dumps({'schema_version': 1, 'command': 'switch', 'switched_to': row}))
elif command == 'remove':
    key = sys.argv[2]
    value['accounts'].pop(key, None)
    registry.write_text(json.dumps(value))
    print(json.dumps({'schema_version': 1, 'command': 'remove', 'removed': [{'account_key': key}]}))
elif command == 'export':
    destination = Path(sys.argv[2])
    destination.mkdir(parents=True, exist_ok=True)
    for key, row in value['accounts'].items():
        (destination / (key.replace('::', '--') + '.auth.json')).write_text(json.dumps(row))
elif command == 'import':
    destination = Path(sys.argv[2])
    files = [destination] if destination.is_file() else list(destination.glob('*.auth.json'))
    for path in files:
        row = json.loads(path.read_text())
        value['accounts'][row['account_key']] = row
    registry.write_text(json.dumps(value))
""",
                encoding="utf-8",
            )
            binary.chmod(0o700)
            canonical = root / "canonical"
            canonical.mkdir(mode=0o700)
            (canonical / "registry.json").write_text(json.dumps({"accounts": {"user::a": {"account_key": "user::a", "active": True}}, "active": "user::a"}))
            adapter = CodexAuthAdapter(binary=str(binary), codex_home=canonical)
            capsule_root = root / "capsules"
            capsule = AccountCapsule.create(adapter, "user::a", "run", 1, capsule_root)
            arg0_dir = capsule.codex_home / "tmp" / "arg0" / "codex-test"
            arg0_dir.mkdir(parents=True, exist_ok=True)
            target = root / "dummy-binary"
            target.write_text("binary", encoding="utf-8")
            (arg0_dir / "apply_patch").symlink_to(target)
            self.assertTrue((arg0_dir / "apply_patch").is_symlink())
            capsule.close()
            self.assertFalse((capsule.codex_home / "tmp").exists())
            self.assertTrue(capsule.codex_home.exists())
            self.assertFalse((capsule.codex_home / "registry.json").exists())

    def test_capsule_symlinked_runtime_tmp_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="nightwatch-capsule-tmp-symlink-") as temporary:
            root = Path(temporary)
            binary = root / "codex-auth"
            binary.write_text(
                """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
home = Path(os.environ['CODEX_HOME'])
registry = home / 'registry.json'
value = json.loads(registry.read_text()) if registry.exists() else {'accounts': {'user::a': {'account_key': 'user::a', 'active': True}}, 'active': 'user::a'}
command = sys.argv[1]
if command == 'list':
    print(json.dumps({'schema_version': 1, 'command': 'list', 'accounts': [dict(row, active=key == value.get('active')) for key, row in value['accounts'].items()]}))
elif command == 'switch':
    value['active'] = sys.argv[2]
    registry.write_text(json.dumps(value))
    row = dict(value['accounts'][sys.argv[2]], active=True)
    print(json.dumps({'schema_version': 1, 'command': 'switch', 'switched_to': row}))
elif command == 'remove':
    key = sys.argv[2]
    value['accounts'].pop(key, None)
    registry.write_text(json.dumps(value))
    print(json.dumps({'schema_version': 1, 'command': 'remove', 'removed': [{'account_key': key}]}))
elif command == 'export':
    destination = Path(sys.argv[2])
    destination.mkdir(parents=True, exist_ok=True)
    for key, row in value['accounts'].items():
        (destination / (key.replace('::', '--') + '.auth.json')).write_text(json.dumps(row))
elif command == 'import':
    destination = Path(sys.argv[2])
    files = [destination] if destination.is_file() else list(destination.glob('*.auth.json'))
    for path in files:
        row = json.loads(path.read_text())
        value['accounts'][row['account_key']] = row
    registry.write_text(json.dumps(value))
""",
                encoding="utf-8",
            )
            binary.chmod(0o700)
            canonical = root / "canonical"
            canonical.mkdir(mode=0o700)
            (canonical / "registry.json").write_text(json.dumps({"accounts": {"user::a": {"account_key": "user::a", "active": True}}, "active": "user::a"}))
            adapter = CodexAuthAdapter(binary=str(binary), codex_home=canonical)
            capsule_root = root / "capsules"
            capsule = AccountCapsule.create(adapter, "user::a", "run", 1, capsule_root)
            outside = root / "outside"
            outside.mkdir(parents=True, exist_ok=True)
            (capsule.codex_home / "tmp").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(AccountSchemaError):
                capsule.close()

    def test_resolve_authorized_accounts_accepts_fingerprint(self):
        fake = AuthBinary(auth_payload())
        try:
            with tempfile.TemporaryDirectory(prefix="nightwatch-resolve-fp-") as temporary:
                root = Path(temporary)
                with patch.dict(os.environ, {"NIGHTWATCH_CODEX_AUTH_BIN": str(fake.path)}):
                    from nightwatch.operations import RunSpec, resolve_authorized_accounts
                    fp_a = account_fingerprint("user::a")
                    spec = RunSpec(
                        root, "goal", None, None, (), None,
                        account_mode="AUTO_POOL",
                        account_selectors=(fp_a,),
                    )
                    resolved = resolve_authorized_accounts(spec, root)
                    self.assertEqual(resolved, ["user::a"])
        finally:
            fake.close()


class DurableThreadStoreTests(unittest.TestCase):
    @staticmethod
    def make_auth_binary(path: Path) -> None:
        code = """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
home = Path(os.environ.get('CODEX_HOME', '.'))
registry = home / 'registry.json'
value = json.loads(registry.read_text()) if registry.exists() else {'accounts': {}, 'active': None}
command = sys.argv[1] if len(sys.argv) > 1 else 'list'
if command == 'list':
    rows = [dict(row, active=key == value.get('active')) for key, row in value['accounts'].items()]
    if '--active' in sys.argv:
        rows = [row for row in rows if row['active']]
    print(json.dumps({'schema_version': 1, 'command': 'list', 'accounts': rows}))
elif command == 'switch':
    value['active'] = sys.argv[2]
    registry.write_text(json.dumps(value))
    print(json.dumps({'schema_version': 1, 'command': 'switch', 'switched_to': {'account_key': sys.argv[2]}}))
elif command == 'remove':
    key = sys.argv[2]
    value['accounts'].pop(key, None)
    if value.get('active') == key:
        value['active'] = next(iter(value['accounts']), None)
    registry.write_text(json.dumps(value))
    print(json.dumps({'schema_version': 1, 'command': 'remove', 'removed': [{'account_key': key}]}))
elif command == 'export':
    destination = Path(sys.argv[2])
    destination.mkdir(parents=True, exist_ok=True)
    for key, row in value['accounts'].items():
        (destination / (key.replace('::', '--') + '.auth.json')).write_text(json.dumps(row))
elif command == 'import':
    for p in Path(sys.argv[2]).glob('*.auth.json'):
        row = json.loads(p.read_text())
        value['accounts'][row['account_key']] = row
    registry.write_text(json.dumps(value))
"""
        path.write_text(code, encoding="utf-8")
        path.chmod(0o700)

    def test_run_codex_home_survives_provider_turn(self):
        with tempfile.TemporaryDirectory(prefix="nightwatch-durable-turn-") as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            state_home = Path(temporary) / "state"
            store = NightwatchStore(root, state_home=state_home)
            store.initialize("run-durable", "goal", str(root), account_mode="AUTO_POOL", authorized_accounts=["user::a"])

            self.assertEqual(store.codex_home, store.directory / "codex-runtime" / "codex-home")
            self.assertTrue(store.codex_home.exists())

            sessions_dir = store.codex_home / "sessions" / "2026" / "09" / "03"
            sessions_dir.mkdir(parents=True, exist_ok=True)
            rollout_file = sessions_dir / "rollout-THREAD-X.jsonl"
            rollout_file.write_text('{"type": "session_meta", "id": "THREAD-X"}\n', encoding="utf-8")

            canonical = Path(temporary) / "canonical"
            canonical.mkdir(mode=0o700)
            (canonical / "registry.json").write_text(json.dumps({
                "accounts": {"user::a": {"account_key": "user::a", "active": True}},
                "active": "user::a",
            }))
            auth_bin = Path(temporary) / "codex-auth"
            self.make_auth_binary(auth_bin)

            adapter = CodexAuthAdapter(binary=str(auth_bin), codex_home=canonical)
            coordinator = AccountPoolCoordinator(adapter, AccountLeaseBroker(Path(temporary) / "leases"), run_codex_home=store.codex_home)
            with coordinator.session("user::a", "run-durable", root, 1) as runtime:
                self.assertEqual(runtime.codex_home, store.codex_home)
                self.assertTrue((runtime.codex_home / "registry.json").exists())

            self.assertTrue(rollout_file.exists())
            self.assertFalse((store.codex_home / "auth.json").exists())
            self.assertFalse((store.codex_home / "registry.json").exists())
            self.assertFalse((store.codex_home / "accounts").exists())

    def test_auth_scrub_does_not_delete_thread_store(self):
        with tempfile.TemporaryDirectory(prefix="nightwatch-scrub-store-") as temporary:
            root = Path(temporary)
            canonical = root / "canonical"
            canonical.mkdir(mode=0o700)
            (canonical / "registry.json").write_text(json.dumps({
                "accounts": {"user::a": {"account_key": "user::a", "active": True}},
                "active": "user::a",
            }))
            auth_bin = root / "codex-auth"
            self.make_auth_binary(auth_bin)
            adapter = CodexAuthAdapter(binary=str(auth_bin), codex_home=canonical)
            capsule_root = root / "capsules"
            capsule = AccountCapsule.create(adapter, "user::a", "run-1", 1, capsule_root)

            sessions_dir = capsule.codex_home / "sessions"
            sessions_dir.mkdir(parents=True, exist_ok=True)
            (sessions_dir / "rollout.jsonl").write_text("rollout data\n")
            (capsule.codex_home / "state_5.sqlite").write_text("sqlite data\n")

            capsule.close()

            self.assertTrue((capsule.codex_home / "sessions" / "rollout.jsonl").exists())
            self.assertTrue((capsule.codex_home / "state_5.sqlite").exists())
            self.assertFalse((capsule.codex_home / "auth.json").exists())
            self.assertFalse((capsule.codex_home / "registry.json").exists())
            self.assertFalse((capsule.codex_home / "accounts").exists())

    def test_auto_pool_same_account_second_turn_preserves_exact_thread(self):
        fake_codex = PRODUCT.parent / "test-artifacts" / "fake-codex" / "fake_codex.py"
        with tempfile.TemporaryDirectory(prefix="nightwatch-same-acct-2turn-") as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            (root / "init.txt").write_text("hello\n")
            subprocess.run(["git", "add", "init.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True)

            state_home = Path(temporary) / "state"
            store = NightwatchStore(root, state_home=state_home)

            verifier_file = Path(temporary) / "verifier_counter.txt"
            verifier_file.write_text("0")
            verify_cmd = f"{sys.executable} -c \"import sys, pathlib; p = pathlib.Path({str(verifier_file)!r}); n = int(p.read_text()); p.write_text(str(n+1)); sys.exit(0 if n >= 1 else 1)\""

            store.initialize(
                "same-acct-2turn",
                "goal requiring two turns",
                str(root),
                verify_commands=[verify_cmd],
                account_mode="AUTO_POOL",
                authorized_accounts=["user::a"],
            )

            canonical = Path(temporary) / "canonical"
            canonical.mkdir(mode=0o700)
            (canonical / "registry.json").write_text(json.dumps({
                "accounts": {"user::a": {"account_key": "user::a", "active": True}},
                "active": "user::a",
            }))

            auth_bin = Path(temporary) / "codex-auth"
            self.make_auth_binary(auth_bin)
            adapter = CodexAuthAdapter(binary=str(auth_bin), codex_home=canonical)

            class StaticQuota:
                def __init__(self, val): self.val = val
                def read(self): return self.val

            quota_val = QuotaSnapshot("live_app_server", "now", QuotaWindow("5h", 0, 300, int(time.time())), QuotaWindow("weekly", 20, 10080, int(time.time())))
            coordinator = AccountPoolCoordinator(
                adapter,
                AccountLeaseBroker(Path(temporary) / "leases"),
                quota_factory=lambda _h, _f, _fd: StaticQuota(quota_val),
                run_codex_home=store.codex_home,
            )

            plan = root / "plan-source.json"
            plan.write_text(json.dumps({"milestones": [{"id": "M1", "title": "step 1"}]}))

            progress = root / "progress-source.json"
            progress.write_text(json.dumps({"milestones": [{"id": "M1", "status": "implemented"}]}))

            with patch.dict(os.environ, {
                "NIGHTWATCH_CODEX_BIN": str(fake_codex),
                "NIGHTWATCH_SKIP_AUTH_CHECK": "1",
                "NIGHTWATCH_QUOTA_BUFFER_SECONDS": "0",
                "FAKE_CODEX_THREAD_ID": "THREAD-EXACT-001",
                "FAKE_CODEX_PLAN_FILE": str(plan),
                "FAKE_CODEX_PROGRESS_FILE": str(progress),
            }, clear=False):
                final = Supervisor(store, account_pool=coordinator).execute(start=True)

            self.assertEqual(final["state"], State.DONE.value)
            self.assertEqual(final["thread_id"], "THREAD-EXACT-001")
            self.assertEqual(final["current_account_key"], "user::a")

            events = store.load_events()
            provider_starts = [e for e in events if e.get("event") == "provider_started"]
            self.assertEqual(len(provider_starts), 2)
            self.assertEqual(provider_starts[0]["state"], State.RUNNING.value)
            self.assertEqual(provider_starts[1]["state"], State.RUNNING.value)

            codex_state = json.loads((root / ".fake-codex-state.json").read_text(encoding="utf-8"))
            self.assertEqual(codex_state["starts"], 1)
            self.assertEqual(codex_state["resumes"], 1)
            self.assertEqual(codex_state["thread_id"], "THREAD-EXACT-001")
            self.assertIsNone(final.get("thread_handoff"))

    def test_cross_account_unsupported_uses_one_controlled_handoff(self):
        fake_codex = PRODUCT.parent / "test-artifacts" / "fake-codex" / "fake_codex.py"
        with tempfile.TemporaryDirectory(prefix="nightwatch-handoff-") as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            (root / "init.txt").write_text("hello\n")
            subprocess.run(["git", "add", "init.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True)

            plan = root / "plan-source.json"
            plan.write_text(json.dumps({"milestones": [{"id": "M1", "title": "m1"}]}))
            progress = root / "progress-source.json"
            progress.write_text(json.dumps({"milestones": [{"id": "M1", "status": "implemented"}]}))

            store = NightwatchStore(root, state_home=Path(temporary) / "state")

            a, b = "user::a", "user::b"
            a_fp = account_fingerprint(a)
            b_fp = account_fingerprint(b)
            canonical = Path(temporary) / "canonical"
            canonical.mkdir(mode=0o700)
            (canonical / "registry.json").write_text(json.dumps({
                "accounts": {
                    a: {"account_key": a, "active": True},
                    b: {"account_key": b, "active": False},
                },
                "active": a,
            }))

            class StaticQuota:
                def __init__(self, val): self.val = val
                def read(self): return self.val

            class Sequence:
                def __init__(self): self.calls = 0
                def __call__(self, _home, fingerprint, _fd):
                    self.calls += 1
                    index = (self.calls - 1) // 2
                    key = a if fingerprint == a_fp else b
                    if index == 0:
                        used = (0, 20) if key == a else (30, 40)
                    elif index == 1:
                        used = (100, 100) if key == a else (30, 40)
                    else:
                        used = (100, 100) if key == a else (0, 20)
                    reset = int(time.time())
                    val = QuotaSnapshot("live_app_server", "now", QuotaWindow("5h", used[0], 300, reset), QuotaWindow("weekly", used[1], 10080, reset))
                    return StaticQuota(val)

            auth_bin = Path(temporary) / "codex-auth"
            self.make_auth_binary(auth_bin)
            adapter = CodexAuthAdapter(binary=str(auth_bin), codex_home=canonical)
            coordinator = AccountPoolCoordinator(
                adapter,
                AccountLeaseBroker(Path(temporary) / "leases"),
                quota_factory=Sequence(),
                run_codex_home=store.codex_home,
            )

            with patch.dict(os.environ, {
                "NIGHTWATCH_CODEX_BIN": str(fake_codex),
                "NIGHTWATCH_SKIP_AUTH_CHECK": "1",
                "NIGHTWATCH_QUOTA_BUFFER_SECONDS": "0",
                "FAKE_CODEX_SCENARIO": "pool",
                "FAKE_CODEX_ACCOUNT_A": a_fp,
                "FAKE_CODEX_ACCOUNT_B": b_fp,
                "FAKE_CODEX_PLAN_FILE": str(plan),
                "FAKE_CODEX_PROGRESS_FILE": str(progress),
                "NIGHTWATCH_CROSS_ACCOUNT_THREAD_MODE": "UNSUPPORTED",
            }, clear=False):
                store.initialize(
                    "handoff-test", "goal", str(root),
                    verify_commands=["test -f fake-implemented.txt", "git diff --check"],
                    account_mode="AUTO_POOL",
                    authorized_accounts=["user::a", "user::b"],
                )
                final = Supervisor(store, account_pool=coordinator).execute(start=True)

            self.assertEqual(final["state"], State.DONE.value)
            self.assertEqual(final["cross_account_thread_mode"], "UNSUPPORTED")
            self.assertIsNotNone(final.get("thread_handoff"))
            self.assertEqual(final["thread_handoff"]["mode"], "CONTROLLED_THREAD_HANDOFF")
            self.assertEqual(final["thread_handoff"]["status"], "captured")
            self.assertEqual(final["thread_handoff"]["from"], a_fp)
            self.assertEqual(final["thread_handoff"]["to"], b_fp)

    def test_cross_account_proven_preserves_exact_thread(self):
        fake_codex = PRODUCT.parent / "test-artifacts" / "fake-codex" / "fake_codex.py"
        with tempfile.TemporaryDirectory(prefix="nightwatch-proven-") as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            (root / "init.txt").write_text("hello\n")
            subprocess.run(["git", "add", "init.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True)

            plan = root / "plan-source.json"
            plan.write_text(json.dumps({"milestones": [{"id": "M1", "title": "m1"}]}))
            progress = root / "progress-source.json"
            progress.write_text(json.dumps({"milestones": [{"id": "M1", "status": "implemented"}]}))

            store = NightwatchStore(root, state_home=Path(temporary) / "state")

            a, b = "user::a", "user::b"
            a_fp = account_fingerprint(a)
            b_fp = account_fingerprint(b)
            canonical = Path(temporary) / "canonical"
            canonical.mkdir(mode=0o700)
            (canonical / "registry.json").write_text(json.dumps({
                "accounts": {
                    a: {"account_key": a, "active": True},
                    b: {"account_key": b, "active": False},
                },
                "active": a,
            }))

            class StaticQuota:
                def __init__(self, val): self.val = val
                def read(self): return self.val

            class Sequence:
                def __init__(self): self.calls = 0
                def __call__(self, _home, fingerprint, _fd):
                    self.calls += 1
                    index = (self.calls - 1) // 2
                    key = a if fingerprint == a_fp else b
                    if index == 0:
                        used = (0, 20) if key == a else (30, 40)
                    elif index == 1:
                        used = (100, 100) if key == a else (30, 40)
                    else:
                        used = (100, 100) if key == a else (0, 20)
                    reset = int(time.time())
                    val = QuotaSnapshot("live_app_server", "now", QuotaWindow("5h", used[0], 300, reset), QuotaWindow("weekly", used[1], 10080, reset))
                    return StaticQuota(val)

            auth_bin = Path(temporary) / "codex-auth"
            self.make_auth_binary(auth_bin)
            adapter = CodexAuthAdapter(binary=str(auth_bin), codex_home=canonical)
            coordinator = AccountPoolCoordinator(
                adapter,
                AccountLeaseBroker(Path(temporary) / "leases"),
                quota_factory=Sequence(),
                run_codex_home=store.codex_home,
            )

            with patch.dict(os.environ, {
                "NIGHTWATCH_CODEX_BIN": str(fake_codex),
                "NIGHTWATCH_SKIP_AUTH_CHECK": "1",
                "NIGHTWATCH_QUOTA_BUFFER_SECONDS": "0",
                "FAKE_CODEX_SCENARIO": "pool",
                "FAKE_CODEX_ACCOUNT_A": a_fp,
                "FAKE_CODEX_ACCOUNT_B": b_fp,
                "FAKE_CODEX_PLAN_FILE": str(plan),
                "FAKE_CODEX_PROGRESS_FILE": str(progress),
                "NIGHTWATCH_CROSS_ACCOUNT_THREAD_MODE": "PROVEN",
            }, clear=False):
                store.initialize(
                    "proven-test", "goal", str(root),
                    verify_commands=["test -f fake-implemented.txt", "git diff --check"],
                    account_mode="AUTO_POOL",
                    authorized_accounts=["user::a", "user::b"],
                )
                final = Supervisor(store, account_pool=coordinator).execute(start=True)

            self.assertEqual(final["state"], State.DONE.value)
            self.assertEqual(final["cross_account_thread_mode"], "PROVEN")
            self.assertIsNone(final.get("thread_handoff"))
            self.assertEqual(final["thread_id"], "POOL-1")
            self.assertEqual(final["current_account_key"], b)

            codex_state = json.loads((root / ".fake-codex-state.json").read_text(encoding="utf-8"))
            self.assertEqual(codex_state["starts"], 1)
            self.assertEqual(codex_state["resumes"], 2)
            self.assertEqual(codex_state["thread_id"], "POOL-1")

    def test_multi_run_thread_store_isolation(self):
        with tempfile.TemporaryDirectory(prefix="nightwatch-iso-") as temporary:
            repo_a = Path(temporary) / "repo_a"
            repo_b = Path(temporary) / "repo_b"
            repo_a.mkdir()
            repo_b.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo_a, check=True)
            subprocess.run(["git", "init", "-q"], cwd=repo_b, check=True)

            state_home = Path(temporary) / "state"
            store_a = NightwatchStore(repo_a, state_home=state_home)
            store_b = NightwatchStore(repo_b, state_home=state_home)

            store_a.initialize("run-a", "goal a", str(repo_a), account_mode="AUTO_POOL")
            store_b.initialize("run-b", "goal b", str(repo_b), account_mode="AUTO_POOL")

            self.assertNotEqual(store_a.codex_home, store_b.codex_home)
            self.assertTrue(store_a.codex_home.is_relative_to(store_a.directory))
            self.assertTrue(store_b.codex_home.is_relative_to(store_b.directory))

            (store_a.codex_home / "sessions").mkdir(parents=True, exist_ok=True)
            (store_a.codex_home / "sessions" / "rollout-a.jsonl").write_text("data a")

            self.assertFalse((store_b.codex_home / "sessions" / "rollout-a.jsonl").exists())

    def test_auto_pool_adoption_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="nightwatch-adopt-rej-") as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)

            from nightwatch.operations import RunSpec, adopt_run
            spec = RunSpec(
                root, "adopt goal", None, None, (), "THREAD-001",
                account_mode="AUTO_POOL",
                account_selectors=("user::a",),
            )
            result = adopt_run(spec)
            self.assertFalse(result.ok)
            self.assertIn("AUTO_POOL adoption of existing interactive threads is not supported", result.message)

            args = cli._parser().parse_args([
                "run", "goal", "--thread", "THREAD-001", "--account-mode", "auto-pool", "--account", "user::a", "--repo", str(root)
            ])
            with self.assertRaises(SystemExit) as ctx:
                cli._run(args)
            self.assertIn("auto-pool adoption", str(ctx.exception))

            adopt_args = cli._parser().parse_args([
                "adopt", "--thread", "THREAD-001", "--account-mode", "auto-pool", "--account", "user::a", "--repo", str(root)
            ])
            with self.assertRaises(SystemExit) as ctx:
                cli._adopt(adopt_args)
            self.assertIn("auto-pool adoption", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
