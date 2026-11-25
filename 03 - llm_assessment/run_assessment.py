#!/usr/bin/env python3
"""Run the CURIA direct-assessment experiment from a frozen configuration."""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from datetime import datetime
from pathlib import Path
from time import monotonic
from zoneinfo import ZoneInfo

from curia_assessment.cleanup import clear_outputs
from curia_assessment.pipeline import analyse, load_config, prepare, resolve, run
from curia_assessment.paths import display_error, display_path
from curia_assessment.reporting import format_model_summary, write_results_report


def _print_preparation(manifest: dict) -> None:
    summary = {key: manifest[key] for key in (
        "eligible_cases", "sampled_cases", "catalogue_base_rules", "catalogue_textual_variants",
        "selected_pairs", "selected_base_rules", "selected_textual_variants", "planned_assessments")}
    if "case_sampling_by_year" in manifest:
        summary["case_sampling_by_year"] = manifest["case_sampling_by_year"]
    if "case_extraction_report" in manifest:
        summary["case_extraction_report"] = manifest["case_extraction_report"]
    print(json.dumps(summary, indent=2))


def _load_credentials() -> None:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().with_name(".env"), override=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare, run and analyse auditable LLM assessments against DECLARE.")
    delete_help = "Permanently empty the project's outputs directory, including all results and request archives."
    parser.add_argument("--delete", dest="delete_outputs", action="store_true", help=delete_help)
    commands = parser.add_subparsers(dest="command")
    for name in ("prepare", "run", "resume", "analyse", "all"):
        command = commands.add_parser(name)
        command.add_argument("--config", default="config.json", help="Configuration file (default: config.json); relative paths are resolved from its directory.")
        if name not in {"analyse", "resume"}:
            command.add_argument("--delete", dest="delete_outputs", action="store_true",
                                 default=argparse.SUPPRESS, help=delete_help)
        if name in {"run", "resume", "all"}:
            if name in {"run", "resume"}:
                command.add_argument("--provider", help="Run only the named provider ID.")
            command.add_argument("--max-requests", type=int, help="Maximum new attempts in this invocation.")
            if name != "resume":
                command.add_argument("--retry-failed", action="store_true", help="Explicitly retry failed or interrupted jobs, retaining earlier attempts.")
                command.add_argument("--recover-incomplete", action="store_true", help="After the planned run, recover truncated phase-A case responses with the configured higher output limit, retaining every original attempt.")
        if name == "all":
            command.add_argument("--no-open", action="store_true", help="Save the results report without opening a browser.")
    args = parser.parse_args()
    if args.command is None and not args.delete_outputs:
        parser.error("a command is required unless --delete is used by itself")
    if args.command in {"analyse", "resume"} and args.delete_outputs:
        parser.error(f"--delete cannot be combined with {args.command}; use --delete by itself to clear outputs")
    display_timezone = ZoneInfo("Europe/Rome")
    started = datetime.now(display_timezone)
    started_monotonic = monotonic()
    print("*** PROGRAM START ***", flush=True)
    print(f"Start: {started:%Y-%m-%d %H:%M:%S %Z}", flush=True)
    try:
        if getattr(args, "recover_incomplete", False):
            recovery_config, _ = load_config(args.config)
            if recovery_config["assessment_mode"] != "case" or recovery_config["conditions"] != ["A"]:
                raise ValueError("--recover-incomplete requires assessment_mode case and conditions [A].")
            if recovery_config.get("max_output_tokens", 16000) != 16000:
                raise ValueError("--recover-incomplete is the earlier 16000-to-32000 recovery protocol; use --retry-failed to retry requests at the current configured limit.")
        if args.delete_outputs:
            if args.command is not None:
                delete_config, _ = load_config(args.config)
                if args.command in {"run", "all"} and args.max_requests is not None and args.max_requests < 1:
                    raise ValueError("max_requests must be a positive integer.")
                if (args.command == "run" and args.provider is not None
                        and args.provider not in {provider["id"] for provider in delete_config["providers"]}):
                    raise ValueError(f"Provider ID is not configured: {args.provider}.")
            removed = clear_outputs()
            print(f"Cleared outputs: {removed} entries removed.", flush=True)
            if args.command is None:
                return 0
        if args.command == "prepare":
            manifest = prepare(args.config)
            _print_preparation(manifest)
            print("Prepared offline. No API requests were made.")
        elif args.command in {"run", "resume"}:
            if args.command == "resume":
                resume_config, resume_root = load_config(args.config)
                if resume_config["assessment_mode"] != "case":
                    raise ValueError("resume requires assessment_mode case; use run --retry-failed for rule-mode experiments.")
                resume_audit = resolve(resume_root, resume_config["output_dir"]) / "_audit"
                if not (resume_audit / "run_manifest.json").is_file():
                    raise ValueError("No prepared case experiment exists in this output directory; resume cannot start a new experiment.")
            _load_credentials()
            run_counts = run(args.config, args.provider, args.max_requests,
                             args.command == "resume" or args.retry_failed)
            print(json.dumps(run_counts, indent=2))
            if args.command == "resume":
                checkpoint = json.loads((resume_audit / "resume_status.json").read_text(encoding="utf-8"))
                selected = [row for name, row in checkpoint["providers"].items()
                            if args.provider is None or name == args.provider]
                if any(row["remaining"] for row in selected):
                    print("Some requested assessments remain incomplete. Progress is saved in _audit/resume_status.json.", flush=True)
                    return 3
            if getattr(args, "recover_incomplete", False):
                from curia_assessment.case_recovery import run_recovery
                print(json.dumps(run_recovery(args.config), indent=2))
        elif args.command == "all":
            config, root = load_config(args.config)
            print("[1/3] Preparing data and assessment plan.", flush=True)
            _print_preparation(prepare(args.config))
            print(f"[2/3] Running all {len(config['providers'])} configured models.", flush=True)
            _load_credentials()
            counts = run(args.config, None, args.max_requests, args.retry_failed)
            print(json.dumps(counts, indent=2))
            if args.recover_incomplete:
                from curia_assessment.case_recovery import run_recovery
                print(json.dumps(run_recovery(args.config), indent=2))
            print("[3/3] Analysing and displaying results.", flush=True)
            results = analyse(args.config)
            print(format_model_summary(results["model_summary"]))
            report = write_results_report(resolve(root, config["output_dir"]), results)
            print(f"Results report: {display_path(report)}", flush=True)
            if not args.no_open:
                try:
                    opened = webbrowser.open(report.resolve().as_uri())
                except (webbrowser.Error, OSError):
                    opened = False
                if not opened:
                    print("Open the saved report in a browser to view the results.", flush=True)
        else:
            results = analyse(args.config)
            print(json.dumps(results["model_summary"], indent=2))
        return 0
    except KeyboardInterrupt:
        print("Interrupted. Recorded attempts are retained; uncertain attempts are not automatically retried.", file=sys.stderr)
        return 130
    except (ValueError, OSError, KeyError, ImportError) as exc:
        print(f"Error: {display_error(exc)}", file=sys.stderr)
        return 2
    finally:
        ended = datetime.now(display_timezone)
        elapsed_minutes = int((monotonic() - started_monotonic) // 60)
        hours, minutes = divmod(elapsed_minutes, 60)
        print(f"End: {ended:%Y-%m-%d %H:%M:%S %Z}", flush=True)
        print(f"Duration: {hours} h {minutes} min", flush=True)
        print("*** PROGRAM END ***", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
