"""Classify provider stop conditions without retrying or recording credentials.

The input is the adapter's already-redacted response. Only HTTP/error metadata
is inspected; model-generated text and documentary evidence are never scanned.
``can_resume`` means the same request may be attempted after the stated external
condition changes. It does not authorise or perform an automatic retry.

Provider semantics:
https://developers.openai.com/api/docs/guides/error-codes
https://platform.claude.com/docs/en/api/errors
https://platform.claude.com/docs/en/api/rate-limits
https://docs.mistral.ai/resources/error-glossary
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import re
from typing import Any

_PROVIDERS = {"openai", "anthropic", "mistral"}
_BUDGET_CODES = {
    "insufficient_quota", "credit_balance_exhausted", "quota_exceeded", "billing_error",
    "billing_hard_limit_reached", "billing_not_active", "billing_required", "payment_required",
    "organization_spend_limit_exceeded", "project_spend_limit_exceeded", "organization_usage_limit_exceeded",
    "usage_limit_exceeded", "spend_limit_exceeded", "enforced_spend_limit_reached",
    "billing_customer_monthly_spending_limit_reached", "billing_workspace_monthly_spending_limit_reached",
    "billing_payment_failed", "billing_api_budget_exhausted", "billing_vibe_budget_exhausted", "billing_wallet_depleted",
}
_MISTRAL_BUDGET_CODES = {"2300", "2301", "2302", "2303", "2304", "2308"}
_RATE_CODES = {"rate_limit_error", "rate_limit_exceeded", "rate_limited", "too_many_requests", "slow_down"}
_AUTH_CODES = {"invalid_api_key", "authentication_error", "permission_error", "permission_denied", "unauthorized", "forbidden"}
_MISSING_CODES = {"missing_api_key", "missing_credentials"}
_INVALID_CODES = {"invalid_request_error", "invalid_request", "bad_request", "not_found_error", "unknown_model", "invalid_model"}


def _normalise(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def _error_signals(response: Mapping[str, Any]) -> tuple[set[str], str]:
    codes: set[str] = set()
    messages = []

    def collect(error: Any) -> None:
        if isinstance(error, str):
            messages.append(error.casefold())
        elif isinstance(error, Mapping):
            for name in ("type", "code", "kind", "error_code"):
                value = error.get(name)
                if isinstance(value, str) or isinstance(value, int) and not isinstance(value, bool):
                    codes.add(_normalise(str(value)))
            for name in ("message", "detail", "msg"):
                value = error.get(name)
                if isinstance(value, str):
                    messages.append(value.casefold())
                elif isinstance(value, (Mapping, list)):
                    collect(value)
            for name in ("error", "details"):
                value = error.get(name)
                if isinstance(value, (str, Mapping, list)):
                    collect(value)
        elif isinstance(error, list):
            for item in error:
                collect(item)

    collect(response.get("error"))
    raw = response.get("raw_response")
    if isinstance(raw, Mapping):
        collect(raw.get("error"))
        if str(raw.get("object", "")).casefold() == "error" or raw.get("type") == "error":
            collect(raw)
    if str(response.get("object", "")).casefold() == "error" or response.get("type") == "error":
        collect(response)
    return codes, " ".join(messages)


def _budget_message(message: str) -> bool:
    exhausted = r"(?:exceed\w*|exhaust\w*|deplet\w*|reached|crossed|too low|zero|empty|insufficient|not enough)"
    patterns = (
        rf"\bcredits?\b.{{0,100}}{exhausted}",
        r"\b(?:insufficient|not enough|out of|no remaining|no available)\s+(?:api\s+)?credits?\b",
        rf"\b(?:spend|spending)\s+(?:limit|cap)\b.{{0,100}}{exhausted}",
        rf"{exhausted}.{{0,100}}\b(?:spend|spending)\s+(?:limit|cap)\b",
        rf"\bmonthly\b.{{0,60}}\b(?:quota|usage|budget|cap|spend|threshold)\b.{{0,100}}{exhausted}",
        rf"{exhausted}.{{0,100}}\bmonthly\b.{{0,60}}\b(?:quota|usage|budget|cap|spend|threshold)\b",
        rf"\bcurrent quota\b.{{0,80}}{exhausted}",
        rf"{exhausted}.{{0,80}}\bcurrent quota\b",
        r"\bapi budget\b.{0,60}(?:exhaust\w*|deplet\w*|exceed\w*|reached)",
        r"\b(?:payment|invoice)\b.{0,60}(?:required|failed|failure|overdue|declined|past due)",
        r"\bbilling\b.{0,60}(?:error|inactive|not active|disabled|failed|failure|payment|credit|limit|budget)",
        r"\breached\b.{0,60}\bspecified\s+(?:workspace\s+)?api usage limits\b",
        r"\b(?:configured|specified)\s+(?:workspace\s+)?api usage limit\w*\b.{0,60}(?:reached|exceed\w*)",
    )
    return any(re.search(pattern, message) is not None for pattern in patterns)


def _retry_after(response: Mapping[str, Any], now: datetime | None) -> int | None:
    headers = response.get("response_headers")
    if not isinstance(headers, Mapping):
        return None
    value = next((value for name, value in headers.items()
                  if isinstance(name, str) and name.casefold() == "retry-after"), None)
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    text = str(value).strip()
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", text):
        seconds = float(text)
        return math.ceil(seconds) if math.isfinite(seconds) else None
    try:
        target = parsedate_to_datetime(text)
    except (ValueError, TypeError, OverflowError):
        return None
    if target is None:
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    reference = now if now is not None else datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return max(0, math.ceil((target - reference).total_seconds()))


def classify_provider_stop(
    provider: str, response: Mapping[str, Any], *, now: datetime | None = None,
) -> dict[str, Any] | None:
    """Return a compact, JSON-safe stop reason, or None for ordinary outcomes.

    Budget/credit exhaustion and temporary throttling are separate reasons.
    Authentication/permission and unsent local requests are resumable after a
    credential/access repair. Invalid requests cannot resume unchanged. Output
    truncation, invalid model JSON and legal uncertainty are not account stops.
    An HTTP-date Retry-After can be evaluated against an explicit ``now`` for
    deterministic use; the current UTC time is used when ``now`` is omitted.
    """
    if not isinstance(provider, str) or provider not in _PROVIDERS:
        raise ValueError("provider must be openai, anthropic or mistral.")
    if not isinstance(response, Mapping):
        raise ValueError("response must be the provider adapter's response mapping.")
    if now is not None and not isinstance(now, datetime):
        raise ValueError("now must be a datetime or None.")
    status = response.get("http_status")
    if isinstance(status, bool) or not isinstance(status, int):
        status = None
    codes, message = _error_signals(response)
    reason, can_resume = None, True
    if codes & _MISSING_CODES:
        reason = "missing_credentials"
    elif status in {401, 403}:
        reason = "authentication_error"
    elif (status == 402 or codes & _BUDGET_CODES
          or provider == "mistral" and codes & _MISTRAL_BUDGET_CODES or _budget_message(message)):
        reason = "account_budget_exhausted"
    elif status == 429 or codes & _RATE_CODES or provider == "mistral" and "1300" in codes:
        reason = "rate_limit"
    elif codes & _AUTH_CODES:
        reason = "authentication_error"
    elif status in {400, 404, 413, 422} or codes & _INVALID_CODES:
        reason, can_resume = "invalid_request", False
    elif response.get("attempted") is False:
        reason = "request_not_sent"
    if reason is None:
        return None
    result: dict[str, Any] = {"reason": reason, "can_resume": can_resume}
    retry_after = _retry_after(response, now)
    if retry_after is not None:
        result["retry_after_seconds"] = retry_after
    return result
