"""Small, auditable adapters for three direct cloud APIs (Python stdlib only).

No SDK, implicit conversation history, web tools, automatic retries, fallback
models, or model-generated repairs are used. ``build_request`` contains no
credentials and can be archived. ``call_model`` reads the relevant key only
when a real HTTP request is made and returns the original JSON response plus
a normalised summary. The runner decides where to save these records.

API documentation checked on 2026-11-09:
https://developers.openai.com/api/docs/guides/text
https://developers.openai.com/api/docs/guides/reasoning
https://platform.claude.com/docs/en/api/messages/create
https://platform.claude.com/docs/en/api/versioning
https://platform.claude.com/docs/en/build-with-claude/effort
https://docs.mistral.ai/api/endpoint/chat
https://docs.mistral.ai/inference/conversations/reasoning
"""

from __future__ import annotations

import json
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from typing import Any


ENDPOINTS = {
    "openai": "https://api.openai.com/v1/responses",
    "anthropic": "https://api.anthropic.com/v1/messages",
    "mistral": "https://api.mistral.ai/v1/chat/completions",
}

DEFAULT_KEY_ENVS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "mistral": "MISTRAL_API_KEY",
}

DEFAULT_ANTHROPIC_VERSION = "2026-10-21"


def build_request(
    provider: dict[str, Any],
    system_prompt: str,
    user_prompt: str,
    max_output_tokens: int,
) -> dict[str, Any]:
    """Construct a serialisable, credential-free request description.

    Configuration fields: ``provider``, ``model``, optional ``api_key_env``,
    ``reasoning_effort`` (default medium), and Anthropic ``api_version``.
    Descriptive metadata such as ``id`` and ``label`` is not transmitted.
    Equal effort labels are NOT a claim of equal compute across providers.
    """
    name = provider.get("provider")
    if name not in ENDPOINTS:
        raise ValueError(f"Unsupported provider: {name!r}.")
    model = provider.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Specify a non-empty model identifier.")
    if "latest" in model.lower():
        raise ValueError("Use an explicit model version rather than a latest alias.")
    if not isinstance(system_prompt, str) or not isinstance(user_prompt, str):
        raise ValueError("Prompts must be strings.")
    if not system_prompt.strip() or not user_prompt.strip():
        raise ValueError("Prompts must not be empty.")
    if isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int) or max_output_tokens <= 0:
        raise ValueError("max_output_tokens must be a positive integer.")
    if any(key in provider for key in ("api_key", "authorization", "base_url", "url", "headers")):
        raise ValueError("Store credentials in environment variables; API endpoints are fixed.")
    if any(key in provider for key in ("temperature", "top_p", "top_k", "seed", "random_seed")):
        raise ValueError("This protocol does not use custom sampling parameters or seeds.")

    key_env = provider.get("api_key_env", DEFAULT_KEY_ENVS[name])
    if not isinstance(key_env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key_env):
        raise ValueError("api_key_env must be an environment variable name.")
    effort = provider.get("reasoning_effort", "medium")
    efforts = {
        "openai": {"none", "low", "medium", "high", "xhigh"},
        "anthropic": {"low", "medium", "high", "xhigh", "max"},
        "mistral": {"low", "medium", "high"},
    }
    if effort not in efforts[name]:
        raise ValueError(f"Unsupported reasoning_effort for the configured {name} model: {effort!r}.")

    api_version = None
    if name == "openai":
        payload = {
            "model": model,
            "instructions": system_prompt,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": user_prompt}]}],
            "max_output_tokens": max_output_tokens,
            "reasoning": {"effort": effort},
            "store": False,
            "stream": False,
        }
    elif name == "anthropic":
        api_version = provider.get("api_version", DEFAULT_ANTHROPIC_VERSION)
        if not isinstance(api_version, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", api_version):
            raise ValueError("The Anthropic API version must use the YYYY-MM-DD format.")
        payload = {
            "model": model,
            "system": system_prompt,
            "messages": [{"role": "user", "content": [{"type": "text", "text": user_prompt}]}],
            "max_tokens": max_output_tokens,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": effort},
            "stream": False,
        }
    else:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_output_tokens,
            "reasoning_effort": effort,
            "stream": False,
        }
    return {
        "provider": name,
        "url": ENDPOINTS[name],
        "method": "POST",
        "api_key_env": key_env,
        "api_version": api_version,
        "payload": payload,
    }


def _text_blocks(content: Any, accepted_types: set[str]) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") in accepted_types
        and isinstance(block.get("text"), str)
    )


def _provider_error(error: Any, kind: str = "provider_error") -> dict[str, Any]:
    """Retain the provider's diagnostic fields without discarding its message."""
    normalised: dict[str, Any] = {"kind": kind}
    if isinstance(error, dict):
        message = error.get("message")
        normalised["detail"] = message if isinstance(message, str) and message else "The provider returned an error."
        for field in ("type", "code", "param"):
            if error.get(field) is not None:
                normalised[field] = error[field]
    elif isinstance(error, str) and error:
        normalised["detail"] = error
    else:
        normalised["detail"] = "The provider returned an error."
    return normalised


def format_error(result: dict[str, Any]) -> str:
    """Format an already sanitised response or saved result for the terminal.

    Older response archives may contain the detailed provider error only in
    raw_response. Reading that field here makes those errors diagnosable
    without repeating an API call or altering the original record.
    """
    error = result.get("error")
    if not error and result.get("status") != "error":
        return ""
    error = dict(error) if isinstance(error, dict) else {"detail": error}
    raw = result.get("raw_response")
    if isinstance(raw, dict) and raw.get("error"):
        error.update(_provider_error(raw["error"], error.get("kind", "provider_error")))
    elif isinstance(error.get("detail"), dict):
        error.update(_provider_error(error["detail"], error.get("kind", "provider_error")))

    def display(value: Any) -> str:
        # Do not allow diagnostic text to introduce terminal control characters.
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", text).split())

    parts = []
    if result.get("http_status") is not None:
        parts.append(f"HTTP {display(result['http_status'])}")
    for field, label in (("type", "type"), ("code", "code"), ("param", "parameter")):
        if error.get(field) is not None:
            parts.append(f"{label}={display(error[field])}")
    detail = error.get("detail") or "No detailed error was recorded; inspect response.json."
    parts.append(display(detail))
    if result.get("request_id"):
        parts.append(f"request_id={display(result['request_id'])}")
    return "; ".join(parts)


def _transport_error(exc: BaseException) -> dict[str, Any]:
    """Describe common transport failures without echoing URLs or credentials."""
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    error: dict[str, Any] = {
        "kind": "transport_error",
        "exception_type": type(exc).__name__,
        "reason_type": type(reason).__name__,
    }
    if isinstance(reason, ssl.SSLCertVerificationError):
        error["code"] = "certificate_verify_failed"
        error["detail"] = (
            "TLS certificate verification failed. Check the certificate configuration "
            "of the Python installation; certificate verification remains enabled."
        )
    elif isinstance(reason, (TimeoutError, socket.timeout)):
        error["code"] = "timeout"
        error["detail"] = "The request timed out; remote outcome unknown, no automatic retry."
    elif isinstance(reason, socket.gaierror):
        error["code"] = "dns_error"
        error["detail"] = "The API host could not be resolved; no automatic retry."
    elif isinstance(reason, ssl.SSLError):
        error["code"] = "tls_error"
        error["detail"] = "The TLS connection failed; no automatic retry."
    elif isinstance(reason, ConnectionError):
        error["code"] = "connection_error"
        error["detail"] = "The connection failed or closed unexpectedly; remote outcome unknown, no automatic retry."
    else:
        error["detail"] = f"{type(exc).__name__}; remote outcome unknown, no automatic retry."
    if isinstance(reason, OSError) and reason.errno is not None:
        error["errno"] = reason.errno
    return error


def extract_result(provider_name: str, response: dict[str, Any]) -> dict[str, Any]:
    """Normalise a provider JSON response without changing its generated text.

    Returned status describes transport/generation, not JSON validity or the
    legal/formal correctness of an extracted rule. Refusals and incomplete
    responses must remain visible experimental outcomes.
    """
    if provider_name not in ENDPOINTS:
        raise ValueError(f"Unsupported provider: {provider_name!r}.")
    if not isinstance(response, dict):
        raise ValueError("The API response must be a JSON object.")
    result: dict[str, Any] = {
        "status": "error",
        "text": "",
        "model_returned": response.get("model"),
        "response_id": response.get("id"),
        "usage": response.get("usage"),
        "finish_reason": None,
        "error": None,
    }
    if response.get("error"):
        result["error"] = _provider_error(response["error"])
        result["finish_reason"] = response.get("status")
        return result

    if provider_name == "openai":
        output = response.get("output", [])
        if not isinstance(output, list):
            result["error"] = {"kind": "unexpected_response", "detail": "output is not a list"}
            return result
        messages = [
            item for item in output
            if isinstance(item, dict) and item.get("type") == "message"
            and item.get("role", "assistant") == "assistant"
        ]
        finals = [message for message in messages if message.get("phase") == "final_answer"]
        messages = finals or [message for message in messages if message.get("phase") != "commentary"]
        result["text"] = "\n".join(
            _text_blocks(message.get("content"), {"output_text"}) for message in messages
        )
        refusal = any(
            isinstance(block, dict) and block.get("type") == "refusal"
            for message in messages
            for block in (message.get("content") or [])
        )
        completion_status = response.get("status")
        result["finish_reason"] = completion_status
        if refusal:
            result["status"] = "refusal"
        elif completion_status == "incomplete":
            result["status"] = "incomplete"
            result["finish_reason"] = (response.get("incomplete_details") or {}).get("reason", "incomplete")
        elif completion_status == "completed":
            result["status"] = "ok"
        else:
            result["error"] = {"kind": "unexpected_status", "detail": completion_status}
    elif provider_name == "anthropic":
        result["text"] = _text_blocks(response.get("content"), {"text"})
        stop = response.get("stop_reason")
        result["finish_reason"] = stop
        if stop in {"end_turn", "stop_sequence"}:
            result["status"] = "ok"
        elif stop in {"max_tokens", "model_context_window_exceeded", "pause_turn"}:
            result["status"] = "incomplete"
        elif stop == "refusal":
            result["status"] = "refusal"
        else:
            result["error"] = {"kind": "unexpected_stop_reason", "detail": stop}
    else:
        choices = response.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            result["error"] = {"kind": "unexpected_response", "detail": "Expected exactly one completion"}
            return result
        choice = choices[0]
        message = choice.get("message") or {}
        if not isinstance(message, dict):
            result["error"] = {"kind": "unexpected_response", "detail": "message is not an object"}
            return result
        result["text"] = _text_blocks(message.get("content"), {"text"})
        finish = choice.get("finish_reason")
        result["finish_reason"] = finish
        if finish in {"content_filter", "refusal"} or message.get("refusal"):
            result["status"] = "refusal"
        elif finish == "stop" and not message.get("tool_calls"):
            result["status"] = "ok"
        elif finish in {"length", "model_length", "max_tokens"}:
            result["status"] = "incomplete"
        else:
            result["error"] = {"kind": "unexpected_finish_reason", "detail": finish}
    if result["status"] == "ok" and not result["text"].strip():
        result["status"] = "error"
        result["error"] = {"kind": "empty_output", "detail": "No final text was returned"}
    return result


def _redact(value: Any, secret: str) -> Any:
    """Avoid persisting a credential if a provider echoes it in an error."""
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]") if secret else value
    if isinstance(value, list):
        return [_redact(item, secret) for item in value]
    if isinstance(value, dict):
        return {_redact(key, secret): _redact(item, secret) for key, item in value.items()}
    return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward an authenticated request to a redirected endpoint."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def call_model(request: dict[str, Any], timeout: float = 180.0) -> dict[str, Any]:
    """Make exactly one API request; return an auditable, sanitised outcome.

    A timeout is ambiguous: the provider may have generated and billed a reply.
    Consequently this function never retries, even on rate limits or HTTP 5xx.
    Missing keys cause a local error before any request is sent.
    """
    name = request.get("provider")
    if name not in ENDPOINTS or request.get("url") != ENDPOINTS[name] or request.get("method") != "POST":
        raise ValueError("Invalid request description or unauthorised endpoint.")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("timeout must be positive.")
    key_env = request.get("api_key_env")
    if not isinstance(key_env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key_env):
        raise ValueError("Invalid environment variable name for the API key.")
    result: dict[str, Any] = {
        "status": "error",
        "text": "",
        "model_returned": None,
        "response_id": None,
        "usage": None,
        "request_id": None,
        "finish_reason": None,
        "error": None,
        "http_status": None,
        "raw_response": None,
        "raw_response_text": None,
        "elapsed_seconds": 0.0,
        "attempted": False,
        "response_headers": {},
    }
    secret = os.environ.get(key_env, "").strip()
    if not secret:
        result["error"] = {"kind": "missing_api_key", "detail": f"Environment variable {key_env} is not set"}
        return result
    if "\r" in secret or "\n" in secret:
        result["error"] = {"kind": "invalid_api_key", "detail": "The API key contains invalid characters"}
        return result

    headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "curia-gpai/1.0"}
    if name == "anthropic":
        headers["x-api-key"] = secret
        headers["anthropic-version"] = request["api_version"]
    else:
        headers["Authorization"] = f"Bearer {secret}"
    body = json.dumps(request["payload"], ensure_ascii=False, allow_nan=False).encode("utf-8")
    http_request = urllib.request.Request(ENDPOINTS[name], data=body, headers=headers, method="POST")
    opener = urllib.request.build_opener(_NoRedirect())
    started = time.monotonic()
    response_text = ""
    response_headers: Any = {}
    try:
        result["attempted"] = True
        with opener.open(http_request, timeout=timeout) as response:
            result["http_status"] = response.status
            response_headers = response.headers
            response_text = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        result["http_status"] = exc.code
        response_headers = exc.headers or {}
        try:
            response_text = exc.read().decode("utf-8", errors="replace")
        except (OSError, TimeoutError):
            response_text = ""
        finally:
            exc.close()
        result["error"] = {"kind": "http_error", "detail": f"HTTP {exc.code}; no automatic retry"}
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        result["error"] = _transport_error(exc)
    finally:
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    for key in ("x-request-id", "request-id", "retry-after", "date"):
        value = response_headers.get(key)
        if value is not None:
            result["response_headers"][key] = value
    result["request_id"] = result["response_headers"].get("x-request-id") or result["response_headers"].get("request-id")
    if response_text:
        try:
            raw = json.loads(response_text)
        except json.JSONDecodeError:
            result["raw_response_text"] = response_text
            if result["error"] is None:
                result["error"] = {"kind": "non_json_response", "detail": "The API did not return valid JSON"}
        else:
            result["raw_response"] = raw
            if result["error"] is not None and isinstance(raw, dict) and raw.get("error"):
                # HTTP errors still have useful JSON bodies: retain their
                # message, code, type and parameter rather than only the status.
                result["error"] = _provider_error(raw["error"], kind=result["error"]["kind"])
            elif result["error"] is None:
                if isinstance(raw, dict):
                    try:
                        result.update(extract_result(name, raw))
                    except (ValueError, TypeError, AttributeError, IndexError) as exc:
                        result["error"] = {"kind": "unexpected_response", "detail": type(exc).__name__}
                else:
                    result["error"] = {"kind": "unexpected_response", "detail": "The JSON response is not an object"}
            if not result["request_id"] and isinstance(raw, dict) and isinstance(raw.get("request_id"), str):
                result["request_id"] = raw["request_id"]
    elif result["error"] is None:
        result["error"] = {"kind": "empty_http_response", "detail": "The HTTP response has no content"}
    return _redact(result, secret)
