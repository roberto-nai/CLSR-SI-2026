#!/usr/bin/env python3
"""Reserve a timestamped new study, then delegate to the existing backend.

Only prepare and all create new studies. Continue an existing study with
run_assessment.py and the frozen configuration printed by this launcher.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
from zoneinfo import ZoneInfo

from curia_assessment.pipeline import load_config, resolve


_ROME = ZoneInfo("Europe/Rome")
_BACKEND = Path(__file__).resolve().with_name("run_assessment.py")
_SUFFIXES = ((re.compile(r"_([0-9]{8}_[0-9]{6})$"), "%Y%m%d_%H%M%S"),
             (re.compile(r"_([0-9]{8})$"), "%Y%m%d"))


def _instant(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(_ROME)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime.")
    return now.astimezone(_ROME)


def timestamped_output_dir(value: str, now: datetime | None = None) -> str:
    """Replace a valid trailing date/time suffix with the Europe/Rome timestamp."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("output_dir must be a non-empty path string.")
    path = Path(value)
    name = path.name
    if name in {"", ".", ".."}:
        raise ValueError("output_dir must name a study directory.")
    for pattern, date_format in _SUFFIXES:
        match = pattern.search(name)
        if match is None:
            continue
        try:
            datetime.strptime(match[1], date_format)
        except ValueError:
            continue  # Preserve numeric suffixes that are not valid dates/times.
        name = name[:match.start()]
        break
    if not name:
        raise ValueError("output_dir needs a study name before its date suffix.")
    return str(path.with_name(name + "_" + _instant(now).strftime("%Y%m%d_%H%M%S")))


def _identity(path: Path) -> tuple[int, int]:
    record = path.lstat()
    return record.st_dev, record.st_ino


def _clean_empty_reservation(path: Path, identity: tuple[int, int] | None, *, directory: bool) -> None:
    """Remove only our own still-empty reservation; never erase generated data."""
    if identity is None:
        return
    try:
        if _identity(path) != identity:
            return
        if directory:
            path.rmdir()  # Fails safely if another process has added contents.
        elif path.lstat().st_size == 0:
            path.unlink()
    except OSError:
        pass


def create_run_config(path: str | Path, now: datetime | None = None) -> tuple[Path, Path]:
    """Freeze validated defaults beside the source config and reserve a new output.

    Relative input paths keep their original base directory. Existing output
    directories and snapshot files are never overwritten, including collisions
    within the same second. Non-empty evidence left by an error is preserved.
    """
    source = Path(path).resolve()
    normalised, root = load_config(source)
    instant = _instant(now)
    stamp = instant.strftime("%Y%m%d_%H%M%S")
    config = {**normalised, "output_dir": timestamped_output_dir(normalised["output_dir"], instant)}
    payload = json.dumps(config, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    frozen = source.with_name(f"{source.stem}_{stamp}.frozen.json")
    output = resolve(root, config["output_dir"])
    output_identity = frozen_identity = None
    try:
        try:
            output.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise FileExistsError(f"The new study directory already exists: {output}. No existing study was changed.") from exc
        output_identity = _identity(output)
        try:
            stream = frozen.open("x", encoding="utf-8")
        except FileExistsError as exc:
            raise FileExistsError(f"The frozen configuration already exists: {frozen}. No file was overwritten.") from exc
        with stream:
            frozen_identity = _identity(frozen)
            stream.write(payload)
    except BaseException:
        _clean_empty_reservation(frozen, frozen_identity, directory=False)
        _clean_empty_reservation(output, output_identity, directory=True)
        raise
    return frozen, output


def _positive_integer(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _print_existing_commands(frozen: Path) -> None:
    print("Continue this same study with its frozen configuration:", flush=True)
    for command in ("run", "resume", "analyse"):
        print(shlex.join([sys.executable, str(_BACKEND), command, "--config", str(frozen)]), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "all"):
        command = commands.add_parser(name)
        command.add_argument("--config", default="config.json", help="Source configuration (default: config.json).")
        if name == "all":
            command.add_argument("--max-requests", type=_positive_integer, help="Maximum new case requests in this invocation.")
            command.add_argument("--no-open", action="store_true", help="Save the HTML report without opening a browser.")
    args = parser.parse_args(argv)
    try:
        frozen, output = create_run_config(args.config)
        print("New output directory: " + str(output), flush=True)
        print("Frozen configuration: " + str(frozen), flush=True)
        command = [sys.executable, str(_BACKEND), args.command, "--config", str(frozen)]
        if args.command == "all":
            if args.max_requests is not None:
                command.extend(("--max-requests", str(args.max_requests)))
            if args.no_open:
                command.append("--no-open")
            _print_existing_commands(frozen)
        completed = subprocess.run(command, cwd=Path.cwd(), check=False)
        if args.command == "prepare" and completed.returncode == 0:
            _print_existing_commands(frozen)
        return completed.returncode
    except KeyboardInterrupt:
        print("Interrupted. Keep the printed frozen configuration to continue this study.", file=sys.stderr)
        return 130
    except (OSError, ValueError, KeyError) as exc:
        print("New study not started: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
