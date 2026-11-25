"""Check descriptive article views against archived operational and legal states."""

from __future__ import annotations

import copy
import csv
import json
import tempfile
import unittest
from pathlib import Path

from curia_assessment.article_reporting import write_article_outputs
from curia_assessment.event_log_statistics import summarise_event_log


class ArticleReportingTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name)
        self.config = {"max_output_tokens": 16000, "country": "EN", "min_events": 5,
                       "year_start": 2024, "year_end": 2025, "cases_per_year": 1,
                       "sampling_seed": 71, "case_ids": None, "repetitions": 1}
        self.pairs = {}
        self.jobs = []
        for case_id in ("C-1/24", "C-2/25"):
            pair_ids = []
            for rule_id in ("R01", "R02", "R03"):
                pair_id = case_id + "__" + rule_id
                pair_ids.append(pair_id)
                self.pairs[pair_id] = {"pair_id": pair_id, "case_id": case_id,
                                       "base_rule_id": rule_id, "variant_id": rule_id,
                                       "declare": 'AtLeastOne("A")'}
            for provider in ("alpha", "beta", "gamma"):
                self.jobs.append({"job_id": provider + "__" + case_id, "provider_id": provider,
                                  "provider": provider, "model": provider + "-pinned", "case_id": case_id,
                                  "condition": "A", "run": 1, "pair_ids": pair_ids,
                                  "rule_ids": ["R01", "R02", "R03"]})
        self.formal = [{"case_id": pair["case_id"], "base_rule_id": pair["base_rule_id"],
                        "formal_satisfied": int(pair["base_rule_id"] == "R01" or pair_id == "C-1/24__R03")}
                       for pair_id, pair in self.pairs.items()]

    @staticmethod
    def attempt(outcomes=("compliance", "compliance", "compliance"), *, status="ok", limit=16000):
        return {"status": status, "actual_output_token_limit": limit,
                "assessments": [{"rule_id": rule_id, "status": "ok", "outcome": outcome}
                                for rule_id, outcome in zip(("R01", "R02", "R03"), outcomes)]}

    def write(self, histories, *, formal=True, jobs=None):
        return write_article_outputs(self.output, config=self.config, pairs=self.pairs,
                                     jobs=self.jobs if jobs is None else jobs, attempts_by_job=histories,
                                     formal_rows=self.formal if formal else None)

    def rows(self):
        with (self.output / "article_results.csv").open(encoding="utf-8-sig", newline="") as stream:
            return {(row["provider_id"], row["analysis_view"]): row for row in csv.DictReader(stream)}

    def test_each_phase_article_includes_observed_log_characteristics(self):
        stats = summarise_event_log([
            {"case_id": case_id, "document_files": ["document.html", "document.txt"], "events": [
                {"activity": "A", "timestamp": f"{year}-01-01"},
                {"activity": "B", "timestamp": f"{year}-01-03"},
            ]}
            for case_id, year in (("C-1/24", 2024), ("C-2/25", 2025))
        ])
        for phase in ("A", "B"):
            with self.subTest(phase=phase):
                config = {**self.config, "conditions": [phase]}
                jobs = [{**job, "condition": phase} for job in self.jobs]
                write_article_outputs(self.output, config=config, pairs=self.pairs,
                                      jobs=jobs, attempts_by_job={},
                                      event_log_statistics=stats)
                text = (self.output / "article_summary.txt").read_text(encoding="utf-8")
                self.assertIn("EVENT-LOG CHARACTERISTICS", text)
                self.assertIn("Total recorded events: 4", text)
                self.assertIn("Distinct activity-sequence variants: 1", text)
                self.assertIn("2.000 days", text)
                self.assertIn("Mean associated documents per case: 1.000", text)

    def test_comparison_has_three_categories_and_only_three_legal_outcome_counts(self):
        self.write({
            "alpha__C-1/24": [self.attempt(("compliance", "violation", "undetermined"))],
            "alpha__C-2/25": [self.attempt(("compliance", "compliance", "undetermined"))],
        })
        row = self.rows()["alpha", "latest"]
        self.assertEqual(row["phase"], "1")
        self.assertEqual(row["n_comparison_agreements"], "3")
        self.assertEqual(row["n_comparison_disagreements"], "1")
        self.assertEqual(row["n_comparison_undetermined"], "2")
        self.assertNotIn("n_not_applicable", row)
        self.assertEqual(row["n_undetermined"], "2")
        self.assertEqual(row["formal_agreement_denominator"], "4")
        self.assertEqual(float(row["formal_agreement_rate"]), 3 / 4)
        text = (self.output / "article_summary.txt").read_text()
        results = text.split("Results, latest responses.", 1)[1].split("Limitations.", 1)[0]
        self.assertIn("Agreement: 3, Disagreement: 1 and Undetermined: 2", results)
        self.assertNotIn("not applicable", results)
        self.assertNotIn("not_applicable", results)
        self.assertNotIn("not applicable", text)
        self.assertNotIn("not_applicable", text)

    def test_unexpected_not_applicable_is_unavailable_not_legal_undetermined_or_compliance(self):
        attempt = self.attempt(("compliance", "not_applicable", "compliance"))
        original = copy.deepcopy(attempt)
        self.write({"alpha__C-1/24": [attempt]})
        row = self.rows()["alpha", "latest"]
        self.assertEqual(row["n_valid_rule_judgements"], "2")
        self.assertEqual(row["n_compliance"], "2")
        self.assertEqual(row["n_undetermined"], "0")
        self.assertEqual(row["n_unavailable_rule_judgements"], "4")
        self.assertEqual(row["n_cases_with_complete_valid_response"], "0")
        self.assertEqual(row["n_cases_with_semantic_undetermined"], "0")
        self.assertEqual(row["n_technical_failures"], "1")
        self.assertEqual(row["case_flags_no"], "0")
        self.assertEqual(row["case_flags_undetermined"], "2")
        self.assertEqual(row["n_comparison_agreements"], "2")
        self.assertEqual(row["n_comparison_undetermined"], "0")
        self.assertEqual(row["formal_agreement_denominator"], "2")
        self.assertNotIn("n_not_applicable", row)
        self.assertEqual(attempt, original)

    def test_missing_reference_is_comparison_undetermined_but_failed_requests_are_not(self):
        self.write({
            "alpha__C-1/24": [self.attempt()],
            "alpha__C-2/25": [{"status": "api_error", "assessments": [], "actual_output_token_limit": 16000}],
        }, formal=False)
        row = self.rows()["alpha", "latest"]
        self.assertEqual(row["n_comparison_agreements"], "0")
        self.assertEqual(row["n_comparison_disagreements"], "0")
        self.assertEqual(row["n_comparison_undetermined"], "3")
        self.assertEqual(row["n_valid_rule_judgements"], "3")
        self.assertEqual(row["n_unavailable_rule_judgements"], "3")
        self.assertEqual(row["n_undetermined"], "0")
        self.assertEqual(row["formal_agreement_rate"], "")

    def _set_annual_sample(self, counts: dict[int, int]) -> None:
        self.pairs, self.jobs = {}, []
        rule_ids = [f"R{number:02d}" for number in range(1, 20)]
        for year, count in counts.items():
            for number in range(1, count + 1):
                case_id = f"C-{number}/{year % 100:02d}"
                pair_ids = []
                for rule_id in rule_ids:
                    pair_id = case_id + "__" + rule_id
                    pair_ids.append(pair_id)
                    self.pairs[pair_id] = {"pair_id": pair_id, "case_id": case_id,
                                           "base_rule_id": rule_id, "variant_id": rule_id,
                                           "declare": 'AtLeastOne("A")'}
                for provider in ("alpha", "beta", "gamma"):
                    self.jobs.append({"job_id": provider + "__" + case_id, "provider_id": provider,
                                      "provider": provider, "model": provider + "-pinned", "case_id": case_id,
                                      "condition": "A", "run": 1, "pair_ids": pair_ids, "rule_ids": rule_ids})

    def test_five_per_year_reports_all_nine_compensating_cases_and_scaled_denominators(self) -> None:
        self.config.update(year_start=2016, year_end=2026, cases_per_year=5,
                           article_report_view="latest", max_output_tokens=32000)
        allocation = {year: 6 for year in range(2016, 2025)}
        allocation.update({2025: 1, 2026: 0})
        self._set_annual_sample(allocation)
        self.write({}, formal=False)
        rows = self.rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual(sum(int(row["n_case_requests_planned"]) for row in rows.values()), 165)
        self.assertEqual(sum(int(row["n_rule_judgements_planned"]) for row in rows.values()), 3135)
        for row in rows.values():
            self.assertEqual(row["n_cases_planned"], "55")
            self.assertEqual(row["n_rule_judgements_planned"], "1045")
        text = (self.output / "article_summary.txt").read_text()
        self.assertIn("Interim results: 0/165", text)
        self.assertIn("target of 55 distinct cases", text)
        self.assertIn("base annual selection contained 46 cases", text)
        self.assertIn("Unfilled base-quota places totalled 9", text)
        self.assertIn("2025: 4; 2026: 5", text)
        self.assertIn("Compensation added 9 cases", text)
        self.assertIn("; ".join(f"{year}: 1" for year in range(2016, 2025)), text)
        self.assertIn("Final selected cases by CaseID year were "
                      + "; ".join(f"{year}: {count}" for year, count in allocation.items()), text)
        self.assertIn("final sample contained 55/55 target cases, leaving an overall shortfall of 0", text)
        self.assertNotIn("an additional case from 2024 compensated for that annual deficit", text)

    def test_exhausted_pool_reports_partial_compensation_and_remaining_shortfall(self) -> None:
        self.config.update(year_start=2024, year_end=2026, cases_per_year=5,
                           article_report_view="latest", max_output_tokens=32000)
        self._set_annual_sample({2024: 6, 2025: 1, 2026: 0})
        self.write({}, formal=False)
        text = (self.output / "article_summary.txt").read_text()
        self.assertIn("target of 15 distinct cases", text)
        self.assertIn("base annual selection contained 6 cases", text)
        self.assertIn("Unfilled base-quota places totalled 9", text)
        self.assertIn("Compensation added 1 case", text)
        self.assertIn("2024: 1", text)
        self.assertIn("final sample contained 7/15 target cases, leaving an overall shortfall of 8", text)

    def test_explicit_fifty_case_target_distinguishes_annual_deficits_from_overall_shortfall(self) -> None:
        self.config.update(year_start=2016, year_end=2026, cases_per_year=5, total_cases=50,
                           article_report_view="latest", max_output_tokens=32000)
        allocation = {year: 5 for year in range(2016, 2021)}
        allocation.update({year: 6 for year in range(2021, 2025)})
        allocation.update({2025: 1, 2026: 0})
        self._set_annual_sample(allocation)
        self.write({}, formal=False)
        rows = self.rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual(sum(int(row["n_case_requests_planned"]) for row in rows.values()), 150)
        self.assertEqual(sum(int(row["n_rule_judgements_planned"]) for row in rows.values()), 2850)
        self.assertTrue(all(row["n_cases_planned"] == "50" and row["n_rule_judgements_planned"] == "950"
                            for row in rows.values()))
        text = (self.output / "article_summary.txt").read_text()
        self.assertIn("Interim results: 0/150", text)
        self.assertIn("nominal total of 55 cases", text)
        self.assertIn("explicit total_cases setting specified an overall target of 50 distinct cases", text)
        self.assertIn("base annual selection contained 46 cases", text)
        self.assertIn("Unfilled base-quota places totalled 9", text)
        self.assertIn("2025: 4; 2026: 5", text)
        self.assertIn("Compensation added 4 cases", text)
        self.assertIn("; ".join(f"{year}: 1" for year in range(2021, 2025)), text)
        self.assertIn("Final selected cases by CaseID year were "
                      + "; ".join(f"{year}: {count}" for year, count in allocation.items()), text)
        self.assertIn("final sample contained 50/50 target cases, leaving an overall shortfall of 0", text)
        self.assertNotIn("50/55 target cases", text)

    def test_invalid_overall_target_is_rejected_before_writing_article_files(self) -> None:
        for value in (0, -1, True, 50.0, "50"):
            with self.subTest(value=value):
                self.config["total_cases"] = value
                with self.assertRaisesRegex(ValueError, "total_cases"):
                    self.write({})
                self.assertFalse((self.output / "article_results.csv").exists())
                self.assertFalse((self.output / "article_summary.txt").exists())

    def test_pending_preparation_has_six_honest_rows_and_no_legal_zero_imputation(self) -> None:
        result = self.write({})
        self.assertEqual(set(result), {"article_results", "article_summary"})
        rows = self.rows()
        self.assertEqual(len(rows), 6)
        for row in rows.values():
            self.assertEqual(row["n_cases_planned"], "2")
            self.assertEqual(row["n_cases_with_complete_valid_response"], "0")
            self.assertEqual(row["n_rule_judgements_planned"], "6")
            self.assertEqual(row["n_api_attempts"], "0")
            self.assertEqual(row["n_technical_failures"], "0")
            self.assertEqual(row["n_valid_rule_judgements"], "0")
            self.assertEqual(row["n_unavailable_rule_judgements"], "6")
            self.assertEqual(row["n_undetermined"], "0")
            self.assertEqual(row["n_cases_with_unavailable_rule_judgements"], "2")
            self.assertEqual(row["n_cases_with_semantic_undetermined"], "0")
            self.assertEqual(row["case_flags_undetermined"], "2")
            self.assertEqual(row["case_flags_yes"], row["case_flags_no"])
            self.assertEqual(row["formal_agreement_denominator"], "0")
            self.assertEqual(row["formal_agreement_rate"], "")
        text = (self.output / "article_summary.txt").read_text()
        self.assertIn("Interim results: 0/6", text)
        self.assertIn("planned rows are not model findings", text)
        self.assertIn("Case documents were not supplied", text)
        self.assertIn("two experts has not been incorporated", text)
        self.assertIn("not establish", text)
        self.assertIn("complete schema-valid response", text)
        self.assertIn("rule judgements met output-validation requirements", text)
        self.assertIn("Passing these checks does not establish", text)

    def test_mixed_first_attempt_and_recovery_keep_distinct_counts_and_denominators(self) -> None:
        first = self.attempt(("violation", "compliance", "unrecognised_outcome"), status="invalid_output")
        histories = {
            "alpha__C-1/24": [first, self.attempt(("compliance", "undetermined", "compliance"), limit=32000)],
            "alpha__C-2/25": [{"status": "api_error", "assessments": [], "actual_output_token_limit": 16000},
                               self.attempt(("compliance", "undetermined", "violation"), limit=32000)],
            "gamma__C-1/24": [self.attempt()], "gamma__C-2/25": [self.attempt()],
        }
        untouched = copy.deepcopy(histories)
        self.write(histories)
        rows = self.rows()
        baseline = rows["alpha", "first_attempt"]
        latest = rows["alpha", "latest"]
        self.assertEqual(baseline["n_api_attempts"], "2")
        self.assertEqual(baseline["n_recovery_attempts"], "0")
        self.assertEqual(baseline["n_valid_rule_judgements"], "2")
        self.assertEqual(baseline["n_unavailable_rule_judgements"], "4")
        self.assertEqual(baseline["n_undetermined"], "0")
        self.assertNotIn("n_not_applicable", baseline)
        self.assertEqual(baseline["case_flags_yes"], "1")
        self.assertEqual(baseline["case_flags_no"], "0")
        self.assertEqual(baseline["case_flags_undetermined"], "1")
        self.assertEqual(baseline["formal_agreement_numerator"], "0")
        self.assertEqual(baseline["formal_agreement_denominator"], "2")
        self.assertAlmostEqual(float(baseline["formal_comparison_coverage_percent"]), 200 / 6)
        self.assertEqual(baseline["n_technical_failures"], "2")
        self.assertEqual(float(baseline["formal_agreement_rate"]), 0.0)
        self.assertAlmostEqual(float(baseline["valid_output_coverage_percent"]), 100 / 3)
        self.assertNotIn("semantic_coverage_percent", baseline)
        self.assertEqual(baseline["n_cases_with_unavailable_rule_judgements"], "2")
        self.assertEqual(baseline["n_cases_with_semantic_undetermined"], "0")
        self.assertEqual(latest["n_api_attempts"], "4")
        self.assertEqual(latest["n_recovery_attempts"], "2")
        self.assertEqual(latest["n_cases_with_recovery"], "2")
        self.assertEqual(latest["n_attempts_above_baseline_output_limit"], "2")
        self.assertEqual(json.loads(latest["observed_output_token_limits"]), [16000, 32000])
        self.assertEqual(latest["n_cases_with_complete_valid_response"], "2")
        self.assertEqual(latest["n_valid_rule_judgements"], "6")
        self.assertEqual(latest["n_compliance"], "3")
        self.assertEqual(latest["n_violation"], "1")
        self.assertNotIn("n_not_applicable", latest)
        self.assertEqual(latest["n_undetermined"], "2")
        self.assertEqual(latest["n_unavailable_rule_judgements"], "0")
        self.assertEqual(latest["n_technical_failures"], "0")
        self.assertEqual(latest["n_cases_with_unavailable_rule_judgements"], "0")
        self.assertEqual(latest["n_cases_with_semantic_undetermined"], "2")
        self.assertEqual(latest["case_flags_yes"], "1")
        self.assertEqual(latest["case_flags_undetermined"], "1")
        self.assertEqual(latest["formal_agreement_numerator"], "4")
        self.assertEqual(latest["formal_agreement_denominator"], "4")
        self.assertAlmostEqual(float(latest["formal_comparison_coverage_percent"]), 400 / 6)
        self.assertEqual(float(latest["formal_agreement_rate"]), 1.0)
        self.assertEqual(rows["gamma", "latest"]["case_flags_no"], "2")
        self.assertEqual(rows["beta", "latest"]["n_api_attempts"], "0")
        self.assertEqual(histories, untouched)
        text = (self.output / "article_summary.txt").read_text()
        self.assertIn("Results, first attempts", text)
        self.assertIn("Results, latest responses", text)
        self.assertIn("sensitivity view", text)
        self.assertIn("2 recovery attempts across 2 cases", text)

    def test_later_failure_does_not_restore_a_more_favourable_earlier_response(self) -> None:
        histories = {"gamma__C-1/24": [self.attempt(), {"status": "api_error", "assessments": [],
                                                      "actual_output_token_limit": 32000}],
                     "gamma__C-2/25": [self.attempt()]}
        self.write(histories)
        rows = self.rows()
        self.assertEqual(rows["gamma", "first_attempt"]["n_valid_rule_judgements"], "6")
        self.assertEqual(rows["gamma", "latest"]["n_valid_rule_judgements"], "3")
        self.assertEqual(rows["gamma", "latest"]["n_unavailable_rule_judgements"], "3")
        self.assertEqual(rows["gamma", "latest"]["case_flags_no"], "1")
        self.assertEqual(rows["gamma", "latest"]["case_flags_undetermined"], "1")
        self.assertEqual(rows["gamma", "latest"]["n_api_attempts"], "3")
        self.assertEqual(rows["gamma", "latest"]["n_technical_failures"], "1")

    def test_missing_formal_reference_and_unknown_output_limit_remain_unavailable(self) -> None:
        attempt = self.attempt()
        del attempt["actual_output_token_limit"]
        self.write({"gamma__C-1/24": [attempt]}, formal=False)
        row = self.rows()["gamma", "latest"]
        self.assertEqual(row["formal_agreement_numerator"], "")
        self.assertEqual(row["formal_agreement_denominator"], "")
        self.assertEqual(row["formal_agreement_rate"], "")
        self.assertEqual(row["formal_comparison_coverage_percent"], "")
        self.assertEqual(row["n_attempts_with_unknown_output_token_limit"], "1")
        self.assertEqual(json.loads(row["observed_output_token_limits"]), [])
        text = (self.output / "article_summary.txt").read_text()
        self.assertIn("No formal reference was supplied", text)
        self.assertIn("output-token limit was unavailable for 1", text)

    def test_phase_b_responses_do_not_enter_log_only_article_views(self) -> None:
        jobs = copy.deepcopy(self.jobs)
        second_phase = {**jobs[0], "job_id": "phase-b", "condition": "B"}
        jobs.append(second_phase)
        self.write({"phase-b": [self.attempt(("violation", "violation", "violation"))]}, jobs=jobs)
        self.assertEqual(len(self.rows()), 6)
        self.assertTrue(all(row["n_api_attempts"] == "0" for row in self.rows().values()))

    def test_phase_two_reports_only_its_jobs_with_three_outcomes_and_first_latest_views(self) -> None:
        self.config.update(conditions=["B"], max_output_tokens=32000)
        phase_two = [{**job, "job_id": "phase2__" + job["job_id"], "condition": "B"} for job in self.jobs]
        histories = {job["job_id"]: [self.attempt(("violation", "violation", "violation"), limit=32000)]
                     for job in self.jobs}
        histories.update({
            "phase2__alpha__C-1/24": [
                {"status": "api_error", "assessments": [], "actual_output_token_limit": 32000},
                self.attempt(("compliance", "violation", "undetermined"), limit=32000),
            ],
            "phase2__alpha__C-2/25": [self.attempt(("compliance", "compliance", "undetermined"), limit=32000)],
        })
        self.write(histories, jobs=self.jobs + phase_two)
        rows = self.rows()
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(row["phase"] == "2" and row["n_cases_planned"] == "2" for row in rows.values()))
        first, latest = rows["alpha", "first_attempt"], rows["alpha", "latest"]
        self.assertEqual((first["n_api_attempts"], first["n_technical_failures"], first["n_valid_rule_judgements"]),
                         ("2", "1", "3"))
        self.assertEqual((latest["n_api_attempts"], latest["n_recovery_attempts"], latest["n_technical_failures"]),
                         ("3", "1", "0"))
        self.assertEqual((latest["n_compliance"], latest["n_violation"], latest["n_undetermined"]), ("3", "1", "2"))
        self.assertEqual((latest["n_comparison_agreements"], latest["n_comparison_disagreements"],
                          latest["n_comparison_undetermined"]), ("3", "1", "2"))
        self.assertEqual(latest["formal_agreement_denominator"], "4")
        self.assertEqual(float(latest["formal_agreement_rate"]), 3 / 4)
        self.assertEqual(rows["beta", "latest"]["n_api_attempts"], "0")
        self.assertNotIn("n_not_applicable", latest)
        text = (self.output / "article_summary.txt").read_text()
        self.assertIn("phase-2 allocation planned 6 case requests and 18 rule judgements", text)
        self.assertIn("same cases, textual requirements, original DECLARE formulas and ordered event traces as phase 1", text)
        self.assertIn("full frozen case-document packets", text)
        self.assertIn("No formal-checker results or model answers from phase 1 were supplied", text)
        self.assertIn("Results, first attempts", text)
        self.assertIn("Results, latest responses", text)
        self.assertNotIn("Case documents were not supplied", text)
        self.assertNotIn("phase-A", text)
        self.assertNotIn("not applicable", text)
        self.assertNotIn("not_applicable", text)
        self.assertNotIn("quoted legal sources", text)

        self.config["article_report_view"] = "latest"
        self.write(histories, jobs=self.jobs + phase_two)
        self.assertEqual(len(self.rows()), 3)
        self.assertTrue(all(row["phase"] == "2" and row["analysis_view"] == "latest" for row in self.rows().values()))
        self.assertNotIn("Results, first attempts", (self.output / "article_summary.txt").read_text())

    def test_article_methods_use_requirements_without_exporting_legacy_quotations(self) -> None:
        for pair in self.pairs.values():
            pair.update(source="LEGACY_QUOTATION_SENTINEL", legal_source="LEGACY_LEGAL_SOURCE_SENTINEL")
        for phase in ("A", "B"):
            with self.subTest(phase=phase):
                self.config["conditions"] = [phase]
                jobs = [{**job, "condition": phase} for job in self.jobs]
                self.write({}, jobs=jobs)
                text = (self.output / "article_summary.txt").read_text()
                self.assertIn("all selected textual requirements", text)
                self.assertIn("their original DECLARE formulas", text)
                self.assertNotIn("legal sources", text)
                self.assertNotIn("LEGACY_QUOTATION_SENTINEL", text)
                self.assertNotIn("LEGACY_LEGAL_SOURCE_SENTINEL", text)

    def test_article_requires_one_valid_configured_phase_and_jobs_for_that_phase(self) -> None:
        for conditions in ([], ["A", "B"], ["C"], "A", None):
            with self.subTest(conditions=conditions):
                self.config["conditions"] = conditions
                with self.assertRaisesRegex(ValueError, "exactly one configured phase"):
                    self.write({})
                self.assertFalse((self.output / "article_results.csv").exists())
        self.config["conditions"] = ["B"]
        with self.assertRaisesRegex(ValueError, "planned phase-2 case requests"):
            self.write({})

    def test_phase_two_latest_view_keeps_uniform_output_budget_guard(self) -> None:
        self.config.update(conditions=["B"], article_report_view="latest", max_output_tokens=32000)
        phase_two = [{**job, "condition": "B"} for job in self.jobs]
        with self.assertRaisesRegex(ValueError, "output-budget-consistent cohort"):
            self.write({"alpha__C-1/24": [self.attempt(limit=16000)]}, jobs=phase_two)
        self.assertFalse((self.output / "article_results.csv").exists())

    def test_duplicate_rule_answers_are_unavailable_instead_of_double_counted(self) -> None:
        attempt = self.attempt()
        attempt["assessments"].append(copy.deepcopy(attempt["assessments"][0]))
        self.write({"alpha__C-1/24": [attempt]})
        row = self.rows()["alpha", "first_attempt"]
        self.assertEqual(row["n_valid_rule_judgements"], "2")
        self.assertEqual(row["n_cases_with_complete_valid_response"], "0")
        self.assertEqual(row["case_flags_no"], "0")
        self.assertEqual(row["case_flags_undetermined"], "2")

    def test_operational_gaps_and_legal_undetermined_are_separate_overlapping_case_counts(self) -> None:
        attempt = self.attempt(("undetermined", "unrecognised_outcome", "compliance"), status="invalid_output")
        self.write({"alpha__C-1/24": [attempt]})
        row = self.rows()["alpha", "first_attempt"]
        self.assertEqual(row["n_cases_with_unavailable_rule_judgements"], "2")
        self.assertEqual(row["n_cases_with_semantic_undetermined"], "1")
        self.assertEqual(row["n_undetermined"], "1")
        self.assertEqual(row["n_unavailable_rule_judgements"], "4")
        self.assertEqual(row["case_flags_undetermined"], "2")

    def test_latest_only_publication_uses_one_uniform_budget_cohort_and_counts_all_its_attempts(self) -> None:
        self.config.update(article_report_view="latest", max_output_tokens=32000)
        histories = {job["job_id"]: [self.attempt(limit=32000)] for job in self.jobs}
        histories["alpha__C-1/24"] = [
            {"status": "api_error", "assessments": [], "actual_output_token_limit": 32000},
            self.attempt(("compliance", "undetermined", "compliance"), limit=32000),
        ]
        histories["alpha__C-2/25"] = [self.attempt(("compliance", "undetermined", "violation"), limit=32000)]
        self.write(histories)
        rows = self.rows()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(key[1] == "latest" for key in rows))
        for row in rows.values():
            self.assertEqual(row["baseline_output_token_limit"], "32000")
            self.assertEqual(json.loads(row["observed_output_token_limits"]), [32000])
            self.assertEqual(row["n_technical_failures"], "0")
            self.assertEqual(row["n_unavailable_rule_judgements"], "0")
        alpha = rows["alpha", "latest"]
        self.assertEqual(alpha["n_api_attempts"], "3")
        self.assertEqual(alpha["n_recovery_attempts"], "1")
        self.assertEqual(alpha["n_undetermined"], "2")
        self.assertEqual(alpha["formal_agreement_numerator"], "4")
        self.assertEqual(alpha["formal_agreement_denominator"], "4")
        self.assertEqual(float(alpha["formal_agreement_rate"]), 1)
        self.assertAlmostEqual(float(alpha["formal_comparison_coverage_percent"]), 400 / 6)
        text = (self.output / "article_summary.txt").read_text()
        self.assertNotIn("first-attempt", text)
        self.assertNotIn("Results, first", text)
        self.assertNotIn("16000", text)
        self.assertIn("same configured output-token limit of 32000", text)
        self.assertIn("Formal-comparison coverage was 4/6 planned rule judgements", text)
        self.assertIn("agreement is conditional on that denominator", text)

    def test_latest_only_publication_rejects_known_superseded_budgets_and_preserves_unknowns(self) -> None:
        self.config.update(article_report_view="latest", max_output_tokens=32000)
        with self.assertRaisesRegex(ValueError, "output-budget-consistent cohort"):
            self.write({"alpha__C-1/24": [self.attempt(limit=16000)]})
        self.assertFalse((self.output / "article_results.csv").exists())
        unknown = self.attempt(limit=32000)
        del unknown["actual_output_token_limit"]
        self.write({"alpha__C-1/24": [unknown]})
        row = self.rows()["alpha", "latest"]
        self.assertEqual(row["n_attempts_with_unknown_output_token_limit"], "1")
        self.assertEqual(json.loads(row["observed_output_token_limits"]), [])

    def test_latest_only_pending_publication_remains_honest(self) -> None:
        self.config.update(article_report_view="latest", max_output_tokens=32000)
        self.write({})
        self.assertEqual(len(self.rows()), 3)
        self.assertTrue(all(row["n_technical_failures"] == "0" and row["n_unavailable_rule_judgements"] == "6"
                            for row in self.rows().values()))
        text = (self.output / "article_summary.txt").read_text()
        self.assertIn("Interim results: 0/6", text)
        self.assertNotIn("Results, first", text)


if __name__ == "__main__":
    unittest.main()
