"""Index downloaded document files using the upstream CaseID filename format."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from pathlib import Path

_DOCUMENT_STEM = re.compile(
    r"(?P<prefix>[A-Za-z0-9._()-]+)_[0-9]{2,}_[0-9]{2,}(?P<unconverted>_unconverted)?"
)
_SUPPORTED_EXTENSIONS = {".txt", ".html", ".pdf"}


def case_document_prefix(case_id: str) -> str:
    """Mirror the filename prefix used by 02_dataset_text_downloader.py."""
    prefix = re.sub(r"[^A-Za-z0-9._()-]+", "_", case_id).strip("._")
    if len(prefix) > 150:
        prefix = prefix[:130] + "_" + hashlib.sha256(case_id.encode()).hexdigest()[:12]
    return prefix


def index_case_documents(path: str | Path) -> dict[str, list[str]]:
    """Find nonempty downloaded texts or originals, grouped by exact case prefix.

    Values are sorted paths relative to case_texts_dir, including nested files.
    The final sequence and total fields delimit the prefix, so ordinary cases,
    appeals and urgent proceedings remain distinct. Temporary files and metadata
    do not establish document availability.
    """
    directory = Path(path)
    if not directory.is_dir():
        raise ValueError(f"case_texts_dir must name an existing directory: {directory}.")
    indexed: dict[str, list[str]] = defaultdict(list)
    for document in sorted(directory.rglob("*"), key=lambda item: item.relative_to(directory).as_posix()):
        extension = document.suffix.lower()
        if extension not in _SUPPORTED_EXTENSIONS:
            continue
        match = _DOCUMENT_STEM.fullmatch(document.stem)
        if match is None or (match["unconverted"] is not None and extension == ".txt"):
            continue
        if not document.is_file() or document.stat().st_size == 0:
            continue
        indexed[match["prefix"]].append(document.relative_to(directory).as_posix())
    return dict(indexed)
