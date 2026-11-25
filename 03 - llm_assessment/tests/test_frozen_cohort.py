"""Reuse frozen cohort inputs for a new protocol without reusing old answers."""

import contextlib
import copy
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_case_pipeline as fixture_module
from curia_assessment import case_pipeline
from curia_assessment import phase_baseline
from curia_assessment import pipeline as p
from curia_assessment.case_prompts import build_case_prompts


class FrozenCohortTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.CasePipelineTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        workbook = patch("curia_assessment.evaluation_exports.write_expert_workbooks")
        workbook.start()
        self.addCleanup(workbook.stop)
        self.fixture.config.update(conditions=["A"], max_output_tokens=32000,
                                   case_ids=["C-2/20", "C-1/20"], rule_ids=["R02", "R01"])
        self.fixture.fixture._write_config()
        original_cross = p.cross_pairs

        def nested_pairs(cases, rules):
            for case in cases:
                case["metadata"]["Nested"] = {"numbers": [1, 2], "detail": {"present": True}}
                case["events"][0]["metadata"]["Nested"] = [{"event_note": "Preserve exact supplied metadata"}]
            return original_cross(cases, rules)

        def historical_prompt(*args, **kwargs):
            system, user, refs = build_case_prompts(*args, **kwargs)
            return "HISTORICAL_FOUR_OUTCOME_PROTOCOL\n" + system, user, refs

        # Deliberately incompatible old request hashes demonstrate that the
        # cohort loader must not reproduce or read the historical requests.
        with patch.object(p, "send_request") as send, patch.object(p, "cross_pairs", side_effect=nested_pairs), \
                patch.object(case_pipeline, "build_case_prompts", side_effect=historical_prompt):
            p.prepare(self.fixture.config_path)
        send.assert_not_called()
        self.root, self.source, self.audit = self.fixture.root, self.fixture.output, self.fixture.audit
        self.pairs = p.read_jsonl(self.audit / "prepared/pairs.jsonl")
        self.old_jobs = p.read_jsonl(self.audit / "prepared/planned_assessments.jsonl")
        p.write_json(self.audit / "requests" / self.old_jobs[0]["job_id"] / "attempt_001/response.json",
                     {"text": "DO_NOT_COPY_THE_OLD_MODEL_ANSWER", "outcome": "not_applicable"})
        self.config, _ = p.load_config(self.fixture.config_path)
        self.config.update(output_dir="new-a", cohort_source_dir="outputs", baseline_phase1_dir=None)

    def load(self, config=None):
        return phase_baseline.load_frozen_cohort(self.config if config is None else config, self.root)

    def source_hashes(self):
        return {str(path.relative_to(self.source)): p.sha256_file(path)
                for path in self.source.rglob("*") if path.is_file()}

    def prepare_new_a(self):
        config_path = self.root / "new-a.json"
        p.write_json(config_path, self.config)
        with patch.object(p, "send_request") as send:
            result = p.prepare(config_path)
        send.assert_not_called()
        return config_path, result

    def refresh_prepared_hash(self, relative):
        path = self.audit / "run_manifest.json"
        manifest = p.read_json(path)
        manifest["prepared_sha256"][relative] = p.sha256_file(self.audit / relative)
        p.write_json(path, manifest)

    def test_exact_order_nested_metadata_and_rule_formulations_reused_without_historical_answers(self):
        before = self.source_hashes()
        with patch.object(phase_baseline, "read_case_attempt", side_effect=AssertionError("No old answers")), \
                patch.object(phase_baseline, "build_case_prompts", side_effect=AssertionError("No old prompt replay")), \
                patch.object(p, "send_request") as send:
            cohort = self.load()
        send.assert_not_called()
        self.assertEqual([case["case_id"] for case in cohort["cases"]], ["C-2/20", "C-1/20"])
        self.assertEqual([rule["base_rule_id"] for rule in cohort["rules"]], ["R02", "R01"])
        self.assertEqual(cohort["pairs"], self.pairs)
        self.assertEqual(cohort["cases"][0]["metadata"]["Nested"], {"numbers": [1, 2], "detail": {"present": True}})
        self.assertEqual(cohort["cases"][0]["events"][0]["metadata"]["Nested"],
                         [{"event_note": "Preserve exact supplied metadata"}])
        self.assertEqual(cohort["formal"], p.read_jsonl(self.audit / "prepared/formal_results.jsonl"))
        self.assertEqual(cohort["provenance"]["model_responses_reused"], 0)
        self.assertNotIn("DO_NOT_COPY_THE_OLD_MODEL_ANSWER", json.dumps(cohort))
        self.assertFalse((self.root / "new-a").exists())
        self.assertEqual(self.source_hashes(), before)

    def test_new_a_prepare_never_resamples_reformulates_or_imports_formal_results(self):
        before = self.source_hashes()
        with patch.object(p, "select_cases_by_year", side_effect=AssertionError("No resampling")), \
                patch.object(p, "select_requirements", side_effect=AssertionError("No reformulation")), \
                patch.object(p, "cross_pairs", side_effect=AssertionError("Use original pair ordering")), \
                patch.object(p, "import_formal_results", side_effect=AssertionError("Use original formal pairs")):
            _, manifest = self.prepare_new_a()
        audit = self.root / "new-a/_audit"
        self.assertEqual((manifest["sampled_cases"], manifest["selected_pairs"], manifest["planned_assessments"]), (2, 4, 2))
        self.assertEqual(p.read_jsonl(audit / "prepared/pairs.jsonl"), self.pairs)
        identity = phase_baseline.cohort_identity(self.config, self.root)
        self.assertEqual(manifest["input_sha256"]["frozen_cohort"], p.digest(identity))
        self.assertEqual(identity["manifest_sha256"], p.sha256_file(self.audit / "run_manifest.json"))
        self.assertEqual(identity["prepared_sha256"], p.read_json(self.audit / "run_manifest.json")["prepared_sha256"])
        self.assertEqual(p.read_json(audit / "prepared/cohort_provenance.json")["model_responses_reused"], 0)
        self.assertFalse((audit / "requests").exists())
        new_jobs = p.read_jsonl(audit / "prepared/planned_assessments.jsonl")
        for old, new in zip(self.old_jobs, new_jobs, strict=True):
            self.assertEqual(old["case_id"], new["case_id"])
            self.assertEqual(old["pair_ids"], new["pair_ids"])
            self.assertEqual(old["rule_ids"], new["rule_ids"])
            self.assertNotEqual(old["prompt_sha256"], new["prompt_sha256"])
            self.assertNotEqual(old["request_sha256"], new["request_sha256"])
        self.assertFalse(any(b"DO_NOT_COPY_THE_OLD_MODEL_ANSWER" in path.read_bytes()
                             for path in (audit / "prepared").rglob("*") if path.is_file()))
        self.assertEqual(self.source_hashes(), before)

    def test_legacy_source_fields_are_integrity_checked_then_projected_out_of_reused_inputs(self):
        pairs_path = self.audit / "prepared/pairs.jsonl"
        legacy_pairs = copy.deepcopy(self.pairs)
        for row in legacy_pairs:
            row["source"] = "LEGACY_LEGAL_SOURCE_MUST_NOT_ENTER_NEW_EVIDENCE"
            row["source_review_id"] = row.pop("requirement_review_id")
        p.write_jsonl(pairs_path, legacy_pairs)
        self.refresh_prepared_hash("prepared/pairs.jsonl")
        rules_path = self.audit / "prepared/selected_rules.csv"
        legacy_rules = self.fixture.csv_rows(rules_path)
        for row in legacy_rules:
            row["source"] = "LEGACY_LEGAL_SOURCE_MUST_NOT_ENTER_NEW_EVIDENCE"
            row["source_review_id"] = row.pop("requirement_review_id")
        p.write_table(rules_path, legacy_rules)
        self.refresh_prepared_hash("prepared/selected_rules.csv")
        before = self.source_hashes()
        cohort = self.load()
        self.assertEqual(cohort["pairs"], self.pairs)
        for row in [*cohort["pairs"], *cohort["rules"]]:
            self.assertFalse({"source", "legal_source", "source_review_id"}.intersection(row))
            self.assertIn("requirement_review_id", row)
        self.assertNotIn("LEGACY_LEGAL_SOURCE", json.dumps(cohort))
        self.assertEqual(self.source_hashes(), before)
        # Projection does not bypass the original frozen-artifact hash check.
        pairs_path.write_bytes(pairs_path.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "frozen cohort evidence changed"):
            self.load()

    def test_new_a_completed_in_fixture_is_the_strict_baseline_for_new_b(self):
        before = self.source_hashes()
        config_path, _ = self.prepare_new_a()
        with patch.object(p, "send_request", return_value=self.fixture.response()) as send, \
                contextlib.redirect_stdout(io.StringIO()):
            p.run(config_path)
        self.assertEqual(send.call_count, 2)
        second = {**self.config, "conditions": ["B"], "output_dir": "new-b", "baseline_phase1_dir": "new-a"}
        second_path = self.root / "new-b.json"
        p.write_json(second_path, second)
        with patch.object(p, "send_request") as send:
            p.prepare(second_path)
        send.assert_not_called()
        second_pairs = p.read_jsonl(self.root / "new-b/_audit/prepared/pairs.jsonl")
        self.assertEqual([{key: value for key, value in pair.items() if key != "passages"} for pair in second_pairs],
                         [{key: value for key, value in pair.items() if key != "passages"} for pair in self.pairs])
        for case_id in ("C-2/20", "C-1/20"):
            members = [pair for pair in second_pairs if pair["case_id"] == case_id]
            system_a, user_a, _ = build_case_prompts(members, "A")
            system_b, user_b, _ = build_case_prompts(members, "B", members[0]["passages"])
            self.assertEqual(system_a, system_b)
            self.assertTrue(user_b.startswith(user_a + "\n\nADDITIONAL DOCUMENT DATA BEGIN\n"))
            self.assertNotIn("DO_NOT_COPY_THE_OLD_MODEL_ANSWER", system_b + user_b)
        self.assertFalse((self.root / "new-b/_audit/requests").exists())
        self.assertEqual(self.source_hashes(), before)

    def test_selection_settings_are_frozen_but_a_new_generation_protocol_is_allowed(self):
        changes = {"min_events": 1, "case_category": None, "country": "EN", "year_start": 2019,
                   "year_end": 2021, "cases_per_year": 1, "total_cases": 1, "case_ids": None,
                   "variant_ids": ["different"], "rule_ids": ["R01"], "rule_count": 1,
                   "sampling_seed": 1, "formal_mode": "compute"}
        for field, value in changes.items():
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, f"retain its setting: {field}"):
                self.load({**self.config, field: value})
        modified = {**self.config, "max_output_tokens": 12345, "timeout_seconds": 901,
                    "providers": [{**self.config["providers"][0], "model": "another-pinned-model"}]}
        self.assertEqual(self.load(modified)["pairs"], self.pairs)

    def test_source_input_bytes_and_document_presence_are_frozen_but_document_content_is_not_a_input(self):
        source = self.root / "events.csv"
        original = source.read_bytes()
        source.write_bytes(original + b"\n")
        with self.assertRaisesRegex(ValueError, "cohort input changed: event_log_csv"):
            self.load()
        source.write_bytes(original)
        document = self.fixture.fixture._document_path("C-1/20")
        document.write_text("Replacement text is not evidence in phase A.")
        self.assertEqual(self.load()["pairs"], self.pairs)
        extra = document.with_name("C-999_20_01_01.txt")
        extra.write_text("An extra document changes eligibility presence.")
        with self.assertRaisesRegex(ValueError, "document-presence index changed"):
            self.load()

    def test_manifest_and_prepared_tampering_are_rejected_and_source_identity_is_frozen(self):
        path = self.audit / "prepared/pairs.jsonl"
        original = path.read_bytes()
        path.write_bytes(original + b"\n")
        with self.assertRaisesRegex(ValueError, "frozen cohort evidence changed"):
            self.load()
        path.write_bytes(original)
        self.prepare_new_a()
        manifest_path = self.audit / "run_manifest.json"
        manifest = p.read_json(manifest_path)
        manifest["created_utc"] = "A changed source-manifest byte is an identity change"
        p.write_json(manifest_path, manifest)
        with self.assertRaisesRegex(ValueError, "input files: frozen_cohort"):
            self.prepare_new_a()
        manifest["configuration"]["sampling_seed"] += 1
        p.write_json(manifest_path, manifest)
        with self.assertRaisesRegex(ValueError, "manifest identity is inconsistent"):
            self.load()

    def test_duplicate_case_or_pair_and_rule_allocation_errors_are_rejected_even_with_updated_file_hashes(self):
        path = self.audit / "prepared/selected_cases.csv"
        original = path.read_bytes()
        rows = self.fixture.csv_rows(path)
        rows[1] = dict(rows[0])
        p.write_table(path, rows)
        self.refresh_prepared_hash("prepared/selected_cases.csv")
        with self.assertRaisesRegex(ValueError, "allocation is inconsistent"):
            self.load()
        path.write_bytes(original)
        self.refresh_prepared_hash("prepared/selected_cases.csv")
        changed = copy.deepcopy(self.pairs)
        changed[-1] = changed[0]
        p.write_jsonl(self.audit / "prepared/pairs.jsonl", changed)
        self.refresh_prepared_hash("prepared/pairs.jsonl")
        with self.assertRaisesRegex(ValueError, "allocation is inconsistent"):
            self.load()
        changed = copy.deepcopy(self.pairs)
        changed[0], changed[1] = changed[1], changed[0]
        p.write_jsonl(self.audit / "prepared/pairs.jsonl", changed)
        self.refresh_prepared_hash("prepared/pairs.jsonl")
        with self.assertRaisesRegex(ValueError, "ordered rule formulation"):
            self.load()

    def test_source_must_be_separate_phase_a_case_and_config_single_phase(self):
        with self.assertRaisesRegex(ValueError, "separate output directory"):
            self.load({**self.config, "output_dir": "outputs"})
        self.assertIsNone(phase_baseline.cohort_identity({**self.config, "cohort_source_dir": None}, self.root))
        with self.assertRaisesRegex(ValueError, "No frozen cohort"):
            self.load({**self.config, "cohort_source_dir": None})
        for change in ({"conditions": ["A", "B"]}, {"conditions": ["C"]}, {"assessment_mode": "rule"}):
            path = self.root / "invalid-config.json"
            p.write_json(path, {**self.config, **change})
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "cohort_source_dir requires"):
                p.load_config(path)
        manifest_path = self.audit / "run_manifest.json"
        manifest = p.read_json(manifest_path)
        manifest["configuration"]["conditions"] = ["B"]
        manifest["experiment_sha256"] = p.digest({key: manifest[key] for key in
                                                  ("configuration", "input_sha256", "code_sha256", "pipeline_version")})
        p.write_json(manifest_path, manifest)
        with self.assertRaisesRegex(ValueError, "log-only case experiment"):
            self.load()

    def test_case_register_order_cannot_diverge_from_original_ordered_pair_groups(self):
        path = self.audit / "prepared/selected_cases.csv"
        p.write_table(path, list(reversed(self.fixture.csv_rows(path))))
        self.refresh_prepared_hash("prepared/selected_cases.csv")
        with self.assertRaisesRegex(ValueError, "allocation is inconsistent"):
            self.load()

    def test_every_case_retains_the_exact_registered_rule_formulation(self):
        path = self.audit / "prepared/pairs.jsonl"
        original = path.read_bytes()
        for field, value in (("requirement", "A rewritten requirement"), ("declare", 'AtLeastOne("UNREQUESTED")'),
                              ("variant_id", "A different variant")):
            with self.subTest(field=field):
                changed = copy.deepcopy(self.pairs)
                changed[-1][field] = value
                p.write_jsonl(path, changed)
                self.refresh_prepared_hash("prepared/pairs.jsonl")
                with self.assertRaisesRegex(ValueError, "rule formulations disagree"):
                    self.load()
        path.write_bytes(original)
        self.refresh_prepared_hash("prepared/pairs.jsonl")
        path = self.audit / "prepared/selected_rules.csv"
        rows = self.fixture.csv_rows(path)
        rows[0]["requirement"] = "A rewritten registered requirement"
        p.write_table(path, rows)
        self.refresh_prepared_hash("prepared/selected_rules.csv")
        with self.assertRaisesRegex(ValueError, "rule formulations disagree"):
            self.load()

    def test_registered_case_metadata_and_shared_events_must_match_every_rule(self):
        path = self.audit / "prepared/selected_cases.csv"
        original = path.read_bytes()
        rows = self.fixture.csv_rows(path)
        rows[0]["metadata"] = json.dumps({"Nested": {"altered": "case metadata"}})
        p.write_table(path, rows)
        self.refresh_prepared_hash("prepared/selected_cases.csv")
        with self.assertRaisesRegex(ValueError, "case metadata disagree"):
            self.load()
        path.write_bytes(original)
        self.refresh_prepared_hash("prepared/selected_cases.csv")
        for field in ("events", "metadata"):
            with self.subTest(field=field):
                changed = copy.deepcopy(self.pairs)
                if field == "events":
                    changed[1]["events"][0]["activity"] = "Different event for this rule"
                else:
                    changed[1]["metadata"]["Nested"] = {"altered": "case metadata"}
                p.write_jsonl(self.audit / "prepared/pairs.jsonl", changed)
                self.refresh_prepared_hash("prepared/pairs.jsonl")
                with self.assertRaisesRegex(ValueError, "inconsistent shared case evidence"):
                    self.load()

    def test_log_only_cohort_cannot_already_contain_document_passages(self):
        changed = copy.deepcopy(self.pairs)
        changed[0]["passages"] = [{"passage_id": "D0001", "source_reference": "old.txt", "text": "Old document"}]
        p.write_jsonl(self.audit / "prepared/pairs.jsonl", changed)
        self.refresh_prepared_hash("prepared/pairs.jsonl")
        with self.assertRaisesRegex(ValueError, "contains document evidence"):
            self.load()


if __name__ == "__main__":
    unittest.main()
