"""Immutable plans, independent runs and preservation of original outputs.

This module does not assess legal content or DECLARE semantics.
Output checks concern JSON, requested slots and exact activity membership.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.metadata
import io
import json
import os
import platform
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .activities import read_activity_vocabulary, validate_declare_activities
from .providers import build_request, call_model, format_error

FAMILIES = {"existence": "E", "relation": "R", "mutual": "M", "negative": "N"}
CONTENT_FIELDS = ("source_reference", "source_quote", "requirement", "declare")
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, obj: Any) -> None:
    """Write atomically, preventing partial JSON files after an interruption."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_dotenv(path: Path) -> None:
    """Read KEY=value entries without a shell, substitution or code execution."""
    if not path.exists():
        return
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"Invalid .env format on line {number}.")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        # Values from the selected env file take precedence over the process environment.
        os.environ[key] = value


def make_slots(quotas: dict[str, int]) -> list[dict[str, str]]:
    if set(quotas) != set(FAMILIES):
        raise ValueError("Quotas must specify existence, relation, mutual and negative.")
    slots = []
    for family, prefix in FAMILIES.items():
        count = quotas[family]
        if type(count) is not int or count < 0 or count > 20:
            raise ValueError(f"Invalid quota: {family}.")
        slots.extend({"slot_id": f"{prefix}{i}", "family": family} for i in range(1, count + 1))
    if not slots:
        raise ValueError("At least one rule must be requested.")
    return slots


def code_files() -> list[Path]:
    files = list((PROJECT_ROOT / "curia_gpai").glob("*.py"))
    files += [PROJECT_ROOT / "run_experiments.py", PROJECT_ROOT / "prepare_source.py"]
    files += [PROJECT_ROOT / "requirements.txt"]
    return sorted(p for p in files if p.exists())


def create_plan(config_path: Path, destination: Path, repetitions: int | None = None) -> dict:
    config_path = config_path.resolve()
    config = read_json(config_path)
    base = config_path.parent
    if repetitions is not None:
        config["repetitions"] = repetitions
    repeats = config.get("repetitions")
    if type(repeats) is not int or not 1 <= repeats <= 20:
        raise ValueError("repetitions must be an integer between 1 and 20.")
    if destination.exists():
        raise ValueError(f"The directory already exists: {destination}. Choose a new name.")
    slots = make_slots(config["quotas"])
    providers = config.get("providers", [])
    if not providers or len({p["id"] for p in providers}) != len(providers):
        raise ValueError("Providers must have unique IDs.")
    for provider in providers:
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", provider["id"]):
            raise ValueError("Provider IDs may contain letters, digits, _ and -.")
        if any(x in provider["model"].lower() for x in ("latest", "preview", "experimental")):
            raise ValueError("Use a stable model and an explicit version identifier.")
        # Keep secret values in .env, never in the configuration.
        forbidden = {"api_key", "token", "authorization", "headers", "base_url"}
        if forbidden.intersection(provider):
            raise ValueError("Do not put API keys, tokens or custom endpoints in config.json.")

    source_path = (base / config["source_file"]).resolve()
    if not source_path.is_file():
        raise ValueError("The source has not been prepared. Run prepare_source.py --download or --input PDF first.")
    source_bytes = source_path.read_bytes()
    source = source_bytes.decode("utf-8-sig")
    if len(source.strip()) < 1000 or "<html" in source[:500].lower():
        raise ValueError("The source is empty, too short or contains HTML.")
    source_manifest_path = base / config.get("source_manifest", "data/source_manifest.json")
    if not source_manifest_path.is_file():
        raise ValueError("The source manifest is missing. Prepare the source with prepare_source.py.")
    source_manifest = read_json(source_manifest_path)
    if source_manifest.get("text_sha256") != sha256(source_bytes):
        raise ValueError("The source text has changed since preparation: regenerate the manifest.")
    original_bytes = None
    original_snapshot_name = None
    if source_manifest.get("input_file"):
        original_name = source_manifest["input_file"]
        if not isinstance(original_name, str) or Path(original_name).name != original_name:
            raise ValueError("The source manifest contains an invalid original filename.")
        original_path = source_manifest_path.parent / original_name
        if not original_path.is_file():
            raise ValueError("The original copy of the prepared source is missing.")
        original_bytes = original_path.read_bytes()
        if sha256(original_bytes) != source_manifest.get("input_sha256"):
            raise ValueError("The original source has changed since preparation.")
        original_snapshot_name = "source_original" + original_path.suffix.lower()

    event_log_file = config.get("event_log_file")
    if not isinstance(event_log_file, str) or not event_log_file.strip():
        raise ValueError("Set event_log_file to the CSV containing the allowed Activity labels before creating a new plan.")
    activity_vocabulary = read_activity_vocabulary(
        (base / event_log_file).resolve(), config.get("activity_column", "Activity")
    )

    system = (base / config["system_prompt_file"]).read_text(encoding="utf-8")
    task_template = (base / config["task_prompt_file"]).read_text(encoding="utf-8")
    templates = read_json(base / config["templates_file"])
    if set(templates) != set(FAMILIES):
        raise ValueError("DECLARE definitions must contain the four expected families.")
    for placeholder in ("{{SLOTS}}", "{{TEMPLATES}}", "{{ACTIVITIES}}", "{{SOURCE_TEXT}}"):
        if task_template.count(placeholder) != 1:
            raise ValueError(f"The prompt must contain {placeholder} exactly once.")
    # Expand once so inserted source text and labels cannot act as placeholders.
    replacements = {
        "{{SLOTS}}": json.dumps(slots, ensure_ascii=False, indent=2),
        "{{TEMPLATES}}": json.dumps(templates, ensure_ascii=False, indent=2),
        "{{ACTIVITIES}}": json.dumps(activity_vocabulary["activities"], ensure_ascii=False, indent=2),
        "{{SOURCE_TEXT}}": source,
    }
    task = re.sub(r"\{\{(?:SLOTS|TEMPLATES|ACTIVITIES|SOURCE_TEXT)\}\}", lambda match: replacements[match.group()], task_template)
    maximum = config.get("max_output_tokens", 16000)
    if type(maximum) is not int or maximum < 1024:
        raise ValueError("max_output_tokens must be an integer >= 1024.")
    timeout = config.get("timeout_seconds", 300)
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError("timeout_seconds must be positive.")
    requests = {p["id"]: build_request(p, system, task, maximum) for p in providers}

    destination.mkdir(parents=True)
    frozen = destination / "frozen"
    frozen.mkdir()
    (frozen / "source.txt").write_bytes(source_bytes)
    if original_bytes is not None:
        (frozen / original_snapshot_name).write_bytes(original_bytes)
    (frozen / "system_prompt.txt").write_text(system, encoding="utf-8")
    (frozen / "user_prompt.txt").write_text(task, encoding="utf-8")
    (frozen / "task_template.txt").write_text(task_template, encoding="utf-8")
    write_json(frozen / "config.json", config)
    write_json(frozen / "source_manifest.json", source_manifest)
    write_json(frozen / "declare_templates.json", templates)
    write_json(frozen / "activity_vocabulary.json", activity_vocabulary)
    code_hashes = {}
    for source_code in code_files():
        relative = source_code.relative_to(PROJECT_ROOT)
        target = frozen / "code" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_code, target)
        code_hashes[str(relative)] = sha256(source_code.read_bytes())
    for provider_id, request in requests.items():
        write_json(frozen / "requests" / f"{provider_id}.json", request)

    # Interleave providers within each repetition. The repetition number is not
    # sent to the model: inputs remain identical across runs of the same model.
    jobs = [
        {"job_id": f"{p['id']}__r{r:02d}", "provider_id": p["id"],
         "provider": p["provider"], "model": p["model"], "repetition": r}
        for r in range(1, repeats + 1) for p in providers
    ]
    files_hashes = {
        str(p.relative_to(destination)): sha256(p.read_bytes())
        for p in sorted(frozen.rglob("*")) if p.is_file()
    }
    dependencies = {}
    for name in ("pypdf",):
        try:
            dependencies[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            dependencies[name] = None
    plan = {
        "schema_version": 2, "created_at_utc": utc_now(), "study_name": config.get("study_name"),
        "package_version": __version__, "python": sys.version, "platform": platform.platform(),
        "dependencies": dependencies, "repetitions": repeats, "slots": slots,
        "original_source_snapshot": f"frozen/{original_snapshot_name}" if original_snapshot_name else None,
        "activity_vocabulary_file": "frozen/activity_vocabulary.json",
        "activity_count": activity_vocabulary["activity_count"],
        "jobs": jobs, "timeout_seconds": timeout, "blinding_seed": config.get("blinding_seed", 20261109),
        "frozen_sha256": files_hashes, "code_sha256": code_hashes,
        "notes": ["No LLM judge", "No automatic retries or output repairs",
                  "Provider effort labels do not imply equal compute budgets",
                  "Slot IDs across runs do not identify the same legal prescription",
                  "Activity arguments must exactly match the frozen event-log vocabulary",
                  "Only activity labels, not case-level event-log rows, are included in model requests"]
    }
    write_json(destination / "plan.json", plan)
    (destination / "plan.sha256").write_text(sha256((destination / "plan.json").read_bytes()) + "\n", encoding="ascii")
    return plan


def verify_plan(experiment: Path, check_code: bool = False) -> dict:
    plan_path = experiment / "plan.json"
    expected = (experiment / "plan.sha256").read_text(encoding="ascii").strip()
    if sha256(plan_path.read_bytes()) != expected:
        raise ValueError("The plan has been modified since it was frozen.")
    plan = read_json(plan_path)
    for name, digest in plan["frozen_sha256"].items():
        path = experiment / name
        if not path.is_file() or sha256(path.read_bytes()) != digest:
            raise ValueError(f"Frozen file missing or modified: {name}")
    if check_code:
        for name, digest in plan["code_sha256"].items():
            path = PROJECT_ROOT / name
            if not path.is_file() or sha256(path.read_bytes()) != digest:
                raise ValueError(f"The code has changed ({name}). Create a new plan with a new name.")
    return plan


def frozen_activities(experiment: Path, plan: dict) -> list[str] | None:
    """Load a new plan's frozen vocabulary; preserve legacy read-only access."""
    name = plan.get("activity_vocabulary_file")
    if name is None:
        if plan.get("schema_version", 1) >= 2:
            raise ValueError("The plan is missing its frozen activity vocabulary.")
        return None
    if name != "frozen/activity_vocabulary.json":
        raise ValueError("Unexpected activity vocabulary path in the frozen plan.")
    vocabulary = read_json(experiment / name)
    activities = vocabulary.get("activities")
    if not isinstance(activities, list) or not activities or not all(isinstance(item, str) and item.strip() for item in activities):
        raise ValueError("The frozen activity vocabulary is empty or invalid.")
    if len(activities) != len(set(activities)):
        raise ValueError("The frozen activity vocabulary contains duplicate labels.")
    return activities


def parse_output(text: str, slots: list[dict], activities: list[str] | None = None) -> dict:
    """Read JSON without legal/formal corrections or an additional LLM call."""
    raw = text.strip()
    if raw.startswith("```") and raw.endswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].strip().lower() in ("```", "```json") and lines[-1].strip() == "```":
            raw = "\n".join(lines[1:-1]).strip()
    issues: list[str] = []
    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"parse_status": "invalid_json", "rows": [], "issues": ["JSON could not be parsed"], "unexpected": []}
    if not isinstance(document, dict) or not isinstance(document.get("rules"), list):
        return {"parse_status": "invalid_schema", "rows": [], "issues": ["Expected an object containing a rules array"], "unexpected": document}
    if set(document) != {"rules"}:
        issues.append("Additional keys in the top-level JSON object")
    by_id: dict[str, list] = {s["slot_id"]: [] for s in slots}
    unexpected = []
    for item in document["rules"]:
        if not isinstance(item, dict) or not isinstance(item.get("slot_id"), str) or item["slot_id"] not in by_id:
            unexpected.append(item)
            continue
        by_id[item["slot_id"]].append(item)
    if unexpected:
        issues.append(f"Items outside the requested slots: {len(unexpected)}")
    rows = []
    for slot in slots:
        candidates = by_id[slot["slot_id"]]
        row = {**slot, **{k: "" for k in CONTENT_FIELDS}, "state": "missing", "format_issues": []}
        if not candidates:
            row["format_issues"].append("Slot omitted")
        elif len(candidates) > 1:
            row["state"] = "duplicate_slot"
            row["format_issues"].append("Duplicate slot ID; all original items have been preserved")
            row["original_items"] = candidates
        else:
            item = candidates[0]
            row["original_item"] = item
            expected = {"slot_id", *CONTENT_FIELDS}
            if set(item) != expected:
                row["format_issues"].append("Missing or additional fields")
            if all(k in item and item[k] is None for k in CONTENT_FIELDS):
                row["state"] = "abstained"
            else:
                for k in CONTENT_FIELDS:
                    value = item.get(k)
                    row[k] = value if isinstance(value, str) else ""
                    if not isinstance(value, str) or not value.strip():
                        row["format_issues"].append(f"Empty or non-text field: {k}")
                # Partial proposals are still shown to the experts.
                row["state"] = "proposed" if any(row[k].strip() for k in CONTENT_FIELDS) else "empty"
        if activities is not None and row["state"] == "proposed":
            row.update(validate_declare_activities(row["declare"], activities))
        rows.append(row)
    return {"parse_status": "ok" if not issues and not any(r["format_issues"] for r in rows) else "format_issues",
            "rows": rows, "issues": issues, "unexpected": unexpected}


def attempts_for(experiment: Path, job_id: str) -> list[Path]:
    return sorted((experiment / "runs" / job_id).glob("attempt_*/"))


def latest_result(experiment: Path, job_id: str) -> dict | None:
    attempts = attempts_for(experiment, job_id)
    if not attempts:
        return None
    path = attempts[-1] / "result.json"
    if not path.exists():
        return {"status": "interrupted", "attempt_dir": str(attempts[-1].relative_to(experiment))}
    checksum_file = path.with_suffix(".sha256")
    if not checksum_file.exists() or checksum_file.read_text(encoding="ascii").strip() != sha256(path.read_bytes()):
        raise ValueError(f"Result modified or checksum missing: {path}")
    result = read_json(path)
    for filename, field in (("request.json", "request_sha256"), ("response.json", "response_sha256"), ("output.txt", "output_sha256")):
        original = attempts[-1] / filename
        if not original.is_file() or sha256(original.read_bytes()) != result.get(field):
            raise ValueError(f"Original output modified or missing: {original}")
    result["attempt_dir"] = str(attempts[-1].relative_to(experiment))
    return result


def write_metrics(experiment: Path, jobs: list[dict]) -> None:
    """Write one token and timing record for every model repetition attempt."""
    fields = (
        "job_id", "provider_id", "provider", "requested_model", "model_returned",
        "repetition", "attempt", "status", "started_at_utc", "finished_at_utc",
        "elapsed_seconds", "slot_count", "proposal_count", "complete_proposal_count",
        "partial_proposal_count", "abstention_count", "other_slot_count",
        "abstention_rate", "parse_status", "input_tokens", "output_tokens",
        "total_tokens", "usage_json",
    )
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, delimiter=";")
    writer.writeheader()
    for job in jobs:
        for attempt_path in attempts_for(experiment, job["job_id"]):
            started_path = attempt_path / "started.json"
            started = read_json(started_path) if started_path.is_file() else {}
            result_path = attempt_path / "result.json"
            result = read_json(result_path) if result_path.is_file() else {}
            parsed = result.get("parsed")
            parsed_rows = parsed.get("rows") if isinstance(parsed, dict) else None
            if isinstance(parsed_rows, list):
                slot_count = len(parsed_rows)
                proposal_count = sum(
                    isinstance(row, dict) and row.get("state") == "proposed" for row in parsed_rows
                )
                complete_proposal_count = sum(
                    isinstance(row, dict)
                    and row.get("state") == "proposed"
                    and all(isinstance(row.get(field), str) and row[field].strip()
                            for field in CONTENT_FIELDS)
                    for row in parsed_rows
                )
                partial_proposal_count = proposal_count - complete_proposal_count
                abstention_count = sum(
                    isinstance(row, dict) and row.get("state") == "abstained" for row in parsed_rows
                )
                other_slot_count = slot_count - proposal_count - abstention_count
                abstention_rate = round(abstention_count / slot_count, 6) if slot_count else ""
                parse_status = parsed.get("parse_status", "")
            else:
                (slot_count, proposal_count, complete_proposal_count,
                 partial_proposal_count, abstention_count, other_slot_count) = ("",) * 6
                abstention_rate = parse_status = ""
            usage = result.get("usage")
            usage = usage if isinstance(usage, dict) else {}

            def token_count(*keys: str) -> int | None:
                for key in keys:
                    value = usage.get(key)
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                        return value
                return None

            input_tokens = token_count("input_tokens", "prompt_tokens")
            output_tokens = token_count("output_tokens", "completion_tokens")
            total_tokens = token_count("total_tokens")
            if total_tokens is None and input_tokens is not None and output_tokens is not None:
                total_tokens = input_tokens + output_tokens
            writer.writerow({
                "job_id": job["job_id"],
                "provider_id": job["provider_id"],
                "provider": job["provider"],
                "requested_model": job["model"],
                "model_returned": result.get("model_returned") or "",
                "repetition": job["repetition"],
                "attempt": started.get("attempt", ""),
                "status": result.get("status", "interrupted"),
                "started_at_utc": started.get("started_at_utc", ""),
                "finished_at_utc": result.get("finished_at_utc", ""),
                "elapsed_seconds": result.get("elapsed_seconds", ""),
                "slot_count": slot_count,
                "proposal_count": proposal_count,
                "complete_proposal_count": complete_proposal_count,
                "partial_proposal_count": partial_proposal_count,
                "abstention_count": abstention_count,
                "other_slot_count": other_slot_count,
                "abstention_rate": abstention_rate,
                "parse_status": parse_status,
                "input_tokens": "" if input_tokens is None else input_tokens,
                "output_tokens": "" if output_tokens is None else output_tokens,
                "total_tokens": "" if total_tokens is None else total_tokens,
                "usage_json": json.dumps(usage, ensure_ascii=False, sort_keys=True),
            })
    metrics_path = experiment / "metrics.csv"
    temporary_path = metrics_path.with_name(metrics_path.name + ".tmp")
    temporary_path.write_text(stream.getvalue(), encoding="utf-8-sig")
    os.replace(temporary_path, metrics_path)


def run_plan(experiment: Path, env_path: Path, retry_errors: bool = False,
             selected_job: str | None = None) -> list[dict]:
    plan = verify_plan(experiment, check_code=True)
    activities = frozen_activities(experiment, plan)
    if selected_job and selected_job not in {j["job_id"] for j in plan["jobs"]}:
        raise ValueError("The job is not included in the plan.")
    key_envs = {
        read_json(experiment / "frozen" / "requests" / f"{job['provider_id']}.json")["api_key_env"]
        for job in plan["jobs"]
    }
    for key_env in key_envs:
        os.environ.pop(key_env, None)
    load_dotenv(env_path)
    pending = []
    for job in plan["jobs"]:
        if selected_job and job["job_id"] != selected_job:
            continue
        last = latest_result(experiment, job["job_id"])
        if last is None or (retry_errors and last["status"] in ("error", "interrupted")):
            pending.append(job)
    # Check all required keys before starting the first call.
    for job in pending:
        request = read_json(experiment / "frozen" / "requests" / f"{job['provider_id']}.json")
        if not os.environ.get(request["api_key_env"], "").strip():
            raise ValueError(f"Missing {request['api_key_env']}. Complete .env (the key is not printed).")
    if not pending:
        write_metrics(experiment, plan["jobs"])
        print("No jobs to run. Existing results have been preserved.")
        return []
    lock = experiment / ".run.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ValueError("The experiment is already running, or a lock file remains. See the README section on interruptions.") from exc
    os.write(descriptor, f"pid={os.getpid()} started={utc_now()}\n".encode())
    os.close(descriptor)
    completed = []
    try:
        # The list calculated before acquiring the lock may be out of date if
        # another process has just completed the jobs. Recalculate it under lock.
        pending = [job for job in pending
                   if (last := latest_result(experiment, job["job_id"])) is None
                   or (retry_errors and last["status"] in ("error", "interrupted"))]
        for job in pending:
            previous = attempts_for(experiment, job["job_id"])
            attempt = experiment / "runs" / job["job_id"] / f"attempt_{len(previous)+1:03d}"
            attempt.mkdir(parents=True, exist_ok=False)
            request = read_json(experiment / "frozen" / "requests" / f"{job['provider_id']}.json")
            write_json(attempt / "request.json", request)
            started_at = utc_now()
            write_json(attempt / "started.json", {**job, "started_at_utc": started_at, "attempt": len(previous)+1})
            print(f"{job['job_id']}: calling {job['model']} (attempt {len(previous)+1})...", flush=True)
            response = call_model(request, timeout=plan["timeout_seconds"])
            write_json(attempt / "response.json", response)
            text = response.get("text", "")
            (attempt / "output.txt").write_text(text, encoding="utf-8")
            parsed = parse_output(text, plan["slots"], activities) if text else {"parse_status": "no_text", "rows": [], "issues": [], "unexpected": []}
            result = {**job, "attempt": len(previous)+1, "started_at_utc": started_at,
                      "finished_at_utc": utc_now(), "status": response["status"],
                      "model_returned": response.get("model_returned"), "usage": response.get("usage"),
                      "elapsed_seconds": response.get("elapsed_seconds"), "request_id": response.get("request_id"),
                      "response_id": response.get("response_id"), "finish_reason": response.get("finish_reason"),
                      "http_status": response.get("http_status"),
                      "attempted": response.get("attempted", True), "error": response.get("error"),
                      "request_sha256": sha256((attempt / "request.json").read_bytes()),
                      "response_sha256": sha256((attempt / "response.json").read_bytes()),
                      "output_sha256": sha256((attempt / "output.txt").read_bytes()), "parsed": parsed}
            write_json(attempt / "result.json", result)
            (attempt / "result.sha256").write_text(sha256((attempt / "result.json").read_bytes()) + "\n", encoding="ascii")
            write_metrics(experiment, plan["jobs"])
            completed.append(result)
            print(f"  Outcome: {result['status']}; format: {parsed['parse_status']}", flush=True)
            diagnostic = format_error(response)
            if diagnostic:
                print(f"  Error detail: {diagnostic}", flush=True)
                print(f"  Saved response: {attempt / 'response.json'}", flush=True)
            invalid_activities = sum(row.get("activity_vocabulary_valid") is False for row in parsed["rows"])
            if invalid_activities:
                print(f"  Activity membership failures: {invalid_activities}; originals retained for review.", flush=True)
    finally:
        lock.unlink(missing_ok=True)
    return completed


def collect_records(experiment: Path) -> tuple[dict, list[dict], list[dict]]:
    plan = verify_plan(experiment)
    activities = frozen_activities(experiment, plan)
    records, job_status = [], []
    for job in plan["jobs"]:
        result = latest_result(experiment, job["job_id"])
        attempts = attempts_for(experiment, job["job_id"])
        status = result["status"] if result else "not_run"
        diagnostic = ""
        if result and result.get("status") == "error":
            response_path = experiment / result["attempt_dir"] / "response.json"
            diagnostic = format_error(read_json(response_path)) if response_path.is_file() else format_error(result)
        job_summary = {**job, "status": status, "attempts": len(attempts),
                       "parse_status": result.get("parsed", {}).get("parse_status", "") if result else "",
                       "model_returned": result.get("model_returned", "") if result else ""}
        # Preserve the exact legacy record shape so existing reviewer sheets
        # remain bound to the same dataset hash during read-only reanalysis.
        if activities is not None:
            job_summary["error_detail"] = diagnostic
        job_status.append(job_summary)
        parsed = result.get("parsed", {}) if result else {}
        by_id = {r["slot_id"]: r for r in parsed.get("rows", [])}
        for slot in plan["slots"]:
            # Truncated response text is preserved but classified separately;
            # repaired or corrected text is never treated as an original output.
            row = by_id.get(slot["slot_id"], {**slot, **{k: "" for k in CONTENT_FIELDS},
                                            "state": "unavailable", "format_issues": []})
            if activities is not None and row["state"] == "proposed":
                row = {**row, **validate_declare_activities(row["declare"], activities)}
            records.append({**job, **row, "run_status": status,
                            "record_key": f"{job['job_id']}::{slot['slot_id']}",
                            "attempts": len(attempts),
                            "parse_status": parsed.get("parse_status", ""),
                            "reviewable": status == "ok" and row["state"] == "proposed"})
    return plan, records, job_status
