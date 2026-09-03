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


if __name__ == "__main__":
    unittest.main()
