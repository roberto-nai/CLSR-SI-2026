"""Download document texts for a court and Country selected from an event CSV.

Create data_text/.gitkeep and _curia_scraping_text_<court>_<country>.csv before
network requests. Update data_text/_index.csv with downloaded filenames per CaseID
at every run and while downloading. Save each distinct document URL once per CaseID, as original
HTML/PDF and UTF-8 text named CaseID_<sequence>_<total>. Use exact
InfoCuria metadata and all EUR-Lex URLs. Retain originals when conversion fails.
By default, download only missing originals and extract only missing texts;
reuse verified files across updated input logs. Use --refresh to replace them.
By default, retain whole cases whose first and last chronological activities
match endpoint_events.json; use --endopint-events none to disable this filter.

Python 3.12. Run from the project root; all filesystem paths are relative to it.
Run with --help; see README.md for examples and resume behaviour.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import re
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from bs4.element import Tag
from pypdf import PdfReader
from pypdf.errors import PyPdfError

logger = logging.getLogger(__name__)

PROJECT_DIR = Path(".")
DEFAULT_ENDPOINT_EVENTS = PROJECT_DIR / "endpoint_events.json"
DISPLAY_TIMEZONE = ZoneInfo("Europe/Rome")
API_URL = "https://infocuriaws.curia.europa.eu/elastic-connector/"
BLOB_URL = "https://infocuriaws.curia.europa.eu/blob/"
MANIFEST_FIELDS = ("CaseID", "EurLex_Link", "EurLex_Links", "Curia_Link")
LANGUAGES = {
    "BG": "BUL", "CS": "CES", "DA": "DAN", "DE": "DEU", "EL": "ELL",
    "EN": "ENG", "ES": "SPA", "ET": "EST", "FI": "FIN", "FR": "FRA",
    "GA": "GLE", "HR": "HRV", "HU": "HUN", "IT": "ITA", "LT": "LIT",
    "LV": "LAV", "MT": "MLT", "NL": "NLD", "PL": "POL", "PT": "POR",
    "RO": "RON", "SK": "SLK", "SL": "SLV", "SV": "SWE",
}


class TextDownloadError(RuntimeError):
    """A document cannot be identified or retrieved in the requested language."""


def project_path(value: str | Path) -> Path:
    """Express filesystem paths relative to the current project root."""
    path = Path(value)
    return path.relative_to(Path.cwd(), walk_up=True) if path.is_absolute() else path


def case_prefix(case_id: str) -> str:
    prefix = re.sub(r"[^A-Za-z0-9._()-]+", "_", case_id).strip("._")
    if len(prefix) > 150:
        prefix = prefix[:130] + "_" + hashlib.sha256(case_id.encode()).hexdigest()[:12]
    return prefix


def source_identity(case_id: str, country: str, url: str) -> str:
    return hashlib.sha256(json.dumps([case_id, country, url]).encode()).hexdigest()


def normalise_document_url(url: str, country: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.hostname == "eur-lex.europa.eu":
        return normalise_eurlex_url(url, country)
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path,
                       parsed.params, urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True))), ""))


@dataclass(frozen=True)
class Document:
    record: dict[str, str]
    sequence: int
    filename: str
    source_key: str

    @property
    def case_id(self) -> str:
        return self.record["CaseID"]


@dataclass(frozen=True)
class OriginalDocument:
    content: bytes
    suffix: str
    source_url: str


@dataclass(frozen=True)
class EndpointEvents:
    initial: frozenset[str]
    final: frozenset[str]


def load_endpoint_events(path: Path) -> EndpointEvents:
    """Validate explicit endpoint rules; invalid files never disable the filter."""
    path = project_path(path)
    try:
        values = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        raise ValueError(f"Invalid endpoint JSON in {path}: {exc}") from exc
    if not isinstance(values, dict):
        raise ValueError(f"Endpoint file must contain a JSON object: {path}")
    labels: dict[str, frozenset[str]] = {}
    for key in ("initial", "final"):
        items = values.get(key)
        if not isinstance(items, list) or any(not isinstance(item, str) or not item.strip() for item in items):
            raise ValueError(f"Endpoint '{key}' must be a list of non-empty activity labels: {path}")
        labels[key] = frozenset(item.strip() for item in items)
    return EndpointEvents(labels["initial"], labels["final"])


def filter_case_endpoints(records: list[tuple[int, dict[str, str]]],
                          endpoints: EndpointEvents) -> list[tuple[int, dict[str, str]]]:
    """Select whole CaseIDs by endpoints, preserving every intermediate event."""
    first: dict[str, tuple[datetime, int, str]] = {}
    last: dict[str, tuple[datetime, int, str]] = {}
    invalid_cases: set[str] = set()
    case_ids = {record["CaseID"] for _, record in records}
    for line, record in records:
        case_id = record["CaseID"]
        try:
            timestamp = datetime.fromisoformat(record["Timestamp"].strip())
            timestamp = (timestamp.replace(tzinfo=UTC) if timestamp.tzinfo is None
                         else timestamp.astimezone(UTC))
        except (ValueError, OverflowError):
            invalid_cases.add(case_id)
            continue
        event = (timestamp, line, record["Activity"].strip())
        if case_id not in first or event < first[case_id]:
            first[case_id] = event
        if case_id not in last or event > last[case_id]:
            last[case_id] = event
    retained = {case_id for case_id in first if case_id not in invalid_cases
                and first[case_id][2] in endpoints.initial and last[case_id][2] in endpoints.final}
    if invalid_cases:
        logger.warning("Endpoint filter excluded %s cases with missing/invalid timestamps: %s",
                       len(invalid_cases), ", ".join(sorted(invalid_cases)[:10]))
    retained_records = [(line, record) for line, record in records if record["CaseID"] in retained]
    logger.info("Endpoint filter: %s/%s CaseIDs retained; all %s events preserved; %s CaseIDs excluded",
                len(retained), len(case_ids), len(retained_records), len(case_ids - retained))
    return retained_records


def select_documents(dataset: Path, case_type: str, country: str,
                     endpoint_events: Path | None = None) -> list[Document]:
    """Filter court, Country and whole-case endpoints, preserving source event order."""
    dataset = project_path(dataset)
    endpoints = load_endpoint_events(endpoint_events) if endpoint_events is not None else None
    records: list[tuple[int, dict[str, str]]] = []
    with dataset.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, delimiter=";")
        required = {"CaseID", "Country"}
        if endpoints is not None:
            required.update(("Activity", "Timestamp"))
        missing = required.difference(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Missing required input columns: {', '.join(sorted(missing))}")
        for line, raw in enumerate(reader, start=2):
            if None in raw:
                raise ValueError(f"Too many CSV fields at line {line}; expected a semicolon-delimited CSV")
            record = {key: value or "" for key, value in raw.items()}
            case_id = record["CaseID"].strip()
            match = re.match(r"^([CTF])[-\s]", case_id)
            court = match.group(1) if match else record.get("ProcedureID", "").split("/")[0]
            if court not in {"C", "T", "F"}:
                court = record.get("Case_Category", "")
            if court != case_type or record["Country"].strip().upper() != country:
                continue
            if not case_id:
                raise ValueError(f"Missing CaseID at line {line}")
            record["CaseID"] = case_id
            record["Country"] = country
            records.append((line, record))
    if endpoints is not None:
        records = filter_case_endpoints(records, endpoints)
    selected: list[Document] = []
    sequences: Counter[str] = Counter()
    filenames: set[str] = set()
    for line, record in records:
        case_id = record["CaseID"]
        try:
            links = json.loads(record.get("EurLex_Links", "") or "[]")
        except ValueError as exc:
            raise ValueError(f"Invalid EurLex_Links JSON at line {line}") from exc
        if not isinstance(links, list) or any(not isinstance(link, str) for link in links):
            raise ValueError(f"EurLex_Links must be a JSON list of strings at line {line}")
        sequences[case_id] += 1
        filename = f"{case_prefix(case_id)}_{sequences[case_id]:02d}.txt"
        if filename in filenames:
            raise ValueError(f"Case identifiers produce a conflicting filename: {filename}")
        filenames.add(filename)
        source_key = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
        selected.append(Document(record, sequences[case_id], filename, source_key))
    return selected


def write_manifest(path: Path, documents: list[Document]) -> None:
    """Publish the four requested columns before any downloads begin."""
    path = project_path(path)
    temporary = path.with_suffix(".csv.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=MANIFEST_FIELDS, delimiter=";")
        writer.writeheader()
        writer.writerows({field: doc.record.get(field, "") for field in MANIFEST_FIELDS}
                         for doc in documents)
    temporary.replace(path)


def normalise_eurlex_url(url: str, country: str) -> str:
    """Change representation language while preserving the legal identifier."""
    parsed = urlparse(url.strip())
    if parsed.hostname != "eur-lex.europa.eu" or parsed.scheme not in {"http", "https"}:
        raise TextDownloadError(f"Not a EUR-Lex document URL: {url}")
    path = re.sub(r"(/legal-content/)[A-Za-z]{2}(/)", rf"\g<1>{country}\2", parsed.path)
    query = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() in {"locale", "from", "lang"}:
            value = country
        if key == "uri" and value.lower().startswith("uriserv:"):
            value = re.sub(r"\.[A-Z]{3}$", "." + LANGUAGES[country], value, flags=re.IGNORECASE)
        query.append((key, value))
    return urlunparse(("https", parsed.netloc, path, parsed.params,
                       urlencode(sorted(query), safe=":"), ""))


def html_document_body(content: bytes, country: str, *, require_document: bool = False) -> Tag:
    """Validate the source before retaining it as a document original."""
    soup = BeautifulSoup(content, "html.parser")
    title = soup.title.get_text(" ", strip=True).lower() if soup.title else ""
    if any(marker in title for marker in ("just a moment", "access denied", "not found", "error", "captcha")):
        raise TextDownloadError(f"Source returned an error or verification page: {title}")
    if soup.html:
        language = str(soup.html.get("lang", "")).split("-")[0].upper()
        if language and language != country:
            raise TextDownloadError(f"Document language is {language}; requested {country}")
    container = soup.select_one("#document1, #TexteOnly, #texte, .eli-container, #document")
    if container is None and require_document:
        raise TextDownloadError("No legal document container in the EUR-Lex page")
    if container is None:
        container = soup.body
    if container is None:
        raise TextDownloadError("No document body in the HTML response")
    if any(marker in container.get_text(" ", strip=True)[:1000].lower() for marker in (
        "verifying you are human", "enable javascript and cookies to continue",
        "your request has been blocked", "please complete the captcha",
    )):
        raise TextDownloadError("Verification page instead of document content")
    return container


def extract_html_text(content: bytes, country: str, *, require_document: bool = False) -> str:
    """Convert the validated HTML bytes to readable text."""
    container = html_document_body(content, country, require_document=require_document)
    for node in container.select("script, style, nav, header, footer, form, noscript, aside, button, svg"):
        node.decompose()
    lines = [" ".join(line.split()) for line in container.get_text("\n").splitlines()]
    text = "\n".join(line for line in lines if line).strip()
    if len(text) < 40 or any(marker in text[:1000].lower() for marker in (
        "verifying you are human", "enable javascript and cookies to continue",
        "your request has been blocked", "please complete the captcha",
    )):
        raise TextDownloadError("Empty document or verification page instead of document text")
    return text + "\n"


def convert_original(original: OriginalDocument, country: str) -> str:
    """Extract text from the same bytes retained in the original file."""
    if original.suffix == ".pdf":
        try:
            reader = PdfReader(io.BytesIO(original.content))
            text = "\n\n".join(page.extract_text() or "" for page in reader.pages).strip()
        except (PyPdfError, ValueError, OSError) as exc:
            raise TextDownloadError(f"Cannot extract PDF text at {original.source_url}: {exc}") from exc
        if len(text) < 40:
            raise TextDownloadError(f"PDF has no extractable text at {original.source_url}; OCR would be required")
        return text + "\n"
    if original.suffix == ".html":
        return extract_html_text(original.content, country,
                                 require_document=urlparse(original.source_url).hostname == "eur-lex.europa.eu")
    raise TextDownloadError(f"Unsupported original format: {original.suffix}")


class TextClient:
    """A rate-limited HTTP session with bounded retries and per-case metadata reuse."""

    def __init__(self, delay: float = 0.3, retries: int = 3, timeout: float = 60):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "InfoCuria-research-text-downloader/1.0 (Python requests)",
            "Accept": "text/html, application/pdf, application/json",
        })
        self.delay, self.retries, self.timeout = delay, retries, timeout
        self.last_request = 0.0
        self.case_cache: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}

    def fetch(self, url: str, payload: dict | None = None) -> requests.Response:
        for attempt in range(self.retries + 1):
            try:
                time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
                self.last_request = time.monotonic()
                response = self.session.request(
                    "POST" if payload is not None else "GET", url, json=payload,
                    timeout=(10, self.timeout),
                )
                if response.status_code in {400, 401, 403, 404, 410}:
                    raise TextDownloadError(f"HTTP {response.status_code} at {url}")
                response.raise_for_status()
                if response.status_code != 200 or not response.content:
                    raise requests.RequestException(f"HTTP {response.status_code} or empty response")
                return response
            except requests.RequestException as exc:
                if attempt == self.retries:
                    raise TextDownloadError(f"Request failed at {url}: {exc}") from exc
                logger.warning("Retry %s/%s at %s: %s", attempt + 1, self.retries, url, exc)
                time.sleep(min(2 ** attempt, 8))
        raise AssertionError("Unreachable HTTP retry state")

    def procedure_documents(self, procedure_id: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        affair_id = procedure_id.rsplit("/", 2)[0]
        if affair_id in self.case_cache:
            return self.case_cache[affair_id]
        response = self.fetch(API_URL + "affairId/procedures", {
            "affId": affair_id, "searchTerm": "", "tabName": "affair",
            "language": "EN", "advancedFiltersValue": [],
        })
        try:
            data = response.json()
            hits = data["searchHits"]
            if data["totalHits"] != len(hits):
                raise TextDownloadError(f"Truncated procedure list for {affair_id}")
            documents = []
            for hit in hits:
                procedure = hit["content"]
                section = hit["innerHits"]["document"]
                if section["totalHits"] != len(section["searchHits"]):
                    raise TextDownloadError(f"Truncated document list for {procedure_id}")
                # Do not retain full-text metadata returned by the API in memory.
                proc = {key: procedure.get(key) for key in (
                    "procedureId", "id", "publishedId", "introductionDate", "jurisdictionCode",
                )}
                for item in section["searchHits"]:
                    document = item["content"]
                    doc = {key: document.get(key) for key in (
                        "logicDocId", "docId", "id", "idProcedure", "docNoPart", "docLang", "docFormats",
                        "groupByLogicalId", "celex",
                    )}
                    documents.append((proc, doc))
            self.case_cache[affair_id] = documents
            return documents
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise TextDownloadError(f"Invalid document metadata for {affair_id}") from exc

    def download_original(self, url: str, country: str) -> OriginalDocument:
        """Fetch the source once, preserving its exact document bytes."""
        response = self.fetch(url)
        content_type = response.headers.get("Content-Type", "").lower()
        final_url = response.url if isinstance(response.url, str) and response.url else url
        if response.content.lstrip().startswith(b"%PDF-"):
            return OriginalDocument(response.content, ".pdf", final_url)
        if "application/pdf" not in content_type and (
            "html" in content_type or response.content.lstrip().startswith((b"<", b"\xef\xbb\xbf<"))
        ):
            html_document_body(response.content, country,
                               require_document=urlparse(final_url).hostname == "eur-lex.europa.eu")
            return OriginalDocument(response.content, ".html", final_url)
        raise TextDownloadError(f"Unsupported document content or content type at {url}: {content_type}")

    def document_text(self, url: str, country: str) -> str:
        return convert_original(self.download_original(url, country), country)

    def close(self) -> None:
        self.session.close()


def curia_document_urls(document: Document, client: TextClient, country: str) -> list[str]:
    """Resolve a logical event to the requested language, never to its case page."""
    row = document.record
    event_procedure, separator, logical = row.get("EventID", "").partition("|")
    procedure_id = row.get("ProcedureID", "") or (event_procedure if separator else "")
    if separator and event_procedure != procedure_id:
        raise TextDownloadError("EventID and ProcedureID refer to different procedures")
    if not re.match(r"^[CTF]/", procedure_id) or len(procedure_id.split("/")) < 7:
        raise TextDownloadError("Exact InfoCuria document requires ProcedureID and EventID or DocumentID")
    candidates = []
    for procedure, item in client.procedure_documents(procedure_id):
        if (procedure.get("procedureId") or procedure.get("id")) != procedure_id:
            continue
        if item.get("idProcedure") and item["idProcedure"] != procedure_id:
            raise TextDownloadError("InfoCuria document belongs to a different procedure")
        ids = {str(item.get("docId", "")), str(item.get("id", "")).rsplit("-", 1)[-1]}
        ids.update(str(variant.get("id", "")).rsplit("-", 1)[-1]
                   for variant in item.get("groupByLogicalId") or [])
        matches = (str(item.get("logicDocId")) == logical if logical
                   else bool(row.get("DocumentID") and row["DocumentID"] in ids))
        if matches:
            candidates.append((procedure, item))
    if len(candidates) != 1:
        raise TextDownloadError(f"Cannot uniquely identify InfoCuria document for {document.filename}")
    procedure, item = candidates[0]
    variants = item.get("groupByLogicalId") or []
    language_variants = [variant for variant in variants if str(variant.get("docLang", "")).upper() == country]
    formats = {str(fmt).upper() for variant in language_variants for fmt in variant.get("formats") or []}
    if not language_variants:
        if str(item.get("docLang", "")).upper() != country:
            raise TextDownloadError(f"InfoCuria document is not available in {country}")
        formats = {str(fmt).upper() for fmt in item.get("docFormats") or []}
    year = str(procedure.get("introductionDate", ""))[:4]
    logical_id = str(item.get("logicDocId", "")).removeprefix("id_")
    part = str(item.get("docNoPart", ""))
    if not re.fullmatch(r"\d{4}", year) or not logical_id.isdigit() or not part.isdigit():
        raise TextDownloadError("Incomplete InfoCuria document URL metadata")
    route = f"{procedure_id[0]}/{year}/{procedure_id.replace('/', '_')}/{logical_id}-{country}-{part}"
    urls = []
    if not formats or "HTML" in formats:
        urls.append(BLOB_URL + "download-file-html/" + route + ".html")
    if not formats or "PDF" in formats:
        urls.append(BLOB_URL + "download-file/" + route + ".pdf")
    if not urls:
        raise TextDownloadError(f"No supported HTML or PDF representation in {country}")
    return urls


def document_urls(document: Document, client: TextClient, country: str) -> list[str]:
    """List all distinct Curia and EUR-Lex document URLs for an event."""
    errors: list[str] = []
    try:
        urls = curia_document_urls(document, client, country)
    except TextDownloadError as exc:
        errors.append(str(exc))
        urls = []
    alternatives = [document.record.get("EurLex_Link", "")]
    alternatives.extend(json.loads(document.record.get("EurLex_Links", "") or "[]"))
    for link in alternatives:
        if link:
            try:
                urls.append(normalise_eurlex_url(link, country))
            except TextDownloadError as exc:
                errors.append(str(exc))
    urls = list(dict.fromkeys(urls))
    if not urls:
        raise TextDownloadError("; ".join(errors) or "No public document links for this event")
    return urls


def download_document(document: Document, client: TextClient, country: str) -> tuple[str, str]:
    """Return the first usable text; the batch runner retains every URL instead."""
    errors = []
    for url in document_urls(document, client, country):
        try:
            return client.document_text(url, country), url
        except TextDownloadError as exc:
            errors.append(str(exc))
    raise TextDownloadError("; ".join(errors) or "No public document links for this event")


def save_report(path: Path, report: dict[str, Any]) -> None:
    path = project_path(path)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


class DownloadIndex:
    """Maintain a global inventory of the original and TXT files actually present."""

    def __init__(self, output: Path):
        self.output = project_path(output)
        self.path = self.output / "_index.csv"
        self.files: dict[str, set[str]] = {}
        self._candidates: dict[str, set[str]] = {}
        for report_path in sorted(self.output.glob("download_report_text_*.json")):
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
                self._include(report.get("documents", []) + report.get("source_history", []))
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                logger.warning("Cannot include download report in file index %s: %s", report_path, exc)
        self.reconcile()

    @staticmethod
    def _filenames(item: dict[str, Any]) -> set[str]:
        names = set()
        for field in ("filename", "original_filename"):
            name = item.get(field)
            if (isinstance(name, str) and name and Path(name).name == name
                    and Path(name).suffix.lower() in {".html", ".pdf", ".txt"}):
                names.add(name)
        if isinstance(item.get("last_success"), dict):
            names.update(DownloadIndex._filenames(item["last_success"]))
        return names

    def _include(self, entries: Iterable[dict[str, Any]]) -> None:
        for item in entries:
            case_id = item.get("CaseID")
            if isinstance(case_id, str) and case_id:
                self.files.setdefault(case_id, set())
                self._candidates.setdefault(case_id, set()).update(self._filenames(item))

    def add_cases(self, case_ids: Iterable[str]) -> None:
        for case_id in case_ids:
            self.files.setdefault(case_id, set())
            self._candidates.setdefault(case_id, set())
        self._save()

    def update(self, entry: dict[str, Any]) -> None:
        self._include([entry])
        self._refresh([entry["CaseID"]])
        self._save()

    def _refresh(self, case_ids: Iterable[str]) -> None:
        for case_id in case_ids:
            self.files[case_id] = {name for name in self._candidates[case_id]
                                   if (self.output / name).is_file()}

    def reconcile(self) -> None:
        self._refresh(self.files)
        self._save()

    def _save(self) -> None:
        temporary = self.path.with_suffix(".csv.tmp")
        with temporary.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=("CaseID", "TotalFiles", "File"), delimiter=";")
            writer.writeheader()
            for case_id, filenames in sorted(self.files.items()):
                writer.writerow({"CaseID": case_id, "TotalFiles": len(filenames),
                                 "File": json.dumps(sorted(filenames), ensure_ascii=False)})
        temporary.replace(self.path)


def file_hash(path: Path) -> str:
    return hashlib.sha256(project_path(path).read_bytes()).hexdigest()


def write_bytes(path: Path, content: bytes) -> None:
    path = project_path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def migrate_source_files(item: dict[str, Any], output: Path, stem: str, *,
                         active_hashes: dict[str, str] | None = None) -> dict[str, Any]:
    """Update a changed case total by moving only report-owned, verified files."""
    result = dict(item)
    ownership = dict(active_hashes or {})
    for field, digest_field in (("original_filename", "original_sha256"), ("filename", "sha256")):
        if result.get(field) and result.get(digest_field):
            ownership.setdefault(result[field], result[digest_field])
    if isinstance(result.get("last_success"), dict):
        result["last_success"] = migrate_source_files(result["last_success"], output, stem, active_hashes=ownership)
    for field, digest_field in (("original_filename", "original_sha256"), ("filename", "sha256")):
        name = result.get(field, "")
        if not name:
            continue
        if Path(name).name != name:
            raise TextDownloadError(f"Previous report contains an invalid filename: {name}")
        suffix = Path(name).suffix
        if (field == "original_filename" and suffix not in {".html", ".pdf"}) or (
            field == "filename" and suffix != ".txt"
        ):
            raise TextDownloadError(f"Previous report contains an invalid file format: {name}")
        unconverted = "_unconverted" if field == "original_filename" and Path(name).stem.endswith("_unconverted") else ""
        target = output / f"{stem}{unconverted}{suffix}"
        old_path = output / name
        digest = result.get(digest_field)
        # A refreshed original can already have replaced the historical bytes.
        # The current entry owns that path; history still owns any previous TXT.
        if digest and old_path != target and ownership.get(name) == digest:
            if old_path.exists():
                if file_hash(old_path) != digest:
                    raise TextDownloadError(f"Existing file has no matching source/hash; use --refresh: {name}")
                if target.exists() and file_hash(target) != digest:
                    raise TextDownloadError(f"Cannot rename onto a different existing file: {target}")
                old_path.replace(target)
            elif target.exists() and file_hash(target) != digest:
                raise TextDownloadError(f"Existing file has no matching source/hash; use --refresh: {target}")
        result[field] = target.name
    if result.get("filename"):
        result["text_file"] = (output / result["filename"]).as_posix()
    if result.get("original_filename"):
        result["original_file"] = (output / result["original_filename"]).as_posix()
    return result


def process_source(document: Document, source_index: int, url: str, client: TextClient,
                   country: str, output: Path, previous: dict[str, Any], refresh: bool,
                   checkpoint: Callable[[dict[str, Any]], None], *, case_total: int = 1) -> dict[str, Any]:
    """Persist an original before conversion, and verify both files when resuming."""
    output = project_path(output)
    stem = f"{case_prefix(document.case_id)}_{source_index:02d}_{case_total:02d}"
    target = output / f"{stem}.txt"
    source_key = source_identity(document.case_id, country, url)
    entry: dict[str, Any] = {
        "CaseID": document.case_id, "event_source_key": document.source_key,
        "source_key": source_key, "requested_url": url, "source_url": url,
        "filename": target.name, "text_file": target.as_posix(), "status": "failed",
        "source_index": source_index, "case_total": case_total,
    }
    old = previous if previous.get("source_key") == source_key else {}
    success = old if old.get("status") in {"downloaded", "existing"} else old.get("last_success", {})
    if success:
        entry["last_success"] = success

    def original_path(item: dict[str, Any]) -> Path | None:
        name = item.get("original_filename", "")
        if name not in {f"{stem}.html", f"{stem}.pdf", f"{stem}_unconverted.html", f"{stem}_unconverted.pdf"}:
            return None
        return output / name

    try:
        if old:
            try:
                old = migrate_source_files(old, output, stem)
            except TextDownloadError:
                if not refresh:
                    raise
                # A requested fresh download can replace the new target while
                # leaving unverifiable files under their previous names intact.
                old = {}
            success = old if old.get("status") in {"downloaded", "existing"} else old.get("last_success", {})
            if success:
                entry["last_success"] = success
            else:
                entry.pop("last_success", None)
        local = original_path(old)
        if not refresh:
            known: dict[str, str] = {}
            for item in (success, old):
                path = original_path(item)
                if path and item.get("original_sha256"):
                    known[path.name] = item["original_sha256"]
                if item.get("sha256"):
                    known[target.name] = item["sha256"]
            for name in (target.name, f"{stem}.html", f"{stem}.pdf",
                         f"{stem}_unconverted.html", f"{stem}_unconverted.pdf"):
                path = output / name
                if path.exists() and (name not in known or file_hash(path) != known[name]):
                    raise TextDownloadError(f"Existing file has no matching source/hash; use --refresh: {name}")
            if (old.get("status") in {"downloaded", "existing"} and local and local.is_file()
                    and target.is_file() and old.get("sha256")):
                entry.update(old, status="existing", source_index=source_index, case_total=case_total)
                entry.pop("last_success", None)
                return entry
            # A failed refresh can still leave the previous pair intact.
            retained = original_path(success)
            if (old.get("status") == "failed" and retained and retained.is_file()
                    and target.is_file() and success.get("sha256")):
                entry.update(success, status="existing", source_index=source_index, case_total=case_total)
                entry.pop("last_success", None)
                return entry

        preserve_text = (not refresh and (local is None or not local.is_file())
                         and target.is_file() and success.get("sha256")
                         and file_hash(target) == success["sha256"])
        if not refresh and local and local.is_file():
            original = OriginalDocument(local.read_bytes(), local.suffix, old.get("source_url", url))
            saved_original = local
        else:
            original = client.download_original(url, country)
            canonical = output / f"{stem}{original.suffix}"
            # Retain an existing pair until a fresh source also converts successfully.
            saved_original = (output / f"{stem}_unconverted{original.suffix}"
                              if not preserve_text and (canonical.exists() or target.exists()) else canonical)
            write_bytes(saved_original, original.content)
        entry.update(status="original_only", original_filename=saved_original.name,
                     original_sha256=hashlib.sha256(original.content).hexdigest(),
                     source_url=original.source_url, original_file=saved_original.as_posix())
        if preserve_text:
            entry.update(status="downloaded", sha256=success["sha256"])
            entry.pop("last_success", None)
            checkpoint(entry)
            return entry
        checkpoint(entry)
        text = convert_original(original, country)
        temporary_text = target.with_suffix(".txt.tmp")
        temporary_text.write_text(text, encoding="utf-8")
        canonical = output / f"{stem}{original.suffix}"
        if saved_original != canonical:
            saved_original.replace(canonical)
            entry["original_filename"] = canonical.name
            entry["original_file"] = canonical.as_posix()
            checkpoint(entry)
        temporary_text.replace(target)
        entry.update(status="downloaded", sha256=file_hash(target))
        entry.pop("last_success", None)
    except (TextDownloadError, OSError) as exc:
        entry["error"] = str(exc)
        logger.error("%s: %s", target.name, exc)
    return entry


def run(args: argparse.Namespace) -> int:
    args.dataset = project_path(args.dataset)
    args.data_dir = project_path(args.data_dir)
    endpoint_events = getattr(args, "endpoint_events", DEFAULT_ENDPOINT_EVENTS)
    args.endpoint_events = project_path(endpoint_events) if endpoint_events is not None else None
    output: Path = args.data_dir
    output.mkdir(parents=True, exist_ok=True)
    (output / ".gitkeep").touch(exist_ok=True)
    download_index = DownloadIndex(output)
    suffix = f"{args.case_type}_{args.country}"
    manifest = output / f"_curia_scraping_text_{suffix}.csv"
    legacy_manifest = output / f"curia_scraping_text_{suffix}.csv"
    if legacy_manifest.exists() and not manifest.exists():
        legacy_manifest.replace(manifest)
    if not manifest.exists():
        write_manifest(manifest, [])
    documents = select_documents(args.dataset, args.case_type, args.country, args.endpoint_events)
    write_manifest(manifest, documents)
    download_index.add_cases(doc.case_id for doc in documents)
    case_count = len({doc.case_id for doc in documents})
    print(f"Distinct cases selected: {case_count}", flush=True)
    print(f"Case index: {manifest.as_posix()}", flush=True)
    print(f"Downloaded file index: {download_index.path.as_posix()}", flush=True)
    report_path = output / f"download_report_text_{suffix}.json"
    previous: dict[str, dict[str, Any]] = {}
    if report_path.exists():
        try:
            previous_report = json.loads(report_path.read_text(encoding="utf-8"))
            # Active entries override history; excluded cases retain their file provenance.
            for item in previous_report.get("source_history", []) + previous_report["documents"]:
                if not item.get("CaseID") or not item.get("requested_url"):
                    continue
                url = normalise_document_url(item["requested_url"], args.country)
                key = source_identity(item["CaseID"], args.country, url)
                candidate = dict(item, source_key=key, requested_url=url)
                if isinstance(candidate.get("last_success"), dict):
                    candidate["last_success"] = dict(candidate["last_success"], source_key=key, requested_url=url)
                previous[key] = candidate
        except (ValueError, KeyError, TypeError):
            logger.warning("Previous report cannot be used to resume: %s", report_path)
    # Retain existing provenance even if this run is interrupted before its turn.
    cases: dict[str, list[Document]] = {}
    for document in documents:
        cases.setdefault(document.case_id, []).append(document)
    records = {key: dict(item) for key, item in previous.items() if item["CaseID"] in cases}
    unresolved_events: list[dict[str, str]] = []
    report: dict[str, Any] = {
        "schema_version": 4,
        "input_file": args.dataset.as_posix(), "case_type": args.case_type, "country": args.country,
        "endpoint_events": args.endpoint_events.as_posix() if args.endpoint_events is not None else None,
        "output_dir": output.as_posix(), "index_file": manifest.as_posix(),
        "download_index_file": download_index.path.as_posix(),
        "started_at": datetime.now(UTC).isoformat(), "selected_events": len(documents),
        "selected_cases": case_count, "complete": False,
        "manifest_only": args.manifest_only, "documents": list(records.values()),
        "source_history": [item for key, item in previous.items() if key not in records],
        "unresolved_events": unresolved_events,
    }

    def checkpoint(entry: dict[str, Any]) -> None:
        records[entry["source_key"]] = dict(entry)
        report["documents"] = list(records.values())
        report["source_history"] = [item for key, item in previous.items() if key not in records]
        save_report(report_path, report)
        download_index.update(entry)

    logger.info("Saved %s events for %s cases to %s", len(documents), report["selected_cases"], manifest)
    if args.manifest_only:
        save_report(report_path, report)
        return 0
    if not documents:
        report.update(complete=True, finished_at=datetime.now(UTC).isoformat(), downloaded=0, existing=0, failed=0)
        save_report(report_path, report)
        return 0
    save_report(report_path, report)
    client = TextClient(delay=args.delay, retries=args.retries, timeout=args.timeout)
    interrupted = False
    try:
        for case_index, (case_id, events) in enumerate(cases.items(), start=1):
            event_lookup = {doc.source_key: doc for doc in events}
            sources: dict[str, Document] = {}
            references: dict[str, set[str]] = {}
            # Keep existing source ordinals and append newly discovered URLs.
            for item in previous.values():
                if item["CaseID"] != case_id:
                    continue
                associated = set(item.get("event_source_keys") or [item.get("event_source_key", "")])
                associated.intersection_update(event_lookup)
                if not associated:
                    continue
                url = item["requested_url"]
                sources.setdefault(url, event_lookup[next(iter(associated))])
                references.setdefault(url, set()).update(associated)
            for doc in events:
                try:
                    urls = document_urls(doc, client, args.country)
                    if not urls:
                        raise TextDownloadError("No document URLs for this event")
                    for candidate in urls:
                        url = normalise_document_url(candidate, args.country)
                        sources.setdefault(url, doc)
                        references.setdefault(url, set()).add(doc.source_key)
                except TextDownloadError as exc:
                    if not any(doc.source_key in values for values in references.values()):
                        unresolved_events.append({"CaseID": case_id, "event_source_key": doc.source_key, "error": str(exc)})
                    logger.error("%s: %s", doc.filename, exc)
            prior_order = [item["requested_url"] for item in previous.values()
                           if item["CaseID"] == case_id and item["requested_url"] in sources]
            sources = {url: sources[url] for url in dict.fromkeys(prior_order + list(sources))}
            active_keys = {source_identity(case_id, args.country, url) for url in sources}
            for key in list(records):
                if records[key]["CaseID"] == case_id and key not in active_keys:
                    records.pop(key)
            case_total = len(sources)
            logger.info("CaseID %s: %s distinct document links across %s events", case_id, case_total, len(events))
            for source_index, (url, doc) in enumerate(sources.items(), start=1):
                key = source_identity(case_id, args.country, url)

                def source_checkpoint(entry: dict[str, Any]) -> None:
                    entry["event_source_key"] = doc.source_key
                    entry["event_source_keys"] = sorted(references[url])
                    checkpoint(entry)

                entry = process_source(doc, source_index, url, client, args.country, output,
                                       previous.get(key, {}), args.refresh, source_checkpoint, case_total=case_total)
                source_checkpoint(entry)
                logger.info("%s: %s", entry["filename"], entry["status"])
            logger.info("Cases processed: %s/%s", case_index, len(cases))
    except KeyboardInterrupt:
        interrupted = True
        logger.warning("Interrupted; completed texts and their report are retained")
    finally:
        client.close()
        counts = Counter(item["status"] for item in records.values())
        report.update(finished_at=datetime.now(UTC).isoformat(), downloaded=counts["downloaded"],
                      existing=counts["existing"], failed=counts["failed"] + len(unresolved_events),
                      original_only=counts["original_only"],
                      selected_sources=len(records), documents=list(records.values()), interrupted=interrupted,
                      source_history=[item for key, item in previous.items() if key not in records],
                      complete=not interrupted and not counts["failed"] and not counts["original_only"] and not unresolved_events)
        save_report(report_path, report)
        download_index.reconcile()
        logger.info("Sources: %s downloaded, %s existing, %s original only, %s failed; report: %s",
                    counts["downloaded"], counts["existing"], counts["original_only"], report["failed"], report_path)
    return 130 if interrupted else (0 if report["complete"] else 1)


def endpoint_events_argument(value: str) -> Path | None:
    return None if value.strip().casefold() == "none" else project_path(value)


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", nargs="?", type=project_path, default=PROJECT_DIR / "data" / "curia_scraping_en.csv")
    parser.add_argument("--case-type", type=str.upper, choices=("C", "T", "F"), default="C")
    parser.add_argument("--country", "--language", type=str.upper, choices=tuple(LANGUAGES), default="EN",
                        help="Filter the Country column and download this language (default EN)")
    parser.add_argument("--endopint-events", "--endpoint-events", dest="endpoint_events",
                        type=endpoint_events_argument, default=DEFAULT_ENDPOINT_EVENTS, metavar="JSON_OR_NONE",
                        help="Whole-case first/last activity filter (default endpoint_events.json); none disables it")
    parser.add_argument("--data-dir", type=project_path, default=PROJECT_DIR / "data_text")
    parser.add_argument("--delay", type=float, default=0.3, help="Minimum seconds between HTTP requests")
    parser.add_argument("--retries", type=int, default=3, help="Retries after the initial HTTP attempt")
    parser.add_argument("--timeout", type=float, default=60, help="HTTP read timeout in seconds")
    parser.add_argument("--refresh", action="store_true", help="Download original documents again and regenerate texts")
    parser.add_argument("--manifest-only", action="store_true", help="Write the filtered manifest without making requests")
    args = parser.parse_args(argv)
    if args.delay < 0 or args.retries < 0 or args.timeout <= 0:
        parser.error("--delay and --retries must be non-negative; --timeout must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = arguments(argv)
    started_at = datetime.now(DISPLAY_TIMEZONE)
    started_monotonic = time.monotonic()
    print("*PROGRAM START*", flush=True)
    print(f"Start time: {started_at:%Y-%m-%d %H:%M:%S %Z}", flush=True)
    try:
        print("Parameters:", flush=True)
        for name, value in vars(args).items():
            print(f"  {name}: {value}", flush=True)
        logging.basicConfig(level=logging.INFO, format="%(asctime)s -- %(levelname)s -- %(message)s")
        return run(args)
    except (OSError, ValueError, TextDownloadError) as exc:
        logger.error("Text download stopped: %s", exc)
        return 1
    except KeyboardInterrupt:
        logger.warning("Text download interrupted")
        return 130
    finally:
        finished_at = datetime.now(DISPLAY_TIMEZONE)
        elapsed_minutes = int((time.monotonic() - started_monotonic) // 60)
        elapsed_hours, remaining_minutes = divmod(elapsed_minutes, 60)
        print(f"End time: {finished_at:%Y-%m-%d %H:%M:%S %Z}", flush=True)
        print(f"Elapsed time: {elapsed_hours}h {remaining_minutes:02d}m", flush=True)
        print("*PROGRAM END*", flush=True)


if __name__ == "__main__":
    sys.exit(main())
