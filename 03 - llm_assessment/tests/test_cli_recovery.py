"""Check explicit recovery ordering and reject incompatible modes before calls."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run_assessment


class RecoveryCliTests(unittest.TestCase):
    def invoke(self, command="all", mode="case", conditions=None):
        events = []
        config = {"assessment_mode": mode, "conditions": conditions or ["A"],
                  "providers": [{"id": "test"}], "output_dir": "results"}
        manifest = dict.fromkeys(("eligible_cases", "sampled_cases", "catalogue_base_rules",
                                  "catalogue_textual_variants", "selected_pairs", "selected_base_rules",
                                  "selected_textual_variants", "planned_assessments"), 1)
        with tempfile.TemporaryDirectory() as temporary, contextlib.ExitStack() as stack:
            root = Path(temporary)
            args = ["run_assessment.py", command, "--recover-incomplete"]
            if command == "all":
                args.append("--no-open")
            stack.enter_context(patch("sys.argv", args))
            stack.enter_context(patch.object(run_assessment, "load_config", return_value=(config, root)))
            stack.enter_context(patch.object(run_assessment, "prepare", return_value=manifest))
            credentials = stack.enter_context(patch.object(run_assessment, "_load_credentials"))
            run = stack.enter_context(patch.object(run_assessment, "run", side_effect=lambda *a: events.append("run") or {}))
            recovery = stack.enter_context(patch("curia_assessment.case_recovery.run_recovery",
                side_effect=lambda *a: events.append("recovery") or {"processed": 1}))
            stack.enter_context(patch.object(run_assessment, "analyse",
                side_effect=lambda *a: events.append("analysis") or {"model_summary": []}))
            stack.enter_context(patch.object(run_assessment, "write_results_report", return_value=root / "results.html"))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            code = run_assessment.main()
            return code, events, run.call_count, recovery.call_count, credentials.call_count

    def test_all_recovers_after_baseline_before_final_analysis(self):
        self.assertEqual(self.invoke(), (0, ["run", "recovery", "analysis"], 1, 1, 1))

    def test_run_requires_explicit_flag_and_calls_recovery_once(self):
        self.assertEqual(self.invoke(command="run"), (0, ["run", "recovery"], 1, 1, 1))

    def test_incompatible_mode_or_phase_is_rejected_before_credentials_and_calls(self):
        for mode, phases in (("rule", ["A"]), ("case", ["A", "B"])):
            with self.subTest(mode=mode, phases=phases):
                self.assertEqual(self.invoke(mode=mode, conditions=phases), (2, [], 0, 0, 0))


if __name__ == "__main__":
    unittest.main()
