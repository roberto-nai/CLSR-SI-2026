"""Replay saved case responses locally without charging for another generation."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from curia_assessment import case_archive, pipeline as p
from curia_assessment.case_archive import recover_case_assessment
from curia_assessment.case_prompts import build_case_prompts, parse_case_assessment


class CaseArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.audit = Path(temporary.name) / "_audit"
        self.pairs = [{
            "pair_id": "pair_" + rule, "base_rule_id": rule, "variant_id": rule,
            "case_id": "C-1/20", "case_year": 2020,
            "source": f"Source for {rule}: A shall occur.", "requirement": "A must occur.",
            "declare": 'AtLeastOne("A")',
            "metadata": {}, "event_count": 1,
            "events": [{"ref": "E0001", "activity": "A", "timestamp": "2020-01-01", "metadata": {}}],
            "passages": [{"passage_id": "D0001", "source_reference": "Frozen document, paragraph 1", "text": "A occurred."}],
        } for rule in ("R01", "R02")]
        self.archive()

    def archive(self, provider="mistral", condition="A") -> None:
        self.provider = {"id": provider, "provider": provider, "model": provider + "-pinned-fixture"}
        self.system, self.user, self.refs = build_case_prompts(
            self.pairs, condition, self.pairs[0]["passages"] if condition != "A" else None)
        self.request = p.build_request(self.provider, self.system, self.user, 32000)
        self.job = {
            "job_id": "case_fixture_" + provider + "_" + condition,
            "case_id": "C-1/20", "condition": condition, "provider": provider, "model": self.provider["model"],
            "pair_ids": [pair["pair_id"] for pair in self.pairs], "rule_ids": ["R01", "R02"],
            "request_sha256": p.digest(self.request), "prompt_sha256": p.digest([self.system, self.user]),
        }
        self.directory = self.audit / "requests" / self.job["job_id"] / "attempt_001"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.started = {"job_id": self.job["job_id"], "attempt": 1, "created_utc": "2026-11-23T10:00:00+00:00"}
        usage = ({"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120} if provider == "mistral"
                 else {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120})
        if provider == "anthropic":
            usage.update(cache_read_input_tokens=0, cache_creation_input_tokens=0)
        self.response = {
            "status": "ok", "text": json.dumps({"assessments": [
                {"rule_id": rule, "outcome": outcome, "evidence_refs": ["REQUIREMENT", "DECLARE", "E0001"],
                 "justification": "The supplied requirement and evidence support this judgement."}
                for rule, outcome in (("R01", "compliance"), ("R02", "undetermined"))
            ]}),
            "attempted": True, "elapsed_seconds": 3.25, "http_status": 200,
            "usage": usage, "model_returned": self.provider["model"], "response_id": "response-fixture",
            "request_id": "request-fixture", "finish_reason": "stop", "error": None,
            "received_utc": "2026-11-23T10:00:03.250000+00:00", "raw_response": {"fixture": True},
        }
        p.write_json(self.directory / "request.json", self.request)
        p.write_json(self.directory / "started.json", self.started)
        p.write_json(self.directory / "response.json", self.response)

    def sources(self) -> dict[str, bytes]:
        return {name: (self.directory / name).read_bytes() for name in ("request.json", "started.json", "response.json")
                if (self.directory / name).is_file()}

    def recover(self):
        with patch.object(p, "send_request", side_effect=AssertionError("Local replay must never contact an API.")):
            return recover_case_assessment(self.audit, self.job, self.directory, self.pairs)

    def test_saved_complete_responses_recover_in_the_same_attempt_for_every_provider(self):
        for provider in ("mistral", "openai", "anthropic"):
            with self.subTest(provider=provider):
                if provider != "mistral":
                    self.archive(provider)
                before = self.sources()
                result = self.recover()
                self.assertEqual(result["status"], "ok")
                self.assertEqual([row["outcome"] for row in result["assessments"]], ["compliance", "undetermined"])
                self.assertTrue(result["recovered_from_response_archive"])
                self.assertEqual(result["completed_utc"], self.response["received_utc"])
                self.assertEqual(result["completion_time_basis"], "response.received_utc")
                self.assertEqual(result["request_sha256"], self.job["request_sha256"])
                self.assertEqual(result["raw_response_sha256"], p.sha256_file(self.directory / "response.json"))
                self.assertEqual((result["input_tokens"], result["output_tokens"], result["total_tokens"]), (100, 20, 120))
                self.assertEqual(result["elapsed_seconds"], 3.25)
                self.assertTrue(result["call_attempted"])
                self.assertEqual(result["response_id"], "response-fixture")
                self.assertEqual(result["provider_request_id"], "request-fixture")
                self.assertEqual(result, p._attempt_result(self.directory, self.job))
                self.assertEqual(self.sources(), before)
                self.assertEqual(list(self.directory.parent.iterdir()), [self.directory])

    def test_phase_b_uses_frozen_document_references_when_replaying(self):
        self.archive("mistral", "B")
        text = json.loads(self.response["text"])
        text["assessments"][0]["evidence_refs"] = ["REQUIREMENT", "D0001"]
        self.response["text"] = json.dumps(text)
        p.write_json(self.directory / "response.json", self.response)
        result = self.recover()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["assessments"][0]["evidence_refs"], ["REQUIREMENT", "D0001"])

    def test_existing_assessment_is_never_overwritten_or_revalidated(self):
        target = self.directory / "assessment.json"
        target.write_bytes(b"Existing derived evidence is preserved exactly.\n")
        before, original = self.sources(), target.read_bytes()
        self.assertIsNone(self.recover())
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(self.sources(), before)

    def test_started_only_attempt_remains_ambiguous_without_retransmission(self):
        (self.directory / "response.json").unlink()
        before = self.sources()
        self.assertIsNone(self.recover())
        self.assertFalse((self.directory / "assessment.json").exists())
        self.assertEqual(p._attempt_result(self.directory, self.job)["status"], "interrupted")
        self.assertEqual(self.sources(), before)

    def test_missing_request_or_start_record_prevents_local_recovery(self):
        for name in ("request.json", "started.json"):
            with self.subTest(name=name):
                path = self.directory / name
                original = path.read_bytes()
                path.unlink()
                before = self.sources()
                with self.assertRaisesRegex(ValueError, "request.json and started.json"):
                    self.recover()
                self.assertEqual(self.sources(), before)
                self.assertFalse((self.directory / "assessment.json").exists())
                path.write_bytes(original)

    def test_provider_error_and_incomplete_generation_remain_operational_outcomes(self):
        for status, http in (("api_error", 429), ("error", 402), ("incomplete", 200)):
            with self.subTest(status=status):
                self.response.update(status=status, http_status=http, error={"code": "quota_or_length"})
                p.write_json(self.directory / "response.json", self.response)
                before = self.sources()
                with patch.object(case_archive, "parse_case_assessment", side_effect=AssertionError("Do not promote a provider failure.")):
                    result = self.recover()
                self.assertEqual(result["status"], status)
                self.assertEqual(result["assessments"], [])
                self.assertEqual(result["error"], {"code": "quota_or_length"})
                self.assertNotIn("outcome", result)
                self.assertEqual(result["total_tokens"], 120)
                self.assertEqual(self.sources(), before)
                (self.directory / "assessment.json").unlink()

    def test_non_json_model_text_is_recorded_without_fabricating_legal_answers(self):
        self.response["text"] = '{"assessments":['
        p.write_json(self.directory / "response.json", self.response)
        result = self.recover()
        self.assertEqual(result["status"], "invalid_json")
        self.assertEqual(len(result["assessments"]), 2)
        self.assertTrue(all(row["outcome"] is None for row in result["assessments"]))
        self.assertTrue(result["call_attempted"])

    def test_complete_rule_allocation_preserves_an_independently_valid_row(self):
        text = json.loads(self.response["text"])
        text["assessments"][1]["evidence_refs"] = ["D_NOT_SUPPLIED"]
        self.response["text"] = json.dumps(text)
        p.write_json(self.directory / "response.json", self.response)
        result = self.recover()
        self.assertEqual(result["status"], "invalid_output")
        self.assertEqual([row["status"] for row in result["assessments"]], ["ok", "invalid_output"])

    def test_missing_received_timestamp_uses_explicit_estimated_completion_basis(self):
        self.response.pop("received_utc")
        self.started["created_utc"] = "2026-11-23T12:00:00+02:00"
        p.write_json(self.directory / "response.json", self.response)
        p.write_json(self.directory / "started.json", self.started)
        result = self.recover()
        expected = datetime(2026, 11, 23, 10, tzinfo=timezone.utc) + timedelta(seconds=3.25)
        self.assertEqual(result["completed_utc"], expected.isoformat())
        self.assertEqual(result["completion_time_basis"], "estimated_started_created_utc_plus_elapsed_seconds")

    def test_missing_timing_uses_local_time_without_claiming_remote_completion(self):
        self.response.pop("received_utc")
        self.response["elapsed_seconds"] = None
        self.started.pop("created_utc")
        p.write_json(self.directory / "response.json", self.response)
        p.write_json(self.directory / "started.json", self.started)
        local = "2026-11-23T14:00:00+00:00"
        with patch.object(p, "utc_now", return_value=local):
            result = self.recover()
        self.assertEqual(result["completed_utc"], local)
        self.assertEqual(result["recovered_utc"], local)
        self.assertEqual(result["completion_time_basis"], "local_recovery_time_not_remote_completion")
        self.assertIsNone(result["elapsed_seconds"])

    def test_altered_or_amended_request_hash_is_not_silently_accepted(self):
        changed = copy.deepcopy(self.request)
        changed["payload"]["max_tokens"] = 64000
        p.write_json(self.directory / "request.json", changed)
        before = self.sources()
        with self.assertRaisesRegex(ValueError, "exact frozen baseline request hash"):
            self.recover()
        self.assertEqual(self.sources(), before)
        self.assertFalse((self.directory / "assessment.json").exists())

    def test_start_record_job_and_attempt_must_match(self):
        for change in ({"job_id": "another_case"}, {"attempt": 2}, {"attempt": True}):
            with self.subTest(change=change):
                p.write_json(self.directory / "started.json", {**self.started, **change})
                with self.assertRaises(ValueError):
                    self.recover()
                self.assertFalse((self.directory / "assessment.json").exists())

    def test_wrong_case_rules_or_evidence_cannot_supply_replay_references(self):
        original = copy.deepcopy(self.pairs)
        for field, value in (("case_id", "C-2/20"), ("base_rule_id", "R03"), ("requirement", "Changed obligation."), ("declare", 'AtLeastOne("B")')):
            with self.subTest(field=field):
                self.pairs = copy.deepcopy(original)
                self.pairs[0][field] = value
                with self.assertRaises(ValueError):
                    self.recover()
                self.assertFalse((self.directory / "assessment.json").exists())

    def test_archived_payload_prompt_must_match_even_when_a_caller_supplies_its_hash(self):
        changed = copy.deepcopy(self.request)
        changed["payload"]["messages"][1]["content"] = "Another case's evidence."
        p.write_json(self.directory / "request.json", changed)
        self.job["request_sha256"] = p.digest(changed)
        with self.assertRaisesRegex(ValueError, "does not match the supplied frozen case evidence"):
            self.recover()
        self.assertFalse((self.directory / "assessment.json").exists())

    def test_historical_source_bearing_request_cannot_replay_as_current_protocol(self):
        historical = copy.deepcopy(self.request)
        historical["payload"]["messages"][0]["content"] += (
            "\nAssess each requirement against its quoted legal_source using S1."
        )
        p.write_json(self.directory / "request.json", historical)
        self.job["request_sha256"] = p.digest(historical)
        before = self.sources()
        with self.assertRaisesRegex(ValueError, "does not match the supplied frozen case evidence"):
            self.recover()
        self.assertEqual(self.sources(), before)
        self.assertFalse((self.directory / "assessment.json").exists())

    def test_malformed_response_values_raise_without_overwriting_source_evidence(self):
        original = copy.deepcopy(self.response)
        changes = [
            {"status": None}, {"status": 2}, {"text": []}, {"attempted": "true"},
            {"usage": []}, {"error": []}, {"elapsed_seconds": -1}, {"elapsed_seconds": True},
            {"elapsed_seconds": "3.25"}, {"http_status": True}, {"http_status": "200"},
            {"model_returned": []}, {"response_id": 3}, {"request_id": []},
            {"received_utc": 1}, {"received_utc": "2026-11-23T10:00:00"}, {"received_utc": "not a date"},
        ]
        for change in changes:
            with self.subTest(change=change):
                p.write_json(self.directory / "response.json", {**original, **change})
                before = self.sources()
                with self.assertRaises(ValueError):
                    self.recover()
                self.assertEqual(self.sources(), before)
                self.assertFalse((self.directory / "assessment.json").exists())
        for malformed in ("[]", '{"status":"ok","elapsed_seconds":NaN}', '{"status":"ok","status":"error"}'):
            with self.subTest(malformed=malformed):
                (self.directory / "response.json").write_text(malformed, encoding="utf-8")
                before = self.sources()
                with self.assertRaises(ValueError):
                    self.recover()
                self.assertEqual(self.sources(), before)

    def test_response_changed_during_parsing_is_not_accepted(self):
        def change_source(text, rule_ids, refs, **kwargs):
            self.response["raw_response"] = {"changed": True}
            p.write_json(self.directory / "response.json", self.response)
            return parse_case_assessment(text, rule_ids, refs, **kwargs)

        with patch.object(case_archive, "parse_case_assessment", side_effect=change_source):
            with self.assertRaisesRegex(ValueError, "evidence changed during local recovery"):
                self.recover()
        self.assertFalse((self.directory / "assessment.json").exists())

    def test_an_assessment_written_during_replay_is_preserved(self):
        sentinel = {"written_by_another_owner": True}

        def concurrent_assessment(text, rule_ids, refs, **kwargs):
            p.write_json(self.directory / "assessment.json", sentinel)
            return parse_case_assessment(text, rule_ids, refs, **kwargs)

        with patch.object(case_archive, "parse_case_assessment", side_effect=concurrent_assessment):
            self.assertIsNone(self.recover())
        self.assertEqual(p.read_json(self.directory / "assessment.json"), sentinel)


if __name__ == "__main__":
    unittest.main()
