"""Offline checks for new timestamped runs without modifying frozen studies."""

import contextlib
from datetime import datetime, timezone
import io
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import new_assessment as launcher
import run_assessment as backend_cli
from curia_assessment import pipeline as p


NOW = datetime(2026, 11, 23, 22, 59, 59, tzinfo=timezone.utc)
STAMP = "20261123_235959"


class NewAssessmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="new-assessment-tests-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "Configuration files"
        self.root.mkdir()
        self.root = self.root.resolve()
        self.path = self.root / "config.template.json"
        self.config = {"rules_csv": "data/rules.csv", "event_log_csv": "data/events.csv",
                       "formal_results": "data/reference.zip", "case_texts_dir": "../case-texts",
                       "output_dir": "outputs/new_study_20261122", "assessment_mode": "case",
                       "max_output_tokens": 32000,
                       "providers": [{"id": "test_openai", "provider": "openai", "model": "test-pinned-2026-11-17"}]}
        self.path.write_text(json.dumps(self.config), encoding="utf-8")

    def names(self):
        return self.root / f"{self.path.stem}_{STAMP}.frozen.json", self.root / f"outputs/new_study_{STAMP}"

    def call_main(self, arguments):
        process = Mock()
        process.run.return_value = SimpleNamespace(returncode=0)
        process.call.return_value = 0
        process.Popen.return_value.wait.return_value = 0
        with patch.object(launcher, "subprocess", process), \
                patch.object(launcher.sys, "argv", [str(Path(launcher.__file__)), *arguments]), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            result = launcher.main()
        return result, process, stdout.getvalue()

    def test_timestamp_converts_utc_to_rome_with_midnight_and_summer_time(self):
        examples = [
            (datetime(2026, 11, 23, 23, 59, 59, tzinfo=timezone.utc), "outputs/study_20261124_005959"),
            (datetime(2026, 7, 1, 22, 30, 1, tzinfo=timezone.utc), "outputs/study_20260702_003001"),
            (datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc), "outputs/study_20260101_010000"),
        ]
        for now, expected in examples:
            with self.subTest(now=now):
                self.assertEqual(launcher.timestamped_output_dir("outputs/study", now), expected)
        with self.assertRaises(ValueError):
            launcher.timestamped_output_dir("outputs/study", datetime(2026, 11, 23))

    def test_valid_trailing_date_or_datetime_is_replaced_once_and_invalid_numeric_names_are_preserved(self):
        for value in ("outputs/study_20261122", "outputs/study_20261122_123456", "outputs/study"):
            with self.subTest(value=value):
                result = launcher.timestamped_output_dir(value, NOW)
                self.assertEqual(result, f"outputs/study_{STAMP}")
                self.assertEqual(launcher.timestamped_output_dir(result, NOW), result)
        for suffix in ("20260230", "12345678", "20162026", "20261122_256199"):
            value = "outputs/study_" + suffix
            with self.subTest(value=value):
                self.assertEqual(launcher.timestamped_output_dir(value, NOW), value + "_" + STAMP)
        self.assertEqual(launcher.timestamped_output_dir("outputs/earlier_20261120_study_20261122", NOW),
                         f"outputs/earlier_20261120_study_{STAMP}")

    def test_snapshot_is_normalised_sibling_and_preserves_relative_paths_template_and_source(self):
        before = self.path.read_bytes()
        source_before = p.code_digest()
        config, root = p.load_config(self.path)
        frozen, output = launcher.create_run_config(self.path, NOW)
        self.assertEqual((frozen, output), self.names())
        self.assertEqual(frozen.parent, self.path.parent)
        self.assertTrue(output.is_dir())
        self.assertEqual(list(output.iterdir()), [])
        saved, saved_root = p.load_config(frozen)
        self.assertEqual(root, saved_root)
        self.assertEqual({key: value for key, value in saved.items() if key != "output_dir"},
                         {key: value for key, value in config.items() if key != "output_dir"})
        self.assertEqual(set(json.loads(frozen.read_text())), set(config))
        self.assertEqual(saved["timeout_seconds"], p.DEFAULTS["timeout_seconds"])
        for field in ("rules_csv", "event_log_csv", "formal_results", "case_texts_dir"):
            self.assertEqual(saved[field], config[field])
            self.assertEqual(p.resolve(saved_root, saved[field]), p.resolve(root, config[field]))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(p.code_digest(), source_before)
        self.assertFalse(any(path.name == "_audit" for path in output.rglob("*")))

    def test_output_collision_refuses_without_touching_existing_results_or_snapshot(self):
        frozen, output = launcher.create_run_config(self.path, NOW)
        marker = output / "existing-result.txt"
        marker.write_bytes(b"Existing results must survive")
        before = {path: path.read_bytes() for path in (self.path, frozen, marker)}
        with self.assertRaises((ValueError, FileExistsError)):
            launcher.create_run_config(self.path, NOW)
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_existing_snapshot_collision_preserves_it_and_leaves_no_empty_output_reservation(self):
        frozen, output = self.names()
        frozen.write_bytes(b"Keep this pre-existing snapshot")
        with self.assertRaises((ValueError, FileExistsError)):
            launcher.create_run_config(self.path, NOW)
        self.assertEqual(frozen.read_bytes(), b"Keep this pre-existing snapshot")
        self.assertFalse(output.exists())

    def test_invalid_template_fails_before_creating_snapshot_or_output(self):
        self.config["max_output_tokens"] = True
        self.path.write_text(json.dumps(self.config))
        before = {path.relative_to(self.root) for path in self.root.rglob("*")}
        with self.assertRaises(ValueError):
            launcher.create_run_config(self.path, NOW)
        self.assertEqual({path.relative_to(self.root) for path in self.root.rglob("*")}, before)

    def test_snapshot_write_failure_cleans_only_its_own_empty_output_reservation(self):
        frozen, output = self.names()
        original_open = Path.open
        def failing_open(path, mode="r", *args, **kwargs):
            if path == frozen and mode == "x":
                raise OSError("Synthetic exclusive snapshot creation failure")
            return original_open(path, mode, *args, **kwargs)
        with patch.object(Path, "open", failing_open):
            with self.assertRaisesRegex(OSError, "Synthetic exclusive"):
                launcher.create_run_config(self.path, NOW)
        self.assertFalse(frozen.exists())
        self.assertFalse(output.exists())

    def test_cleanup_preserves_new_output_contents_added_before_a_snapshot_failure(self):
        frozen, output = self.names()
        original_open = Path.open
        def concurrent_data(path, mode="r", *args, **kwargs):
            if path == frozen and mode == "x":
                (output / "concurrent-data.txt").write_bytes(b"Preserve this independent write")
                raise OSError("Synthetic failure after concurrent output write")
            return original_open(path, mode, *args, **kwargs)
        with patch.object(Path, "open", concurrent_data):
            with self.assertRaisesRegex(OSError, "concurrent output write"):
                launcher.create_run_config(self.path, NOW)
        self.assertFalse(frozen.exists())
        self.assertEqual((output / "concurrent-data.txt").read_bytes(), b"Preserve this independent write")

    def test_prepare_delegates_to_absolute_backend_with_current_python_and_prints_fixed_run_resume_paths(self):
        original_create = launcher.create_run_config
        with patch.object(launcher, "create_run_config", side_effect=lambda path: original_create(path, NOW)), \
                contextlib.chdir(self.root):
            calling_directory = Path.cwd()
            result, process, text = self.call_main(["prepare", "--config", str(self.path)])
        self.assertEqual(result, 0)
        called = process.run.call_args or process.call.call_args or process.Popen.call_args
        command, kwargs = called.args[0], called.kwargs
        frozen, _ = self.names()
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(Path(command[1]), Path(backend_cli.__file__).resolve())
        self.assertTrue(Path(command[1]).is_absolute())
        self.assertEqual(command[2], "prepare")
        self.assertEqual(Path(command[command.index("--config") + 1]), frozen)
        self.assertTrue(kwargs.get("cwd") is None or Path(kwargs["cwd"]) == calling_directory)
        self.assertIn(str(frozen), text)
        self.assertIn("run", text)
        self.assertIn("resume", text)

    def test_frozen_prepare_snapshot_is_reused_unchanged_by_native_run_and_resume(self):
        frozen, output = launcher.create_run_config(self.path, NOW)
        before = frozen.read_bytes()
        audit = output / "_audit"
        audit.mkdir()
        (audit / "run_manifest.json").write_text("{}")
        (audit / "resume_status.json").write_text(json.dumps({"providers": {"test_openai": {"remaining": 0}}}))
        for command in ("run", "resume"):
            with self.subTest(command=command), patch.object(backend_cli.sys, "argv", ["run_assessment.py", command, "--config", str(frozen)]), \
                    patch.object(backend_cli, "_load_credentials"), patch.object(backend_cli, "run", return_value={"processed": 0}) as run, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(backend_cli.main(), 0)
                self.assertEqual(Path(run.call_args.args[0]), frozen)
            self.assertEqual(frozen.read_bytes(), before)
            saved, root = p.load_config(frozen)
            self.assertEqual(p.resolve(root, saved["output_dir"]), output)

    def test_all_forwards_only_authorised_flags_and_default_config(self):
        frozen, output = self.names()
        with patch.object(launcher, "create_run_config", return_value=(frozen, output)) as create:
            result, process, _ = self.call_main(["all", "--max-requests", "1", "--no-open"])
        self.assertEqual(result, 0)
        self.assertEqual(Path(create.call_args.args[0]), Path("config.json"))
        called = process.run.call_args or process.call.call_args or process.Popen.call_args
        command = called.args[0]
        self.assertEqual(command[2], "all")
        self.assertEqual(command[command.index("--max-requests") + 1], "1")
        self.assertIn("--no-open", command)

    def test_delete_resume_analyse_and_invalid_limits_are_rejected_before_any_mutation(self):
        for arguments in (["prepare", "--delete"], ["all", "--delete"], ["resume"], ["analyse"],
                          ["all", "--max-requests", "0"], ["all", "--max-requests", "-1"],
                          ["prepare", "--max-requests", "1"]):
            with self.subTest(arguments=arguments), patch.object(launcher, "create_run_config") as create, \
                    patch.object(launcher, "subprocess") as process, patch.object(launcher.sys, "argv", ["new_assessment.py", *arguments]), \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    launcher.main()
                self.assertEqual(raised.exception.code, 2)
                create.assert_not_called()
                self.assertEqual(process.mock_calls, [])


if __name__ == "__main__":
    unittest.main()
