"""Check explicit output cleanup without touching project files or model APIs."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run_assessment


class DeleteCliTests(unittest.TestCase):
    def _invoke(self, arguments, *, config_error=None, cleanup_error=None):
        events = []
        stdout, stderr = io.StringIO(), io.StringIO()
        manifest = dict.fromkeys((
            "eligible_cases", "sampled_cases", "catalogue_base_rules", "catalogue_textual_variants",
            "selected_pairs", "selected_base_rules", "selected_textual_variants", "planned_assessments",
        ), 1)
        summaries = {"model_summary": []}

        def stage(name, result=None, error=None):
            def execute(*args, **kwargs):
                events.append(name)
                if error is not None:
                    raise error
                return result
            return execute

        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            output = project / "outputs/test"
            with contextlib.ExitStack() as stack:
                stack.enter_context(patch.object(run_assessment.sys, "argv", ["run_assessment.py", *arguments]))
                clear = stack.enter_context(patch.object(
                    run_assessment, "clear_outputs", side_effect=stage("clear", 3, cleanup_error)))
                validate = stack.enter_context(patch.object(
                    run_assessment, "load_config", side_effect=stage("validate", ({
                        "output_dir": "outputs/test", "providers": [{"id": name} for name in
                                                                    ("openai", "anthropic", "mistral")],
                    }, project), config_error)))
                prepare = stack.enter_context(patch.object(
                    run_assessment, "prepare", side_effect=stage("prepare", manifest)))
                run = stack.enter_context(patch.object(
                    run_assessment, "run", side_effect=stage("run", {"processed": 0})))
                analyse = stack.enter_context(patch.object(
                    run_assessment, "analyse", side_effect=stage("analyse", summaries)))
                credentials = stack.enter_context(patch.object(
                    run_assessment, "_load_credentials", side_effect=stage("credentials")))
                stack.enter_context(patch.object(run_assessment, "resolve", return_value=output))
                stack.enter_context(patch.object(
                    run_assessment, "format_model_summary", side_effect=stage("summary", "SUMMARY")))
                writer = stack.enter_context(patch.object(
                    run_assessment, "write_results_report", side_effect=stage("report", output / "results.html")))
                browser = stack.enter_context(patch.object(run_assessment.webbrowser, "open", return_value=True))
                stack.enter_context(contextlib.redirect_stdout(stdout))
                stack.enter_context(contextlib.redirect_stderr(stderr))
                parse_exit = False
                try:
                    status = run_assessment.main()
                except SystemExit as error:
                    status, parse_exit = error.code, True
            return {
                "status": status, "parse_exit": parse_exit,
                "stdout": stdout.getvalue(), "stderr": stderr.getvalue(), "events": events,
                "clear": clear, "validate": validate, "prepare": prepare, "run": run,
                "analyse": analyse, "credentials": credentials, "writer": writer, "browser": browser,
            }

    def test_delete_alone_only_cleans_and_keeps_timing_records(self):
        result = self._invoke(["--delete"])
        self.assertEqual(result["status"], 0)
        self.assertFalse(result["parse_exit"])
        self.assertEqual(result["events"], ["clear"])
        result["clear"].assert_called_once_with()
        for name in ("validate", "prepare", "run", "analyse", "credentials", "writer", "browser"):
            result[name].assert_not_called()
        self.assertIn("Cleared outputs: 3 entries removed.", result["stdout"])
        self.assertEqual(result["stdout"].count("*** PROGRAM START ***"), 1)
        self.assertTrue(result["stdout"].rstrip().endswith("*** PROGRAM END ***"))

    def test_delete_before_or_after_a_worker_command_cleans_once_before_work(self):
        for command in ("prepare", "run", "all"):
            controls = ["--config", "config.test.json"]
            if command in {"run", "all"}:
                controls += ["--max-requests", "2", "--retry-failed"]
            if command == "run":
                controls += ["--provider", "anthropic"]
            if command == "all":
                controls += ["--no-open"]
            for position in ("before", "after", "both"):
                with self.subTest(command=command, position=position):
                    arguments = (["--delete"] if position in {"before", "both"} else [])
                    arguments += [command, *controls]
                    if position in {"after", "both"}:
                        arguments += ["--delete"]
                    result = self._invoke(arguments)
                    self.assertEqual(result["status"], 0)
                    self.assertFalse(result["parse_exit"])
                    result["clear"].assert_called_once_with()
                    self.assertEqual(result["events"][:2], ["validate", "clear"])
                    worker = "prepare" if command == "all" else command
                    self.assertLess(result["events"].index("clear"), result["events"].index(worker))
                    if command == "prepare":
                        result["prepare"].assert_called_once_with("config.test.json")
                        result["run"].assert_not_called()
                    elif command == "run":
                        result["run"].assert_called_once_with("config.test.json", "anthropic", 2, True)
                    else:
                        result["prepare"].assert_called_once_with("config.test.json")
                        result["run"].assert_called_once_with("config.test.json", None, 2, True)
                        result["analyse"].assert_called_once_with("config.test.json")
                        result["browser"].assert_not_called()

    def test_normal_invocation_never_cleans_implicitly(self):
        result = self._invoke(["run"])
        self.assertEqual(result["status"], 0)
        result["clear"].assert_not_called()
        result["run"].assert_called_once_with("config.json", None, None, False)

    def test_invalid_configuration_prevents_cleanup_and_all_workers(self):
        result = self._invoke(["all", "--delete"], config_error=ValueError("Invalid configuration"))
        self.assertEqual(result["status"], 2)
        self.assertEqual(result["events"], ["validate"])
        for name in ("clear", "prepare", "run", "analyse", "credentials", "writer"):
            result[name].assert_not_called()
        self.assertIn("Invalid configuration", result["stderr"])
        self.assertTrue(result["stdout"].rstrip().endswith("*** PROGRAM END ***"))

    def test_cleanup_failure_prevents_execution_and_keeps_closing_banner(self):
        result = self._invoke(["all", "--delete"], cleanup_error=ValueError("An output directory is locked"))
        self.assertEqual(result["status"], 2)
        self.assertEqual(result["events"], ["validate", "clear"])
        for name in ("prepare", "run", "analyse", "credentials", "writer"):
            result[name].assert_not_called()
        self.assertIn("locked", result["stderr"])
        self.assertTrue(result["stdout"].rstrip().endswith("*** PROGRAM END ***"))

    def test_invalid_run_controls_preserve_outputs_before_cleanup(self):
        for arguments in (
            ["all", "--delete", "--max-requests", "0"],
            ["run", "--delete", "--max-requests", "-1"],
            ["run", "--delete", "--provider", "unknown-provider"],
        ):
            with self.subTest(arguments=arguments):
                result = self._invoke(arguments)
                self.assertEqual(result["status"], 2)
                self.assertEqual(result["events"], ["validate"])
                for name in ("clear", "prepare", "run", "analyse", "credentials", "writer"):
                    result[name].assert_not_called()
                self.assertTrue(result["stdout"].rstrip().endswith("*** PROGRAM END ***"))

    def test_analyse_cannot_be_combined_with_delete(self):
        for arguments in (["--delete", "analyse"], ["analyse", "--delete"]):
            with self.subTest(arguments=arguments):
                result = self._invoke(arguments)
                self.assertEqual(result["status"], 2)
                self.assertTrue(result["parse_exit"])
                self.assertEqual(result["events"], [])
                self.assertNotIn("PROGRAM START", result["stdout"])

    def test_missing_command_still_fails_without_delete(self):
        result = self._invoke([])
        self.assertEqual(result["status"], 2)
        self.assertTrue(result["parse_exit"])
        self.assertEqual(result["events"], [])

    def test_help_never_cleans_even_when_delete_was_given(self):
        for arguments in (["--delete", "--help"], ["all", "--delete", "--help"]):
            with self.subTest(arguments=arguments):
                result = self._invoke(arguments)
                self.assertEqual(result["status"], 0)
                self.assertTrue(result["parse_exit"])
                self.assertEqual(result["events"], [])
                self.assertNotIn("PROGRAM START", result["stdout"])


if __name__ == "__main__":
    unittest.main()
