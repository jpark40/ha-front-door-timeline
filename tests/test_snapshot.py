"""Behavior checks for duplicate snapshots without a Home Assistant install."""

import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "timeline_snapshot", ROOT / "custom_components/front_door_timeline/snapshot.py"
)
snapshot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(snapshot)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def capture(self, time, data, label="person", date="2026-10-16"):
        pending = self.directory / ".front_door_pending_test.jpg"
        pending.write_bytes(data)
        destination = self.directory / f"front_door_{date}_{time}_{label}.jpg"
        kept = snapshot.finalize_snapshot(pending, destination)
        self.assertFalse(pending.exists())
        return kept, destination

    def test_repeated_content_keeps_original_timestamp(self):
        first, _ = self.capture("10-00-00-000001", b"frame A")
        kept, duplicate = self.capture("10-01-00-000001", b"frame A")
        self.assertEqual(kept, first)
        self.assertFalse(duplicate.exists())
        self.assertEqual(len(list(self.directory.iterdir())), 1)

    def test_same_size_different_image_is_saved(self):
        first, _ = self.capture("10-00-00", b"frame A")
        kept, second = self.capture("10-01-00", b"frame B")
        self.assertEqual(kept, second)
        self.assertEqual(first.read_bytes(), b"frame A")
        self.assertEqual(second.read_bytes(), b"frame B")

    def test_distinct_event_labels_are_preserved(self):
        person, _ = self.capture("10-00-00", b"frame A", "person")
        package, _ = self.capture("10-01-00", b"frame A", "package_delivered")
        taken, _ = self.capture("10-02-00", b"frame A", "package_taken")
        self.assertEqual(len({person, package, taken}), 3)
        self.assertTrue(all(p.exists() for p in (person, package, taken)))

    def test_repeat_works_across_restart_and_day_rollover(self):
        # Seed a file directly; no in-memory capture state is available.
        prior = self.directory / "front_door_2026-10-15_23-59-59_person.jpg"
        prior.write_bytes(b"frame A")
        kept, duplicate = self.capture("00-00-10", b"frame A")
        self.assertEqual(kept, prior)
        self.assertFalse(duplicate.exists())

    def test_compare_latest_for_label_even_with_other_events_between(self):
        first, _ = self.capture("10-00-00", b"frame A")
        self.capture("10-01-00", b"frame B", "package_delivered")
        kept, duplicate = self.capture("10-02-00", b"frame A")
        self.assertEqual(kept, first)
        self.assertFalse(duplicate.exists())

    def test_return_to_an_earlier_frame_after_a_changed_frame_is_saved(self):
        self.capture("10-00-00", b"frame A")
        self.capture("10-01-00", b"frame B")
        kept, third = self.capture("10-02-00", b"frame A")
        self.assertEqual(kept, third)

    def test_fraction_sorting_supports_old_filename_formats(self):
        self.capture("10-00-00", b"old frame")
        latest, _ = self.capture("10-00-00-123456", b"current frame")
        kept, duplicate = self.capture("10-00-00-123457", b"current frame")
        self.assertEqual(kept, latest)
        self.assertFalse(duplicate.exists())

    def test_pending_and_unrelated_files_are_ignored(self):
        unrelated = self.directory / "family-photo.jpg"
        other_pending = self.directory / ".front_door_pending_other.jpg"
        unrelated.write_bytes(b"frame A")
        other_pending.write_bytes(b"frame A")
        kept, destination = self.capture("10-00-00", b"frame A")
        self.assertEqual(kept, destination)
        self.assertEqual(unrelated.read_bytes(), b"frame A")
        self.assertEqual(other_pending.read_bytes(), b"frame A")
        self.assertIsNone(snapshot.SNAPSHOT_RE.fullmatch(other_pending.name))

    def test_empty_or_missing_image_is_an_error_without_publishing(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                pending = self.directory / ".front_door_pending_test.jpg"
                if existing:
                    pending.touch()
                destination = self.directory / "front_door_2026-10-16_10-00-00_person.jpg"
                with self.assertRaisesRegex(ValueError, "no image was written"):
                    snapshot.finalize_snapshot(pending, destination)
                self.assertFalse(destination.exists())
                self.assertFalse(pending.exists())

    def test_existing_snapshot_is_never_overwritten(self):
        original, _ = self.capture("10-00-00", b"frame A")
        with self.assertRaises(FileExistsError):
            self.capture("10-00-00", b"frame B")
        self.assertEqual(original.read_bytes(), b"frame A")


if __name__ == "__main__":
    unittest.main()
