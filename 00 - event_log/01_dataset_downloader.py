"""Download the complete document event log of closed InfoCuria cases.

Collect all C, T and F proceedings whose main case closed in the selected years, including the complete document history and available EUR-Lex links. Create the data directory automatically and retain cached responses without expiry for resumable runs. Use --refresh to update previously downloaded details.

Country is one original or procedural language code per CaseID. Advisory cases prefer their original Opinion language; multilingual case cards are checked against their own official notices and use MULTI if several languages remain.
Every event of a case receives the same resolved value. Missing language data is reported as a case error, preventing an incomplete run from claiming success.
Date_Lodging records each proceeding's official introductionDate on all its events.

Python 3.12. Run with --help for options and see README.md for scope and schema.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import re
import sqlite3
import sys
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal, overload
from urllib.parse import urlencode, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

SITE_URL = "https://infocuria.curia.europa.eu"
API_URL = "https://infocuriaws.curia.europa.eu/elastic-connector/"
DATA_DIR = Path(__file__).resolve().parent / "data"
FIRST_YEAR = 1952
START_YEAR = 2017
END_YEAR = 2026
CLOSED = "CLOTPUB"
COURTS = ("C", "T", "F")
ECLI_PATTERN = re.compile(r"\bECLI:[A-Z]{2}:[A-Z0-9_]+:\d{4}:[A-Z0-9._-]+", re.IGNORECASE)
JUDICIAL_TYPES = {
    "ARRET", "ARRET_DR", "ARRET_EXT", "ARRET_NP", "AVIS", "CONCL",
    "DECISION", "DELI", "INF", "ORD", "ORD_DR", "ORD_EXT", "ORD_NP",
    "PPO_AG", "REF",
}
OTHER_TYPES = {
    "ARRET_SOM", "ARR_COMM", "AVIS_SOM", "AVIS_COMM", "DEC_SOM", "DEMAV",
    "ORD_COMM", "ORD_SOM", "PV_COMM", "RAD_COMM", "REQ_COMM", "RES",
    "RX_COMM", "SA_COMM", "DDP_COMM",
}
EVENT_FIELDS = [
    "CaseID", "Activity", "Timestamp", "Party_Names", "Area_of_Law", "EurLex_Link",
    "EurLex_Country", "Country", "Case_Language", "ECLI",
    "EurLex_Links", "CELEX", "CELEX_Source", "MainCaseID", "Date_Lodging", "ProcedureID",
    "EventID", "DocumentID", "Document_Type", "Activity_Party", "Case_Status",
    "Case_Closed_Date", "Procedure_Closed_Date", "Curia_Link", "Date_Quality",
]
CASE_FIELDS = [
    "MainCaseID", "AffairID", "Court", "Case_Status", "Date_Lodging", "Case_Closed_Date",
    "Party_Names", "Country", "Case_Language",
    "Procedure_Count", "Event_Count", "Curia_Link",
]
INDEX_FIELDS = (
    "affId", "procedureId", "publishedId", "publishedAffId", "jurisdictionCode",
    "procType", "introductionDate", "closeDate", "lastModifiedDate",
)


class DownloadError(RuntimeError):
    """A source response could not be fetched or verified."""


def clean_text(value: str) -> str:
    return " ".join(BeautifulSoup(value or "", "html.parser").stripped_strings)


def localised(values: list[dict], language: str) -> str:
    """Use the requested label, then the source's English/French fallback."""
    labels = {key.lower(): value for item in values or [] for key, value in item.items()}
    return clean_text(labels.get(language) or labels.get("en") or labels.get("fr") or "")


def compact_response(value: object) -> object:
    """Keep source metadata in the cache without full document text or facets."""
    if isinstance(value, list):
        return [compact_response(item) for item in value]
    if isinstance(value, dict):
        return {
            key: compact_response(item) for key, item in value.items()
            if key not in {"contentML", "aggregatesMap", "highlightFields"}
        }
    return value


class Client:
    """Bounded HTTP retries and a thread-safe response cache without expiry."""

    def __init__(self, path: Path, refresh: bool, delay: float):
        self.database = sqlite3.connect(path, check_same_thread=False)
        self.database.execute(
            "CREATE TABLE IF NOT EXISTS responses "
            "(key TEXT PRIMARY KEY, fetched_at TEXT NOT NULL, payload BLOB NOT NULL)"
        )
        self.database.commit()
        self.refresh = refresh
        self.delay = delay
        self.database_lock = threading.Lock()
        self.request_lock = threading.Lock()
        self.local = threading.local()
        self.last_request = 0.0
        self.cache_hits = 0
        self.requests_made = 0
        self.oldest_cached_response = None

    @overload
    def request(self, url: str, payload: dict | None = None, cache: bool = False,
                force: bool = False, headers: dict | None = None,
                as_text: Literal[False] = False) -> dict[str, Any]: ...

    @overload
    def request(self, url: str, payload: dict | None = None, cache: bool = False,
                force: bool = False, headers: dict | None = None, *,
                as_text: Literal[True]) -> str: ...

    @overload
    def request(self, url: str, payload: dict | None = None, cache: bool = False,
                force: bool = False, headers: dict | None = None,
                as_text: bool = False) -> dict[str, Any] | str: ...

    def request(self, url: str, payload: dict | None = None, cache: bool = False,
                force: bool = False, headers: dict | None = None,
                as_text: bool = False) -> dict[str, Any] | str:
        def validate_result(value: object) -> dict[str, Any] | str:
            if as_text:
                if isinstance(value, str):
                    return value
                raise DownloadError(f"Expected a text response at {url}")
            if isinstance(value, dict):
                return value
            raise DownloadError(f"Expected a JSON object at {url}")

        key = hashlib.sha256(json.dumps([url, payload], sort_keys=True).encode()).hexdigest()
        if cache and not self.refresh and not force:
            with self.database_lock:
                row = self.database.execute(
                    "SELECT payload, fetched_at FROM responses WHERE key = ?", (key,)
                ).fetchone()
                if row:
                    try:
                        # Validate report metadata; response age never expires the cache.
                        datetime.fromisoformat(row[1])
                        result = validate_result(json.loads(zlib.decompress(row[0])))
                        self.cache_hits += 1
                        self.oldest_cached_response = min(self.oldest_cached_response or row[1], row[1])
                        return result
                    except (ValueError, TypeError, zlib.error, DownloadError):
                        self.database.execute("DELETE FROM responses WHERE key = ?", (key,))
                        self.database.commit()
        if not hasattr(self.local, "session"):
            self.local.session = requests.Session()
            self.local.session.headers.update({
                "User-Agent": "InfoCuria-research-downloader/2.0 (Python requests)",
                "Accept": "application/json",
                "Origin": SITE_URL,
                "Referer": SITE_URL + "/",
            })
        for attempt in range(4):
            try:
                with self.request_lock:
                    time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
                    self.last_request = time.monotonic()
                    self.requests_made += 1
                response = self.local.session.request(
                    "POST" if payload is not None else "GET", url,
                    json=payload, headers=headers, timeout=(10, 60),
                )
                if as_text and response.status_code in {404, 410}:
                    return ""
                response.raise_for_status()
                if response.status_code != 200 or not response.content:
                    raise DownloadError(f"Unexpected HTTP {response.status_code} or empty body at {url}")
                result = validate_result(response.text if as_text else compact_response(response.json()))
                if cache:
                    with self.database_lock:
                        self.database.execute(
                            "INSERT OR REPLACE INTO responses VALUES (?, ?, ?)",
                            (key, datetime.now(UTC).isoformat(),
                             zlib.compress(json.dumps(result, ensure_ascii=False).encode())),
                        )
                        self.database.commit()
                return result
            except (requests.RequestException, ValueError, DownloadError) as exc:
                if attempt == 3:
                    raise DownloadError(f"Request failed after four attempts at {url}: {exc}") from exc
                logger.warning("Retry %s/3 at %s (%s)", attempt + 1, url, exc)
                time.sleep(min(2 ** attempt, 8))
        raise AssertionError("Unreachable retry state")

    def close(self):
        self.database.close()

    def invalidate(self, url: str, payload: dict | None = None):
        key = hashlib.sha256(json.dumps([url, payload], sort_keys=True).encode()).hexdigest()
        with self.database_lock:
            self.database.execute("DELETE FROM responses WHERE key = ?", (key,))
            self.database.commit()


def search_payload(language: str, start: date | None, end: date | None,
                   page: int, page_size: int, cases: list[str]) -> dict:
    advanced = []
    if start and end:
        advanced.append({"field": "closeDate", "values": [start.isoformat(), end.isoformat()]})
    if cases:
        advanced.append({"field": "affair", "values": [" OR ".join(f'"{case}"' for case in cases)]})
    return {
        "tabName": "affair", "language": language.upper(), "searchTerm": "",
        "searchSources": ["document", "metadata"],
        "pagination": {"pageNumber": page, "pageSize": page_size},
        "sortTermList": [{"sortDirection": "DESC", "sortTerm": "AFF_NUM"}],
        "filtersValue": [
            {"field": "affairState", "values": [CLOSED]},
            {"field": "jurisdiction", "values": list(COURTS)},
        ],
        "advancedFiltersValue": advanced,
    }


def verified_hits(response: dict, context: str, require_exact: bool = True) -> tuple[int, list]:
    total = response.get("totalHits")
    hits = response.get("searchHits")
    relation = response.get("totalHitsRelation")
    if not isinstance(total, int) or not isinstance(hits, list):
        raise DownloadError(f"Invalid response schema for {context}")
    if require_exact and relation != "EQUAL_TO":
        raise DownloadError(f"Inexact source count for {context}: {relation}")
    return total, hits


def discover(client: Client, args, report: dict) -> list[dict]:
    start, end = date(args.start_year, 1, 1), date(args.end_year, 12, 31)

    def search(first, last, page=0):
        return client.request(API_URL + "search", search_payload(
            args.language, first, last, page, args.page_size, args.case,
        ))

    if args.case:
        found = {}
        for requested in args.case:
            response = client.request(API_URL + "search", search_payload(
                args.language, start, end, 0, args.page_size, [requested],
            ))
            total, hits = verified_hits(response, requested)
            if not total or total > 10000:
                raise DownloadError(f"Case filter did not yield a bounded closed case: {requested}")
            all_hits = []
            for page in range((total + args.page_size - 1) // args.page_size):
                if page:
                    response = client.request(API_URL + "search", search_payload(
                        args.language, start, end, page, args.page_size, [requested],
                    ))
                    page_total, hits = verified_hits(response, requested)
                    if page_total != total:
                        raise DownloadError(f"Source count changed during case search for {requested}")
                if len(hits) != min(args.page_size, total - page * args.page_size):
                    raise DownloadError(f"Truncated case search for {requested}")
                all_hits.extend(hits)
            procedure_ids = [hit["content"]["procedureId"] for hit in all_hits]
            if len(set(procedure_ids)) != total:
                raise DownloadError(f"Duplicate procedures in case search for {requested}")
            hits = [hit for hit in all_hits if requested in {
                hit["content"].get("publishedId"), hit["content"].get("publishedAffId"),
            }]
            if not hits:
                raise DownloadError(f"No exact closed-case match for {requested}")
            for hit in hits:
                case = hit["content"]
                if (case.get("affairStateCode") != CLOSED
                        or case.get("jurisdictionCode") not in COURTS
                        or not start <= date.fromisoformat(case["closeDate"]) <= end):
                    raise DownloadError(f"Case filter ignored closure criteria for {requested}")
                if case["affId"] not in found or case.get("procType") == "P":
                    found[case["affId"]] = {key: case[key] for key in INDEX_FIELDS if key in case}
        report.update(expected_cases=len(found), discovered_cases=len(found), partitions=[])
        return sorted(found.values(), key=lambda item: item["affId"])

    expected, _ = verified_hits(search(start, end), "whole scope")
    all_closed, _ = verified_hits(search(None, None), "all closed cases")
    earlier = 0
    if args.start_year > FIRST_YEAR:
        earlier, _ = verified_hits(search(date(FIRST_YEAR, 1, 1), date(args.start_year - 1, 12, 31)),
                                  "cases closed before start")
    later = 0
    if args.end_year < 9999:
        later, _ = verified_hits(search(date(args.end_year + 1, 1, 1), date(9999, 12, 31)),
                                 "cases closed after cutoff")
    report.update(expected_search_hits=expected, all_closed_search_hits=all_closed,
                  closed_before_start=earlier, closed_after_cutoff=later, partitions=[])
    if all_closed != earlier + expected + later:
        raise DownloadError(
            f"Closed cases with missing/uncovered closure dates: {all_closed - earlier - expected - later}"
        )
    logger.info("Source count: %s procedure records of cases closed between %s and %s", expected, start, end)
    found = {}
    procedure_ids = set()
    # Disjoint calendar years avoid Elasticsearch's 10,000-result window.
    for year in range(args.start_year, args.end_year + 1):
        first, last = date(year, 1, 1), date(year, 12, 31)
        response = search(first, last)
        total, hits = verified_hits(response, f"year {year}")
        if total > 10000:
            raise DownloadError(f"Year {year} exceeds the API result window ({total})")
        partition_ids = set()
        for page in range((total + args.page_size - 1) // args.page_size):
            if page:
                response = search(first, last, page)
                page_total, hits = verified_hits(response, f"year {year}, page {page}")
                if page_total != total:
                    raise DownloadError(f"Source count changed during year {year}; rerun discovery")
            expected_page_size = min(args.page_size, total - page * args.page_size)
            if len(hits) != expected_page_size:
                raise DownloadError(f"Truncated page for year {year}, page {page}")
            for hit in hits:
                case = hit["content"]
                aff_id = case["affId"]
                procedure_id = case["procedureId"]
                closed_on = date.fromisoformat(case["closeDate"])
                if (case.get("affairStateCode") != CLOSED
                        or case.get("jurisdictionCode") not in COURTS
                        or not first <= closed_on <= last):
                    raise DownloadError(f"Source ignored the requested filters for {aff_id}")
                if procedure_id in procedure_ids:
                    raise DownloadError(f"Duplicate procedure in search pagination: {procedure_id}")
                procedure_ids.add(procedure_id)
                partition_ids.add(procedure_id)
                if aff_id in found and found[aff_id]["closeDate"] != case["closeDate"]:
                    raise DownloadError(f"Conflicting main closure dates for {aff_id}")
                if aff_id not in found or case.get("procType") == "P":
                    found[aff_id] = {key: case[key] for key in INDEX_FIELDS if key in case}
        if len(partition_ids) != total:
            raise DownloadError(f"Incomplete partition {year}: {len(partition_ids)}/{total}")
        report["partitions"].append({"year": year, "expected": total, "retrieved": len(partition_ids)})
        if total:
            logger.info("Year %s: %s procedure records", year, total)
    if len(procedure_ids) != expected:
        raise DownloadError(f"Discovery mismatch: {len(procedure_ids)}/{expected} procedure records")
    # A final fresh count detects concurrent index changes during pagination.
    final_count, _ = verified_hits(search(start, end), "final scope count")
    if final_count != expected:
        raise DownloadError("The source count changed during discovery; rerun")
    report.update(expected_cases=len(found), discovered_cases=len(found),
                  discovered_search_hits=len(procedure_ids))
    return sorted(found.values(), key=lambda item: item["affId"])


def translation_value(translations: dict, path: str) -> str:
    if path in translations:
        return clean_text(translations[path])
    current = translations
    for part in path.split("."):
        if not isinstance(current, dict):
            return ""
        current = current.get(part, {})
    return clean_text(current) if isinstance(current, str) else ""


def celex_for_document(document: dict) -> str:
    """Mirror InfoCuria's buildAllCelex/getValidAllCelex URL identifiers."""
    kind = document.get("docTypeCode", "")
    celex = document.get("celex") or ""
    if kind == "CONCL" or not celex:
        parts = document.get("idProcedure", "").split("/")
        if len(parts) < 3:
            return celex
        court, number, short_year = parts[:3]
        century = "20" if int(short_year) < 50 else "19"
        celex = f"6{century}{short_year}{court}?{number}"
    judgment = [("TC", "TJ"), ("CC", "CJ"), ("FC", "FJ"),
                ("T?", "TJ"), ("C?", "CJ"), ("F?", "FJ")]
    order = [("TC", "TO"), ("CC", "CO"), ("FC", "FO"),
             ("T?", "TO"), ("C?", "CO"), ("F?", "FO")]
    replacements = []
    suffix = ""
    if kind in {"ARRET_DR", "ARRET_EXT", "ARRET_NP"}:
        replacements = judgment
    elif kind in {"ORD_DR", "ORD_EXT", "ORD_NP"}:
        replacements = order
    elif kind == "ARR_COMM":
        replacements = [("TJ", "TA"), ("TC", "TA"), ("CJ", "CA"), ("CC", "CA"),
                        ("FC", "FA"), ("FJ", "FA"), ("T?", "TA"), ("C?", "CA"), ("F?", "FA")]
    elif kind in {"ORD_COMM", "RAD_COMM"}:
        replacements = [("TO", "TB"), ("TC", "TB"), ("CC", "CB"), ("FC", "FB"),
                        ("T?", "TB"), ("C?", "CB"), ("F?", "FB")]
    elif kind in {"REQ_COMM", "PV_COMM", "SA_COMM", "DDP_COMM"}:
        replacements = [("TC", "TN"), ("CC", "CN"), ("FC", "FN"),
                        ("T?", "TN"), ("C?", "CN"), ("F?", "FN")]
        if kind == "DDP_COMM":
            replacements += [("TJ", "TN"), ("CJ", "CN"), ("FJ", "FN"),
                             ("TO", "TN"), ("CO", "CN"), ("FO", "FN")]
    elif kind == "CONCL":
        replacements = [("CP", "CC"), ("TP", "TC"), ("C?", "CC"), ("T?", "TC"), ("F?", "FC")]
    elif kind == "RES" and "_RES" not in celex:
        replacements, suffix = judgment, "_RES"
    elif kind in {"ARRET_SOM", "ORD_SOM"} and "_SUM" not in celex:
        replacements, suffix = order if kind == "ORD_SOM" else judgment, "_SUM"
    for before, after in replacements:
        celex = celex.replace(before, after, 1)
    return celex + suffix


def eurlex_links(document: dict, language: str) -> tuple[str, list[str]]:
    kind = document.get("docTypeCode", "")
    celex = celex_for_document(document)
    enabled = kind in OTHER_TYPES or (kind in JUDICIAL_TYPES and document.get("pubNum"))
    links = []
    if celex and enabled:
        section = "SUM" if "_RES" in celex else "TXT"
        query = urlencode({"uri": "CELEX:" + celex}, safe=":")
        links.append(f"https://eur-lex.europa.eu/legal-content/{language.upper()}/{section}/?{query}")
    legacy = document.get("lienPublicationRefJo") or ""
    if legacy:
        # InfoCuria's transformOldEurLexLink appends the ISO 639-2 code.
        iso3 = {
            "bg": "BUL", "cs": "CES", "da": "DAN", "de": "DEU", "el": "ELL",
            "en": "ENG", "es": "SPA", "et": "EST", "fi": "FIN", "fr": "FRA",
            "ga": "GLE", "hr": "HRV", "hu": "HUN", "it": "ITA", "lt": "LIT",
            "lv": "LAV", "mt": "MLT", "nl": "NLD", "pl": "POL", "pt": "POR",
            "ro": "RON", "sk": "SLK", "sl": "SLV", "sv": "SWE",
        }
        legacy = legacy.replace("{0}", language) + iso3.get(language, "")
        if urlparse(legacy).hostname == "eur-lex.europa.eu":
            links.insert(0, legacy)
    # Retain literal source document links as well as the frontend CELEX link.
    for key in ("docUrl",):
        link = document.get(key) or ""
        if link and "{0}" not in link and urlparse(link).hostname == "eur-lex.europa.eu":
            links.append(link)
    return celex, list(dict.fromkeys(links))


LANGUAGE_CODES = frozenset({
    "BG", "CS", "DA", "DE", "EL", "EN", "ES", "ET", "FI", "FR", "GA", "HR",
    "HU", "IT", "LT", "LV", "MT", "NL", "PL", "PT", "RO", "SK", "SL", "SV",
})


def resolve_case_language(procedure: dict, main: dict | None = None) -> str:
    """Read a single case language, preferring the original language for an Avis."""
    advisory = procedure.get("natureCode") == "AVIS" or str(procedure.get("publishedId", "")).startswith("Avis ")
    if advisory:
        original = clean_text(procedure.get("conclLang", "")).upper()
        if original in LANGUAGE_CODES:
            return original
    raw = clean_text(procedure.get("procLang", "")).upper()
    if not raw and main and main is not procedure:
        return resolve_case_language(main)
    return raw if raw in LANGUAGE_CODES else ""


LANGUAGE_NAMES = {
    'Bulgarian': 'BG', 'Croatian': 'HR', 'Czech': 'CS', 'Danish': 'DA',
    'Dutch': 'NL', 'English': 'EN', 'Estonian': 'ET', 'Finnish': 'FI',
    'French': 'FR', 'German': 'DE', 'Greek': 'EL', 'Hungarian': 'HU',
    'Irish': 'GA', 'Italian': 'IT', 'Latvian': 'LV', 'Lithuanian': 'LT',
    'Maltese': 'MT', 'Polish': 'PL', 'Portuguese': 'PT', 'Romanian': 'RO',
    'Slovak': 'SK', 'Slovenian': 'SL', 'Slovene': 'SL', 'Spanish': 'ES',
    'Swedish': 'SV',
}
_INITIAL_NOTICES = frozenset({'DDP_COMM','PV_COMM','REQ_COMM','AVI_COMM','SA_COMM','RX_COMM','DDP_INT'})
_LANGUAGE_LABEL = re.compile(
    r'\bLanguage(?:s)? of (?:the )?(?:case|proceedings|procedure|request|application)\s*:\s*', re.IGNORECASE,
)
_ORIGINAL_LABEL = re.compile(
    r'\bOriginal language(?: of (?:the )?(?:Opinion|conclusions|judgment))?\s*:\s*', re.IGNORECASE,
)
_LANGUAGE_VALUE = re.compile(r'(?:'+ '|'.join(sorted(map(re.escape, LANGUAGE_NAMES),key=len,reverse=True))+r')\b',re.IGNORECASE)
_NAME_CODES = {name.casefold():code for name,code in LANGUAGE_NAMES.items()}


def extract_notice_language(html: str, allow_original: bool = False) -> str:
    """Return the explicit case language, MULTI for several, or empty if absent.

    Original-language footnotes belong to the Advocate General's opinion and
    may only be enabled for advisory cases that explicitly need that field.
    """
    soup = BeautifulSoup(html, 'html.parser')
    paragraphs = soup.find_all('p')
    fragments = [' '.join(node.stripped_strings) for node in paragraphs]
    if not fragments:
        fragments = [soup.get_text(' ', strip=True)]
    labels = (_LANGUAGE_LABEL, _ORIGINAL_LABEL) if allow_original else (_LANGUAGE_LABEL,)
    codes = set()
    for fragment in fragments:
        for pattern in labels:
            for match in pattern.finditer(fragment):
                tail = fragment[match.end():]
                if re.match(r'all(?: the)? (?:official )?languages\b', tail, re.IGNORECASE):
                    return 'MULTI'
                first = _LANGUAGE_VALUE.match(tail)
                if first is None:
                    continue
                found = _NAME_CODES[first.group().casefold()]
                remainder = tail[first.end():]
                if re.match(r'\s*(?:,|/|;|\band\b|\bor\b|&)\s*' + _LANGUAGE_VALUE.pattern,
                            remainder, re.IGNORECASE):
                    return 'MULTI'
                codes.add(found)
    if len(codes) > 1:
        return 'MULTI'
    return next(iter(codes)) if codes else ''


def notice_candidates(procedure: dict, documents: list[dict]) -> list[tuple[str, str]]:
    """Return document types and HTML URLs, prioritising initial notices."""
    procedure_id = procedure.get('id') or procedure.get('procedureId','')
    year = str(procedure.get('introductionDate',''))[:4]
    court = procedure.get('jurisdictionCode','')
    if not procedure_id or not re.fullmatch(r'\d{4}',year) or court not in {'C','T','F'}:
        return []
    advisory = procedure.get('natureCode') == 'AVIS' or procedure.get('publishedId', '').startswith('Avis ')
    documents = [document for document in documents if advisory or document.get('docTypeCode') != 'CONCL']
    ordered = sorted(documents, key=lambda d:(
        0 if d.get('docTypeCode') in _INITIAL_NOTICES else 1,
        d.get('docDate') or '9999-12-31',
    ))
    result=[]
    for document in ordered:
        logical=str(document.get('logicDocId','')).removeprefix('id_')
        part=document.get('docNoPart')
        if not logical or part is None:
            continue
        url = ('https://infocuriaws.curia.europa.eu/blob/download-file-html/'
               f'{court}/{year}/{procedure_id.replace("/","_")}/{logical}-EN-{part}.html')
        result.append((document.get('docTypeCode',''),url))
    return result


def resolve_procedure_country(client: Client, procedure: dict, main: dict,
                              documents: list[dict]) -> str:
    """Resolve Country from the case card or its own official case notice."""
    country = resolve_case_language(procedure, main)
    if country:
        return country
    raw = clean_text(procedure.get("procLang") or main.get("procLang", "")).upper()
    languages = set(raw.split())
    for kind, url in notice_candidates(procedure, documents):
        try:
            html = client.request(url, cache=True, as_text=True)
            country = extract_notice_language(
                html, allow_original=procedure.get("natureCode") == "AVIS"
                or procedure.get("publishedId", "").startswith("Avis "),
            )
        except DownloadError as exc:
            logger.warning("Language notice unavailable for %s: %s", procedure["publishedId"], exc)
            continue
        if country == "MULTI" or (country and (not languages or country in languages)):
            logger.info("Case %s: Country %s from its official notice", procedure["publishedId"], country)
            return country
    if len(languages) > 1 and languages.issubset(LANGUAGE_CODES):
        logger.info("Case %s: Country MULTI; source languages: %s", procedure["publishedId"], raw)
        return "MULTI"
    raise DownloadError(f"No verifiable case language for {procedure['publishedId']}")


def case_url(case_id: str, language: str) -> str:
    return SITE_URL + "/tabs/affair?" + urlencode({"lang": language.upper(), "publishedId": case_id})


def parse_detail(response: dict, expected: dict, language: str,
                 translations: dict, french: dict, client: Client | None = None) -> tuple[list[dict], dict]:
    total, procedures = verified_hits(response, expected["affId"], require_exact=False)
    if total != len(procedures) or not procedures:
        raise DownloadError(f"Truncated procedure list for {expected['affId']}")
    rows = []
    event_ids = set()
    main = next((hit["content"] for hit in procedures if hit["content"].get("procType") == "P"), None)
    if not main:
        raise DownloadError(f"Main procedure missing for {expected['affId']}")
    if (main.get("affId") != expected["affId"]
            or main.get("affairStateCode") != CLOSED
            or main.get("closeDate") != expected["closeDate"]):
        raise DownloadError(f"Case changed since discovery: {expected['affId']}")
    main_id = main["publishedAffId"]
    countries = {}
    for hit in procedures:
        procedure = hit["content"]
        # Closure filters apply to the main case; subsidiary metadata can lag behind.
        if procedure.get("affId") != expected["affId"]:
            raise DownloadError(f"Unexpected subsidiary procedure for {expected['affId']}")
        documents = hit.get("innerHits", {}).get("document")
        if documents is None:
            raise DownloadError(f"Document section missing for {procedure['procedureId']}")
        doc_total, doc_hits = verified_hits(documents, procedure["procedureId"])
        if doc_total != len(doc_hits):
            raise DownloadError(f"Truncated document list for {procedure['procedureId']}")
        country = (resolve_procedure_country(client, procedure, main, [hit["content"] for hit in doc_hits])
                   if client is not None else resolve_case_language(procedure, main))
        countries[procedure["procedureId"]] = country
        for doc_hit in doc_hits:
            document = doc_hit["content"]
            if document.get("idProcedure") != procedure["procedureId"]:
                raise DownloadError("Document belongs to an unexpected procedure")
            logical_id = document.get("logicDocId") or document.get("id")
            if not logical_id:
                raise DownloadError("Document without a stable identifier")
            event_id = procedure["procedureId"] + "|" + logical_id
            if event_id in event_ids:
                raise DownloadError(f"Duplicate logical event: {event_id}")
            event_ids.add(event_id)
            kind = document.get("docTypeCode", "")
            activity = translation_value(translations, "facet.document.type." + kind)
            activity = activity or translation_value(translations, "facet-document-type-" + kind)
            activity = activity or clean_text(document.get("docType", "")) or kind
            ecli_match = ECLI_PATTERN.search(activity)
            ecli = clean_text(document.get("ecli", ""))
            if not ecli and ecli_match:
                ecli = ecli_match.group()
            activity = " ".join(ECLI_PATTERN.sub("", activity).split())
            author = translation_value(translations, document.get("authorCode", ""))
            author = author or clean_text(document.get("author", ""))
            if author:
                activity += f" ({author})"
            timestamp = document.get("docDate") or ""
            quality = ""
            try:
                parsed_date = date.fromisoformat(timestamp)
                if parsed_date > date.today():
                    quality = "future_source_date"
            except ValueError:
                quality = "missing_date" if not timestamp else "invalid_source_date"
            celex, links = eurlex_links(document, language)
            subjects = document.get("affairMatCodeML") or procedure.get("matCodeML") or []
            subject_labels = [localised(subject.get("label", []), language) for subject in subjects]
            rows.append({
                "CaseID": document.get("idPublished") or procedure["publishedId"],
                "Activity": activity,
                "Timestamp": timestamp,
                "Party_Names": localised(procedure.get("usualNameML", []), language)
                               or clean_text(procedure.get("parties", "")),
                "Area_of_Law": " | ".join(dict.fromkeys(label for label in subject_labels if label)),
                "EurLex_Link": links[0] if links else "",
                "EurLex_Country": country,
                "Country": country,
                "Case_Language": country,
                "ECLI": ecli,
                "EurLex_Links": json.dumps(links, ensure_ascii=False),
                "CELEX": celex,
                "CELEX_Source": document.get("celex", ""),
                "MainCaseID": main_id,
                "Date_Lodging": procedure.get("introductionDate") or "",
                "ProcedureID": procedure["procedureId"],
                "EventID": event_id,
                "DocumentID": document.get("docId", logical_id),
                "Document_Type": kind,
                "Activity_Party": author,
                "Case_Status": CLOSED,
                "Case_Closed_Date": main["closeDate"],
                "Procedure_Closed_Date": procedure.get("procClosDate", ""),
                "Curia_Link": case_url(procedure["publishedId"], language),
                "Date_Quality": quality,
            })
    country = countries[main["procedureId"]]
    inventory = {
        "MainCaseID": main_id,
        "AffairID": main["affId"],
        "Court": main["jurisdictionCode"],
        "Case_Status": CLOSED,
        "Date_Lodging": main.get("introductionDate") or "",
        "Case_Closed_Date": main["closeDate"],
        "Party_Names": localised(main.get("usualNameML", []), language)
                       or clean_text(main.get("parties", "")),
        "Country": country,
        "Case_Language": country,
        "Procedure_Count": len(procedures),
        "Event_Count": len(rows),
        "Curia_Link": case_url(main_id, language),
    }
    return rows, inventory


def write_csv(path: Path, fields: list[str], rows: list[dict]):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def write_report(path: Path, report: dict):
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def enrich_existing_csv(args) -> int:
    """Repair Country using the case cards, preserving the existing event records."""
    path = args.enrich_countries.resolve()
    obsolete = {"Country_Code", "Country_Source", "Country_Inferred", "Country_Inferred_Code", "Country_Inference_Source"}

    def read_records(filename):
        with filename.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream, delimiter=";")
            fields = [field for field in reader.fieldnames or [] if field not in obsolete]
            records = [{key: value for key, value in row.items() if key not in obsolete} for row in reader]
        if "Country" not in fields:
            fields.append("Country")
        return fields, records

    fields, rows = read_records(path)
    if "CaseID" not in fields:
        raise DownloadError("The country repair input must contain CaseID")
    inventory_path = path.with_name(path.name.replace("curia_scraping_", "curia_cases_", 1))
    inventories, inventory_fields = [], []
    if inventory_path != path and inventory_path.exists():
        inventory_fields, inventories = read_records(inventory_path)
    inventory_map = {record["MainCaseID"]: record for record in inventories}
    groups = {main_id: [] for main_id in inventory_map}
    for row in rows:
        groups.setdefault(row.get("MainCaseID") or row["CaseID"], []).append(row)
    report = {
        "started_at": datetime.now(UTC).isoformat(), "input_file": str(path),
        "events": len(rows), "cases": len(groups), "source": SITE_URL,
        "country_definition": "original_or_procedural_language", "failures": [], "successful": False,
    }
    client = Client(args.data_dir / "infocuria_cache.sqlite3", args.refresh, args.delay)
    try:
        for index, (main_id, events) in enumerate(groups.items(), start=1):
            try:
                inventory = inventory_map.get(main_id, {})
                affair_id = inventory.get("AffairID")
                if not affair_id:
                    procedure_id = next((row.get("ProcedureID") for row in events if row.get("ProcedureID")), "")
                    affair_id = procedure_id.rsplit("/", 2)[0] if procedure_id else ""
                if not affair_id:
                    response = client.request(API_URL + "search", search_payload(
                        args.language, None, None, 0, 100, [main_id],
                    ))
                    _, hits = verified_hits(response, main_id)
                    exact = [hit["content"] for hit in hits if hit["content"].get("publishedId") == main_id]
                    if len(exact) != 1:
                        raise DownloadError(f"Cannot identify the case card for {main_id}")
                    affair_id = exact[0]["affId"]
                payload = {"affId": affair_id, "searchTerm": "", "tabName": "affair",
                           "language": args.language.upper(), "advancedFiltersValue": []}
                detail = client.request(API_URL + "affairId/procedures", payload, cache=True)
                total, hits = verified_hits(detail, main_id, require_exact=False)
                if total != len(hits):
                    raise DownloadError(f"Truncated case card for {main_id}")
                main = next((hit["content"] for hit in hits if hit["content"].get("procType") == "P"), None)
                if not main:
                    raise DownloadError(f"Main procedure missing for {main_id}")
                countries = {}
                for hit in hits:
                    procedure = hit["content"]
                    if procedure.get("affId") != affair_id:
                        raise DownloadError(f"Unexpected case card for {main_id}")
                    documents = hit.get("innerHits", {}).get("document", {})
                    doc_total, doc_hits = verified_hits(documents, procedure["publishedId"])
                    if doc_total != len(doc_hits):
                        raise DownloadError(f"Truncated document list for {procedure['publishedId']}")
                    countries[procedure["publishedId"]] = resolve_procedure_country(
                        client, procedure, main, [document["content"] for document in doc_hits],
                    )
                for record in events:
                    country = countries.get(record["CaseID"])
                    if not country:
                        raise DownloadError(f"Country missing for {record['CaseID']}")
                    record["Country"] = country
                    for alias in ("EurLex_Country", "Case_Language"):
                        if alias in fields:
                            record[alias] = country
                if inventory:
                    inventory["Country"] = countries[main_id]
                    if "Case_Language" in inventory_fields:
                        inventory["Case_Language"] = countries[main_id]
            except (DownloadError, KeyError, ValueError, TypeError) as exc:
                report["failures"].append({"CaseID": main_id, "error": str(exc)})
                logger.error("Country repair failed for %s: %s", main_id, exc)
            if index % 1000 == 0 or index == len(groups):
                logger.info("Case languages checked: %s/%s", index, len(groups))
        if not report["failures"]:
            write_csv(path, fields, rows)
            if inventories:
                write_csv(inventory_path, inventory_fields, inventories)
            report.update(successful=True, events_with_country=len(rows),
                          cases_with_country=len(groups), cases_without_country=0,
                          multilingual_cases=len({row["CaseID"] for row in rows if row["Country"] == "MULTI"}))
            logger.info("Country repaired for every event in %s", path)
    except (DownloadError, OSError, KeyError, ValueError, TypeError) as exc:
        report["failures"].append({"error": str(exc)})
        logger.error("Country repair stopped: %s", exc)
    except KeyboardInterrupt:
        report["failures"].append({"error": "Interrupted by user"})
        logger.warning("Country repair interrupted; source files have not been replaced")
    finally:
        report.update(finished_at=datetime.now(UTC).isoformat(),
                      http_requests=client.requests_made, cache_hits=client.cache_hits)
        client.close()
        report_path = args.data_dir / f"country_enrichment_report_{args.language}.json"
        write_report(report_path, report)
        logger.info("Country repair report saved to %s", report_path)
    return 0 if report["successful"] else 1


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-year", type=int, default=START_YEAR, help="Inclusive first main-case closure year (default 2017)")
    parser.add_argument("--end-year", type=int, default=END_YEAR, help="Inclusive main-case closure year (default 2026)")
    parser.add_argument("--language", default="en", help="InfoCuria label language (default en)")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--workers", type=int, default=4, help="Concurrent case-detail requests (1..8)")
    parser.add_argument("--page-size", type=int, default=1000, help="Search results per page (1..1000)")
    parser.add_argument("--delay", type=float, default=0.15, help="Minimum seconds between HTTP request starts")
    parser.add_argument("--refresh", action="store_true", help="Download details and translations again, bypassing the non-expiring cache")
    parser.add_argument("--case", action="append", default=[], help="Restrict to a case number, repeatable")
    parser.add_argument("--max-cases", type=int, help="Download a sample after verifying the complete case index")
    parser.add_argument("--discover-only", action="store_true", help="Verify and save the case index without fetching events")
    parser.add_argument("--enrich-countries", type=Path, metavar="CSV", help="Repair Country from the original/case language in an existing downloader CSV")
    args = parser.parse_args()
    args.language = args.language.lower()
    if not re.fullmatch(r"[a-z]{2}", args.language):
        parser.error("--language must be a two-letter language code")
    if not FIRST_YEAR <= args.start_year <= args.end_year < 9999:
        parser.error("Years must satisfy 1952 <= --start-year <= --end-year <= 9998")
    if not 1 <= args.workers <= 8 or not 1 <= args.page_size <= 1000 or args.delay < 0:
        parser.error("Invalid --workers, --page-size or --delay")
    if args.max_cases is not None and args.max_cases < 1:
        parser.error("--max-cases must be positive")
    return args


def main() -> int:
    args = arguments()
    args.data_dir.mkdir(parents=True, exist_ok=True)
    (args.data_dir / ".gitkeep").touch(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s -- %(levelname)s -- %(message)s")
    if args.enrich_countries:
        return enrich_existing_csv(args)
    sample = bool(args.case or args.max_cases)
    suffix = "_sample" if sample else ""
    stem = f"{args.language}{suffix}"
    report_path = args.data_dir / f"download_report_{stem}.json"
    report = {
        "started_at": datetime.now(UTC).isoformat(), "source": SITE_URL,
        "api": API_URL, "start_year": args.start_year, "end_year": args.end_year,
        "closure_start": f"{args.start_year}-01-01", "closure_cutoff": f"{args.end_year}-12-31",
        "courts": list(COURTS), "language": args.language, "case_filter": args.case,
        "sample": sample, "complete": False, "failures": [],
        "future_year_end": date(args.end_year, 12, 31) > date.today(),
        "cache_refreshed": args.refresh,
        "country_definition": "original_or_procedural_language",
    }
    client = Client(args.data_dir / "infocuria_cache.sqlite3", args.refresh, args.delay)
    exit_code = 1
    try:
        cases = discover(client, args, report)
        write_report(args.data_dir / f"case_index_{stem}.json", {
            "start_year": args.start_year, "end_year": args.end_year,
            "closure_start": report["closure_start"],
            "closure_cutoff": report["closure_cutoff"], "cases": cases,
        })
        if args.discover_only:
            report["discovery_complete"] = True
            report["events_downloaded"] = False
            exit_code = 0
        else:
            selected = cases[:args.max_cases] if args.max_cases else cases
            report["selected_cases"] = len(selected)
            logger.info("Total cases to download: %s", len(selected))
            print()
            print("Starting download of selected cases...")
            print()
            translations = client.request(SITE_URL + f"/assets/i18n/{args.language}.json", cache=True)
            french = client.request(SITE_URL + "/assets/i18n/fr.json", cache=True)

            def download(case: dict) -> tuple[tuple[list[dict], dict], None] | tuple[None, dict]:
                payload = {
                    "affId": case["affId"], "searchTerm": "", "tabName": "affair",
                    "language": args.language.upper(), "advancedFiltersValue": [],
                }
                try:
                    for attempt in range(2):
                        detail = client.request(API_URL + "affairId/procedures", payload,
                                                cache=True, force=bool(attempt))
                        try:
                            parsed = parse_detail(detail, case, args.language, translations, french, client=client)
                            return parsed, None
                        except (DownloadError, KeyError, ValueError, TypeError):
                            client.invalidate(API_URL + "affairId/procedures", payload)
                            if attempt:
                                raise
                except (DownloadError, KeyError, ValueError, TypeError) as exc:
                    return None, {"AffairID": case["affId"], "CaseID": case.get("publishedAffId"), "error": str(exc)}
                raise AssertionError("Unreachable detail retry state")

            inventories = []
            event_count = date_anomalies = 0
            staging_path = args.data_dir / f"curia_scraping_{stem}_partial.csv.tmp"
            with staging_path.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=EVENT_FIELDS, delimiter=";")
                writer.writeheader()
                with ThreadPoolExecutor(max_workers=args.workers) as executor:
                    # Batches bound pending futures and memory on Python 3.12.
                    batch_size = 100
                    index = 0
                    for offset in range(0, len(selected), batch_size):
                        batch = selected[offset:offset + batch_size]
                        results = list(executor.map(download, batch))
                        for result, failure in results:
                            index += 1
                            if failure is not None:
                                report["failures"].append(failure)
                                logger.error("Case failed: %s", failure)
                            elif result is not None:
                                events, inventory = result
                                events.sort(key=lambda row: (row["CaseID"], row["Timestamp"], row["EventID"]))
                                writer.writerows(events)
                                event_count += len(events)
                                date_anomalies += sum(bool(row["Date_Quality"]) for row in events)
                                inventories.append(inventory)
                            else:
                                raise AssertionError("Download returned neither a result nor a failure")
                            if index % 100 == 0 or index == len(selected):
                                stream.flush()
                                logger.info("Cases processed: %s/%s; events: %s", index, len(selected), event_count)
            report.update(
                processed_cases=len(inventories), events=event_count,
                cases_without_events=sum(item["Event_Count"] == 0 for item in inventories),
                cases_without_country=sum(not item["Country"] for item in inventories),
                cases_without_language=sum(not item["Case_Language"] for item in inventories),
                multilingual_main_cases=sum(item["Country"] == "MULTI" for item in inventories),
                date_anomalies=date_anomalies,
                successful=not report["failures"], events_downloaded=True,
            )
            output_stem = stem + ("_partial" if report["failures"] else "")
            event_path = args.data_dir / f"curia_scraping_{output_stem}.csv"
            case_path = args.data_dir / f"curia_cases_{output_stem}.csv"
            write_csv(case_path, CASE_FIELDS, inventories)
            staging_path.replace(event_path)
            report["output_files"] = [str(event_path.resolve()), str(case_path.resolve())]
            report["complete"] = not sample and len(inventories) == report["expected_cases"] and not report["failures"]
            logger.info("Saved %s events for %s main cases to %s", event_count, len(inventories), event_path)
            exit_code = 0 if not report["failures"] else 1
    except (DownloadError, OSError, KeyError, ValueError, TypeError) as exc:
        report["complete"] = False
        report["successful"] = False
        logger.error("Download stopped: %s", exc)
        report["failures"].append({"error": str(exc)})
    except KeyboardInterrupt:
        report["complete"] = False
        logger.warning("Interrupted; completed HTTP responses are retained in the cache")
        report["failures"].append({"error": "Interrupted by user"})
        exit_code = 130
    finally:
        report.update(
            finished_at=datetime.now(UTC).isoformat(),
            http_requests=client.requests_made, cache_hits=client.cache_hits,
            oldest_cached_response_at=client.oldest_cached_response,
            cache_expires=False, cache_ttl_hours=None,
        )
        client.close()
        write_report(report_path, report)
        logger.info("Report saved to %s", report_path)
    return exit_code


if __name__ == "__main__":
    started_at = datetime.now().astimezone()
    started_monotonic = time.monotonic()
    print()
    print("*** PROGRAM START ****", flush=True)
    print()
    print(f"Start time: {started_at:%Y-%m-%d %H:%M:%S %Z}", flush=True)
    try:
        sys.exit(main())
    finally:
        finished_at = datetime.now().astimezone()
        elapsed_minutes = int((time.monotonic() - started_monotonic) // 60)
        elapsed_hours, remaining_minutes = divmod(elapsed_minutes, 60)
        print(f"End time: {finished_at:%Y-%m-%d %H:%M:%S %Z}", flush=True)
        print(f"Elapsed time: {elapsed_hours}h {remaining_minutes:02d}m", flush=True)
        print()
        print("*** PROGRAM END ****", flush=True)
        print()
