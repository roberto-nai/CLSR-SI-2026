"""Offline API contract checks: no credentials or network calls are used."""

import io
import json
import socket
import ssl
import unittest
import urllib.error
from email.message import Message
from unittest.mock import Mock, patch

from curia_assessment import providers


MODELS = {
    "openai": "gpt-5.5-2026-04-23",
    "anthropic": "claude-sonnet-5-5",
    "mistral": "mistral-large-4-0",
}


def request_for(name="openai"):
    return providers.build_request(
        {"provider": name, "model": MODELS[name], "reasoning_effort": "medium"},
        "System instruction", "Normative source", 16000,
    )


def openai_response():
    return {
        "id": "resp_test", "model": MODELS["openai"], "status": "completed",
        "usage": {"input_tokens": 100, "output_tokens": 50},
        "output": [
            {"type": "reasoning", "summary": []},
            {"type": "message", "role": "assistant", "phase": "commentary",
             "content": [{"type": "output_text", "text": "Preliminary text"}]},
            {"type": "message", "role": "assistant", "phase": "final_answer",
             "content": [{"type": "output_text", "text": '{"outcome": "Undetermined"}'}]},
        ],
    }


class FakeResponse(io.BytesIO):
    def __init__(self, document):
        super().__init__(json.dumps(document).encode())
        self.status = 200
        self.headers = Message()
        self.headers["x-request-id"] = "req_test"


class ProviderTests(unittest.TestCase):
    def setUp(self):
        # A failing mock is the default, so an accidental live call is impossible.
        self.opener = Mock()
        self.opener.open.side_effect = AssertionError("Unexpected HTTP call")
        self.opener_patch = patch.object(providers.urllib.request, "build_opener", return_value=self.opener)
        self.build_opener = self.opener_patch.start()
        self.addCleanup(self.opener_patch.stop)
        self.env_patch = patch.dict(providers.os.environ, {}, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def test_payloads_use_documented_parameters_and_no_tools(self):
        for name in MODELS:
            with self.subTest(provider=name):
                request = request_for(name)
                payload = request["payload"]
                self.assertEqual(request["url"], providers.ENDPOINTS[name])
                self.assertEqual(payload["model"], MODELS[name])
                self.assertFalse(payload["stream"])
                for field in ("temperature", "top_p", "top_k", "tools", "previous_response_id"):
                    self.assertNotIn(field, payload)
                self.assertNotIn("headers", request)
                self.assertNotIn("api_key", request)
        self.assertEqual(request_for("openai")["payload"], {
            "model": MODELS["openai"], "instructions": "System instruction",
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "Normative source"}]}],
            "max_output_tokens": 16000, "reasoning": {"effort": "medium"},
            "store": False, "stream": False,
        })
        anthropic = request_for("anthropic")
        self.assertEqual(anthropic["api_version"], "2026-10-21")
        self.assertEqual(anthropic["payload"]["thinking"], {"type": "adaptive"})
        self.assertEqual(anthropic["payload"]["output_config"], {"effort": "medium"})
        self.assertEqual(anthropic["payload"]["max_tokens"], 16000)
        mistral = request_for("mistral")["payload"]
        self.assertEqual(mistral["reasoning_effort"], "medium")
        self.assertEqual(mistral["max_tokens"], 16000)
        self.assertEqual([m["role"] for m in mistral["messages"]], ["system", "user"])

    def test_openai_extracts_final_answer_and_keeps_metadata(self):
        raw = openai_response()
        result = providers.extract_result("openai", raw)
        self.assertEqual(result["text"], '{"outcome": "Undetermined"}')
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["model_returned"], MODELS["openai"])
        self.assertEqual(result["response_id"], "resp_test")
        self.assertEqual(result["usage"], raw["usage"])
        self.assertEqual(len(raw["output"]), 3)

    def test_openai_reasoning_only_is_not_a_successful_assessment(self):
        raw = openai_response()
        raw["output"] = [{"type": "reasoning", "summary": []}]
        result = providers.extract_result("openai", raw)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "no_text")
        self.assertEqual(result["text"], "")
        raw["status"] = "incomplete"
        raw["incomplete_details"] = {"reason": "max_output_tokens"}
        result = providers.extract_result("openai", raw)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["finish_reason"], "max_output_tokens")
        self.assertIn("max_output_tokens", providers.format_error(result))

    def test_openai_message_without_phase_is_supported(self):
        raw = openai_response()
        raw["output"] = [raw["output"][-1]]
        del raw["output"][0]["phase"]
        result = providers.extract_result("openai", raw)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["text"], '{"outcome": "Undetermined"}')

    def test_openai_commentary_is_not_used_as_a_final_answer(self):
        raw = openai_response()
        raw["output"] = raw["output"][:2]
        result = providers.extract_result("openai", raw)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["text"], "")

    def test_claude_and_mistral_exclude_thinking(self):
        blocks = [{"type": "thinking", "thinking": "Scratch work"},
                  {"type": "text", "text": '{"outcome": "Undetermined"}'}]
        fixtures = {
            "anthropic": {"content": blocks, "stop_reason": "end_turn"},
            "mistral": {"choices": [{"message": {"content": blocks}, "finish_reason": "stop"}]},
        }
        for name, response in fixtures.items():
            with self.subTest(provider=name):
                response["model"] = MODELS[name]
                result = providers.extract_result(name, response)
                self.assertEqual(result["text"], '{"outcome": "Undetermined"}')
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["model_returned"], MODELS[name])

    def test_mistral_string_content_is_supported(self):
        result = providers.extract_result("mistral", {
            "model": MODELS["mistral"],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10},
            "choices": [{"message": {"content": '{"outcome": "Undetermined"}'},
                         "finish_reason": "stop"}],
        })
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["usage"]["completion_tokens"], 10)
        self.assertEqual(result["text"], '{"outcome": "Undetermined"}')

    def test_refusals_and_truncations_are_not_successes(self):
        fixtures = [
            ("openai", {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output": []}, "incomplete"),
            ("openai", {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "No"}]}]}, "refusal"),
            ("anthropic", {"stop_reason": "max_tokens", "content": [{"type": "text", "text": "partial"}]}, "incomplete"),
            ("anthropic", {"stop_reason": "refusal", "content": []}, "refusal"),
            ("mistral", {"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]}, "incomplete"),
            ("mistral", {"choices": [{"finish_reason": "stop", "message": {"content": "", "refusal": "No"}}]}, "refusal"),
        ]
        for name, response, status in fixtures:
            with self.subTest(provider=name, status=status):
                self.assertEqual(providers.extract_result(name, response)["status"], status)

    def test_successful_http_call_preserves_raw_and_request_id(self):
        providers.os.environ["OPENAI_API_KEY"] = "offline-placeholder-not-a-real-key"
        raw = openai_response()
        self.opener.open.side_effect = None
        self.opener.open.return_value = FakeResponse(raw)
        result = providers.send_request(request_for(), timeout_seconds=300)
        self.assertTrue(result["attempted"])
        self.assertEqual(result["raw_response"], raw)
        self.assertEqual(result["request_id"], "req_test")
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(result["status"], "ok")
        self.opener.open.assert_called_once()
        self.assertNotIn("offline-placeholder-not-a-real-key", json.dumps(result))

    def test_call_model_wrapper_preserves_the_exact_provider_configuration(self):
        config = {"provider": "anthropic", "model": MODELS["anthropic"],
                  "reasoning_effort": "medium", "api_version": "2026-10-21"}
        expected = providers.build_request(config, "System instruction", "Case dossier", 16000)
        with patch.object(providers, "send_request", return_value={"status": "ok"}) as send:
            result = providers.call_model(config, "System instruction", "Case dossier", 16000,
                                          timeout_seconds=90)
        send.assert_called_once_with(expected, timeout_seconds=90)
        self.assertEqual(result, {"status": "ok"})
        self.opener.open.assert_not_called()

    def test_each_provider_sends_the_archived_payload_without_persisting_credentials(self):
        fixtures = {
            "openai": openai_response(),
            "anthropic": {"model": MODELS["anthropic"], "stop_reason": "end_turn",
                          "content": [{"type": "text", "text": "{}"}]},
            "mistral": {"model": MODELS["mistral"], "choices": [
                {"message": {"content": "{}"}, "finish_reason": "stop"}]},
        }
        for name, response in fixtures.items():
            with self.subTest(provider=name):
                request = request_for(name)
                archived = json.dumps(request, sort_keys=True)
                secret = "offline-placeholder-not-a-real-key"
                providers.os.environ[request["api_key_env"]] = secret
                self.opener.open.reset_mock()
                self.opener.open.side_effect = None
                self.opener.open.return_value = FakeResponse(response)
                result = providers.send_request(request, timeout_seconds=120)
                http_request = self.opener.open.call_args.args[0]
                self.assertEqual(json.loads(http_request.data), request["payload"])
                self.assertEqual(self.opener.open.call_args.kwargs["timeout"], 120)
                if name == "anthropic":
                    self.assertEqual(http_request.get_header("X-api-key"), secret)
                    self.assertEqual(http_request.get_header("Anthropic-version"), "2026-10-21")
                else:
                    self.assertEqual(http_request.get_header("Authorization"), f"Bearer {secret}")
                self.assertEqual(json.dumps(request, sort_keys=True), archived)
                self.assertNotIn(secret, archived)
                self.assertNotIn(secret, json.dumps(result))
                self.assertEqual(result["status"], "ok")
                self.opener.open.assert_called_once()

    def test_non_finite_timeouts_are_rejected_without_a_request(self):
        for timeout in (float("inf"), float("nan"), 0, -1, True):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                providers.send_request(request_for(), timeout_seconds=timeout)
        self.opener.open.assert_not_called()

    def test_http_errors_are_redacted_without_retry(self):
        secret = "offline-placeholder-not-a-real-key"
        providers.os.environ["OPENAI_API_KEY"] = secret
        for status in (401, 429, 503):
            with self.subTest(status=status):
                self.opener.open.reset_mock()
                body = json.dumps({"error": {"message": "Rejected " + secret}}).encode()
                self.opener.open.side_effect = urllib.error.HTTPError(
                    providers.ENDPOINTS["openai"], status, "Error", Message(), io.BytesIO(body))
                result = providers.send_request(request_for())
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["http_status"], status)
                self.assertNotIn(secret, json.dumps(result))
                self.assertIn("[REDACTED]", result["raw_response"]["error"]["message"])
                self.assertIn("[REDACTED]", result["error"]["detail"])
                self.opener.open.assert_called_once()

    def test_http_error_keeps_provider_cause_and_request_id(self):
        providers.os.environ["OPENAI_API_KEY"] = "offline-placeholder-not-a-real-key"
        cases = [
            (400, "invalid_request_error", "unsupported_parameter", "reasoning.effort"),
            (404, "invalid_request_error", "model_not_found", "model"),
            (429, "insufficient_quota", "credit_balance_exhausted", None),
        ]
        for status, error_type, code, parameter in cases:
            with self.subTest(status=status, code=code):
                self.opener.open.reset_mock()
                message = f"Provider explanation for {code}."
                raw = {"error": {"message": message, "type": error_type, "code": code, "param": parameter}}
                headers = Message()
                headers["x-request-id"] = "req_failure"
                self.opener.open.side_effect = urllib.error.HTTPError(
                    providers.ENDPOINTS["openai"], status, "Error", headers,
                    io.BytesIO(json.dumps(raw).encode()),
                )
                result = providers.send_request(request_for())
                self.assertEqual(result["error"]["kind"], "http_error")
                self.assertEqual(result["error"]["detail"], message)
                self.assertEqual(result["error"]["type"], error_type)
                self.assertEqual(result["error"]["code"], code)
                self.assertEqual(result["error"].get("param"), parameter)
                self.assertEqual(result["request_id"], "req_failure")
                summary = providers.format_error(result)
                self.assertIn(f"HTTP {status}", summary)
                self.assertIn(f"code={code}", summary)
                self.assertIn(message, summary)
                self.assertIn("request_id=req_failure", summary)
                self.opener.open.assert_called_once()

    def test_failed_openai_response_keeps_provider_error_details(self):
        result = providers.extract_result("openai", {
            "id": "resp_failed", "status": "failed", "model": MODELS["openai"],
            "error": {"code": "server_error", "message": "The response could not be completed."},
        })
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["finish_reason"], "failed")
        self.assertEqual(result["response_id"], "resp_failed")
        self.assertEqual(result["error"]["code"], "server_error")
        self.assertIn("could not be completed", providers.format_error(result))

    def test_legacy_error_archive_can_be_diagnosed_without_a_new_request(self):
        archived = {
            "status": "error", "http_status": 404, "request_id": "req_old",
            "error": {"kind": "http_error", "detail": "HTTP 404; no automatic retry"},
            "raw_response": {"error": {
                "message": "The configured model is not available to this project.",
                "type": "invalid_request_error", "code": "model_not_found", "param": "model",
            }},
        }
        original = json.dumps(archived, sort_keys=True)
        summary = providers.format_error(archived)
        self.assertIn("model_not_found", summary)
        self.assertIn("not available to this project", summary)
        self.assertEqual(json.dumps(archived, sort_keys=True), original)
        self.opener.open.assert_not_called()

    def test_transport_diagnostics_identify_certificate_timeout_and_dns_failures(self):
        providers.os.environ["OPENAI_API_KEY"] = "offline-placeholder-not-a-real-key"
        cases = [
            (urllib.error.URLError(ssl.SSLCertVerificationError(1, "private details")), "certificate_verify_failed"),
            (TimeoutError("private details"), "timeout"),
            (urllib.error.URLError(socket.gaierror(-2, "private details")), "dns_error"),
        ]
        for failure, code in cases:
            with self.subTest(code=code):
                self.opener.open.reset_mock()
                self.opener.open.side_effect = failure
                result = providers.send_request(request_for())
                self.assertEqual(result["error"]["kind"], "transport_error")
                self.assertEqual(result["error"]["code"], code)
                self.assertNotIn("private details", json.dumps(result))
                self.assertIn(f"code={code}", providers.format_error(result))
                self.opener.open.assert_called_once()

    def test_error_display_removes_control_characters_and_ignores_successes(self):
        summary = providers.format_error({
            "status": "error",
            "error": {"code": "invalid_request", "detail": "First line\nSecond line\x1b[31m"},
        })
        self.assertNotIn("\n", summary)
        self.assertNotIn("\x1b", summary)
        self.assertEqual(providers.format_error({"status": "ok", "error": None}), "")

    def test_missing_key_never_opens_connection(self):
        result = providers.send_request(request_for())
        self.assertFalse(result["attempted"])
        self.assertEqual(result["error"]["kind"], "missing_api_key")
        self.opener.open.assert_not_called()
        self.build_opener.assert_not_called()

    def test_redirects_and_transport_failures_are_not_retried(self):
        providers.os.environ["OPENAI_API_KEY"] = "offline-placeholder-not-a-real-key"
        self.opener.open.side_effect = urllib.error.URLError("offline")
        result = providers.send_request(request_for())
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["kind"], "transport_error")
        self.opener.open.assert_called_once()
        handler = self.build_opener.call_args.args[0]
        self.assertIsNone(handler.redirect_request(None, None, 302, "redirect", {}, "https://example.invalid"))
        modified = request_for()
        modified["url"] = "https://example.invalid"
        with self.assertRaises(ValueError):
            providers.send_request(modified)
        self.opener.open.assert_called_once()


if __name__ == "__main__":
    unittest.main()
