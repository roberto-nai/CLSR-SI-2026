"""Distinguish exhausted account budgets from throttling and model-output errors."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import unittest

from curia_assessment.resumption import classify_provider_stop


class ProviderStopClassificationTests(unittest.TestCase):
    def test_payment_required_stops_each_provider_without_copying_error_text(self) -> None:
        for provider in ("openai", "anthropic", "mistral"):
            with self.subTest(provider=provider):
                response = {"http_status": 402, "error": {"message": "Private billing account detail [REDACTED]."}}
                result = classify_provider_stop(provider, response)
                self.assertEqual(result, {"reason": "account_budget_exhausted", "can_resume": True})
                self.assertNotIn("Private", json.dumps(result))

    def test_openai_quota_credit_and_spend_codes_are_not_temporary_rate_limits(self) -> None:
        for code in ("insufficient_quota", "credit_balance_exhausted", "organization_spend_limit_exceeded",
                     "project_spend_limit_exceeded", "organization_usage_limit_exceeded"):
            for status in (429, 200):
                with self.subTest(code=code, status=status):
                    response = {"http_status": status, "error": {"type": "insufficient_quota", "code": code}}
                    self.assertEqual(classify_provider_stop("openai", response)["reason"], "account_budget_exhausted")

    def test_anthropic_legacy_credit_and_configured_spend_messages_override_bad_request(self) -> None:
        for message in ("Your credit balance is too low.", "Credit balance exhausted; add credits.",
                        "You have reached your specified API usage limits.",
                        "Configured workspace API usage limit reached."):
            with self.subTest(message=message):
                result = classify_provider_stop("anthropic", {"http_status": 400,
                    "error": {"type": "invalid_request_error", "detail": message}})
                self.assertEqual(result["reason"], "account_budget_exhausted")

    def test_anthropic_nested_spend_cap_details_are_read_from_preserved_raw_error(self) -> None:
        response = {"http_status": 429, "error": {"type": "rate_limit_error"},
                    "raw_response": {"type": "error", "error": {"type": "rate_limit_error",
                        "details": {"error_code": "enforced_spend_limit_reached"}}}}
        self.assertEqual(classify_provider_stop("anthropic", response)["reason"], "account_budget_exhausted")
        # Absence of Retry-After by itself must not invent an account-budget error.
        self.assertEqual(classify_provider_stop("anthropic", {"http_status": 429,
                         "error": {"type": "rate_limit_error"}})["reason"], "rate_limit")

    def test_mistral_flat_error_json_and_numeric_billing_codes_work_even_with_http_200(self) -> None:
        for code in ("2300", "2301", "2302", "2303", "2304", "2308", 2308):
            with self.subTest(code=code):
                result = classify_provider_stop("mistral", {"http_status": 200,
                    "raw_response": {"object": "error", "code": code, "message": "Account blocked."}})
                self.assertEqual(result["reason"], "account_budget_exhausted")
        flat = {"object": "error", "type": "billing_api_budget_exhausted", "code": "2303"}
        self.assertEqual(classify_provider_stop("mistral", flat)["reason"], "account_budget_exhausted")

    def test_rate_limit_retry_after_supports_seconds_decimal_seconds_and_http_dates(self) -> None:
        now = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
        values = (("90", 90), ("1.2", 2), ("Mon, 23 Nov 2026 12:02:00 GMT", 120),
                  ("Mon, 23 Nov 2026 11:59:00 GMT", 0))
        for header, expected in values:
            with self.subTest(header=header):
                result = classify_provider_stop("openai", {"http_status": 429,
                    "error": {"code": "rate_limit_exceeded"}, "response_headers": {"Retry-After": header}}, now=now)
                self.assertEqual(result, {"reason": "rate_limit", "can_resume": True, "retry_after_seconds": expected})

    def test_invalid_retry_after_values_do_not_create_a_fabricated_wait(self) -> None:
        for value in ("not a delay", "-1", "nan", "inf", True, None, {}):
            with self.subTest(value=value):
                result = classify_provider_stop("mistral", {"http_status": 429,
                    "response_headers": {"retry-after": value}})
                self.assertNotIn("retry_after_seconds", result)

    def test_authentication_and_permissions_are_separate_from_account_budget(self) -> None:
        for provider in ("openai", "anthropic", "mistral"):
            for status in (401, 403):
                with self.subTest(provider=provider, status=status):
                    result = classify_provider_stop(provider, {"http_status": status,
                        "error": {"type": "authentication_error", "message": "Access denied."}})
                    self.assertEqual(result, {"reason": "authentication_error", "can_resume": True})

    def test_invalid_frozen_requests_are_not_marked_resumable_without_correction(self) -> None:
        for status in (400, 404, 413, 422):
            with self.subTest(status=status):
                result = classify_provider_stop("mistral", {"http_status": status,
                    "error": {"type": "invalid_request_error", "detail": "Prompt exceeds the context window."}})
                self.assertEqual(result, {"reason": "invalid_request", "can_resume": False})

    def test_missing_credentials_and_local_unsent_failures_do_not_claim_a_sent_api_call(self) -> None:
        result = classify_provider_stop("openai", {"http_status": None, "attempted": False,
            "error": {"kind": "missing_api_key", "detail": "The configured environment variable is absent."}})
        self.assertEqual(result, {"reason": "missing_credentials", "can_resume": True})
        self.assertEqual(classify_provider_stop("anthropic", {"attempted": False,
            "error": {"kind": "invalid_api_key"}})["reason"], "authentication_error")
        self.assertEqual(classify_provider_stop("mistral", {"attempted": False,
            "error": {"kind": "local_configuration_error"}})["reason"], "request_not_sent")

    def test_output_length_format_errors_and_model_text_never_claim_exhausted_account_credit(self) -> None:
        samples = [
            {"status": "ok", "http_status": 200, "attempted": True,
             "text": "The quoted evidence discusses insufficient_quota and billing errors.", "error": None},
            {"status": "incomplete", "http_status": 200, "attempted": True, "finish_reason": "length",
             "raw_response": {"choices": [{"message": {"content": "API budget exhausted"}}]}},
            {"status": "incomplete", "http_status": 200, "attempted": True,
             "raw_response": {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}},
            {"status": "invalid_json", "http_status": 200, "attempted": True,
             "error": {"kind": "invalid_json", "detail": "The generated response was not JSON."}},
            {"status": "incomplete", "http_status": 200, "attempted": True,
             "error": {"detail": "The output token budget is exhausted."}},
            {"status": "error", "http_status": 503, "attempted": True,
             "error": {"type": "overloaded_error", "detail": "Model capacity unavailable."}},
        ]
        for provider in ("openai", "anthropic", "mistral"):
            for response in samples:
                with self.subTest(provider=provider, response=response):
                    self.assertIsNone(classify_provider_stop(provider, response))

    def test_validation_error_input_echo_is_not_scanned_for_billing_words(self) -> None:
        response = {"http_status": 400, "error": {"detail": [{"type": "value_error",
            "msg": "Invalid schema field.", "input": "The legal source says credit balance exhausted."}]}}
        self.assertEqual(classify_provider_stop("mistral", response)["reason"], "invalid_request")

    def test_classification_does_not_modify_the_preserved_response(self) -> None:
        response = {"http_status": 429, "error": {"type": "insufficient_quota"},
                    "response_headers": {"retry-after": "5"}}
        original = copy.deepcopy(response)
        self.assertEqual(classify_provider_stop("openai", response)["reason"], "account_budget_exhausted")
        self.assertEqual(response, original)

    def test_invalid_classifier_arguments_fail_clearly(self) -> None:
        for provider in ("unknown", None, []):
            with self.subTest(provider=provider), self.assertRaisesRegex(ValueError, "provider"):
                classify_provider_stop(provider, {})
        with self.assertRaisesRegex(ValueError, "response"):
            classify_provider_stop("openai", [])
        with self.assertRaisesRegex(ValueError, "now"):
            classify_provider_stop("openai", {}, now="today")


if __name__ == "__main__":
    unittest.main()
