"""Exercise the single-command workflow without network or real credentials."""

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run_assessment
from curia_assessment.paths import display_path


class AllCliTests(unittest.TestCase):
    def _invoke(self, extra_args=(), *, failure_stage=None, failure=None, counts=None):
        events = []
        stdout, stderr = io.StringIO(), io.StringIO()
        manifest = dict.fromkeys((
            "eligible_cases", "sampled_cases", "catalogue_base_rules", "catalogue_textual_variants",
            "selected_pairs", "selected_base_rules", "selected_textual_variants", "planned_assessments",
        ), 3)
        summaries = {"model_summary": [{"provider": "openai", "n_pending_or_not_run": 1}]}
        counts = counts or {
            "processed": 9, "successful": 9, "failed": 0,
            "skipped_existing": 0, "blocked_providers": 0,
        }

        def stage(name, result):
            def execute(*args, **kwargs):
                events.append(name)
                if name == failure_stage:
                    raise failure
                if name == "run":
                    for provider in ("OPENAI", "ANTHROPIC", "MISTRAL"):
                        self.assertEqual(os.environ[f"{provider}_API_KEY"], f"file-{provider.lower()}")
                return result
            return execute

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "outputs"
            report = output / "results.html"
            (root / ".env").write_text(
                "OPENAI_API_KEY=file-openai\n"
                "ANTHROPIC_API_KEY=file-anthropic\n"
                "MISTRAL_API_KEY=file-mistral\n",
                encoding="utf-8",
            )
            with contextlib.ExitStack() as stack:
                stack.enter_context(patch.object(run_assessment, "__file__", str(root / "run_assessment.py")))
                stack.enter_context(patch.object(run_assessment.sys, "argv", ["run_assessment.py", "all", *extra_args]))
                stack.enter_context(patch.dict(os.environ, {
                    f"{name}_API_KEY": f"inherited-{name.lower()}"
                    for name in ("OPENAI", "ANTHROPIC", "MISTRAL")
                }, clear=True))
                prepare = stack.enter_context(patch.object(run_assessment, "prepare", side_effect=stage("prepare", manifest)))
                run = stack.enter_context(patch.object(run_assessment, "run", side_effect=stage("run", counts)))
                analyse = stack.enter_context(patch.object(run_assessment, "analyse", side_effect=stage("analyse", summaries)))
                load_config = stack.enter_context(patch.object(run_assessment, "load_config", return_value=({
                    "output_dir": "outputs", "providers": [{"id": name} for name in ("openai", "anthropic", "mistral")],
                }, root)))
                stack.enter_context(patch.object(run_assessment, "resolve", return_value=output))
                formatter = stack.enter_context(patch.object(run_assessment, "format_model_summary", side_effect=stage("summary", "MODEL SUMMARY")))
                writer = stack.enter_context(patch.object(run_assessment, "write_results_report", side_effect=stage("report", report)))
                browser = stack.enter_context(patch.object(run_assessment.webbrowser, "open", side_effect=stage("open", True)))
                stack.enter_context(contextlib.redirect_stdout(stdout))
                stack.enter_context(contextlib.redirect_stderr(stderr))
                status = run_assessment.main()
            return {
                "status": status, "stdout": stdout.getvalue(), "stderr": stderr.getvalue(),
                "events": events, "prepare": prepare, "run": run, "analyse": analyse,
                "load_config": load_config, "formatter": formatter, "writer": writer,
                "browser": browser, "report": report, "output": output, "summaries": summaries,
            }

    def test_all_prepares_runs_all_providers_and_displays_report_in_order(self):
        result = self._invoke()
        self.assertEqual(result["status"], 0)
        self.assertEqual(result["stderr"], "")
        self.assertEqual(result["events"], ["prepare", "run", "analyse", "summary", "report", "open"])
        result["prepare"].assert_called_once_with("config.json")
        result["run"].assert_called_once_with("config.json", None, None, False)
        result["analyse"].assert_called_once_with("config.json")
        result["formatter"].assert_called_once_with(result["summaries"]["model_summary"])
        result["writer"].assert_called_once_with(result["output"], result["summaries"])
        result["browser"].assert_called_once_with(result["report"].resolve().as_uri())
        self.assertIn("MODEL SUMMARY", result["stdout"])
        self.assertIn(f"Results report: {display_path(result['report'])}", result["stdout"])
        self.assertNotIn(f"Results report: {result['report']}", result["stdout"])
        self.assertEqual(result["stdout"].count("*** PROGRAM START ***"), 1)
        self.assertEqual(result["stdout"].count("*** PROGRAM END ***"), 1)

    def test_all_accepts_short_test_config_and_run_controls_without_opening_browser(self):
        result = self._invoke(("--config", "config.test.json", "--max-requests", "4", "--retry-failed", "--no-open"))
        self.assertEqual(result["status"], 0)
        result["prepare"].assert_called_once_with("config.test.json")
        result["run"].assert_called_once_with("config.test.json", None, 4, True)
        result["analyse"].assert_called_once_with("config.test.json")
        result["browser"].assert_not_called()
        self.assertEqual(result["events"], ["prepare", "run", "analyse", "summary", "report"])

    def test_all_stops_after_failed_preparation_without_model_requests(self):
        result = self._invoke(failure_stage="prepare", failure=ValueError("Invalid frozen inputs"))
        self.assertEqual(result["status"], 2)
        self.assertEqual(result["events"], ["prepare"])
        result["run"].assert_not_called()
        result["writer"].assert_not_called()
        self.assertIn("Invalid frozen inputs", result["stderr"])
        self.assertTrue(result["stdout"].rstrip().endswith("*** PROGRAM END ***"))

    def test_all_preserves_interruption_status_and_closes_timing(self):
        result = self._invoke(failure_stage="run", failure=KeyboardInterrupt())
        self.assertEqual(result["status"], 130)
        self.assertEqual(result["events"], ["prepare", "run"])
        result["analyse"].assert_not_called()
        result["writer"].assert_not_called()
        self.assertIn("Interrupted.", result["stderr"])
        self.assertTrue(result["stdout"].rstrip().endswith("*** PROGRAM END ***"))

    def test_all_reports_partial_results_without_implicit_retry(self):
        result = self._invoke(("--no-open",), counts={
            "processed": 1, "successful": 0, "failed": 1,
            "skipped_existing": 0, "blocked_providers": 1,
        })
        self.assertEqual(result["run"].call_count, 1)
        result["run"].assert_called_once_with("config.json", None, None, False)
        result["analyse"].assert_called_once_with("config.json")
        result["writer"].assert_called_once_with(result["output"], result["summaries"])
        self.assertIn("MODEL SUMMARY", result["stdout"])


if __name__ == "__main__":
    unittest.main()
