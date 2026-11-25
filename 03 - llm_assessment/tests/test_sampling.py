"""Verify common rule selection and the complete case--rule crossing offline."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest

from curia_assessment.sampling import cross_pairs, select_requirements


class SamplingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.variants = []
        for number in range(1, 6):
            base = f"R{number:02d}"
            for formulation in range(1, 4 if number % 2 else 2):
                identifier = f"{base}__V{formulation:04d}"
                self.variants.append({
                    "base_rule_id": base, "variant_id": identifier,
                    "source_review_id": f"V{formulation:04d}",
                    "source": f"Source for {identifier}.",
                    "requirement": f"Supplied requirement for {identifier}.",
                    "declare": f"AtLeastOne(activity_{number})",
                })
        self.cases = [
            {"case_id": "C-1/20", "events": [{"activity": "A"}], "metadata": {"Court": "C"}},
            {"case_id": "C-2/20", "events": [{"activity": "B"}], "metadata": {"Court": "C"}},
        ]

    def test_rule_count_counts_original_rules_and_one_formulation_each(self) -> None:
        selected, audit = select_requirements(self.variants, rule_count=3, seed=71)
        self.assertEqual(len(selected), 3)
        self.assertEqual(len({item["base_rule_id"] for item in selected}), 3)
        self.assertEqual(len(audit), len(self.variants))
        self.assertEqual(sum(row["variant_selected"] for row in audit), 3)
        self.assertEqual({row["rule_selection_method"] for row in audit}, {"seeded_rule_sample"})

    def test_all_rules_and_input_order_independence(self) -> None:
        selected, audit = select_requirements(self.variants, seed=71)
        reversed_selection, reversed_audit = select_requirements(list(reversed(self.variants)), seed=71)
        self.assertEqual(len(selected), 5)
        self.assertEqual(selected, reversed_selection)
        self.assertEqual(audit, reversed_audit)
        self.assertTrue(all(row["rule_selected"] for row in audit))

    def test_legacy_source_is_discarded_before_selection_and_crossing(self) -> None:
        before = copy.deepcopy(self.variants)
        selected, audit = select_requirements(self.variants, seed=71)
        self.assertTrue(all("source" not in row and "source_review_id" not in row for row in selected + audit))
        self.assertTrue(all(row["requirement_review_id"] for row in selected))
        self.assertEqual(self.variants, before)
        changed = copy.deepcopy(self.variants)
        for row in changed:
            row["source"] = "A different legal source that must never affect selection."
        self.assertEqual(select_requirements(changed, seed=71), (selected, audit))
        legacy = self.variants[:1]
        pairs = cross_pairs(self.cases, legacy)
        self.assertTrue(all("source" not in row and "source_review_id" not in row for row in pairs))
        self.assertTrue(all(row["requirement"] == legacy[0]["requirement"] for row in pairs))

    def test_minus_one_selects_all_rules_and_retains_explicit_selection(self) -> None:
        all_selected = select_requirements(self.variants, rule_count=None, seed=71)
        self.assertEqual(select_requirements(self.variants, rule_count=-1, seed=71), all_selected)
        selected, _ = select_requirements(self.variants, rule_count=-1, rule_ids=["R04", "R02"], seed=71)
        self.assertEqual([row["base_rule_id"] for row in selected], ["R04", "R02"])
        identifiers = ["R01__V0002", "R03__V0003"]
        selected, _ = select_requirements(self.variants, rule_count=-1, variant_ids=identifiers, seed=71)
        self.assertEqual({row["variant_id"] for row in selected}, set(identifiers))

    def test_retained_rule_formulation_is_independent_of_rule_count(self) -> None:
        all_selected, _ = select_requirements(self.variants, seed=29)
        reference = {item["base_rule_id"]: item["variant_id"] for item in all_selected}
        for count in range(1, 6):
            selected, _ = select_requirements(self.variants, rule_count=count, seed=29)
            self.assertTrue(all(reference[item["base_rule_id"]] == item["variant_id"] for item in selected))

    def test_explicit_rule_ids_override_numeric_count_and_preserve_order(self) -> None:
        selected, audit = select_requirements(self.variants, rule_count=100,
                                              rule_ids=["R05", "R02"], seed=71)
        self.assertEqual([item["base_rule_id"] for item in selected], ["R05", "R02"])
        self.assertEqual({row["rule_selection_method"] for row in audit}, {"explicit_rule_ids"})

    def test_explicit_variants_select_only_the_named_formulations(self) -> None:
        identifiers = ["R01__V0002", "R03__V0003"]
        selected, audit = select_requirements(self.variants, variant_ids=identifiers, seed=71)
        self.assertEqual({item["variant_id"] for item in selected}, set(identifiers))
        chosen = [row for row in audit if row["variant_selected"]]
        self.assertEqual({row["variant_selection_method"] for row in chosen}, {"explicit_variant_id"})
        limited, _ = select_requirements(self.variants, rule_count=1, variant_ids=identifiers, seed=71)
        self.assertEqual(len(limited), 1)
        self.assertIn(limited[0]["variant_id"], identifiers)
        both, _ = select_requirements(self.variants, rule_ids=["R03", "R01"], variant_ids=identifiers)
        self.assertEqual([item["variant_id"] for item in both], ["R03__V0003", "R01__V0002"])

    def test_incompatible_or_ambiguous_explicit_selections_fail(self) -> None:
        for arguments in (
            {"variant_ids": ["R01__V0001", "R01__V0002"]},
            {"rule_ids": ["R01"], "variant_ids": ["R03__V0001"]},
            {"rule_ids": ["R01", "R03"], "variant_ids": ["R01__V0001"]},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                select_requirements(self.variants, **arguments)

    def test_invalid_counts_identifiers_and_seed_fail(self) -> None:
        invalid = [
            {"rule_count": 0}, {"rule_count": -2}, {"rule_count": True}, {"rule_count": False},
            {"rule_count": -1.0}, {"rule_count": 1.0}, {"rule_count": "-1"},
            {"rule_count": 1.5}, {"rule_count": 6}, {"rule_ids": []},
            {"rule_ids": ["R01", "R01"]}, {"rule_ids": ["unknown"]},
            {"variant_ids": []}, {"variant_ids": ["unknown"]},
            {"variant_ids": ["R01__V0001", "R01__V0001"]},
            {"rule_ids": [1]}, {"rule_ids": "R01"}, {"seed": True}, {"seed": "71"},
        ]
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                select_requirements(self.variants, **arguments)

    def test_catalogue_duplicates_and_conflicting_formalisations_fail(self) -> None:
        with self.assertRaisesRegex(ValueError, "Duplicate textual variant"):
            select_requirements(self.variants + [self.variants[0]])
        changed = copy.deepcopy(self.variants)
        changed[1]["declare"] = "AtLeastOne(other_activity)"
        with self.assertRaisesRegex(ValueError, "different DECLARE"):
            select_requirements(changed)

    def test_crossing_uses_every_rule_for_every_distinct_case(self) -> None:
        selected, _ = select_requirements(self.variants, rule_count=3, seed=71)
        before_cases, before_variants = copy.deepcopy(self.cases), copy.deepcopy(selected)
        pairs = cross_pairs(self.cases, selected)
        expected = {(case["case_id"], item["base_rule_id"])
                    for case in self.cases for item in selected}
        self.assertEqual(len(pairs), 2 * 3)
        self.assertEqual({(pair["case_id"], pair["base_rule_id"]) for pair in pairs}, expected)
        self.assertEqual(len({pair["pair_id"] for pair in pairs}), len(pairs))
        for pair in pairs:
            payload = json.dumps([pair["case_id"], pair["variant_id"]], ensure_ascii=False,
                                 sort_keys=True, separators=(",", ":")).encode("utf-8")
            self.assertEqual(pair["pair_id"], "pair_" + hashlib.sha256(payload).hexdigest()[:20])
        self.assertEqual(self.cases, before_cases)
        self.assertEqual(selected, before_variants)

    def test_crossing_rejects_duplicate_cases_and_multiple_formulations_per_rule(self) -> None:
        selected, _ = select_requirements(self.variants, rule_count=2)
        with self.assertRaisesRegex(ValueError, "duplicate case"):
            cross_pairs(self.cases + [self.cases[0]], selected)
        with self.assertRaisesRegex(ValueError, "exactly one textual formulation"):
            cross_pairs(self.cases, self.variants[:2])
        for cases, variants in (([], selected), (self.cases, [])):
            with self.assertRaises(ValueError):
                cross_pairs(cases, variants)


if __name__ == "__main__":
    unittest.main()
