"""Normalise reported resource use without estimating missing measurements.

Anthropic reports uncached, cache-write and cache-read input buckets separately.
OpenAI and Mistral already include cached tokens in their input totals. Cache
and reasoning details must not be added to an existing input/output total.

Provider semantics checked against the official references:
https://developers.openai.com/api/reference/resources/responses/methods/create
https://developers.openai.com/api/docs/guides/token-counting
https://platform.claude.com/docs/en/build-with-claude/prompt-caching
https://docs.mistral.ai/inference/conversations/advanced/prompt-caching
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


TOKEN_FIELDS = (
    "input_tokens", "output_tokens", "total_tokens", "cached_input_tokens",
    "cache_creation_input_tokens", "reasoning_tokens",
)
_GROUP_FIELDS = ("provider", "model", "condition", "run")


def _token(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _detail(usage: Mapping[str, Any], section: str, name: str) -> int | None:
    details = usage.get(section)
    return _token(details.get(name)) if isinstance(details, Mapping) else None


def normalise_usage(provider: str, usage: Any) -> dict[str, int | None]:
    """Return common token counts while preserving unknown values as ``None``.

    Optional detail fields remain unknown when omitted, including explicit
    zeroes only when actually reported. Anthropic total input is available only
    when all three input buckets are reported; its uncached bucket alone would
    otherwise understate input use. Total tokens can be derived when both full
    input and output counts are known. Raw provider usage is never modified.
    """
    result: dict[str, int | None] = dict.fromkeys(TOKEN_FIELDS)
    if not isinstance(usage, Mapping):
        return result
    if provider == "openai":
        result.update(
            input_tokens=_token(usage.get("input_tokens")),
            output_tokens=_token(usage.get("output_tokens")),
            total_tokens=_token(usage.get("total_tokens")),
            cached_input_tokens=_detail(usage, "input_tokens_details", "cached_tokens"),
            cache_creation_input_tokens=_detail(usage, "input_tokens_details", "cache_write_tokens"),
            reasoning_tokens=_detail(usage, "output_tokens_details", "reasoning_tokens"),
        )
    elif provider == "anthropic":
        uncached = _token(usage.get("input_tokens"))
        created = _token(usage.get("cache_creation_input_tokens"))
        cached = _token(usage.get("cache_read_input_tokens"))
        result.update(
            input_tokens=uncached + created + cached if None not in (uncached, created, cached) else None,
            output_tokens=_token(usage.get("output_tokens")),
            cached_input_tokens=cached,
            cache_creation_input_tokens=created,
            reasoning_tokens=_detail(usage, "output_tokens_details", "thinking_tokens"),
        )
    elif provider == "mistral":
        result.update(
            input_tokens=_token(usage.get("prompt_tokens")),
            output_tokens=_token(usage.get("completion_tokens")),
            total_tokens=_token(usage.get("total_tokens")),
            cached_input_tokens=_detail(usage, "prompt_tokens_details", "cached_tokens"),
            reasoning_tokens=_detail(usage, "completion_tokens_details", "reasoning_tokens"),
        )
    else:
        return result
    if result["total_tokens"] is None and result["input_tokens"] is not None and result["output_tokens"] is not None:
        result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
    return result


def _seconds(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except OverflowError:
        return None
    return numeric if math.isfinite(numeric) and numeric >= 0 else None


def summarise_resource_usage(
    planned_jobs: list[dict[str, Any]], ledger: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Sum reported resources across all attempts, including failures and retries.

    Each total is the known sum, with a separate reported-attempt count exposing
    incomplete coverage. No observations produce ``None``, not an invented
    zero. Elapsed time is API request duration, excluding preparation and time
    spent waiting for other providers. Planned groups with no attempts remain
    present. The passed jobs, ledger and original provider usage stay unchanged.
    """
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for job in planned_jobs:
        groups.setdefault(tuple(job.get(field) for field in _GROUP_FIELDS), [])
    for row in ledger:
        groups.setdefault(tuple(row.get(field) for field in _GROUP_FIELDS), []).append(row)
    summaries = []
    for group, attempts in groups.items():
        result: dict[str, Any] = dict(zip(_GROUP_FIELDS, group, strict=True))
        result["n_attempts"] = len(attempts)
        times = [value for row in attempts if (value := _seconds(row.get("elapsed_seconds"))) is not None]
        result["n_attempts_with_elapsed_seconds"] = len(times)
        result["elapsed_seconds_total"] = round(math.fsum(times), 6) if times else None
        result["elapsed_seconds_mean"] = round(math.fsum(times) / len(times), 6) if times else None
        normalised = [normalise_usage(row.get("provider"), row.get("usage")) for row in attempts]
        for field in TOKEN_FIELDS:
            reported = [row[field] for row in normalised if row[field] is not None]
            result[field + "_total"] = sum(reported) if reported else None
            result["n_attempts_with_" + field] = len(reported)
        summaries.append(result)
    return summaries
