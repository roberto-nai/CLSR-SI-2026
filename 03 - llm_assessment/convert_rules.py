#!/usr/bin/env python3
"""Convert the approved-rule workbook into the experiment's UTF-8 CSV format."""

from __future__ import annotations

import argparse
import json
import sys

from curia_assessment.workbook_import import convert_workbook


def main() -> int:
    parser = argparse.ArgumentParser(description="Import approved source–requirement text without modifying the workbook.")
    parser.add_argument("--input", required=True, help="Source XLSX workbook.")
    parser.add_argument("--output", required=True, help="Destination CSV with ID, DECLARE, source and requirement.")
    parser.add_argument("--declare", dest="declare_path", help="Optional DECLARE file whose identifiers and expressions must match.")
    parser.add_argument("--sheet", help="Worksheet name; required only when the workbook contains multiple matching sheets.")
    args = parser.parse_args()
    try:
        manifest = convert_workbook(args.input, args.output, args.declare_path, args.sheet)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
