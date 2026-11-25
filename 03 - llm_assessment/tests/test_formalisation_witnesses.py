"""Synthetic offline witness checks; no model calls or study-output fixtures."""
from __future__ import annotations

import copy
import itertools
import unittest
from unittest.mock import patch

import pandas as pd

from curia_assessment import formalisation_witnesses as checker


def witness(events, *, formula=True, fulfilled=False, engaged=True, target="both", unknown=False):
    return {"target": target, "events": events, "world_facts": ["Explicit hypothetical facts only."],
            "obligation_engaged": engaged, "requirement_fulfilled": fulfilled,
            "formula_satisfied": formula, "depends_on_unknown_mapping": unknown,
            "explanation": "A synthetic test, never a real judicial case.", "evidence_refs": ["DECLARE"]}


class FormulaTests(unittest.TestCase):
    def test_all_eight_templates_match_real_engine_on_40_short_traces(self):
        declarations = ['AtLeastOne("A")', 'Init("A")', 'Precedence("A", "B")',
                        'Response("A", "B")', 'RespondedExistence("A", "B")',
                        'CoExistence("A", "B")', 'NotCoExistence("A", "B")', 'Succession("A", "B")']
        rules = [checker.parse_rule(f"R{i:02d}", d) for i, d in enumerate(declarations, 1)]
        traces = [list(word) for n in range(4) for word in itertools.product(["A", "B", "X"], repeat=n)]
        rows, originals = [], {}
        for index, events in enumerate(traces):
            ident = f"SYNTHETIC_TEST_{index:03d}"; originals[ident] = events
            for position, activity in enumerate(events or ["SYNTHETIC_NEUTRAL"]):
                rows.append({"CaseID": ident, "Activity": activity,
                             "Timestamp": pd.Timestamp("2000-01-01"), "_source_row": position})
        pairs, _, metadata = checker.run_conformance(pd.DataFrame(rows), rules, tie_break="source")
        self.assertEqual(len(pairs), 320)
        self.assertTrue(metadata["known_answer_contract_checks_passed"])
        by_rule = {r.rule_id: r for r in rules}
        for row in pairs.to_dict("records"):
            with self.subTest(rule=row["rule_id"], events=originals[row["CaseID"]]):
                self.assertEqual(bool(row["formal_satisfied"]), checker.evaluate_formula(by_rule[row["rule_id"]], originals[row["CaseID"]]))

    def test_existing_loader_aliases_preserve_original_formula(self):
        for declaration, expected in [('Existence1("A")', 'AtLeastOne'),
                                      ('Responded Existence("A", "B")', 'RespondedExistence'),
                                      ('co-existence("A", "B")', 'CoExistence')]:
            rule = checker.parse_rule("R01", declaration)
            self.assertEqual(rule.template, expected)
            self.assertEqual(rule.constraint, declaration)
        with self.assertRaises(ValueError):
            checker.parse_rule("R01", 'ChainResponse("A", "B")')

    def test_empty_trace_neutral_activity_is_fresh_and_never_changes_witness(self):
        rule = checker.parse_rule("R01", 'AtLeastOne("SYNTHETIC_NEUTRAL")')
        events = []
        result = checker.engine_formula(rule, events, "SYNTHETIC_EMPTY")
        self.assertFalse(result["formula_satisfied"])
        self.assertTrue(result["empty_trace_neutral_encoding"])
        self.assertEqual(result["neutral_activity"], "SYNTHETIC_NEUTRAL_X")
        self.assertEqual(events, [])
        with self.assertRaises(ValueError):
            checker.engine_formula(rule, [], "C-123_24")

    def test_strict_positions_and_exact_labels_are_not_alphabetical_or_case_folded(self):
        rule = checker.parse_rule("R01", 'Response("A", "B")')
        self.assertFalse(checker.engine_formula(rule, ["B", "A"], "SYNTHETIC_REVERSE")["formula_satisfied"])
        self.assertTrue(checker.engine_formula(rule, ["A", "B"], "SYNTHETIC_FORWARD")["formula_satisfied"])
        self.assertFalse(checker.evaluate_formula(rule, ["A", "b"]))


class WitnessTests(unittest.TestCase):
    def setUp(self):
        self.rule = checker.parse_rule("R01", 'AtLeastOne("A")')
        self.job = {"job_id": "audit_synthetic", "rule_id": "R01", "provider_id": "synthetic_provider",
                    "model": "synthetic_model", "request_sha256": "request-hash"}
        self.row = {"base_rule_id": "R01", "declare": 'AtLeastOne("A")', "requirement": "Hypothetical requirement."}

    def check(self, value, **kwargs):
        return checker.check_counterexample(self.rule, value, {"A", "B", "X"}, index=1,
                                             job_id=self.job["job_id"], **kwargs)

    def job_result(self, witnesses, primary="formula_weaker", secondary="formula_weaker", outcome="Disagreement"):
        assessment = {"attempt": 1, "raw_response_sha256": "raw-hash", "counterexamples": witnesses,
                      "in_scope_assessment": {"outcome": outcome, "relation": primary},
                      "ungated_assessment": {"outcome": outcome, "relation": secondary}}
        before = copy.deepcopy(assessment)
        result = checker.check_job(self.job, self.row, assessment, {"A", "B", "X"})
        self.assertEqual(assessment, before)
        return result

    def test_formula_consistency_never_certifies_requirement_or_world_truth(self):
        value = witness(["A"]); before = copy.deepcopy(value)
        result = self.check(value)
        self.assertTrue(result["valid_in_scope_witness"] and result["valid_ungated_witness"])
        self.assertEqual(result["direction"], "formula_weaker")
        self.assertFalse(result["requirement_truth_validated"] or result["world_facts_validated"])
        self.assertEqual(result["model_counterexample"], before)
        self.assertEqual(value, before)

    def test_wrong_formula_claim_is_retained_as_flag_not_repaired_or_accepted(self):
        value = witness(["A"], formula=False, fulfilled=True)
        result = self.check(value)
        self.assertEqual(result["formula_engine_status"], "mismatched")
        self.assertFalse(result["formula_satisfied_claim"])
        self.assertTrue(result["formula_satisfied_engine"])
        self.assertFalse(result["valid_in_scope_witness"] or result["valid_ungated_witness"])
        self.assertEqual(result["model_counterexample"], value)

    def test_unknown_mapping_never_supports_either_head(self):
        result = self.check(witness(["A"], unknown=True))
        self.assertEqual(result["formula_engine_status"], "matched")
        self.assertFalse(result["valid_in_scope_witness"] or result["valid_ungated_witness"])

    def test_scope_overreach_null_is_not_legal_compliance_and_is_secondary_only(self):
        value = witness([], formula=False, fulfilled=None, engaged=False, target="ungated")
        result = self.job_result([value], primary="formula_stronger", secondary="scope_overreach")
        w = result["witnesses"][0]
        self.assertIsNone(w["model_counterexample"]["requirement_fulfilled"])
        self.assertFalse(result["in_scope_formula_consistent_D"])
        self.assertTrue(result["ungated_formula_consistent_D"])
        self.assertEqual(w["direction"], "scope_overreach")

    def test_witness_target_and_direction_must_match_head(self):
        result = self.job_result([witness([], formula=False, fulfilled=True)])
        self.assertFalse(result["in_scope_formula_consistent_D"])
        result = self.job_result([witness(["A"], target="ungated")])
        self.assertFalse(result["in_scope_formula_consistent_D"])
        self.assertTrue(result["ungated_formula_consistent_D"])

    def test_incomparable_requires_both_independently_checked_directions(self):
        weaker = witness(["A"]); stronger = witness([], formula=False, fulfilled=True)
        one = self.job_result([weaker], primary="incomparable", secondary="incomparable")
        self.assertFalse(one["in_scope_formula_consistent_D"])
        both = self.job_result([weaker, stronger], primary="incomparable", secondary="incomparable")
        self.assertTrue(both["in_scope_formula_consistent_D"] and both["ungated_formula_consistent_D"])
        self.assertEqual(both["in_scope_supporting_witness_indices"], [1, 2])

    def test_engine_error_or_pure_disagreement_is_a_flag_not_a_model_recode(self):
        with patch.object(checker, "engine_formula", side_effect=RuntimeError("Synthetic unavailable engine")):
            result = self.job_result([witness(["A"])])
        self.assertEqual(result["model_in_scope_assessment"]["outcome"], "Disagreement")
        self.assertEqual(result["witnesses"][0]["formula_engine_status"], "not_evaluable")
        self.assertFalse(result["in_scope_formula_consistent_D"])
        with patch.object(checker, "evaluate_formula", return_value=False):
            result = self.check(witness(["A"]))
        self.assertFalse(result["engine_pure_agree"] or result["valid_in_scope_witness"])

    def test_agreement_and_empty_witness_lists_do_not_manufacture_disagreement(self):
        result = self.job_result([], primary="equivalent", secondary="unresolved", outcome="Agreement")
        self.assertEqual(result["witnesses"], [])
        self.assertFalse(result["in_scope_formula_consistent_D"] or result["ungated_formula_consistent_D"])

    def test_unknown_event_label_is_flagged_without_normalisation(self):
        result = self.check(witness(["a"]))
        self.assertEqual(result["formula_engine_status"], "not_evaluable")
        self.assertEqual(result["events"], ["a"])

    def test_first_pass_nonengaged_both_and_null_engaged_never_support_disagreement(self):
        nonengaged = witness([], formula=False, fulfilled=None, engaged=False, target="both")
        result = self.job_result([nonengaged], primary="formula_stronger", secondary="scope_overreach")
        self.assertFalse(result["in_scope_formula_consistent_D"] or result["ungated_formula_consistent_D"])
        self.assertTrue(result["witnesses"][0]["flags"])
        engaged_null = witness(["A"], formula=True, fulfilled=None)
        self.assertFalse(self.job_result([engaged_null])["in_scope_formula_consistent_D"])

    def test_first_pass_malformed_witnesses_and_unavailable_heads_are_retained(self):
        for value in [None, "not an object", {}, {**witness(["A"]), "extra": True}]:
            result = self.check(value)
            self.assertEqual(result["formula_engine_status"], "not_evaluable")
            self.assertEqual(result["model_counterexample"], value)
        assessment = {"attempt": 1, "raw_response_sha256": "raw", "counterexamples": "wrong list type",
                      "in_scope_assessment": None, "ungated_assessment": {"outcome": "Disagreement", "relation": []}}
        result = checker.check_job(self.job, self.row, assessment, {"A"})
        self.assertEqual(result["model_counterexamples"], "wrong list type")
        self.assertIsNone(result["model_in_scope_assessment"])
        self.assertEqual(result["witnesses"], [])
        self.assertFalse(result["in_scope_formula_consistent_D"] or result["ungated_formula_consistent_D"])

    def test_outcome_relation_inconsistency_is_flagged_without_relabelling(self):
        result = self.job_result([witness(["A"])], primary="unresolved", secondary="equivalent")
        self.assertEqual(result["model_in_scope_assessment"]["outcome"], "Disagreement")
        self.assertFalse(result["in_scope_formula_consistent_D"])
        self.assertTrue(any("outcome/relation" in f for f in result["logical_flags"]))

    def test_engine_cache_keeps_different_formulas_separate(self):
        cache = {}
        first = self.check(witness(["A"]), engine_cache=cache)
        other = checker.parse_rule("R02", 'AtLeastOne("B")')
        second = checker.check_counterexample(other, witness(["A"], formula=False, fulfilled=True),
            {"A", "B"}, index=1, job_id="other_synthetic_job", engine_cache=cache)
        self.assertTrue(first["formula_satisfied_engine"])
        self.assertFalse(second["formula_satisfied_engine"])
        self.assertEqual(len(cache), 2)

    def test_cached_engine_result_cannot_be_changed_through_returned_records(self):
        cache = {}
        first = self.check(witness(["A"]), engine_cache=cache)
        first["engine_check"]["formula_satisfied"] = False
        second = self.check(witness(["A"]), engine_cache=cache)
        self.assertTrue(second["engine_check"]["formula_satisfied"])
        self.assertTrue(second["valid_in_scope_witness"])

    def test_world_fact_contradictions_remain_for_separate_text_review(self):
        value = witness(["A"])
        value["world_facts"] = ["An extra represented act was omitted from the events list."]
        result = self.check(value)
        self.assertTrue(result["formula_claim_matches_engine"])
        self.assertFalse(result["world_facts_validated"] or result["requirement_truth_validated"])
        self.assertEqual(result["model_counterexample"]["world_facts"], value["world_facts"])

    def test_primary_agreement_does_not_suppress_a_secondary_scope_counterexample(self):
        value = witness([], formula=False, fulfilled=None, engaged=False, target="ungated")
        assessment = {"attempt": 1, "raw_response_sha256": "raw-hash", "counterexamples": [value],
            "in_scope_assessment": {"outcome": "Agreement", "relation": "equivalent"},
            "ungated_assessment": {"outcome": "Disagreement", "relation": "scope_overreach"}}
        result = checker.check_job(self.job, self.row, assessment, {"A", "B", "X"})
        self.assertFalse(result["in_scope_formula_consistent_D"])
        self.assertTrue(result["ungated_formula_consistent_D"])
        self.assertEqual(result["model_in_scope_assessment"], assessment["in_scope_assessment"])

    def test_numbers_do_not_masquerade_as_boolean_witness_claims(self):
        for field, value in [("formula_satisfied", 1), ("obligation_engaged", 1),
                             ("depends_on_unknown_mapping", 0), ("requirement_fulfilled", 0)]:
            with self.subTest(field=field):
                item = witness(["A"]); item[field] = value
                result = self.check(item)
                self.assertEqual(result["formula_engine_status"], "not_evaluable")
                self.assertFalse(result["valid_in_scope_witness"] or result["valid_ungated_witness"])
                self.assertEqual(result["model_counterexample"][field], value)

    def test_oversized_sequence_is_retained_without_truncation_or_engine_replay(self):
        value = witness(["A"] * 9)
        with patch.object(checker, "engine_formula") as engine:
            result = self.check(value)
        engine.assert_not_called()
        self.assertEqual(result["events"], value["events"])
        self.assertEqual(result["formula_engine_status"], "not_evaluable")

    def test_provenance_and_literal_requirement_are_preserved_without_case_context(self):
        requirement = 'A literal requirement with “quoted text”, accents é and a newline.\nSecond line.'
        row = {**self.row, "requirement": requirement}
        job = {**self.job, "case_id": "not_a_real_case_context"}
        assessment = {"attempt": 3, "raw_response_sha256": "immutable-raw-hash", "counterexamples": [],
            "in_scope_assessment": {"outcome": "Undetermined", "relation": "unresolved"},
            "ungated_assessment": {"outcome": "Undetermined", "relation": "unresolved"}}
        result = checker.check_job(job, row, assessment, {"A"})
        self.assertEqual(result["requirement"], requirement)
        self.assertEqual(result["declare"], row["declare"])
        self.assertEqual(result["request_sha256"], job["request_sha256"])
        self.assertEqual(result["raw_response_sha256"], assessment["raw_response_sha256"])
        self.assertEqual(result["attempt"], 3)
        self.assertNotIn("case_id", result)

    def test_another_rule_identity_cannot_be_silently_joined_to_the_job(self):
        assessment = {"attempt": 1, "raw_response_sha256": "raw", "counterexamples": []}
        with self.assertRaisesRegex(ValueError, "rule identity"):
            checker.check_job(self.job, {**self.row, "base_rule_id": "R02"}, assessment, {"A"})


class RuleInputSafetyTests(unittest.TestCase):
    def test_multiline_formulas_and_unsafe_identifiers_are_not_reinterpreted(self):
        for rule_id, declaration in [("../R01", 'AtLeastOne("A")'),
                                     ("R01", 'AtLeastOne("A")\nAtLeastOne("B")'),
                                     ("R01", 'AtLeastOne("A")\tAtLeastOne("B")')]:
            with self.subTest(rule_id=rule_id, declaration=declaration):
                with self.assertRaises(ValueError):
                    checker.parse_rule(rule_id, declaration)


if __name__ == "__main__":
    unittest.main()

