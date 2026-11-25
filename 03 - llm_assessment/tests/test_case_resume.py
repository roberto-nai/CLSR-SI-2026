"""Exercise provider-budget checkpoints and recovery without network calls."""
import contextlib
import io
import json
import unittest
from unittest.mock import patch

import test_case_pipeline as case_fixture
from curia_assessment import pipeline


class CaseResumeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = case_fixture.CasePipelineTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.config = self.fixture.config
        self.config.update(conditions=["B"], max_output_tokens=32000, request_workers=3,
                           requests_per_provider=1, providers=[
            {"id": provider, "provider": provider, "model": "test-pinned-2026-11-17"}
            for provider in ("openai", "anthropic", "mistral")])
        self.fixture.fixture._write_config()
        self.audit = self.fixture.audit
        # These tests exercise persistence/HTTP behaviour, not Excel rendering.
        workbook = patch("curia_assessment.evaluation_exports.write_expert_workbooks")
        workbook.start()
        self.addCleanup(workbook.stop)

    def run_case(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return pipeline.run(self.fixture.config_path, **kwargs)

    def test_budget_stop_keeps_other_providers_running_and_recharge_resumes_only_missing_cases(self):
        def response(request, **kwargs):
            if request["provider"] == "mistral":
                return {"status": "error", "attempted": True, "http_status": 402,
                        "error": {"code": "2308", "detail": "Billing wallet depleted."},
                        "usage": None, "elapsed_seconds": 0.01}
            return self.fixture.response()
        with patch.object(pipeline, "send_request", side_effect=response) as first_send:
            first = self.run_case()
        self.assertEqual(first_send.call_count, 5)
        self.assertEqual((first["successful"], first["failed"], first["blocked_providers"]), (4, 1, 1))
        checkpoint = pipeline.read_json(self.audit / "resume_status.json")
        self.assertEqual(checkpoint["complete"], 4)
        self.assertEqual(checkpoint["providers"]["mistral"]["state"], "account_budget_exhausted")
        self.assertEqual(checkpoint["providers"]["mistral"]["remaining"], 2)
        self.assertEqual(checkpoint["providers"]["anthropic"]["state"], "complete")
        with patch.object(pipeline, "send_request") as no_retry:
            self.run_case()
        no_retry.assert_not_called()
        recovered_response = self.fixture.response()
        recovered_response["usage"] = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
        with patch.object(pipeline, "send_request", return_value=recovered_response) as resume_send:
            resumed = self.run_case(retry_failed=True)
        self.assertEqual(resume_send.call_count, 2)
        self.assertEqual((resumed["successful"], resumed["skipped_existing"]), (2, 4))
        self.assertTrue(pipeline.read_json(self.audit / "resume_status.json")["all_complete"])
        resources = self.fixture.csv_rows(self.fixture.output / "model_resources.csv")
        mistral = next(row for row in resources if row["provider_id"] == "mistral")
        self.assertEqual((mistral["n_recorded_attempts"], mistral["n_api_calls"]), ("3", "3"))
        self.assertEqual(mistral["input_tokens"], "200")
        self.assertEqual(mistral["measured_input_token_attempts"], "2")
        self.assertEqual(mistral["output_tokens"], "40")

    def test_received_response_is_replayed_locally_without_paid_retry_or_double_counting(self):
        with patch.object(pipeline, "send_request", return_value=self.fixture.response()):
            self.run_case(provider_id="openai")
        job = next(job for job in self.fixture.jobs() if job["provider_id"] == "openai")
        directory = self.audit / "requests" / job["job_id"] / "attempt_001"
        hashes = {name: pipeline.sha256_file(directory / name) for name in ("request.json", "response.json", "started.json")}
        (directory / "assessment.json").unlink()
        with patch.object(pipeline, "send_request") as send:
            resumed = self.run_case(provider_id="openai", retry_failed=True)
        send.assert_not_called()
        self.assertEqual(resumed["recovered_from_archive"], 1)
        self.assertEqual(resumed["skipped_existing"], 2)
        self.assertTrue(pipeline.read_json(directory / "assessment.json")["recovered_from_response_archive"])
        self.assertEqual(hashes, {name: pipeline.sha256_file(directory / name) for name in hashes})
        self.assertEqual(len(list((directory.parent).glob("attempt_*"))), 1)
        resource = next(row for row in self.fixture.csv_rows(self.fixture.output / "model_resources.csv") if row["provider_id"] == "openai")
        self.assertEqual((resource["n_recorded_attempts"], resource["n_api_calls"]), ("2", "2"))

    def test_openai_and_anthropic_budget_errors_stop_and_resume_independently(self):
        for provider, status, error in (
            ("openai", 429, {"code": "insufficient_quota", "detail": "Credit balance exhausted."}),
            ("anthropic", 400, {"type": "invalid_request_error", "detail": "Your credit balance is too low."}),
        ):
            with self.subTest(provider=provider):
                def unavailable(request, **kwargs):
                    return {"status": "error", "attempted": True, "http_status": status,
                            "error": error, "usage": None, "elapsed_seconds": 0.01}
                with patch.object(pipeline, "send_request", side_effect=unavailable) as send:
                    stopped = self.run_case(provider_id=provider)
                self.assertEqual(send.call_count, 1)
                self.assertEqual(stopped["blocked_providers"], 1)
                checkpoint = pipeline.read_json(self.audit / "resume_status.json")
                self.assertEqual(checkpoint["providers"][provider]["state"], "account_budget_exhausted")
                with patch.object(pipeline, "send_request", return_value=self.fixture.response()) as recover:
                    completed = self.run_case(provider_id=provider, retry_failed=True)
                self.assertEqual(recover.call_count, 2)
                self.assertEqual(completed["successful"], 2)
                checkpoint = pipeline.read_json(self.audit / "resume_status.json")
                self.assertEqual(checkpoint["providers"][provider]["remaining"], 0)

    def test_transmission_without_saved_response_stays_uncertain_until_explicit_retry(self):
        with patch.object(pipeline, "send_request", return_value=self.fixture.response()):
            self.run_case(provider_id="openai")
        job = next(job for job in self.fixture.jobs() if job["provider_id"] == "openai")
        directory = self.audit / "requests" / job["job_id"] / "attempt_001"
        (directory / "assessment.json").unlink()
        (directory / "response.json").unlink()
        with patch.object(pipeline, "send_request") as send:
            self.run_case(provider_id="openai")
        send.assert_not_called()
        statuses = pipeline.read_json(self.audit / "resume_status.json")
        self.assertEqual(statuses["providers"]["openai"]["needs_retry"], 1)
        with patch.object(pipeline, "send_request", return_value=self.fixture.response()) as retry:
            self.run_case(provider_id="openai", retry_failed=True)
        self.assertEqual(retry.call_count, 1)
        self.assertTrue((directory.parent / "attempt_002/assessment.json").is_file())

    def test_invalid_frozen_requests_are_not_resubmitted_by_resume(self):
        failure = {"status": "error", "attempted": True, "http_status": 400,
                   "error": {"type": "invalid_request_error", "detail": "Invalid request parameter."},
                   "usage": None, "elapsed_seconds": 0.01}
        with patch.object(pipeline, "send_request", return_value=failure):
            self.run_case(provider_id="openai")
        with patch.object(pipeline, "send_request", return_value=self.fixture.response()) as resume:
            continued = self.run_case(retry_failed=True)
        self.assertEqual(resume.call_count, 4)
        self.assertEqual({call.args[0]["provider"] for call in resume.call_args_list}, {"anthropic", "mistral"})
        self.assertEqual(continued["blocked_providers"], 1)
        checkpoint = pipeline.read_json(self.audit / "resume_status.json")
        self.assertEqual(checkpoint["providers"]["openai"]["state"], "invalid_request")
        self.assertFalse(checkpoint["providers"]["openai"]["stop"]["can_resume"])


if __name__ == "__main__":
    unittest.main()
