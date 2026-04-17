from __future__ import annotations

import json
import logging
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse, unquote

import requests
from bs4 import BeautifulSoup, Tag
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
    safe_filename,
    strip_html_fragment,
    unique_dicts,
)
from QDArchive.schemas.unified_metadata import new_metadata_record
from QDArchive.schemas.validators import annotate_record_with_validation


# =========================================================
# MODULE CONFIGURATION
# =========================================================
PARSER_VERSION = "1.0.0"

# DEFAULT_SOURCE_URL = "https://www.datalumos.org/datalumos/project/244745/version/V1/view"
DEFAULT_SOURCE_URL = "https://www.datalumos.org/datalumos/project/233924/version/V1/view"

DEFAULT_HEADLESS = True
DEFAULT_WAIT_MS = 5000
DEFAULT_REQUEST_TIMEOUT = 60
DEFAULT_PLAYWRIGHT_TIMEOUT_MS = 45000
DEFAULT_MAX_PAGES = 100

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

REPOSITORY_NAME = "DataLumos"
REPOSITORY_URL = "https://www.datalumos.org"

EXPORT_FORMAT_PATTERNS = {
    "Dublin Core": [r"\bdublin core\b", r"\boai[_-]?dc\b"],
    "DDI 2.5": [r"\bddi\s*2\.5\b", r"\boai[_-]?ddi25\b", r"\bddi25\b"],
    "DATS 2.2 (JSON)": [r"\bdats\s*2\.2\s*\(json\)\b", r"\bdats\s*2\.2\b", r"\bdats\b"],
    "DCAT-US 1.1 (beta)": [r"\bdcat-us\s*1\.1\s*\(beta\)\b", r"\bdcat[- ]us\s*1\.1\b", r"\bdcat-us\b"],
}

MIME_BY_EXTENSION = {
    "pdf": "application/pdf",
    "zip": "application/zip",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "csv": "text/csv",
    "tsv": "text/tab-separated-values",
    "txt": "text/plain",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "json": "application/json",
    "xml": "application/xml",
    "sav": "application/octet-stream",
    "dta": "application/octet-stream",
    "sas7bdat": "application/octet-stream",
    "rdata": "application/octet-stream",
    "rda": "application/octet-stream",
}

CANONICAL_ORG_MAP = {
    "inter-university consortium for political and social research": "Inter-university Consortium for Political and Social Research (ICPSR)",
    "inter-university consortium for political and social research (icpsr)": "Inter-university Consortium for Political and Social Research (ICPSR)",
    "icpsr": "Inter-university Consortium for Political and Social Research (ICPSR)",
    "datalumos": "DataLumos",
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
    "sas7bdat": "data",
    "rdata": "data",
    "rda": "data",
    "json": "data",
    "xml": "data",
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
    """Configuration for one DataLumos extraction run."""

    source_url: str = DEFAULT_SOURCE_URL
    save_output_json: bool = DEFAULT_SAVE_OUTPUT_JSON
    output_dir: str = DEFAULT_OUTPUT_DIR
    headless: bool = DEFAULT_HEADLESS
    wait_ms: int = DEFAULT_WAIT_MS
    request_timeout: int = DEFAULT_REQUEST_TIMEOUT
    playwright_timeout_ms: int = DEFAULT_PLAYWRIGHT_TIMEOUT_MS
    max_pages: int = DEFAULT_MAX_PAGES
    user_agent: str = DEFAULT_USER_AGENT


# =========================================================
# LOGGING
# =========================================================
def setup_logging(level: int = DEFAULT_LOG_LEVEL) -> None:
    """
    Configure extractor logging.

    INFO:
    - crawl/fetch lifecycle
    - success summaries
    - final extraction summary

    DEBUG:
    - parser fallbacks
    - per-page parse counts
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


def split_semicolon_values(values: List[Any]) -> List[str]:
    """Split semicolon-delimited values and return a clean, de-duplicated list."""
    output: List[str] = []
    for value in values:
        text = clean_text(value)
        if not text:
            continue

        for part in re.split(r"\s*;\s*", text):
            cleaned = clean_text(part)
            if cleaned and cleaned not in output:
                output.append(cleaned)
    return output


def normalize_date_string(value: Any) -> str:
    """
    Normalize common date formats to ISO date when safely possible.

    Conservative fallback:
    - returns original cleaned text when parsing is uncertain
    """
    text = clean_text(value)
    if not text:
        return ""

    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%Y %I:%M:%p", "%m/%d/%Y %I:%M:%S:%p"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.date().isoformat()
        except Exception:
            continue
    return text


def normalize_time_period_value(value: Any) -> str:
    """Normalize month/year ranges when they appear in compact forms."""
    text = clean_text(value)
    if not text:
        return ""

    text = text.replace("–", "-").replace("—", "-")
    match = re.match(r"^(\d{1,2})/(\d{4})\s*-\s*(\d{1,2})/(\d{4})$", text)
    if match:
        m1, y1, m2, y2 = match.groups()
        return f"{y1}-{int(m1):02d} -- {y2}-{int(m2):02d}"

    return text


def infer_page_type(url: str) -> str:
    """Infer repository page type from URL."""
    lowered = url.lower()
    if "datalumos.org" in lowered:
        return "datalumos"
    return "unknown"


def infer_project_id(url: str) -> str:
    """Extract DataLumos project ID from URL."""
    match = re.search(r"/project/(\d+)", url)
    return match.group(1) if match else ""


def infer_version_from_url(url: str) -> str:
    """Extract version label from URL."""
    match = re.search(r"/version/([^/]+)/", url)
    return match.group(1) if match else ""


def canonicalize_org_name(value: Any) -> str:
    """Canonicalize known organization names to one preferred display form."""
    text = clean_text(value)
    if not text:
        return ""
    lowered = text.lower().strip()
    return CANONICAL_ORG_MAP.get(lowered, text)


def canonicalize_org_list(values: List[Any]) -> List[str]:
    """Canonicalize and de-duplicate organization names."""
    output: List[str] = []
    for value in values:
        canonical = canonicalize_org_name(value)
        if canonical and canonical not in output:
            output.append(canonical)
    return output


def get_query_param(url: str, key: str) -> str:
    """Read a single query parameter value from a URL."""
    values = parse_qs(urlparse(url).query).get(key, [])
    return values[0] if values else ""


def parse_datalumos_href_filename_and_extension(href: str) -> Tuple[str, str]:
    """
    Extract filename and extension from DataLumos resource URLs.

    DataLumos often carries the real file path inside the query parameter `path`,
    so this helper checks that first before falling back to URL path parsing.
    """
    path_param = unquote(get_query_param(href, "path"))
    candidate = path_param or unquote(urlparse(href).path or "")
    file_name = clean_text(Path(candidate).name)
    extension = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    return file_name, extension


def normalize_dataset_url(url: str) -> str:
    """
    Normalize a DataLumos page/resource URL for stable storage.

    Drops fragment and unstable session-like parameters.
    """
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)

    filtered_query: List[Tuple[str, str]] = []
    for key in sorted(query.keys()):
        if key.lower() == "jsessionid":
            continue
        for value in sorted(query[key]):
            filtered_query.append((key, value))

    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", urlencode(filtered_query, doseq=True), ""))


def normalize_crawl_url(url: str) -> str:
    """
    Normalize page identity for crawl deduplication.

    Keeps only crawl-relevant query parameters.
    """
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    allowed = {"pageSelected", "pageSize", "path", "type"}

    filtered_query: List[Tuple[str, str]] = []
    for key in sorted(query.keys()):
        if key not in allowed:
            continue
        for value in sorted(query[key]):
            filtered_query.append((key, value))

    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", urlencode(filtered_query, doseq=True), ""))


def normalize_file_resource_url(url: str) -> str:
    """Normalize file identity URL so duplicate representations collapse."""
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)

    filtered_query: List[Tuple[str, str]] = []
    for key in sorted(query.keys()):
        if key not in {"path", "type"}:
            continue
        for value in sorted(query[key]):
            filtered_query.append((key, value))

    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", urlencode(filtered_query, doseq=True), ""))


def normalize_version_url(url: str) -> str:
    """Normalize a version page URL by dropping query and fragment noise."""
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def infer_mime_from_extension(extension: str) -> str:
    """Infer MIME type from file extension."""
    return MIME_BY_EXTENSION.get(clean_text(extension).lower(), "")


def infer_file_format(extension: str, mime_type: str) -> str:
    """Infer a file format label from extension or MIME type."""
    extension = clean_text(extension).lower()
    if extension:
        return extension

    for ext, mime in MIME_BY_EXTENSION.items():
        if mime == mime_type:
            return ext
    return ""


def detect_export_formats_in_text(text: Any) -> List[str]:
    """Detect supported export formats from arbitrary text."""
    lowered = clean_text(text).lower()
    if not lowered:
        return []

    found: List[str] = []
    for label, patterns in EXPORT_FORMAT_PATTERNS.items():
        if any(re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in patterns):
            found.append(label)
    return found


# =========================================================
# BALANCED JS PARSING
# =========================================================
def read_balanced_expression(text: str, start_idx: int) -> Tuple[str, int]:
    """
    Read a balanced JavaScript assignment expression until a top-level semicolon.

    Useful for inline `var xyz = ...` style data exposed in script blocks.
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
    Extract inline JavaScript assignments like:
    - var xyz = ...
    - variables.xyz = ...

    JSON-like payloads are parsed when possible.
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


def parse_header_block(soup: BeautifulSoup) -> Dict[str, str]:
    """Extract principal investigators and version from the visible header area."""
    output = {
        "principal_investigators": "",
        "version": "",
    }

    for paragraph in soup.find_all("p"):
        strong = paragraph.find("strong")
        if not strong:
            continue

        label = clean_text(strong.get_text(" ", strip=True)).lower().rstrip(":")
        text = clean_text(paragraph.get_text(" ", strip=True))
        text = re.sub(r"View help for [^.]+", "", text)
        text = clean_text(text)
        value = clean_text(text.split(":", 1)[1]) if ":" in text else ""

        if label == "principal investigator(s)":
            output["principal_investigators"] = value
        elif label == "version":
            output["version"] = value

    return output


def parse_project_panels(soup: BeautifulSoup) -> Dict[str, Dict[str, List[str]]]:
    """
    Parse DataLumos panel blocks into:
    {
        "Section Title": {
            "Label": ["Value1", "Value2"]
        }
    }
    """
    sections: Dict[str, Dict[str, List[str]]] = {}
    current_heading: Optional[str] = None

    for tag in soup.find_all(["div", "h2", "h3", "h4", "p"]):
        if tag.name == "div" and "panel-title" in (tag.get("class") or []):
            heading = clean_text(tag.get_text(" ", strip=True))
            if heading:
                current_heading = heading
                sections.setdefault(current_heading, {})
            continue

        if tag.name == "div" and "panel-heading" in (tag.get("class") or []) and current_heading:
            strong = tag.find("strong")
            if not strong:
                continue

            label = clean_text(strong.get_text(" ", strip=True)).rstrip(":")
            clone = BeautifulSoup(str(tag), "html.parser")
            for help_tag in clone.find_all(attrs={"data-helplink": True}):
                help_tag.decompose()

            text = clean_text(clone.get_text(" ", strip=True))
            value = clean_text(text.split(":", 1)[1]) if ":" in text else text

            if label and value:
                sections[current_heading].setdefault(label, [])
                if value not in sections[current_heading][label]:
                    sections[current_heading][label].append(value)

    return sections


def extract_project_citation(soup: BeautifulSoup) -> str:
    """Extract project citation text when present."""
    citation_div = soup.find(id="projectCitation")
    if citation_div:
        return clean_text(citation_div.get_text(" ", strip=True))

    for strong in soup.find_all("strong"):
        if "project citation" in clean_text(strong.get_text()).lower():
            parent = strong.find_parent()
            if parent:
                text = clean_text(parent.get_text(" ", strip=True))
                if text:
                    return text
    return ""


def parse_versions_block(soup: BeautifulSoup, base_url: str) -> List[Dict[str, Any]]:
    """Parse the 'Published Versions' block into structured version entries."""
    versions: List[Dict[str, Any]] = []

    for h2 in soup.find_all("h2"):
        if clean_text(h2.get_text(" ", strip=True)).lower() != "published versions":
            continue

        container = h2.find_parent(["fieldset", "div"])
        if not container:
            continue

        for anchor in container.find_all("a", href=True):
            text = clean_text(anchor.get_text(" ", strip=True))
            if not text:
                continue

            match = re.match(r"(?P<version>V[^ ]+)\s*\[(?P<date>[^\]]+)\]", text, flags=re.IGNORECASE)
            if not match:
                continue

            versions.append(
                {
                    "version": clean_text(match.group("version")),
                    "published_date": normalize_date_string(match.group("date")),
                    "url": normalize_version_url(urljoin(base_url, anchor["href"])),
                    "is_current": False,
                }
            )

    return unique_dicts(versions)


def extract_export_formats_from_html(soup: BeautifulSoup, visible_text: str) -> List[str]:
    """Detect known export formats from visible text and HTML attributes."""
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


def extract_access_disclaimer(soup: BeautifulSoup) -> str:
    """Extract access/license disclaimer text."""
    license_div = soup.find(id="projectLicense")
    if not license_div:
        return ""

    outer = license_div.find_parent(class_="well")
    if not outer:
        return ""

    return clean_text(outer.get_text(" ", strip=True))


def extract_action_signals(soup: BeautifulSoup, visible_text: str) -> Dict[str, Any]:
    """
    Detect access-related and analysis-related action buttons/links.

    Signals:
    - documentation_only_download
    - access_restricted_data_button
    - analyze_online
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


def normalize_publication_entry(text: str) -> Dict[str, Any]:
    """Convert a visible publication text line into a normalized publication item."""
    item = {"title": clean_text(text)}

    year_match = re.search(r"\b(19|20)\d{2}\b", text)
    if year_match:
        item["year"] = year_match.group(0)

    doi_match = re.search(r"(10\.\d{4,9}/[^\s;]+)", text)
    if doi_match:
        item["doi"] = canonicalize_doi(doi_match.group(1).rstrip(".,)"))

    return item


def extract_related_publications(soup: BeautifulSoup) -> Dict[str, Any]:
    """Extract related publications from parsed panel content."""
    result = {"count": None, "items": []}
    panels = parse_project_panels(soup)
    methodology = panels.get("Methodology", {})

    raw_candidates: List[str] = []
    for label, values in methodology.items():
        if "related publication" in label.lower():
            raw_candidates.extend(values)

    text = clean_text(soup.get_text(" ", strip=True))
    if "No related publications for this project" in text:
        return {"count": 0, "items": []}

    items: List[Dict[str, Any]] = []
    for value in raw_candidates:
        if "no related publications" in value.lower():
            return {"count": 0, "items": []}
        if value:
            items.append(normalize_publication_entry(value))

    items = unique_dicts([item for item in items if item.get("title")])
    if items:
        result["count"] = len(items)
        result["items"] = items

    return result


def guess_file_content(title: str, file_name: str, href: str, folder_hint: str = "") -> str:
    """Infer a high-level file content label from title, filename, URL, and folder hint."""
    hay = clean_text(f"{title} {file_name} {href} {folder_hint}").lower()
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""

    if "metrics" in hay or "usage/download" in hay:
        return ""

    if "codebook" in hay:
        return "Codebook"

    if any(token in hay for token in [
        "questionnaire",
        "data-collection-instruments",
        "interview",
        "consentform",
        "manual",
        "worksheet",
        "brochure",
        "postcard",
        "script",
        "privacypledge",
    ]):
        return "Questionnaire"

    if "setup" in hay or "dictionary" in hay:
        return "Setup"

    if any(token in hay for token in [
        "documentation",
        "faq",
        "background",
        "summary findings",
        "overview",
        "revision history",
        "research projects and publications",
        "user guide",
        "variable-list",
        "variable list",
    ]):
        return "Documentation"

    if ext in {"zip", "csv", "tsv", "sav", "dta", "sas7bdat", "rdata", "rda", "json", "xml"}:
        return "Data"

    if ext in {"xlsx", "xls"}:
        if any(token in hay for token in ["chart", "charts", "/charts/"]):
            return "Other"
        if "variable" in hay or "codebook" in hay:
            return "Documentation"
        return "Data"

    if ext == "pdf":
        if "codebook" in hay:
            return "Codebook"
        if any(token in hay for token in ["questionnaire", "interview", "manual", "consentform", "worksheet", "brochure", "postcard", "script", "privacypledge"]):
            return "Questionnaire"
        return "Documentation"

    if ext in {"png", "jpg", "jpeg", "gif"}:
        return "Other"

    return "Other"


def extract_file_table_items(soup: BeautifulSoup, base_url: str) -> Dict[str, List[Dict[str, Any]]]:
    """
    Parse the visible DataLumos file/folder table.

    Returns:
    {
        "folders": [...],
        "files": [...]
    }
    """
    folders: List[Dict[str, Any]] = []
    files: List[Dict[str, Any]] = []

    table = soup.find("table", class_="table")
    if not table:
        return {"folders": folders, "files": files}

    for row in table.select("tbody tr"):
        cells = row.find_all("td")
        if len(cells) < 4:
            continue

        link = row.find("a", href=True)
        if not link:
            continue

        href = normalize_dataset_url(urljoin(base_url, clean_text(link["href"])))
        title = clean_text(link.get_text(" ", strip=True))
        display_size = clean_text(cells[2].get_text(" ", strip=True))
        last_modified = clean_text(cells[3].get_text(" ", strip=True))
        folder_path = unquote(get_query_param(href, "path"))

        is_folder = "type=folder" in href or bool(row.find("i", class_=re.compile(r"glyphicon-folder-open")))
        if is_folder:
            folders.append(
                {
                    "title": title,
                    "url": href,
                    "last_modified": last_modified,
                    "path": folder_path,
                }
            )
            continue

        file_name, extension = parse_datalumos_href_filename_and_extension(href)
        mime_type = infer_mime_from_extension(extension)
        file_format = infer_file_format(extension, mime_type)
        file_content = guess_file_content(title or file_name, file_name, href, folder_path)

        files.append(
            {
                "file_id": None,
                "dataset_number": None,
                "dataset_identifier": "",
                "title": title or file_name,
                "file_name": file_name,
                "file_stem": Path(file_name).stem if file_name else "",
                "file_format": file_format,
                "file_extension": extension,
                "mime_type": mime_type,
                "file_content": file_content,
                "file_category": "",
                "file_description": folder_path,
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
                "uri": normalize_file_resource_url(href),
                "download_url": normalize_file_resource_url(href),
                "api_url": "",
                "parent_path": folder_path,
                "folder_path": folder_path,
                "last_modified": last_modified,
                "display_size": display_size,
                "language": "",
                "source": "datalumos_html",
                "raw_occurrence_count": 1,
            }
        )

    for file_entry in files:
        file_entry["file_category"] = classify_file_group(file_entry)

    return {"folders": unique_dicts(folders), "files": unique_dicts(files)}


def extract_pager_urls(soup: BeautifulSoup, current_url: str) -> List[str]:
    """Extract pagination URLs from page pager controls."""
    urls: List[str] = []

    for anchor in soup.select("ul.pager a[href]"):
        href = normalize_dataset_url(urljoin(current_url, clean_text(anchor["href"])))
        if href not in urls:
            urls.append(href)

    page_select = soup.find("select", id="pageIdOptions")
    if page_select:
        parsed = urlparse(current_url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        query.pop("jsessionid", None)

        for option in page_select.find_all("option"):
            value = clean_text(option.get("value"))
            if not value:
                continue

            query["pageSelected"] = [value]
            if "pageSize" not in query:
                query["pageSize"] = ["10"]

            page_url = urlunparse(
                (
                    parsed.scheme,
                    parsed.netloc,
                    parsed.path,
                    "",
                    urlencode(sorted((k, v) for k, values in query.items() for v in values)),
                    "",
                )
            )
            page_url = normalize_dataset_url(page_url)
            if page_url not in urls:
                urls.append(page_url)

    return urls


# =========================================================
# MERGE HELPERS
# =========================================================
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
    if isinstance(identifier, str) and "doi.org" in identifier.lower():
        return canonicalize_doi(identifier)
    return ""


def parse_citation_parts(citation: str) -> Dict[str, str]:
    """Extract DOI, date, and distributor hints from a project citation string."""
    output = {
        "doi": "",
        "published_date": "",
        "distributor": "",
    }
    text = clean_text(citation)
    if not text:
        return output

    doi_match = re.search(r"https?://doi\.org/\S+", text, flags=re.IGNORECASE)
    if doi_match:
        output["doi"] = canonicalize_doi(doi_match.group(0).rstrip(".,)"))

    date_match = re.search(r"\b\d{4}-\d{2}-\d{2}\b", text)
    if date_match:
        output["published_date"] = date_match.group(0)

    distributor_match = re.search(r":\s*([^.;]+?)\s*\[distributor\]", text, flags=re.IGNORECASE)
    if distributor_match:
        output["distributor"] = canonicalize_org_name(clean_text(distributor_match.group(1)))

    return output


def parse_funding(jsonld: Optional[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Extract funding metadata from JSON-LD if available."""
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


def dedupe_versions(versions: List[Dict[str, Any]], current_version: str, fallback_url: str) -> List[Dict[str, Any]]:
    """De-duplicate version entries and mark the current version."""
    merged: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    order: List[Tuple[str, str, str]] = []

    for version_entry in versions:
        version = clean_text(version_entry.get("version"))
        published_date = normalize_date_string(version_entry.get("published_date"))
        url = normalize_version_url(version_entry.get("url") or fallback_url)
        key = (version, published_date, url)

        if key not in merged:
            merged[key] = {
                "version": version,
                "published_date": published_date,
                "url": url,
                "is_current": False,
            }
            order.append(key)

    deduped = [merged[key] for key in order]
    if not deduped and current_version:
        deduped = [
            {
                "version": current_version,
                "published_date": "",
                "url": normalize_version_url(fallback_url),
                "is_current": True,
            }
        ]

    for item in deduped:
        item["is_current"] = clean_text(item.get("version")) == clean_text(current_version)

    if deduped and not any(item["is_current"] for item in deduped):
        deduped[0]["is_current"] = True

    return deduped


def dedupe_file_key(file_entry: Dict[str, Any]) -> str:
    """Return a stable deduplication key for a file entry."""
    return first_non_empty(
        file_entry.get("resource_uuid"),
        file_entry.get("file_id"),
        normalize_file_resource_url(file_entry.get("uri", "")),
        f"{file_entry.get('file_name')}|{file_entry.get('title')}",
    )


def dedupe_file_entries(file_entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge duplicate file entries while preserving the richest record."""
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
    public_unique_files = [f for f in unique_files if f.get("public") is True]
    restricted_unique_files = [f for f in unique_files if f.get("public") is False]
    downloadable_public_files = [f for f in downloadable_files if f.get("public") is True]
    downloadable_documentation_files = [f for f in documentation_files if f.get("downloadable") is True]
    downloadable_data_files = [f for f in data_files if f.get("downloadable") is True]

    return {
        "raw_entry_count": len(raw_files),
        "unique_file_count": len(unique_files),
        "raw_public_entries": sum(1 for f in raw_files if f.get("public") is True),
        "unique_public_files": len(public_unique_files),
        "raw_restricted_or_nonpublic_entries": sum(1 for f in raw_files if f.get("public") is False),
        "unique_restricted_or_nonpublic_files": len(restricted_unique_files),
        "raw_downloadable_entries": sum(1 for f in raw_files if f.get("downloadable") is True),
        "unique_downloadable_files": len(downloadable_files),
        "downloadable_public_files": len(downloadable_public_files),
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
class DataLumosExtractor:
    """
    Production DataLumos extractor.

    Design goals:
    - always emit the shared unified metadata schema
    - keep all DataLumos-specific crawl and parsing here
    - reuse shared normalizer and validator for final schema consistency
    """

    def __init__(self, config: ExtractorConfig, logger_: Optional[logging.Logger] = None) -> None:
        self.config = config
        self.logger = logger_ or logging.getLogger(self.__class__.__name__)
        self.project_id = infer_project_id(config.source_url)

        if not self.project_id:
            raise ValueError(f"Could not infer DataLumos project ID from URL: {config.source_url}")

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": self.config.user_agent,
                "Accept-Language": "en-US,en;q=0.9",
            }
        )

    def __enter__(self) -> "DataLumosExtractor":
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
        """Run the full crawl -> parse -> merge -> normalize -> validate pipeline."""
        self.logger.info(
            "Starting DataLumos extraction | source_url=%s | project_id=%s",
            self.config.source_url,
            self.project_id,
        )

        record = self._initialize_record()
        pages = self._crawl_pages(record)

        if not pages:
            raise ExtractionError(f"Could not fetch any pages for: {self.config.source_url}")

        self._populate_record_from_pages(record, pages)
        self._compute_access_flags(record)
        self._finalize_record(record, pages)

        output_path = self._save_metadata_json(record)
        if output_path:
            self.logger.info("Saved metadata JSON to %s", output_path)

        self.logger.info(
            "Finished DataLumos extraction | project_id=%s | raw_files=%s | unique_files=%s | warnings=%s",
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
        record["page_type"] = "datalumos"
        record["repository"] = REPOSITORY_NAME
        record["repository_url"] = REPOSITORY_URL
        record["study_id"] = self.project_id

        record["provenance"]["repository"] = REPOSITORY_NAME
        record["provenance"]["repository_url"] = REPOSITORY_URL
        record["provenance"]["page_type"] = "datalumos"
        record["provenance"]["parser_version"] = PARSER_VERSION
        record["provenance"]["schema_version"] = record["schema_version"]

        record["identifiers"]["study_id"] = self.project_id
        record["identifiers"]["source_record_id"] = self.project_id
        record["identifiers"]["other_identifiers"] = [
            {
                "type": "DataLumos project_id",
                "value": self.project_id,
            }
        ]

        return record

    # -----------------------------------------------------
    # FETCH LAYER
    # -----------------------------------------------------
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
        """Fetch, parse, and package all extractable artifacts for a single crawled page."""
        fetched = self._fetch_rendered_page(url)
        soup = BeautifulSoup(fetched["html"], "html.parser")

        all_variable_assignments: Dict[str, Any] = {}
        for script in soup.find_all("script"):
            script_text = script.get_text() or ""
            assignments = extract_all_variable_assignments(script_text)
            all_variable_assignments.update(assignments)

        jsonld_dataset = find_jsonld_dataset(soup)
        meta_map = extract_meta_map(soup)
        header = parse_header_block(soup)
        panels = parse_project_panels(soup)
        action_signals = extract_action_signals(soup, fetched["visible_text"])
        versions = parse_versions_block(soup, fetched["final_url"])
        export_formats = extract_export_formats_from_html(soup, fetched["visible_text"])
        citation = extract_project_citation(soup)
        access_disclaimer = extract_access_disclaimer(soup)
        publications_data = extract_related_publications(soup)
        table_items = extract_file_table_items(soup, fetched["final_url"])
        pager_urls = extract_pager_urls(soup, fetched["final_url"])

        self.logger.debug(
            "Packaged page | url=%s | panels=%s | files=%s | folders=%s | versions=%s",
            url,
            len(panels),
            len(table_items.get("files", [])),
            len(table_items.get("folders", [])),
            len(versions),
        )

        return {
            "requested_url": fetched["requested_url"],
            "final_url": normalize_dataset_url(fetched["final_url"]),
            "status_code": fetched["status_code"],
            "page_title": fetched["page_title"],
            "visible_text": fetched["visible_text"],
            "html": fetched["html"],
            "content_type": fetched.get("content_type", ""),
            "fetch_method": fetched.get("fetch_method", ""),
            "jsonld_dataset": jsonld_dataset,
            "meta_map": meta_map,
            "header": header,
            "panels": panels,
            "action_signals": action_signals,
            "action_texts": action_signals.get("action_texts", []),
            "variables_map": all_variable_assignments,
            "versions": versions,
            "export_formats": export_formats,
            "citation": citation,
            "access_disclaimer": access_disclaimer,
            "publications_data": publications_data,
            "table_items": table_items,
            "pager_urls": pager_urls,
        }

    # -----------------------------------------------------
    # CRAWLER
    # -----------------------------------------------------
    def _crawl_pages(self, record: MetadataRecord) -> Dict[str, PagePackage]:
        """
        Crawl the main project page plus paginated/folder pages.

        Crawl identity is normalized so pagination and folder traversal remain
        stable without looping unnecessarily.
        """
        pages: Dict[str, PagePackage] = {}
        queue: List[str] = [normalize_dataset_url(self.config.source_url)]
        visited: Set[str] = set()

        while queue and len(visited) < self.config.max_pages:
            current_url = queue.pop(0)
            crawl_key = normalize_crawl_url(current_url)

            if crawl_key in visited:
                continue

            try:
                self.logger.info("Fetching page | %s", current_url)
                page = self._package_page(current_url)
                pages[crawl_key] = page
                visited.add(crawl_key)

                fetch_method = page.get("fetch_method")
                if fetch_method and fetch_method not in record["diagnostics"]["metadata_sources"]:
                    record["diagnostics"]["metadata_sources"].append(fetch_method)

                record["provenance"]["source_urls_visited"].append(page["final_url"])
                record["provenance"]["html_pages_fetched"].append(
                    {
                        "page_name": f"crawl_page_{len(record['provenance']['html_pages_fetched']) + 1}",
                        "requested_url": current_url,
                        "final_url": page["final_url"],
                        "fetch_method": fetch_method,
                        "status_code": page.get("status_code"),
                    }
                )
                if fetch_method and fetch_method not in record["provenance"]["fetch_methods"]:
                    record["provenance"]["fetch_methods"].append(fetch_method)

                queued_keys = {normalize_crawl_url(x) for x in queue}

                for pager_url in page.get("pager_urls", []):
                    pager_key = normalize_crawl_url(pager_url)
                    if pager_key not in visited and pager_key not in queued_keys:
                        queue.append(pager_url)
                        queued_keys.add(pager_key)

                for folder in page.get("table_items", {}).get("folders", []):
                    folder_url = folder.get("url", "")
                    if not folder_url:
                        continue
                    folder_key = normalize_crawl_url(folder_url)
                    if folder_key not in visited and folder_key not in queued_keys:
                        queue.append(folder_url)
                        queued_keys.add(folder_key)

            except Exception as exc:
                warning = f"Failed to fetch page '{current_url}': {type(exc).__name__}: {exc}"
                record["diagnostics"]["fetch_warnings"].append(warning)
                self.logger.warning(warning)

        if len(visited) >= self.config.max_pages:
            record["diagnostics"]["field_warnings"].append(
                f"Stopped crawling after reaching max_pages={self.config.max_pages}; crawl normalization may need review for this record."
            )

        return pages

    # -----------------------------------------------------
    # RECORD POPULATION
    # -----------------------------------------------------
    def _populate_record_from_pages(self, record: MetadataRecord, pages: Dict[str, PagePackage]) -> None:
        """Merge all crawled pages into one unified metadata record."""
        main_key = normalize_crawl_url(self.config.source_url)
        main_page = pages.get(main_key)
        if main_page is None:
            main_page = next(iter(pages.values()))

        record["final_url"] = normalize_dataset_url(main_page["final_url"])
        record["page_title"] = main_page["page_title"]
        record["page_type"] = infer_page_type(main_page["final_url"])
        record["study_id"] = first_non_empty(record["study_id"], infer_project_id(main_page["final_url"]))

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

        header_values: Dict[str, str] = {}
        for page in pages.values():
            header = page.get("header", {})
            for key, value in header.items():
                if value and key not in header_values:
                    header_values[key] = value

        panels: Dict[str, Dict[str, List[str]]] = {}
        for page in pages.values():
            for section_name, section_data in page.get("panels", {}).items():
                panels.setdefault(section_name, {})
                for label, values in section_data.items():
                    panels[section_name].setdefault(label, [])
                    for value in values:
                        if value not in panels[section_name][label]:
                            panels[section_name][label].append(value)

        citation_text = ""
        for page in pages.values():
            citation_text = first_non_empty(citation_text, page.get("citation"))
        citation_parts = parse_citation_parts(citation_text)

        current_version = first_non_empty(
            header_values.get("version"),
            jsonld.get("version") if isinstance(jsonld, dict) else "",
            infer_version_from_url(main_page["final_url"]),
        )

        versions: List[Dict[str, Any]] = []
        for page in pages.values():
            versions.extend(page.get("versions", []))

        if not versions and current_version:
            versions = [
                {
                    "version": current_version,
                    "published_date": first_non_empty(
                        normalize_date_string(jsonld.get("datePublished") if isinstance(jsonld, dict) else ""),
                        normalize_date_string(meta_map.get("DC.date", [""])[0] if meta_map.get("DC.date") else ""),
                        citation_parts.get("published_date"),
                    ),
                    "url": main_page["final_url"],
                    "is_current": True,
                }
            ]

        record["versions"] = dedupe_versions(versions, current_version, main_page["final_url"])

        # ---------- identity ----------
        record["title"] = first_non_empty(
            jsonld.get("name") if isinstance(jsonld, dict) else "",
            meta_map.get("DC.title", [""])[0] if meta_map.get("DC.title") else "",
            main_page["page_title"],
        )

        doi_value = first_non_empty(
            parse_jsonld_doi(jsonld),
            citation_parts.get("doi"),
            meta_map.get("DC.identifier", [""])[0] if meta_map.get("DC.identifier") else "",
        )
        doi_value = canonicalize_doi(doi_value)

        record["doi"] = doi_value
        record["version"] = current_version
        record["published_date"] = first_non_empty(
            normalize_date_string(jsonld.get("datePublished") if isinstance(jsonld, dict) else ""),
            normalize_date_string(meta_map.get("DC.date", [""])[0] if meta_map.get("DC.date") else ""),
            citation_parts.get("published_date"),
            record["versions"][0]["published_date"] if record["versions"] else "",
        )
        record["modified_date"] = normalize_date_string(jsonld.get("dateModified") if isinstance(jsonld, dict) else "")

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

        if not record["authors"]:
            pi_text = clean_text(header_values.get("principal_investigators"))
            if pi_text:
                record["authors"] = split_semicolon_values([pi_text])

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
        catalog_obj = jsonld.get("includedInDataCatalog") if isinstance(jsonld, dict) else {}
        record["publisher"] = canonicalize_org_name(
            first_non_empty(
                catalog_obj.get("name") if isinstance(catalog_obj, dict) else "",
                REPOSITORY_NAME,
            )
        )

        distributors: List[str] = []
        if citation_parts.get("distributor"):
            distributors.append(citation_parts["distributor"])

        dc_publisher = clean_text(meta_map.get("DC.publisher", [""])[0] if meta_map.get("DC.publisher") else "")
        if dc_publisher:
            distributors.append(dc_publisher)

        record["distributors"] = canonicalize_org_list(distributors)
        record["citation"] = citation_text

        # ---------- descriptive ----------
        project_description = panels.get("Project Description", {})
        scope = panels.get("Scope of Project", {})
        methodology = panels.get("Methodology", {})

        record["abstract"] = first_non_empty(
            jsonld.get("description") if isinstance(jsonld, dict) else "",
            (project_description.get("Summary") or [""])[0],
        )
        record["summary"] = record["abstract"]

        record["purpose"] = first_non_empty(
            (project_description.get("Purpose") or [""])[0],
            (methodology.get("Purpose") or [""])[0],
        )
        record["study_design"] = first_non_empty(
            (methodology.get("Study Design") or [""])[0],
        )
        record["sample"] = first_non_empty(
            (methodology.get("Sampling") or [""])[0],
        )
        record["universe"] = first_non_empty(
            (scope.get("Universe") or [""])[0],
            (methodology.get("Universe") or [""])[0],
        )

        record["language"] = clean_list(ensure_list(jsonld.get("inLanguage") if isinstance(jsonld, dict) else []))
        record["keywords"] = split_semicolon_values(
            ensure_list(jsonld.get("keywords") if isinstance(jsonld, dict) else []) + scope.get("Subject Terms", [])
        )
        record["subjects"] = split_semicolon_values(scope.get("Subject Terms", []))
        record["topics"] = split_semicolon_values(scope.get("Topic", []))

        # ---------- relationships ----------
        record["relationships"]["series"] = clean_list([])
        record["relationships"]["collections"] = clean_list([])
        record["relationships"]["related_datasets"] = clean_list([])

        # ---------- funding ----------
        record["funding"] = parse_funding(jsonld)

        # ---------- time ----------
        raw_collection_dates = clean_list(scope.get("Time Period(s)", []))
        temporal_candidates = ensure_list(jsonld.get("temporalCoverage") if isinstance(jsonld, dict) else []) + raw_collection_dates

        record["time"]["time_method"] = split_semicolon_values(
            methodology.get("Time Method", []) + scope.get("Time Method", [])
        )
        record["time"]["collection_dates"] = raw_collection_dates
        record["time"]["temporal_coverage"] = clean_list(
            [normalize_time_period_value(x) for x in temporal_candidates if clean_text(x)]
        )

        # ---------- coverage ----------
        spatial_jsonld: List[str] = []
        for item in ensure_list(jsonld.get("spatialCoverage") if isinstance(jsonld, dict) else []):
            if isinstance(item, dict):
                spatial_jsonld.append(first_non_empty(item.get("name"), item.get("description")))
            else:
                spatial_jsonld.append(clean_text(item))

        geographic_coverage = clean_list(spatial_jsonld + scope.get("Geographic Coverage", []))
        record["coverage"]["geographic_coverage"] = geographic_coverage
        record["coverage"]["spatial_coverage"] = geographic_coverage

        # ---------- methodology ----------
        analysis_units = split_semicolon_values(
            methodology.get("Unit(s) of Observation", []) + methodology.get("Analysis Unit(s)", [])
        )
        kind_of_data = split_semicolon_values(scope.get("Data Type(s)", []))
        collection_mode = split_semicolon_values(methodology.get("Collection Mode(s)", []))

        record["methodology"]["analysis_units"] = analysis_units
        record["methodology"]["kind_of_data"] = kind_of_data
        record["methodology"]["collection_mode"] = collection_mode
        record["methodology"]["sampling_procedure"] = clean_html_list(methodology.get("Sampling", []))
        record["unit_of_analysis"] = analysis_units[0] if analysis_units else ""

        # ---------- access ----------
        record["access"]["license"] = first_non_empty(
            jsonld.get("license") if isinstance(jsonld, dict) else "",
        )
        record["access"]["license_url"] = record["access"]["license"]
        record["access"]["terms_url"] = record["access"]["license_url"]
        record["access"]["restrictions"] = first_non_empty(
            next((page.get("access_disclaimer") for page in pages.values() if page.get("access_disclaimer")), ""),
        )

        documentation_only_download = False
        access_restricted_data_button = False
        analyze_online = False
        raw_action_signals: List[Dict[str, Any]] = []

        for crawl_key, page in pages.items():
            action_signals = page.get("action_signals", {})
            raw_action_signals.append(
                {
                    "page_key": crawl_key,
                    "signals": action_signals,
                }
            )
            documentation_only_download = documentation_only_download or bool(action_signals.get("documentation_only_download"))
            access_restricted_data_button = access_restricted_data_button or bool(action_signals.get("access_restricted_data_button"))
            analyze_online = analyze_online or bool(action_signals.get("analyze_online"))

        record["access"]["documentation_only_download"] = documentation_only_download
        record["access"]["access_restricted_data_button"] = access_restricted_data_button
        record["access"]["analyze_online"] = analyze_online

        # DataLumos HTML pages do not presently expose a clear restricted-data flag structure
        record["access"]["restricted_data_types"]["idars"] = False
        record["access"]["restricted_data_types"]["useAgreement"] = False
        record["access"]["restricted_data_types"]["restricted"] = False
        record["access"]["restricted_data_types"]["vde"] = False
        record["access"]["restricted_data_types"]["enclave"] = False

        # ---------- notes ----------
        record["notes"]["collection_notes"] = clean_html_list(scope.get("Collection Notes", []))
        record["notes"]["collection_changes"] = clean_html_list(scope.get("Collection Changes", []))

        # ---------- variables ----------
        record["variables"]["count"] = None

        # ---------- publications ----------
        publication_items: List[Dict[str, Any]] = []
        publication_counts: List[int] = []
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
        else:
            record["publications"]["count"] = 0

        # ---------- export ----------
        export_formats: List[str] = []
        raw_export_signals: List[Dict[str, Any]] = []

        for crawl_key, page in pages.items():
            raw_export_signals.append(
                {
                    "page_key": crawl_key,
                    "export_formats": page.get("export_formats", []),
                }
            )
            export_formats.extend(page.get("export_formats", []))

        export_formats = clean_list(export_formats)
        canonical_order = list(EXPORT_FORMAT_PATTERNS.keys())
        record["export_formats"] = [label for label in canonical_order if label in export_formats]

        # ---------- files ----------
        collected_files: List[Dict[str, Any]] = []
        for page in pages.values():
            collected_files.extend(page.get("table_items", {}).get("files", []))

        raw_files = unique_dicts(collected_files)
        unique_files = dedupe_file_entries(raw_files)

        documentation_files = [f for f in unique_files if classify_file_group(f) == "documentation"]
        data_files = [f for f in unique_files if classify_file_group(f) == "data"]
        setup_files = [f for f in unique_files if classify_file_group(f) == "setup"]
        other_files = [f for f in unique_files if classify_file_group(f) == "other"]
        downloadable_files = [f for f in unique_files if f.get("downloadable") is True]

        record["files"] = {
            "endpoint": "",
            "status_code": None,
            "content_type": "text/html",
            "source": "datalumos_html",
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

        # ---------- source_specific ----------
        record["source_specific"]["raw_jsonld"] = jsonld
        record["source_specific"]["raw_meta_tags"] = meta_map
        record["source_specific"]["raw_page_blocks"] = {
            crawl_key: {
                "header": page.get("header", {}),
                "panels": page.get("panels", {}),
                "citation": page.get("citation", ""),
                "versions": page.get("versions", []),
            }
            for crawl_key, page in pages.items()
        }
        record["source_specific"]["raw_export_signals"] = raw_export_signals
        record["source_specific"]["raw_action_signals"] = raw_action_signals
        record["source_specific"]["raw_api_payloads"]["files_api"] = {
            "url": "",
            "status_code": None,
            "content_type": "",
            "note": "DataLumos extractor uses crawled HTML pages and visible file table rows; no files API used.",
        }
        record["source_specific"]["extra"] = {
            "header_values": header_values,
            "citation_text": citation_text,
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
        elif unique_files:
            record["access"]["has_public_files"] = False
        else:
            record["access"]["has_public_files"] = None

        if unique_files and all(f.get("public") is True for f in unique_files):
            record["access"]["open_access"] = True
        elif any(f.get("public") is False for f in unique_files):
            record["access"]["open_access"] = False

        if record["access"]["has_restricted_files"]:
            record["access"]["authentication_required"] = True

    def _finalize_record(self, record: MetadataRecord, pages: Dict[str, PagePackage]) -> None:
        """
        Run final cleanup, shared normalization, and shared validation.

        This is the lock point:
        - DataLumos-specific extraction ends here
        - shared schema normalization starts here
        - final validation guarantees consistent output shape
        """
        if not record["access"]["license"]:
            record["access"]["license"] = record["access"]["license_url"]
        if not record["access"]["terms_url"]:
            record["access"]["terms_url"] = record["access"]["license_url"]

        record["provenance"]["source_urls_visited"] = clean_list(record["provenance"]["source_urls_visited"])
        record["diagnostics"]["metadata_sources"] = clean_list(record["diagnostics"]["metadata_sources"])

        if not record["versions"]:
            record["diagnostics"]["field_warnings"].append("No published versions block was parsed.")
        if not record["files"]["raw_files"]:
            record["diagnostics"]["field_warnings"].append("No file entries were extracted from crawled DataLumos pages.")
        if not record["authors"]:
            record["diagnostics"]["field_warnings"].append("No authors were extracted.")
        if not record["publisher"]:
            record["diagnostics"]["field_warnings"].append("No publisher was extracted.")
        if not record["export_formats"]:
            record["diagnostics"]["field_warnings"].append(
                "No export formats were detected on crawled pages; review export link parsing for this project."
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
def extract_datalumos_metadata(
    source_url: str,
    *,
    save_output_json: bool = DEFAULT_SAVE_OUTPUT_JSON,
    output_dir: str = DEFAULT_OUTPUT_DIR,
    headless: bool = DEFAULT_HEADLESS,
    wait_ms: int = DEFAULT_WAIT_MS,
    request_timeout: int = DEFAULT_REQUEST_TIMEOUT,
    playwright_timeout_ms: int = DEFAULT_PLAYWRIGHT_TIMEOUT_MS,
    max_pages: int = DEFAULT_MAX_PAGES,
    user_agent: str = DEFAULT_USER_AGENT,
) -> MetadataRecord:
    """Public entry point for DataLumos extraction."""
    config = ExtractorConfig(
        source_url=source_url,
        save_output_json=save_output_json,
        output_dir=output_dir,
        headless=headless,
        wait_ms=wait_ms,
        request_timeout=request_timeout,
        playwright_timeout_ms=playwright_timeout_ms,
        max_pages=max_pages,
        user_agent=user_agent,
    )

    with DataLumosExtractor(config=config) as extractor:
        return extractor.extract()


# =========================================================
# EXAMPLE RUN
# =========================================================
if __name__ == "__main__":
    setup_logging()

    try:
        metadata = extract_datalumos_metadata(DEFAULT_SOURCE_URL)
        logging.getLogger("DataLumosExtractor").info(
            "Summary | page_type=%s | project_id=%s | title=%s | doi=%s | raw_files=%s | unique_files=%s",
            metadata["page_type"],
            metadata["study_id"],
            metadata["title"],
            metadata["doi"],
            metadata["files"]["summary"]["raw_entry_count"],
            metadata["files"]["summary"]["unique_file_count"],
        )
    except Exception:
        logging.getLogger("DataLumosExtractor").exception("DataLumos metadata extraction failed")
        raise