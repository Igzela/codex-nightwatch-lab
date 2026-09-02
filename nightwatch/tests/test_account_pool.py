from __future__ import annotations

import json
import os
import subprocess
import tempfile
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
    AccountSchemaError,
    AccountUnavailable,
    CodexAuthAdapter,
    account_fingerprint,
    earliest_relevant_reset,
    select_best_account,
)
from nightwatch import cli  # noqa: E402
from nightwatch.models import QuotaSnapshot, QuotaWindow  # noqa: E402
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
    def __init__(self, payload: dict, *, sleep: float = 0, stderr: str = "", malformed: bool = False, argv_path: Path | None = None):
        self.temporary = tempfile.TemporaryDirectory(prefix="nightwatch-auth-fake-")
        self.path = Path(self.temporary.name) / "codex-auth"
        argv_capture = f"open({str(argv_path)!r}, 'w').write(json.dumps(sys.argv[1:]))\n" if argv_path else ""
        code = (
            "#!/usr/bin/env python3\n"
            "import json, sys, time\n"
            f"time.sleep({sleep!r})\n"
            f"print({json.dumps(stderr)!r}, file=sys.stderr, end='')\n"
            f"{argv_capture}"
            f"if {malformed!r}: print('not-json'); sys.exit(0)\n"
            f"payload = {payload!r}\n"
            "if sys.argv[1:2] == ['switch']:\n"
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
    print(json.dumps({'schema_version': 1, 'command': 'remove', 'removed': [key]}))
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


if __name__ == "__main__":
    unittest.main()
