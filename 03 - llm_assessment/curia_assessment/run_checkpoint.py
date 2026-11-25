"""Persist case-level progress independently of model evaluation reports."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


def write_run_checkpoint(
    audit: Path, jobs: list[dict[str, Any]], states: dict[str, str],
    provider_stops: dict[str, dict[str, Any]], *, invocation_finished: bool = False,
) -> Path:
    """Atomically save progress after every attempt, including budget stops.

    Caller serialises updates with the runner's counter lock. This file is an
    operational view; original request/response archives remain authoritative.
    """
    providers = {}
    rows = []
    for job in jobs:
        status = states.get(job["job_id"], "pending")
        counts = providers.setdefault(job["provider_id"], Counter())
        counts["planned"] += 1
        counts["complete" if status == "ok" else "running" if status == "running"
               else "pending" if status == "pending" else "needs_retry"] += 1
        rows.append({"job_id": job["job_id"], "case_id": job["case_id"],
                     "provider_id": job["provider_id"], "phase": {"A": "1", "B": "2"}.get(job["condition"], job["condition"]),
                     "status": status})
    summaries = {}
    for provider, counts in providers.items():
        stop = provider_stops.get(provider)
        complete = counts["complete"] == counts["planned"]
        active_state = "running" if counts["running"] else "pending" if counts["pending"] else "needs_retry"
        summaries[provider] = {
            **{key: counts[key] for key in ("planned", "complete", "running", "pending", "needs_retry")},
            "remaining": counts["planned"] - counts["complete"],
            "state": "complete" if complete else stop["reason"] if stop else "incomplete" if invocation_finished else active_state,
            "stop": stop,
        }
    payload = {
        "schema_version": 1, "updated_utc": datetime.now(timezone.utc).isoformat(),
        "invocation_finished": invocation_finished,
        "all_complete": all(row["remaining"] == 0 for row in summaries.values()),
        "planned": len(jobs), "complete": sum(row["complete"] for row in summaries.values()),
        "providers": summaries, "jobs": rows,
        "resume_method": "After restoring credit/quota or resolving the reported provider issue, run: python run_assessment.py resume --config <the same frozen configuration> [--provider <provider_id>]. Completed results are skipped. Every new retry retains its original request and is archived as another attempt.",
        "uncertain_attempts": "A transmitted request without a saved response may already have been billed. The resume command explicitly permits retrying such attempts; no background retries are performed.",
        "invalid_requests": "A provider stop with can_resume=false cannot be retried unchanged; correct the preparation in a new experiment rather than repeatedly sending the invalid frozen request.",
    }
    path = audit / "resume_status.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path
