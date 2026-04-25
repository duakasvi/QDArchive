from __future__ import annotations

import json
import logging
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, NavigableString, Tag
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from schemas.normalizers import (
    bool_from_flag,
    canonicalize_doi,
    clean_html_list,
    clean_list,
    clean_text,
    ensure_list,
    first_non_empty,
    maybe_json_load,
    merge_unique_list,
    normalize_metadata_record,
    parse_href_filename_and_extension,
    safe_filename,
    strip_html_fragment,
    unique_dicts,
)
from schemas.unified_metadata import new_metadata_record
from schemas.validators import annotate_record_with_validation


# =========================================================
# MODULE CONFIGURATION
# =========================================================
PARSER_VERSION = "1.0.0"

# DEFAULT_SOURCE_URL = "https://www.openicpsr.org/openicpsr/project/245048/version/V1/view"
DEFAULT_SOURCE_URL = "https://www.openicpsr.org/openicpsr/project/221341/version/V1/view"

DEFAULT_HEADLESS = True
DEFAULT_WAIT_MS = 5000
DEFAULT_REQUEST_TIMEOUT = 60
DEFAULT_PLAYWRIGHT_TIMEOUT_MS = 45000

DEFAULT_SAVE_OUTPUT_JSON = True
DEFAULT_OUTPUT_DIR = "KASVI/metadata"

DEFAULT_LOG_LEVEL = logging.INFO
LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)

REPOSITORY_NAME = "openICPSR"
REPOSITORY_URL = "https://www.openicpsr.org"

EXPORT_FORMAT_PATTERNS = {
    "Dublin Core": [
        r"\bdublin core\b",
        r"\boai[_-]?dc\b",
    ],
    "DDI 2.5": [
        r"\bddi\s*2\.5\b",
        r"\boai[_-]?ddi25\b",
        r"\bddi25\b",
    ],
    "DATS 2.2 (JSON)": [
        r"\bdats\s*2\.2\s*\(json\)\b",
        r"\bdats\s*2\.2\b",
        r"\bdats\b",
    ],
    "DCAT-US 1.1 (beta)": [
        r"\bdcat-us\s*1\.1\s*\(beta\)\b",
        r"\bdcat[- ]us\s*1\.1\b",
        r"\bdcat-us\b",
    ],
}

MIME_BY_EXTENSION = {
    "pdf": "application/pdf",
    "txt": "text/plain",
    "tsv": "text/tab-separated-values",
    "csv": "text/csv",
    "xml": "application/xml",
    "json": "application/json",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc": "application/msword",
    "zip": "application/zip",
    "gz": "application/gzip",
    "rda": "application/octet-stream",
    "sav": "application/octet-stream",
    "dta": "application/octet-stream",
    "sas": "text/plain",
    "sps": "text/plain",
    "dct": "text/plain",
    "do": "text/plain",
}

FILE_EXTENSION_TO_CATEGORY = {
    "pdf": "documentation",
    "doc": "documentation",
    "docx": "documentation",
    "txt": "data",
    "csv": "data",
    "tsv": "data",
    "xlsx": "data",
    "xls": "data",
    "zip": "data",
    "sav": "data",
    "dta": "data",
    "rda": "data",
    "sas": "setup",
    "sps": "setup",
    "dct": "setup",
    "do": "setup",
}

MetadataRecord = Dict[str, Any]
PagePackage = Dict[str, Any]


logger = logging.getLogger(__name__)


# =========================================================
# EXCEPTIONS
# =========================================================
class ExtractionError(RuntimeError):
    """Raised when the extractor cannot complete the minimum required workflow."""


# =========================================================
# RUNTIME CONFIG
# =========================================================
@dataclass(frozen=True)
class ExtractorConfig:
    """Configuration for one openICPSR extraction run."""

    source_url: str = DEFAULT_SOURCE_URL
    save_output_json: bool = DEFAULT_SAVE_OUTPUT_JSON
    output_dir: str = DEFAULT_OUTPUT_DIR
    headless: bool = DEFAULT_HEADLESS
    wait_ms: int = DEFAULT_WAIT_MS
    request_timeout: int = DEFAULT_REQUEST_TIMEOUT
    playwright_timeout_ms: int = DEFAULT_PLAYWRIGHT_TIMEOUT_MS
    user_agent: str = DEFAULT_USER_AGENT


# =========================================================
# LOGGING
# =========================================================
def setup_logging(level: int = DEFAULT_LOG_LEVEL) -> None:
    """
    Configure extractor logging.

    INFO:
    - lifecycle milestones
    - fetch success/failure summaries
    - extraction summary

    DEBUG:
    - parser fallbacks
    - extraction counts
    - low-level troubleshooting details
    """
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        logging.basicConfig(level=level, format=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
    else:
        root_logger.setLevel(level)


# =========================================================
# GENERAL HELPERS
# =========================================================
def now_utc() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


def html_to_visible_text(html_content: str) -> str:
    """Extract visible text from an HTML document."""
    soup = BeautifulSoup(html_content, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return clean_text(soup.get_text("\n", strip=True))


def html_looks_usable(html_content: str) -> bool:
    """Heuristic check for minimally usable HTML content."""
    if not html_content:
        return False
    lower = html_content.lower()
    if "<html" not in lower:
        return False
    if "<title" not in lower:
        return False
    if len(html_content) < 1500:
        return False
    return True


def normalize_date_like(value: Any) -> str:
    """
    Normalize date-like text conservatively.

    openICPSR pages often expose human-readable date strings, so we avoid
    aggressive coercion and preserve the source semantics.
    """
    return clean_text(value)


def infer_project_id(url: str) -> str:
    """Extract openICPSR project ID from a URL."""
    match = re.search(r"/project/(\d+)", url)
    return match.group(1) if match else ""


def infer_version_from_url(url: str) -> str:
    """Extract version label from an openICPSR URL."""
    match = re.search(r"/version/([^/]+)/", url)
    return match.group(1) if match else ""


def infer_mime_type(file_name: str, file_format: str = "") -> str:
    """Infer MIME type from explicit file format or filename extension."""
    normalized_format = clean_text(file_format).lower()
    if normalized_format in MIME_BY_EXTENSION:
        return MIME_BY_EXTENSION[normalized_format]

    _, extension = parse_href_filename_and_extension(file_name)
    return MIME_BY_EXTENSION.get(extension, "")


def detect_export_formats_in_text(text: Any) -> List[str]:
    """Detect supported metadata export formats from arbitrary text."""
    lowered = clean_text(text).lower()
    if not lowered:
        return []

    found: List[str] = []
    for label, patterns in EXPORT_FORMAT_PATTERNS.items():
        if any(re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in patterns):
            found.append(label)
    return found


def guess_file_content(title: str, href: str, surrounding_text: str = "") -> str:
    """Infer a high-level file content label from title, URL, and local context."""
    hay = clean_text(f"{title} {href} {surrounding_text}").lower()

    if "metrics" in hay or "usage/download" in hay:
        return ""

    if "codebook" in hay:
        return "Codebook"
    if "questionnaire" in hay or "survey instrument" in hay:
        return "Questionnaire"
    if "readme" in hay or "documentation" in hay or "manual" in hay or "guide" in hay:
        return "Documentation"
    if "setup" in hay or "dictionary" in hay:
        return "Setup"
    if "transcript" in hay or "interview" in hay or "focus group" in hay:
        return "Data"

    _, extension = parse_href_filename_and_extension(href)
    if extension in {"sav", "dta", "rda", "csv", "tsv", "txt", "zip", "xlsx", "xls"}:
        return "Data"
    if extension in {"sas", "sps", "dct", "do"}:
        return "Setup"
    if extension in {"pdf", "doc", "docx"}:
        return "Documentation"

    if "download" in hay and "data" in hay:
        return "Data"

    return ""


# =========================================================
# BALANCED JS PARSING
# =========================================================
def read_balanced_expression(text: str, start_idx: int) -> Tuple[str, int]:
    """
    Read a balanced JavaScript assignment expression until a top-level semicolon.

    This allows extraction of inline JS objects/arrays that are not neatly
    embedded as JSON script blocks.
    """
    i = start_idx
    n = len(text)

    while i < n and text[i].isspace():
        i += 1

    expr_start = i
    depth_curly = 0
    depth_square = 0
    depth_paren = 0
    in_string = False
    escape = False
    string_char = ""

    while i < n:
        ch = text[i]

        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == string_char:
                in_string = False
        else:
            if ch in ("'", '"'):
                in_string = True
                string_char = ch
            elif ch == "{":
                depth_curly += 1
            elif ch == "}":
                depth_curly -= 1
            elif ch == "[":
                depth_square += 1
            elif ch == "]":
                depth_square -= 1
            elif ch == "(":
                depth_paren += 1
            elif ch == ")":
                depth_paren -= 1
            elif ch == ";" and depth_curly == 0 and depth_square == 0 and depth_paren == 0:
                return text[expr_start:i].strip(), i + 1

        i += 1

    return text[expr_start:i].strip(), i


def extract_all_variable_assignments(script_text: str) -> Dict[str, Any]:
    """
    Extract inline JavaScript assignments of the form:
    - var xyz = ...
    - variables.xyz = ...

    JSON-like values are parsed when possible; otherwise raw expressions are kept.
    """
    output: Dict[str, Any] = {}

    for match in re.finditer(r"(?:variables\.|var\s+)([A-Za-z0-9_]+)\s*=\s*", script_text):
        key = match.group(1)
        raw_expr, _ = read_balanced_expression(script_text, match.end())
        parsed = maybe_json_load(raw_expr.strip())
        output[key] = parsed if parsed is not None else raw_expr.strip()

    return output


# =========================================================
# HTML PARSERS
# =========================================================
def find_jsonld_dataset(soup: BeautifulSoup) -> Optional[Dict[str, Any]]:
    """Return the first JSON-LD Dataset object found in the document."""
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = (script.string or script.get_text() or "").strip()
        if not raw:
            continue

        parsed = maybe_json_load(raw)
        if isinstance(parsed, dict) and parsed.get("@type") == "Dataset":
            return parsed

        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict) and item.get("@type") == "Dataset":
                    return item
    return None


def extract_meta_map(soup: BeautifulSoup) -> Dict[str, List[str]]:
    """Extract HTML meta tags into a normalized multi-value map."""
    meta_map: Dict[str, List[str]] = {}
    for tag in soup.find_all("meta"):
        key = tag.get("name") or tag.get("property") or tag.get("http-equiv")
        value = tag.get("content")
        if key and value:
            meta_map.setdefault(key, [])
            cleaned = clean_text(value)
            if cleaned and cleaned not in meta_map[key]:
                meta_map[key].append(cleaned)
    return meta_map


def normalize_label(text: str) -> str:
    """Normalize a visible metadata label."""
    text = clean_text(text)
    text = re.sub(r"View help for.*$", "", text).strip()
    return text


def is_noise_value(text: str) -> bool:
    """Identify low-value label or value text that should be ignored."""
    lowered = clean_text(text).lower()
    return not lowered or lowered in {"hide", "beta"} or lowered.startswith("view help for")


def extract_metadata_sections(soup: BeautifulSoup) -> Dict[str, List[str]]:
    """
    Parse openICPSR metadata-like sections using visible headings and nearby siblings.

    This is intentionally heuristic because openICPSR pages often rely more on
    rendered layout than on rigid semantic blocks.
    """
    sections: Dict[str, List[str]] = {}

    for heading in soup.find_all(["h2", "h3", "h4"]):
        label = normalize_label(heading.get_text(" ", strip=True))
        if not label:
            continue

        nearby: List[str] = []
        sibling = heading.find_next_sibling()
        hops = 0

        while sibling and hops < 6:
            if isinstance(sibling, Tag) and sibling.name in {"h2", "h3", "h4"}:
                break

            text = clean_text(sibling.get_text(" ", strip=True)) if isinstance(sibling, Tag) else clean_text(str(sibling))
            if text and not is_noise_value(text):
                nearby.append(text)

            sibling = sibling.find_next_sibling()
            hops += 1

        nearby = clean_list(nearby)
        if nearby:
            sections[label] = nearby

    return sections


def extract_action_signals(soup: BeautifulSoup, visible_text: str) -> Dict[str, Any]:
    """
    Detect UI actions related to access and analysis.

    These signals are used as cross-checks against the structured metadata.
    """
    signals = {
        "documentation_only_download": False,
        "access_restricted_data_button": False,
        "analyze_online": False,
        "action_texts": [],
    }

    action_texts: List[str] = []

    for tag in soup.find_all(["a", "button", "input"]):
        parts = [
            tag.get_text(" ", strip=True),
            tag.get("value"),
            tag.get("aria-label"),
            tag.get("title"),
            tag.get("href"),
            tag.get("id"),
            " ".join(tag.get("class", [])),
            tag.get("data-title"),
        ]
        combined = clean_text(" | ".join([clean_text(part) for part in parts if clean_text(part)]))
        if not combined:
            continue

        display_label = first_non_empty(
            tag.get_text(" ", strip=True),
            tag.get("value"),
            tag.get("aria-label"),
            tag.get("title"),
            tag.get("id"),
            tag.get("href"),
        )
        if display_label:
            action_texts.append(display_label)

        lowered = combined.lower()
        if re.search(r"\bdocumentation only\b", lowered):
            signals["documentation_only_download"] = True
        if re.search(r"\baccess restricted data\b", lowered):
            signals["access_restricted_data_button"] = True
        if re.search(r"\banalyze online(?:\s*\(sda\))?\b", lowered):
            signals["analyze_online"] = True

    lowered_visible = visible_text.lower()
    if "documentation only" in lowered_visible:
        signals["documentation_only_download"] = True
    if "access restricted data" in lowered_visible:
        signals["access_restricted_data_button"] = True
    if "analyze online (sda)" in lowered_visible or re.search(r"\banalyze online\b", lowered_visible):
        signals["analyze_online"] = True

    signals["action_texts"] = clean_list(action_texts)
    return signals


def extract_export_formats_from_html(soup: BeautifulSoup, visible_text: str) -> List[str]:
    """Detect known export formats from page text and element metadata."""
    found: List[str] = []

    def add_formats(text: Any) -> None:
        for label in detect_export_formats_in_text(text):
            if label not in found:
                found.append(label)

    add_formats(visible_text)

    for tag in soup.find_all(["a", "button", "li", "div", "span"]):
        add_formats(tag.get_text(" ", strip=True))
        add_formats(tag.get("href"))
        add_formats(tag.get("title"))
        add_formats(tag.get("aria-label"))
        add_formats(tag.get("data-format"))
        add_formats(tag.get("id"))
        add_formats(" ".join(tag.get("class", [])))

    canonical_order = list(EXPORT_FORMAT_PATTERNS.keys())
    return [label for label in canonical_order if label in found]


def extract_openicpsr_file_rows(soup: BeautifulSoup, base_url: str) -> List[Dict[str, Any]]:
    """
    Extract visible file-like rows and links from an openICPSR project page.

    openICPSR does not expose a convenient public files API in the same way as ICPSR,
    so this extractor intentionally relies on HTML-visible file links.
    """
    results: List[Dict[str, Any]] = []
    seen_uris: Set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = clean_text(anchor.get("href"))
        title = clean_text(anchor.get_text(" ", strip=True))
        hay = f"{href} {title}".lower()

        if any(skip in hay for skip in [
            "download detailed metrics",
            "usage/download",
            "/metrics/",
            "cite",
            "citation",
            "endnote",
            "bibtex",
            "dublin core",
            "ddi 2.5",
            "dats 2.2",
            "dcat-us",
            "export metadata",
            "mailto:",
            "javascript:",
        ]):
            continue

        if not re.search(
            r"(type=file|/file/|/files/|download|\.pdf\b|\.docx?\b|\.zip\b|\.csv\b|\.txt\b|\.xlsx?\b|\.sav\b|\.dta\b|\.rda\b)",
            hay,
            re.IGNORECASE,
        ):
            continue

        absolute_href = urljoin(base_url, href)
        if absolute_href in seen_uris:
            continue
        seen_uris.add(absolute_href)

        file_name, extension = parse_href_filename_and_extension(absolute_href)

        row_text = ""
        row = anchor.find_parent("tr")
        if row:
            row_text = clean_text(row.get_text(" | ", strip=True))
        else:
            container = anchor.find_parent(["div", "li", "section", "article"])
            if container:
                row_text = clean_text(container.get_text(" | ", strip=True))

        size_match = re.search(r"\b(\d+(?:\.\d+)?)\s*(KB|MB|GB|bytes?)\b", row_text, flags=re.IGNORECASE)
        modified_match = re.search(
            r"\b(\d{4}-\d{2}-\d{2}|\w+\s+\d{1,2},\s+\d{4}|\d{1,2}/\d{1,2}/\d{2,4})\b",
            row_text,
            flags=re.IGNORECASE,
        )

        file_type = extension
        if not file_type:
            type_match = re.search(r"\b(pdf|docx?|zip|csv|txt|xlsx?|sav|dta|rda|sas|sps|dct|do)\b", row_text, flags=re.IGNORECASE)
            if type_match:
                file_type = type_match.group(1).lower()

        title_final = title or file_name
        guessed_content = guess_file_content(title_final, absolute_href, row_text)

        results.append(
            {
                "file_id": None,
                "dataset_number": None,
                "dataset_identifier": "",
                "title": title_final,
                "file_name": file_name,
                "file_stem": Path(file_name).stem if file_name else "",
                "file_format": file_type,
                "file_extension": extension or file_type,
                "mime_type": infer_mime_type(file_name, file_type),
                "file_content": guessed_content,
                "file_category": "",
                "file_description": row_text,
                "size_bytes": None,
                "size_compressed_bytes": None,
                "kbytes": None,
                "checksum": "",
                "checksum_type": "",
                "record_count": None,
                "variable_count": None,
                "case_count": None,
                "observation_count": None,
                "max_record_length": None,
                "rank": None,
                "public": True,
                "listed_on_public_page": True,
                "downloadable": True,
                "deliver_download": None,
                "include_in_bundle": [],
                "required_for_bundle": "",
                "dataset_format_flags": [],
                "terms_of_use": [],
                "resource_uuid": "",
                "uri": absolute_href,
                "download_url": absolute_href,
                "api_url": "",
                "parent_path": "",
                "folder_path": "",
                "last_modified": clean_text(modified_match.group(1)) if modified_match else "",
                "display_size": clean_text(size_match.group(0)) if size_match else "",
                "language": "",
                "source": "openicpsr_html",
                "raw_occurrence_count": 1,
            }
        )

    for file_entry in results:
        file_entry["file_category"] = classify_file_group(file_entry)

    return results


# =========================================================
# MERGE HELPERS
# =========================================================
def get_jsonld_value(dataset: Optional[Dict[str, Any]], key: str) -> Any:
    """Safely read a top-level JSON-LD key."""
    if not isinstance(dataset, dict):
        return None
    return dataset.get(key)


def get_meta_first(meta_map: Dict[str, List[str]], *keys: str) -> str:
    """Return the first available meta value from an ordered key list."""
    for key in keys:
        values = meta_map.get(key, [])
        if values:
            return values[0]
    return ""


def collect_sections(page: Dict[str, Any], label: str) -> List[str]:
    """Collect one visible metadata section from the packaged page."""
    return clean_list(page.get("sections", {}).get(label, []))


def parse_jsonld_authors_and_affiliations(jsonld: Optional[Dict[str, Any]]) -> Dict[str, List[str]]:
    """Extract creator names and affiliations from JSON-LD."""
    authors: List[str] = []
    affiliations: List[str] = []

    if not isinstance(jsonld, dict):
        return {"authors": authors, "affiliations": affiliations}

    for creator in ensure_list(jsonld.get("creator")):
        if not isinstance(creator, dict):
            continue

        name = clean_text(creator.get("name"))
        if name and name not in authors:
            authors.append(name)

        for affiliation in ensure_list(creator.get("affiliation")):
            if isinstance(affiliation, dict):
                aff_text = clean_text(affiliation.get("name"))
            else:
                aff_text = clean_text(affiliation)

            if aff_text and aff_text not in affiliations:
                affiliations.append(aff_text)

    return {"authors": authors, "affiliations": affiliations}


def parse_jsonld_doi(jsonld: Optional[Dict[str, Any]]) -> str:
    """Extract DOI-like identifier from JSON-LD."""
    if not isinstance(jsonld, dict):
        return ""

    identifier = jsonld.get("identifier")
    if isinstance(identifier, dict):
        return canonicalize_doi(
            first_non_empty(identifier.get("url"), identifier.get("@id"), identifier.get("value"))
        )
    return ""


def parse_funding(jsonld: Optional[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Extract funding info from JSON-LD funding blocks."""
    merged: Dict[Tuple[str, str], Dict[str, str]] = {}

    if isinstance(jsonld, dict):
        for item in ensure_list(jsonld.get("funding")):
            if not isinstance(item, dict):
                continue

            funder = item.get("funder", {})
            funder_name = clean_text(funder.get("name") if isinstance(funder, dict) else funder)
            identifier = clean_text(item.get("identifier"))
            key = (funder_name, identifier)

            entry = {
                "funder_name": funder_name,
                "identifier": identifier,
                "grant_number": identifier,
                "display": clean_text(item.get("display")),
                "url": clean_text(item.get("url")),
            }
            merged[key] = {k: v for k, v in entry.items() if v}

    return [value for value in merged.values() if value]


def parse_distributors(meta_map: Dict[str, List[str]], page: Dict[str, Any]) -> List[str]:
    """Extract distributor names from visible sections and citation-like meta fields."""
    distributors: List[str] = []
    distributors.extend(collect_sections(page, "Distributor(s)"))

    citation_candidates: List[str] = []
    for key in ["DC.bibliographicCitation", "citation_reference", "citation"]:
        citation_candidates.extend(meta_map.get(key, []))

    for citation in citation_candidates:
        for match in re.finditer(r"([^.;]+?)\s*\[distributor\]", citation, flags=re.IGNORECASE):
            candidate = clean_text(match.group(1))
            if candidate and candidate not in distributors:
                distributors.append(candidate)

    dc_publisher = clean_text(get_meta_first(meta_map, "DC.publisher"))
    if dc_publisher and any("distributor" in c.lower() for c in citation_candidates):
        if dc_publisher not in distributors:
            distributors.append(dc_publisher)

    return clean_list(distributors)


def dedupe_file_key(file_entry: Dict[str, Any]) -> str:
    """Return a stable deduplication key for a file entry."""
    return first_non_empty(
        file_entry.get("resource_uuid"),
        file_entry.get("file_id"),
        file_entry.get("uri"),
        f"{file_entry.get('file_name')}|{file_entry.get('title')}",
    )


def dedupe_file_entries(file_entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge duplicate file entries while preserving the richest combined record."""
    merged: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []

    for file_entry in file_entries:
        key = dedupe_file_key(file_entry)

        if key not in merged:
            merged[key] = deepcopy(file_entry)
            merged[key]["raw_occurrence_count"] = 1
            order.append(key)
            continue

        existing = merged[key]
        existing["raw_occurrence_count"] = existing.get("raw_occurrence_count", 1) + 1

        for field, value in file_entry.items():
            if field == "raw_occurrence_count":
                continue

            current = existing.get(field)
            if isinstance(current, list) and isinstance(value, list):
                existing[field] = merge_unique_list(current, value)
            elif isinstance(current, bool) and value is True:
                existing[field] = True
            elif current in (None, "", [], {}) and value not in (None, "", [], {}):
                existing[field] = deepcopy(value)

    return [merged[key] for key in order]


def classify_file_group(file_entry: Dict[str, Any]) -> str:
    """Classify a file into documentation, data, setup, or other."""
    content = clean_text(file_entry.get("file_content")).lower()
    if content in {"codebook", "questionnaire", "documentation"}:
        return "documentation"
    if content == "data":
        return "data"
    if content == "setup":
        return "setup"

    extension = clean_text(file_entry.get("file_extension")).lower()
    return FILE_EXTENSION_TO_CATEGORY.get(extension, "other")


def summarize_files(raw_files: List[Dict[str, Any]], unique_files: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute aggregate file metrics for files.summary."""
    raw_formats: Dict[str, int] = {}
    unique_formats: Dict[str, int] = {}
    raw_contents: Dict[str, int] = {}
    unique_contents: Dict[str, int] = {}

    for file_entry in raw_files:
        fmt = clean_text(file_entry.get("file_format"))
        if fmt:
            raw_formats[fmt] = raw_formats.get(fmt, 0) + 1

        content = clean_text(file_entry.get("file_content"))
        if content:
            raw_contents[content] = raw_contents.get(content, 0) + 1

    for file_entry in unique_files:
        fmt = clean_text(file_entry.get("file_format"))
        if fmt:
            unique_formats[fmt] = unique_formats.get(fmt, 0) + 1

        content = clean_text(file_entry.get("file_content"))
        if content:
            unique_contents[content] = unique_contents.get(content, 0) + 1

    documentation_files = [f for f in unique_files if classify_file_group(f) == "documentation"]
    data_files = [f for f in unique_files if classify_file_group(f) == "data"]
    setup_files = [f for f in unique_files if classify_file_group(f) == "setup"]
    other_files = [f for f in unique_files if classify_file_group(f) == "other"]

    downloadable_files = [f for f in unique_files if f.get("downloadable") is True]
    downloadable_documentation_files = [f for f in documentation_files if f.get("downloadable") is True]
    downloadable_data_files = [f for f in data_files if f.get("downloadable") is True]

    return {
        "raw_entry_count": len(raw_files),
        "unique_file_count": len(unique_files),
        "raw_public_entries": sum(1 for f in raw_files if f.get("public") is True),
        "unique_public_files": sum(1 for f in unique_files if f.get("public") is True),
        "raw_restricted_or_nonpublic_entries": sum(1 for f in raw_files if f.get("public") is False),
        "unique_restricted_or_nonpublic_files": sum(1 for f in unique_files if f.get("public") is False),
        "raw_downloadable_entries": sum(1 for f in raw_files if f.get("downloadable") is True),
        "unique_downloadable_files": len(downloadable_files),
        "downloadable_public_files": sum(1 for f in downloadable_files if f.get("public") is True),
        "downloadable_documentation_files": len(downloadable_documentation_files),
        "downloadable_data_files": len(downloadable_data_files),
        "documentation_file_count": len(documentation_files),
        "data_file_count": len(data_files),
        "setup_file_count": len(setup_files),
        "other_file_count": len(other_files),
        "raw_formats": raw_formats,
        "unique_formats": unique_formats,
        "raw_file_contents": raw_contents,
        "unique_file_contents": unique_contents,
    }


# =========================================================
# EXTRACTOR
# =========================================================
class OpenICPSRExtractor:
    """
    Production openICPSR project extractor.

    Responsibilities:
    - fetch the project page
    - parse HTML and inline metadata
    - infer file metadata from visible file links
    - emit the unified schema
    - run shared normalization and validation before save/return
    """

    def __init__(self, config: ExtractorConfig, logger_: Optional[logging.Logger] = None) -> None:
        self.config = config
        self.logger = logger_ or logging.getLogger(self.__class__.__name__)
        self.project_id = infer_project_id(config.source_url)

        if not self.project_id:
            raise ValueError(f"Could not infer openICPSR project ID from URL: {config.source_url}")

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": self.config.user_agent,
                "Accept-Language": "en-US,en;q=0.9",
            }
        )

    def __enter__(self) -> "OpenICPSRExtractor":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        """Close the shared HTTP session."""
        try:
            self.session.close()
        except Exception:
            self.logger.debug("Failed to close HTTP session", exc_info=True)

    def extract(self) -> MetadataRecord:
        """Run the full extraction pipeline and return unified metadata."""
        self.logger.info(
            "Starting openICPSR extraction | source_url=%s | project_id=%s",
            self.config.source_url,
            self.project_id,
        )

        record = self._initialize_record()
        page = self._fetch_and_package_page(record)

        self._populate_record_from_page(record, page)
        record["files"] = self._build_files_section(page, record)
        self._compute_access_flags(record)
        self._finalize_record(record, page)

        output_path = self._save_metadata_json(record)
        if output_path:
            self.logger.info("Saved metadata JSON to %s", output_path)

        self.logger.info(
            "Finished openICPSR extraction | project_id=%s | raw_files=%s | unique_files=%s | warnings=%s",
            record["study_id"],
            record["files"]["summary"]["raw_entry_count"],
            record["files"]["summary"]["unique_file_count"],
            len(record["diagnostics"]["field_warnings"]) + len(record["diagnostics"]["fetch_warnings"]),
        )
        return record

    # -----------------------------------------------------
    # RECORD INITIALIZATION
    # -----------------------------------------------------
    def _initialize_record(self) -> MetadataRecord:
        """Create the unified schema record and pre-populate repository constants."""
        record = new_metadata_record(self.config.source_url)

        record["generated_at_utc"] = now_utc()
        record["page_type"] = "openicpsr"
        record["repository"] = REPOSITORY_NAME
        record["repository_url"] = REPOSITORY_URL
        record["study_id"] = self.project_id

        record["provenance"]["repository"] = REPOSITORY_NAME
        record["provenance"]["repository_url"] = REPOSITORY_URL
        record["provenance"]["page_type"] = "openicpsr"
        record["provenance"]["parser_version"] = PARSER_VERSION
        record["provenance"]["schema_version"] = record["schema_version"]

        record["identifiers"]["study_id"] = self.project_id
        record["identifiers"]["source_record_id"] = self.project_id
        record["identifiers"]["other_identifiers"] = [
            {
                "type": "openICPSR project_id",
                "value": self.project_id,
            }
        ]

        return record

    # -----------------------------------------------------
    # FETCH LAYER
    # -----------------------------------------------------
    def _fetch_and_package_page(self, record: MetadataRecord) -> PagePackage:
        """Fetch and package the source project page."""
        try:
            packaged = self._package_page(self.config.source_url)

            fetch_method = packaged.get("fetch_method")
            if fetch_method and fetch_method not in record["diagnostics"]["metadata_sources"]:
                record["diagnostics"]["metadata_sources"].append(fetch_method)

            record["provenance"]["source_urls_visited"].append(packaged["final_url"])
            record["provenance"]["html_pages_fetched"].append(
                {
                    "page_name": "main",
                    "requested_url": self.config.source_url,
                    "final_url": packaged["final_url"],
                    "fetch_method": fetch_method,
                    "status_code": packaged.get("status_code"),
                }
            )
            if fetch_method and fetch_method not in record["provenance"]["fetch_methods"]:
                record["provenance"]["fetch_methods"].append(fetch_method)

            self.logger.info(
                "Fetched project page successfully | method=%s | status=%s",
                fetch_method,
                packaged.get("status_code"),
            )
            return packaged
        except Exception as exc:
            warning = f"Failed to fetch project page '{self.config.source_url}': {type(exc).__name__}: {exc}"
            record["diagnostics"]["fetch_warnings"].append(warning)
            self.logger.warning(warning)
            raise ExtractionError(warning) from exc

    def _fetch_rendered_page(self, url: str) -> Dict[str, Any]:
        """Fetch a page using requests first, then Playwright fallback if needed."""
        result = self._fetch_via_requests(url)
        if result is not None:
            return result
        return self._fetch_via_playwright(url)

    def _fetch_via_requests(self, url: str) -> Optional[Dict[str, Any]]:
        """Attempt a fast requests-based fetch and reject obviously unusable HTML."""
        try:
            response = self.session.get(url, timeout=self.config.request_timeout, allow_redirects=True)

            if response.status_code >= 400:
                self.logger.debug(
                    "Requests fetch returned HTTP >= 400 | url=%s | status=%s",
                    url,
                    response.status_code,
                )
                return None

            html_content = response.text
            if not html_looks_usable(html_content):
                self.logger.debug("Requests fetch returned unusable HTML | url=%s", url)
                return None

            soup = BeautifulSoup(html_content, "html.parser")
            title = clean_text(soup.title.get_text(" ", strip=True)) if soup.title else ""

            return {
                "requested_url": url,
                "final_url": response.url,
                "status_code": response.status_code,
                "page_title": title,
                "html": html_content,
                "visible_text": html_to_visible_text(html_content),
                "fetch_method": "requests",
                "content_type": clean_text(response.headers.get("content-type")),
            }
        except Exception:
            self.logger.debug("Requests fetch failed | url=%s", url, exc_info=True)
            return None

    def _fetch_via_playwright(self, url: str) -> Dict[str, Any]:
        """Fetch a page with Playwright for JS-rendered fallback support."""
        last_error: Optional[Exception] = None
        browser = None

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=self.config.headless)

                for wait_until in ("commit", "load"):
                    page = browser.new_page(
                        user_agent=self.config.user_agent,
                        viewport={"width": 1440, "height": 2200},
                    )
                    try:
                        page.goto(url, wait_until=wait_until, timeout=self.config.playwright_timeout_ms)
                        try:
                            page.wait_for_load_state("domcontentloaded", timeout=15000)
                        except PlaywrightTimeoutError:
                            self.logger.debug("Playwright domcontentloaded timed out | url=%s", url)

                        page.wait_for_timeout(self.config.wait_ms)
                        html_content = page.content()

                        try:
                            visible_text = page.locator("body").inner_text()
                        except Exception:
                            visible_text = html_to_visible_text(html_content)

                        return {
                            "requested_url": url,
                            "final_url": page.url,
                            "status_code": None,
                            "page_title": clean_text(page.title()),
                            "html": html_content,
                            "visible_text": visible_text,
                            "fetch_method": "playwright",
                            "content_type": "",
                        }
                    except Exception as exc:
                        last_error = exc
                        self.logger.debug(
                            "Playwright fetch attempt failed | url=%s | wait_until=%s | error=%s",
                            url,
                            wait_until,
                            exc,
                        )
                    finally:
                        try:
                            page.close()
                        except Exception:
                            self.logger.debug("Failed to close Playwright page | url=%s", url, exc_info=True)
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    self.logger.debug("Failed to close Playwright browser | url=%s", url, exc_info=True)

        raise ExtractionError(f"Failed to fetch page via Playwright: {type(last_error).__name__}: {last_error}")

    # -----------------------------------------------------
    # PAGE PACKAGING
    # -----------------------------------------------------
    def _package_page(self, url: str) -> PagePackage:
        """Fetch, parse, and package all extractable artifacts for the project page."""
        fetched = self._fetch_rendered_page(url)
        soup = BeautifulSoup(fetched["html"], "html.parser")

        all_variable_assignments: Dict[str, Any] = {}
        for script in soup.find_all("script"):
            script_text = script.get_text() or ""
            assignments = extract_all_variable_assignments(script_text)
            all_variable_assignments.update(assignments)

        jsonld_dataset = find_jsonld_dataset(soup)
        meta_map = extract_meta_map(soup)
        sections = extract_metadata_sections(soup)
        action_signals = extract_action_signals(soup, fetched["visible_text"])
        export_formats = extract_export_formats_from_html(soup, fetched["visible_text"])

        self.logger.debug(
            "Packaged openICPSR page | sections=%s | js_assignments=%s | export_formats=%s",
            len(sections),
            len(all_variable_assignments),
            export_formats,
        )

        return {
            "requested_url": fetched["requested_url"],
            "final_url": fetched["final_url"],
            "status_code": fetched["status_code"],
            "page_title": fetched["page_title"],
            "visible_text": fetched["visible_text"],
            "html": fetched["html"],
            "content_type": fetched.get("content_type", ""),
            "fetch_method": fetched.get("fetch_method", ""),
            "jsonld_dataset": jsonld_dataset,
            "meta_map": meta_map,
            "sections": sections,
            "action_signals": action_signals,
            "action_texts": action_signals.get("action_texts", []),
            "export_formats": export_formats,
            "variables_map": all_variable_assignments,
        }

    # -----------------------------------------------------
    # RECORD POPULATION
    # -----------------------------------------------------
    def _populate_record_from_page(self, record: MetadataRecord, page: PagePackage) -> None:
        """Populate the unified record from the packaged openICPSR project page."""
        record["final_url"] = page["final_url"]
        record["page_title"] = page["page_title"]
        record["study_id"] = first_non_empty(record["study_id"], infer_project_id(page["final_url"]))
        record["identifiers"]["study_id"] = record["study_id"]
        record["identifiers"]["source_record_id"] = record["study_id"]

        jsonld = page.get("jsonld_dataset")
        meta_map = page.get("meta_map", {})

        publisher_obj = get_jsonld_value(jsonld, "publisher")
        catalog_obj = get_jsonld_value(jsonld, "includedInDataCatalog")

        # ---------- identity ----------
        record["title"] = first_non_empty(
            get_jsonld_value(jsonld, "name"),
            get_meta_first(meta_map, "DC.title", "og:title", "twitter:title"),
            page["page_title"],
        )

        record["abstract"] = first_non_empty(
            get_jsonld_value(jsonld, "description"),
            get_meta_first(meta_map, "description", "og:description", "twitter:description"),
            collect_sections(page, "Summary")[0] if collect_sections(page, "Summary") else "",
        )
        record["summary"] = record["abstract"]

        doi_value = canonicalize_doi(
            first_non_empty(parse_jsonld_doi(jsonld), get_meta_first(meta_map, "DC.identifier"))
        )
        record["doi"] = doi_value
        record["version"] = first_non_empty(
            get_jsonld_value(jsonld, "version"),
            infer_version_from_url(page["final_url"]),
        )

        record["published_date"] = normalize_date_like(
            first_non_empty(get_jsonld_value(jsonld, "datePublished"), get_meta_first(meta_map, "DC.date"))
        )
        record["modified_date"] = normalize_date_like(get_jsonld_value(jsonld, "dateModified"))

        record["dates"]["published_date"] = record["published_date"]
        record["dates"]["modified_date"] = record["modified_date"]
        record["dates"]["release_date"] = record["published_date"]

        record["identifiers"]["doi"] = doi_value
        record["identifiers"]["doi_canonical"] = doi_value

        if doi_value:
            existing_identifiers = ensure_list(record["identifiers"].get("other_identifiers"))
            doi_identifier = {
                "type": "DOI",
                "value": doi_value,
                "url": doi_value,
            }
            if doi_identifier not in existing_identifiers:
                existing_identifiers.append(doi_identifier)
            record["identifiers"]["other_identifiers"] = existing_identifiers

        # ---------- people ----------
        author_info = parse_jsonld_authors_and_affiliations(jsonld)
        record["authors"] = author_info["authors"]
        record["author_affiliations"] = author_info["affiliations"]

        contributors: List[Dict[str, Any]] = []
        for idx, author in enumerate(record["authors"]):
            affiliation = record["author_affiliations"][idx] if idx < len(record["author_affiliations"]) else ""
            contributors.append(
                {
                    "name": author,
                    "role": "Creator",
                    "affiliation": affiliation,
                    "orcid": "",
                    "url": "",
                }
            )
        record["contributors"] = contributors

        # ---------- publisher / distributor / citation ----------
        record["publisher"] = first_non_empty(
            get_jsonld_value(catalog_obj, "name") if isinstance(catalog_obj, dict) else "",
            get_jsonld_value(publisher_obj, "name") if isinstance(publisher_obj, dict) else "",
            get_meta_first(meta_map, "DC.publisher"),
            REPOSITORY_NAME,
        )
        record["distributors"] = parse_distributors(meta_map, page)
        record["citation"] = first_non_empty(
            get_meta_first(meta_map, "DC.bibliographicCitation", "citation_reference", "citation"),
        )

        # ---------- descriptive ----------
        record["purpose"] = first_non_empty(
            collect_sections(page, "Purpose")[0] if collect_sections(page, "Purpose") else "",
        )
        record["study_design"] = first_non_empty(
            collect_sections(page, "Study Design")[0] if collect_sections(page, "Study Design") else "",
        )
        record["sample"] = first_non_empty(
            collect_sections(page, "Sample")[0] if collect_sections(page, "Sample") else "",
        )
        record["universe"] = first_non_empty(
            collect_sections(page, "Universe")[0] if collect_sections(page, "Universe") else "",
        )

        record["language"] = clean_list(ensure_list(get_jsonld_value(jsonld, "inLanguage")))
        record["keywords"] = clean_list(ensure_list(get_jsonld_value(jsonld, "keywords")))
        record["subjects"] = clean_list(collect_sections(page, "Subject Terms"))
        record["topics"] = clean_list(collect_sections(page, "Topic"))

        # ---------- relationships ----------
        record["relationships"]["series"] = clean_list(collect_sections(page, "Series"))
        record["relationships"]["collections"] = clean_list(collect_sections(page, "Collection"))
        record["relationships"]["related_datasets"] = clean_list(collect_sections(page, "Related Projects"))

        # ---------- funding ----------
        record["funding"] = parse_funding(jsonld)

        # ---------- time ----------
        record["time"]["time_method"] = clean_list(collect_sections(page, "Time Method"))
        record["time"]["collection_dates"] = clean_list(collect_sections(page, "Collection Dates"))
        record["time"]["temporal_coverage"] = clean_list(ensure_list(get_jsonld_value(jsonld, "temporalCoverage")))

        # ---------- coverage ----------
        geographic_coverage = clean_list(
            ensure_list(get_jsonld_value(jsonld, "spatialCoverage")) + collect_sections(page, "Geographic Coverage")
        )
        record["coverage"]["geographic_coverage"] = geographic_coverage
        record["coverage"]["spatial_coverage"] = geographic_coverage

        # ---------- methodology ----------
        analysis_units = clean_list(collect_sections(page, "Unit(s) of Observation"))
        kind_of_data = clean_list(collect_sections(page, "Data Type(s)") + collect_sections(page, "Kind of Data"))
        collection_mode = clean_list(collect_sections(page, "Mode of Data Collection"))
        sampling_procedure = clean_html_list(collect_sections(page, "Sampling"))

        record["methodology"]["analysis_units"] = analysis_units
        record["methodology"]["kind_of_data"] = kind_of_data
        record["methodology"]["collection_mode"] = collection_mode
        record["methodology"]["sampling_procedure"] = sampling_procedure
        record["unit_of_analysis"] = analysis_units[0] if analysis_units else ""

        # ---------- access ----------
        restrictions = first_non_empty(
            strip_html_fragment(get_jsonld_value(jsonld, "conditionsOfAccess")),
            collect_sections(page, "Restrictions")[0] if collect_sections(page, "Restrictions") else "",
        )

        record["access"]["license"] = first_non_empty(
            clean_text(get_jsonld_value(jsonld, "license")),
            get_meta_first(meta_map, "DC.license"),
        )
        record["access"]["license_url"] = first_non_empty(
            clean_text(get_jsonld_value(jsonld, "license")),
            get_meta_first(meta_map, "DC.license"),
        )
        record["access"]["terms_url"] = record["access"]["license_url"]
        record["access"]["restrictions"] = restrictions

        action_signals = page.get("action_signals", {})
        record["access"]["documentation_only_download"] = bool(action_signals.get("documentation_only_download"))
        record["access"]["access_restricted_data_button"] = bool(action_signals.get("access_restricted_data_button"))
        record["access"]["analyze_online"] = bool(action_signals.get("analyze_online"))

        # openICPSR projects are public-facing by default; retain conservative restriction flags
        record["access"]["restricted_data_types"]["idars"] = False
        record["access"]["restricted_data_types"]["useAgreement"] = False
        record["access"]["restricted_data_types"]["restricted"] = False
        record["access"]["restricted_data_types"]["vde"] = False
        record["access"]["restricted_data_types"]["enclave"] = False

        # ---------- notes ----------
        record["notes"]["collection_notes"] = clean_html_list(collect_sections(page, "Collection Notes"))
        record["notes"]["collection_changes"] = clean_html_list(collect_sections(page, "Collection Changes"))

        # ---------- variables / publications ----------
        record["variables"]["count"] = None
        record["publications"]["count"] = None
        record["publications"]["items"] = []

        # ---------- export ----------
        record["export_formats"] = page.get("export_formats") or []

        # ---------- source_specific ----------
        record["source_specific"]["raw_jsonld"] = jsonld
        record["source_specific"]["raw_meta_tags"] = meta_map
        record["source_specific"]["raw_page_blocks"] = {
            "main": {
                "sections": page.get("sections", {}),
            }
        }
        record["source_specific"]["raw_export_signals"] = [
            {
                "page_name": "main",
                "export_formats": page.get("export_formats", []),
            }
        ]
        record["source_specific"]["raw_action_signals"] = [
            {
                "page_name": "main",
                "signals": action_signals,
            }
        ]
        record["source_specific"]["extra"] = {
            "js_assignments_keys": sorted(list(page.get("variables_map", {}).keys())),
        }

    # -----------------------------------------------------
    # FILES
    # -----------------------------------------------------
    def _build_files_section(self, page: PagePackage, record: MetadataRecord) -> Dict[str, Any]:
        """Build the full files section from HTML-visible file links."""
        soup = BeautifulSoup(page["html"], "html.parser")
        raw_files = unique_dicts(extract_openicpsr_file_rows(soup, page["final_url"]))
        unique_files = dedupe_file_entries(raw_files)

        documentation_files = [f for f in unique_files if classify_file_group(f) == "documentation"]
        data_files = [f for f in unique_files if classify_file_group(f) == "data"]
        setup_files = [f for f in unique_files if classify_file_group(f) == "setup"]
        other_files = [f for f in unique_files if classify_file_group(f) == "other"]
        downloadable_files = [f for f in unique_files if f.get("downloadable") is True]

        self.logger.debug(
            "Built files section | raw_files=%s | unique_files=%s",
            len(raw_files),
            len(unique_files),
        )

        record["source_specific"]["raw_api_payloads"]["files_api"] = {
            "url": "",
            "status_code": None,
            "content_type": "",
            "note": "openICPSR extractor uses HTML-visible file links; no files API used.",
        }

        return {
            "endpoint": "",
            "status_code": None,
            "content_type": page.get("content_type", ""),
            "source": "openicpsr_html",
            "counting_basis": "unique_resources",
            "filesets": [],
            "raw_files": raw_files,
            "unique_files": unique_files,
            "downloadable_files": downloadable_files,
            "documentation_files": documentation_files,
            "data_files": data_files,
            "setup_files": setup_files,
            "other_files": other_files,
            "summary": summarize_files(raw_files, unique_files),
        }

    # -----------------------------------------------------
    # FINALIZATION
    # -----------------------------------------------------
    def _compute_access_flags(self, record: MetadataRecord) -> None:
        """Compute derived access flags from file-level and page-level signals."""
        unique_files = record["files"].get("unique_files", [])

        record["access"]["has_downloadable_files"] = record["files"]["summary"]["unique_downloadable_files"] > 0
        record["access"]["has_restricted_files"] = (
            record["files"]["summary"]["unique_restricted_or_nonpublic_files"] > 0
            or record["access"]["restricted_data_types"]["restricted"]
            or record["access"]["access_restricted_data_button"]
        )

        if any(f.get("public") is True for f in unique_files):
            record["access"]["has_public_files"] = True
        elif any(f.get("listed_on_public_page") for f in unique_files):
            record["access"]["has_public_files"] = None
        else:
            record["access"]["has_public_files"] = None

        if unique_files and all(f.get("public") is True for f in unique_files):
            record["access"]["open_access"] = True
        elif any(f.get("public") is False for f in unique_files):
            record["access"]["open_access"] = False

        if record["access"]["has_restricted_files"]:
            record["access"]["authentication_required"] = True

    def _finalize_record(self, record: MetadataRecord, page: PagePackage) -> None:
        """
        Run final cleanup, shared normalization, and shared validation.

        This is the lock point:
        - source-specific extraction ends here
        - shared schema normalization starts here
        - final validation guarantees consistent output shape
        """
        if not record["access"]["license"]:
            record["access"]["license"] = record["access"]["license_url"]
        if not record["access"]["terms_url"]:
            record["access"]["terms_url"] = record["access"]["license_url"]

        record["provenance"]["source_urls_visited"] = clean_list(record["provenance"]["source_urls_visited"])
        record["diagnostics"]["metadata_sources"] = clean_list(record["diagnostics"]["metadata_sources"])

        if not record["authors"]:
            record["diagnostics"]["field_warnings"].append("No authors were extracted.")
        if not record["publisher"]:
            record["diagnostics"]["field_warnings"].append("No publisher was extracted.")
        if not record["export_formats"]:
            record["diagnostics"]["field_warnings"].append(
                "No export formats were detected on the page; review export link parsing for this project."
            )
        if not record["files"]["unique_files"]:
            record["diagnostics"]["field_warnings"].append(
                "No files were detected on the page; review the openICPSR file-row parser for this project."
            )

        normalized = normalize_metadata_record(record)
        record.clear()
        record.update(normalized)

        validation_result = annotate_record_with_validation(record)

        self.logger.debug(
            "Validation result | valid=%s | missing_required=%s | warnings=%s | errors=%s",
            validation_result.valid,
            validation_result.missing_required_fields,
            len(validation_result.warnings),
            len(validation_result.errors),
        )

        if not validation_result.valid:
            for error in validation_result.errors:
                self.logger.error("Validation error: %s", error)
            raise ExtractionError(
                "Unified metadata validation failed. "
                f"Errors: {validation_result.errors}"
            )

    def _save_metadata_json(self, record: MetadataRecord) -> Optional[str]:
        """Persist the metadata record as JSON when saving is enabled."""
        if not self.config.save_output_json:
            self.logger.info("save_output_json=False; skipping file write")
            return None

        output_dir = Path(self.config.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        file_stem = safe_filename(record.get("doi") or record.get("study_id") or record.get("title") or "metadata")
        output_path = output_dir / f"{file_stem}.json"

        with output_path.open("w", encoding="utf-8") as file_handle:
            json.dump(record, file_handle, indent=2, ensure_ascii=False)

        return str(output_path)


# =========================================================
# PUBLIC FUNCTION
# =========================================================
def extract_openicpsr_metadata(
    source_url: str,
    *,
    save_output_json: bool = DEFAULT_SAVE_OUTPUT_JSON,
    output_dir: str = DEFAULT_OUTPUT_DIR,
    headless: bool = DEFAULT_HEADLESS,
    wait_ms: int = DEFAULT_WAIT_MS,
    request_timeout: int = DEFAULT_REQUEST_TIMEOUT,
    playwright_timeout_ms: int = DEFAULT_PLAYWRIGHT_TIMEOUT_MS,
    user_agent: str = DEFAULT_USER_AGENT,
) -> MetadataRecord:
    """Public entry point for openICPSR extraction."""
    config = ExtractorConfig(
        source_url=source_url,
        save_output_json=save_output_json,
        output_dir=output_dir,
        headless=headless,
        wait_ms=wait_ms,
        request_timeout=request_timeout,
        playwright_timeout_ms=playwright_timeout_ms,
        user_agent=user_agent,
    )

    with OpenICPSRExtractor(config=config) as extractor:
        return extractor.extract()


# =========================================================
# EXAMPLE RUN
# =========================================================
if __name__ == "__main__":
    setup_logging()

    try:
        metadata = extract_openicpsr_metadata(DEFAULT_SOURCE_URL)
        logging.getLogger("OpenICPSRExtractor").info(
            "Summary | project_id=%s | title=%s | doi=%s | raw_files=%s | unique_files=%s",
            metadata["study_id"],
            metadata["title"],
            metadata["doi"],
            metadata["files"]["summary"]["raw_entry_count"],
            metadata["files"]["summary"]["unique_file_count"],
        )
    except Exception:
        logging.getLogger("OpenICPSRExtractor").exception("openICPSR metadata extraction failed")
        raise