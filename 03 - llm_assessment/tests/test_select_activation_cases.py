"""Verify activation enrichment without LLM outcomes or production changes."""

import copy
import itertools
import unittest

import select_activation_cases as selection


def fixtures(sets, *, years=None, repeats=None, violated=None):
    """Complete original-rule grids: triggered rules plus one separate unary."""
    names = list(sets)
    universe = sorted(set().union(*(set(value) for value in sets.values())))
    cases, rows = [], []
    for case_id in names:
        year = years[case_id] if years else 2000 + int(case_id.split("/")[1][:2])
        cases.append({"case_id": case_id, "case_year": year, "event_count": 5,
                      "metadata": {"Nested": {"case": case_id, "values": [1, 2]}},
                      "events": [{"ref": "E0001", "activity": "Recorded activity"}],
                      "document_files": [case_id.replace("/", "_") + ".txt"]})
        for rule in [*universe, "UNARY"]:
            active = rule in sets[case_id]
            unary = rule == "UNARY"
            broken = active and violated is not None and (case_id, rule) in violated
            count = (repeats or {}).get((case_id, rule), 1) if active else 0
            rows.append({"case_id": case_id, "base_rule_id": rule, "rule_id": rule,
                         "constraint": f'AtLeastOne("{rule}")' if unary else f'Response("{rule}", "target")',
                         "template": "AtLeastOne" if unary else "Response",
                         "formal_satisfied": int(not broken),
                         "formal_outcome": "satisfied" if unary or active and not broken else "violated" if broken else "vacuously_satisfied",
                         "activation_status": "not_applicable" if unary else "active" if active else "inactive",
                         "activation_count": None if unary else count,
                         "fulfilment_count": None if unary else 0 if broken else count,
                         "violation_count": None if unary else count if broken else 0,
                         "vacuously_satisfied": not unary and not active,
                         "component_count": 1, "event_count": 5})
    return cases, rows


def score(profile):
    return len(profile["active_rule_ids"])


class ActivationSelectionTests(unittest.TestCase):
    def test_profiles_count_distinct_active_original_rules_not_repeated_activations_or_unary(self):
        cases, rows = fixtures({"C-1/20": {"A", "B"}, "C-2/20": {"A"}},
                               repeats={("C-1/20", "A"): 100, ("C-2/20", "A"): 1000})
        before = copy.deepcopy((cases, rows))
        profiles = selection.build_profiles(cases, rows)
        indexed = {row["case_id"]: row for row in profiles}
        self.assertEqual(indexed["C-1/20"]["active_rule_ids"], ["A", "B"])
        self.assertEqual(indexed["C-2/20"]["active_rule_ids"], ["A"])
        self.assertEqual(indexed["C-2/20"]["inactive_rule_ids"], ["B"])
        self.assertEqual(indexed["C-1/20"]["unary_rule_ids"], ["UNARY"])
        for profile in profiles:
            self.assertEqual(profile.get("n_active_rules", profile.get("n_active")), score(profile))
            self.assertNotIn("UNARY", profile["active_rule_ids"])
        chosen = selection.select_activation_cohort(profiles, {2020: 1}, 17)
        self.assertEqual([row["case_id"] for row in chosen], ["C-1/20"])
        self.assertEqual((cases, rows), before)

    def test_profile_keeps_complete_case_metadata_events_and_documents(self):
        cases, rows = fixtures({"C-2/20": {"A"}, "C-1/20": {"A", "B"}})
        profiles = selection.build_profiles(cases, rows)
        source = {case["case_id"]: case for case in cases}
        for profile in profiles:
            for field, value in source[profile["case_id"]].items():
                self.assertEqual(profile[field], value)

    def test_duplicate_missing_unknown_or_inconsistent_original_rule_pairs_are_rejected(self):
        cases, rows = fixtures({"C-1/20": {"A", "B"}, "C-2/20": {"A"}})
        malformed = [rows + [copy.deepcopy(rows[0])], rows[:-1],
                     [row for row in rows if row["case_id"] != "C-2/20"]]
        changed = copy.deepcopy(rows)
        changed[0]["activation_status"] = "unknown"
        malformed.append(changed)
        changed = copy.deepcopy(rows)
        changed[0].update(activation_status="not_applicable", activation_count=None,
                          fulfilment_count=None, violation_count=None)
        malformed.append(changed)
        for values in malformed:
            with self.subTest(values=values), self.assertRaises(ValueError):
                selection.build_profiles(cases, values)

    def test_primary_active_count_always_beats_secondary_rare_coverage(self):
        cases, rows = fixtures({"C-1/20": {"A", "B", "C"}, "C-2/20": {"RARE"},
                               "C-3/20": {"A", "B", "C"}, "C-4/20": {"A", "B", "C"}})
        selected = selection.select_activation_cohort(selection.build_profiles(cases, rows), {2020: 1}, 17)
        self.assertEqual(score(selected[0]), 3)
        self.assertNotEqual(selected[0]["case_id"], "C-2/20")

    def test_rare_coverage_breaks_equal_primary_score_before_seed(self):
        cases, rows = fixtures({"C-1/20": {"COMMON_A", "COMMON_B"},
                               "C-2/20": {"RARE_A", "RARE_B"},
                               "C-3/20": {"COMMON_A", "COMMON_B"}})
        profiles = selection.build_profiles(cases, rows)
        for seed in (1, 17, 9999):
            with self.subTest(seed=seed):
                chosen = selection.select_activation_cohort(profiles, {2020: 1}, seed)
                self.assertEqual([row["case_id"] for row in chosen], ["C-2/20"])

    def test_forced_above_cutoff_cases_are_preserved_and_boundary_coverage_is_marginal(self):
        cases, rows = fixtures({"C-1/20": {"A", "B", "C"}, "C-2/20": {"A", "B"},
                               "C-3/20": {"X", "Y"}, "C-4/20": {"X"}})
        chosen = selection.select_activation_cohort(selection.build_profiles(cases, rows), {2020: 2}, 17)
        self.assertEqual({row["case_id"] for row in chosen}, {"C-1/20", "C-3/20"})
        self.assertEqual(sum(map(score, chosen)), 5)

    def test_per_year_primary_objective_matches_independent_exhaustive_maximum(self):
        cases, rows = fixtures({"C-1/19": {"A", "B"}, "C-2/19": {"A"}, "C-3/19": {"B", "C", "D"},
                               "C-1/20": {"A", "C"}, "C-2/20": {"B", "C"}, "C-3/20": {"A", "B", "C"}})
        profiles = selection.build_profiles(cases, rows)
        quotas = {2019: 2, 2020: 1, 2021: 0}
        possible = [list(itertools.combinations([p for p in profiles if p["case_year"] == year], n))
                    for year, n in quotas.items()]
        maximum = max(sum(score(profile) for group in allocation for profile in group)
                      for allocation in itertools.product(*possible))
        chosen = selection.select_activation_cohort(profiles, quotas, 17)
        self.assertEqual(sum(map(score, chosen)), maximum)
        self.assertEqual(len(chosen), 3)
        self.assertEqual(sum(p["case_year"] == 2019 for p in chosen), 2)
        self.assertEqual(sum(p["case_year"] == 2020 for p in chosen), 1)
        self.assertEqual(len({p["case_id"] for p in chosen}), len(chosen))

    def test_input_order_is_irrelevant_and_seed_only_breaks_exact_ties(self):
        cases, rows = fixtures({f"C-{number}/20": {"A", "B"} for number in (1, 2, 3, 4, 5, 6)})
        profiles = selection.build_profiles(cases, rows)
        original = copy.deepcopy(profiles)
        for seed in (1, 17, 99):
            a = selection.select_activation_cohort(profiles, {2020: 3}, seed)
            b = selection.select_activation_cohort(list(reversed(profiles)), {2020: 3}, seed)
            self.assertEqual(a, b)
        chosen_sets = {tuple(p["case_id"] for p in selection.select_activation_cohort(profiles, {2020: 3}, seed))
                       for seed in range(20)}
        self.assertGreater(len(chosen_sets), 1)
        self.assertEqual(profiles, original)

    def test_output_is_year_then_natural_case_id_and_infeasible_quota_rejected(self):
        cases, rows = fixtures({"C-10/20": {"A"}, "C-2/20": {"A"}, "C-1/19": {"A"}})
        profiles = selection.build_profiles(cases, rows)
        chosen = selection.select_activation_cohort(profiles, {2019: 1, 2020: 2, 2021: 0}, 17)
        self.assertEqual([p["case_id"] for p in chosen], ["C-1/19", "C-2/20", "C-10/20"])
        for quotas in ({2020: 3}, {2021: 1}, {2020: -1}):
            with self.subTest(quotas=quotas), self.assertRaises(ValueError):
                selection.select_activation_cohort(profiles, quotas, 17)

    def test_formally_violated_active_cases_are_retained_without_requiring_target_presence(self):
        cases, rows = fixtures({"C-1/20": {"A", "B"}, "C-2/20": {"A"}},
                               violated={("C-1/20", "A"), ("C-1/20", "B")})
        chosen = selection.select_activation_cohort(selection.build_profiles(cases, rows), {2020: 1}, 17)
        self.assertEqual(chosen[0]["case_id"], "C-1/20")
        self.assertEqual(chosen[0]["active_rule_ids"], ["A", "B"])
        self.assertEqual(chosen[0].get("n_formal_violated"), 2)

    def test_required_cases_consume_year_quota_and_primary_is_exact_under_that_constraint(self):
        cases, rows = fixtures({"C-1/20": {"A", "B", "C"}, "C-2/20": {"A", "B", "C"},
                               "C-3/20": {"A"}, "C-4/20": {"B"}, "C-1/21": {"A", "B"}})
        profiles = selection.build_profiles(cases, rows)
        required = ("C-3/20",)
        quotas = {2020: 2, 2021: 1}
        chosen = selection.select_activation_cohort(profiles, quotas, 17, required_case_ids=required)
        self.assertIn("C-3/20", {p["case_id"] for p in chosen})
        self.assertEqual(sum(p["case_year"] == 2020 for p in chosen), 2)
        feasible = [combination for combination in itertools.combinations(profiles, 3)
                    if set(required).issubset({p["case_id"] for p in combination})
                    and all(sum(p["case_year"] == year for p in combination) == n for year, n in quotas.items())]
        self.assertEqual(sum(map(score, chosen)), max(sum(map(score, values)) for values in feasible))
        self.assertEqual(sum(map(score, chosen)), 6)

    def test_required_cases_cannot_overfill_a_year_or_refer_to_an_unknown_case(self):
        cases, rows = fixtures({"C-1/20": {"A"}, "C-2/20": {"A", "B"}, "C-1/21": {"B"}})
        profiles = selection.build_profiles(cases, rows)
        for quotas, required in (({2020: 1}, ("C-1/20", "C-2/20")),
                                  ({2020: 2}, ("C-999/20",)), ({2020: 2, 2021: 0}, ("C-1/21",))):
            with self.subTest(quotas=quotas, required=required), self.assertRaises(ValueError):
                selection.select_activation_cohort(profiles, quotas, 17, required_case_ids=required)

    def test_order_publication_coverage_does_not_require_judgment_or_prefer_satisfied_cases(self):
        cases, rows = fixtures({"C-1/20": {"A", "B", "C"}, "C-2/20": {"A", "B"},
                               "C-3/20": {"A"}, "C-4/20": {"A"}},
                               violated={("C-3/20", "A")})
        required = ("C-3/20", "C-4/20")
        for case in cases:
            if case["case_id"] in required:
                case["events"] = [{"ref": "E0001", "activity": "Order (OJ)"}]
        profiles = selection.build_profiles(cases, rows)
        chosen = selection.select_activation_cohort(profiles, {2020: 3}, 17, required_case_ids=required)
        self.assertEqual({p["case_id"] for p in chosen}, {"C-1/20", *required})
        self.assertEqual(sum(any(event["activity"] == "Order (OJ)" for event in p["events"]) for p in chosen), 2)
        self.assertTrue(all(not any(event["activity"] == "Judgment" for event in p["events"])
                            for p in chosen if p["case_id"] in required))
        # Change only formal satisfaction to demonstrate it is not a ranking
        # feature: active rule IDs, year, event coverage and selection stay fixed.
        changed = copy.deepcopy(rows)
        for row in changed:
            if row["case_id"] == "C-3/20" and row["activation_status"] == "active":
                row.update(formal_satisfied=1, formal_outcome="satisfied", fulfilment_count=1, violation_count=0)
        alternate = selection.select_activation_cohort(selection.build_profiles(cases, changed), {2020: 3}, 17,
                                                       required_case_ids=required)
        self.assertEqual([p["case_id"] for p in chosen], [p["case_id"] for p in alternate])


if __name__ == "__main__":
    unittest.main()
