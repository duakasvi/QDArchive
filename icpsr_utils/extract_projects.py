import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlencode, urljoin

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# =========================================================
# CONFIG
# =========================================================
BASE_SEARCH_URL = "https://www.icpsr.umich.edu/web/ICPSR/search/studies"

SAVE_OUTPUT_JSON = False
OUTPUT_DIR = "KASVI/search_outputs"

SAVE_DEBUG_HTML_ON_FAILURE = True
DEBUG_HTML_DIR = "KASVI/search_debug_html"

HEADLESS = True
ROWS_PER_PAGE = 200

REQUEST_TIMEOUT_SEC = 45
PLAYWRIGHT_TIMEOUT_MS = 45000
RENDER_WAIT_MS = 6000
MAX_RETRIES_PER_PAGE = 2

BROWSER_VIEWPORT = {"width": 1440, "height": 2200}
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)

LOG_LEVEL = logging.INFO
LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

logger = logging.getLogger(__name__)


def setup_logging(level: int = LOG_LEVEL) -> None:
    """
    Configure application logging.

    This function is safe to call multiple times. It only configures the root
    logger if no handlers are already attached.
    """
    root_logger = logging.getLogger()

    if not root_logger.handlers:
        logging.basicConfig(
            level=level,
            format=LOG_FORMAT,
            datefmt=LOG_DATE_FORMAT,
        )
    else:
        root_logger.setLevel(level)


# =========================================================
# GENERAL HELPERS
# =========================================================
def now_utc() -> str:
    """Return the current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat()


def clean_text(value: Any) -> str:
    """Normalize whitespace and return a clean string."""
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def clean_list(values: List[Any]) -> List[str]:
    """Normalize a list of values into a deduplicated list of clean strings."""
    output: List[str] = []
    for value in values:
        text = clean_text(value)
        if text and text not in output:
            output.append(text)
    return output


def ensure_list(value: Any) -> List[Any]:
    """Wrap a single value into a list; return empty list for None."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def safe_filename(value: str) -> str:
    """Convert a string into a filesystem-safe filename stem."""
    value = clean_text(value)
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value)
    value = value.strip("._")
    return value or "search_results"


def normalize_keyword(keyword: Optional[str]) -> str:
    """
    Normalize the search keyword.

    Rules:
    - None, empty string, or '*' => search all studies
    - otherwise use trimmed keyword as-is
    """
    if keyword is None:
        return "*"

    keyword = clean_text(keyword)
    if not keyword or keyword == "*":
        return "*"

    return keyword


def build_search_url(keyword: str, start: int, rows: int) -> str:
    """
    Build the ICPSR search URL.

    Parameters:
        keyword: Search term or '*' for all studies.
        start: Zero-based offset.
        rows: Number of results requested on this page.
    """
    params = {
        "q": keyword,
        "start": start,
        "rows": rows,
    }
    return f"{BASE_SEARCH_URL}?{urlencode(params)}"


def build_requests_session() -> requests.Session:
    """
    Build a reusable HTTP session for public search pages.
    """
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )
    return session


def save_debug_html(html: str, keyword: str, start: int, reason: str) -> Optional[str]:
    """
    Save failed/degraded HTML for inspection.
    """
    if not SAVE_DEBUG_HTML_ON_FAILURE:
        return None

    os.makedirs(DEBUG_HTML_DIR, exist_ok=True)
    stem = safe_filename(f"{keyword}_start_{start}_{reason}")
    path = os.path.join(DEBUG_HTML_DIR, f"{stem}.html")

    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(html or "")
        return path
    except Exception as exc:
        logger.debug("Could not save debug HTML: %s", exc)
        return None


# =========================================================
# SEARCH RESULTS JSON EXTRACTION
# =========================================================
SEARCH_RESULT_MARKERS = [
    "searchResults : ",
    "searchResults: ",
    "searchResults = ",
    '"searchResults":',
    '"searchResults" :',
    "window.searchResults = ",
    "window.searchResults= ",
]


def extract_balanced_json_value(text: str, marker: str) -> Dict[str, Any]:
    """
    Extract a balanced JSON object or array from text starting after a marker.
    """
    marker_idx = text.find(marker)
    if marker_idx == -1:
        raise ValueError(f"Could not find marker: {marker}")

    i = marker_idx + len(marker)
    while i < len(text) and text[i].isspace():
        i += 1

    if i >= len(text):
        raise ValueError(f"No JSON payload after marker: {marker}")

    opening = text[i]
    if opening not in "{[":
        raise ValueError(f"Marker found but payload does not start with JSON object/array: {marker}")

    closing = "}" if opening == "{" else "]"

    depth = 0
    in_string = False
    escape = False

    for j in range(i, len(text)):
        ch = text[j]

        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == opening:
                depth += 1
            elif ch == closing:
                depth -= 1
                if depth == 0:
                    json_text = text[i:j + 1]
                    parsed = json.loads(json_text)

                    if isinstance(parsed, dict):
                        return parsed

                    if isinstance(parsed, list):
                        return {
                            "response": {
                                "numFound": len(parsed),
                                "docs": parsed,
                            }
                        }

                    raise ValueError("Parsed JSON payload is neither dict nor list.")

    raise ValueError("Could not parse balanced JSON payload.")


def extract_study_links_from_html(html: str) -> List[Dict[str, Any]]:
    """
    Fallback HTML parser.

    When the embedded searchResults JSON is missing, extract visible study links
    directly from the rendered HTML. This fallback is intentionally minimal:
    it guarantees study URL + study ID + title when possible.
    """
    soup = BeautifulSoup(html, "html.parser")
    docs: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, str]] = set()

    for anchor in soup.find_all("a", href=True):
        href = clean_text(anchor.get("href"))
        absolute_url = urljoin(BASE_SEARCH_URL, href)

        match = re.search(r"/web/ICPSR/studies/(\d+)", absolute_url)
        if not match:
            continue

        study_id = match.group(1)
        title = clean_text(anchor.get_text(" ", strip=True))
        if not title:
            continue

        key = (study_id, title)
        if key in seen:
            continue
        seen.add(key)

        docs.append(
            {
                "ID": int(study_id),
                "TITLE": title,
                "URL": absolute_url,
                "AUTHOR": [],
                "DATEUPDATED": "",
                "OWNER": "",
                "DOI": "",
                "ARCHIVE": "ICPSR",
                "PUBLISH_STATUS": "",
            }
        )

    return docs


def parse_search_results_from_html(html: str) -> Dict[str, Any]:
    """
    Parse the embedded search results object from page HTML.

    Tries known marker variations first.
    Falls back to visible HTML study links if JSON is absent.
    """
    last_error: Optional[Exception] = None

    for marker in SEARCH_RESULT_MARKERS:
        try:
            return extract_balanced_json_value(html, marker)
        except Exception as exc:
            last_error = exc
            logger.debug("Marker did not match: %s | error=%s", marker, exc)

    fallback_docs = extract_study_links_from_html(html)
    if fallback_docs:
        logger.info(
            "Embedded search JSON not found; falling back to visible study-link extraction | docs=%s",
            len(fallback_docs),
        )
        return {
            "response": {
                "numFound": len(fallback_docs),
                "docs": fallback_docs,
            },
            "_fallback_parse_mode": "html_link_fallback",
        }

    raise ValueError(f"Could not extract search results JSON or fallback study links. Last error: {last_error}")


def extract_total_found(search_results: Dict[str, Any]) -> int:
    """
    Extract the total number of matching studies from the parsed search JSON.
    """
    response = search_results.get("response", {})
    num_found = response.get("numFound")
    if isinstance(num_found, int):
        return num_found

    for key_path in [
        ("pagination", "numFound"),
        ("meta", "total"),
        ("total",),
    ]:
        current: Any = search_results
        ok = True
        for key in key_path:
            if not isinstance(current, dict) or key not in current:
                ok = False
                break
            current = current[key]
        if ok and isinstance(current, int):
            return current

    return 0


def extract_docs(search_results: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Extract the search result docs list from the parsed search JSON.
    """
    response = search_results.get("response", {})
    docs = response.get("docs", [])
    if isinstance(docs, list):
        return docs
    return []


# =========================================================
# STUDY NORMALIZATION
# =========================================================
def build_result_url(doc: Dict[str, Any]) -> str:
    """
    Build the final study URL from a search result document.

    Priority:
    1. Use explicit `URL` if present.
    2. Fall back to ICPSR study page using study ID.
    """
    explicit_url = clean_text(doc.get("URL"))
    if explicit_url:
        return explicit_url

    study_id = doc.get("ID")
    if study_id is not None:
        return f"https://www.icpsr.umich.edu/web/ICPSR/studies/{study_id}"

    return ""


def normalize_search_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert a raw search result document into a clean, stable record.
    """
    authors = doc.get("AUTHOR")
    if authors is None and isinstance(doc.get("AUTHOR_SPLIT"), list):
        authors = doc.get("AUTHOR_SPLIT")

    record = {
        "study_id": doc.get("ID"),
        "title": clean_text(doc.get("TITLE")),
        "author": clean_list(ensure_list(authors)),
        "dateupdated": clean_text(doc.get("DATEUPDATED")),
        "owner": clean_text(doc.get("OWNER")),
        "url": build_result_url(doc),
        "doi": clean_text(doc.get("DOI")),
        "archive": clean_text(doc.get("ARCHIVE")),
        "publish_status": clean_text(doc.get("PUBLISH_STATUS")),
    }

    return record


def deduplicate_studies(studies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Deduplicate normalized study records while preserving order.
    """
    seen = set()
    output: List[Dict[str, Any]] = []

    for study in studies:
        key = make_study_identity_key(study)
        if key in seen:
            continue
        seen.add(key)
        output.append(study)

    return output


def make_study_identity_key(study: Dict[str, Any]) -> Tuple[str, str, str]:
    """
    Build a stable identity key for one normalized study record.
    """
    return (
        clean_text(study.get("study_id")),
        clean_text(study.get("url")),
        clean_text(study.get("title")),
    )


def build_page_signature(studies: List[Dict[str, Any]]) -> Tuple[Tuple[str, str, str], ...]:
    """
    Build a stable page signature from normalized study records.

    This is used to detect repeated pages caused by pagination bugs.
    """
    return tuple(make_study_identity_key(study) for study in studies)


def split_new_and_duplicate_studies(
    studies: List[Dict[str, Any]],
    seen_study_keys: Set[Tuple[str, str, str]],
) -> Tuple[List[Dict[str, Any]], int]:
    """
    Split one page's studies into:
    - new studies not yet seen
    - duplicate count already seen in previous pages
    """
    new_studies: List[Dict[str, Any]] = []
    duplicate_count = 0

    for study in studies:
        key = make_study_identity_key(study)
        if key in seen_study_keys:
            duplicate_count += 1
        else:
            new_studies.append(study)

    return new_studies, duplicate_count


def summarize_page_titles(studies: List[Dict[str, Any]], limit: int = 3) -> List[str]:
    """
    Return the first few titles from a page for debug logging.
    """
    titles = [clean_text(study.get("title")) for study in studies if clean_text(study.get("title"))]
    return titles[:limit]


# =========================================================
# PAGE FETCHING
# =========================================================
def html_contains_search_payload(html: str) -> bool:
    """
    Return True when the HTML appears to contain an embedded search payload.
    """
    lowered = html.lower()
    for marker in SEARCH_RESULT_MARKERS:
        if marker.lower() in lowered:
            return True
    return False


def html_looks_like_degraded_page(html: str) -> bool:
    """
    Detect degraded ICPSR search pages that do not contain the real search app.
    """
    lowered = html.lower()
    return (
        "error loading remote resources" in lowered
        or "please enable javascript in your browser" in lowered
        or "javascript is required to use the core functionality" in lowered
        or "chrome-error://chromewebdata/" in lowered
    )


def html_is_usable_search_page(html: str) -> bool:
    """
    Decide whether the HTML is usable for parsing.

    Accept either:
    - embedded JSON payload
    - visible study links
    """
    if not html or len(html) < 1000:
        return False

    if html_contains_search_payload(html):
        return True

    if extract_study_links_from_html(html):
        return True

    return False


def fetch_search_page_html_via_requests(session: requests.Session, url: str) -> Optional[str]:
    """
    Try fetching the public search page via requests first.
    """
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT_SEC, allow_redirects=True)
        response.raise_for_status()
        html = response.text or ""

        if html_is_usable_search_page(html):
            logger.debug("Requests fetch succeeded and returned usable HTML | len=%s | url=%s", len(html), url)
            return html

        logger.debug(
            "Requests fetch returned unusable HTML | len=%s | degraded=%s | url=%s",
            len(html),
            html_looks_like_degraded_page(html),
            url,
        )
        return html

    except Exception as exc:
        logger.debug("Requests fetch failed for URL=%s | error=%s", url, exc)
        return None


def fetch_search_page_html_via_playwright(page, url: str) -> str:
    """
    Fetch a search results page in Playwright and return rendered HTML.

    Uses relaxed loading to reduce timeout risk on slow pages.
    """
    last_error: Optional[Exception] = None

    for attempt in range(1, MAX_RETRIES_PER_PAGE + 1):
        try:
            logger.info(
                "Fetching page with Playwright (attempt %s/%s): %s",
                attempt,
                MAX_RETRIES_PER_PAGE,
                url,
            )

            page.goto(url, wait_until="commit", timeout=PLAYWRIGHT_TIMEOUT_MS)

            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except PlaywrightTimeoutError:
                logger.debug("domcontentloaded wait timed out for URL: %s", url)

            page.wait_for_timeout(RENDER_WAIT_MS)

            html = page.content()
            if not html or len(html) < 1000:
                raise ValueError("Rendered HTML looks incomplete.")

            if html_is_usable_search_page(html):
                logger.debug("Playwright fetch returned usable HTML | len=%s | url=%s", len(html), url)
                return html

            if html_looks_like_degraded_page(html):
                raise ValueError("Rendered HTML is degraded and missing the real search app.")

            raise ValueError("Rendered HTML did not contain search payload or fallback study links.")

        except Exception as exc:
            last_error = exc
            logger.warning("Failed to fetch page with Playwright %s: %s", url, exc)

    raise RuntimeError(f"Could not fetch search page after retries: {last_error}")


def fetch_search_page_html(session: requests.Session, page, url: str) -> str:
    """
    Robust public search-page fetcher.

    Strategy:
    1. Try requests first.
    2. If requests returned usable HTML, use it.
    3. Otherwise fall back to Playwright.
    """
    requests_html = fetch_search_page_html_via_requests(session, url)

    if requests_html and html_is_usable_search_page(requests_html):
        logger.info("Using requests HTML for search page: %s", url)
        return requests_html

    logger.info("Requests fetch was not usable; falling back to Playwright for URL: %s", url)
    return fetch_search_page_html_via_playwright(page, url)


# =========================================================
# SAVE HELPERS
# =========================================================
def save_search_output(result: Dict[str, Any], normalized_keyword: str) -> Optional[str]:
    """
    Save search output JSON to disk if SAVE_OUTPUT_JSON is enabled.

    Returns:
        Output path if saved, otherwise None.
    """
    if not SAVE_OUTPUT_JSON:
        logger.info("SAVE_OUTPUT_JSON is False; skipping file save")
        return None

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    file_stem = "all_studies" if normalized_keyword == "*" else safe_filename(normalized_keyword)
    output_path = os.path.join(OUTPUT_DIR, f"icpsr_search_{file_stem}.json")

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    logger.info("Saved output JSON: %s", output_path)
    return output_path


# =========================================================
# MAIN SEARCH LOGIC
# =========================================================
def search_icpsr_studies(keyword: Optional[str]) -> Dict[str, Any]:
    """
    Search ICPSR studies for a keyword and return a full JSON-ready result.

    Parameters:
        keyword:
            - a normal search keyword such as "qualitative"
            - "*" or empty/None to fetch all available studies

    Returns:
        A dictionary with:
        - summary
        - studies

    Duplicate pagination guard:
    - stops if a page signature exactly repeats
    - stops if a page contains zero new studies compared with already collected pages
    """
    normalized_keyword = normalize_keyword(keyword)

    all_studies: List[Dict[str, Any]] = []
    seen_study_keys: Set[Tuple[str, str, str]] = set()
    seen_page_signatures: Set[Tuple[Tuple[str, str, str], ...]] = set()

    total_found: Optional[int] = None
    pages_fetched = 0
    start = 0

    duplicate_guard_triggered = False
    duplicate_guard_reason = ""
    duplicate_guard_start: Optional[int] = None

    logger.info("Starting search for keyword: %s", normalized_keyword)

    session = build_requests_session()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            args=[
                "--disable-blink-features=AutomationControlled",
            ],
        )
        page = browser.new_page(
            viewport=BROWSER_VIEWPORT,
            user_agent=DEFAULT_USER_AGENT,
        )

        try:
            while True:
                search_url = build_search_url(
                    keyword=normalized_keyword,
                    start=start,
                    rows=ROWS_PER_PAGE,
                )

                html = fetch_search_page_html(session, page, search_url)

                try:
                    search_results = parse_search_results_from_html(html)
                except Exception as exc:
                    debug_path = save_debug_html(
                        html=html,
                        keyword=normalized_keyword,
                        start=start,
                        reason="parse_failure",
                    )
                    extra = f" Debug HTML saved to: {debug_path}" if debug_path else ""
                    raise ValueError(f"Could not parse search page at start={start}: {exc}.{extra}") from exc

                docs = extract_docs(search_results)
                current_total_found = extract_total_found(search_results)

                logger.debug(
                    "Parsed page start=%s | docs_count=%s | current_total_found=%s",
                    start,
                    len(docs),
                    current_total_found,
                )

                if total_found is None:
                    total_found = current_total_found
                    logger.info("Total studies reported by search: %s", total_found)
                elif current_total_found and total_found != current_total_found:
                    logger.debug(
                        "Search total changed across pages | original_total=%s | current_total=%s | start=%s",
                        total_found,
                        current_total_found,
                        start,
                    )

                if not docs:
                    logger.info("No more docs found on this page. Stopping pagination.")
                    break

                normalized_docs = [normalize_search_doc(doc) for doc in docs]
                normalized_docs = [doc for doc in normalized_docs if clean_text(doc.get("url"))]
                page_signature = build_page_signature(normalized_docs)

                logger.debug(
                    "Page start=%s | first_titles=%s",
                    start,
                    summarize_page_titles(normalized_docs),
                )

                # -------------------------------------------------
                # Duplicate guard 1: exact repeated page signature
                # -------------------------------------------------
                if page_signature in seen_page_signatures:
                    duplicate_guard_triggered = True
                    duplicate_guard_reason = "repeated_page_signature"
                    duplicate_guard_start = start

                    logger.warning(
                        "Duplicate pagination guard triggered at start=%s | reason=%s | stopping pagination",
                        start,
                        duplicate_guard_reason,
                    )
                    break

                new_docs, duplicate_count_on_page = split_new_and_duplicate_studies(
                    normalized_docs,
                    seen_study_keys,
                )

                logger.debug(
                    "Page start=%s | page_docs=%s | new_docs=%s | duplicates_on_page=%s",
                    start,
                    len(normalized_docs),
                    len(new_docs),
                    duplicate_count_on_page,
                )

                # -------------------------------------------------
                # Duplicate guard 2: page contains zero new studies
                # -------------------------------------------------
                if start > 0 and not new_docs:
                    duplicate_guard_triggered = True
                    duplicate_guard_reason = "page_contains_zero_new_studies"
                    duplicate_guard_start = start

                    logger.warning(
                        "Duplicate pagination guard triggered at start=%s | reason=%s | stopping pagination",
                        start,
                        duplicate_guard_reason,
                    )
                    break

                seen_page_signatures.add(page_signature)

                for study in new_docs:
                    seen_study_keys.add(make_study_identity_key(study))

                all_studies.extend(new_docs)
                pages_fetched += 1

                logger.info(
                    "Fetched page %s | start=%s | page_docs=%s | new_docs=%s | duplicates_on_page=%s | accumulated=%s",
                    pages_fetched,
                    start,
                    len(docs),
                    len(new_docs),
                    duplicate_count_on_page,
                    len(all_studies),
                )

                start += ROWS_PER_PAGE

                if total_found is not None and total_found > 0 and len(all_studies) >= total_found:
                    logger.info("Collected all reported studies. Pagination complete.")
                    break

        finally:
            try:
                browser.close()
            finally:
                session.close()

    deduped_studies = deduplicate_studies(all_studies)

    if total_found is None or total_found <= 0:
        total_found = len(deduped_studies)

    result = {
        "summary": {
            "keyword_input": keyword if keyword is not None else "",
            "keyword_normalized": normalized_keyword,
            "total_studies": total_found,
            "studies_extracted": len(deduped_studies),
            "pages_fetched": pages_fetched,
            "rows_per_page": ROWS_PER_PAGE,
            "search_url_example": build_search_url(normalized_keyword, 0, ROWS_PER_PAGE),
            "duplicate_guard_triggered": duplicate_guard_triggered,
            "duplicate_guard_reason": duplicate_guard_reason,
            "duplicate_guard_start": duplicate_guard_start,
            "generated_at_utc": now_utc(),
        },
        "studies": deduped_studies,
    }

    save_search_output(result, normalized_keyword)

    logger.info(
        "Finished search | total_studies=%s | studies_extracted=%s | pages_fetched=%s | duplicate_guard_triggered=%s",
        result["summary"]["total_studies"],
        result["summary"]["studies_extracted"],
        result["summary"]["pages_fetched"],
        result["summary"]["duplicate_guard_triggered"],
    )

    return result


# =========================================================
# EXAMPLE RUN
# =========================================================
if __name__ == "__main__":
    setup_logging()

    try:
        output = search_icpsr_studies("qualitative")
        logger.info("Search summary: %s", json.dumps(output["summary"], ensure_ascii=False))
    except Exception:
        logger.exception("ICPSR study search failed")
        raise