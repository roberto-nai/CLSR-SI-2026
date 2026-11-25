"""Exercise frozen, per-attempt token-budget recovery without live API calls."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from curia_assessment import case_pipeline, pipeline as p
from curia_assessment.case_prompts import build_case_prompts
from curia_assessment.case_recovery import build_recovery_plan, read_case_attempt, run_recovery
from curia_assessment.metrics import summarise_resource_usage


class CaseRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / "output"
        self.audit = self.output / "_audit"
        self.audit.mkdir(parents=True)
        self.provider = {"id": "mistral", "provider": "mistral", "model": "mistral-pinned-fixture", "reasoning_effort": "medium"}
        self.providers = {"mistral": self.provider}
        self.pairs = {}
        for rule in ("R01", "R02"):
            pair_id = "pair_" + rule
            self.pairs[pair_id] = {
                "pair_id": pair_id, "base_rule_id": rule, "variant_id": rule, "case_id": "C-1/20", "case_year": 2020,
                "source": f"Article {rule}: A shall occur.", "requirement": "A must occur.",
                "declare": 'AtLeastOne("A")', "metadata": {},
                "event_count": 1, "events": [{"ref": "E0001", "activity": "A", "timestamp": "2020-01-01", "metadata": {}}],
                "passages": [],
            }
        self.system, self.user, self.refs = build_case_prompts(list(self.pairs.values()), "A", None)
        self.request = p.build_request(self.provider, self.system, self.user, 16000)
        self.job = {
            "job_id": "case_fixture", "pair_ids": list(self.pairs), "rule_ids": ["R01", "R02"],
            "case_id": "C-1/20", "case_year": 2020, "condition": "A", "run": 1,
            "provider_id": "mistral", "provider": "mistral", "model": self.provider["model"],
            "request_sha256": p.digest(self.request), "prompt_sha256": p.digest([self.system, self.user]),
        }
        self.config = {"conditions": ["A"], "providers": [self.provider], "output_dir": "output",
                       "request_workers": 2, "requests_per_provider": 2, "timeout_seconds": 900,
                       "recovery_max_output_tokens": 32000}
        self.archive(self.job)
        self.write_prepared([self.job])

    def directory(self, job=None, attempt=1):
        return self.audit / "requests" / (job or self.job)["job_id"] / f"attempt_{attempt:03d}"

    def archive(self, job, *, attempt=1, request=None, status="incomplete", finish="length", assessments=None):
        directory = self.directory(job, attempt)
        directory.mkdir(parents=True, exist_ok=True)
        request = copy.deepcopy(request or self.request)
        response = {"status": status, "finish_reason": finish, "attempted": True,
                    "usage": {"prompt_tokens": 100, "completion_tokens": 16000, "total_tokens": 16100},
                    "elapsed_seconds": 3.0, "http_status": 200, "raw_response": {"fixture": True}}
        p.write_json(directory / "request.json", request)
        p.write_json(directory / "response.json", response)
        result = {"job_id": job["job_id"], "request_sha256": p.digest(request), "status": status,
                  "assessments": assessments or [], "usage": response["usage"], "elapsed_seconds": 3.0,
                  "call_attempted": True, "raw_response_sha256": p.sha256_file(directory / "response.json")}
        result.update(p.normalise_usage(job["provider"], response["usage"]))
        p.write_json(directory / "assessment.json", result)
        return directory

    def write_prepared(self, jobs):
        p.write_jsonl(self.audit / "prepared/planned_assessments.jsonl", jobs)
        p.write_jsonl(self.audit / "prepared/pairs.jsonl", list(self.pairs.values()))
        p.write_json(self.audit / "run_manifest.json", {"experiment_sha256": "unchanged-baseline-identity", "prepared_sha256": {}})

    def freeze(self, plan):
        p.write_json(self.audit / "recovery_plan.json", plan)
        manifest = p.read_json(self.audit / "run_manifest.json")
        manifest["prepared_sha256"]["recovery_plan.json"] = p.sha256_file(self.audit / "recovery_plan.json")
        p.write_json(self.audit / "run_manifest.json", manifest)

    def plan(self, jobs=None):
        return build_recovery_plan(self.audit, jobs or [self.job], self.pairs, self.providers)

    def run_mocked(self, response):
        with patch.object(p, "load_config", return_value=(self.config, self.root)), \
                patch.object(case_pipeline, "_verify", return_value=p.read_json(self.audit / "run_manifest.json")), \
                patch.object(case_pipeline, "_analyse", return_value={}) as analyse, \
                patch.object(p, "send_request", return_value=response) as send, \
                contextlib.redirect_stdout(io.StringIO()):
            counts = run_recovery(self.root / "config.json")
        return counts, send, analyse

    def success(self):
        return {"status": "ok", "attempted": True, "finish_reason": "stop", "http_status": 200,
                "model_returned": self.provider["model"], "elapsed_seconds": 4.0,
                "usage": {"prompt_tokens": 100, "completion_tokens": 2000, "total_tokens": 2100},
                "text": json.dumps({"assessments": [
                    {"rule_id": rule, "outcome": "compliance", "evidence_refs": ["REQUIREMENT", "DECLARE", "E0001"],
                     "justification": "The requirement asks for A and the recorded event is A."}
                    for rule in self.job["rule_ids"]]}), "raw_response": {"fixture": True}}

    def test_baseline_read_is_unchanged_and_reports_actual_budget(self):
        directory = self.directory()
        before = {path.name: path.read_bytes() for path in directory.iterdir()}
        read = read_case_attempt(self.audit, self.job, directory)
        self.assertEqual(read["status"], "incomplete")
        self.assertEqual(read["actual_output_token_limit"], 16000)
        self.assertEqual(before, {path.name: path.read_bytes() for path in directory.iterdir()})

    def test_plan_targets_only_latest_known_phase_a_length_truncations(self):
        jobs = [self.job]
        for name, status, finish, phase in (
            ("ok", "ok", "stop", "A"), ("other_incomplete", "incomplete", "other", "A"),
            ("invalid", "invalid_output", "length", "A"), ("phase_b", "incomplete", "length", "B"),
        ):
            job = {**self.job, "job_id": "case_" + name, "condition": phase}
            self.archive(job, status=status, finish=finish)
            jobs.append(job)
        jobs.append({**self.job, "job_id": "case_pending"})
        before = {str(path.relative_to(self.audit)): path.read_bytes() for path in self.audit.rglob("*") if path.is_file()}
        plan = self.plan(jobs)
        self.assertEqual(len(plan["entries"]), 1)
        entry = plan["entries"][0]
        self.assertEqual((entry["job_id"], entry["attempt"], entry["previous_attempt"]), (self.job["job_id"], 2, 1))
        self.assertEqual(plan["initial_output_token_limit"], 16000)
        self.assertEqual(plan["recovery_output_token_limit"], 32000)
        expected = copy.deepcopy(self.request)
        expected["payload"]["max_tokens"] = 32000
        self.assertEqual(entry["request"], expected)
        self.assertEqual(entry["request_sha256"], p.digest(expected))
        self.assertEqual(before, {str(path.relative_to(self.audit)): path.read_bytes() for path in self.audit.rglob("*") if path.is_file()})
        with self.assertRaisesRegex(ValueError, "32000"):
            build_recovery_plan(self.audit, jobs, self.pairs, self.providers, max_output_tokens=64000)

    def test_amended_attempt_requires_exact_frozen_job_attempt_and_request(self):
        plan = self.plan()
        request = plan["entries"][0]["request"]
        directory = self.archive(self.job, attempt=2, request=request)
        with self.assertRaisesRegex(ValueError, "not authorised"):
            read_case_attempt(self.audit, self.job, directory)
        self.freeze(plan)
        read = read_case_attempt(self.audit, self.job, directory)
        self.assertEqual(read["actual_output_token_limit"], 32000)
        self.assertEqual(read["request_sha256"], p.digest(request))
        amended_plan = copy.deepcopy(plan)
        amended_plan["entries"][0]["attempt"] = 3
        self.freeze(amended_plan)
        with self.assertRaisesRegex(ValueError, "not authorised"):
            read_case_attempt(self.audit, self.job, directory)
        self.freeze(plan)
        p.write_json(self.audit / "recovery_plan.json", {**plan, "unauthorised_edit": True})
        with self.assertRaisesRegex(ValueError, "frozen"):
            read_case_attempt(self.audit, self.job, directory)

    def test_allowlist_cannot_authorise_another_budget_provider_or_prompt(self):
        original = self.plan()
        for mutation in ("budget", "provider", "prompt", "original_hash"):
            with self.subTest(mutation=mutation):
                plan = copy.deepcopy(original)
                entry = plan["entries"][0]
                if mutation == "budget":
                    entry["max_output_tokens"] = 64000
                    entry["request"]["payload"]["max_tokens"] = 64000
                elif mutation == "provider":
                    entry["request"]["payload"]["model"] = "different-model"
                elif mutation == "prompt":
                    entry["request"]["payload"]["messages"][-1]["content"] += " Changed evidence."
                else:
                    entry["original_request_sha256"] = "another_original_hash"
                entry["request_sha256"] = p.digest(entry["request"])
                self.freeze(plan)
                directory = self.archive(self.job, attempt=2, request=entry["request"])
                with self.assertRaises(ValueError):
                    read_case_attempt(self.audit, self.job, directory)

    def test_run_recovery_parses_new_response_preserves_old_attempt_and_never_repeats(self):
        self.freeze(self.plan())
        before = {path.name: path.read_bytes() for path in self.directory().iterdir()}
        counts, send, analyse = self.run_mocked(self.success())
        self.assertEqual((counts["processed"], counts["successful"]), (1, 1))
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[0]["payload"]["max_tokens"], 32000)
        self.assertEqual(send.call_args.kwargs["timeout_seconds"], 900)
        self.assertEqual(analyse.call_count, 1)
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.directory().iterdir()})
        latest = read_case_attempt(self.audit, self.job, self.directory(attempt=2))
        self.assertEqual(latest["status"], "ok")
        self.assertEqual(len(latest["assessments"]), 2)
        self.assertTrue(all(row["outcome"] == "compliance" for row in latest["assessments"]))
        ledger = [{**self.job, **read_case_attempt(self.audit, self.job, self.directory(attempt=attempt))}
                  for attempt in (1, 2)]
        usage = summarise_resource_usage([self.job], ledger)[0]
        self.assertEqual(usage["n_attempts"], 2)
        self.assertEqual(usage["total_tokens_total"], 18200)
        self.assertEqual(usage["elapsed_seconds_total"], 7.0)
        resumed, send, _ = self.run_mocked(self.success())
        self.assertEqual(resumed["processed"], 0)
        self.assertEqual(resumed["skipped_existing"], 1)
        send.assert_not_called()

    def test_interrupted_authorised_attempt_is_not_transmitted_again(self):
        plan = self.plan()
        self.freeze(plan)
        directory = self.directory(attempt=2)
        directory.mkdir()
        p.write_json(directory / "request.json", plan["entries"][0]["request"])
        counts, send, _ = self.run_mocked(self.success())
        self.assertEqual(counts["processed"], 0)
        self.assertEqual(counts["skipped_interrupted"], 1)
        send.assert_not_called()
        self.assertFalse((directory / "assessment.json").exists())

    def test_failed_32000_recovery_is_preserved_and_not_replanned(self):
        plan = self.plan()
        self.freeze(plan)
        self.archive(self.job, attempt=2, request=plan["entries"][0]["request"])
        self.assertEqual(self.plan()["entries"], [])
        counts, send, _ = self.run_mocked(self.success())
        self.assertEqual(counts["skipped_existing"], 1)
        send.assert_not_called()

    def test_invalid_recovery_response_remains_failed_without_another_attempt(self):
        self.freeze(self.plan())
        response = self.success()
        response["text"] = "The provider returned malformed JSON."
        counts, send, _ = self.run_mocked(response)
        self.assertEqual((counts["processed"], counts["failed"]), (1, 1))
        send.assert_called_once()
        self.assertEqual(read_case_attempt(self.audit, self.job, self.directory(attempt=2))["status"], "invalid_json")
        counts, send, _ = self.run_mocked(self.success())
        self.assertEqual(counts["skipped_existing"], 1)
        send.assert_not_called()

    def test_unexpected_transmission_error_keeps_an_unknown_attempt_and_still_analyses(self):
        self.freeze(self.plan())
        with patch.object(p, "load_config", return_value=(self.config, self.root)), \
                patch.object(case_pipeline, "_verify", return_value={}), \
                patch.object(case_pipeline, "_analyse", return_value={}) as analyse, \
                patch.object(p, "send_request", side_effect=RuntimeError("Transmission state is unknown.")), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "unknown"):
                run_recovery(self.root / "config.json")
        self.assertEqual(analyse.call_count, 1)
        self.assertEqual(read_case_attempt(self.audit, self.job, self.directory(attempt=2))["status"], "interrupted")
        counts, send, _ = self.run_mocked(self.success())
        self.assertEqual(counts["skipped_interrupted"], 1)
        send.assert_not_called()

    def test_missing_or_empty_plan_is_audited_and_frozen_before_transmission(self):
        original_manifest = (self.audit / "run_manifest.json").read_bytes()
        counts, send, _ = self.run_mocked(self.success())
        self.assertEqual(counts["successful"], 1)
        send.assert_called_once()
        plan_path = self.audit / "recovery_plan.json"
        manifest = p.read_json(self.audit / "run_manifest.json")
        self.assertEqual(manifest["experiment_sha256"], "unchanged-baseline-identity")
        self.assertEqual(manifest["prepared_sha256"]["recovery_plan.json"], p.sha256_file(plan_path))
        provenance = self.audit / manifest["recovery_provenance"][0]
        self.assertTrue(provenance.is_file())
        self.assertEqual((provenance.parent / "run_manifest.json").read_bytes(), original_manifest)

    def test_empty_plan_can_be_replaced_after_an_eligible_failure_is_recorded(self):
        self.freeze({"entries": [], "initial_output_token_limit": 16000, "recovery_output_token_limit": 32000})
        old_plan = (self.audit / "recovery_plan.json").read_bytes()
        counts, send, _ = self.run_mocked(self.success())
        self.assertEqual(counts["successful"], 1)
        send.assert_called_once()
        manifest = p.read_json(self.audit / "run_manifest.json")
        provenance = self.audit / manifest["recovery_provenance"][0]
        self.assertEqual((provenance.parent / "recovery_plan.json").read_bytes(), old_plan)


if __name__ == "__main__":
    unittest.main()
