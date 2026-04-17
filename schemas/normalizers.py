from __future__ import annotations

import html
import json
import re
from copy import deepcopy
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse, urlunparse, unquote

from bs4 import BeautifulSoup


MetadataRecord = Dict[str, Any]

_DROP_TEXT_VALUES = {"", "hide", "null", "none", "view help"}
_DOI_PREFIX_RE = re.compile(r"^https?://(?:dx\.)?doi\.org/", flags=re.IGNORECASE)
_PLAIN_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$", flags=re.IGNORECASE)


# =========================================================
# BASIC TEXT / VALUE NORMALIZATION
# =========================================================
def flatten_values(value: Any) -> List[Any]:
    """Flatten arbitrarily nested iterables into a one-dimensional list."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        output: List[Any] = []
        for item in value:
            output.extend(flatten_values(item))
        return output
    return [value]


def ensure_list(value: Any) -> List[Any]:
    """Return a value as a list, preserving lists and wrapping scalars."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def clean_text(value: Any) -> str:
    """
    Normalize arbitrary text-like input into a compact single-line string.

    Rules:
    - HTML entities are unescaped
    - non-breaking / zero-width spaces are normalized
    - internal whitespace is collapsed
    """
    if value is None:
        return ""

    text = str(value)
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    text = text.replace("\u200b", " ").replace("\ufeff", " ")
    text = re.sub(r"[\t\r\n]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def strip_html_fragment(value: Any) -> str:
    """Convert a small HTML fragment to visible normalized text."""
    if value is None:
        return ""
    soup = BeautifulSoup(str(value), "html.parser")
    return clean_text(soup.get_text(" ", strip=True))


def normalize_optional_text(value: Any) -> str:
    """Normalize a possibly empty text field."""
    return clean_text(value)


def first_non_empty(*values: Any) -> str:
    """Return the first value that yields non-empty normalized text."""
    for value in values:
        text = clean_text(value)
        if text:
            return text
    return ""


def bool_from_flag(value: Any) -> Optional[bool]:
    """Convert common truthy/falsy flag representations to bool or None."""
    if value in (1, "1", True, "true", "TRUE", "yes", "YES"):
        return True
    if value in (0, "0", False, "false", "FALSE", "no", "NO"):
        return False
    return None


def maybe_json_load(value: Any) -> Any:
    """Safely attempt to parse JSON text; return None on failure."""
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except Exception:
        return None


def normalize_date_like(value: Any) -> str:
    """
    Normalize a date-like field conservatively.

    This function intentionally does not aggressively coerce formats because
    repositories often expose mixed precise/imprecise date values.
    """
    return clean_text(value)


def canonicalize_url(value: Any, *, strip_fragment: bool = True) -> str:
    """Normalize a URL-like string and optionally remove the fragment."""
    text = clean_text(value)
    if not text:
        return ""
    try:
        parsed = urlparse(text)
        if not parsed.scheme or not parsed.netloc:
            return text
        if strip_fragment:
            parsed = parsed._replace(fragment="")
        return urlunparse(parsed)
    except Exception:
        return text


def canonicalize_doi(value: Any) -> str:
    """
    Normalize DOI values to canonical https://doi.org/... form when possible.
    """
    text = clean_text(value)
    if not text:
        return ""

    if _DOI_PREFIX_RE.match(text):
        suffix = _DOI_PREFIX_RE.sub("", text)
        return f"https://doi.org/{suffix}"

    if _PLAIN_DOI_RE.match(text):
        return f"https://doi.org/{text}"

    return text


def safe_filename(value: str) -> str:
    """Create a filesystem-safe filename stem."""
    value = clean_text(value)
    value = re.sub(r"^https?://", "", value)
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value)
    value = value.strip("._")
    return value or "metadata"


def parse_href_filename_and_extension(href: str) -> Tuple[str, str]:
    """Extract filename and lowercase extension from a URL-like href."""
    parsed = urlparse(href)
    path = unquote(parsed.path or "")
    file_name = clean_text(path.split("/")[-1] if path else "")
    extension = ""
    if "." in file_name:
        extension = file_name.rsplit(".", 1)[-1].lower()
    return file_name, extension


# =========================================================
# LIST / DICT NORMALIZATION
# =========================================================
def clean_list(
    values: Iterable[Any],
    *,
    drop_values: Optional[Set[str]] = None,
) -> List[str]:
    """
    Normalize, filter, and de-duplicate a list of text values.
    """
    drop_values = {v.lower() for v in (drop_values or _DROP_TEXT_VALUES)}
    output: List[str] = []

    for value in flatten_values(list(values)):
        text = clean_text(value)
        if not text:
            continue
        if text.lower() in drop_values:
            continue
        if text not in output:
            output.append(text)

    return output


def clean_html_list(
    values: Iterable[Any],
    *,
    drop_values: Optional[Set[str]] = None,
) -> List[str]:
    """
    Normalize, strip HTML, filter, and de-duplicate a list of text values.
    """
    drop_values = {v.lower() for v in (drop_values or _DROP_TEXT_VALUES)}
    output: List[str] = []

    for value in flatten_values(list(values)):
        text = strip_html_fragment(value)
        if not text:
            continue
        if text.lower() in drop_values:
            continue
        if text not in output:
            output.append(text)

    return output


def unique_dicts(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """De-duplicate a list of dictionaries using a JSON-stable key."""
    seen: Set[str] = set()
    output: List[Dict[str, Any]] = []

    for item in items:
        key = json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)
        if key not in seen:
            seen.add(key)
            output.append(item)

    return output


def merge_unique_list(left: Sequence[Any], right: Sequence[Any]) -> List[Any]:
    """Merge two sequences while preserving order and uniqueness."""
    output: List[Any] = []
    for item in list(left) + list(right):
        if item not in output:
            output.append(item)
    return output


def deep_merge_missing(target: Dict[str, Any], defaults: Dict[str, Any]) -> Dict[str, Any]:
    """
    Recursively fill missing keys in target from defaults.

    Existing non-empty values in target are preserved.
    """
    for key, default_value in defaults.items():
        if key not in target:
            target[key] = deepcopy(default_value)
            continue

        target_value = target[key]
        if isinstance(target_value, dict) and isinstance(default_value, dict):
            deep_merge_missing(target_value, default_value)

    return target


def remove_empty_values(value: Any) -> Any:
    """
    Recursively remove empty strings, empty dicts, and empty lists.

    False, 0, and None are preserved because they can be meaningful in metadata.
    """
    if isinstance(value, dict):
        cleaned: Dict[str, Any] = {}
        for key, item in value.items():
            normalized = remove_empty_values(item)
            if normalized == "":
                continue
            if normalized == []:
                continue
            if normalized == {}:
                continue
            cleaned[key] = normalized
        return cleaned

    if isinstance(value, list):
        cleaned_list = []
        for item in value:
            normalized = remove_empty_values(item)
            if normalized == "":
                continue
            if normalized == []:
                continue
            if normalized == {}:
                continue
            cleaned_list.append(normalized)
        return cleaned_list

    return value


# =========================================================
# STRUCTURED ENTRY NORMALIZERS
# =========================================================
def normalize_identifier_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a single identifier entry."""
    normalized = {
        "type": clean_text(entry.get("type")),
        "value": clean_text(entry.get("value")),
        "url": canonicalize_url(entry.get("url")),
    }
    if normalized["type"].lower() == "doi":
        normalized["value"] = canonicalize_doi(normalized["value"])
        normalized["url"] = canonicalize_doi(normalized["url"] or normalized["value"])

    return {k: v for k, v in normalized.items() if v not in ("", None)}


def normalize_contributor_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a single contributor entry."""
    normalized = {
        "name": clean_text(entry.get("name")),
        "role": clean_text(entry.get("role")),
        "affiliation": clean_text(entry.get("affiliation")),
        "orcid": clean_text(entry.get("orcid")),
        "url": canonicalize_url(entry.get("url")),
    }
    return {k: v for k, v in normalized.items() if v not in ("", None)}


def normalize_funding_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a single funding entry."""
    normalized = {
        "funder_name": clean_text(entry.get("funder_name")),
        "identifier": clean_text(entry.get("identifier")),
        "grant_number": clean_text(entry.get("grant_number") or entry.get("identifier")),
        "display": clean_text(entry.get("display")),
        "url": canonicalize_url(entry.get("url")),
    }
    return {k: v for k, v in normalized.items() if v not in ("", None)}


def normalize_publication_item(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a single publication entry."""
    normalized = {
        "title": clean_text(entry.get("title")),
        "doi": canonicalize_doi(entry.get("doi")),
        "journal": clean_text(entry.get("journal")),
        "year": clean_text(entry.get("year")),
        "authors": clean_list(entry.get("authors", [])),
        "url": canonicalize_url(entry.get("url")),
        "citation": clean_text(entry.get("citation")),
    }
    return {k: v for k, v in normalized.items() if v not in ("", None, [])}


def normalize_file_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a single file entry."""
    normalized = deepcopy(entry)

    text_fields = [
        "title",
        "file_name",
        "file_stem",
        "file_format",
        "file_extension",
        "mime_type",
        "file_content",
        "file_category",
        "file_description",
        "checksum",
        "checksum_type",
        "required_for_bundle",
        "resource_uuid",
        "uri",
        "download_url",
        "api_url",
        "parent_path",
        "folder_path",
        "last_modified",
        "display_size",
        "language",
        "source",
        "dataset_identifier",
    ]
    for field in text_fields:
        if field in normalized:
            normalized[field] = clean_text(normalized.get(field))

    url_fields = ["uri", "download_url", "api_url"]
    for field in url_fields:
        if field in normalized:
            normalized[field] = canonicalize_url(normalized.get(field))

    list_fields = ["include_in_bundle", "dataset_format_flags", "terms_of_use"]
    for field in list_fields:
        normalized[field] = clean_list(normalized.get(field, []))

    bool_fields = ["public", "listed_on_public_page", "downloadable", "deliver_download"]
    for field in bool_fields:
        if field in normalized:
            normalized[field] = bool_from_flag(normalized.get(field))

    return normalized


# =========================================================
# METADATA RECORD NORMALIZATION
# =========================================================
_TOP_LEVEL_TEXT_FIELDS = [
    "schema_version",
    "source_url",
    "final_url",
    "page_title",
    "page_type",
    "repository",
    "repository_url",
    "study_id",
    "title",
    "subtitle",
    "doi",
    "version",
    "published_date",
    "modified_date",
    "created_date",
    "deposit_date",
    "release_date",
    "summary",
    "abstract",
    "purpose",
    "study_design",
    "sample",
    "universe",
    "citation",
    "publisher",
    "unit_of_analysis",
    "generated_at_utc",
]

_TOP_LEVEL_LIST_FIELDS = [
    "authors",
    "author_affiliations",
    "distributors",
    "keywords",
    "subjects",
    "topics",
    "language",
    "export_formats",
]

_DATE_SECTION_FIELDS = [
    "created_date",
    "published_date",
    "modified_date",
    "deposit_date",
    "release_date",
]

_TIME_SECTION_LIST_FIELDS = [
    "time_method",
    "collection_dates",
    "temporal_coverage",
]

_COVERAGE_SECTION_LIST_FIELDS = [
    "geographic_coverage",
    "spatial_coverage",
]

_METHODOLOGY_SECTION_LIST_FIELDS = [
    "analysis_units",
    "kind_of_data",
    "collection_mode",
    "sampling_procedure",
]

_NOTES_SECTION_LIST_FIELDS = [
    "collection_notes",
    "collection_changes",
]

_ACCESS_TEXT_FIELDS = [
    "license",
    "license_url",
    "terms_url",
    "restrictions",
]

_ACCESS_BOOL_FIELDS = [
    "open_access",
    "authentication_required",
    "has_restricted_files",
    "has_public_files",
    "has_downloadable_files",
    "documentation_only_download",
    "access_restricted_data_button",
    "analyze_online",
]

_RESTRICTED_ACCESS_BOOL_FIELDS = [
    "idars",
    "useAgreement",
    "restricted",
    "vde",
    "enclave",
]

_PROVENANCE_TEXT_FIELDS = [
    "repository",
    "repository_url",
    "page_type",
    "parser_version",
    "schema_version",
]

_DIAGNOSTICS_LIST_FIELDS = [
    "fetch_warnings",
    "field_warnings",
    "validation_warnings",
    "metadata_sources",
]


def normalize_metadata_record(record: MetadataRecord) -> MetadataRecord:
    """
    Normalize a unified metadata record in place and return it.

    This is the shared final cleanup step every extractor should call before
    validation and persistence.
    """
    normalized = deepcopy(record)

    # ---------- top-level text ----------
    for field in _TOP_LEVEL_TEXT_FIELDS:
        if field in normalized:
            normalized[field] = clean_text(normalized.get(field))

    # ---------- canonical DOI ----------
    if "doi" in normalized:
        normalized["doi"] = canonicalize_doi(normalized.get("doi"))

    # ---------- top-level URLs ----------
    for field in ["source_url", "final_url", "repository_url"]:
        if field in normalized:
            normalized[field] = canonicalize_url(normalized.get(field))

    # ---------- top-level simple lists ----------
    for field in _TOP_LEVEL_LIST_FIELDS:
        normalized[field] = clean_list(normalized.get(field, []))

    # ---------- identifiers ----------
    identifiers = normalized.setdefault("identifiers", {})
    identifiers["doi"] = canonicalize_doi(identifiers.get("doi") or normalized.get("doi"))
    identifiers["doi_canonical"] = canonicalize_doi(
        identifiers.get("doi_canonical") or identifiers.get("doi") or normalized.get("doi")
    )
    identifiers["study_id"] = clean_text(identifiers.get("study_id") or normalized.get("study_id"))
    identifiers["source_record_id"] = clean_text(
        identifiers.get("source_record_id") or identifiers.get("study_id") or normalized.get("study_id")
    )

    other_identifiers = []
    for item in ensure_list(identifiers.get("other_identifiers")):
        if isinstance(item, dict):
            normalized_item = normalize_identifier_entry(item)
            if normalized_item:
                other_identifiers.append(normalized_item)
    identifiers["other_identifiers"] = unique_dicts(other_identifiers)

    # ---------- contributors ----------
    contributors = []
    for item in ensure_list(normalized.get("contributors")):
        if isinstance(item, dict):
            normalized_item = normalize_contributor_entry(item)
            if normalized_item:
                contributors.append(normalized_item)
    normalized["contributors"] = unique_dicts(contributors)

    # ---------- funding ----------
    funding = []
    for item in ensure_list(normalized.get("funding")):
        if isinstance(item, dict):
            normalized_item = normalize_funding_entry(item)
            if normalized_item:
                funding.append(normalized_item)
    normalized["funding"] = unique_dicts(funding)

    # ---------- dates ----------
    dates = normalized.setdefault("dates", {})
    for field in _DATE_SECTION_FIELDS:
        dates[field] = normalize_date_like(dates.get(field) or normalized.get(field))

    if not normalized.get("published_date"):
        normalized["published_date"] = dates.get("published_date", "")
    if not normalized.get("modified_date"):
        normalized["modified_date"] = dates.get("modified_date", "")

    # ---------- time ----------
    time_section = normalized.setdefault("time", {})
    for field in _TIME_SECTION_LIST_FIELDS:
        time_section[field] = clean_list(time_section.get(field, []))

    # ---------- coverage ----------
    coverage = normalized.setdefault("coverage", {})
    for field in _COVERAGE_SECTION_LIST_FIELDS:
        coverage[field] = clean_list(coverage.get(field, []))

    # ---------- methodology ----------
    methodology = normalized.setdefault("methodology", {})
    for field in _METHODOLOGY_SECTION_LIST_FIELDS:
        methodology[field] = clean_list(methodology.get(field, []))

    # ---------- access ----------
    access = normalized.setdefault("access", {})
    for field in _ACCESS_TEXT_FIELDS:
        access[field] = clean_text(access.get(field))

    for field in ["license_url", "terms_url"]:
        access[field] = canonicalize_url(access.get(field))

    for field in _ACCESS_BOOL_FIELDS:
        access[field] = bool_from_flag(access.get(field))

    restricted_types = access.setdefault("restricted_data_types", {})
    for field in _RESTRICTED_ACCESS_BOOL_FIELDS:
        restricted_types[field] = bool_from_flag(restricted_types.get(field))

    # ---------- notes ----------
    notes = normalized.setdefault("notes", {})
    for field in _NOTES_SECTION_LIST_FIELDS:
        notes[field] = clean_html_list(notes.get(field, []))

    # ---------- variables ----------
    variables = normalized.setdefault("variables", {})
    variables["overview"] = clean_text(variables.get("overview"))
    if "count" in variables and variables["count"] == "":
        variables["count"] = None

    # ---------- publications ----------
    publications = normalized.setdefault("publications", {})
    normalized_items = []
    for item in ensure_list(publications.get("items")):
        if isinstance(item, dict):
            normalized_item = normalize_publication_item(item)
            if normalized_item:
                normalized_items.append(normalized_item)
    publications["items"] = unique_dicts(normalized_items)

    # ---------- files ----------
    files = normalized.setdefault("files", {})
    file_bucket_names = [
        "raw_files",
        "unique_files",
        "downloadable_files",
        "documentation_files",
        "data_files",
        "setup_files",
        "other_files",
    ]
    for bucket in file_bucket_names:
        normalized_bucket = []
        for item in ensure_list(files.get(bucket)):
            if isinstance(item, dict):
                normalized_bucket.append(normalize_file_entry(item))
        files[bucket] = normalized_bucket

    normalized_filesets = []
    for fileset in ensure_list(files.get("filesets")):
        if not isinstance(fileset, dict):
            continue
        normalized_fileset = deepcopy(fileset)
        for field in ["identifier", "title", "description"]:
            if field in normalized_fileset:
                normalized_fileset[field] = clean_text(normalized_fileset.get(field))
        normalized_fileset["files"] = [
            normalize_file_entry(item)
            for item in ensure_list(normalized_fileset.get("files"))
            if isinstance(item, dict)
        ]
        normalized_filesets.append(normalized_fileset)
    files["filesets"] = normalized_filesets

    files["endpoint"] = canonicalize_url(files.get("endpoint"))
    files["content_type"] = clean_text(files.get("content_type"))
    files["source"] = clean_text(files.get("source"))
    files["counting_basis"] = clean_text(files.get("counting_basis"))

    summary = files.setdefault("summary", {})
    for key, value in list(summary.items()):
        if isinstance(value, dict):
            cleaned_dict = {}
            for subkey, subvalue in value.items():
                cleaned_key = clean_text(subkey)
                cleaned_dict[cleaned_key] = subvalue
            summary[key] = cleaned_dict

    # ---------- relationships ----------
    relationships = normalized.setdefault("relationships", {})
    relationships["series"] = clean_list(relationships.get("series", []))
    relationships["collections"] = clean_list(relationships.get("collections", []))
    relationships["related_datasets"] = clean_list(relationships.get("related_datasets", []))
    relationships["source_documents"] = clean_list(relationships.get("source_documents", []))

    # ---------- provenance ----------
    provenance = normalized.setdefault("provenance", {})
    for field in _PROVENANCE_TEXT_FIELDS:
        provenance[field] = clean_text(provenance.get(field))

    provenance["source_urls_visited"] = [
        canonicalize_url(url) for url in clean_list(provenance.get("source_urls_visited", []))
    ]
    provenance["fetch_methods"] = clean_list(provenance.get("fetch_methods", []))

    html_pages_fetched = []
    for item in ensure_list(provenance.get("html_pages_fetched")):
        if isinstance(item, dict):
            html_pages_fetched.append(
                {
                    "page_name": clean_text(item.get("page_name")),
                    "requested_url": canonicalize_url(item.get("requested_url")),
                    "final_url": canonicalize_url(item.get("final_url")),
                    "fetch_method": clean_text(item.get("fetch_method")),
                    "status_code": item.get("status_code"),
                }
            )
    provenance["html_pages_fetched"] = html_pages_fetched

    api_endpoints_called = []
    for item in ensure_list(provenance.get("api_endpoints_called")):
        if isinstance(item, dict):
            api_endpoints_called.append(
                {
                    "name": clean_text(item.get("name")),
                    "url": canonicalize_url(item.get("url")),
                    "status_code": item.get("status_code"),
                    "content_type": clean_text(item.get("content_type")),
                }
            )
    provenance["api_endpoints_called"] = api_endpoints_called

    # ---------- diagnostics ----------
    diagnostics = normalized.setdefault("diagnostics", {})
    for field in _DIAGNOSTICS_LIST_FIELDS:
        diagnostics[field] = clean_list(diagnostics.get(field, []))

    completeness = diagnostics.setdefault("completeness", {})
    completeness["missing_required_fields"] = clean_list(completeness.get("missing_required_fields", []))
    required_present = completeness.get("required_fields_present")
    completeness["required_fields_present"] = bool_from_flag(required_present)

    # ---------- source_specific ----------
    source_specific = normalized.setdefault("source_specific", {})
    source_specific.setdefault("raw_jsonld", None)
    source_specific.setdefault("raw_meta_tags", {})
    source_specific.setdefault("raw_api_payloads", {})
    source_specific.setdefault("raw_page_blocks", {})
    source_specific.setdefault("raw_export_signals", [])
    source_specific.setdefault("raw_action_signals", [])
    source_specific.setdefault("extra", {})

    return normalized