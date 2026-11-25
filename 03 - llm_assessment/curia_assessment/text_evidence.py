"""Freeze local case documents as deterministic full text or bounded passages."""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import unicodedata
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .case_documents import case_document_prefix, index_case_documents

_PASSAGE_CHARACTERS = 12000
_FORMAT_ORDER = {".txt": 0, ".html": 1, ".pdf": 2}
_BLOCK_TAGS = {"article", "blockquote", "br", "dd", "div", "dl", "dt", "h1", "h2", "h3", "h4",
               "h5", "h6", "hr", "li", "main", "ol", "p", "pre", "section", "table", "td", "th", "tr", "ul"}
_HIDDEN_TAGS = {"head", "noscript", "script", "style"}


class _DocumentHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _HIDDEN_TAGS:
            self.hidden += 1
        elif not self.hidden and tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _HIDDEN_TAGS:
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)

    def text(self) -> str:
        text = re.sub(r"[^\S\n]+", " ", "".join(self.parts))
        return re.sub(r"\n[ \n]*\n", "\n\n", text).strip()


def _extract_document(content: bytes, extension: str) -> str:
    if extension == ".txt":
        return content.decode("utf-8-sig")
    if extension == ".html":
        parser = _DocumentHTMLParser()
        parser.feed(content.decode("utf-8-sig"))
        parser.close()
        return parser.text()
    executable = shutil.which("pdftotext")
    if executable is None:
        raise ValueError("PDF text extraction requires the pdftotext executable.")
    # Extract the same bytes that were hashed, even if the source file changes.
    result = subprocess.run(
        [executable, "-enc", "UTF-8", "-layout", "-nopgbrk", "-", "-"],
        input=content, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=False,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError("PDF text extraction failed" + (": " + detail if detail else "."))
    return result.stdout.decode("utf-8-sig")


def _character_limit(value: int | None, name: str) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
        raise ValueError(f"{name} must be a positive integer or null for no limit.")


def _identifier_only(text: str) -> bool:
    """Recognise identifier stubs without rejecting short substantive passages.

    This deliberately narrow test requires a CaseID, ECLI or CELEX identifier;
    other words, including document titles, keep the extracted text eligible.
    """
    normalised = unicodedata.normalize("NFKC", text)
    patterns = (
        r"\b(?:CASE\s+)?[CTF]\s*[-‐‑‒–—]\s*\d+\s*/\s*\d{2,4}(?:\s+(?:PPU|P|RX(?:-II)?))?\b",
        r"\b(?:ECLI\s*:\s*)?[A-Z]{2}:[A-Z][A-Z0-9]*:\d{4}:[A-Z0-9.:-]+\b",
        r"\b(?:CELEX\s*:\s*)?\d{5}[A-Z]{1,2}\d{4}(?:\(\d+\))?\b",
    )
    remainder, matches = normalised, 0
    for pattern in patterns:
        remainder, count = re.subn(pattern, " ", remainder, flags=re.IGNORECASE)
        matches += count
    if not matches:
        return False
    remainder = re.sub(r"\bPAGE\s*\d+(?:\s+OF\s+\d+)?\b", " ", remainder, flags=re.IGNORECASE)
    return not re.sub(r"[\W\d_]+", "", remainder, flags=re.UNICODE)


def _ocr_text(text: str, row: dict[str, Any], relative: str, directory: Path | None) -> str:
    """Use a separately stored OCR transcription only for empty/identifier PDFs."""
    if directory is None or Path(relative).suffix.lower() != ".pdf" or text and not _identifier_only(text):
        return text
    relative_sidecar = Path(relative).with_suffix(".txt")
    sidecar = (directory / relative_sidecar).resolve()
    if not sidecar.is_relative_to(directory):
        raise ValueError("An OCR sidecar must remain within ocr_sidecars_dir.")
    row.update(
        ocr_source_file=relative_sidecar.as_posix(), ocr_source_path=str(sidecar),
        ocr_source_sha256=None, original_extracted_characters=len(text),
        original_extraction_status="identifier_only" if text else "empty",
        ocr_status="missing", ocr_error=None,
    )
    try:
        content = sidecar.read_bytes()
        row["ocr_source_sha256"] = hashlib.sha256(content).hexdigest()
        extracted = content.decode("utf-8-sig").strip()
    except (OSError, UnicodeError) as exc:
        row.update(ocr_status="unreadable" if sidecar.exists() else "missing", ocr_error=str(exc))
        return text
    if not extracted or _identifier_only(extracted):
        row.update(ocr_status="identifier_only" if extracted else "empty",
                   ocr_error="The OCR sidecar contains no substantive document text.")
        return text
    row.update(ocr_status="used", extraction_method="ocr_sidecar")
    return extracted


def _document_groups(files: list[str]) -> list[list[str]]:
    grouped: dict[str, list[str]] = {}
    for relative in files:
        path = Path(relative)
        stem = path.stem.removesuffix("_unconverted")
        key = (path.parent / stem).as_posix()
        grouped.setdefault(key, []).append(relative)
    return [sorted(grouped[key], key=lambda name: (
        _FORMAT_ORDER[Path(name).suffix.lower()], "_unconverted" in Path(name).stem, name,
    )) for key in sorted(grouped)]


def _character_allocations(lengths: list[int], maximum: int | None) -> list[int]:
    """Share a case budget fairly, redistributing unused shares from short texts."""
    if maximum is None or sum(lengths) <= maximum:
        return list(lengths)
    if maximum < len(lengths):
        raise ValueError("max_case_characters must allow at least one character per readable document.")
    allocations = [0] * len(lengths)
    pending = list(range(len(lengths)))
    remaining = maximum
    while pending:
        share = remaining // len(pending)
        complete = [index for index in pending if lengths[index] <= share]
        if complete:
            for index in complete:
                allocations[index] = lengths[index]
                remaining -= lengths[index]
            pending = [index for index in pending if index not in complete]
        else:
            share, extra = divmod(remaining, len(pending))
            for position, index in enumerate(pending):
                allocations[index] = share + (position < extra)
            break
    return allocations


def build_document_evidence(
    cases: list[dict[str, Any]], case_texts_dir: str | Path, *,
    max_document_characters: int | None, max_case_characters: int | None,
    deduplicate_exact_text: bool = False, exclude_identifier_only: bool = False,
    ocr_sidecars_dir: str | Path | None = None,
) -> tuple[dict[str, list[dict[str, str]]], list[dict[str, Any]]]:
    """Read local documents once, freezing passage text, source hashes and omissions.

    TXT is preferred to HTML and then PDF for each relative document stem; an
    unreadable or empty preferred format can fall back to its original. Every
    readable document receives a share of the case budget. Bounded documents use
    their beginning and end, with exact ranges in the extracted, stripped text.
    No model, rule outcome or relevance judgement affects passage selection.

    Null character limits include every extracted character. Optional exact
    deduplication is confined to each case and keeps every source association
    in passage references and the audit. Optional OCR transcriptions are read
    from a separate directory at the PDF's same relative stem plus .txt, only
    when its original extraction is empty or contains identifiers alone.
    """
    _character_limit(max_document_characters, "max_document_characters")
    _character_limit(max_case_characters, "max_case_characters")
    for name, value in (("deduplicate_exact_text", deduplicate_exact_text),
                        ("exclude_identifier_only", exclude_identifier_only)):
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be Boolean.")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Document evidence requires a non-empty case list.")
    case_ids = [case.get("case_id") if isinstance(case, dict) else None for case in cases]
    if any(not isinstance(identifier, str) or not identifier.strip() for identifier in case_ids):
        raise ValueError("Document evidence requires non-empty case identifiers.")
    if len(set(case_ids)) != len(case_ids):
        raise ValueError("Document evidence case identifiers must be unique.")
    directory = Path(case_texts_dir)
    ocr_directory = None
    if ocr_sidecars_dir is not None:
        if not isinstance(ocr_sidecars_dir, (str, Path)) or not str(ocr_sidecars_dir).strip():
            raise ValueError("ocr_sidecars_dir must name a separate existing directory or be null.")
        ocr_directory = Path(ocr_sidecars_dir).resolve()
        if not ocr_directory.is_dir():
            raise ValueError("ocr_sidecars_dir must name a separate existing directory.")
        source_directory = directory.resolve()
        if ocr_directory.is_relative_to(source_directory) or source_directory.is_relative_to(ocr_directory):
            raise ValueError("ocr_sidecars_dir must be separate from the case_texts_dir directory tree.")
    indexed = index_case_documents(directory)
    enhanced = deduplicate_exact_text or exclude_identifier_only or ocr_directory is not None
    evidence: dict[str, list[dict[str, str]]] = {}
    audit: list[dict[str, Any]] = []
    for case_id in sorted(case_ids):
        documents: list[tuple[str, dict[str, Any]]] = []
        case_audit = []
        by_text: dict[str, dict[str, Any]] = {}
        associations: dict[str, list[dict[str, Any]]] = {}
        for alternatives in _document_groups(indexed.get(case_document_prefix(case_id), [])):
            for relative in alternatives:
                row = {
                    "case_id": case_id, "source_file": relative, "source_sha256": None,
                    "source_format": Path(relative).suffix.lower().lstrip("."),
                    "extracted_characters": 0, "included_characters": 0, "omitted_characters": 0,
                    "included": False, "truncated": False, "passage_count": 0,
                    "status": "unreadable", "error": None, "truncation_reason": "",
                }
                if enhanced:
                    row.update(extracted_text_sha256=None, extraction_method="native",
                               duplicate_of=None, associated_source_files=[], associated_passage_ids=[])
                case_audit.append(row)
                try:
                    content = (directory / relative).read_bytes()
                    row["source_sha256"] = hashlib.sha256(content).hexdigest()
                    text = _extract_document(content, Path(relative).suffix.lower()).strip()
                except (OSError, UnicodeError, ValueError, subprocess.TimeoutExpired) as exc:
                    row["error"] = str(exc)
                    continue
                text = _ocr_text(text, row, relative, ocr_directory)
                row["extracted_characters"] = len(text)
                if enhanced:
                    row["extracted_text_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
                if not text:
                    row.update(status="empty", error="No non-whitespace document text could be extracted.")
                    continue
                if exclude_identifier_only and _identifier_only(text):
                    row.update(status="identifier_only", error="Only document identifiers and page numbers could be extracted.")
                    continue
                if deduplicate_exact_text and text in by_text:
                    canonical = by_text[text]
                    row.update(status="duplicate", duplicate_of=canonical["source_file"])
                    associations[canonical["source_file"]].append(row)
                    break
                documents.append((text, row))
                by_text[text] = row
                associations[relative] = [row]
                break
        if not documents:
            details = "; ".join(f"{row['source_file']}: {row['error']}" for row in case_audit)
            raise ValueError(f"No readable document text is available for CaseID {case_id}."
                             + (" " + details if details else " Check case_texts_dir and downloaded documents."))
        capped_lengths = [min(len(text), max_document_characters) if max_document_characters is not None
                          else len(text) for text, _ in documents]
        allocations = _character_allocations(capped_lengths, max_case_characters)
        passages = []
        for (text, row), document_cap, allocation in zip(documents, capped_lengths, allocations):
            total = len(text)
            ranges = [(0, total)] if allocation == total else [(0, (allocation + 1) // 2)]
            if allocation < total and allocation // 2:
                ranges.append((total - allocation // 2, total))
            reasons = []
            if document_cap < total:
                reasons.append("max_document_characters")
            if allocation < document_cap:
                reasons.append("max_case_characters")
            omitted = total - allocation
            coverage = (f"excerpt; {omitted} document characters omitted" if omitted
                        else "full extracted document text included")
            associated = associations[row["source_file"]]
            names = [item["source_file"] for item in associated]
            source_reference = row["source_file"]
            if row.get("extraction_method") == "ocr_sidecar":
                source_reference += f" (OCR transcription: {row['ocr_source_file']})"
            if len(associated) > 1:
                aliases = []
                for item in associated[1:]:
                    name = item["source_file"]
                    if item.get("extraction_method") == "ocr_sidecar":
                        name += f" (OCR transcription: {item['ocr_source_file']})"
                    aliases.append(name)
                source_reference += " [identical extracted text also associated with: " + "; ".join(aliases) + "]"
            passage_ids = []
            for start, end in ranges:
                chunks = []
                for offset in range(start, end, _PASSAGE_CHARACTERS):
                    stop = min(offset + _PASSAGE_CHARACTERS, end)
                    if (enhanced and max_document_characters is None and max_case_characters is None
                            and not text[offset:stop].strip() and chunks):
                        # Full-text evidence must preserve even a whitespace-only
                        # chunk. Attach it to its preceding substantive passage;
                        # this exceptional passage can exceed the usual 12k size.
                        chunks[-1] = (chunks[-1][0], stop)
                    else:
                        chunks.append((offset, stop))
                for offset, stop in chunks:
                    passage_text = text[offset:stop]
                    if not passage_text.strip():
                        if "whitespace_only_chunk" not in reasons:
                            reasons.append("whitespace_only_chunk")
                        continue
                    passage_id = f"D{len(passages) + 1:04d}"
                    passages.append({
                        "passage_id": passage_id,
                        "source_reference": (f"{source_reference} [characters {offset + 1}–{stop} "
                                             f"of {total}; {coverage}]"),
                        "text": passage_text,
                    })
                    passage_ids.append(passage_id)
                    row["included_characters"] += len(passage_text)
                    row["passage_count"] += 1
            row["omitted_characters"] = total - row["included_characters"]
            row["included"] = row["passage_count"] > 0
            row["truncated"] = row["omitted_characters"] > 0
            row["status"] = "truncated" if row["truncated"] else "included"
            row["truncation_reason"] = "; ".join(reasons)
            if enhanced:
                for item in associated:
                    item.update(associated_source_files=list(names), associated_passage_ids=list(passage_ids),
                                represented_characters=row["included_characters"])
        evidence[case_id] = passages
        audit.extend(case_audit)
    return evidence, audit
