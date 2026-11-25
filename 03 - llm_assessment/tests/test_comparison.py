"""Meaningful checks of formal agreement, missingness and variant denominators."""

from __future__ import annotations

import copy
import unittest

from curia_assessment.comparison import compare_rows, summarise_comparisons


def prediction(case_id="C-1/26", rule_id="R01", *, variant_id=None, outcome="compliance",
               status="ok", run=1, **extra):
    return {
        "provider": "test",
        "model": "test-model",
        "condition": "A",
        "run": run,
        "case_id": case_id,
        "base_rule_id": rule_id,
        "variant_id": variant_id or rule_id,
        "status": status,
        "outcome": outcome,
        **extra,
    }


def formal(case_id="C-1/26", rule_id="R01", *, satisfied=True, vacuous=False,
           activation="active", aliases=False):
    return {
        ("CaseID" if aliases else "case_id"): case_id,
        ("rule_id" if aliases else "base_rule_id"): rule_id,
        "formal_satisfied": int(satisfied),
        "formal_outcome": "vacuously_satisfied" if vacuous else ("satisfied" if satisfied else "violated"),
        "activation_status": activation,
        "vacuously_satisfied": vacuous,
        "constraint": 'Response("a", "b")',
        "template": "Response",
        "trace_sha256": "trace-hash",
    }


class ComparisonTests(unittest.TestCase):
    def test_binary_agreement_uses_one_as_satisfied(self):
        predictions = [
            prediction("C-1/26", outcome="compliance"),
            prediction("C-2/26", outcome="violation"),
            prediction("C-3/26", outcome="violation"),
            prediction("C-4/26", outcome="compliance"),
        ]
        baseline = [
            formal("C-1/26"),
            formal("C-2/26", satisfied=False),
            formal("C-3/26"),
            formal("C-4/26", satisfied=False),
        ]
        joined = compare_rows(predictions, baseline)
        self.assertEqual([row["comparison_status"] for row in joined],
                         ["agreement", "agreement", "disagreement", "disagreement"])
        self.assertEqual([row["comparison_category"] for row in joined],
                         ["Agreement", "Agreement", "Disagreement", "Disagreement"])
        summary = summarise_comparisons(joined)["model_summary"][0]
        self.assertEqual(summary["n_comparable_binary_decisions"], 4)
        self.assertEqual(summary["agreement_rate"], 1 / 2)

    def test_vacuity_is_satisfied_and_is_retained_as_a_separate_stratum(self):
        joined = compare_rows(
            [prediction("C-1/26"), prediction("C-2/26", outcome="violation")],
            [
                formal("C-1/26", vacuous=True, activation="inactive"),
                formal("C-2/26", vacuous=True, activation="inactive"),
            ],
        )
        self.assertEqual([row["agreement"] for row in joined], [True, False])
        summaries = summarise_comparisons(joined)
        self.assertEqual(summaries["activation_summary"][0]["formal_outcome"], "vacuously_satisfied")
        self.assertEqual(summaries["model_summary"][0]["n_formally_vacuous_assessments"], 2)

    def test_unary_activation_not_applicable_remains_valid_but_legal_label_is_rejected(self):
        joined = compare_rows(
            [
                prediction("C-1/26", outcome="compliance"),
                prediction("C-2/26", outcome="undetermined"),
                prediction("C-3/26", outcome="not_applicable"),
            ],
            [
                formal("C-1/26", activation="not_applicable"),
                formal("C-2/26", activation="not_applicable"),
                formal("C-3/26", activation="not_applicable"),
            ],
        )
        self.assertEqual(joined[0]["comparison_status"], "agreement")
        self.assertEqual(joined[1]["comparison_status"], "undetermined")
        self.assertEqual(joined[1]["comparison_category"], "Undetermined")
        self.assertEqual(joined[2]["comparison_status"], "invalid_outcome")
        self.assertIsNone(joined[2]["comparison_category"])
        self.assertFalse(joined[2]["llm_label_valid"])
        self.assertEqual(joined[2]["outcome"], "not_applicable")
        self.assertTrue(all(row["formal_activation_status"] == "not_applicable" for row in joined))
        summary = summarise_comparisons(joined)["model_summary"][0]
        self.assertEqual(summary["n_valid_outputs"], 2)
        self.assertEqual(summary["n_invalid_outputs"], 1)
        self.assertNotIn("n_not_applicable", summary)
        self.assertNotIn("not_applicable_rate_among_valid_outputs", summary)
        self.assertEqual(summary["n_comparison_undetermined"], 1)
        self.assertEqual(summary["n_comparable_binary_decisions"], 1)
        self.assertEqual(summary["agreement_rate"], 1)

    def test_non_binary_outputs_and_pending_jobs_remain_in_coverage_denominators(self):
        predictions = [
            prediction("C-1/26"),
            prediction("C-2/26", status="pending", outcome=None),
            prediction("C-3/26", status="error", outcome=None),
            prediction("C-4/26", outcome="undetermined"),
            prediction("C-5/26", outcome="not_applicable"),
        ]
        joined = compare_rows(predictions, [formal(row["case_id"]) for row in predictions])
        summary = summarise_comparisons(joined)["model_summary"][0]
        self.assertEqual(summary["n_planned_assessments"], 5)
        self.assertEqual(summary["n_recorded_results"], 4)
        self.assertEqual(summary["n_valid_outputs"], 2)
        self.assertEqual(summary["n_binary_decisions"], 1)
        self.assertEqual(summary["valid_output_coverage"], 2 / 5)
        self.assertEqual(summary["binary_decision_coverage"], 1 / 5)
        self.assertEqual(summary["agreement_rate"], 1)
        self.assertEqual(summary["n_errors"], 1)
        self.assertEqual(summary["n_pending_or_not_run"], 1)
        self.assertEqual([row["comparison_category"] for row in joined],
                         ["Agreement", None, None, "Undetermined", None])
        self.assertEqual(summary["n_comparison_undetermined"], 1)
        self.assertEqual(summary["n_invalid_outputs"], 1)

    def test_missing_formal_results_are_not_disagreements(self):
        joined = compare_rows([prediction()], [])
        self.assertEqual(joined[0]["comparison_status"], "missing_formal_result")
        self.assertEqual(joined[0]["comparison_category"], "Undetermined")
        self.assertFalse(joined[0]["formal_result_available"])
        self.assertIsNone(joined[0]["agreement"])
        summary = summarise_comparisons(joined)["model_summary"][0]
        self.assertEqual(summary["n_valid_outputs"], 1)
        self.assertEqual(summary["n_missing_formal_results"], 1)
        self.assertEqual(summary["n_disagreements"], 0)
        self.assertEqual(summary["n_comparison_undetermined"], 1)
        self.assertIsNone(summary["agreement_rate"])
        self.assertEqual(summary["comparison_coverage"], 0)

    def test_failure_types_and_invalid_labels_are_preserved(self):
        labels = [
            ("error", "compliance"), ("incomplete", "violation"),
            ("invalid_json", None), ("refusal", None),
            ("no_text", None), ("ok", "COMPLIANT"),
        ]
        predictions = [
            prediction(f"C-{index}/26", status=status, outcome=outcome)
            for index, (status, outcome) in enumerate(labels, 1)
        ]
        joined = compare_rows(predictions, [formal(row["case_id"]) for row in predictions])
        self.assertEqual([row["comparison_status"] for row in joined],
                         ["error", "incomplete", "invalid_json", "refusal", "no_text", "invalid_outcome"])
        self.assertTrue(all(row["comparison_category"] is None for row in joined))
        summary = summarise_comparisons(joined)["model_summary"][0]
        self.assertEqual(summary["n_valid_outputs"], 0)
        self.assertEqual(summary["n_comparison_undetermined"], 0)
        self.assertEqual(summary["n_invalid_outputs"], 2)
        self.assertIsNone(summary["agreement_rate"])
        self.assertIsNone(summary["undetermined_rate_among_valid_outputs"])
        self.assertNotIn("not_applicable_rate_among_valid_outputs", summary)
        self.assertEqual(sum(row["n_assessments"] for row in summarise_comparisons(joined)["confusion_matrix"]), 6)

    def test_three_comparison_categories_partition_only_valid_outputs_in_every_summary(self):
        predictions = [
            prediction("C-1/26"),
            prediction("C-2/26", outcome="violation"),
            prediction("C-3/26", outcome="violation"),
            prediction("C-4/26", outcome="not_applicable"),
            prediction("C-5/26", outcome="undetermined"),
            prediction("C-6/26"),  # Valid binary output without a formal reference.
            prediction("C-7/26", outcome="violation"),
            prediction("C-8/26", status="pending", outcome=None),
            prediction("C-9/26", status="api_error", outcome="compliance"),
            prediction("C-10/26", outcome="COMPLIANT"),
        ]
        references = [formal(row["case_id"], satisfied=row["case_id"] != "C-2/26")
                      for row in predictions if row["case_id"] not in {"C-6/26", "C-7/26"}]
        joined = compare_rows(predictions, references)
        self.assertEqual([row["comparison_category"] for row in joined],
                         ["Agreement", "Agreement", "Disagreement", None, "Undetermined",
                          "Undetermined", "Undetermined", None, None, None])
        summaries = summarise_comparisons(joined)
        model = summaries["model_summary"][0]
        self.assertEqual((model["n_valid_outputs"], model["n_comparable_binary_decisions"]), (6, 3))
        self.assertEqual((model["n_comparison_agreements"], model["n_comparison_disagreements"],
                          model["n_comparison_undetermined"]), (2, 1, 3))
        self.assertEqual((model["n_undetermined"], model["n_missing_formal_results"]), (1, 2))
        self.assertNotIn("n_not_applicable", model)
        self.assertEqual(model["agreement_rate"], 2 / 3)
        self.assertEqual((model["n_pending_or_not_run"], model["n_errors"], model["n_invalid_outputs"]), (1, 1, 2))
        for kind in ("model_summary", "rule_summary", "activation_summary"):
            for row in summaries[kind]:
                with self.subTest(kind=kind, row=row):
                    self.assertEqual(row["n_comparison_agreements"], row["n_agreements"])
                    self.assertEqual(row["n_comparison_disagreements"], row["n_disagreements"])
                    self.assertEqual(row["n_comparison_agreements"] + row["n_comparison_disagreements"]
                                     + row["n_comparison_undetermined"], row["n_valid_outputs"])
                    self.assertEqual(row["n_comparison_agreements"] + row["n_comparison_disagreements"],
                                     row["n_comparable_binary_decisions"])

    def test_nonbinary_and_missing_reference_are_not_counted_twice(self):
        joined = compare_rows([prediction("C-1/26", outcome="undetermined"),
                               prediction("C-2/26", outcome="undetermined")], [])
        self.assertEqual([row["comparison_category"] for row in joined], ["Undetermined", "Undetermined"])
        summary = summarise_comparisons(joined)["model_summary"][0]
        self.assertEqual(summary["n_comparison_undetermined"], 2)
        self.assertEqual(summary["n_missing_formal_results"], 2)
        self.assertEqual(summary["n_comparable_binary_decisions"], 0)
        self.assertIsNone(summary["agreement_rate"])
        self.assertIsNone(summary["disagreement_rate"])

    def test_variants_share_a_formal_rule_without_becoming_independent_original_rules(self):
        joined = compare_rows(
            [
                prediction(variant_id="R01__V0001", outcome="compliance"),
                prediction(variant_id="R01__V0002", outcome="violation"),
            ],
            [formal()],
        )
        summaries = summarise_comparisons(joined)
        summary = summaries["model_summary"][0]
        self.assertEqual(summary["n_base_rules"], 1)
        self.assertEqual(summary["n_rule_variants"], 2)
        self.assertEqual(summary["n_case_rule_pairs"], 1)
        self.assertEqual(summary["n_case_variant_pairs"], 2)
        self.assertEqual(summary["n_planned_assessments"], 2)
        self.assertEqual(len(summaries["rule_summary"]), 2)
        self.assertEqual(summary["agreement_rate"], 0.5)

    def test_metadata_and_original_inputs_are_preserved(self):
        original = prediction(request_sha256="request-hash", evidence_refs=["E0001"],
                              justification="The later event is recorded.")
        copy_before = copy.deepcopy(original)
        joined = compare_rows([original], [formal(aliases=True)])
        self.assertEqual(original, copy_before)
        for key, value in original.items():
            self.assertEqual(joined[0][key], value)
        self.assertEqual(joined[0]["formal_trace_sha256"], "trace-hash")

    def test_csv_formal_values_and_compatible_identifier_aliases(self):
        row = formal(vacuous=True, activation="inactive")
        row.update(CaseID=row["case_id"], rule_id=row["base_rule_id"],
                   formal_satisfied="1", vacuously_satisfied="True")
        joined = compare_rows([prediction()], [row])
        self.assertIs(joined[0]["formal_vacuously_satisfied"], True)
        self.assertTrue(joined[0]["agreement"])

    def test_conflicting_formal_aliases_and_duplicate_formal_keys_are_rejected(self):
        conflict = formal()
        conflict["CaseID"] = "C-999/26"
        with self.assertRaisesRegex(ValueError, "Conflicting aliases"):
            compare_rows([prediction()], [conflict])
        with self.assertRaisesRegex(ValueError, "Duplicate formal"):
            compare_rows([prediction()], [formal(), formal()])

    def test_inconsistent_formal_outcomes_are_rejected(self):
        row = formal()
        row["formal_outcome"] = "violated"
        with self.assertRaisesRegex(ValueError, "disagrees"):
            compare_rows([prediction()], [row])
        with self.assertRaisesRegex(ValueError, "inactive"):
            compare_rows([prediction()], [formal(vacuous=True, activation="not_applicable")])

    def test_duplicate_jobs_and_duplicate_assessments_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Duplicate planned assessment"):
            compare_rows([prediction(), prediction()], [formal()])
        with self.assertRaisesRegex(ValueError, "Duplicate job_id"):
            compare_rows([
                prediction("C-1/26", job_id="job-1"),
                prediction("C-2/26", job_id="job-1"),
            ], [formal("C-1/26"), formal("C-2/26")])

    def test_repetitions_are_reported_separately(self):
        joined = compare_rows(
            [prediction(run=1), prediction(run=2, outcome="violation")], [formal()]
        )
        summary = summarise_comparisons(joined)["model_summary"]
        self.assertEqual([row["run"] for row in summary], [1, 2])
        self.assertEqual([row["agreement_rate"] for row in summary], [1, 0])

    def test_actual_call_coverage_requires_explicit_flags(self):
        joined = compare_rows([
            prediction("C-1/26", call_attempted=True),
            prediction("C-2/26", status="pending", outcome=None, call_attempted=False),
        ], [formal("C-1/26"), formal("C-2/26")])
        summary = summarise_comparisons(joined)["model_summary"][0]
        self.assertEqual(summary["n_calls_attempted"], 1)
        self.assertEqual(summary["call_attempt_coverage"], 0.5)
        joined[1].pop("call_attempted")
        partial = summarise_comparisons(joined)["model_summary"][0]
        self.assertIsNone(partial["n_calls_attempted"])
        self.assertIsNone(partial["call_attempt_coverage"])

    def test_missing_output_status_is_explicit(self):
        row = prediction()
        row.pop("status")
        joined = compare_rows([row], [formal()])
        self.assertEqual(joined[0]["comparison_status"], "missing_output")
        self.assertIsNone(joined[0]["comparison_category"])
        self.assertEqual(summarise_comparisons(joined)["model_summary"][0]["n_missing_outputs"], 1)

    def test_empty_input_has_no_invented_summary_rows(self):
        self.assertEqual(summarise_comparisons(compare_rows([], [])), {
            "model_summary": [], "rule_summary": [],
            "activation_summary": [], "confusion_matrix": [],
        })


if __name__ == "__main__":
    unittest.main()

