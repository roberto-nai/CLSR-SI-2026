"""Check case-level API accounting, response reuse and measurement coverage."""

from __future__ import annotations

import copy
import csv
import tempfile
import unittest
from pathlib import Path

from curia_assessment.metrics import summarise_resource_usage
from curia_assessment.resource_reporting import write_model_resources


class ModelResourceReportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name)

    @staticmethod
    def job(identifier, *, provider="openai", model="pinned-model", phase="A", run=1, provider_id=None):
        return {"job_id": identifier, "provider_id": provider_id or provider, "provider": provider,
                "model": model, "condition": phase, "run": run,
                "rule_ids": [f"R{number:02d}" for number in range(1, 20)]}

    def export(self, jobs, ledger):
        summary = summarise_resource_usage(jobs, ledger)
        before = copy.deepcopy((jobs, ledger, summary))
        path = write_model_resources(self.output, jobs=jobs, ledger=ledger, usage_summary=summary)
        self.assertEqual((jobs, ledger, summary), before)
        self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))
        with path.open(encoding="utf-8-sig", newline="") as stream:
            return list(csv.DictReader(stream))

    def test_retained_imported_usage_counts_once_and_new_transmissions_stay_separate(self):
        jobs = [self.job(f"case_{number}") for number in range(33)]
        ledger = [
            {**job, "attempt": 1, "call_attempted": True, "status": "ok", "elapsed_seconds": 10.0,
             "usage": {"input_tokens": 100, "output_tokens": 30, "total_tokens": 130},
             **({"imported_from": {"study": "prior-cohort", "attempt": 1}} if number < 4 else {})}
            for number, job in enumerate(jobs)
        ]
        row = self.export(jobs, ledger)[0]
        self.assertEqual(row["planned_case_requests"], "33")
        self.assertEqual(row["n_recorded_attempts"], "33")
        self.assertEqual(row["n_api_calls"], "33")
        self.assertEqual(row["n_reused_responses"], "4")
        self.assertEqual(row["n_new_api_calls"], "29")
        self.assertEqual(row["total_tokens"], "4290")
        self.assertEqual(row["api_seconds_total"], "330.0")
        self.assertEqual(row["api_seconds_mean"], "10.0")
        self.assertEqual(row["measured_total_token_attempts"], "33")
        self.assertEqual(row["measured_elapsed_attempts"], "33")

    def test_parallel_request_seconds_are_summed_without_wall_clock_or_cpu_claims(self):
        jobs = [self.job("first"), self.job("second")]
        ledger = [{**job, "attempt": 1, "call_attempted": True, "elapsed_seconds": seconds,
                   "started_utc": "2026-11-22T10:00:00+00:00", "completed_utc": f"2026-11-22T10:00:{int(seconds):02d}+00:00"}
                  for job, seconds in zip(jobs, (10.0, 12.0), strict=True)]
        row = self.export(jobs, ledger)[0]
        self.assertEqual(row["api_seconds_total"], "22.0")
        self.assertEqual(row["api_seconds_mean"], "11.0")
        self.assertFalse(any("cpu" in field or "wall" in field for field in row))

    def test_unknown_and_partial_measurements_expose_coverage_without_imputed_zeroes(self):
        first = self.job("known")
        second = self.job("unknown")
        pending = self.job("pending", phase="B")
        ledger = [
            {**first, "attempt": 1, "call_attempted": True, "usage": {"input_tokens": 10, "output_tokens": 5}, "elapsed_seconds": 0},
            {**second, "attempt": 1, "status": "interrupted", "usage": None, "elapsed_seconds": None},
        ]
        rows = {row["phase"]: row for row in self.export([first, second, pending], ledger)}
        self.assertEqual(rows["A"]["n_recorded_attempts"], "2")
        self.assertEqual(rows["A"]["n_api_calls"], "1")
        self.assertEqual(rows["A"]["unknown_call_attempt_flags"], "1")
        self.assertEqual(rows["A"]["total_tokens"], "15")
        self.assertEqual(rows["A"]["measured_total_token_attempts"], "1")
        self.assertEqual(rows["A"]["api_seconds_total"], "0.0")
        self.assertEqual(rows["A"]["measured_elapsed_attempts"], "1")
        self.assertEqual(rows["B"]["n_api_calls"], "0")
        self.assertEqual(rows["B"]["total_tokens"], "")
        self.assertEqual(rows["B"]["api_seconds_total"], "")
        unknown = self.export([second], [ledger[1]])[0]
        self.assertEqual(unknown["n_api_calls"], "")
        self.assertEqual(unknown["n_new_api_calls"], "")
        self.assertEqual(unknown["total_tokens"], "")
        self.assertEqual(unknown["api_seconds_mean"], "")
        self.assertEqual(unknown["known_call_attempt_flags"], "0")

    def test_two_models_from_one_provider_and_phases_and_runs_remain_distinct(self):
        jobs = [self.job("first", model="model-one", provider_id="deployment-one"),
                self.job("second", model="model-two", provider_id="deployment-two"),
                self.job("phase-b", model="model-one", phase="B", provider_id="deployment-one"),
                self.job("run-two", model="model-one", run=2, provider_id="deployment-one")]
        ledger = [{**job, "attempt": 1, "call_attempted": True,
                   "usage": {"input_tokens": index, "output_tokens": 1}, "elapsed_seconds": index}
                  for index, job in enumerate(jobs, 1)]
        rows = {(row["model"], row["phase"], row["run"]): row for row in self.export(jobs, ledger)}
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[("model-one", "A", "1")]["provider_id"], "deployment-one")
        self.assertEqual(rows[("model-two", "A", "1")]["provider_id"], "deployment-two")
        self.assertEqual(rows[("model-one", "A", "1")]["total_tokens"], "2")
        self.assertEqual(rows[("model-two", "A", "1")]["total_tokens"], "3")
        self.assertEqual(rows[("model-one", "B", "1")]["total_tokens"], "4")
        self.assertEqual(rows[("model-one", "A", "2")]["total_tokens"], "5")

    def test_reasoning_and_cached_tokens_are_not_added_to_reported_totals(self):
        first = self.job("first")
        second = self.job("second", provider="anthropic")
        ledger = [
            {**first, "attempt": 1, "call_attempted": True, "imported_attempt": True,
             "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150,
                       "input_tokens_details": {"cached_tokens": 80}, "output_tokens_details": {"reasoning_tokens": 40}}},
            {**second, "attempt": 1, "call_attempted": True,
             "usage": {"input_tokens": 100, "cache_read_input_tokens": 20, "cache_creation_input_tokens": 30,
                       "output_tokens": 40, "output_tokens_details": {"thinking_tokens": 10}}},
        ]
        rows = {row["provider"]: row for row in self.export([first, second], ledger)}
        self.assertEqual((rows["openai"]["input_tokens"], rows["openai"]["output_tokens"], rows["openai"]["total_tokens"]), ("100", "50", "150"))
        self.assertEqual(rows["openai"]["n_reused_responses"], "1")
        self.assertEqual(rows["openai"]["n_new_api_calls"], "0")
        self.assertEqual((rows["anthropic"]["input_tokens"], rows["anthropic"]["output_tokens"], rows["anthropic"]["total_tokens"]), ("150", "40", "190"))

    def test_failed_and_retried_attempts_retain_resources_and_duplicate_rows_are_rejected(self):
        job = self.job("case")
        ledger = [
            {**job, "attempt": 1, "status": "incomplete", "call_attempted": True,
             "usage": {"input_tokens": 10, "output_tokens": 16000}, "elapsed_seconds": 5},
            {**job, "attempt": 2, "status": "ok", "call_attempted": True,
             "usage": {"input_tokens": 10, "output_tokens": 2000}, "elapsed_seconds": 4},
        ]
        row = self.export([job], ledger)[0]
        self.assertEqual(row["planned_case_requests"], "1")
        self.assertEqual(row["n_api_calls"], "2")
        self.assertEqual(row["total_tokens"], "18020")
        self.assertEqual(row["api_seconds_total"], "9.0")
        repeated = [*ledger, dict(ledger[0])]
        with self.assertRaisesRegex(ValueError, "must not be repeated"):
            write_model_resources(self.output, jobs=[job], ledger=repeated,
                                  usage_summary=summarise_resource_usage([job], repeated))


if __name__ == "__main__":
    unittest.main()
