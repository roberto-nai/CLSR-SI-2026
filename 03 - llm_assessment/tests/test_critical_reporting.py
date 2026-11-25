"""Independent endpoint/resource tests; provider calls and workbook I/O are mocked."""
from __future__ import annotations

import copy
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from curia_assessment import critical_reporting as report, pipeline as p


def providers():
    return [{"id": name, "provider": name, "model": "pinned-" + name} for name in ("openai", "anthropic", "mistral")]


def leaf(rule_id, outcome="compliance", trace="compliance", status="ok"):
    return {"rule_id": rule_id, "status": status, "outcome": outcome,
            "requirement_assessment": {"outcome": outcome}, "trace_conformance": {"outcome": trace}}


class ResourceTests(unittest.TestCase):
    def test_native_case_requests_are_not_multiplied_by_nineteen(self):
        provider = providers()[0]
        jobs = [{"provider_id": "openai", "complete": True, "rule_ids": [f"R{i:02d}" for i in range(1, 20)]} for _ in range(50)]
        ledger = [{"provider_id": "openai", "call_attempted": True, "input_tokens": 100, "output_tokens": 20,
                   "total_tokens": 120, "elapsed_seconds": 1.0} for _ in jobs]
        row = report._resources("case_assessment", [provider], jobs, ledger)[0]
        self.assertEqual((row["planned_requests"], row["completed_requests"], row["retained_attempts"], row["recorded_api_calls"]), (50, 50, 50, 50))
        self.assertEqual((row["input_tokens"], row["output_tokens"], row["total_tokens"], row["api_seconds_total"]), (5000, 1000, 6000, 50))

    def test_failure_unknown_and_measured_zero_preserve_coverage(self):
        provider = providers()[0]
        row = report._resources("case_assessment", [provider], [{"provider_id": "openai", "complete": False}], [
            {"provider_id": "openai", "call_attempted": True, "input_tokens": 10, "output_tokens": 0, "total_tokens": 10, "elapsed_seconds": 0},
            {"provider_id": "openai", "call_attempted": None, "input_tokens": None, "output_tokens": None, "total_tokens": None, "elapsed_seconds": None},
        ])[0]
        self.assertEqual((row["retained_attempts"], row["recorded_api_calls"], row["unknown_call_attempt_flags"]), (2, 1, 1))
        self.assertEqual((row["output_tokens"], row["measured_output_attempts"]), (0, 1))
        self.assertEqual((row["api_seconds_total"], row["api_seconds_mean"], row["measured_elapsed_attempts"]), (0, 0, 1))

    def test_unrun_stage_zero_does_not_make_unknown_combined_usage_zero(self):
        provider = providers()[0]
        unrun = report._resources("case_assessment", [provider], [{"provider_id": "openai"}], [])[0]
        self.assertEqual((unrun["input_tokens"], unrun["output_tokens"], unrun["total_tokens"], unrun["api_seconds_total"]), (0, 0, 0, 0))
        unknown = report._resources("formalisation_audit", [provider], [{"provider_id": "openai"}], [
            {"provider_id": "openai", "call_attempted": True, "elapsed_seconds": None}
        ])[0]
        self.assertIsNone(unknown["input_tokens"]); self.assertIsNone(unknown["api_seconds_total"])
        combined = report._combined_resources([unrun, unknown])[0]
        self.assertEqual(combined["retained_attempts"], 1)
        for key in ("input_tokens", "output_tokens", "total_tokens", "api_seconds_total", "api_seconds_mean"):
            self.assertIsNone(combined[key], key)

    def test_combined_known_partial_sums_and_weighted_mean(self):
        provider = providers()[0]
        case = report._resources("case_assessment", [provider], [{"provider_id": "openai"}], [
            {"provider_id": "openai", "call_attempted": True, "input_tokens": 30, "output_tokens": 0, "total_tokens": 30, "elapsed_seconds": 2},
            {"provider_id": "openai", "call_attempted": True, "elapsed_seconds": None},
        ])[0]
        audit = report._resources("formalisation_audit", [provider], [{"provider_id": "openai"}], [
            {"provider_id": "openai", "call_attempted": True, "input_tokens": 20, "output_tokens": 5, "total_tokens": 25, "elapsed_seconds": 8}
        ])[0]
        total = report._combined_resources([case, audit])[0]
        self.assertEqual((total["retained_attempts"], total["recorded_api_calls"], total["input_tokens"]), (3, 3, 50))
        self.assertEqual((total["measured_input_attempts"], total["api_seconds_total"], total["api_seconds_mean"]), (2, 10, 5))


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.job = {"job_id": "case1", "provider_id": "openai", "model": "pinned-openai", "case_id": "C-1/20", "rule_ids": ["R01", "R02"]}
        self.formal = {("C-1/20", "R01"): 1, ("C-1/20", "R02"): 1}

    def test_requirement_checker_and_trace_checker_are_distinct(self):
        results = {"case1": {"assessments": [leaf("R01", "violation", "compliance"), leaf("R02", "undetermined", "violation")]}}
        summary = report._leaf_counts([self.job], results, self.formal)[0]
        self.assertEqual(summary["comparison"], {"Disagreement": 1, "Undetermined": 1})
        self.assertEqual(summary["trace_comparison"], {"Agreement": 1, "Disagreement": 1})
        self.assertEqual((summary["planned"], summary["unavailable"], summary["trace_unavailable"]), (2, 0, 0))

    def test_unknown_raw_outcome_types_are_unavailable_not_undetermined(self):
        for bad in ([], {}, None, "not_applicable"):
            rows = [leaf("R01", bad, bad, "invalid_output"), leaf("R02", bad, bad, "invalid_output")]
            with self.subTest(value=bad):
                summary = report._leaf_counts([self.job], {"case1": {"assessments": rows}}, self.formal, reported=True)[0]
                self.assertEqual((summary["requirement"], summary["trace"], summary["unavailable"], summary["trace_unavailable"]), ({}, {}, 2, 2))
        summary = report._leaf_counts([self.job], {"case1": {"assessments": [leaf("R01", "undetermined", "undetermined", "invalid_output"), leaf("R02", "undetermined", "undetermined", "invalid_output")]}}, self.formal, reported=True)[0]
        self.assertEqual(summary["requirement"], {"undetermined": 2})
        self.assertEqual((summary["trace"], summary["trace_unavailable"]), ({}, 2))

    def test_partial_shape_failures_do_not_become_semantic_uncertainty(self):
        rows = [leaf("R01"), leaf("R02", None, None, "invalid_output")]
        summary = report._leaf_counts([self.job], {"case1": {"assessments": rows}}, self.formal)[0]
        self.assertEqual((summary["requirement"], summary["unavailable"], summary["structurally_valid_leaves"]), ({"compliance": 1}, 1, 1))
        self.assertNotIn("Undetermined", summary["comparison"])
        malformed = report._leaf_counts([self.job], {"case1": {"assessments": [rows[0]]}}, self.formal)[0]
        self.assertEqual((malformed["requirement"], malformed["unavailable"]), ({}, 2))

    def test_partial_case_flag_unavailable_with_positive_violation_exception(self):
        cases = [{"case_id": "C-1/20", "case_year": 2020}]
        models = providers()
        values = [{"case_id": "C-1/20", "provider_id": "openai", "n_completed": 2, "violation": "undetermined", "status": "partial"},
                  {"case_id": "C-1/20", "provider_id": "anthropic", "n_completed": 2, "violation": "yes", "status": "partial"},
                  {"case_id": "C-1/20", "provider_id": "mistral", "n_completed": 19, "violation": "no", "status": "completed"}]
        row = report._case_comparison(cases, models, values)[0]
        self.assertEqual((row["openai_case_flag"], row["anthropic_case_flag"], row["mistral_case_flag"]), ("unavailable", "yes", "no"))
        with self.assertRaises(ValueError): report._case_comparison(cases, models, [*values, values[0]])

    def test_rule_audit_denominator_is_nineteen_not_case_pairs(self):
        rows = [{"provider_id": provider["id"], "model": provider["model"], "first_in_scope": "Disagreement", "latest_in_scope": "Disagreement",
                 "first_ungated": "", "latest_ungated": "", "in_scope_formula_consistent_D": "true", "ungated_formula_consistent_D": "false"}
                for provider in providers() for _ in range(19)]
        summaries = report._audit_summary(rows)
        self.assertEqual(len(summaries), 6)
        self.assertTrue(all(row["planned"] == 19 for row in summaries))
        self.assertTrue(all(row["latest"]["Disagreement"] == 19 for row in summaries if row["domain"] == "in_scope"))
        self.assertTrue(all(row["latest_unavailable"] == 19 and row["latest"]["Undetermined"] == 0 for row in summaries if row["domain"] == "ungated"))

    def test_audit_raw_bad_labels_are_not_promoted_or_crashing(self):
        from curia_assessment import formalisation_runner
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            p.write_jsonl(root / "_audit/prepared/rules.jsonl", [{"rule_id": f"R{i:02d}", "declare": 'AtLeastOne("A")', "requirement": "A occurs."} for i in range(1, 20)])
            runner = MagicMock(); runner.jobs = [{"job_id": "a", "rule_id": "R01", "provider_id": "openai", "model": "pinned-openai"}]
            runner.attempt_directories.return_value = [root / "attempt_001"]
            runner.read_attempt.return_value = {"status": "invalid_output", "raw_json": {"in_scope_assessment": {"outcome": []}, "ungated_assessment": {"outcome": {}}}, "error": "Bad enum", "counterexamples": []}
            with patch.object(formalisation_runner, "AuditRunner", return_value=runner):
                rows, _, _, _ = report._audit_data(root)
            self.assertEqual((rows[0]["first_in_scope"], rows[0]["first_ungated"], rows[0]["latest_in_scope"]), ("", "", ""))


class ReportingIntegrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.study = Path(temp.name).resolve(); self.case = self.study / "case_assessment"; self.audit = self.study / "formalisation_audit"
        (self.case / "_audit/prepared").mkdir(parents=True); (self.audit / "_audit").mkdir(parents=True)
        self.providers = providers(); self.rule_ids = [f"R{i:02d}" for i in range(1, 20)]
        self.cases = [{"case_id": f"C-{i+1}/20", "case_year": 2020} for i in range(50)]
        self.jobs = [{"job_id": f"case_{i}_{provider['id']}", "case_id": case["case_id"], "case_year": 2020, "rule_ids": self.rule_ids,
                      "provider_id": provider["id"], "provider": provider["provider"], "model": provider["model"]}
                     for i, case in enumerate(self.cases) for provider in self.providers]
        self.results = {job["job_id"]: {"status": "ok", "assessments": [leaf(rule) for rule in self.rule_ids]} for job in self.jobs}
        self.results[self.jobs[0]["job_id"]]["assessments"][0] = leaf("R01", "violation")
        self.rules = [{"base_rule_id": rule, "declare": 'AtLeastOne("A")', "requirement": "A occurs."} for rule in self.rule_ids]
        self.pairs = [{**case, **rule, "pair_id": case["case_id"] + "__" + rule["base_rule_id"],
                       "document_files": [f"{case['case_id'].replace('/', '_')}_01_01.html",
                                          f"{case['case_id'].replace('/', '_')}_01_01.txt"],
                       "document_file_count": 2,
                       "events": [{"ref": "E0001", "activity": "Application (OJ)", "timestamp": "2020-01-01", "source_row": 1, "metadata": {}},
                                  {"ref": "E0002", "activity": "Judgment", "timestamp": "2020-02-01", "source_row": 2, "metadata": {}}]}
                      for case in self.cases for rule in self.rules]
        p.write_table(self.case / "_audit/prepared/selected_cases.csv", self.cases)
        p.write_table(self.case / "_audit/prepared/selected_rules.csv", self.rules)
        p.write_jsonl(self.case / "_audit/prepared/pairs.jsonl", self.pairs)
        p.write_jsonl(self.case / "_audit/prepared/planned_assessments.jsonl", self.jobs)
        p.write_jsonl(self.case / "_audit/prepared/formal_results.jsonl", [{"case_id": case["case_id"], "base_rule_id": rule, "formal_satisfied": 1} for case in self.cases for rule in self.rule_ids])
        p.write_json(self.study / "_audit/config.json", {"providers": self.providers, "case_ids": [case["case_id"] for case in self.cases]})
        p.write_json(self.study / "study.json", {"schema_version": 1, "protocol_id": "critical_phase1_v1", "case_protocol": "critical_v1", "phase": 1,
            "planned_cases": 50, "rules": 19, "planned_case_requests": 150, "planned_audit_requests": 57,
            "case_assessment_dir": "case_assessment", "formalisation_audit_dir": "formalisation_audit", "case_config": "_audit/config.json"})
        self.audit_rows = []
        self.audit_jobs = []
        for rule in self.rule_ids:
            for provider in self.providers:
                row = {key: "" for key in report.AUDIT_FIELDS}
                row.update(rule_id=rule, provider_id=provider["id"], model=provider["model"], job_id="audit_"+rule+provider["id"],
                           status="ok", first_status="ok", declare='AtLeastOne("A")', requirement="A occurs.",
                           first_in_scope="Disagreement", latest_in_scope="Disagreement", in_scope_relation="formula_weaker", in_scope_formula_consistent_D="true",
                           first_ungated="Undetermined", latest_ungated="Undetermined", ungated_relation="unresolved", ungated_formula_consistent_D="false",
                           text_review_status="pending", expert_outcome="", expert_notes="")
                self.audit_rows.append(row)
                self.audit_jobs.append({"job_id": row["job_id"], "provider_id": provider["id"], "provider": provider["provider"], "model": provider["model"], "complete": True})
        def ledger(jobs):
            return [{**job, "call_attempted": True, "input_tokens": 100, "output_tokens": 20, "total_tokens": 120, "elapsed_seconds": 1} for job in jobs]
        self.case_jobs = [{**job, "complete": True} for job in self.jobs]
        self.case_ledger, self.audit_ledger = ledger(self.case_jobs), ledger(self.audit_jobs)
        self.case_mock = patch.object(report, "_case_archive", return_value=(self.results, self.results, self.case_ledger, self.case_jobs))
        self.audit_mock = patch.object(report, "_audit_data", return_value=(self.audit_rows, self.audit_ledger, self.audit_jobs, []))
        def workbook(staging, *args):
            (staging / "formalisation_results.xlsx").write_bytes(b"MOCK NATIVE WORKBOOK")
            (staging / "previews").mkdir()
        self.book_mock = patch.object(report, "_workbook", side_effect=workbook)
        for tool in (self.case_mock, self.audit_mock, self.book_mock): tool.start(); self.addCleanup(tool.stop)

    def read(self, name):
        with (self.study / name).open(encoding="utf-8-sig", newline="") as stream: return list(csv.DictReader(stream))

    def test_full_50_150_57_outputs_and_four_blank_independent_expert_copies(self):
        native = self.case / "expert_review/expert_1.csv"; native.parent.mkdir(); native.write_text("human native case annotation")
        # A public native CSV is deliberately stale; the parent must use verified archived leaves.
        p.write_table(self.case / "case_results.csv", [{"case_id": case["case_id"], "provider_id": provider["id"], "n_completed": 19, "violation": "no", "status": "completed"} for case in self.cases for provider in self.providers])
        state = report.write_critical_outputs(self.study)
        self.assertEqual((state["case_requests_complete"], state["audit_requests_complete"]), (150, 57))
        self.assertEqual(len(self.read("case_comparison.csv")), 50)
        self.assertEqual(self.read("case_comparison.csv")[0]["openai_case_flag"], "yes")
        self.assertEqual(len(self.read("formalisation_results.csv")), 57)
        for number in (1, 2):
            for extension in ("csv", "xlsx"):
                self.assertEqual((self.study / f"expert_review/expert_{number}_audit.{extension}").read_bytes(), (self.study / f"formalisation_results.{extension}").read_bytes())
            self.assertTrue(all(row["expert_outcome"] == row["expert_notes"] == "" for row in self.read(f"expert_review/expert_{number}_audit.csv")))
        self.assertEqual(native.read_text(), "human native case annotation")
        combined = [row for row in self.read("study_resources.csv") if row["stage"] == "combined"]
        self.assertEqual(len(combined), 3)
        self.assertTrue(all(int(row["planned_requests"]) == int(row["retained_attempts"]) == 69 for row in combined))
        self.assertEqual(sum(int(row["recorded_api_calls"]) for row in combined), 207)
        statistics = p.read_json(self.study / "event_log_statistics.json")
        self.assertEqual((statistics["n_cases"], statistics["n_events"]), (50, 100))
        self.assertEqual(statistics["n_documents"], 50)
        self.assertEqual(statistics["mean_documents_per_case"], 1)
        self.assertEqual(statistics["n_document_files"], 100)
        for name in report.EVENT_LOG_FILES:
            self.assertEqual(state["published_sha256"][name], p.sha256_file(self.study / name))
        text = (self.study / "study_report.txt").read_text()
        self.assertIn("EVENT-LOG CHARACTERISTICS", text)
        self.assertIn("documents", text.lower())
        self.assertIn(report.format_event_log_statistics(statistics), text)
        self.assertTrue(self.read("event_log_statistics.csv"))
        self.assertTrue((self.study / "event_log_statistics.txt").read_text())

    def test_report_statistics_are_optional_for_existing_callers(self):
        text = report._report(False, [], [], [], [], [])
        self.assertIn("CASE ASSESSMENTS", text)
        self.assertNotIn("EVENT-LOG CHARACTERISTICS", text)

    def test_existing_publication_can_add_statistics_without_rewriting_frozen_inputs(self):
        state = report.write_critical_outputs(self.study)
        frozen = {path: path.read_bytes() for path in (self.case / "_audit/prepared").iterdir() if path.is_file()}
        for name in report.EVENT_LOG_FILES:
            (self.study / name).unlink()
            state["published_sha256"].pop(name)
        p.write_json(self.study / "_audit/critical_report_state.json", state)
        updated = report.write_critical_outputs(self.study)
        for name in report.EVENT_LOG_FILES:
            self.assertEqual(updated["published_sha256"][name], p.sha256_file(self.study / name))
        self.assertTrue(all(path.read_bytes() == value for path, value in frozen.items()))

    def test_untracked_statistics_are_preserved_during_old_publication_upgrade(self):
        state = report.write_critical_outputs(self.study)
        for name in report.EVENT_LOG_FILES:
            (self.study / name).unlink()
            state["published_sha256"].pop(name)
        p.write_json(self.study / "_audit/critical_report_state.json", state)
        target = self.study / "event_log_statistics.txt"
        target.write_text("human event-log analysis")
        with self.assertRaisesRegex(ValueError, "untracked.*event_log_statistics.txt"):
            report.write_critical_outputs(self.study)
        self.assertEqual(target.read_text(), "human event-log analysis")

    def test_statistics_case_count_mismatch_does_not_publish_outputs(self):
        write_statistics = report.write_event_log_statistics
        def wrong_count(output, records):
            stats = write_statistics(output, records)
            return {**stats, "n_cases": 49}
        with patch.object(report, "write_event_log_statistics", side_effect=wrong_count):
            with self.assertRaisesRegex(ValueError, "statistics.*frozen case cohort"):
                report.write_critical_outputs(self.study)
        self.assertFalse((self.study / "study_report.txt").exists())
        self.assertTrue(all(not (self.study / name).exists() for name in report.EVENT_LOG_FILES))

    def test_frozen_event_records_must_match_selected_case_identifiers(self):
        self.pairs[0]["case_id"] = "C-9999/20"
        p.write_jsonl(self.case / "_audit/prepared/pairs.jsonl", self.pairs)
        with self.assertRaisesRegex(ValueError, "event-log cohort differs"):
            report.write_critical_outputs(self.study)
        self.assertFalse((self.study / "study_report.txt").exists())

    def test_expert_edits_survive_regeneration_and_repeated_resume(self):
        report.write_critical_outputs(self.study)
        target = self.study / "expert_review/expert_1_audit.csv"; target.write_text("human expert rating")
        workbook = self.study / "expert_review/expert_1_audit.xlsx"; workbook.write_bytes(b"human workbook rating")
        old_base = (self.study / "formalisation_results.csv").read_bytes()
        self.audit_rows[0].update(latest_in_scope="Undetermined", in_scope_relation="unresolved", in_scope_formula_consistent_D="false")
        for _ in range(2):
            report.write_critical_outputs(self.study)
            self.assertEqual(target.read_text(), "human expert rating")
            self.assertEqual(workbook.read_bytes(), b"human workbook rating")
            self.assertEqual((self.study / "expert_review/expert_2_audit.csv").read_bytes(), (self.study / "formalisation_results.csv").read_bytes())
        self.assertNotEqual((self.study / "formalisation_results.csv").read_bytes(), old_base)

    def test_machine_output_edits_still_block_regeneration(self):
        report.write_critical_outputs(self.study)
        target = self.study / "study_report.txt"; target.write_text("human research manuscript")
        with self.assertRaisesRegex(ValueError, "manually edited|untracked"):
            report.write_critical_outputs(self.study)
        self.assertEqual(target.read_text(), "human research manuscript")

    def test_partial_missing_responses_are_not_fabricated_undetermined(self):
        self.results.clear()
        for job in self.case_jobs: job["complete"] = False
        self.case_ledger.clear()
        state = report.write_critical_outputs(self.study)
        self.assertIn("partial", state["status"])
        self.assertEqual(state["case_requests_complete"], 0)
        self.assertTrue(all(row["openai_case_flag"] == "unavailable" for row in self.read("case_comparison.csv")))
        text = (self.study / "study_report.txt").read_text()
        self.assertIn("operationally unavailable 950", text)
        self.assertIn("different denominators", text)


if __name__ == "__main__":
    unittest.main()
