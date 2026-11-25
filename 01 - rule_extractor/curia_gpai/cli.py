"""Command-line interface for the guided extraction protocol."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .core import PROJECT_ROOT, collect_records, create_plan, latest_result, read_json, run_plan, verify_plan
from .providers import format_error
from .review import export_reviews, summarise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="CURIA experiments: guided DECLARE extraction and human validation.")
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="Freeze the source, activity vocabulary, prompts, code and parameters without calling an API.")
    plan.add_argument("--config", type=Path, default=PROJECT_ROOT / "config.json")
    plan.add_argument("--name", default="study_01", help="Name of the new experiment folder.")
    plan.add_argument("--output-root", type=Path, default=PROJECT_ROOT / "experiments")
    plan.add_argument("--repetitions", type=int, choices=[2, 3], help="Set the repetition count before freezing the plan.")
    run = sub.add_parser("run", help="Run outstanding jobs through the APIs, consuming API credit.")
    run.add_argument("--experiment", type=Path, required=True)
    run.add_argument("--env", type=Path, default=PROJECT_ROOT / ".env")
    run.add_argument("--job", help="Run only this job from the plan.")
    run.add_argument("--retry-errors", action="store_true", help="Retry errors or interrupted calls explicitly, preserving all previous attempts.")
    status = sub.add_parser("status", help="Display outcomes and verify file integrity.")
    status.add_argument("--experiment", type=Path, required=True)
    diagnose = sub.add_parser("diagnose", help="Read archived API errors without making calls or changing an experiment.")
    diagnose.add_argument("--experiment", type=Path, required=True)
    diagnose.add_argument("--job", help="Inspect only this job, such as openai__r01.")
    export = sub.add_parser("export", help="Create independent CSV review sheets with model names visible.")
    export.add_argument("--experiment", type=Path, required=True)
    export.add_argument("--output", type=Path)
    export.add_argument("--allow-incomplete", action="store_true")
    summary = sub.add_parser("summarise", help="Read YES/NO judgements and calculate descriptive summaries.")
    summary.add_argument("--experiment", type=Path, required=True)
    summary.add_argument("--reviews", type=Path)
    summary.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            import re
            if not re.fullmatch(r"[A-Za-z0-9_-]+", args.name):
                raise ValueError("Experiment names may contain letters, digits, _ and -.")
            destination = args.output_root.resolve() / args.name
            result = create_plan(args.config, destination, args.repetitions)
            print(f"Plan created: {destination}")
            print(f"Repetitions per model: {result['repetitions']}; planned calls: {len(result['jobs'])}; "
                  f"slots per call: {len(result['slots'])}; total: {len(result['jobs'])*len(result['slots'])}.")
            print(f"Allowed activity labels: {result['activity_count']}; frozen vocabulary: frozen/activity_vocabulary.json")
            print("No API calls made. Complete prompt: frozen/user_prompt.txt")
        else:
            experiment = args.experiment.resolve()
            if args.command == "run":
                run_plan(experiment, args.env, args.retry_errors, args.job)
            elif args.command == "status":
                _, records, jobs = collect_records(experiment)
                print(json.dumps({"jobs": jobs, "requested_slots": len(records),
                                  "reviewable_proposals": sum(r["reviewable"] for r in records)}, ensure_ascii=False, indent=2))
            elif args.command == "diagnose":
                plan = verify_plan(experiment)
                if args.job and args.job not in {job["job_id"] for job in plan["jobs"]}:
                    raise ValueError("The job is not included in the plan.")
                for job in plan["jobs"]:
                    if args.job and job["job_id"] != args.job:
                        continue
                    result = latest_result(experiment, job["job_id"])
                    if result is None:
                        print(f"{job['job_id']}: not run; no archived response.")
                        continue
                    response_path = experiment / result["attempt_dir"] / "response.json"
                    response = read_json(response_path) if response_path.is_file() else result
                    print(f"{job['job_id']}: {result['status']}")
                    diagnostic = format_error(response)
                    print(f"  {diagnostic}" if diagnostic else "  No API error is recorded in this attempt.")
                    if response_path.is_file():
                        print(f"  Saved response: {response_path}")
                print("Read-only diagnosis completed. No API calls made and no files changed.")
            elif args.command == "export":
                destination = args.output or experiment / "reviews"
                result = export_reviews(experiment, destination, args.allow_incomplete)
                print(f"Review sheets created: {destination}; proposals to assess: {result['n_reviewable_proposals']}.")
                print("Give each reviewer only their own CSV file, the instructions and the common source.")
            elif args.command == "summarise":
                reviews = args.reviews or experiment / "reviews"
                destination = args.output or experiment / "summary"
                result = summarise(experiment, reviews, destination)
                print(f"Summary created: {destination}; fully assessed proposals: "
                      f"{result['complete_reviews']}/{result['reviewable_proposals']}.")
        return 0
    except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}")
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted. Previously received originals have been preserved. See README for resuming the experiment.")
        return 130
