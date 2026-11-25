"""Prospective schema and orchestration tests; all transmissions are mocked."""
from __future__ import annotations

import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import critical_assessment as study_cli
from curia_assessment import formalisation_protocol as protocol
from curia_assessment import formalisation_witnesses as witnesses


def counterexample():
    return {"target": "in_scope", "events": ["A"], "world_facts": ["A complete hypothetical fact."],
            "obligation_engaged": True, "requirement_fulfilled": False, "formula_satisfied": True,
            "depends_on_unknown_mapping": False, "explanation": "A synthetic contrast.", "evidence_refs": ["DECLARE"]}


def response():
    return {"in_scope_assessment": {"outcome": "Agreement", "relation": "equivalent",
                "evidence_refs": ["REQUIREMENT", "DECLARE"], "justification": "Equivalent within the stipulated domain."},
            "ungated_assessment": {"outcome": "Undetermined", "relation": "unresolved",
                "evidence_refs": ["LOG_SCHEMA"], "justification": "The proposed mapping is unresolved."},
            "scope_conditions": [], "mapping_assumptions": [], "missing_information": [], "counterexamples": []}


class FormalisationParserTests(unittest.TestCase):
    def parse(self, value):
        return protocol.parse_audit_assessment(json.dumps(value), set(protocol.ALLOWED_REFS), {"A", "B"})

    def test_neutral_agreement_and_uncertainty_do_not_need_a_mismatch(self):
        value = response(); parsed = self.parse(value)
        self.assertEqual(parsed["status"], "ok")
        self.assertEqual(parsed["in_scope_assessment"]["outcome"], "Agreement")
        self.assertEqual(parsed["ungated_assessment"]["outcome"], "Undetermined")
        self.assertEqual(parsed["counterexamples"], [])

    def test_extra_fields_are_retained_raw_and_known_witness_projection_is_exact(self):
        value = response(); value["unknown_top"] = {"retained": True}
        value["in_scope_assessment"]["extra_note"] = "retained"
        value["counterexamples"] = [{**counterexample(), "extra_witness_note": "retained"}]
        before = copy.deepcopy(value); parsed = self.parse(value)
        self.assertEqual(parsed["status"], "ok")
        self.assertEqual(parsed["raw_json"], before)
        self.assertEqual(value, before)
        self.assertNotIn("extra_note", parsed["in_scope_assessment"])
        self.assertEqual(set(parsed["counterexamples"][0]), witnesses.WITNESS_FIELDS)
        self.assertTrue(parsed["format_notes"])

    def test_disagreement_without_proof_is_a_retained_logical_flag(self):
        value = response(); value["in_scope_assessment"].update(outcome="Disagreement", relation="formula_weaker")
        parsed = self.parse(value)
        self.assertEqual(parsed["status"], "ok")
        self.assertEqual(parsed["in_scope_assessment"]["outcome"], "Disagreement")
        self.assertTrue(any(f["code"] == "missing_claimed_proof_direction" for f in parsed["logical_flags"]))

    def test_outcome_relation_conflict_does_not_trigger_regeneration(self):
        value = response(); value["in_scope_assessment"]["relation"] = "formula_stronger"
        parsed = self.parse(value)
        self.assertEqual(parsed["status"], "ok")
        self.assertEqual(parsed["in_scope_assessment"]["outcome"], "Agreement")
        self.assertTrue(any(f["code"] == "outcome_relation_conflict" for f in parsed["logical_flags"]))

    def test_scope_and_engagement_contradictions_remain_structurally_valid(self):
        value = response(); value["in_scope_assessment"].update(outcome="Disagreement", relation="formula_stronger")
        value["counterexamples"] = [{**counterexample(), "target": "both", "obligation_engaged": False,
                                      "requirement_fulfilled": None, "formula_satisfied": False}]
        parsed = self.parse(value)
        self.assertEqual(parsed["status"], "ok")
        self.assertFalse(parsed["counterexamples"][0]["obligation_engaged"])
        self.assertTrue(any(f["code"] == "unengaged_primary_or_fulfilment" for f in parsed["logical_flags"]))

    def test_engaged_null_fulfilment_is_a_diagnostic_not_an_output_failure(self):
        value = response(); value["counterexamples"] = [{**counterexample(), "requirement_fulfilled": None}]
        parsed = self.parse(value)
        self.assertEqual(parsed["status"], "ok")
        self.assertTrue(any(f["code"] == "engaged_without_fulfilment_claim" for f in parsed["logical_flags"]))

    def test_unknown_mapping_does_not_validate_a_declared_disagreement(self):
        value = response(); value["in_scope_assessment"].update(outcome="Disagreement", relation="formula_weaker")
        value["counterexamples"] = [{**counterexample(), "depends_on_unknown_mapping": True}]
        parsed = self.parse(value)
        self.assertEqual(parsed["status"], "ok")
        self.assertEqual(parsed["in_scope_assessment"]["outcome"], "Disagreement")
        self.assertTrue(parsed["logical_flags"])

    def test_wrong_formula_claim_is_not_checked_by_the_response_parser(self):
        value = response(); value["in_scope_assessment"].update(outcome="Disagreement", relation="formula_stronger")
        value["counterexamples"] = [{**counterexample(), "formula_satisfied": False, "requirement_fulfilled": True}]
        with patch.object(witnesses, "engine_formula") as engine:
            parsed = self.parse(value)
        engine.assert_not_called()
        self.assertEqual(parsed["status"], "ok")
        self.assertFalse(parsed["counterexamples"][0]["formula_satisfied"])

    def test_unknown_activity_is_an_offline_proof_flag_without_a_retry(self):
        value = response(); value["counterexamples"] = [{**counterexample(), "events": ["unlisted exact label"]}]
        parsed = self.parse(value)
        self.assertEqual(parsed["status"], "ok")
        self.assertEqual(parsed["counterexamples"][0]["events"], ["unlisted exact label"])
        self.assertTrue(any(f["code"] == "unknown_activity_label" for f in parsed["logical_flags"]))

    def test_required_shapes_types_enums_and_references_remain_structural(self):
        values = []
        missing = response(); del missing["counterexamples"]; values.append(missing)
        wrong_type = response(); wrong_type["counterexamples"] = [{**counterexample(), "formula_satisfied": 1}]; values.append(wrong_type)
        wrong_label = response(); wrong_label["in_scope_assessment"]["outcome"] = "disagreement"; values.append(wrong_label)
        old_reference = response(); old_reference["in_scope_assessment"]["evidence_refs"] = ["S1"]; values.append(old_reference)
        for value in values:
            with self.subTest(value=value): self.assertEqual(self.parse(value)["status"], "invalid_output")

    def test_ambiguous_or_nonfinite_json_is_not_salvaged(self):
        for text in ['{"x":NaN}', '{"x":1e999}', '{"x":1,"x":2}', '{} {}']:
            with self.subTest(text=text):
                parsed = protocol.parse_audit_assessment(text, set(protocol.ALLOWED_REFS), {"A"})
                self.assertEqual(parsed["status"], "invalid_json")
        text = "```json\n" + json.dumps(response()) + "\n```"
        parsed = protocol.parse_audit_assessment(text, set(protocol.ALLOWED_REFS), {"A"})
        self.assertEqual(parsed["status"], "ok")
        self.assertTrue(parsed["format_notes"])


class StudyOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.study = Path(self.temp.name) / "new_study"; (self.study / "_audit").mkdir(parents=True)
        self.case = self.stage(50); self.audit = self.stage(19)
        study_cli.p.write_json(self.study / "_audit/config.phase1.frozen.json", {
            "providers": [{"id": name} for name in ("openai", "anthropic", "mistral")]})
        self.runner = Mock(); self.runner.inspect_provider_stops.return_value = {}
        self.runner.execute.side_effect = lambda *args, **kwargs: self.audit
        reporting = types.ModuleType("curia_assessment.critical_reporting")
        reporting.write_critical_outputs = Mock(return_value={})
        self.reporting = reporting
        patches = [patch.object(study_cli, "verify", return_value={}),
                   patch.object(study_cli, "verify_historical", return_value={"integrity_pass": True}),
                   patch.object(study_cli, "AuditRunner", return_value=self.runner),
                   patch.object(study_cli, "_stage_status", side_effect=self.status),
                   patch("run_assessment._load_credentials"),
                   patch.dict(sys.modules, {"curia_assessment.critical_reporting": reporting})]
        for item in patches: item.start(); self.addCleanup(item.stop)

    @staticmethod
    def stage(planned_each):
        return {"planned": 3 * planned_each, "complete": 3 * planned_each,
                "providers": {key: {"planned": planned_each, "complete": planned_each,
                    "remaining": 0, "stop": None} for key in ("openai", "anthropic", "mistral")}}

    def status(self, study, directory):
        state = self.case if directory == "case_assessment" else self.audit
        state["complete"] = sum(row["planned"] - row["remaining"] for row in state["providers"].values())
        return state

    @staticmethod
    def quota():
        return {"reason": "account_budget_exhausted", "can_resume": True}

    def run_study(self, side_effect=None, **kwargs):
        with patch.object(study_cli, "run_case", side_effect=side_effect) as case_run, redirect_stdout(io.StringIO()):
            result = study_cli.run_study(self.study, **kwargs)
        return result, case_run

    def test_technical_retry_and_audit_stage_never_probe_a_stopped_provider(self):
        self.case["providers"]["openai"].update(remaining=1, stop=self.quota())
        self.case["providers"]["anthropic"]["remaining"] = 1
        self.audit["providers"]["openai"]["remaining"] = 19
        def case_run(config, provider_id=None, retry_failed=False):
            if provider_id == "anthropic": self.case["providers"]["anthropic"]["remaining"] = 0
        result, calls = self.run_study(case_run)
        self.assertEqual([c.kwargs["provider_id"] for c in calls.call_args_list], [None, "anthropic"])
        self.assertEqual(self.runner.execute.call_args.kwargs["exclude_provider_ids"], {"openai"})
        self.assertEqual(result["stage"], "incomplete")
        journal = study_cli.p.read_json(self.study / "_audit/cross_stage_provider_stops.json")
        self.assertEqual(journal["providers"]["openai"]["reason"], "account_budget_exhausted")

    def test_general_resume_does_not_implicitly_resolve_a_historical_case_stop(self):
        self.case["providers"]["openai"].update(remaining=1, stop=self.quota())
        self.audit["providers"]["openai"]["remaining"] = 19
        _, calls = self.run_study(resume=True)
        self.assertEqual([c.kwargs["provider_id"] for c in calls.call_args_list], ["anthropic", "mistral"])
        self.assertEqual(self.runner.execute.call_args.kwargs["exclude_provider_ids"], {"openai"})
        self.assertFalse(self.runner.execute.call_args.kwargs["resolve_stop"])

    def test_case_only_stop_resolution_does_not_require_a_nonexistent_audit_stop(self):
        self.case["providers"]["openai"].update(remaining=1, stop=self.quota())
        self.audit["providers"]["openai"]["remaining"] = 19
        def case_run(config, provider_id=None, retry_failed=False):
            self.assertEqual(provider_id, "openai"); self.assertTrue(retry_failed)
            self.case["providers"]["openai"].update(remaining=0, stop=None)
        def audit_run(*args, **kwargs):
            self.assertFalse(kwargs["resolve_stop"])
            self.assertIsNone(kwargs["resolution_note"])
            self.audit["providers"]["openai"]["remaining"] = 0
            return self.status(self.study, "formalisation_audit")
        self.runner.execute.side_effect = audit_run
        result, _ = self.run_study(case_run, resume=True, provider="openai", resolve_stop=True,
                                   resolution_note="Operator confirmed restored credit.")
        self.assertEqual(result["stage"], "complete")
        journal = study_cli.p.read_json(self.study / "_audit/resolution_requests.json")
        self.assertEqual(journal[0]["case_stop"]["reason"], "account_budget_exhausted")
        self.assertEqual(journal[0]["provider"], "openai")

    def test_an_existing_audit_stop_is_resolved_only_for_the_selected_provider(self):
        self.runner.inspect_provider_stops.return_value = {"openai": self.quota()}
        self.audit["providers"]["openai"].update(remaining=19, stop=self.quota())
        def audit_run(*args, **kwargs):
            self.assertEqual(args[:2], ("resume", "openai"))
            self.assertTrue(kwargs["resolve_stop"])
            self.assertEqual(kwargs["resolution_note"], "Restored.")
            self.audit["providers"]["openai"].update(remaining=0, stop=None)
            return self.status(self.study, "formalisation_audit")
        self.runner.execute.side_effect = audit_run
        result, calls = self.run_study(resume=True, provider="openai", resolve_stop=True, resolution_note="Restored.")
        self.assertEqual(result["stage"], "complete")
        self.assertEqual([c.kwargs["provider_id"] for c in calls.call_args_list], ["openai"])


class StudyPathSafetyTests(unittest.TestCase):
    def test_foreign_study_failure_never_writes_a_status_into_old_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            old = Path(temporary) / "old_output"; (old / "_audit").mkdir(parents=True)
            marker = old / "_audit/preserved.txt"; marker.write_bytes(b"original")
            before = {str(f.relative_to(old)): f.read_bytes() for f in old.rglob("*") if f.is_file()}
            with patch("run_assessment._load_credentials") as credentials, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(study_cli.main(["run", "--study", str(old)]), 2)
            credentials.assert_not_called()
            self.assertEqual(before, {str(f.relative_to(old)): f.read_bytes() for f in old.rglob("*") if f.is_file()})

    def test_frozen_paths_cannot_escape_the_new_study(self):
        with tempfile.TemporaryDirectory() as temporary:
            study = Path(temporary) / "study"; (study / "_audit").mkdir(parents=True)
            outside = Path(temporary) / "outside.txt"; outside.write_text("preserved")
            link = study / "_audit/link"; link.symlink_to(Path(temporary), target_is_directory=True)
            for relative in ("../outside.txt", "_audit/link/outside.txt"):
                identity = {"protocol_id": study_cli.PROTOCOL, "code_sha256": "fixed-code",
                    "entrypoint_sha256": "entrypoint", "frozen_files": {relative: "unused"}}
                study_cli.p.write_json(study / "_audit/run_manifest.json", identity)
                with patch.object(study_cli.p, "code_digest", return_value="fixed-code"), \
                     patch.object(study_cli.p, "sha256_file", return_value="entrypoint"), \
                     patch.object(study_cli, "prepare_case") as prepare:
                    with self.assertRaises(ValueError): study_cli.verify(study)
                    prepare.assert_not_called()
            self.assertEqual(outside.read_text(), "preserved")


if __name__ == "__main__":
    unittest.main()
