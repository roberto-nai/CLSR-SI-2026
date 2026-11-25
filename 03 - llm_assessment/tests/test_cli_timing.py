"""Check terminal timing records without model requests or real credentials."""

import contextlib
from datetime import datetime
import io
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import run_assessment


class CliTimingTests(unittest.TestCase):
    def _invoke(self, command, result=None, error=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        zone = ZoneInfo("Europe/Rome")
        started = datetime(2026, 11, 19, 12, 30, tzinfo=zone)
        ended = datetime(2026, 11, 20, 13, 37, 59, tzinfo=zone)

        def work(*args):
            print("WORK")
            if error is not None:
                raise error
            return result

        with patch.object(run_assessment.sys, "argv", ["run_assessment.py", command]), \
             patch.object(run_assessment, "datetime") as wall_clock, \
             patch.object(run_assessment, "monotonic", side_effect=[1000, 1000 + 25 * 3600 + 7 * 60 + 59]), \
             patch.object(run_assessment, command, side_effect=work), \
             patch("dotenv.load_dotenv"), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            wall_clock.now.side_effect = [started, ended]
            status = run_assessment.main()
        return status, stdout.getvalue(), stderr.getvalue()

    def _assert_timing(self, output):
        expected = [
            "*** PROGRAM START ***",
            "Start: 2026-11-19 12:30:00 CET",
            "WORK",
            "End: 2026-11-20 13:37:59 CET",
            "Duration: 25 h 7 min",
            "*** PROGRAM END ***",
        ]
        positions = [output.index(line) for line in expected]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(output.count("*** PROGRAM START ***"), 1)
        self.assertEqual(output.count("*** PROGRAM END ***"), 1)
        self.assertTrue(output.rstrip().endswith("*** PROGRAM END ***"))

    def test_successful_commands_report_start_end_and_duration_beyond_one_day(self):
        manifest = dict.fromkeys((
            "eligible_cases", "sampled_cases", "catalogue_base_rules", "catalogue_textual_variants",
            "selected_pairs", "selected_base_rules", "selected_textual_variants", "planned_assessments",
        ), 1)
        for command, result in (
            ("prepare", manifest),
            ("run", {"processed": 1}),
            ("analyse", {"model_summary": []}),
        ):
            with self.subTest(command=command):
                status, output, errors = self._invoke(command, result=result)
                self.assertEqual(status, 0)
                self.assertEqual(errors, "")
                self._assert_timing(output)

    def test_errors_and_interrupts_report_end_without_changing_exit_status(self):
        for error, expected_status, expected_message in (
            (ValueError("Synthetic configuration failure"), 2, "Error: Synthetic configuration failure"),
            (KeyboardInterrupt(), 130, "Interrupted."),
        ):
            with self.subTest(error=type(error).__name__):
                status, output, errors = self._invoke("run", error=error)
                self.assertEqual(status, expected_status)
                self.assertIn(expected_message, errors)
                self._assert_timing(output)

    def test_help_does_not_emit_program_timing_records(self):
        output = io.StringIO()
        with patch.object(run_assessment.sys, "argv", ["run_assessment.py", "--help"]), \
             contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as raised:
                run_assessment.main()
        self.assertEqual(raised.exception.code, 0)
        self.assertNotIn("PROGRAM START", output.getvalue())
        self.assertNotIn("PROGRAM END", output.getvalue())


if __name__ == "__main__":
    unittest.main()
