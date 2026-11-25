"""Exercise two-phase, case-batched execution and expert exports without APIs."""

from __future__ import annotations

import contextlib
import csv
import io
import json
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import test_pipeline as fixture_module
from curia_assessment import pipeline


class CasePipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixture_module.PipelineTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.output = self.fixture.output
        self.audit = self.output / "_audit"
        self.config_path = self.fixture.config_path
        self.config = self.fixture.config
        self.config.update(assessment_mode="case", conditions=["A", "B"], request_workers=1)
        for case_id in ("C-1/20", "C-2/20"):
            self.fixture._document_path(case_id).write_text(
                f"CASE_DOCUMENT_SENTINEL for {case_id}. The recorded case document describes B.", encoding="utf-8",
            )
        self.fixture._write_config()

    @staticmethod
    def response(*, status="ok", http_status=200) -> dict:
        return {
            "status": status, "attempted": True, "http_status": http_status,
            "text": json.dumps({"assessments": [
                {"rule_id": rule_id, "outcome": "compliance", "evidence_refs": ["REQUIREMENT", "DECLARE", "E0001"],
                 "justification": "MODEL_RESPONSE_SENTINEL: the requirement and supplied evidence support compliance."}
                for rule_id in ("R01", "R02")
            ]}) if status == "ok" else "",
            "model_returned": "test-pinned-2026-11-17",
            "error": None if status == "ok" else {"kind": "provider_error", "detail": "Synthetic authentication failure."},
            "raw_response": {"fixture": True},
            "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
            "elapsed_seconds": 0.25,
        }

    @staticmethod
    def csv_rows(path: Path) -> list[dict[str, str]]:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            return list(csv.DictReader(stream))

    def run_pipeline(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return pipeline.run(self.config_path, **kwargs)

    def jobs(self):
        return pipeline.read_jsonl(self.audit / "prepared/planned_assessments.jsonl")

    def test_preparation_plans_case_requests_and_keeps_public_results_readable(self) -> None:
        with patch.object(pipeline, "send_request") as send:
            manifest = pipeline.prepare(self.config_path)
        send.assert_not_called()
        self.assertEqual(manifest["sampled_cases"], 2)
        self.assertEqual(manifest["selected_pairs"], 4)
        self.assertEqual(manifest["planned_assessments"], 4)
        self.assertEqual(manifest["planned_rule_assessments"], 8)
        jobs = self.jobs()
        self.assertEqual(len(jobs), 4)
        self.assertEqual({job["condition"] for job in jobs}, {"A", "B"})
        self.assertTrue(all(set(job["rule_ids"]) == {"R01", "R02"} for job in jobs))
        self.assertEqual({job["case_id"] for job in jobs if job["condition"] == "A"},
                         {job["case_id"] for job in jobs if job["condition"] == "B"})
        for name in ("case_results.csv", "rule_assessments.csv", "case_extraction_report.txt", "results.html"):
            self.assertTrue((self.output / name).is_file(), name)
        stats = pipeline.read_json(self.output / "event_log_statistics.json")
        self.assertEqual((stats["n_cases"], stats["n_events"], stats["n_variants"]), (2, 4, 2))
        self.assertEqual(stats["mean_case_duration_days"], 1)
        self.assertEqual((stats["n_documents"], stats["n_document_files"]), (2, 2))
        self.assertEqual(stats["mean_documents_per_case"], 1)
        html = (self.output / "results.html").read_text(encoding="utf-8")
        self.assertIn("Event-log characteristics", html)
        self.assertIn("event_log_statistics.csv", html)
        self.assertTrue((self.audit / "run_manifest.json").is_file())
        self.assertFalse((self.output / "run_manifest.json").exists())
        self.assertFalse((self.output / "prepared").exists())
        cases = self.csv_rows(self.output / "case_results.csv")
        rules = self.csv_rows(self.output / "rule_assessments.csv")
        self.assertEqual(len(cases), 4)
        self.assertEqual(len(rules), 8)
        self.assertNotIn("source", rules[0])
        self.assertNotIn("legal_source", rules[0])
        self.assertIn("declare", rules[0])
        prepared = pipeline.read_jsonl(self.audit / "prepared/pairs.jsonl")
        selected_rules = self.csv_rows(self.audit / "prepared/selected_rules.csv")
        for row in [*prepared, *selected_rules]:
            self.assertFalse({"source", "legal_source", "source_review_id"}.intersection(row))
            self.assertIn("requirement_review_id", row)
        self.assertEqual({row["status"] for row in cases}, {"pending"})
        self.assertEqual({row["violation"] for row in cases}, {"undetermined"})
        for number in (1, 2):
            expert = self.csv_rows(self.output / f"expert_review/expert_{number}.csv")
            self.assertEqual(len(expert), 8)
            fields = list(expert[0])
            self.assertIn("evidence_support", fields)
            self.assertIn("declare", fields)
            self.assertFalse({"source", "legal_source", "source_support"}.intersection(fields))
            self.assertEqual(fields[fields.index("run") + 1], "model")
            self.assertEqual({row["model"] for row in expert}, {"test-pinned-2026-11-17"})
            self.assertFalse({"provider", "formal_outcome", "formal_satisfied", "variant_id",
                              "response_sha256", "annotation_response_sha256"}.intersection(fields))
            ordering = [(int(row["case_year"]), row["case_id"]) for row in expert]
            self.assertEqual(ordering, sorted(ordering))
            self.assertEqual({row["phase"] for row in expert}, {"1", "2"})
            self.assertTrue(all(row["expert_outcome"] == row["judgement_correct"] == "" for row in expert))
            for row in expert:
                dossier = self.output / "expert_review" / row["dossier_path"]
                self.assertTrue(dossier.is_file())
                original = next(rule for rule in selected_rules if rule["base_rule_id"] == row["rule_id"])
                self.assertEqual(row["declare"], original["declare"])
                self.assertIn(original["declare"], dossier.read_text())

    def test_run_batches_rules_separates_phase_evidence_and_resumes_without_calls(self) -> None:
        manifest = pipeline.prepare(self.config_path)
        with patch.object(pipeline, "send_request", return_value=self.response()) as send:
            first = self.run_pipeline()
        self.assertEqual(send.call_count, 4)
        self.assertEqual(first["successful"], 4)
        self.assertEqual(first["failed"], 0)
        jobs = self.jobs()
        prompts = []
        with (self.root / "rules.csv").open(encoding="utf-8", newline="") as stream:
            literal_formulas = {row["ID"]: row["DECLARE"] for row in csv.DictReader(stream, delimiter=";")}
        for job in jobs:
            archived = pipeline.read_json(self.audit / "requests" / job["job_id"] / "attempt_001/request.json")
            payload = archived["payload"]
            user = payload["input"][0]["content"][0]["text"]
            system = payload["instructions"]
            body = json.loads(user.split("EVIDENCE DATA BEGIN\n", 1)[1].split("\nEVIDENCE DATA END", 1)[0])
            self.assertEqual(body["case"]["case_id"], job["case_id"])
            self.assertEqual({row["rule_id"] for row in body["rules"]}, {"R01", "R02"})
            for row in body["rules"]:
                self.assertEqual(set(row), {"rule_id", "requirement", "declare"})
                self.assertEqual(row["declare"], literal_formulas[row["rule_id"]])
                self.assertIsInstance(row["requirement"], str)
            self.assertIn("REQUIREMENT", user)
            self.assertIn("DECLARE", user)
            self.assertNotIn("legal_source", user)
            self.assertNotIn("Article 1:", user)
            self.assertNotIn("Article 2:", user)
            self.assertNotIn("Article 3:", user)
            if job["condition"] == "A":
                self.assertNotIn("CASE_DOCUMENT_SENTINEL", user)
                self.assertNotIn("additional_passages", body)
            else:
                self.assertIn("CASE_DOCUMENT_SENTINEL", user)
                self.assertNotIn("additional_passages", body)
                documents = json.loads(user.split("ADDITIONAL DOCUMENT DATA BEGIN\n", 1)[1]
                                       .split("\nADDITIONAL DOCUMENT DATA END", 1)[0])
                self.assertTrue(documents["additional_passages"])
                self.assertIn("D0001", documents["allowed_evidence_identifiers"])
                baseline_job = next(row for row in jobs if row["condition"] == "A" and row["case_id"] == job["case_id"])
                baseline_request = pipeline.read_json(self.audit / "requests" / baseline_job["job_id"] / "attempt_001/request.json")
                baseline_user = baseline_request["payload"]["input"][0]["content"][0]["text"]
                self.assertTrue(user.startswith(baseline_user))
                self.assertEqual(system, baseline_request["payload"]["instructions"])
            for hidden in ("MODEL_RESPONSE_SENTINEL", "formal_satisfied", "formal_outcome", "test_openai",
                           "test-pinned-2026-11-17"):
                self.assertNotIn(hidden, system + user)
            prompts.append(user)
        self.assertEqual(len(prompts), manifest["planned_assessments"])
        self.assertEqual(len(list((self.audit / "requests").glob("*/attempt_001/response.json"))), 4)
        self.assertFalse((self.output / "requests").exists())
        cases = self.csv_rows(self.output / "case_results.csv")
        rules = self.csv_rows(self.output / "rule_assessments.csv")
        self.assertEqual({row["status"] for row in cases}, {"completed"})
        self.assertEqual({row["violation"] for row in cases}, {"no"})
        self.assertEqual({row["n_completed"] for row in cases}, {"2"})
        self.assertEqual({row["outcome"] for row in rules}, {"compliance"})
        self.assertEqual(len(rules), 8)
        self.assertTrue((self.output / "case_comparison.csv").is_file())
        self.assertEqual(len(self.csv_rows(self.output / "case_comparison.csv")), 2)
        for number in (1, 2):
            expert = self.csv_rows(self.output / f"expert_review/expert_{number}.csv")
            self.assertEqual({row["outcome"] for row in expert}, {"compliance"})
            self.assertEqual({row["model"] for row in expert}, {"test-pinned-2026-11-17"})
            ordering = [(int(row["case_year"]), row["case_id"]) for row in expert]
            self.assertEqual(ordering, sorted(ordering))
            self.assertEqual({row["phase"] for row in expert}, {"1", "2"})
        summary = pipeline.analyse(self.config_path)["model_summary"]
        self.assertEqual(len(summary), 2)
        self.assertEqual({row["n_valid_outputs"] for row in summary}, {4})
        with patch.object(pipeline, "send_request") as repeated:
            resumed = self.run_pipeline()
        repeated.assert_not_called()
        self.assertEqual(resumed["processed"], 0)
        self.assertEqual(resumed["skipped_existing"], 4)

    def test_changed_used_document_prevents_requests_and_identifies_input_change(self) -> None:
        pipeline.prepare(self.config_path)
        self.fixture._document_path("C-1/20").write_text("Changed frozen document text.", encoding="utf-8")
        with patch.object(pipeline, "send_request") as send:
            with self.assertRaisesRegex(ValueError, "input files: phase_2_documents"):
                self.run_pipeline()
        send.assert_not_called()

    def test_request_limit_resumes_only_remaining_case_batches(self) -> None:
        self.config.update(request_workers=2, requests_per_provider=2)
        self.fixture._write_config()
        with patch.object(pipeline, "send_request", return_value=self.response()) as send:
            first = self.run_pipeline(max_requests=1)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(first["processed"], 1)
        self.assertEqual(sum(row["status"] == "ok" for row in self.csv_rows(self.output / "rule_assessments.csv")), 2)
        with patch.object(pipeline, "send_request", return_value=self.response()) as send:
            resumed = self.run_pipeline()
        self.assertEqual(send.call_count, 3)
        self.assertEqual(resumed["successful"], 3)
        self.assertEqual(resumed["skipped_existing"], 1)
        self.assertEqual({row["status"] for row in self.csv_rows(self.output / "rule_assessments.csv")}, {"ok"})

    def test_provider_streams_overlap_without_duplicating_case_phase_jobs(self) -> None:
        self.config.update(request_workers=2, requests_per_provider=2)
        self.fixture._write_config()
        barrier = threading.Barrier(2)
        lock = threading.Lock()
        active = maximum = 0
        observed = []

        def delayed_response(request, **kwargs):
            nonlocal active, maximum
            user = request["payload"]["input"][0]["content"][0]["text"]
            body = json.loads(user.split("EVIDENCE DATA BEGIN\n", 1)[1].split("\nEVIDENCE DATA END", 1)[0])
            identifier = (body["case"]["case_id"], "B" if "\nADDITIONAL DOCUMENT DATA BEGIN\n" in user else "A")
            with lock:
                active += 1
                maximum = max(maximum, active)
                observed.append(identifier)
            try:
                # Both calls in each phase must be in flight before either returns.
                barrier.wait(timeout=5)
                return self.response()
            finally:
                with lock:
                    active -= 1

        with patch.object(pipeline, "send_request", side_effect=delayed_response) as send:
            counts = self.run_pipeline()
        self.assertEqual(send.call_count, 4)
        self.assertEqual(counts["successful"], 4)
        self.assertEqual(maximum, 2)
        self.assertEqual(len(observed), len(set(observed)))
        self.assertEqual(set(observed), {(case_id, phase) for case_id in ("C-1/20", "C-2/20") for phase in ("A", "B")})
        self.assertEqual(len(list((self.audit / "requests").glob("*/attempt_001/response.json"))), 4)
        self.assertFalse(list((self.audit / "requests").glob("*/attempt_002")))
        with patch.object(pipeline, "send_request") as send:
            resumed = self.run_pipeline()
        send.assert_not_called()
        self.assertEqual(resumed["processed"], 0)
        self.assertEqual(resumed["skipped_existing"], 4)

    def test_authentication_failure_allows_only_already_inflight_provider_requests(self) -> None:
        self.config.update(request_workers=2, requests_per_provider=2)
        self.fixture._write_config()
        barrier = threading.Barrier(2)

        def authentication_failure(*args, **kwargs):
            barrier.wait(timeout=5)
            return self.response(status="api_error", http_status=401)

        with patch.object(pipeline, "send_request", side_effect=authentication_failure) as send:
            counts = self.run_pipeline()
        self.assertEqual(send.call_count, 2)
        self.assertEqual(counts["processed"], 2)
        self.assertEqual(counts["failed"], 2)
        self.assertEqual(counts["blocked_providers"], 1)
        rules = self.csv_rows(self.output / "rule_assessments.csv")
        self.assertTrue(all(row["status"] == "api_error" for row in rules if row["phase"] == "A"))
        self.assertTrue(all(row["status"] == "pending" for row in rules if row["phase"] == "B"))

    def test_authentication_failure_blocks_further_cases_and_later_phase(self) -> None:
        response = self.response(status="api_error", http_status=401)
        with patch.object(pipeline, "send_request", return_value=response) as send:
            counts = self.run_pipeline()
        self.assertEqual(send.call_count, 1)
        self.assertEqual(counts["processed"], 1)
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(counts["blocked_providers"], 1)
        rules = self.csv_rows(self.output / "rule_assessments.csv")
        self.assertEqual(sum(row["status"] == "api_error" for row in rules), 2)
        self.assertEqual(sum(row["status"] == "pending" for row in rules), 6)
        self.assertTrue(all(row["status"] == "pending" for row in rules if row["phase"] == "B"))
        self.assertEqual(len(list((self.audit / "requests").glob("*/attempt_001/response.json"))), 1)


if __name__ == "__main__":
    unittest.main()
