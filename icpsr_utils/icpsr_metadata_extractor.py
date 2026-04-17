from __future__ import annotations

import json
import logging
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, NavigableString, Tag
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from QDArchive.schemas.normalizers import (
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
from QDArchive.schemas.unified_metadata import new_metadata_record
from QDArchive.schemas.validators import annotate_record_with_validation


PARSER_VERSION = "2.3.0-datadoc-render-fallback"

DEFAULT_SOURCE_URL = "https://www.icpsr.umich.edu/web/ICPSR/studies/38533"
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

REPOSITORY_NAME = "ICPSR"
REPOSITORY_URL = "https://www.icpsr.umich.edu"

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
    "zip": "application/zip",
    "gz": "application/gzip",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "sas": "application/octet-stream",
    "spss": "application/octet-stream",
    "stata": "application/octet-stream",
    "ascii": "text/plain",
    "delimited": "text/plain",
    "r": "application/octet-stream",
    "qda": "application/zip",
    "sps": "text/plain",
    "do": "text/plain",
}

PROJECT_FORMAT_TO_EXTENSION = {
    "SAS": "zip",
    "SPSS": "zip",
    "Stata": "zip",
    "ASCII": "zip",
    "R": "zip",
    "Delimited": "zip",
    "Qualitative Data": "zip",
    "Documentation Only": "zip",
}

DATA_LABELS = {
    "stata",
    "spss",
    "sas",
    "r",
    "ascii",
    "delimited",
    "qualitative data",
    "documentation only",
}

DOC_PREFIXES = (
    "documentation",
    "questionnaire",
    "frequencies",
    "manual",
    "flashcards",
    "description",
    "readme",
    "reference",
    "supplements",
    "flowchart",
    "report",
)

SETUP_KEYWORDS = ("setup",)

BRACKET_EXTENSION_MAP = {
    "pdf": "pdf",
    "excel 2007 spreadsheet": "xlsx",
    "excel spreadsheet": "xlsx",
    "excel": "xlsx",
    "csv": "csv",
    "text": "txt",
    "txt": "txt",
    "zip": "zip",
}

METRICS_FILE_NAMES = {"metrics", "usage_report", "usage-report"}

DATADOC_INVENTORY_MARKERS = (
    'id="data-doc"',
    "id='data-doc'",
    "download-link-menu",
    "Documentation [PDF]",
    "Questionnaire [PDF]",
)

logger = logging.getLogger(__name__)
MetadataRecord = Dict[str, Any]
PagePackage = Dict[str, Any]


class ExtractionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExtractorConfig:
    source_url: str = DEFAULT_SOURCE_URL
    save_output_json: bool = DEFAULT_SAVE_OUTPUT_JSON
    output_dir: str = DEFAULT_OUTPUT_DIR
    headless: bool = DEFAULT_HEADLESS
    wait_ms: int = DEFAULT_WAIT_MS
    request_timeout: int = DEFAULT_REQUEST_TIMEOUT
    playwright_timeout_ms: int = DEFAULT_PLAYWRIGHT_TIMEOUT_MS
    user_agent: str = DEFAULT_USER_AGENT


def setup_logging(level: int = DEFAULT_LOG_LEVEL) -> None:
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        logging.basicConfig(level=level, format=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
    else:
        root_logger.setLevel(level)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def html_to_visible_text(html_content: str) -> str:
    soup = BeautifulSoup(html_content, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return clean_text(soup.get_text("\n", strip=True))


def html_looks_usable(html_content: str) -> bool:
    if not html_content:
        return False
    lower = html_content.lower()
    if "<html" not in lower or "<title" not in lower:
        return False
    return len(html_content) >= 1500


def datadoc_html_has_inventory(html_content: str, visible_text: str = "") -> bool:
    lowered_html = (html_content or "").lower()
    lowered_text = (visible_text or "").lower()

    for marker in DATADOC_INVENTORY_MARKERS:
        if marker.lower() in lowered_html or marker.lower() in lowered_text:
            return True

    return False


def normalize_date_like(value: Any) -> str:
    return clean_text(value)


def infer_study_id(url: str) -> str:
    match = re.search(r"/studies/(\d+)", url)
    return match.group(1) if match else ""


def build_url_map(source_url: str) -> Dict[str, str]:
    study_id = infer_study_id(source_url)
    if not study_id:
        raise ValueError(f"Could not infer ICPSR study ID from URL: {source_url}")

    base = f"{REPOSITORY_URL}/web/ICPSR/studies/{study_id}"
    return {
        "main": base,
        "summary": f"{base}/summary",
        "datadocumentation": f"{base}/datadocumentation",
        "variables": f"{base}/variables",
        "publications": f"{base}/publications",
        "export": f"{base}/export",
        "terms": f"{base}/terms",
    }


def infer_mime_type(file_name: str, file_format: str = "") -> str:
    normalized_format = clean_text(file_format).lower()
    if normalized_format in MIME_BY_EXTENSION:
        return MIME_BY_EXTENSION[normalized_format]

    _, extension = parse_href_filename_and_extension(file_name)
    return MIME_BY_EXTENSION.get(extension, "")


def detect_export_formats_in_text(text: Any) -> List[str]:
    lowered = clean_text(text).lower()
    if not lowered:
        return []

    found: List[str] = []
    for label, patterns in EXPORT_FORMAT_PATTERNS.items():
        if any(re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in patterns):
            found.append(label)
    return found


def read_balanced_expression(text: str, start_idx: int) -> Tuple[str, int]:
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
    output: Dict[str, Any] = {}

    for match in re.finditer(r"(?:variables\.|var\s+)([A-Za-z0-9_]+)\s*=\s*", script_text):
        key = match.group(1)
        raw_expr, _ = read_balanced_expression(script_text, match.end())
        parsed = maybe_json_load(raw_expr.strip())
        output[key] = parsed if parsed is not None else raw_expr.strip()

    return output


def find_jsonld_dataset(soup: BeautifulSoup) -> Optional[Dict[str, Any]]:
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
    text = clean_text(text)
    text = re.sub(r"View help for.*$", "", text).strip()
    return text


def is_noise_value(text: str) -> bool:
    lowered = clean_text(text).lower()
    return not lowered or lowered in {"hide", "beta"} or lowered.startswith("view help for")


def extract_metadata_sections(soup: BeautifulSoup) -> Dict[str, List[str]]:
    sections: Dict[str, List[str]] = {}

    for heading in soup.select("h3.metadata-field"):
        label = normalize_label(" ".join(heading.stripped_strings))
        if not label:
            continue

        values: List[str] = []
        for sibling in heading.next_siblings:
            if isinstance(sibling, Tag) and sibling.name == "h3" and "metadata-field" in (sibling.get("class") or []):
                break

            if isinstance(sibling, NavigableString):
                text = clean_text(str(sibling))
                if text and not is_noise_value(text):
                    values.append(text)
            elif isinstance(sibling, Tag):
                text = clean_text(sibling.get_text(" ", strip=True))
                if text and not is_noise_value(text):
                    values.append(text)

        values = clean_list(values)
        if values:
            sections[label] = values

    return sections


def extract_tab_urls(soup: BeautifulSoup, base_url: str) -> Dict[str, str]:
    tabs: Dict[str, str] = {}
    known_ids = {
        "summaryLink": "summary",
        "datadocumentationLink": "datadocumentation",
        "variablesLink": "variables",
        "publicationsLink": "publications",
        "exportLink": "export",
    }

    for anchor in soup.find_all("a", id=True, href=True):
        if anchor["id"] in known_ids:
            tabs[known_ids[anchor["id"]]] = urljoin(base_url, anchor["href"])

    return tabs


def extract_action_signals(soup: BeautifulSoup, visible_text: str) -> Dict[str, Any]:
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
        elif re.search(r"\brestricted data\b", lowered) and any(
            token in lowered for token in ["button", "btn", "link", "href", "/restricted", "access"]
        ):
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


def extract_search_response_dicts(variables_map: Dict[str, Any]) -> List[Dict[str, Any]]:
    responses: List[Dict[str, Any]] = []
    for value in variables_map.values():
        if isinstance(value, dict) and isinstance(value.get("response"), dict):
            responses.append(value)
    return responses


def extract_variable_count_from_payloads(variables_map: Dict[str, Any], visible_text: str) -> Optional[int]:
    for payload in extract_search_response_dicts(variables_map):
        response = payload.get("response", {})
        if isinstance(response.get("numFound"), int):
            return response["numFound"]

    match = re.search(r"(\d+)\s+to\s+(\d+)\s+of\s+(\d+)", visible_text)
    if match:
        return int(match.group(3))

    if "no search results found" in visible_text.lower():
        return 0

    return None


def normalize_publication_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    item = {
        "title": clean_text(doc.get("TITLE") or doc.get("title")),
        "doi": canonicalize_doi(doc.get("DOI") or doc.get("doi")),
        "journal": clean_text(doc.get("JOURNAL") or doc.get("journal")),
        "year": clean_text(doc.get("YEAR_PUB") or doc.get("year")),
        "url": clean_text(doc.get("URL") or doc.get("URL_ABS") or doc.get("url")),
        "citation": clean_text(doc.get("citation")),
        "authors": clean_list(doc.get("AUTHORS_SPLIT") or doc.get("authors") or []),
    }
    return {k: v for k, v in item.items() if v not in ("", [], None)}


def extract_publications_from_payloads(variables_map: Dict[str, Any], visible_text: str) -> Dict[str, Any]:
    count: Optional[int] = None
    items: List[Dict[str, Any]] = []

    for payload in extract_search_response_dicts(variables_map):
        response = payload.get("response", {})
        docs = response.get("docs", [])
        if not isinstance(docs, list):
            continue

        normalized_docs = [normalize_publication_doc(doc) for doc in docs if isinstance(doc, dict)]
        normalized_docs = [doc for doc in normalized_docs if doc.get("title")]
        if normalized_docs:
            items.extend(normalized_docs)
            if isinstance(response.get("numFound"), int):
                count = max(count or 0, response["numFound"])

    items = unique_dicts(items)

    if count is None and items:
        count = len(items)

    if count is None and re.search(r"\b0\s+publications?\b", visible_text.lower()):
        count = 0

    return {"count": count, "items": items}


def extract_export_formats_from_html(soup: BeautifulSoup, visible_text: str) -> List[str]:
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


def get_jsonld_value(dataset: Optional[Dict[str, Any]], key: str) -> Any:
    if not isinstance(dataset, dict):
        return None
    return dataset.get(key)


def get_meta_first(meta_map: Dict[str, List[str]], *keys: str) -> str:
    for key in keys:
        values = meta_map.get(key, [])
        if values:
            return values[0]
    return ""


def collect_sections_across_pages(pages: Dict[str, PagePackage], label: str) -> List[str]:
    values: List[str] = []
    for page in pages.values():
        for value in page.get("sections", {}).get(label, []):
            if value not in values:
                values.append(value)
    return clean_list(values)


def extract_inline_metadata_bundle(pages: Dict[str, PagePackage]) -> Dict[str, Any]:
    bundle: Dict[str, Any] = {}

    for page in pages.values():
        variables_map = page.get("variables_map", {})

        metadata_obj = variables_map.get("metadata")
        if isinstance(metadata_obj, dict):
            for key, value in metadata_obj.items():
                if key not in bundle and value not in ("", None, {}, []):
                    bundle[key] = value

        for key in ["studyId", "versionLabel", "restrictedDataTypes", "title", "versionNumber"]:
            value = variables_map.get(key)
            if value not in ("", None, {}, []):
                bundle[key] = value

    return bundle


def parse_jsonld_authors_and_affiliations(jsonld: Optional[Dict[str, Any]]) -> Dict[str, List[str]]:
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
    if not isinstance(jsonld, dict):
        return ""

    identifier = jsonld.get("identifier")
    if isinstance(identifier, dict):
        return canonicalize_doi(
            first_non_empty(identifier.get("url"), identifier.get("@id"), identifier.get("value"))
        )
    return ""


def parse_funding(jsonld: Optional[Dict[str, Any]], inline_metadata: Dict[str, Any]) -> List[Dict[str, str]]:
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

    for item in ensure_list(inline_metadata.get("fundingSources")):
        if not isinstance(item, dict):
            continue

        funder_name = clean_text(item.get("funderName"))
        identifier = clean_text(item.get("grantNo"))
        key = (funder_name, identifier)

        entry = merged.get(key, {})
        if funder_name:
            entry["funder_name"] = funder_name
        if identifier:
            entry["identifier"] = identifier
            entry.setdefault("grant_number", identifier)

        display = clean_text(item.get("display"))
        if display:
            entry["display"] = display

        merged[key] = entry

    return [value for value in merged.values() if value]


def parse_distributors(meta_map: Dict[str, List[str]], inline_metadata: Dict[str, Any], pages: Dict[str, PagePackage]) -> List[str]:
    distributors = clean_list(
        ensure_list(inline_metadata.get("distributor")) + collect_sections_across_pages(pages, "Distributor(s)")
    )
    distributors = [clean_text(value) for value in distributors if clean_text(value)]

    citation_candidates: List[str] = []
    for key in ["DC.bibliographicCitation", "citation_reference", "citation"]:
        citation_candidates.extend(meta_map.get(key, []))

    for citation in citation_candidates:
        for match in re.finditer(r"([^.;]+?)\s*\[distributor\]", citation, flags=re.IGNORECASE):
            candidate = clean_text(match.group(1))
            if candidate and candidate not in distributors:
                distributors.append(candidate)

    return clean_list(distributors)


def normalize_affiliation_value(value: Any) -> str:
    if isinstance(value, list):
        return clean_text("; ".join(clean_text(v) for v in value if clean_text(v)))
    return clean_text(value)


def parse_bracketed_visible_label(label: str) -> Tuple[str, str, str]:
    text = clean_text(label)
    match = re.match(r"^\s*(.*?)\s*\[([^\]]+)\]\s*(.*?)\s*$", text)
    if match:
        prefix = clean_text(match.group(1))
        bracket = clean_text(match.group(2))
        suffix = clean_text(match.group(3))
        return prefix, bracket, suffix
    return text, "", ""


def infer_visible_file_format(label: str) -> str:
    label_clean = clean_text(label)
    lowered = label_clean.lower()

    _, bracket, _ = parse_bracketed_visible_label(label_clean)
    bracket_lower = bracket.lower()

    if bracket_lower in BRACKET_EXTENSION_MAP:
        return BRACKET_EXTENSION_MAP[bracket_lower]

    if lowered == "stata":
        return "stata"
    if lowered == "spss":
        return "spss"
    if lowered == "sas":
        return "sas"
    if lowered == "r":
        return "r"
    if lowered == "ascii":
        return "ascii"
    if lowered == "delimited":
        return "delimited"
    if lowered == "qualitative data":
        return "qda"
    if lowered == "documentation only":
        return "zip"

    if "stata setup" in lowered:
        return "do"
    if "spss setup" in lowered:
        return "sps"
    if "sas setup" in lowered:
        return "sas"

    return ""


def parse_visible_resource_label(label: str) -> Dict[str, str]:
    raw_label = clean_text(label)
    prefix, bracket, suffix = parse_bracketed_visible_label(raw_label)

    content_label = prefix if bracket else raw_label
    display_name = suffix if bracket else raw_label
    file_extension = infer_visible_file_format(raw_label)

    content_lower = clean_text(content_label).lower()
    display_lower = clean_text(display_name).lower()

    if display_lower in METRICS_FILE_NAMES:
        file_category = "other"
    elif any(token in display_lower for token in SETUP_KEYWORDS) or any(token in content_lower for token in SETUP_KEYWORDS):
        file_category = "setup"
    elif content_lower in DATA_LABELS:
        file_category = "data"
    elif content_lower.startswith(DOC_PREFIXES) or file_extension == "pdf":
        file_category = "documentation"
    elif file_extension in {"xlsx", "xls", "csv", "txt"} and content_lower.startswith(DOC_PREFIXES):
        file_category = "documentation"
    else:
        file_category = "other"

    if display_name:
        base_name = safe_filename(display_name)
    else:
        base_name = safe_filename(raw_label)

    file_name = base_name
    if file_extension:
        if not file_name.lower().endswith(f".{file_extension}"):
            file_name = safe_filename(f"{base_name}.{file_extension}")

    identity_key = f"{display_lower}|{file_extension}|{content_lower}"

    return {
        "raw_label": raw_label,
        "content_label": content_label,
        "display_name": display_name,
        "file_extension": file_extension,
        "file_format": file_extension,
        "file_category": file_category,
        "file_name": file_name,
        "identity_key": identity_key,
    }


def classify_visible_file_group(label: str) -> str:
    parsed = parse_visible_resource_label(label)
    return parsed["file_category"]


def build_visible_file_name(label: str) -> str:
    parsed = parse_visible_resource_label(label)
    return parsed["file_name"]


def parse_dataset_row_name(value: str) -> Tuple[Optional[int], str]:
    text = clean_text(value)
    match = re.match(r"DS\s*(\d+)\s+(.*)", text, flags=re.IGNORECASE)
    if match:
        return int(match.group(1)), clean_text(match.group(2))
    return None, text


def extract_dataset_number_from_row(row: Tag) -> Optional[int]:
    for class_name in row.get("class", []):
        match = re.match(r"dataset-(\d+)$", clean_text(class_name))
        if match:
            return int(match.group(1))
    return None


def extract_dataset_identity(row: Tag, cell: Tag) -> Tuple[Optional[int], str, str]:
    dataset_number = extract_dataset_number_from_row(row)

    text = clean_text(cell.get_text(" ", strip=True))
    if dataset_number is None:
        dataset_number, dataset_title = parse_dataset_row_name(text)
        dataset_identifier = f"DS{dataset_number}" if dataset_number is not None else ""
        return dataset_number, dataset_identifier, dataset_title

    prefix_pattern = rf"^DS\s*{dataset_number}\s*"
    dataset_title = re.sub(prefix_pattern, "", text, count=1, flags=re.IGNORECASE).strip()
    dataset_identifier = f"DS{dataset_number}"
    return dataset_number, dataset_identifier, dataset_title


def extract_dropdown_labels_from_cell(cell: Tag) -> List[str]:
    labels: List[str] = []

    for anchor in cell.select("ul.dropdown-menu a, a.dropdown-item"):
        label = clean_text(anchor.get_text(" ", strip=True))
        if label and label.lower() not in {"preview", "download"} and label not in labels:
            labels.append(label)

    if labels:
        return labels

    for li in cell.select("ul.dropdown-menu li"):
        text = clean_text(li.get_text(" ", strip=True))
        if text and text.lower() not in {"preview", "download"} and text not in labels:
            labels.append(text)

    return labels


def extract_project_download_options(
    jsonld: Optional[Dict[str, Any]],
    datadoc_soup: Optional[BeautifulSoup],
    study_id: str,
) -> List[Dict[str, Any]]:
    options: List[Dict[str, Any]] = []

    if isinstance(jsonld, dict):
        for item in ensure_list(jsonld.get("distribution")):
            if not isinstance(item, dict):
                continue

            label = clean_text(item.get("fileFormat"))
            url = clean_text(item.get("contentURL"))
            encoding = clean_text(item.get("encodingFormat"))
            if not label:
                continue

            options.append(
                {
                    "label": label,
                    "download_url": url,
                    "encoding_format": encoding,
                    "file_type": PROJECT_FORMAT_TO_EXTENSION.get(label, "zip"),
                    "file_name": safe_filename(f"{study_id}_{label}.zip"),
                    "source": "page_jsonld",
                }
            )

    if datadoc_soup is not None:
        quick_download_labels: List[str] = []
        quick_download_button = datadoc_soup.find("div", {"id": "quickDownload"})
        if quick_download_button:
            for anchor in quick_download_button.find_all("a"):
                label = clean_text(anchor.get_text(" ", strip=True))
                if label and label not in quick_download_labels:
                    quick_download_labels.append(label)

        for label in quick_download_labels:
            if not any(opt["label"] == label for opt in options):
                options.append(
                    {
                        "label": label,
                        "download_url": "",
                        "encoding_format": "",
                        "file_type": PROJECT_FORMAT_TO_EXTENSION.get(label, "zip"),
                        "file_name": safe_filename(f"{study_id}_{label}.zip"),
                        "source": "page_menu_only",
                    }
                )

    return options


def dedupe_file_key(file_entry: Dict[str, Any]) -> str:
    return first_non_empty(
        clean_text(file_entry.get("resource_uuid")),
        clean_text(file_entry.get("file_id")),
        clean_text(file_entry.get("uri")),
        f"{clean_text(file_entry.get('dataset_identifier'))}|{clean_text(file_entry.get('file_name'))}|{clean_text(file_entry.get('file_content'))}",
    )


def dedupe_file_entries(file_entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
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


def extract_datadoc_table_files(datadoc_soup: Optional[BeautifulSoup], study_id: str) -> Dict[str, Any]:
    empty_result = {
        "filesets": [],
        "raw_files": [],
        "unique_files": [],
        "downloadable_files": [],
        "documentation_files": [],
        "data_files": [],
        "setup_files": [],
        "other_files": [],
    }

    if datadoc_soup is None:
        return empty_result

    table = datadoc_soup.find("table", {"id": "data-doc"})
    if table is None:
        return empty_result

    raw_files: List[Dict[str, Any]] = []
    filesets: List[Dict[str, Any]] = []

    rows = table.select("tbody > tr")
    if not rows:
        rows = table.find_all("tr")

    for row_idx, row in enumerate(rows, start=1):
        tds = row.find_all("td", recursive=False)
        if len(tds) < 4:
            continue

        dataset_number, dataset_identifier, dataset_title = extract_dataset_identity(row, tds[0])
        dataset_size = clean_text(tds[1].get_text(" ", strip=True))

        preview_labels = extract_dropdown_labels_from_cell(tds[2])
        download_labels = extract_dropdown_labels_from_cell(tds[3])

        merged_items: Dict[str, Dict[str, Any]] = {}
        item_order: List[str] = []

        for mode, labels in [("preview", preview_labels), ("download", download_labels)]:
            for label in labels:
                parsed = parse_visible_resource_label(label)
                identity_key = parsed["identity_key"]

                if identity_key not in merged_items:
                    merged_items[identity_key] = {
                        "raw_label": parsed["raw_label"],
                        "content_label": parsed["content_label"],
                        "display_name": parsed["display_name"],
                        "file_extension": parsed["file_extension"],
                        "file_format": parsed["file_format"],
                        "file_category": parsed["file_category"],
                        "file_name": parsed["file_name"],
                        "previewable": False,
                        "downloadable": False,
                    }
                    item_order.append(identity_key)

                if mode == "preview":
                    merged_items[identity_key]["previewable"] = True
                if mode == "download":
                    merged_items[identity_key]["downloadable"] = True

        fileset_files: List[Dict[str, Any]] = []

        for item_idx, identity_key in enumerate(item_order, start=1):
            item = merged_items[identity_key]
            file_entry = {
                "file_id": f"page-{study_id}-{dataset_identifier or 'dataset'}-{row_idx}-{item_idx}-{safe_filename(item['display_name'] or item['raw_label'])}",
                "dataset_number": dataset_number,
                "dataset_identifier": dataset_identifier,
                "title": item["display_name"] or item["raw_label"],
                "file_name": item["file_name"],
                "file_stem": Path(item["file_name"]).stem if item["file_name"] else "",
                "file_format": item["file_format"],
                "file_extension": item["file_extension"],
                "mime_type": infer_mime_type(item["file_name"], item["file_format"]),
                "file_content": item["content_label"] or item["file_category"],
                "file_category": item["file_category"],
                "file_description": item["raw_label"],
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
                "rank": item_idx,
                "public": True,
                "listed_on_public_page": True,
                "downloadable": item["downloadable"],
                "previewable": item["previewable"],
                "deliver_download": item["downloadable"],
                "include_in_bundle": [],
                "required_for_bundle": "",
                "dataset_format_flags": [],
                "terms_of_use": [],
                "resource_uuid": "",
                "uri": "",
                "download_url": "",
                "api_url": "",
                "parent_path": dataset_identifier,
                "folder_path": dataset_title,
                "last_modified": "",
                "display_size": "",
                "language": "",
                "source": "icpsr_page_visible_dropdown",
                "raw_occurrence_count": 1,
            }

            raw_files.append(file_entry)
            fileset_files.append(file_entry)

        filesets.append(
            {
                "dataset_number": dataset_number,
                "identifier": dataset_identifier,
                "title": dataset_title,
                "description": "",
                "display_size": dataset_size,
                "file_count": len(fileset_files),
                "bundles": {},
                "sda_components": None,
                "preview_items": preview_labels,
                "download_items": download_labels,
                "files": fileset_files,
            }
        )

    unique_files = dedupe_file_entries(raw_files)
    documentation_files = [f for f in unique_files if f.get("file_category") == "documentation"]
    data_files = [f for f in unique_files if f.get("file_category") == "data"]
    setup_files = [f for f in unique_files if f.get("file_category") == "setup"]
    other_files = [f for f in unique_files if f.get("file_category") == "other"]
    downloadable_files = [f for f in unique_files if f.get("downloadable") is True]

    return {
        "filesets": filesets,
        "raw_files": raw_files,
        "unique_files": unique_files,
        "downloadable_files": downloadable_files,
        "documentation_files": documentation_files,
        "data_files": data_files,
        "setup_files": setup_files,
        "other_files": other_files,
    }


def summarize_visible_files(
    raw_files: List[Dict[str, Any]],
    unique_files: List[Dict[str, Any]],
    project_download_options: List[Dict[str, Any]],
) -> Dict[str, Any]:
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

    documentation_files = [f for f in unique_files if f.get("file_category") == "documentation"]
    data_files = [f for f in unique_files if f.get("file_category") == "data"]
    setup_files = [f for f in unique_files if f.get("file_category") == "setup"]
    other_files = [f for f in unique_files if f.get("file_category") == "other"]
    downloadable_files = [f for f in unique_files if f.get("downloadable") is True]
    public_unique_files = [f for f in unique_files if f.get("public") is True]

    return {
        "raw_entry_count": len(raw_files),
        "unique_file_count": len(unique_files),
        "raw_public_entries": sum(1 for f in raw_files if f.get("public") is True),
        "unique_public_files": len(public_unique_files),
        "raw_restricted_or_nonpublic_entries": sum(1 for f in raw_files if f.get("public") is False),
        "unique_restricted_or_nonpublic_files": sum(1 for f in unique_files if f.get("public") is False),
        "raw_downloadable_entries": sum(1 for f in raw_files if f.get("downloadable") is True),
        "unique_downloadable_files": len(downloadable_files),
        "downloadable_public_files": sum(1 for f in downloadable_files if f.get("public") is True),
        "downloadable_documentation_files": sum(1 for f in documentation_files if f.get("downloadable") is True),
        "downloadable_data_files": sum(1 for f in data_files if f.get("downloadable") is True),
        "documentation_file_count": len(documentation_files),
        "data_file_count": len(data_files),
        "setup_file_count": len(setup_files),
        "other_file_count": len(other_files),
        "raw_formats": raw_formats,
        "unique_formats": unique_formats,
        "raw_file_contents": raw_contents,
        "unique_file_contents": unique_contents,
        "project_download_option_count": len(project_download_options),
    }


class ICPSRExtractor:
    def __init__(self, config: ExtractorConfig, logger_: Optional[logging.Logger] = None) -> None:
        self.config = config
        self.logger = logger_ or logging.getLogger(self.__class__.__name__)
        self.study_id = infer_study_id(config.source_url)

        if not self.study_id:
            raise ValueError(f"Could not infer ICPSR study ID from URL: {config.source_url}")

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": self.config.user_agent,
                "Accept-Language": "en-US,en;q=0.9",
            }
        )

    def __enter__(self) -> "ICPSRExtractor":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            self.logger.debug("Failed to close HTTP session", exc_info=True)

    def extract(self) -> MetadataRecord:
        self.logger.info(
            "Starting ICPSR extraction | source_url=%s | study_id=%s",
            self.config.source_url,
            self.study_id,
        )

        record = self._initialize_record()
        url_map = build_url_map(self.config.source_url)

        self.logger.info("Resolved %d study endpoints for study_id=%s", len(url_map), self.study_id)
        pages = self._fetch_pages(url_map, record)
        if "main" not in pages:
            raise ExtractionError(f"Could not fetch main study page for {self.config.source_url}")

        self._populate_record_from_pages(record, pages, url_map)
        record["files"] = self._build_files_section_from_pages(record["study_id"], record, pages)
        self._compute_access_flags(record)
        self._finalize_record(record, pages, url_map)

        output_path = self._save_metadata_json(record)
        if output_path:
            self.logger.info("Saved metadata JSON to %s", output_path)

        self.logger.info(
            "Finished ICPSR extraction | study_id=%s | raw_files=%s | unique_files=%s | warnings=%s",
            record["study_id"],
            record["files"]["summary"]["raw_entry_count"],
            record["files"]["summary"]["unique_file_count"],
            len(record["diagnostics"]["field_warnings"]) + len(record["diagnostics"]["fetch_warnings"]),
        )
        return record

    def _initialize_record(self) -> MetadataRecord:
        record = new_metadata_record(self.config.source_url)

        record["generated_at_utc"] = now_utc()
        record["page_type"] = "icpsr"
        record["repository"] = REPOSITORY_NAME
        record["repository_url"] = REPOSITORY_URL
        record["study_id"] = self.study_id

        record["provenance"]["repository"] = REPOSITORY_NAME
        record["provenance"]["repository_url"] = REPOSITORY_URL
        record["provenance"]["page_type"] = "icpsr"
        record["provenance"]["parser_version"] = PARSER_VERSION
        record["provenance"]["schema_version"] = record["schema_version"]

        record["identifiers"]["study_id"] = self.study_id
        record["identifiers"]["source_record_id"] = self.study_id
        record["identifiers"]["other_identifiers"] = [
            {
                "type": "ICPSR study_id",
                "value": self.study_id,
            }
        ]

        return record

    def _fetch_pages(self, url_map: Dict[str, str], record: MetadataRecord) -> Dict[str, PagePackage]:
        pages: Dict[str, PagePackage] = {}

        for page_name, page_url in url_map.items():
            try:
                self.logger.info("Fetching page '%s' | %s", page_name, page_url)
                packaged = self._package_page(page_name, page_url)
                pages[page_name] = packaged

                fetch_method = packaged.get("fetch_method")
                if fetch_method and fetch_method not in record["diagnostics"]["metadata_sources"]:
                    record["diagnostics"]["metadata_sources"].append(fetch_method)

                record["provenance"]["source_urls_visited"].append(packaged["final_url"])
                record["provenance"]["html_pages_fetched"].append(
                    {
                        "page_name": page_name,
                        "requested_url": page_url,
                        "final_url": packaged["final_url"],
                        "fetch_method": fetch_method,
                        "status_code": packaged.get("status_code"),
                    }
                )
                if fetch_method and fetch_method not in record["provenance"]["fetch_methods"]:
                    record["provenance"]["fetch_methods"].append(fetch_method)

                self.logger.info(
                    "Fetched page '%s' successfully | method=%s | status=%s",
                    page_name,
                    fetch_method,
                    packaged.get("status_code"),
                )
            except Exception as exc:
                warning = f"Failed to fetch page '{page_name}' -> {page_url}: {type(exc).__name__}: {exc}"
                record["diagnostics"]["fetch_warnings"].append(warning)
                self.logger.warning(warning)

        return pages

    def _fetch_rendered_page(self, url: str) -> Dict[str, Any]:
        result = self._fetch_via_requests(url)
        if result is not None:
            return result
        return self._fetch_via_playwright(url)

    def _fetch_via_requests(self, url: str) -> Optional[Dict[str, Any]]:
        try:
            response = self.session.get(url, timeout=self.config.request_timeout, allow_redirects=True)
            if response.status_code >= 400:
                return None

            html_content = response.text
            if not html_looks_usable(html_content):
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

    def _package_page(self, page_name: str, url: str) -> PagePackage:
        fetched = self._fetch_rendered_page(url)

        if page_name == "datadocumentation" and not datadoc_html_has_inventory(
            fetched.get("html", ""),
            fetched.get("visible_text", ""),
        ):
            self.logger.info(
                "Data & Documentation inventory was not present in the first response; retrying with Playwright rendering."
            )
            try:
                rendered = self._fetch_via_playwright(url)
                if datadoc_html_has_inventory(rendered.get("html", ""), rendered.get("visible_text", "")):
                    fetched = rendered
                    self.logger.info(
                        "Data & Documentation inventory recovered via Playwright rendering."
                    )
                else:
                    fetched = rendered
                    self.logger.warning(
                        "Playwright retry completed but the Data & Documentation inventory markers are still missing."
                    )
            except Exception as exc:
                self.logger.warning(
                    "Playwright retry for Data & Documentation failed; continuing with the original response. error=%s",
                    exc,
                )

        soup = BeautifulSoup(fetched["html"], "html.parser")

        all_variable_assignments: Dict[str, Any] = {}
        for script in soup.find_all("script"):
            script_text = script.get_text() or ""
            assignments = extract_all_variable_assignments(script_text)
            all_variable_assignments.update(assignments)

        jsonld_dataset = find_jsonld_dataset(soup)
        meta_map = extract_meta_map(soup)
        sections = extract_metadata_sections(soup)
        tab_urls = extract_tab_urls(soup, fetched["final_url"])
        action_signals = extract_action_signals(soup, fetched["visible_text"])
        variable_count = extract_variable_count_from_payloads(all_variable_assignments, fetched["visible_text"])
        publications_data = extract_publications_from_payloads(all_variable_assignments, fetched["visible_text"])
        export_formats = extract_export_formats_from_html(soup, fetched["visible_text"])

        return {
            "page_name": page_name,
            "requested_url": fetched["requested_url"],
            "final_url": fetched["final_url"],
            "status_code": fetched["status_code"],
            "page_title": fetched["page_title"],
            "visible_text": fetched["visible_text"],
            "html": fetched["html"],
            "content_type": fetched.get("content_type", ""),
            "fetch_method": fetched.get("fetch_method", ""),
            "soup": soup,
            "jsonld_dataset": jsonld_dataset,
            "meta_map": meta_map,
            "sections": sections,
            "tab_urls": tab_urls,
            "action_signals": action_signals,
            "action_texts": action_signals.get("action_texts", []),
            "variables_map": all_variable_assignments,
            "variable_count": variable_count,
            "publications_data": publications_data,
            "export_formats": export_formats,
        }

    def _populate_record_from_pages(
        self,
        record: MetadataRecord,
        pages: Dict[str, PagePackage],
        url_map: Dict[str, str],
    ) -> None:
        main_page = pages["main"]
        record["final_url"] = main_page["final_url"]
        record["page_title"] = main_page["page_title"]
        record["study_id"] = first_non_empty(record["study_id"], infer_study_id(main_page["final_url"]))
        record["identifiers"]["study_id"] = record["study_id"]
        record["identifiers"]["source_record_id"] = record["study_id"]

        jsonld = None
        for page in pages.values():
            if page.get("jsonld_dataset"):
                jsonld = page["jsonld_dataset"]
                break

        meta_map: Dict[str, List[str]] = {}
        for page in pages.values():
            for key, values in page.get("meta_map", {}).items():
                meta_map.setdefault(key, [])
                for value in values:
                    if value not in meta_map[key]:
                        meta_map[key].append(value)

        inline_metadata = extract_inline_metadata_bundle(pages)
        publisher_obj = get_jsonld_value(jsonld, "publisher")
        catalog_obj = get_jsonld_value(jsonld, "includedInDataCatalog")

        record["title"] = first_non_empty(
            get_jsonld_value(jsonld, "name"),
            inline_metadata.get("title"),
            get_meta_first(meta_map, "DC.title", "og:title", "twitter:title"),
            main_page["page_title"],
        )

        record["abstract"] = first_non_empty(
            get_jsonld_value(jsonld, "description"),
            inline_metadata.get("description"),
            collect_sections_across_pages(pages, "Summary")[0] if collect_sections_across_pages(pages, "Summary") else "",
        )
        record["summary"] = record["abstract"]

        doi_value = canonicalize_doi(
            first_non_empty(
                parse_jsonld_doi(jsonld),
                get_meta_first(meta_map, "DC.identifier"),
            )
        )
        record["doi"] = doi_value
        record["version"] = first_non_empty(
            get_jsonld_value(jsonld, "version"),
            inline_metadata.get("versionLabel"),
            inline_metadata.get("versionNumber"),
        )

        record["published_date"] = normalize_date_like(
            first_non_empty(
                get_jsonld_value(jsonld, "datePublished"),
                inline_metadata.get("created"),
                get_meta_first(meta_map, "DC.date"),
            )
        )
        record["modified_date"] = normalize_date_like(get_jsonld_value(jsonld, "dateModified"))

        record["dates"]["published_date"] = record["published_date"]
        record["dates"]["modified_date"] = record["modified_date"]
        record["dates"]["created_date"] = normalize_date_like(inline_metadata.get("created"))
        record["dates"]["deposit_date"] = normalize_date_like(inline_metadata.get("created"))
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

        author_info = parse_jsonld_authors_and_affiliations(jsonld)
        record["authors"] = author_info["authors"]
        record["author_affiliations"] = author_info["affiliations"]

        contributors: List[Dict[str, Any]] = []

        for creator in ensure_list(inline_metadata.get("creator")):
            if not isinstance(creator, dict):
                continue

            name = clean_text(creator.get("personName") or creator.get("display") or creator.get("orgName"))
            affiliation = normalize_affiliation_value(
                creator.get("affiliationsDisplay") or creator.get("personOrgName") or creator.get("orgName")
            )

            if name and name not in record["authors"]:
                record["authors"].append(name)
            if affiliation and affiliation not in record["author_affiliations"]:
                record["author_affiliations"].append(affiliation)

            if name:
                contributors.append(
                    {
                        "name": name,
                        "role": "Creator",
                        "affiliation": affiliation,
                        "orcid": "",
                        "url": "",
                    }
                )

        for idx, author in enumerate(record["authors"]):
            affiliation = record["author_affiliations"][idx] if idx < len(record["author_affiliations"]) else ""
            contributor = {
                "name": author,
                "role": "Creator",
                "affiliation": affiliation,
                "orcid": "",
                "url": "",
            }
            if contributor["name"] and contributor not in contributors:
                contributors.append(contributor)

        record["contributors"] = contributors

        record["publisher"] = first_non_empty(
            get_jsonld_value(publisher_obj, "name") if isinstance(publisher_obj, dict) else "",
            get_jsonld_value(catalog_obj, "name") if isinstance(catalog_obj, dict) else "",
            get_meta_first(meta_map, "DC.publisher"),
            REPOSITORY_NAME,
        )
        record["distributors"] = parse_distributors(meta_map, inline_metadata, pages)
        record["citation"] = first_non_empty(
            get_meta_first(meta_map, "DC.bibliographicCitation", "citation_reference", "citation"),
        )

        purpose_candidates = collect_sections_across_pages(pages, "Purpose") + collect_sections_across_pages(pages, "Study Purpose")
        design_candidates = collect_sections_across_pages(pages, "Study Design")
        sample_candidates = collect_sections_across_pages(pages, "Sample")
        universe_candidates = collect_sections_across_pages(pages, "Universe")

        record["purpose"] = first_non_empty(
            strip_html_fragment(inline_metadata.get("purpose")),
            purpose_candidates[0] if purpose_candidates else "",
        )
        record["study_design"] = first_non_empty(
            strip_html_fragment(inline_metadata.get("studyDesign")),
            design_candidates[0] if design_candidates else "",
        )
        record["sample"] = first_non_empty(
            strip_html_fragment(inline_metadata.get("sampProc")),
            sample_candidates[0] if sample_candidates else "",
        )
        record["universe"] = first_non_empty(
            strip_html_fragment(inline_metadata.get("universe")),
            universe_candidates[0] if universe_candidates else "",
        )

        record["language"] = clean_list(
            ensure_list(get_jsonld_value(jsonld, "inLanguage")) + ensure_list(inline_metadata.get("language"))
        )
        record["keywords"] = clean_list(
            ensure_list(get_jsonld_value(jsonld, "keywords")) + ensure_list(inline_metadata.get("keyword"))
        )
        record["subjects"] = clean_list(
            ensure_list(inline_metadata.get("subject")) + collect_sections_across_pages(pages, "Subject Terms")
        )
        record["topics"] = clean_list(
            ensure_list(inline_metadata.get("topic")) + collect_sections_across_pages(pages, "Topic")
        )

        record["relationships"]["series"] = clean_list(
            ensure_list(inline_metadata.get("series")) + collect_sections_across_pages(pages, "Series")
        )
        record["relationships"]["collections"] = clean_list(ensure_list(inline_metadata.get("collection")))
        record["relationships"]["related_datasets"] = clean_list(
            ensure_list(inline_metadata.get("relatedStudies"))
        )

        record["funding"] = parse_funding(jsonld, inline_metadata)

        record["time"]["time_method"] = clean_list(
            ensure_list(inline_metadata.get("timeMeth")) + collect_sections_across_pages(pages, "Time Method")
        )
        record["time"]["collection_dates"] = clean_list(ensure_list(inline_metadata.get("collectionDates")))
        record["time"]["temporal_coverage"] = clean_list(
            ensure_list(get_jsonld_value(jsonld, "temporalCoverage")) + ensure_list(inline_metadata.get("timePeriods"))
        )

        geographic_coverage = clean_list(
            ensure_list(get_jsonld_value(jsonld, "spatialCoverage"))
            + ensure_list(inline_metadata.get("location"))
            + collect_sections_across_pages(pages, "Geographic Coverage")
        )
        record["coverage"]["geographic_coverage"] = geographic_coverage
        record["coverage"]["spatial_coverage"] = geographic_coverage

        analysis_units = clean_list(
            ensure_list(inline_metadata.get("analysisUnit")) + collect_sections_across_pages(pages, "Unit(s) of Observation")
        )
        kind_of_data = clean_list(
            ensure_list(inline_metadata.get("kindOfData"))
            + collect_sections_across_pages(pages, "Data Type(s)")
            + collect_sections_across_pages(pages, "Kind of Data")
        )
        collection_mode = clean_list(
            ensure_list(inline_metadata.get("collectionMode")) + collect_sections_across_pages(pages, "Mode of Data Collection")
        )
        sampling_procedure = clean_html_list(
            ensure_list(inline_metadata.get("sampProc")) + collect_sections_across_pages(pages, "Sampling")
        )

        record["methodology"]["analysis_units"] = analysis_units
        record["methodology"]["kind_of_data"] = kind_of_data
        record["methodology"]["collection_mode"] = collection_mode
        record["methodology"]["sampling_procedure"] = sampling_procedure
        record["unit_of_analysis"] = analysis_units[0] if analysis_units else ""

        restrictions_candidates = collect_sections_across_pages(pages, "Restrictions")
        restrictions = first_non_empty(
            strip_html_fragment(get_jsonld_value(jsonld, "conditionsOfAccess")),
            strip_html_fragment(inline_metadata.get("accessRights")),
            restrictions_candidates[0] if restrictions_candidates else "",
        )

        if not restrictions and "terms" in pages:
            restrictions = pages["terms"].get("visible_text", "")

        restricted_data_types = inline_metadata.get("restrictedDataTypes", {})
        if not isinstance(restricted_data_types, dict):
            restricted_data_types = {}

        terms_url = url_map.get("terms", "")
        record["access"]["license"] = first_non_empty(
            clean_text(get_jsonld_value(jsonld, "license")),
            terms_url,
        )
        record["access"]["license_url"] = first_non_empty(
            clean_text(get_jsonld_value(jsonld, "license")),
            terms_url,
        )
        record["access"]["terms_url"] = terms_url
        record["access"]["restrictions"] = restrictions

        for key in record["access"]["restricted_data_types"].keys():
            record["access"]["restricted_data_types"][key] = bool(restricted_data_types.get(key, False))

        documentation_only_download = False
        access_restricted_data_button = False
        analyze_online = False
        raw_action_signals: List[Dict[str, Any]] = []

        for page in pages.values():
            action_signals = page.get("action_signals", {})
            raw_action_signals.append(
                {
                    "page_name": page.get("page_name"),
                    "signals": action_signals,
                }
            )
            documentation_only_download = documentation_only_download or bool(action_signals.get("documentation_only_download"))
            access_restricted_data_button = access_restricted_data_button or bool(action_signals.get("access_restricted_data_button"))
            analyze_online = analyze_online or bool(action_signals.get("analyze_online"))

        record["access"]["documentation_only_download"] = documentation_only_download
        record["access"]["access_restricted_data_button"] = access_restricted_data_button
        record["access"]["analyze_online"] = analyze_online

        record["notes"]["collection_notes"] = clean_html_list(ensure_list(inline_metadata.get("collectionNotes")))
        record["notes"]["collection_changes"] = clean_html_list(ensure_list(inline_metadata.get("collectionChanges")))

        variable_counts = [page.get("variable_count") for page in pages.values() if isinstance(page.get("variable_count"), int)]
        record["variables"]["count"] = max(variable_counts) if variable_counts else None
        if record["variables"]["count"] is not None:
            record["variables"]["overview"] = f"{record['variables']['count']} variables reported."

        publication_items: List[Dict[str, Any]] = []
        publication_counts: List[int] = []
        publications_page_fetched = "publications" in pages

        for page in pages.values():
            publications = page.get("publications_data", {})
            publication_items.extend(publications.get("items", []))
            if isinstance(publications.get("count"), int):
                publication_counts.append(publications["count"])

        publication_items = unique_dicts(publication_items)
        record["publications"]["items"] = publication_items

        if publication_counts:
            record["publications"]["count"] = max(publication_counts)
        elif publication_items:
            record["publications"]["count"] = len(publication_items)
        elif publications_page_fetched:
            record["publications"]["count"] = 0
        else:
            record["publications"]["count"] = None
            record["diagnostics"]["field_warnings"].append(
                "Publications page was not fetched successfully; publications.count left null."
            )

        export_formats: List[str] = []
        export_page_fetched = "export" in pages
        raw_export_signals: List[Dict[str, Any]] = []

        for page in pages.values():
            raw_export_signals.append(
                {
                    "page_name": page.get("page_name"),
                    "export_formats": page.get("export_formats", []),
                }
            )
            if page.get("export_formats"):
                export_formats.extend(page["export_formats"])

        export_formats = clean_list(export_formats)
        if export_formats:
            canonical_order = list(EXPORT_FORMAT_PATTERNS.keys())
            record["export_formats"] = [label for label in canonical_order if label in export_formats]
        elif export_page_fetched:
            record["export_formats"] = []
            record["diagnostics"]["field_warnings"].append(
                "Export page was fetched but no export formats were detected; review export parser for this study."
            )
        else:
            record["export_formats"] = []
            record["diagnostics"]["field_warnings"].append(
                "Export page was not fetched successfully; export_formats left as an empty list."
            )

        record["source_specific"]["raw_jsonld"] = jsonld
        record["source_specific"]["raw_meta_tags"] = meta_map
        record["source_specific"]["raw_page_blocks"] = {
            page_name: {
                "sections": page.get("sections", {}),
                "tab_urls": page.get("tab_urls", {}),
            }
            for page_name, page in pages.items()
        }
        record["source_specific"]["raw_export_signals"] = raw_export_signals
        record["source_specific"]["raw_action_signals"] = raw_action_signals
        record["source_specific"]["extra"] = {
            "inline_metadata": inline_metadata,
        }

    def _build_files_section_from_pages(
        self,
        study_id: str,
        record: MetadataRecord,
        pages: Dict[str, PagePackage],
    ) -> Dict[str, Any]:
        datadoc_page = pages.get("datadocumentation")
        datadoc_soup = datadoc_page.get("soup") if datadoc_page else None

        jsonld = None
        for page in pages.values():
            if page.get("jsonld_dataset"):
                jsonld = page["jsonld_dataset"]
                break

        table_inventory = extract_datadoc_table_files(datadoc_soup, study_id)
        project_download_options = extract_project_download_options(jsonld, datadoc_soup, study_id)
        summary = summarize_visible_files(
            raw_files=table_inventory["raw_files"],
            unique_files=table_inventory["unique_files"],
            project_download_options=project_download_options,
        )

        record["source_specific"]["extra"]["project_download_options"] = project_download_options
        record["source_specific"]["extra"]["page_file_inventory_basis"] = "datadocumentation_rendered_table_and_page_jsonld"

        return {
            "endpoint": "",
            "status_code": datadoc_page.get("status_code") if datadoc_page else None,
            "content_type": datadoc_page.get("content_type", "") if datadoc_page else "",
            "source": "icpsr_page",
            "counting_basis": "page_visible_resources",
            "project_download_options": project_download_options,
            "filesets": table_inventory["filesets"],
            "raw_files": table_inventory["raw_files"],
            "unique_files": table_inventory["unique_files"],
            "downloadable_files": table_inventory["downloadable_files"],
            "documentation_files": table_inventory["documentation_files"],
            "data_files": table_inventory["data_files"],
            "setup_files": table_inventory["setup_files"],
            "other_files": table_inventory["other_files"],
            "summary": summary,
        }

    def _compute_access_flags(self, record: MetadataRecord) -> None:
        unique_files = record["files"].get("unique_files", [])
        project_download_options = record["files"].get("project_download_options", [])

        record["access"]["has_downloadable_files"] = (
            record["files"]["summary"]["unique_downloadable_files"] > 0
            or len(project_download_options) > 0
        )
        record["access"]["has_restricted_files"] = (
            record["access"]["restricted_data_types"]["restricted"]
            or record["access"]["access_restricted_data_button"]
        )

        if any(f.get("public") is True for f in unique_files):
            record["access"]["has_public_files"] = True
        elif any(f.get("public") is False for f in unique_files):
            record["access"]["has_public_files"] = False
        else:
            record["access"]["has_public_files"] = None

        if record["access"]["has_restricted_files"]:
            record["access"]["open_access"] = False
            record["access"]["authentication_required"] = True
        elif unique_files or project_download_options:
            record["access"]["open_access"] = True

    def _finalize_record(
        self,
        record: MetadataRecord,
        pages: Dict[str, PagePackage],
        url_map: Dict[str, str],
    ) -> None:
        if not record["access"]["terms_url"]:
            record["access"]["terms_url"] = url_map.get("terms", "")
        if not record["access"]["license_url"]:
            record["access"]["license_url"] = record["access"]["terms_url"]
        if not record["access"]["license"]:
            record["access"]["license"] = record["access"]["license_url"]

        record["provenance"]["source_urls_visited"] = clean_list(record["provenance"]["source_urls_visited"])
        record["diagnostics"]["metadata_sources"] = clean_list(record["diagnostics"]["metadata_sources"])

        if not record["authors"]:
            record["diagnostics"]["field_warnings"].append("No authors were extracted.")
        if not record["publisher"]:
            record["diagnostics"]["field_warnings"].append("No publisher was extracted.")
        if not record["files"]["raw_files"]:
            record["diagnostics"]["field_warnings"].append(
                "No page-visible file rows were extracted from the Data & Documentation page."
            )
        if "publications" not in pages:
            record["diagnostics"]["field_warnings"].append("Publications page was not available during extraction.")
        if "export" not in pages:
            record["diagnostics"]["field_warnings"].append("Export page was not available during extraction.")

        normalized = normalize_metadata_record(record)
        record.clear()
        record.update(normalized)

        validation_result = annotate_record_with_validation(record)

        if not validation_result.valid:
            for error in validation_result.errors:
                self.logger.error("Validation error: %s", error)
            raise ExtractionError(
                "Unified metadata validation failed. "
                f"Errors: {validation_result.errors}"
            )

    def _save_metadata_json(self, record: MetadataRecord) -> Optional[str]:
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


def extract_icpsr_metadata(
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

    with ICPSRExtractor(config=config) as extractor:
        return extractor.extract()


if __name__ == "__main__":
    setup_logging()

    try:
        metadata = extract_icpsr_metadata(DEFAULT_SOURCE_URL)
        logging.getLogger("ICPSRExtractor").info(
            "Summary | study_id=%s | title=%s | doi=%s | raw_files=%s | unique_files=%s | project_download_options=%s",
            metadata["study_id"],
            metadata["title"],
            metadata["doi"],
            metadata["files"]["summary"]["raw_entry_count"],
            metadata["files"]["summary"]["unique_file_count"],
            metadata["files"]["summary"]["project_download_option_count"],
        )
    except Exception:
        logging.getLogger("ICPSRExtractor").exception("ICPSR metadata extraction failed")
        raise