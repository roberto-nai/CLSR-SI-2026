"""Check project-relative display paths without changing internal file locations."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from curia_assessment import paths


class DisplayPathTests(unittest.TestCase):
    def test_project_paths_are_relative_even_from_another_working_directory(self):
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            elsewhere = Path(temporary).resolve()
            project = elsewhere / "project with spaces"
            with patch.object(paths, "PROJECT_ROOT", project):
                try:
                    os.chdir(elsewhere)
                    self.assertEqual(paths.display_path(project / "outputs/test/results.html"),
                                     Path("outputs/test/results.html"))
                    self.assertEqual(paths.display_path(project), Path("."))
                finally:
                    os.chdir(original_cwd)

    def test_external_paths_use_parent_components_instead_of_absolute_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            with patch.object(paths, "PROJECT_ROOT", parent / "project"):
                self.assertEqual(paths.display_path(parent / "case texts/input.txt"),
                                 Path("../case texts/input.txt"))

    def test_filesystem_errors_keep_both_filenames_relative(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            with patch.object(paths, "PROJECT_ROOT", project):
                missing = FileNotFoundError(2, "No such file", str(project / "data/missing.csv"))
                message = paths.display_error(missing)
                self.assertIn("data/missing.csv", message)
                self.assertNotIn(str(project), message)
                rename = OSError(18, "Cannot move file", str(project / "source.txt"),
                                 None, str(project / "destination.txt"))
                message = paths.display_error(rename)
                self.assertIn("'source.txt' -> 'destination.txt'", message)
                self.assertNotIn(str(project), message)
        self.assertEqual(paths.display_error(ValueError("Invalid configuration")), "Invalid configuration")
