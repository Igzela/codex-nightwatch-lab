from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PRODUCT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PRODUCT))
TEST_STATE_HOME = tempfile.mkdtemp(prefix="nightwatch-event-state-")
os.environ["NIGHTWATCH_STATE_HOME"] = TEST_STATE_HOME

from nightwatch.storage import NightwatchStore, StateIntegrityError  # noqa: E402


def fixture() -> tuple[tempfile.TemporaryDirectory, Path]:
    temporary = tempfile.TemporaryDirectory(prefix="nightwatch-events-")
    root = Path(temporary.name)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "nightwatch@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Nightwatch Test"], cwd=root, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "fixture"], cwd=root, check=True)
    return temporary, root


class EventStoreTests(unittest.TestCase):
    def test_event_segment_rotation_preserves_monotonic_sequence(self):
        temporary, root = fixture()
        try:
            store = NightwatchStore(root)
            with patch.dict(os.environ, {"NIGHTWATCH_MAX_SEGMENT_BYTES": "2000"}, clear=False):
                store.initialize("run-rotate", "goal", str(root))
                for i in range(25):
                    store.append_event(f"event_{i}", f"reason_{i}", {"data": "x" * 150})

                manifest = json.loads(store.events_manifest_path.read_text(encoding="utf-8"))
                self.assertGreater(len(manifest["segments"]), 1)

                events = store.load_events()
                self.assertEqual(len(events), 26)  # 1 run_created + 25
                for idx, ev in enumerate(events, 1):
                    self.assertEqual(ev["seq"], idx)
        finally:
            temporary.cleanup()

    def test_event_log_can_exceed_old_one_mb_lifetime_limit(self):
        temporary, root = fixture()
        try:
            store = NightwatchStore(root)
            # Use 200KB segment size to create multiple segments totaling > 1.1 MB
            with patch.dict(os.environ, {"NIGHTWATCH_MAX_SEGMENT_BYTES": "200000"}, clear=False):
                store.initialize("run-large", "goal", str(root))
                payload = "a" * 2000
                total_events = 550  # 550 * ~2100 bytes = ~1.15 MB
                for i in range(total_events):
                    store.append_event("bulk_event", f"event_{i}", {"payload": payload})

                # Verify total size across segments exceeds 1MB
                manifest = json.loads(store.events_manifest_path.read_text(encoding="utf-8"))
                total_bytes = sum(seg["byte_size"] for seg in manifest["segments"])
                self.assertGreater(total_bytes, 1_000_000)
                self.assertGreater(len(manifest["segments"]), 2)

                # load_state must succeed without failing on old 1MB ceiling
                state = store.load_state()
                self.assertEqual(state["run_id"], "run-large")

                # load_events must succeed and return all events sequentially
                all_events = store.load_events()
                self.assertEqual(len(all_events), total_events + 1)
                self.assertEqual(all_events[-1]["seq"], total_events + 1)
        finally:
            temporary.cleanup()

    def test_synthetic_soak_frontier_scaling(self):
        temporary, root = fixture()
        try:
            store = NightwatchStore(root)
            with patch.dict(os.environ, {"NIGHTWATCH_MAX_SEGMENT_BYTES": "50000"}, clear=False):
                store.initialize("run-soak", "goal", str(root))
                payload = "s" * 1000
                for i in range(500):
                    store.append_event("soak_event", f"iter_{i}", {"data": payload})

                manifest = json.loads(store.events_manifest_path.read_text(encoding="utf-8"))
                self.assertGreaterEqual(len(manifest["segments"]), 10)

                read_paths: list[Path] = []
                orig_read = store._read_segment_body

                def tracking_read(path: Path):
                    read_paths.append(path)
                    return orig_read(path)

                store._read_segment_body = tracking_read

                for _ in range(50):
                    s = store.load_state()
                    self.assertEqual(s["run_id"], "run-soak")

                self.assertEqual(len(read_paths), 0)

                store.append_event("final_event", "done")
                self.assertEqual(len(read_paths), 0)
        finally:
            temporary.cleanup()

    def test_event_append_does_not_rescan_entire_history(self):
        temporary, root = fixture()
        try:
            store = NightwatchStore(root)
            with patch.dict(os.environ, {"NIGHTWATCH_MAX_SEGMENT_BYTES": "1500"}, clear=False):
                store.initialize("run-no-rescan", "goal", str(root))
                for i in range(20):
                    store.append_event(f"ev_{i}", "reason", {"pad": "x" * 100})

                manifest = json.loads(store.events_manifest_path.read_text(encoding="utf-8"))
                self.assertGreater(len(manifest["segments"]), 2)

                # Instrument _read_segment_body
                read_paths: list[Path] = []
                orig_read = store._read_segment_body

                def tracking_read(path: Path):
                    read_paths.append(path)
                    return orig_read(path)

                store._read_segment_body = tracking_read

                # Appending one new event must not read historical segment bodies
                store.append_event("single_append", "reason", {"info": "fast"})
                self.assertEqual(len(read_paths), 0)
        finally:
            temporary.cleanup()

    def test_state_load_does_not_materialize_full_timeline(self):
        temporary, root = fixture()
        try:
            store = NightwatchStore(root)
            with patch.dict(os.environ, {"NIGHTWATCH_MAX_SEGMENT_BYTES": "1500"}, clear=False):
                store.initialize("run-state-load", "goal", str(root))
                for i in range(20):
                    store.append_event(f"ev_{i}", "reason", {"pad": "x" * 100})

                manifest = json.loads(store.events_manifest_path.read_text(encoding="utf-8"))
                self.assertGreater(len(manifest["segments"]), 2)

                # Instrument _read_segment_body
                read_paths: list[Path] = []
                orig_read = store._read_segment_body

                def tracking_read(path: Path):
                    read_paths.append(path)
                    return orig_read(path)

                store._read_segment_body = tracking_read

                # load_state should only validate frontier and not read full history bodies
                state = store.load_state()
                self.assertEqual(state["run_id"], "run-state-load")
                self.assertEqual(len(read_paths), 0)
        finally:
            temporary.cleanup()

    def test_event_segment_corruption_fails_closed(self):
        temporary, root = fixture()
        try:
            store = NightwatchStore(root)
            with patch.dict(os.environ, {"NIGHTWATCH_MAX_SEGMENT_BYTES": "1500"}, clear=False):
                store.initialize("run-corrupt", "goal", str(root))
                for i in range(15):
                    store.append_event(f"ev_{i}", "reason", {"pad": "x" * 100})

                manifest = json.loads(store.events_manifest_path.read_text(encoding="utf-8"))
                self.assertGreater(len(manifest["segments"]), 1)

                sealed_name = manifest["segments"][0]["name"]
                sealed_path = store.events_dir / sealed_name

                # Tamper with the content of a sealed segment (same length to test digest)
                content = sealed_path.read_bytes()
                tampered = content.replace(b"ev_0", b"bad0")
                sealed_path.write_bytes(tampered)

                with self.assertRaises(StateIntegrityError):
                    store.load_events()
        finally:
            temporary.cleanup()

    def test_event_segment_deletion_fails_closed(self):
        temporary, root = fixture()
        try:
            store = NightwatchStore(root)
            with patch.dict(os.environ, {"NIGHTWATCH_MAX_SEGMENT_BYTES": "1500"}, clear=False):
                store.initialize("run-delete", "goal", str(root))
                for i in range(15):
                    store.append_event(f"ev_{i}", "reason", {"pad": "x" * 100})

                manifest = json.loads(store.events_manifest_path.read_text(encoding="utf-8"))
                self.assertGreater(len(manifest["segments"]), 1)

                sealed_name = manifest["segments"][0]["name"]
                (store.events_dir / sealed_name).unlink()

                with self.assertRaises(StateIntegrityError):
                    store.load_state()
        finally:
            temporary.cleanup()

    def test_event_segment_reorder_or_chain_break_fails_closed(self):
        temporary, root = fixture()
        try:
            store = NightwatchStore(root)
            with patch.dict(os.environ, {"NIGHTWATCH_MAX_SEGMENT_BYTES": "1500"}, clear=False):
                store.initialize("run-chain-break", "goal", str(root))
                for i in range(15):
                    store.append_event(f"ev_{i}", "reason", {"pad": "x" * 100})

                manifest = json.loads(store.events_manifest_path.read_text(encoding="utf-8"))
                self.assertGreater(len(manifest["segments"]), 1)

                # Corrupt prev_sha256 chain in manifest
                manifest["segments"][1]["prev_sha256"] = "0" * 64
                store.events_manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

                with self.assertRaises(StateIntegrityError):
                    store.load_state()
        finally:
            temporary.cleanup()

    def test_legacy_events_jsonl_remains_readable(self):
        temporary, root = fixture()
        try:
            store = NightwatchStore(root)
            # Create a legacy run that has only events.jsonl
            store.initialize("run-legacy", "goal", str(root))

            # Migrate it back to legacy layout by copying events to events.jsonl and removing events dir
            all_events = store.load_events()
            import shutil
            shutil.rmtree(store.events_dir)

            with store.legacy_events_path.open("w", encoding="utf-8") as handle:
                for ev in all_events:
                    handle.write(json.dumps(ev) + "\n")

            self.assertTrue(store.legacy_events_path.exists())
            self.assertFalse(store.events_manifest_path.exists())

            # Legacy layout remains readable directly
            loaded = store.load_events()
            self.assertEqual(len(loaded), len(all_events))
            state = store.load_state()
            self.assertEqual(state["run_id"], "run-legacy")
        finally:
            temporary.cleanup()

    def test_legacy_event_history_migrates_or_extends_without_loss(self):
        temporary, root = fixture()
        try:
            store = NightwatchStore(root)
            store.initialize("run-legacy-ext", "goal", str(root))

            # Simulate legacy state with events.jsonl only
            import shutil
            shutil.rmtree(store.events_dir)
            legacy_events = [
                {"seq": 1, "ts": "2026-01-01T00:00:00Z", "event": "run_created", "reason": "init", "run_id": "run-legacy-ext", "state": "NEW"},
                {"seq": 2, "ts": "2026-01-01T00:01:00Z", "event": "resumed", "reason": "resume", "run_id": "run-legacy-ext", "state": "RUNNING"},
            ]
            with store.legacy_events_path.open("w", encoding="utf-8") as handle:
                for ev in legacy_events:
                    handle.write(json.dumps(ev) + "\n")

            legacy_content_before = store.legacy_events_path.read_text(encoding="utf-8")

            # Appending an event on the legacy store triggers Pattern A migration
            store.append_event("milestone_verified", "checks passed")

            # Verify legacy events.jsonl was NOT modified
            legacy_content_after = store.legacy_events_path.read_text(encoding="utf-8")
            self.assertEqual(legacy_content_before, legacy_content_after)

            # Manifest was created and tracks events.jsonl as segment 0
            manifest = json.loads(store.events_manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["segments"][0]["name"], "events.jsonl")
            self.assertTrue(manifest["segments"][0]["is_legacy_root"])
            self.assertEqual(manifest["segments"][0]["seq_start"], 1)
            self.assertEqual(manifest["segments"][0]["seq_end"], 2)

            # Active segment contains the new event with seq = 3
            events = store.load_events()
            self.assertEqual(len(events), 3)
            self.assertEqual(events[2]["seq"], 3)
            self.assertEqual(events[2]["event"], "milestone_verified")
        finally:
            temporary.cleanup()

    def test_event_sequence_survives_supervisor_restart(self):
        temporary, root = fixture()
        try:
            store1 = NightwatchStore(root)
            with patch.dict(os.environ, {"NIGHTWATCH_MAX_SEGMENT_BYTES": "2000"}, clear=False):
                store1.initialize("run-restart-seq", "goal", str(root))
                for i in range(10):
                    store1.append_event(f"ev_{i}", "reason")

                # Restart: new store instance
                store2 = NightwatchStore(root)
                state = store2.load_state()
                self.assertEqual(state["run_id"], "run-restart-seq")

                # Append more events on store2
                for i in range(10, 20):
                    store2.append_event(f"ev_{i}", "reason")

                events = store2.load_events()
                self.assertEqual(len(events), 21)
                for idx, ev in enumerate(events, 1):
                    self.assertEqual(ev["seq"], idx)
        finally:
            temporary.cleanup()

    def test_event_frontier_survives_crash_between_append_and_state_update(self):
        temporary, root = fixture()
        try:
            store = NightwatchStore(root)
            store.initialize("run-crash-test", "goal", str(root))
            state = store.load_state()

            # Case A: Partial uncommitted line in active segment (simulating mid-write crash)
            with store.events_path.open("a", encoding="utf-8") as f:
                f.write('{"seq": 2, "event": "incomplete')

            # Next load_state should cleanly truncate the partial uncommitted record
            state_after = store.load_state()
            self.assertEqual(state_after["run_id"], "run-crash-test")

            # Sequence continues monotonically from 2
            store.append_event("resumed_after_crash", "recovered")
            events = store.load_events()
            self.assertEqual(len(events), 2)
            self.assertEqual(events[1]["seq"], 2)
            self.assertEqual(events[1]["event"], "resumed_after_crash")
        finally:
            temporary.cleanup()

    def _setup_store_with_single_segment(self, root: Path, run_id: str) -> tuple[NightwatchStore, int]:
        store = NightwatchStore(root)
        store.initialize(run_id, "goal", str(root))
        for i in range(5):
            store.append_event(f"ev_{i}", "setup", {"pad": "x" * 20})
        manifest = json.loads(store.events_manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["segments"]), 1)
        self.assertEqual(manifest["current_segment"], "segment-000001.jsonl")
        return store, manifest["last_seq"]

    def test_rotation_crash_after_segment_create_recovers(self):
        temporary, root = fixture()
        try:
            store, last_seq = self._setup_store_with_single_segment(root, "run-rot-create")

            orphan = store.events_dir / "segment-000002.jsonl"
            orphan.touch(mode=0o600)
            self.assertTrue(orphan.exists())
            self.assertEqual(orphan.stat().st_size, 0)

            store2 = NightwatchStore(root)
            state = store2.load_state()
            self.assertEqual(state["run_id"], "run-rot-create")
            self.assertFalse(orphan.exists(), "empty orphan segment must be safely removed")

            manifest2 = json.loads(store2.events_manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest2["last_seq"], last_seq)

            store2.append_event("ev_after_crash", "recovered")
            events = store2.load_events()
            self.assertEqual(len(events), last_seq + 1)
            self.assertEqual(events[-1]["seq"], last_seq + 1)
        finally:
            temporary.cleanup()

    def test_rotation_crash_after_partial_orphan_write_recovers(self):
        temporary, root = fixture()
        try:
            store, last_seq = self._setup_store_with_single_segment(root, "run-rot-partial")

            orphan = store.events_dir / "segment-000002.jsonl"
            orphan.write_text('{"event": "incomplete_append", "seq": ' + str(last_seq + 1), encoding="utf-8")

            store2 = NightwatchStore(root)
            state = store2.load_state()
            self.assertEqual(state["run_id"], "run-rot-partial")
            self.assertFalse(orphan.exists(), "partial uncommitted orphan must be safely discarded")

            manifest2 = json.loads(store2.events_manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest2["last_seq"], last_seq)

            store2.append_event("ev_after_partial", "recovered")
            events = store2.load_events()
            self.assertEqual(len(events), last_seq + 1)
            self.assertEqual(events[-1]["seq"], last_seq + 1)
        finally:
            temporary.cleanup()

    def test_rotation_crash_after_complete_next_event_adopts_event(self):
        temporary, root = fixture()
        try:
            store, last_seq = self._setup_store_with_single_segment(root, "run-rot-adopt")

            orphan = store.events_dir / "segment-000002.jsonl"
            event_item = {
                "event": "adopted_event",
                "git_head": None,
                "reason": "rotation_orphan",
                "repo": str(root),
                "run_id": "run-rot-adopt",
                "seq": last_seq + 1,
                "state": "RUNNING",
                "ts": "2026-09-03T12:00:00Z",
            }
            orphan.write_text(json.dumps(event_item) + "\n", encoding="utf-8")

            store2 = NightwatchStore(root)
            state = store2.load_state()
            self.assertEqual(state["run_id"], "run-rot-adopt")
            self.assertTrue(orphan.exists(), "valid complete orphan must be adopted and preserved")

            manifest2 = json.loads(store2.events_manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest2["last_seq"], last_seq + 1)
            self.assertEqual(manifest2["current_segment"], "segment-000002.jsonl")
            self.assertEqual(len(manifest2["segments"]), 2)
            self.assertIsNotNone(manifest2["segments"][0]["sha256"], "previous segment must be sealed")
            self.assertEqual(manifest2["segments"][1]["name"], "segment-000002.jsonl")
            self.assertEqual(manifest2["segments"][1]["prev_sha256"], manifest2["segments"][0]["sha256"])

            events = store2.load_events()
            self.assertEqual(len(events), last_seq + 1)
            self.assertEqual(events[-1]["seq"], last_seq + 1)
            self.assertEqual(events[-1]["event"], "adopted_event")
        finally:
            temporary.cleanup()

    def test_rotation_crash_after_manifest_commit_is_idempotent(self):
        temporary, root = fixture()
        try:
            store, _ = self._setup_store_with_single_segment(root, "run-rot-idempotent")
            # Force rotation by setting a small segment limit
            with patch.dict(os.environ, {"NIGHTWATCH_MAX_SEGMENT_BYTES": "200"}, clear=False):
                store.append_event("trigger_rotation", "setup", {"data": "x" * 100})

            manifest = json.loads(store.events_manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["segments"]), 2)
            last_seq = manifest["last_seq"]

            store2 = NightwatchStore(root)
            state = store2.load_state()
            self.assertEqual(state["run_id"], "run-rot-idempotent")
            events = store2.load_events()
            self.assertEqual(len(events), last_seq)
            manifest2 = json.loads(store2.events_manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest, manifest2)
        finally:
            temporary.cleanup()

    def test_rotation_orphan_wrong_seq_fails_closed(self):
        temporary, root = fixture()
        try:
            store, last_seq = self._setup_store_with_single_segment(root, "run-rot-wrong-seq")

            orphan = store.events_dir / "segment-000002.jsonl"
            bad_event = {"event": "skip", "seq": last_seq + 5, "ts": "2026-09-03T12:00:00Z"}
            orphan.write_text(json.dumps(bad_event) + "\n", encoding="utf-8")

            store2 = NightwatchStore(root)
            with self.assertRaises(StateIntegrityError):
                store2.load_state()
        finally:
            temporary.cleanup()

    def test_rotation_orphan_malformed_complete_record_fails_closed(self):
        temporary, root = fixture()
        try:
            store, _ = self._setup_store_with_single_segment(root, "run-rot-malformed")

            orphan = store.events_dir / "segment-000002.jsonl"
            orphan.write_text('{"event": "broken", "malformed": ]\n', encoding="utf-8")

            store2 = NightwatchStore(root)
            with self.assertRaises(StateIntegrityError):
                store2.load_state()
        finally:
            temporary.cleanup()

    def test_rotation_multiple_orphans_fail_closed(self):
        temporary, root = fixture()
        try:
            store, _ = self._setup_store_with_single_segment(root, "run-rot-multi")

            (store.events_dir / "segment-000002.jsonl").touch()
            (store.events_dir / "segment-000003.jsonl").touch()

            store2 = NightwatchStore(root)
            with self.assertRaises(StateIntegrityError):
                store2.load_state()
        finally:
            temporary.cleanup()

    def test_rotation_orphan_symlink_fails_closed(self):
        temporary, root = fixture()
        try:
            store, _ = self._setup_store_with_single_segment(root, "run-rot-symlink")

            target = store.directory / "dummy.jsonl"
            target.write_text('{"event": "dummy", "seq": 12, "ts": "2026-09-03T12:00:00Z"}\n', encoding="utf-8")
            orphan = store.events_dir / "segment-000002.jsonl"
            os.symlink(target, orphan)

            store2 = NightwatchStore(root)
            with self.assertRaises(StateIntegrityError):
                store2.load_state()
        finally:
            temporary.cleanup()

    def test_rotation_recovery_preserves_global_monotonic_sequence(self):
        temporary, root = fixture()
        try:
            store, last_seq = self._setup_store_with_single_segment(root, "run-rot-mono")

            orphan = store.events_dir / "segment-000002.jsonl"
            ev = {
                "event": "adopted",
                "git_head": None,
                "reason": "rot",
                "repo": str(root),
                "run_id": "run-rot-mono",
                "seq": last_seq + 1,
                "state": "RUNNING",
                "ts": "2026-09-03T12:00:00Z",
            }
            orphan.write_text(json.dumps(ev) + "\n", encoding="utf-8")

            store2 = NightwatchStore(root)
            store2.load_state()

            for i in range(10):
                store2.append_event(f"post_recover_{i}", "continue", {"pad": "y" * 100})

            events = store2.load_events()
            self.assertEqual(len(events), last_seq + 11)
            for idx, item in enumerate(events, 1):
                self.assertEqual(item["seq"], idx)
        finally:
            temporary.cleanup()

    def test_rotation_recovery_survives_new_store_instance(self):
        temporary, root = fixture()
        try:
            store, last_seq = self._setup_store_with_single_segment(root, "run-rot-inst")

            orphan = store.events_dir / "segment-000002.jsonl"
            ev = {
                "event": "adopted",
                "git_head": None,
                "reason": "rot",
                "repo": str(root),
                "run_id": "run-rot-inst",
                "seq": last_seq + 1,
                "state": "RUNNING",
                "ts": "2026-09-03T12:00:00Z",
            }
            orphan.write_text(json.dumps(ev) + "\n", encoding="utf-8")

            store2 = NightwatchStore(root)
            state2 = store2.load_state()
            events2 = store2.load_events()

            store3 = NightwatchStore(root)
            state3 = store3.load_state()
            events3 = store3.load_events()

            self.assertEqual(state2["run_id"], state3["run_id"])
            self.assertEqual(len(events2), len(events3))
            self.assertEqual(events2, events3)
        finally:
            temporary.cleanup()

    def test_rotation_recovery_then_next_append_succeeds(self):
        temporary, root = fixture()
        try:
            store, last_seq = self._setup_store_with_single_segment(root, "run-rot-next")

            orphan = store.events_dir / "segment-000002.jsonl"
            ev = {
                "event": "adopted",
                "git_head": None,
                "reason": "rot",
                "repo": str(root),
                "run_id": "run-rot-next",
                "seq": last_seq + 1,
                "state": "RUNNING",
                "ts": "2026-09-03T12:00:00Z",
            }
            orphan.write_text(json.dumps(ev) + "\n", encoding="utf-8")

            store2 = NightwatchStore(root)
            store2.load_state()

            store2.append_event("next_append", "reason", {"extra": "val"})
            events = store2.load_events()
            self.assertEqual(len(events), last_seq + 2)
            self.assertEqual(events[-1]["seq"], last_seq + 2)
            self.assertEqual(events[-1]["event"], "next_append")
        finally:
            temporary.cleanup()

    def test_rotation_crash_hooks_deterministic_points(self):
        points = ("AFTER_SEGMENT_CREATE", "AFTER_EVENT_APPEND", "AFTER_ROTATION_MANIFEST_COMMIT")
        for point in points:
            with self.subTest(point=point):
                temporary, root = fixture()
                try:
                    store, _ = self._setup_store_with_single_segment(root, "run-rot-hook-" + point)

                    child_code = (
                        "import os, sys; "
                        "from pathlib import Path; "
                        "from nightwatch.storage import NightwatchStore; "
                        "os.environ['NIGHTWATCH_MAX_SEGMENT_BYTES'] = '100'; "
                        "store = NightwatchStore(sys.argv[1]); "
                        "store.append_event('trigger_rotation', 'trigger', {'data': 'x' * 100}); "
                    )
                    environment = dict(os.environ)
                    environment.update({
                        "PYTHONPATH": str(PRODUCT),
                        "NIGHTWATCH_ENABLE_TEST_CRASH_HOOKS": "1",
                        "NIGHTWATCH_TEST_CRASH_POINT": point,
                    })
                    child = subprocess.Popen(
                        [sys.executable, "-c", child_code, str(root)],
                        env=environment,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    try:
                        self.assertEqual(child.wait(timeout=5), -9, point)
                    finally:
                        if child.poll() is None:
                            child.kill()
                            child.wait(timeout=5)
                        if child.stdout is not None:
                            child.stdout.close()
                        if child.stderr is not None:
                            child.stderr.close()

                    store_after = NightwatchStore(root)
                    state = store_after.load_state()
                    self.assertIsNotNone(state)
                    events = store_after.load_events()
                    self.assertGreater(len(events), 0)
                finally:
                    temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
