"""Exercise destructive cleanup only inside isolated temporary project roots."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from curia_assessment import cleanup


class CleanupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="curia_cleanup_test_")
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name).resolve()
        root_patch = patch.object(cleanup, "PROJECT_ROOT", self.project)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.outputs = self.project / "outputs"

    def test_removes_every_generated_child_and_preserves_inputs_and_configuration(self):
        self.outputs.mkdir()
        untouched = {
            self.project / "config.json": '{"min_events": 5}',
            self.project / "config.test.json": '{"case_count": 5}',
            self.project / ".env": "SYNTHETIC_KEY=not-a-real-key\n",
            self.project / "data" / "input.csv": "case,event\n1,A\n",
        }
        for path, text in untouched.items():
            path.parent.mkdir(exist_ok=True)
            path.write_text(text)
        (self.outputs / ".DS_Store").write_bytes(b"generated metadata")
        (self.outputs / "loose.csv").write_text("generated")
        (self.outputs / "run/prepared/nested").mkdir(parents=True)
        (self.outputs / "run/prepared/nested/plan.json").write_text("{}")
        (self.outputs / "archive/older_run/requests").mkdir(parents=True)
        (self.outputs / "archive/older_run/requests/response.json").write_text("{}")

        self.assertEqual(cleanup.clear_outputs(), 4)
        self.assertTrue(self.outputs.is_dir())
        self.assertEqual(list(self.outputs.iterdir()), [])
        for path, expected in untouched.items():
            self.assertEqual(path.read_text(), expected)

    def test_missing_directory_is_created_and_repeated_cleanup_is_empty(self):
        self.assertFalse(self.outputs.exists())
        self.assertEqual(cleanup.clear_outputs(), 0)
        self.assertTrue(self.outputs.is_dir())
        self.assertEqual(cleanup.clear_outputs(), 0)

    def test_unlinks_external_and_broken_links_without_following_targets(self):
        self.outputs.mkdir()
        external = self.project / "external_run"
        external.mkdir()
        (external / "keep.txt").write_text("must survive")
        (external / ".assessment.lock").write_text("external lock")
        (self.outputs / "linked-run").symlink_to(external, target_is_directory=True)
        (self.outputs / "linked-file").symlink_to(external / "keep.txt")
        (self.outputs / "broken-link").symlink_to(self.project / "missing")

        self.assertEqual(cleanup.clear_outputs(), 3)
        self.assertEqual(list(self.outputs.iterdir()), [])
        self.assertEqual((external / "keep.txt").read_text(), "must survive")
        self.assertEqual((external / ".assessment.lock").read_text(), "external lock")

    def test_outputs_directory_link_is_rejected_without_deleting_its_target(self):
        external = self.project / "external"
        external.mkdir()
        keep = external / "keep.txt"
        keep.write_text("must survive")
        self.outputs.symlink_to(external, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "outputs is a symbolic link"):
            cleanup.clear_outputs()
        self.assertTrue(self.outputs.is_symlink())
        self.assertEqual(keep.read_text(), "must survive")

    def test_outputs_file_is_rejected_without_modifying_it(self):
        self.outputs.write_text("unexpected user file")
        with self.assertRaisesRegex(ValueError, "outputs is not a directory"):
            cleanup.clear_outputs()
        self.assertEqual(self.outputs.read_text(), "unexpected user file")

    def test_nested_lock_stops_cleanup_before_any_file_is_removed(self):
        (self.outputs / "first/prepared").mkdir(parents=True)
        (self.outputs / "last/run").mkdir(parents=True)
        (self.outputs / "first/prepared/plan.json").write_text("keep plan")
        (self.outputs / "loose.csv").write_text("keep loose")
        lock = self.outputs / "last/run/.assessment.lock"
        lock.write_text("running")
        before = {path.relative_to(self.outputs): path.read_bytes()
                  for path in self.outputs.rglob("*") if path.is_file()}

        with self.assertRaisesRegex(ValueError, "outputs/last/run/.assessment.lock") as raised:
            cleanup.clear_outputs()
        self.assertNotIn(str(self.project), str(raised.exception))
        after = {path.relative_to(self.outputs): path.read_bytes()
                 for path in self.outputs.rglob("*") if path.is_file()}
        self.assertEqual(after, before)
