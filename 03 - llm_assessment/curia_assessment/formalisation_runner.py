#!/usr/bin/env python3
"""Resume a prospective frozen rule audit without resampling logical errors."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
import sys
import threading
from typing import Any

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from curia_assessment import pipeline as p
from curia_assessment.metrics import normalise_usage, summarise_resource_usage
from curia_assessment.prompts import _unique_keys, _reject_constant
from curia_assessment.providers import build_request, send_request
from curia_assessment.resumption import classify_provider_stop

OUTPUT = None
MODELS = {"openai": "gpt-5.5-2026-04-23", "anthropic": "claude-sonnet-5-5", "mistral": "mistral-large-4-0"}
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_ATTEMPT = re.compile(r"attempt_([0-9]{3,})")


ALLOWED_REFS = {"REQUIREMENT", "DECLARE", "LOG_SCHEMA", "ACTIVITY_DICTIONARY", "SEMANTICS"}


from curia_assessment.formalisation_protocol import parse_audit_assessment


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must be timezone-aware.")
    return parsed.astimezone(timezone.utc)


class AuditRunner:
    def __init__(self, output: Path):
        self.output = Path(output).resolve()
        self.audit = self.output / "_audit"
        self.manifest_path = self.audit / "manifest.json"
        self.manifest = p.read_json(self.manifest_path)
        self.manifest_sha256 = p.sha256_file(self.manifest_path)
        self.verify_frozen()
        self.protocol = p.read_json(self.audit / "protocol.json")
        self.jobs = p.read_jsonl(self.audit / "prepared/jobs.jsonl")
        self.allowed_refs = p.read_json(self.audit / "prepared/allowed_refs.json")
        self.providers = {provider["id"]: provider for provider in self.protocol["providers"]}
        self._validate_plan()
        self.requests = {job["job_id"]: p.read_json(self.audit / f"prepared/requests/{job['job_id']}.json") for job in self.jobs}
        self.mutex = threading.RLock()
        self.stopped = threading.Event()
        self.provider_stops: dict[str, dict] = {}
        self.resolutions: list[dict] = []
        self.states: dict[str, str] = {}
        self.ledger: list[dict] = []
        self.fatal_error: str | None = None

    def verify_frozen(self) -> None:
        if p.sha256_file(self.manifest_path) != self.manifest_sha256:
            raise ValueError("Pilot manifest changed.")
        if self.manifest.get("production_code_sha256") != p.code_digest():
            raise ValueError("Production source differs from the pilot's frozen source hash.")
        frozen = self.manifest.get("frozen_files")
        if not isinstance(frozen, dict) or not frozen:
            raise ValueError("Pilot manifest requires frozen_files.")
        required = {"_audit/protocol.json", "_audit/prepared/jobs.jsonl", "_audit/prepared/allowed_refs.json", "_audit/source_snapshot/formalisation_runner.py", "_audit/source_snapshot/formalisation_protocol.py"}
        if not required.issubset(frozen):
            raise ValueError("Pilot script, protocol and plan must all be frozen.")
        for relative, digest in frozen.items():
            rel = Path(relative)
            path = self.output / rel
            if (rel.is_absolute() or ".." in rel.parts or not path.is_file()
                    or any(part.is_symlink() for part in [path, *path.parents] if part != self.output.parent)
                    or p.sha256_file(path) != digest):
                raise ValueError(f"Frozen pilot file changed or is unsafe: {relative}.")
        actual = {str(path.relative_to(self.output)) for path in (self.audit / "prepared").rglob("*") if path.is_file() or path.is_symlink()}
        if actual != {relative for relative in frozen if relative.startswith("_audit/prepared/")}:
            raise ValueError("Prepared pilot inventory differs from the frozen manifest.")

    def _validate_plan(self) -> None:
        if (self.protocol.get("max_output_tokens") != 32000 or self.protocol.get("timeout_seconds") != 900
                or self.protocol.get("request_workers") != 6 or self.protocol.get("requests_per_provider") != 2):
            raise ValueError("Pilot requires the frozen 32000/900/6/2 resource settings.")
        if len(self.protocol["providers"]) != 3 or len(self.providers) != 3 or {x.get("provider") for x in self.providers.values()} != set(MODELS):
            raise ValueError("Pilot requires exactly the three distinct pinned providers.")
        for provider in self.providers.values():
            if (provider.get("model") != MODELS[provider["provider"]] or provider.get("reasoning_effort", "medium") != "medium"
                    or provider["provider"] == "anthropic" and provider.get("api_version", "2026-10-21") != "2026-10-21"):
                raise ValueError("Pilot provider settings changed.")
        if len(self.jobs) != 57 or len({job.get("job_id") for job in self.jobs}) != 57:
            raise ValueError("Audit requires 57 unique jobs.")
        pairs: dict[str, tuple] = {}
        allocations = set()
        for job in self.jobs:
            if not isinstance(job.get("job_id"), str) or not _NAME.fullmatch(job["job_id"]):
                raise ValueError("Unsafe pilot job identifier.")
            provider = self.providers.get(job.get("provider_id"))
            if provider is None or any(job.get(key) != provider.get(key) for key in ("provider", "model")) or job.get("run") != 1 or isinstance(job.get("run"), bool):
                raise ValueError("Job provider/model/run does not match the protocol.")
            pair = job.get("pilot_pair_id")
            if not isinstance(pair, str) or pair != job.get("rule_id") or pair != job.get("audit_pair_id"):
                raise ValueError("Both audit allocation identifiers must equal the original rule_id.")
            identity = (job.get("rule_id"),)
            if any(not isinstance(value, str) or not value for value in identity) or pairs.setdefault(pair, identity) != identity:
                raise ValueError("Pilot pair metadata differs between providers.")
            allocation = (pair, job["provider_id"])
            if allocation in allocations:
                raise ValueError("Duplicate pilot pair/provider allocation.")
            allocations.add(allocation)
            request = p.read_json(self.audit / f"prepared/requests/{job['job_id']}.json")
            if p.digest(request) != job.get("request_sha256"):
                raise ValueError("Prepared request hash differs from the frozen job.")
            payload = request.get("payload", {})
            if job["provider"] == "openai":
                system, user = payload.get("instructions"), payload.get("input", [{}])[0].get("content", [{}])[0].get("text")
            elif job["provider"] == "anthropic":
                system, user = payload.get("system"), payload.get("messages", [{}])[0].get("content", [{}])[0].get("text")
            else:
                system, user = [message.get("content") for message in payload.get("messages", [])] if len(payload.get("messages", [])) == 2 else (None, None)
            if build_request(provider, system, user, 32000) != request:
                raise ValueError("Prepared request does not match the pinned adapter protocol.")
            if job.get("prompt_sha256") != p.digest([system, user]):
                raise ValueError("Prepared prompt differs from its frozen prompt hash.")
        if set(pairs) != {f"R{i:02d}" for i in range(1, 20)} or set(self.allowed_refs) != set(pairs):
            raise ValueError("Audit requires exactly nineteen rules and evidence-reference allocations.")
        for refs in self.allowed_refs.values():
            if (not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs) or len(refs) != len(set(refs))
                    or set(refs) != ALLOWED_REFS):
                raise ValueError("Audit references must equal the five frozen rule/dictionary/semantics identifiers.")
        labels = self.protocol.get("activity_labels")
        if (not isinstance(labels, list) or not labels or any(not isinstance(label, str) or not label.strip() for label in labels)
                or len(labels) != len(set(labels))):
            raise ValueError("The protocol requires a frozen, unique activity-label dictionary.")
        expected_requests = {f"_audit/prepared/requests/{job['job_id']}.json" for job in self.jobs}
        if expected_requests != {key for key in self.manifest["frozen_files"] if key.startswith("_audit/prepared/requests/")}:
            raise ValueError("Frozen request inventory differs from planned jobs.")

    def attempt_directories(self, job: dict) -> list[Path]:
        parent = self.audit / "requests" / job["job_id"]
        if not parent.exists():
            return []
        if parent.is_symlink() or not parent.is_dir():
            raise ValueError("Unsafe pilot attempt directory.")
        directories = sorted(parent.iterdir())
        for index, path in enumerate(directories, 1):
            if path.is_symlink() or not path.is_dir() or path.name != f"attempt_{index:03d}":
                raise ValueError("Pilot attempts must be ordinary, canonical and contiguous.")
        return directories

    def read_attempt(self, job: dict, directory: Path, *, write_missing: bool = False) -> dict:
        directory = Path(directory)
        if (directory.is_symlink() or not directory.is_dir() or not _ATTEMPT.fullmatch(directory.name)
                or directory.resolve().parent != (self.audit / "requests" / job["job_id"]).resolve()):
            raise ValueError("Attempt does not belong to the frozen pilot job.")
        if any(path.is_symlink() or not path.is_file() or path.name not in {"request.json", "started.json", "response.json", "assessment.json"}
               for path in directory.iterdir()):
            raise ValueError("Unexpected or unsafe pilot attempt evidence.")
        request_path, response_path = directory / "request.json", directory / "response.json"
        if not request_path.is_file() or p.digest(p.read_json(request_path)) != job["request_sha256"]:
            raise ValueError("Archived pilot request changed.")
        if not (directory / "started.json").is_file():
            raise ValueError("Archived pilot attempt lacks its started record.")
        started = p.read_json(directory / "started.json")
        if started.get("job_id") != job["job_id"] or started.get("request_sha256") != job["request_sha256"]:
            raise ValueError("Archived pilot started identity changed.")
        _time(started["created_utc"])
        if not response_path.is_file():
            if (directory / "assessment.json").exists():
                raise ValueError("Pilot assessment lacks its raw response.")
            return {**job, "status": "interrupted", "attempt": int(directory.name[8:]), "actual_output_token_limit": 32000,
                    "usage": None, "elapsed_seconds": None, "call_attempted": None,
                    "error": "No saved response; transmission and billing are ambiguous.", **normalise_usage(job["provider"], None)}
        response = p.read_json(response_path)
        if not isinstance(response, dict) or not isinstance(response.get("status"), str):
            raise ValueError("Archived pilot response has an invalid shape.")
        p.canonical(response)
        if (response.get("attempted") is not None and not isinstance(response["attempted"], bool)
                or response.get("usage") is not None and not isinstance(response["usage"], dict)):
            raise ValueError("Archived pilot response has invalid resource types.")
        elapsed = response.get("elapsed_seconds")
        if elapsed is not None and (isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or elapsed < 0):
            raise ValueError("Archived pilot elapsed time must be finite and non-negative.")
        if response.get("received_utc") is not None:
            _time(response["received_utc"])
        if response["status"] == "ok" and response.get("model_returned") != job["model"]:
            raise ValueError("The successful response does not name the pinned pilot model.")
        if response["status"] == "ok":
            result = parse_audit_assessment(response.get("text"), set(self.allowed_refs[job["pilot_pair_id"]]), set(self.protocol["activity_labels"]))
        else:
            result = {"status": response["status"], "in_scope_assessment": None, "ungated_assessment": None, "scope_conditions": [], "mapping_assumptions": [], "missing_information": [], "counterexamples": [],
                      "error": response.get("error"), "format_notes": []}
        result.update({**job, "attempt": int(directory.name[8:]), "raw_response_sha256": p.sha256_file(response_path),
                       "completed_utc": response.get("received_utc"), "call_attempted": response.get("attempted"),
                       **{key: response.get(key) for key in ("usage", "elapsed_seconds", "model_returned", "response_id", "request_id", "http_status", "finish_reason")},
                       "actual_output_token_limit": 32000, **normalise_usage(job["provider"], response.get("usage"))})
        assessment_path = directory / "assessment.json"
        if assessment_path.exists():
            if p.read_json(assessment_path) != result:
                raise ValueError("Archived pilot assessment differs from strict raw replay.")
        elif write_missing:
            p.write_json(assessment_path, result)
        return result

    def _stop_record(self, job: dict, directory: Path) -> dict | None:
        path = directory / "response.json"
        if not path.is_file():
            return None
        response = p.read_json(path)
        stamp = response.get("received_utc") or p.utc_now()
        info = classify_provider_stop(job["provider"], response, now=_time(stamp))
        if info is None:
            return None
        record = {**info, "job_id": job["job_id"], "attempt": int(directory.name[8:]),
                  "response_sha256": p.sha256_file(path), "observed_utc": stamp}
        if "retry_after_seconds" in info:
            record["retry_not_before_utc"] = (_time(stamp) + timedelta(seconds=info["retry_after_seconds"])).isoformat()
        return record

    def _load_progress(self, write_missing: bool) -> None:
        request_root = self.audit / "requests"
        if request_root.exists():
            if request_root.is_symlink() or not request_root.is_dir():
                raise ValueError("Unsafe pilot request root.")
            planned = {job["job_id"] for job in self.jobs}
            if any(path.is_symlink() or not path.is_dir() or path.name not in planned for path in request_root.iterdir()):
                raise ValueError("Unplanned or unsafe job in pilot request inventory.")
        path = self.audit / "resume_status.json"
        previous = p.read_json(path) if path.exists() else {}
        self.provider_stops = dict(previous.get("provider_stops", {}))
        self.resolutions = p.read_json(self.audit / "stop_resolutions.json") if (self.audit / "stop_resolutions.json").exists() else []
        acknowledged = {sha for row in self.resolutions for sha in row["response_sha256s"]}
        self.provider_stops = {provider: stop for provider, stop in self.provider_stops.items()
                               if stop.get("response_sha256") not in acknowledged}
        self.ledger, self.states = [], {}
        self.new_stop_records: dict[str, list[dict]] = {}
        for job in self.jobs:
            attempts = self.attempt_directories(job)
            for directory in attempts:
                result = self.read_attempt(job, directory, write_missing=write_missing)
                self.ledger.append(result)
                stop = self._stop_record(job, directory)
                if stop is not None and stop["response_sha256"] not in acknowledged:
                    self.new_stop_records.setdefault(job["provider_id"], []).append(stop)
                    self.provider_stops[job["provider_id"]] = stop
            self.states[job["job_id"]] = result["status"] if attempts else "pending"

    def _resolve_stop(self, provider_id: str, note: str) -> None:
        if not note or not note.strip():
            raise ValueError("Explicit stop resolution requires a non-empty resolution note.")
        stops = self.new_stop_records.get(provider_id, [])
        remembered = self.provider_stops.get(provider_id)
        if remembered is not None and not any(row["response_sha256"] == remembered.get("response_sha256") for row in stops):
            stops = [*stops, remembered]
        if not stops:
            raise ValueError("The selected provider has no stop to resolve.")
        if any(not stop.get("can_resume", False) for stop in stops):
            raise ValueError("Invalid frozen requests cannot be resolved or retried unchanged.")
        if any(stop.get("retry_not_before_utc") and _time(stop["retry_not_before_utc"]) > datetime.now(timezone.utc) for stop in stops):
            raise ValueError("The recorded Retry-After deadline has not elapsed.")
        self.resolutions.append({"provider_id": provider_id, "resolved_utc": p.utc_now(), "note": note.strip(),
                                 "response_sha256s": sorted({stop["response_sha256"] for stop in stops}), "resolved_stops": stops})
        p.write_json(self.audit / "stop_resolutions.json", self.resolutions)
        self.provider_stops.pop(provider_id, None)

    def _checkpoint(self, finished: bool) -> None:
        providers = {}
        for identifier in self.providers:
            jobs = [job for job in self.jobs if job["provider_id"] == identifier]
            counts = {state: sum(self.states.get(job["job_id"]) == state for job in jobs) for state in ("ok", "running", "pending")}
            providers[identifier] = {"planned": len(jobs), "complete": counts["ok"], "running": counts["running"],
                                     "pending": counts["pending"], "remaining": len(jobs) - counts["ok"],
                                     "stop": self.provider_stops.get(identifier)}
        complete = sum(row["complete"] for row in providers.values())
        p.write_json(self.audit / "resume_status.json", {"schema_version": 1, "updated_utc": p.utc_now(),
            "planned": len(self.jobs), "complete": complete, "running": sum(row["running"] for row in providers.values()),
            "all_complete": complete == len(self.jobs), "invocation_finished": finished, "providers": providers,
            "provider_stops": self.provider_stops, "suspended_provider_ids": getattr(self, "suspended_provider_ids", []), "fatal_error": self.fatal_error,
            "jobs": [{**job, "status": self.states.get(job["job_id"], "pending")} for job in self.jobs]})

    def inspect_provider_stops(self) -> dict:
        """Read archived provider stops under the native lock; make no calls."""
        with p.output_lock(self.audit):
            self.verify_frozen()
            self._load_progress(write_missing=False)
            return dict(self.provider_stops)

    def execute(self, command: str, provider_id: str | None = None, *, resolve_stop: bool = False,
                resolution_note: str | None = None, credentials_loader=None, exclude_provider_ids=None) -> dict:
        if command not in {"run", "resume"} or provider_id is not None and provider_id not in self.providers:
            raise ValueError("Unknown pilot command or provider.")
        if resolve_stop and (command != "resume" or provider_id is None) or resolution_note and not resolve_stop:
            raise ValueError("Stop resolution requires resume --provider ID --resolve-stop --resolution-note TEXT.")
        excluded = set(exclude_provider_ids or ())
        if not excluded <= self.providers.keys():
            raise ValueError("Unknown suspended provider.")
        self.suspended_provider_ids = sorted(excluded)
        with p.output_lock(self.audit):
            self.verify_frozen()
            self._load_progress(write_missing=command == "resume")
            if resolve_stop:
                self._resolve_stop(provider_id, resolution_note or "")
            self._checkpoint(False)
            candidates = [job for job in self.jobs if (provider_id is None or job["provider_id"] == provider_id)
                          and self.states[job["job_id"]] != "ok" and job["provider_id"] not in self.provider_stops
                          and job["provider_id"] not in excluded
                          and (command == "resume" or not self.attempt_directories(job))]
            try:
                if candidates:
                    if credentials_loader is None:
                        from run_assessment import _load_credentials
                        credentials_loader = _load_credentials
                    credentials_loader()
                    with ThreadPoolExecutor(max_workers=6) as executor:
                        futures = []
                        for identifier in self.providers:
                            selected = [job for job in candidates if job["provider_id"] == identifier]
                            for stream in range(2):
                                futures.append(executor.submit(self._stream, selected[stream::2]))
                        for future in futures:
                            future.result()
            finally:
                self._load_progress(write_missing=command == "resume")
                self._checkpoint(True)
                p.write_jsonl(self.audit / "attempt_resources.jsonl", self.ledger)
                p.write_table(self.output / "model_resources.csv", summarise_resource_usage(self.jobs, self.ledger))
            if self.fatal_error:
                raise ValueError(self.fatal_error)
        return p.read_json(self.audit / "resume_status.json")

    def _stream(self, jobs: list[dict]) -> None:
        for job in jobs:
            with self.mutex:
                if self.stopped.is_set() or job["provider_id"] in self.provider_stops:
                    return
                try:
                    self.verify_frozen()
                    previous = self.attempt_directories(job)
                    directory = self.audit / "requests" / job["job_id"] / f"attempt_{len(previous) + 1:03d}"
                    directory.mkdir(parents=True, exist_ok=False)
                    request = self.requests[job["job_id"]]
                    p.write_json(directory / "request.json", request)
                    p.write_json(directory / "started.json", {"job_id": job["job_id"], "request_sha256": job["request_sha256"], "created_utc": p.utc_now()})
                    self.states[job["job_id"]] = "running"
                    self._checkpoint(False)
                except Exception as exc:
                    self.fatal_error = f"Frozen pilot guard failed ({type(exc).__name__}): {exc}"
                    self.stopped.set()
                    return
            try:
                response = send_request(request, timeout_seconds=900)
                response["received_utc"] = p.utc_now()
                p.write_json(directory / "response.json", response)
                result = self.read_attempt(job, directory, write_missing=True)
                stop = self._stop_record(job, directory)
                with self.mutex:
                    self.states[job["job_id"]] = result["status"]
                    if stop is not None:
                        self.provider_stops[job["provider_id"]] = stop
                    self._checkpoint(False)
            except Exception as exc:
                with self.mutex:
                    self.fatal_error = f"Pilot archive/adapter failure ({type(exc).__name__}); inspect the retained attempt before resuming."
                    self.states[job["job_id"]] = "interrupted"
                    self.stopped.set()
                    self._checkpoint(False)
                return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run", "resume"])
    parser.add_argument("--provider")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--resolve-stop", action="store_true")
    parser.add_argument("--resolution-note")
    args = parser.parse_args(argv)
    try:
        runner = AuditRunner(args.output)
        result = runner.execute(args.command, args.provider, resolve_stop=args.resolve_stop, resolution_note=args.resolution_note)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Pilot stopped: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({key: result[key] for key in ("planned", "complete", "all_complete", "providers")}, indent=2))
    selected = [row for key, row in result["providers"].items() if args.provider is None or key == args.provider]
    return 0 if all(row["remaining"] == 0 for row in selected) else 3


if __name__ == "__main__":
    raise SystemExit(main())
