"""Opt-in dual case protocol, semantic diagnostics and frozen local replay."""
from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import unittest
from unittest.mock import patch

import test_case_prompts as examples
import test_case_pipeline as pipeline_examples
from curia_assessment import case_pipeline, pipeline as p
from curia_assessment.case_archive import recover_case_assessment
from curia_assessment.case_prompts import build_case_prompts, parse_case_assessment
from curia_assessment.phase_baseline import load_phase1_baseline


def answer(rule_id="R01", **changes):
    value = {"rule_id": rule_id,
             "trace_conformance": {"outcome": "compliance", "basis": "observed_satisfaction", "evidence_refs": ["DECLARE", "E0001"], "justification": "The finite recorded sequence satisfies the literal formula."},
             "requirement_assessment": {"outcome": "undetermined", "scope_status": "unknown", "basis": "scope_uncertain", "missing_facts": ["The decisive procedural scope is not recorded."], "evidence_refs": ["REQUIREMENT", "CASE"], "justification": "The supplied evidence does not establish applicability."}}
    value.update(changes)
    return value


def response(*values):
    return json.dumps({"assessments": list(values)})


class CriticalPromptTests(unittest.TestCase):
    def setUp(self):
        self.pairs = examples.pairs()
        self.refs = build_case_prompts(self.pairs, case_protocol="critical_v1")[2]

    def parse(self, *values):
        return parse_case_assessment(response(*values), ["R01", "R02"], self.refs, case_protocol="critical_v1")

    def test_default_protocol_prompt_bytes_remain_identical(self):
        system, user, _ = build_case_prompts(self.pairs)
        self.assertEqual(hashlib.sha256(system.encode()).hexdigest(), "8fbcbbed704fec8022486f3288a29e6a74c8083d027b73dbd24fb9f9db86809d")
        self.assertEqual(hashlib.sha256(user.encode()).hexdigest(), "1b152fba4f4584486111c55c302d3c61a3d38fbadafebcbbec3afc4f671024f2")
        self.assertEqual(build_case_prompts(self.pairs), build_case_prompts(self.pairs, case_protocol="requirement_declare_v1"))

    def test_critical_evidence_literal_and_phase_b_append_only(self):
        original = copy.deepcopy(self.pairs)
        for pair in self.pairs:
            pair["metadata"]["formalisation_audit"] = {"outcome": "AUDIT_SENTINEL", "witness": "WITNESS_SENTINEL"}
        system, user, refs = build_case_prompts(self.pairs, "A", case_protocol="critical_v1")
        bs, bu, br = build_case_prompts(self.pairs, "B", examples.passages(), case_protocol="critical_v1")
        self.assertEqual(bs, system); self.assertTrue(bu.startswith(user)); self.assertEqual(br, refs | {"D0001"})
        body = examples.body(user)
        self.assertEqual(body["rules"], [{"rule_id": x["base_rule_id"], "requirement": x["requirement"], "declare": x["declare"]} for x in original])
        for forbidden in ["AUDIT_SENTINEL", "WITNESS_SENTINEL", "private-checker-marker", "private-provider-marker", '"source"']:
            self.assertNotIn(forbidden, user)
        self.assertIn("Do not treat an unrecorded act as absent in the actual proceeding", system)
        self.assertIn("Equal timestamps", system)
        self.assertIn("does not determine a case-level Disagreement", system)

    def test_nested_heads_preserved_requirement_only_projected(self):
        rows = [answer("R01"), answer("R02")]
        rows[0]["trace_conformance"].update(outcome="violation", basis="observed_violation")
        result = self.parse(*rows)
        self.assertEqual(result["status"], "ok")
        for original, parsed in zip(rows, result["assessments"]):
            self.assertEqual(parsed["trace_conformance"], original["trace_conformance"])
            self.assertEqual(parsed["requirement_assessment"], original["requirement_assessment"])
            self.assertEqual(parsed["outcome"], "undetermined")
            self.assertEqual(parsed["evidence_refs"], original["requirement_assessment"]["evidence_refs"])

    def test_semantic_contradictions_are_valid_and_diagnosed_without_recoding(self):
        first = answer("R01")
        first["trace_conformance"].update(outcome="compliance", basis="observed_violation")
        first["requirement_assessment"].update(outcome="violation", scope_status="out_of_scope", basis="scope_exclusion")
        second = answer("R02"); second["requirement_assessment"]["missing_facts"] = []
        parsed = self.parse(first, second)
        self.assertEqual(parsed["status"], "ok")
        self.assertEqual(parsed["assessments"][0]["outcome"], "violation")
        self.assertEqual(parsed["assessments"][0]["trace_conformance"], first["trace_conformance"])
        self.assertEqual(len(parsed["diagnostic_flags"]), 4)
        self.assertIn("uncertainty_without_missing_facts", {x["code"] for x in parsed["diagnostic_flags"]})

    def test_shape_reference_and_enum_errors_remain_operational(self):
        for mutate in [lambda x: x["trace_conformance"].update(outcome="undetermined"),
                       lambda x: x["requirement_assessment"].update(evidence_refs=["UNKNOWN"]),
                       lambda x: x["requirement_assessment"].update(missing_facts="not a list"),
                       lambda x: x["trace_conformance"].update(extra="unexpected")]:
            first = answer("R01"); mutate(first)
            result = self.parse(first, answer("R02"))
            self.assertEqual(result["status"], "invalid_output")
            self.assertIsNone(result["assessments"][0]["outcome"])
            self.assertEqual(result["assessments"][1]["status"], "ok")

    def test_incomplete_duplicate_or_extra_rule_allocations_invalidate_batch(self):
        for records in [[answer("R01")], [answer("R01"), answer("R01")], [answer("R01"), answer("R03")]]:
            self.assertEqual(self.parse(*records)["status"], "invalid_output")
        for text in ['{"assessments":[],"assessments":[]}', '{"x":NaN}', '{"x":1e999}']:
            self.assertEqual(parse_case_assessment(text, ["R01"], self.refs, case_protocol="critical_v1")["status"], "invalid_json")


class CriticalPipelineTests(unittest.TestCase):
    def setUp(self):
        self.fixture = pipeline_examples.CasePipelineTests(); self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.config = self.fixture.config
        self.config.update(case_protocol="critical_v1", conditions=["A"], max_output_tokens=32000)
        self.fixture.fixture._write_config()
        for name in ("write_evaluation_outputs", "write_article_outputs", "write_results_report"):
            tool = patch.object(case_pipeline, name); tool.start(); self.addCleanup(tool.stop)

    def model_response(self, contradictory=False):
        value = self.fixture.response()
        records = [answer("R01"), answer("R02")]
        if contradictory:
            records[0]["trace_conformance"]["basis"] = "observed_violation"
        value["text"] = response(*records)
        return value

    def run(self, result=None):
        # unittest uses run(result); preserve its lifecycle while naming the helper clearly below.
        return super().run(result)

    def execute(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return p.run(self.fixture.config_path, **kwargs)

    def test_config_opt_in_and_invalid_protocols(self):
        config, _ = p.load_config(self.fixture.config_path)
        self.assertEqual(config["case_protocol"], "critical_v1")
        for value in ["unknown", [], None]:
            self.config["case_protocol"] = value; self.fixture.fixture._write_config()
            with self.assertRaises(ValueError): p.load_config(self.fixture.config_path)

    def test_frozen_jobs_parse_semantic_flags_then_resume_without_new_calls(self):
        with patch.object(p, "send_request", return_value=self.model_response(True)) as send:
            first = self.execute()
            second = self.execute(retry_failed=True)
        self.assertEqual((first["successful"], second["processed"], send.call_count), (2, 0, 2))
        jobs = self.fixture.jobs()
        self.assertTrue(all(job["case_protocol"] == "critical_v1" for job in jobs))
        for job in jobs:
            result = p.read_json(self.fixture.audit / "requests" / job["job_id"] / "attempt_001/assessment.json")
            self.assertTrue(result["diagnostic_flags"])
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["assessments"][0]["outcome"], "undetermined")

    def test_received_critical_response_replayed_locally_without_paid_retry(self):
        with patch.object(p, "send_request", return_value=self.model_response()) as send:
            self.execute()
        job = self.fixture.jobs()[0]; directory = self.fixture.audit / "requests" / job["job_id"] / "attempt_001"
        (directory / "assessment.json").unlink()
        with patch.object(p, "send_request") as send:
            result = self.execute(retry_failed=True)
        send.assert_not_called()
        self.assertEqual(result["recovered_from_archive"], 1)
        restored = p.read_json(directory / "assessment.json")
        self.assertEqual(restored["status"], "ok")
        self.assertIsNotNone(restored["assessments"][0]["trace_conformance"])

    def test_phase_b_reconstructs_critical_a_and_rejects_protocol_switch(self):
        with patch.object(p, "send_request", return_value=self.model_response()): self.execute()
        config, root = p.load_config(self.fixture.config_path)
        config.update(conditions=["B"], output_dir="phase2", baseline_phase1_dir="outputs")
        baseline = load_phase1_baseline(config, root)
        self.assertEqual(len(baseline["cases"]), 2)
        with self.assertRaisesRegex(ValueError, "case_protocol"):
            load_phase1_baseline({**config, "case_protocol": "requirement_declare_v1"}, root)


if __name__ == "__main__":
    unittest.main()
