#!/usr/bin/env python3
"""Prepare and freeze the original English text of the 2012 rules.

Preparation uses only the supplied or downloaded document, without an LLM,
reconstruction or automatic updates to consolidated editions.
The checks identify obvious file or edition errors and do not replace
the researchers' verification of the source.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from datetime import datetime, timezone
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parent
SOURCE_ID = "CELEX:32012Q0929(01)"
TITLE = "Rules of Procedure of the Court of Justice"
SOURCE_URL = "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32012Q0929(01)"
PDF_URLS = (
    "https://eur-lex.europa.eu/legal-content/EN/TXT/PDF/?uri=CELEX:32012Q0929(01)",
    "https://eur-lex.europa.eu/legal-content/EN/TXT/PDF/?uri=uriserv%3AOJ.L_.2012.265.01.0001.01.ENG",
    "https://eur-lex.europa.eu/eli/proc_rules/2012/929/oj/eng/pdf",
)
TEXT_NAME = "rules_of_procedure_2012_en.txt"
MANIFEST_NAME = "source_manifest.json"
MAX_INPUT_BYTES = 20 * 1024 * 1024


class SourceError(ValueError):
    """Report a readable source error without producing incomplete data."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_local(path: Path) -> bytes:
    if not path.is_file():
        raise SourceError(f"File not found: {path}")
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise SourceError("The source exceeds the 20 MiB limit for this document.")
    data = path.read_bytes()
    if not data:
        raise SourceError("The source file is empty.")
    return data


def download_pdf(timeout: float = 30.0) -> tuple[bytes, str]:
    """Try only official public endpoints, without bypassing access controls."""
    errors: list[str] = []
    for url in PDF_URLS:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "curia-gpai-research/1.0", "Accept": "application/pdf"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                code = response.status
                data = response.read(MAX_INPUT_BYTES + 1)
            if code != 200:
                errors.append(f"HTTP {code}")
                continue
            if len(data) > MAX_INPUT_BYTES:
                raise SourceError("The download exceeds 20 MiB: check that this is the correct document.")
            if not data.startswith(b"%PDF-"):
                errors.append("response is not a PDF (possible anti-bot check)")
                continue
            return data, url
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
    raise SourceError(
        "The PDF could not be downloaded from the official endpoints. "
        "No legal source text has been saved.\n"
        f"Open {SOURCE_URL}, download the original English PDF from 2012, then run:\n"
        '  python prepare_source.py --input "/path/to/document.pdf"\n'
        "Attempt outcomes: " + "; ".join(errors)
    )


def extract_pdf(data: bytes) -> tuple[str, int, str]:
    try:
        import pypdf
    except ImportError as exc:
        raise SourceError(
            "pypdf is missing. Install the dependencies first: python -m pip install -r requirements.txt"
        ) from exc
    try:
        reader = pypdf.PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise SourceError("The PDF is encrypted; use the original public PDF from EUR-Lex.")
        pages = len(reader.pages)
        if pages != 42:
            raise SourceError(
                f"The PDF contains {pages} pages; the expected original publication contains 42 "
                "(OJ L 265, pp. 1–42). Download the individual act, not the entire Official Journal issue."
            )
        sections: list[str] = []
        for number, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            if len(text.strip()) < 30:
                raise SourceError(
                    f"Page {number} does not contain enough extractable text. "
                    "Use the original text-based PDF, not a scanned copy."
                )
            sections.append(f"[PDF PAGE {number} OF {pages}]\n{text.strip()}")
        return "\n\n".join(sections) + "\n", pages, pypdf.__version__
    except SourceError:
        raise
    except Exception as exc:
        raise SourceError(f"The PDF could not be read: {type(exc).__name__}: {exc}") from exc


def extract_text(data: bytes) -> str:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SourceError("The TXT file must use UTF-8 encoding.") from exc
    head = text.lstrip()[:1000].lower()
    if "<!doctype html" in head or "<html" in head:
        raise SourceError("An HTML page was supplied instead of UTF-8 legal source text.")
    if "\x00" in text:
        raise SourceError("The TXT file contains NUL characters: check its format and encoding.")
    return text.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"


def validate_identity(text: str) -> dict[str, bool | int]:
    """Check plausibility and completeness without certifying interpretation."""
    flat = re.sub(r"\s+", " ", text)
    article_ids = {
        int(value) for value in re.findall(r"\bArticle\s+(\d{1,3})\b", text, flags=re.I)
    }
    checks: dict[str, bool | int] = {
        "english_title_present": TITLE.casefold() in flat.casefold(),
        "original_oj_date_present": bool(re.search(r"29\s*\.\s*9\s*\.\s*2012", text)),
        "original_adoption_date_present": bool(
            re.search(r"Done\s+at\s+Luxembourg,?\s+25\s+September\s+2012", flat, flags=re.I)
        ),
        "article_1_present": 1 in article_ids,
        "article_210_present": 210 in article_ids,
        "article_210_heading_present": bool(
            re.search(r"Publication\s+and\s+entry\s+into\s+force\s+of\s+these\s+Rules", flat, flags=re.I)
        ),
        "at_least_200_article_numbers_present": len(article_ids.intersection(range(1, 211))) >= 200,
        "at_least_50000_characters": len(text) >= 50_000,
        "not_consolidated_celex_identifier": "02012Q0929" not in text,
        "characters": len(text),
        "distinct_article_numbers_1_to_210": len(article_ids.intersection(range(1, 211))),
    }
    failed = [name for name, value in checks.items() if value is False]
    if failed:
        raise SourceError(
            "The document fails the checks for the original English source from 2012: "
            + ", ".join(failed)
            + ". Check the language, edition and completeness; no source has been saved."
        )
    return checks


def prepare_source(
    data: bytes,
    *,
    filename: str,
    output_dir: Path,
    force: bool = False,
    downloaded_url: str | None = None,
) -> dict:
    is_pdf = data.startswith(b"%PDF-")
    if not is_pdf and Path(filename).suffix.lower() != ".txt":
        raise SourceError("Supply a valid PDF or a UTF-8 .txt file.")
    if is_pdf:
        text, pages, pypdf_version = extract_pdf(data)
        input_name = "rules_of_procedure_2012_en.pdf"
        method = "pypdf.PdfReader.extract_text; one explicit page marker per original page"
    else:
        text, pages, pypdf_version = extract_text(data), None, None
        input_name = "rules_of_procedure_2012_en_original.txt"
        method = "UTF-8 text import; BOM removed, line endings normalised, final newline"
    checks = validate_identity(text)
    text_bytes = text.encode("utf-8")
    timestamp = utc_now()
    manifest = {
        "schema_version": 1,
        "source_id": SOURCE_ID,
        "title": TITLE,
        "edition": "Original publication, OJ L 265, 29 September 2012, pp. 1–42",
        "language": "EN",
        "source_url": SOURCE_URL,
        "downloaded_url": downloaded_url,
        "retrieved_at": timestamp if downloaded_url else None,
        "imported_at": None if downloaded_url else timestamp,
        "prepared_at": timestamp,
        "provenance": (
            "Downloaded from a public official EUR-Lex PDF endpoint."
            if downloaded_url else
            "Local user-supplied file; source_url identifies the expected document, not a verified download origin."
        ),
        "input_filename_supplied": filename,
        "input_file": input_name,
        "input_sha256": sha256(data),
        "input_size_bytes": len(data),
        "text_file": TEXT_NAME,
        "text_sha256": sha256(text_bytes),
        "text_size_bytes": len(text_bytes),
        "pages": pages,
        "extraction_method": method,
        "pypdf_version": pypdf_version,
        "validation_checks": checks,
        "validation_note": (
            "Heuristic identity/completeness checks only. Researchers must verify the original "
            "document and extraction before freezing the experimental plan. These checks are not legal validation."
        ),
    }
    payloads = {
        input_name: data,
        TEXT_NAME: text_bytes,
        MANIFEST_NAME: (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    existing = [name for name in payloads if (output_dir / name).exists()]
    if existing and not force:
        raise SourceError(
            "The source already exists: " + ", ".join(existing)
            + ". It has not been overwritten. Use --force only when deliberately preparing a new source."
        )
    # Prepare all bytes before replacing each file atomically.
    with tempfile.TemporaryDirectory(prefix=".prepare-source-", dir=output_dir) as stage_dir:
        stage = Path(stage_dir)
        for name, content in payloads.items():
            destination = stage / name
            with destination.open("wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        for name in payloads:
            if not force and (output_dir / name).exists():
                raise SourceError(f"The file {name} appeared during preparation; the operation has been stopped.")
        for name in payloads:
            os.replace(stage / name, output_dir / name)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--download", action="store_true", help="Download the original English PDF from EUR-Lex.")
    source.add_argument("--input", type=Path, help="Import an original PDF or the complete UTF-8 text.")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data", help="Directory for the prepared source.")
    parser.add_argument("--force", action="store_true", help="Deliberately replace a previously prepared source.")
    args = parser.parse_args(argv)
    try:
        if args.download:
            data, url = download_pdf()
            filename = "EUR-Lex_CELEX_32012Q0929_01_EN.pdf"
        else:
            data = read_local(args.input.expanduser())
            url = None
            filename = args.input.name
        manifest = prepare_source(
            data,
            filename=filename,
            output_dir=args.output_dir.expanduser().resolve(),
            force=args.force,
            downloaded_url=url,
        )
    except SourceError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"FILE ACCESS ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"Source prepared: {args.output_dir / TEXT_NAME}")
    print(f"Text SHA-256: {manifest['text_sha256']}")
    print("Check the extracted text before freezing the experimental plan.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
