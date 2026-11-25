"""Verify frozen phase-1 reuse and strict phase-2 protocol/selection guards."""

import contextlib
import copy
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_case_pipeline as fixture_module
from curia_assessment import pipeline as p
from curia_assessment.case_prompts import build_case_prompts
from curia_assessment.phase_baseline import baseline_identity, load_phase1_baseline


class PhaseBaselineTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.CasePipelineTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        workbook = patch("curia_assessment.evaluation_exports.write_expert_workbooks")
        workbook.start()
        self.addCleanup(workbook.stop)
        self.fixture.config.update(conditions=["A"], max_output_tokens=32000)
        self.fixture.fixture._write_config()
        with patch.object(p, "send_request", return_value=self.fixture.response()) as send:
            with contextlib.redirect_stdout(io.StringIO()):
                p.run(self.fixture.config_path)
        self.assertEqual(send.call_count, 2)
        self.root, self.audit = self.fixture.root, self.fixture.audit
        config, _ = p.load_config(self.fixture.config_path)
        self.config = copy.deepcopy(config)
        self.config.update(conditions=["B"], output_dir="phase2", baseline_phase1_dir="outputs",
                           max_document_characters=None, max_case_characters=None,
                           deduplicate_exact_text=True, exclude_identifier_only=True)
        self.jobs = p.read_jsonl(self.audit / "prepared/planned_assessments.jsonl")
        self.pairs = p.read_jsonl(self.audit / "prepared/pairs.jsonl")

    def load(self, config=None):
        return load_phase1_baseline(self.config if config is None else config, self.root)

    def refresh_prepared_hash(self, relative):
        path = self.audit / "run_manifest.json"
        manifest = p.read_json(path)
        manifest["prepared_sha256"][relative] = p.sha256_file(self.audit / relative)
        p.write_json(path, manifest)

    def first_attempt(self):
        return self.audit / "requests" / self.jobs[0]["job_id"] / "attempt_001"

    def baseline_hashes(self):
        return {str(path.relative_to(self.fixture.output)): p.sha256_file(path)
                for path in self.fixture.output.rglob("*") if path.is_file()}

    def test_reuses_exact_ordered_cases_rules_and_pairs_without_resampling_or_answer_leakage(self):
        before = self.baseline_hashes()
        with patch.object(p, "select_cases_by_year", side_effect=AssertionError("Must not resample")), \
                patch.object(p, "select_requirements", side_effect=AssertionError("Must not reformulate")), \
                patch.object(p, "import_formal_results", side_effect=AssertionError("Must reuse reference")), \
                patch.object(p, "send_request") as send:
            baseline = self.load()
        send.assert_not_called()
        self.assertEqual([case["case_id"] for case in baseline["cases"]], ["C-1/20", "C-2/20"])
        self.assertEqual(baseline["pairs"], self.pairs)
        self.assertEqual([rule["base_rule_id"] for rule in baseline["rules"]], self.jobs[0]["rule_ids"])
        self.assertEqual(len(baseline["formal"]), 4)
        self.assertEqual(len(baseline["provenance"]["phase_1_requests_reproduced"]), 2)
        self.assertNotIn("MODEL_RESPONSE_SENTINEL", json.dumps(baseline))
        self.assertFalse((self.root / "phase2").exists())
        self.assertEqual(self.baseline_hashes(), before)

    def test_phase2_prepare_uses_frozen_pair_identity_and_only_appends_document_data(self):
        config_path = self.root / "phase2.json"
        p.write_json(config_path, self.config)
        before = self.baseline_hashes()
        with patch.object(p, "select_cases_by_year", side_effect=AssertionError("Must not resample")), \
                patch.object(p, "select_requirements", side_effect=AssertionError("Must not reformulate")), \
                patch.object(p, "import_formal_results", side_effect=AssertionError("Must reuse reference")), \
                patch.object(p, "send_request") as send:
            manifest = p.prepare(config_path)
        send.assert_not_called()
        self.assertEqual(manifest["sampled_cases"], 2)
        self.assertEqual(manifest["planned_assessments"], 2)
        self.assertEqual(manifest["planned_rule_assessments"], 4)
        phase2_audit = self.root / "phase2/_audit"
        second = p.read_jsonl(phase2_audit / "prepared/pairs.jsonl")
        self.assertEqual([{key: value for key, value in pair.items() if key != "passages"} for pair in second],
                         [{key: value for key, value in pair.items() if key != "passages"} for pair in self.pairs])
        for job in self.jobs:
            members = [pair for pair in second if pair["case_id"] == job["case_id"]]
            system, user, refs = build_case_prompts(members, "B", members[0]["passages"])
            original = p.read_json(self.audit / "requests" / job["job_id"] / "attempt_001/request.json")["payload"]
            self.assertEqual(system, original["instructions"])
            self.assertTrue(user.startswith(original["input"][0]["content"][0]["text"]))
            self.assertIn("D0001", refs)
            self.assertNotIn("MODEL_RESPONSE_SENTINEL", user)
        self.assertEqual(self.baseline_hashes(), before)

    def test_protocol_changes_are_rejected_while_document_processing_options_are_allowed(self):
        self.assertEqual(len(self.load()["cases"]), 2)
        changes = ({"max_output_tokens": 64000}, {"sampling_seed": 1}, {"timeout_seconds": 901},
                   {"min_events": 1}, {"repetitions": 2}, {"cases_per_year": 1}, {"rule_count": 1},
                   {"providers": [{**self.config["providers"][0], "model": "another-pinned-model"}]})
        for change in changes:
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "retain the phase-1 setting"):
                self.load({**self.config, **change})

    def test_historical_source_prompt_identity_cannot_become_new_phase_a_baseline(self):
        jobs = copy.deepcopy(self.jobs)
        jobs[0]["prompt_sha256"] = p.digest("Historical requirement-plus-source prompt")
        p.write_jsonl(self.audit / "prepared/planned_assessments.jsonl", jobs)
        self.refresh_prepared_hash("prepared/planned_assessments.jsonl")
        before = self.baseline_hashes()
        with patch.object(p, "send_request") as send:
            with self.assertRaisesRegex(ValueError, "Phase-1 request cannot be reproduced exactly"):
                self.load()
        send.assert_not_called()
        self.assertEqual(self.baseline_hashes(), before)
        self.assertFalse((self.root / "phase2").exists())

    def test_changed_input_bytes_and_changed_prepared_evidence_are_rejected(self):
        source = self.root / "events.csv"
        original = source.read_bytes()
        source.write_bytes(original + b"\n")
        with self.assertRaisesRegex(ValueError, "phase-1 input changed: event_log_csv"):
            self.load()
        source.write_bytes(original)
        path = self.audit / "prepared/pairs.jsonl"
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "frozen phase-1 evidence changed"):
            self.load()

    def test_manifest_identity_cannot_be_changed_without_detection(self):
        path = self.audit / "run_manifest.json"
        manifest = p.read_json(path)
        manifest["configuration"]["max_output_tokens"] = 64000
        p.write_json(path, manifest)
        with self.assertRaisesRegex(ValueError, "manifest identity is inconsistent"):
            self.load()

    def test_completed_flag_cannot_hide_foreign_job_or_changed_provider_response(self):
        directory = self.first_attempt()
        assessment_path = directory / "assessment.json"
        assessment_bytes = assessment_path.read_bytes()
        response_path = directory / "response.json"
        response_bytes = response_path.read_bytes()
        assessment = p.read_json(assessment_path)
        assessment["job_id"] = "foreign_case_job"
        p.write_json(assessment_path, assessment)
        with self.assertRaisesRegex(ValueError, "assessment does not match"):
            self.load()
        assessment_path.write_bytes(assessment_bytes)
        response_path.write_bytes(response_bytes + b"\n")
        with self.assertRaisesRegex(ValueError, "provider response changed or is missing"):
            self.load()

    def test_missing_or_incomplete_baseline_result_is_rejected_without_local_or_paid_recovery(self):
        path = self.first_attempt() / "assessment.json"
        original = path.read_bytes()
        path.unlink()
        with patch.object(p, "send_request") as send:
            with self.assertRaisesRegex(ValueError, "completed result"):
                self.load()
        send.assert_not_called()
        self.assertFalse(path.exists())
        path.write_bytes(original)
        assessment = p.read_json(path)
        assessment["status"] = "invalid_output"
        p.write_json(path, assessment)
        with self.assertRaisesRegex(ValueError, "incomplete assessment"):
            self.load()

    def test_duplicate_selected_case_or_job_allocation_is_rejected_even_with_matching_file_hash(self):
        path = self.audit / "prepared/selected_cases.csv"
        original = path.read_bytes()
        rows = self.fixture.csv_rows(path)
        rows[1] = dict(rows[0])
        p.write_table(path, rows)
        self.refresh_prepared_hash("prepared/selected_cases.csv")
        with self.assertRaisesRegex(ValueError, "case/model/run exactly once"):
            self.load()
        path.write_bytes(original)
        self.refresh_prepared_hash("prepared/selected_cases.csv")
        jobs = [self.jobs[0], self.jobs[0]]
        p.write_jsonl(self.audit / "prepared/planned_assessments.jsonl", jobs)
        self.refresh_prepared_hash("prepared/planned_assessments.jsonl")
        with self.assertRaisesRegex(ValueError, "case/model/run exactly once"):
            self.load()

    def test_case_pair_order_must_match_the_original_request_allocation(self):
        jobs = copy.deepcopy(self.jobs)
        jobs[0]["pair_ids"].reverse()
        p.write_jsonl(self.audit / "prepared/planned_assessments.jsonl", jobs)
        self.refresh_prepared_hash("prepared/planned_assessments.jsonl")
        with self.assertRaisesRegex(ValueError, "case allocation is inconsistent"):
            self.load()

    def test_same_output_or_absent_baseline_is_rejected_and_config_guards_phase_b(self):
        with self.assertRaisesRegex(ValueError, "separate output directory"):
            self.load({**self.config, "output_dir": "outputs"})
        self.assertIsNone(baseline_identity({**self.config, "baseline_phase1_dir": None}, self.root))
        with self.assertRaisesRegex(ValueError, "No phase-1 baseline"):
            self.load({**self.config, "baseline_phase1_dir": None})
        path = self.root / "invalid-phase.json"
        p.write_json(path, {**self.config, "conditions": ["A"]})
        with self.assertRaisesRegex(ValueError, "baseline_phase1_dir requires"):
            p.load_config(path)


if __name__ == "__main__":
    unittest.main()
