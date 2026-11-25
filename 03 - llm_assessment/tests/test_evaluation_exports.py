"""Verify transparent case aggregation and safe independent expert review exports."""

from __future__ import annotations

import copy
import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from curia_assessment.evaluation_exports import write_evaluation_outputs


class EvaluationExportTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name)
        workbook_export = patch("curia_assessment.evaluation_exports.write_expert_workbooks")
        workbook_export.start()
        self.addCleanup(workbook_export.stop)
        self.pairs = {}
        self.jobs = []
        for case_id, year in (("C-1/17", 2017), ("C-2/18", 2018)):
            for rule in ("R01", "R02"):
                pair_id = case_id + "_" + rule
                self.pairs[pair_id] = {
                    "pair_id": pair_id, "case_id": case_id, "case_year": year,
                    "base_rule_id": rule, "variant_id": rule,
                    "source": f'Article {rule}, supplied source.\nQuotation: "shall".',
                    "declare": 'AtLeastOne( "A" )',
                    "requirement": "The recorded case must contain A.",
                    "metadata": {"Country": "EN", "formal_outcome": "FORMAL_SECRET"},
                    "events": [{"ref": "E0001", "activity": "Application", "timestamp": "2020-01-01", "metadata": {}}],
                    "document_files": ["C-1_17_01_01.txt"],
                    "passages": [{"passage_id": "D1", "source_reference": "Case document, paragraph 1",
                                  "text": "DOCUMENTARY_SENTINEL: the applicant filed A."}],
                }
                for phase in ("A", "B"):
                    for provider in ("openai", "mistral"):
                        self.jobs.append({
                            "job_id": f"job_{case_id}_{rule}_{phase}_{provider}", "pair_id": pair_id,
                            "case_id": case_id, "base_rule_id": rule, "variant_id": rule,
                            "condition": phase, "provider_id": provider, "provider": provider,
                            "model": f"{provider}-pinned-fixture", "run": 1,
                        })
        self.assessments = []

    def export(self):
        return write_evaluation_outputs(self.output, config={}, pairs=self.pairs,
                                        jobs=self.jobs, assessments=self.assessments)

    @staticmethod
    def read(path: Path) -> list[dict[str, str]]:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            return list(csv.DictReader(stream))

    @staticmethod
    def save(path: Path, rows: list[dict[str, str]]) -> None:
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def state(self, number=1):
        return json.loads((self.output / f"_audit/expert_review_state/expert_{number}.json").read_text())["assessments"]

    def result(self, case_id: str, provider: str, phase: str, rule_id: str, *, status="ok", outcome="compliance", explanation="Recorded evidence supports this judgement."):
        job = next(job for job in self.jobs if job["case_id"] == case_id and job["provider_id"] == provider
                   and job["condition"] == phase and job["base_rule_id"] == rule_id)
        result = {"job_id": job["job_id"], "status": status, "outcome": outcome,
                  "justification": explanation if status == "ok" else None,
                  "evidence_refs": ["REQUIREMENT", "DECLARE", "E0001"] if status == "ok" else []}
        self.assessments.append(result)
        return result

    def test_case_flags_preserve_unknowns_pending_and_textual_requirements(self) -> None:
        self.result("C-1/17", "openai", "A", "R01", outcome="violation")
        self.result("C-1/17", "openai", "B", "R01", outcome="compliance")
        self.result("C-1/17", "openai", "B", "R02", outcome="compliance")
        self.result("C-1/17", "mistral", "A", "R01", outcome="compliance")
        self.result("C-1/17", "mistral", "A", "R02", outcome="undetermined")
        self.result("C-1/17", "mistral", "B", "R01", status="invalid_json", outcome=None)
        self.result("C-2/18", "mistral", "B", "R01", status="api_error", outcome=None)
        self.result("C-2/18", "mistral", "B", "R02", status="interrupted", outcome=None)
        paths = self.export()
        cases = {(row["case_id"], row["provider_id"], row["phase"]): row
                 for row in self.read(self.output / paths["case_results"])}
        self.assertEqual(len(cases), 8)
        violation = cases[("C-1/17", "openai", "A")]
        self.assertEqual((violation["violation"], violation["status"]), ("yes", "partial"))
        self.assertEqual((violation["n_rules"], violation["n_completed"], violation["n_pending"]), ("2", "1", "1"))
        self.assertEqual(json.loads(violation["violated_rule_ids"]), ["R01"])
        explanations = json.loads(violation["explanations"])
        self.assertNotIn("source", explanations[0])
        self.assertEqual(explanations[0]["declare"], self.pairs["C-1/17_R01"]["declare"])
        self.assertEqual(explanations[0]["requirement"], self.pairs["C-1/17_R01"]["requirement"])
        self.assertEqual(explanations[0]["justification"], self.assessments[0]["justification"])
        self.assertEqual(explanations[0]["evidence_refs"], ["REQUIREMENT", "DECLARE", "E0001"])
        self.assertEqual(cases[("C-1/17", "openai", "B")]["violation"], "no")
        self.assertEqual(cases[("C-1/17", "openai", "B")]["n_compliance"], "2")
        self.assertNotIn("n_not_applicable", cases[("C-1/17", "openai", "B")])
        self.assertEqual(cases[("C-1/17", "mistral", "A")]["violation"], "undetermined")
        self.assertEqual(cases[("C-1/17", "mistral", "A")]["n_undetermined"], "1")
        self.assertEqual(cases[("C-1/17", "mistral", "B")]["n_failed"], "1")
        self.assertEqual(cases[("C-2/18", "openai", "A")]["status"], "pending")
        failed = cases[("C-2/18", "mistral", "B")]
        self.assertEqual((failed["status"], failed["violation"], failed["n_failed"]), ("failed", "undetermined", "2"))
        rules = self.read(self.output / paths["rule_assessments"])
        self.assertEqual(len(rules), len(self.jobs))
        self.assertEqual(len({row["assessment_id"] for row in rules}), len(self.jobs))
        self.assertEqual({row["job_id"] for row in rules}, {job["job_id"] for job in self.jobs})
        for filename in ("case_results.csv", "case_comparison.csv", "rule_assessments.csv", "expert_review/expert_1.csv", "expert_review/expert_2.csv"):
            self.assertTrue((self.output / filename).read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_wide_comparison_has_one_case_per_row_without_merging_views(self) -> None:
        self.result("C-1/17", "openai", "A", "R01", outcome="violation")
        self.result("C-1/17", "openai", "A", "R02", outcome="compliance")
        for rule in ("R01", "R02"):
            self.result("C-1/17", "openai", "B", rule)
            self.result("C-1/17", "mistral", "A", rule)
        self.export()
        rows = {row["case_id"]: row for row in self.read(self.output / "case_comparison.csv")}
        self.assertEqual(set(rows), {"C-1/17", "C-2/18"})
        first = rows["C-1/17"]
        self.assertEqual(first["A_openai_violation"], "yes")
        self.assertEqual(first["B_openai_violation"], "no")
        self.assertEqual(first["A_mistral_violation"], "no")
        self.assertEqual(first["B_mistral_violation"], "undetermined")
        self.assertEqual(json.loads(first["A_openai_violated_rule_ids"]), ["R01"])
        self.assertEqual(first["B_mistral_status"], "pending")
        self.assertIn("rule_assessments.csv", first["A_openai_explanations"])
        self.assertIn("R01: violation — Recorded evidence supports this judgement.", first["A_openai_explanations"])
        self.assertIn("phase=A; provider_id=openai", first["A_openai_explanations"])
        self.assertEqual(rows["C-2/18"]["A_openai_violation"], "undetermined")

    def test_wide_explanation_excerpts_fit_excel_without_truncating_rule_details(self) -> None:
        explanation = "Verbatim model explanation. " * 2000
        self.result("C-1/17", "openai", "A", "R01", outcome="violation", explanation=explanation)
        self.export()
        row = next(row for row in self.read(self.output / "case_comparison.csv") if row["case_id"] == "C-1/17")
        excerpt = row["A_openai_explanations"]
        self.assertLessEqual(len(excerpt), 24000)
        self.assertIn("R01: violation — Verbatim model explanation.", excerpt)
        self.assertIn("[Excerpt truncated; complete explanations, formulas and requirements in rule_assessments.csv.", excerpt)
        self.assertIn("phase=A; provider_id=openai", excerpt)
        detail = next(row for row in self.read(self.output / "rule_assessments.csv")
                      if row["case_id"] == "C-1/17" and row["phase"] == "A" and row["provider_id"] == "openai" and row["rule_id"] == "R01")
        self.assertEqual(detail["justification"], explanation)

    def test_expert_templates_group_cases_show_model_and_share_phase_specific_dossiers(self) -> None:
        self.export()
        first = self.read(self.output / "expert_review/expert_1.csv")
        second = self.read(self.output / "expert_review/expert_2.csv")
        self.assertEqual(first, second)
        self.assertEqual(len(first), len(self.jobs))
        self.assertFalse({"provider", "provider_id", "job_id", "variant_id", "response_sha256",
                          "annotation_response_sha256", "formal_outcome", "formal_satisfied", "source",
                          "legal_source", "quoted_source", "source_quote", "legal_basis", "source_support"} & set(first[0]))
        columns = list(first[0])
        self.assertEqual(columns[columns.index("run") + 1], "model")
        self.assertEqual({row["model"] for row in first}, {"openai-pinned-fixture", "mistral-pinned-fixture"})
        self.assertEqual([(int(row["case_year"]), row["case_id"]) for row in first],
                         sorted((int(row["case_year"]), row["case_id"]) for row in first))
        self.assertEqual({row["phase"] for row in first}, {"1", "2"})
        for row in first:
            self.assertEqual(row["declare"], 'AtLeastOne( "A" )')
            for field in ("expert_outcome", "judgement_correct", "evidence_support", "explanation_quality", "comments"):
                self.assertEqual(row[field], "")
            self.assertEqual(row["annotation_status"], "unreviewed")
            self.assertTrue((self.output / "expert_review" / row["dossier_path"]).is_file())
        self.assertEqual(set(self.state()), {row["assessment_id"] for row in first})
        self.assertTrue(all(row["response_sha256"] and row["annotation_response_sha256"] == ""
                            for row in self.state().values()))
        dossiers = list((self.output / "expert_review/dossiers").glob("*.txt"))
        self.assertEqual(len(dossiers), 4)
        for dossier in dossiers:
            text = dossier.read_text(encoding="utf-8")
            self.assertIn("E0001 | 2020-01-01 | Application", text)
            self.assertNotIn("FORMAL_SECRET", text)
            self.assertNotIn("-pinned-fixture", text)
            self.assertIn('DECLARE (scoped to this rule):\nAtLeastOne( "A" )', text)
            if "phase_A" in dossier.name:
                self.assertIn("Phase: 1\n", text)
                self.assertNotIn("Phase: A", text)
                self.assertNotIn("DOCUMENTARY_SENTINEL", text)
            else:
                self.assertIn("Phase: 2\n", text)
                self.assertNotIn("Phase: B", text)
                self.assertEqual(text.count("DOCUMENTARY_SENTINEL"), 1)
        for filename in ("case_results.csv", "rule_assessments.csv", "expert_review/expert_1.csv"):
            self.assertNotIn("DOCUMENTARY_SENTINEL", (self.output / filename).read_text(encoding="utf-8-sig"))
        instructions = (self.output / "expert_review/instructions.txt").read_text(encoding="utf-8")
        self.assertIn("compliance | violation | undetermined", instructions)
        self.assertNotIn("not_applicable", instructions)
        self.assertIn("outside the case's scope", instructions)
        self.assertIn("yellow for undetermined", instructions)
        self.assertIn("every planned assessment is valid compliance", instructions)
        self.assertIn("0 | 1 | 2 | 3", instructions)
        self.assertIn("independently", instructions)
        self.assertIn("model name is included immediately after run", instructions)
        self.assertIn("hide that column", instructions)
        self.assertIn("Phase 1 uses the supplied textual requirements, original DECLARE formulas and ordered event log", instructions)
        self.assertIn("Phase 2 uses the same evidence and additionally supplies", instructions)
        self.assertNotIn("future document-assisted runs", instructions)
        self.assertIn("ordered by case_year", instructions)
        self.assertIn("REQUIREMENT and DECLARE are scoped to their rule", instructions)
        self.assertIn("supplied textual requirement, original DECLARE formula and case evidence", instructions)
        self.assertNotIn("legal source", instructions)
        self.assertNotIn("S1", instructions)

    def test_legacy_pair_quotations_never_enter_new_exports_or_dossiers(self) -> None:
        for pair in self.pairs.values():
            pair.update(source="QUOTED_SOURCE_SENTINEL", legal_source={"text": "LEGAL_SOURCE_SENTINEL"},
                        quoted_source="ANOTHER_QUOTATION_SENTINEL", source_quote="SOURCE_QUOTE_SENTINEL",
                        legal_basis="LEGAL_BASIS_SENTINEL")
        original = copy.deepcopy(self.pairs)
        self.result("C-1/17", "openai", "A", "R01")
        self.export()
        self.assertEqual(self.pairs, original)
        for file in self.output.rglob("*"):
            if not file.is_file():
                continue
            text = file.read_text(encoding="utf-8-sig")
            for marker in ("QUOTED_SOURCE_SENTINEL", "LEGAL_SOURCE_SENTINEL", "ANOTHER_QUOTATION_SENTINEL",
                           "SOURCE_QUOTE_SENTINEL", "LEGAL_BASIS_SENTINEL"):
                self.assertNotIn(marker, text, file.name)
        dossier = next((self.output / "expert_review/dossiers").glob("*phase_B.txt")).read_text()
        self.assertIn("REQUIREMENT (scoped to this rule)", dossier)
        self.assertIn('DECLARE (scoped to this rule):\nAtLeastOne( "A" )', dossier)
        self.assertIn("Case document, paragraph 1", dossier)
        self.assertIn("DOCUMENTARY_SENTINEL", dossier)

    def test_pairs_without_any_quoted_source_fields_export_successfully(self) -> None:
        for pair in self.pairs.values():
            pair.pop("source")
        self.export()
        self.assertTrue(all("requirement" in row and "source" not in row
                            for row in self.read(self.output / "rule_assessments.csv")))

    def test_missing_original_formula_stops_before_any_existing_export_is_changed(self) -> None:
        self.export()
        before = {file: file.read_bytes() for file in self.output.rglob("*") if file.is_file()}
        pair = self.pairs["C-1/17_R01"]
        for value in (None, "", " ", 17):
            with self.subTest(formula=value):
                pair["declare"] = value
                with self.assertRaisesRegex(ValueError, "original DECLARE formula.*not invented"):
                    self.export()
                self.assertEqual({file: file.read_bytes() for file in self.output.rglob("*") if file.is_file()}, before)

    def test_formula_change_retains_ratings_and_flags_the_reviewed_response_identity(self) -> None:
        self.result("C-1/17", "openai", "A", "R01")
        self.export()
        path = self.output / "expert_review/expert_1.csv"
        rows = self.read(path)
        target = next(row for row in rows if row["status"] == "ok")
        target.update(comments="Review the original constraint.", evidence_support="supported")
        identifier = target["assessment_id"]
        old_hash = self.state()[identifier]["response_sha256"]
        self.save(path, rows)
        formula = 'Response( "A", "B" )'
        self.pairs["C-1/17_R01"]["declare"] = formula
        self.export()
        current = next(row for row in self.read(path) if row["assessment_id"] == identifier)
        self.assertEqual(current["declare"], formula)
        self.assertEqual(current["outcome"], "compliance")
        self.assertEqual(current["comments"], target["comments"])
        self.assertEqual(current["evidence_support"], "supported")
        self.assertEqual(current["annotation_status"], "response_changed_since_annotation")
        self.assertEqual(self.state()[identifier]["annotation_response_sha256"], old_hash)
        self.assertNotEqual(self.state()[identifier]["response_sha256"], old_hash)

    def test_historical_forms_without_formula_keep_annotations_and_are_flagged_for_review(self) -> None:
        self.result("C-1/17", "openai", "A", "R01")
        self.export()
        path = self.output / "expert_review/expert_1.csv"
        rows = self.read(path)
        for row in rows:
            row.pop("declare")
        target = next(row for row in rows if row["status"] == "ok")
        target["comments"] = "Keep this pre-formula review."
        identifier = target["assessment_id"]
        self.save(path, rows)
        (self.output / "_audit/expert_review_state/expert_1.json").unlink()
        self.export()
        current = next(row for row in self.read(path) if row["assessment_id"] == identifier)
        self.assertEqual(current["comments"], target["comments"])
        self.assertEqual(current["declare"], self.pairs["C-1/17_R01"]["declare"])
        self.assertEqual(current["annotation_status"], "response_changed_since_annotation")
        self.assertEqual(len(self.state()[identifier]["annotation_response_sha256"]), 64)

    def test_nonempty_legacy_source_support_stops_before_overwriting_any_files(self) -> None:
        result = self.result("C-1/17", "openai", "A", "R01")
        self.export()
        path = self.output / "expert_review/expert_2.csv"
        rows = self.read(path)
        for row in rows:
            row["source_support"] = ""
        rows[0]["source_support"] = "supported"
        self.save(path, rows)
        before = {file: file.read_bytes() for file in self.output.rglob("*") if file.is_file()}
        result["justification"] = "A new response must not erase the old review."
        with self.assertRaisesRegex(ValueError, "Legacy source_support annotations.*not translated automatically"):
            self.export()
        self.assertEqual({file: file.read_bytes() for file in self.output.rglob("*") if file.is_file()}, before)

    def test_empty_legacy_columns_are_removed_and_other_ratings_remain_flagged(self) -> None:
        self.result("C-1/17", "openai", "A", "R01")
        self.export()
        path = self.output / "expert_review/expert_1.csv"
        rows = self.read(path)
        for row in rows:
            row.update(source="OLD_QUOTATION_SENTINEL", legal_source="OLD_LEGAL_SENTINEL",
                       quoted_source="OLD_QUOTED_SENTINEL", source_quote="OLD_QUOTE_SENTINEL",
                       legal_basis="OLD_BASIS_SENTINEL", source_support="")
        target = next(row for row in rows if row["status"] == "ok")
        target.update(comments="Preserve this historical review.", evidence_support="partly_supported")
        identifier = target["assessment_id"]
        self.save(path, rows)
        (self.output / "_audit/expert_review_state/expert_1.json").unlink()
        self.export()
        current = next(row for row in self.read(path) if row["assessment_id"] == identifier)
        self.assertEqual(current["comments"], target["comments"])
        self.assertEqual(current["evidence_support"], "partly_supported")
        self.assertEqual(current["annotation_status"], "response_changed_since_annotation")
        self.assertFalse({"source", "legal_source", "quoted_source", "source_quote", "legal_basis", "source_support"} & set(current))
        for file in self.output.rglob("*"):
            if file.is_file():
                self.assertNotIn("OLD_QUOTATION_SENTINEL", file.read_text(encoding="utf-8-sig"))
        self.export()
        self.assertEqual(next(row for row in self.read(path) if row["assessment_id"] == identifier), current)

    def test_expert_order_prioritises_year_over_lexicographic_case_id_and_keeps_scientific_phases(self) -> None:
        for case_id, year in (("C-900/16", 2016), ("C-0/25", 2025)):
            pair_id = case_id + "_R01"
            self.pairs[pair_id] = {**copy.deepcopy(self.pairs["C-1/17_R01"]),
                                   "pair_id": pair_id, "case_id": case_id, "case_year": year}
            self.jobs.append({**self.jobs[0], "job_id": "job_" + case_id,
                              "pair_id": pair_id, "case_id": case_id})
        self.export()
        scientific = self.read(self.output / "rule_assessments.csv")
        phases = {row["assessment_id"]: row["phase"] for row in scientific}
        self.assertEqual(set(phases.values()), {"A", "B"})
        self.assertEqual({row["phase"] for row in self.read(self.output / "case_results.csv")}, {"A", "B"})
        self.assertEqual({job["condition"] for job in self.jobs}, {"A", "B"})
        for number in (1, 2):
            expert = self.read(self.output / f"expert_review/expert_{number}.csv")
            keys = [(int(row["case_year"]), row["case_id"]) for row in expert]
            self.assertEqual(keys, sorted(keys))
            self.assertEqual(expert[0]["case_id"], "C-900/16")
            self.assertEqual(expert[-1]["case_id"], "C-0/25")
            self.assertNotEqual([row["case_id"] for row in expert], sorted(row["case_id"] for row in expert))
            self.assertEqual({row["assessment_id"] for row in expert}, set(phases))
            for row in expert:
                scientific_phase = phases[row["assessment_id"]]
                self.assertEqual(row["phase"], {"A": "1", "B": "2"}[scientific_phase])
                self.assertIn(f"_phase_{scientific_phase}.txt", row["dossier_path"])

    def test_migrating_lettered_expert_phases_keeps_annotations_and_response_identity(self) -> None:
        self.result("C-1/17", "openai", "A", "R01")
        self.export()
        path = self.output / "expert_review/expert_1.csv"
        rows = self.read(path)
        target = next(row for row in rows if row["status"] == "ok")
        identifier = target["assessment_id"]
        response_hash = self.state()[identifier]["response_sha256"]
        target["comments"] = "Preserve my review across the presentation change."
        for row in rows:
            row["phase"] = {"1": "A", "2": "B"}[row["phase"]]
        self.save(path, rows)
        self.export()
        current = next(row for row in self.read(path) if row["assessment_id"] == identifier)
        self.assertEqual(current["phase"], "1")
        self.assertEqual(current["comments"], target["comments"])
        self.assertEqual(current["annotation_status"], "annotated")
        self.assertEqual(self.state()[identifier]["annotation_response_sha256"], response_hash)
        self.assertEqual(self.state()[identifier]["response_sha256"], response_hash)

    def test_reanalysis_preserves_each_experts_annotations_and_custom_columns(self) -> None:
        result = self.result("C-1/17", "openai", "A", "R01")
        self.export()
        path = self.output / "expert_review/expert_1.csv"
        rows = self.read(path)
        target = next(row for row in rows if row["case_id"] == "C-1/17" and row["phase"] == "1" and row["rule_id"] == "R01" and row["status"] == "ok")
        identifier = target["assessment_id"]
        old_hash = self.state()[identifier]["response_sha256"]
        for row in rows:
            row["additional_expert_notes"] = ""
        target.update(expert_outcome="violation", judgement_correct="no", evidence_support="supported", explanation_quality="1",
                      comments='Independent judgement, with "quotation".\nSecond line.', additional_expert_notes="Preserve me.")
        self.save(path, rows)
        result.update(outcome="violation", justification="Revised recorded response.")
        self.export()
        current = next(row for row in self.read(path) if row["assessment_id"] == identifier)
        self.assertEqual(current["expert_outcome"], "violation")
        self.assertEqual(current["evidence_support"], "supported")
        self.assertEqual(current["comments"], target["comments"])
        self.assertEqual(current["additional_expert_notes"], "Preserve me.")
        self.assertEqual(current["outcome"], "violation")
        self.assertNotIn("annotation_response_sha256", current)
        self.assertNotIn("response_sha256", current)
        self.assertEqual(self.state()[identifier]["annotation_response_sha256"], old_hash)
        self.assertNotEqual(self.state()[identifier]["response_sha256"], old_hash)
        self.assertEqual(current["annotation_status"], "response_changed_since_annotation")
        other = next(row for row in self.read(self.output / "expert_review/expert_2.csv") if row["assessment_id"] == identifier)
        self.assertEqual(other["expert_outcome"], "")
        self.assertEqual(other["annotation_status"], "unreviewed")
        self.export()
        self.assertEqual(next(row for row in self.read(path) if row["assessment_id"] == identifier), current)

    def test_legacy_hash_columns_migrate_privately_without_becoming_custom_columns(self) -> None:
        result = self.result("C-1/17", "openai", "A", "R01")
        self.export()
        path = self.output / "expert_review/expert_1.csv"
        private = self.state()
        rows = self.read(path)
        for row in rows:
            row.pop("model")
            row["variant_id"] = row["rule_id"]
            row["response_sha256"] = private[row["assessment_id"]]["response_sha256"]
            row["annotation_response_sha256"] = ""
            row["legacy_expert_notes"] = ""
        target = next(row for row in rows if row["status"] == "ok")
        identifier = target["assessment_id"]
        anchor = target["response_sha256"]
        target.update(comments="Keep the legacy review.", legacy_expert_notes="Independent custom note.")
        self.save(path, rows)
        (self.output / "_audit/expert_review_state/expert_1.json").unlink()
        result.update(outcome="violation", justification="A later recorded response.")
        self.export()
        migrated = next(row for row in self.read(path) if row["assessment_id"] == identifier)
        self.assertEqual(migrated["comments"], "Keep the legacy review.")
        self.assertEqual(migrated["legacy_expert_notes"], "Independent custom note.")
        self.assertEqual(migrated["model"], "openai-pinned-fixture")
        self.assertFalse({"variant_id", "response_sha256", "annotation_response_sha256"} & set(migrated))
        self.assertEqual(migrated["annotation_status"], "response_changed_since_annotation")
        self.assertEqual(self.state()[identifier]["annotation_response_sha256"], anchor)
        self.export()
        self.assertEqual(next(row for row in self.read(path) if row["assessment_id"] == identifier), migrated)

    def test_new_csv_can_recover_review_identity_if_private_sidecar_is_unavailable(self) -> None:
        result = self.result("C-1/17", "openai", "A", "R01")
        self.export()
        path = self.output / "expert_review/expert_1.csv"
        rows = self.read(path)
        target = next(row for row in rows if row["status"] == "ok")
        identifier = target["assessment_id"]
        anchor = self.state()[identifier]["response_sha256"]
        target["comments"] = "Review of the displayed response."
        self.save(path, rows)
        (self.output / "_audit/expert_review_state/expert_1.json").unlink()
        result.update(justification="Changed explanation.")
        self.export()
        current = next(row for row in self.read(path) if row["assessment_id"] == identifier)
        self.assertEqual(current["comments"], target["comments"])
        self.assertEqual(current["annotation_status"], "response_changed_since_annotation")
        self.assertEqual(self.state()[identifier]["annotation_response_sha256"], anchor)

    def test_expert_can_acknowledge_changed_response_without_editing_private_hashes(self) -> None:
        result = self.result("C-1/17", "openai", "A", "R01")
        self.export()
        path = self.output / "expert_review/expert_1.csv"
        rows = self.read(path)
        target = next(row for row in rows if row["status"] == "ok")
        identifier = target["assessment_id"]
        target["comments"] = "Original rating."
        self.save(path, rows)
        result.update(justification="Changed explanation.")
        self.export()
        rows = self.read(path)
        target = next(row for row in rows if row["assessment_id"] == identifier)
        self.assertEqual(target["annotation_status"], "response_changed_since_annotation")
        reviewed_hash = self.state()[identifier]["response_sha256"]
        target.update(annotation_status="annotated", comments="Reconciled independent review.")
        self.save(path, rows)
        self.export()
        reconciled = next(row for row in self.read(path) if row["assessment_id"] == identifier)
        self.assertEqual(reconciled["annotation_status"], "annotated")
        self.assertEqual(self.state()[identifier]["annotation_response_sha256"], reviewed_hash)
        result.update(justification="Another subsequent response.")
        self.export()
        self.assertEqual(next(row for row in self.read(path) if row["assessment_id"] == identifier)["annotation_status"],
                         "response_changed_since_annotation")

    def test_duplicate_or_foreign_expert_ids_stop_before_any_overwrite(self) -> None:
        self.export()
        first_path = self.output / "expert_review/expert_1.csv"
        second_path = self.output / "expert_review/expert_2.csv"
        before_first = first_path.read_bytes()
        before_state = (self.output / "_audit/expert_review_state/expert_1.json").read_bytes()
        before_cases = (self.output / "case_results.csv").read_bytes()
        rows = self.read(second_path)
        original_rows = copy.deepcopy(rows)
        for invalid in ("duplicate", "foreign"):
            with self.subTest(invalid=invalid):
                bad = copy.deepcopy(original_rows)
                if invalid == "duplicate":
                    bad.append(dict(bad[0]))
                else:
                    bad[0]["assessment_id"] = "assessment_from_another_experiment"
                self.save(second_path, bad)
                with self.assertRaises(ValueError):
                    self.export()
                self.assertEqual(first_path.read_bytes(), before_first)
                self.assertEqual((self.output / "_audit/expert_review_state/expert_1.json").read_bytes(), before_state)
                self.assertEqual((self.output / "case_results.csv").read_bytes(), before_cases)
                self.assertEqual(self.read(second_path), bad)
        self.save(second_path, original_rows)
        self.export()

    def test_missing_assessments_stay_pending_and_invalid_job_sets_fail(self) -> None:
        self.export()
        self.assertTrue(all(row["status"] == "pending" and row["violation"] == "undetermined"
                            for row in self.read(self.output / "case_results.csv")))
        self.jobs.append(dict(self.jobs[0]))
        with self.assertRaisesRegex(ValueError, "unique job"):
            self.export()
        self.jobs.pop()
        result = self.result("C-1/17", "openai", "A", "R01")
        self.assessments.append(dict(result))
        with self.assertRaisesRegex(ValueError, "duplicate job"):
            self.export()

    def test_unsupported_model_labels_are_rejected_without_rewriting_existing_exports(self) -> None:
        self.export()
        before = {path: path.read_bytes() for path in self.output.rglob("*") if path.is_file()}
        result = self.result("C-1/17", "openai", "A", "R01")
        for status, outcome in (("ok", "not_applicable"), ("invalid_output", "not_applicable"),
                                ("ok", "unknown"), ("ok", None), ("ok", ""), ("ok", [])):
            with self.subTest(status=status, outcome=outcome):
                result.update(status=status, outcome=outcome)
                with self.assertRaisesRegex(ValueError, "unsupported model outcome"):
                    self.export()
                self.assertEqual({path: path.read_bytes() for path in self.output.rglob("*") if path.is_file()}, before)

    def test_failed_response_is_operational_and_cannot_become_an_all_compliance_case(self) -> None:
        self.result("C-1/17", "openai", "A", "R01", status="invalid_output", outcome=None)
        self.result("C-1/17", "openai", "A", "R02", outcome="compliance")
        self.export()
        case = next(row for row in self.read(self.output / "case_results.csv")
                    if (row["case_id"], row["provider_id"], row["phase"]) == ("C-1/17", "openai", "A"))
        self.assertEqual((case["violation"], case["status"], case["n_failed"], case["n_undetermined"]),
                         ("undetermined", "partial", "1", "0"))
        failed = next(row for row in self.read(self.output / "rule_assessments.csv") if row["status"] == "invalid_output")
        self.assertEqual((failed["outcome"], failed["violation"]), ("", "undetermined"))

    def test_unsupported_expert_rating_stops_before_overwriting_any_review(self) -> None:
        self.result("C-1/17", "openai", "A", "R01")
        self.export()
        path = self.output / "expert_review/expert_2.csv"
        rows = self.read(path)
        rows[0].update(expert_outcome="not_applicable", comments="Preserve this legacy annotation for manual review.")
        self.save(path, rows)
        before = {item: item.read_bytes() for item in self.output.rglob("*") if item.is_file()}
        with self.assertRaisesRegex(ValueError, "Unsupported expert_outcome"):
            self.export()
        self.assertEqual({item: item.read_bytes() for item in self.output.rglob("*") if item.is_file()}, before)

    def test_duplicate_expert_column_names_cannot_silently_drop_comments(self) -> None:
        self.export()
        path = self.output / "expert_review/expert_1.csv"
        rows = self.read(path)
        fields = list(rows[0])
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow([*fields, "comments"])
            for row in rows:
                row["comments"] = "First comment must not disappear."
                writer.writerow([*(row[field] for field in fields), "Second comment must not replace the first."])
        original = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "duplicate column"):
            self.export()
        self.assertEqual(path.read_bytes(), original)

    def test_malformed_private_state_stops_before_overwriting_expert_files(self) -> None:
        self.export()
        path = self.output / "expert_review/expert_1.csv"
        original = path.read_bytes()
        state_path = self.output / "_audit/expert_review_state/expert_1.json"
        state_path.write_text("Not valid JSON", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "private expert review state"):
            self.export()
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(state_path.read_text(), "Not valid JSON")


if __name__ == "__main__":
    unittest.main()
