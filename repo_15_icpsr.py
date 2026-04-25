import importlib
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from icpsr_utils.icpsr_public_downloader import (
    ALLOWED_FILE_STATUSES,
    DownloadConfig,
    download_public_study_assets,
)


# =========================================================
# CONFIGURATION
# =========================================================
@dataclass(frozen=True)
class PipelineConfig:
    """
    Main qualitative-identification pipeline for ICPSR-family repositories.

    This version performs:
    1. study discovery
    2. metadata extraction
    3. qualitative filtering
    4. download attempt for every qualitative project
       (even if extractor did not detect files)
    5. DB persistence for projects + versions + files
    6. summary generation

    Qualitative rule:
    - PASS if ANY enabled signal is true:
        a) summary + abstract merged text
        b) kind_of_data
        c) collection_mode
    - No file-level matching
    - No numeric-only shield
    """

    # -----------------------------------------------------
    # Locked module integration
    # -----------------------------------------------------
    study_search_module: str = "icpsr_utils.extract_projects"
    study_search_callable: str = "search_icpsr_studies"

    metadata_module: str = "icpsr_utils.extract_metadata"
    metadata_callable: str = "extract_metadata_by_url"

    # -----------------------------------------------------
    # Search config
    # -----------------------------------------------------
    query: Optional[List[str]] = None
    per_page: int = 1000

    # -----------------------------------------------------
    # Qualitative rule flags
    # -----------------------------------------------------
    use_kind_of_data: bool = True
    use_collection_mode: bool = True

    # -----------------------------------------------------
    # Download config
    # -----------------------------------------------------
    enable_downloads: bool = True
    download_headless: bool = True
    download_timeout_ms: int = 90000
    download_max_size_bytes: int = 5 * 1024 * 1024 * 1024
    download_profile_dirname: str = "icpsr_browser_profile"

    # -----------------------------------------------------
    # IO / storage
    # -----------------------------------------------------
    base_dir: str = "downloads"
    repo_name: str = "icpsr"
    db_path: str = "23048230-seeding.db"
    summary_filename: str = "icpsr_repo15_summary.json"

    # -----------------------------------------------------
    # Repository config
    # -----------------------------------------------------
    repository_id: int = 15
    repository_url: str = "https://www.icpsr.umich.edu"
    download_method: str = "SCRAPING"

    # -----------------------------------------------------
    # Logging
    # -----------------------------------------------------
    log_level: int = logging.INFO
    log_format: str = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    log_date_format: str = "%Y-%m-%d %H:%M:%S"

    @property
    def script_dir(self) -> str:
        return os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()

    @property
    def schema_path(self) -> str:
        return os.path.join(self.script_dir, "schemas", "schema.sql")

    @property
    def summary_path(self) -> str:
        return os.path.join(self.script_dir, self.summary_filename)

    @property
    def download_profile_dir(self) -> str:
        return os.path.join(self.script_dir, self.download_profile_dirname)


QUERY: List[str] = [
    # ─── SECTION 1 — Direct API / Search Box Terms ────────────────────────────
    "qualitative",
    "qualitative data",
    "qualitative interview",
    "qualitative research",
    "qualitative study",
    "qualitative methods",
    "qualitative analysis",
    "qualitative mixed methods",
    "interview transcript",
    "interview transcripts",
    "focus group",
    "focus group transcript",
    "focus group interview",
    "in-depth interview",
    "semi-structured interview",
    "unstructured interview",
    "structured interview",
    "oral history",
    "oral history transcript",
    "life history",
    "life history interview",
    "narrative interview",
    "biographical interview",
    "ethnographic interview",
    "key informant interview",
    "elite interview",
    "cognitive interview",
    "phone interview",
    "telephone interview",
    "video interview",
    "recorded interview",
    "field notes",
    "fieldnotes",
    "ethnography",
    "ethnographic",
    "ethnographic fieldwork",
    "participant observation",
    "naturalistic observation",
    "structured observation",
    "observational study",
    "diary study",
    "diary data",
    "open-ended responses",
    "open-ended questions",
    "narrative data",
    "narrative analysis",
    "grounded theory",
    "thematic analysis",
    "content analysis",
    "discourse analysis",
    "phenomenology",
    "phenomenological",
    "case study",
    "case studies",
    "mixed methods",
    "text data",
    "verbatim",
    "verbatim transcript",
    "interview guide",
    "interview protocol",
    "topic guide",

    # ─── SECTION 2 — ICPSR Download Format Labels ─────────────────────────────
    "Qualitative Data",
    "Documentation Only",
    "Qualitative Product Suite",
    "raw transcript",
    "annotated transcript",
    "interview notes",
    "interview audio",
    "interview video",

    # ─── SECTION 3 — ICPSR Data Type Filter Values ────────────────────────────
    "qualitative data type",
    "mixed-method",
    "administrative data",
    "video data qualitative",
    "social media qualitative",

    # ─── SECTION 4 — ICPSR Restriction Type Targets ───────────────────────────
    "public use qualitative",
    "public data qualitative",
    "restricted use qualitative",
    "secure download qualitative",
    "Virtual Data Enclave qualitative",
    "open access qualitative",

    # ─── SECTION 5 — ICPSR Thematic Collections ───────────────────────────────
    "Health and Medical Care Archive",
    "HMCA qualitative",
    "National Archive of Criminal Justice Data",
    "NACJD qualitative",
    "National Addiction HIV Data Archive Program",
    "NAHDAP qualitative",
    "Resource Center for Minority Data",
    "RCMD qualitative",
    "Child Care Early Education Research Connections",
    "Data Sharing for Demographic Research",
    "DSDR qualitative",
    "National Neighborhood Data Archive",
    "NaNDA qualitative",
    "National Archive of Data on Arts and Culture",
    "NADAC qualitative",
    "Social Media Archive ICPSR",
    "SOMAR qualitative",
    "College and Beyond II",
    "Qualitative Data Sharing Project Series",
    "QDS Project Series",
    "DataLumos qualitative",
    "openICPSR qualitative",
    "Robert Wood Johnson Foundation Archive",
    "RWJF qualitative",
    "Correlates of War qualitative",
    "COVID-19 qualitative",
    "terrorism qualitative",
    "education qualitative",
    "aging qualitative",
    "criminal justice qualitative",
    "substance abuse qualitative",
    "mental health qualitative",
    "child care qualitative",
    "population health qualitative",
    "health disparities qualitative",

    # ─── SECTION 6 — ICPSR Subject Thesaurus Terms ────────────────────────────
    "interviews",
    "focus groups",
    "oral histories",
    "life histories",
    "field research",
    "qualitative methods",
    "ethnographic methods",
    "case study methods",
    "narrative methods",
    "coding qualitative",
    "data collection qualitative",
    "research methodology qualitative",
    "mixed methods research",
    "behavioral sciences",
    "social sciences qualitative",
    "political science qualitative",
    "sociology qualitative",
    "anthropology qualitative",
    "economics qualitative",
    "demography qualitative",
    "history qualitative",
    "gerontology qualitative",
    "criminal justice qualitative interview",
    "public health qualitative",
    "law qualitative",
    "international relations qualitative",
    "education qualitative interview",
    "psychology qualitative",
    "social work qualitative",
    "communications qualitative",
    "gender studies qualitative",
    "race ethnicity qualitative",
    "minority populations qualitative",
    "immigration qualitative",
    "poverty qualitative",
    "inequality qualitative",
    "community qualitative",
    "family qualitative",
    "children qualitative",
    "youth qualitative",
    "aging qualitative interview",
    "elderly qualitative",
    "health behavior qualitative",
    "mental health qualitative interview",
    "substance abuse qualitative interview",
    "drug use qualitative",
    "alcohol use qualitative",
    "violence qualitative",
    "crime qualitative",
    "victimization qualitative",
    "incarceration qualitative",
    "housing qualitative",
    "employment qualitative",
    "labor market qualitative",
    "income qualitative",
    "welfare qualitative",
    "voting qualitative",
    "political participation qualitative",
    "civic engagement qualitative",
    "public opinion qualitative",
    "religion qualitative",
    "identity qualitative",
    "sexuality qualitative",
    "disability qualitative",
    "trauma qualitative",
    "resilience qualitative",
    "caregiving qualitative",
    "social networks qualitative",
    "social capital qualitative",

    # ─── SECTION 7 — ICPSR Qualitative Methodology Types ─────────────────────
    "in-depth unstructured interview",
    "focus group interview qualitative",
    "unstructured diary",
    "semi-structured diary",
    "naturalistic observation qualitative",
    "participant observation workplace",
    "participant observation community",
    "structured observation qualitative",
    "meeting minutes qualitative",
    "official records qualitative",
    "medical records qualitative",
    "news sources qualitative",
    "social media text qualitative",
    "open-ended survey comments",
    "open-ended questionnaire qualitative",
    "courtroom observation",
    "classroom observation",
    "healthcare observation",
    "online gaming observation",
    "community policing observation",
    "nightclub ethnography",
    "diary interview",
    "audio recording qualitative",
    "video recording qualitative",

    # ─── SECTION 8 — ICPSR File Type Search Terms ─────────────────────────────
    "nvp qualitative",
    "nvpx qualitative",
    "qdpx qualitative",
    "txt transcript",
    "rtf transcript",
    "pdf transcript",
    "docx interview",
    "doc interview",
    "zip qualitative",
    "codebook qualitative",
    "README qualitative",
    "FocusGroup transcript",
    "FieldNotes qualitative",
    "InterviewGuide qualitative",

    # ─── SECTION 9 — ICPSR Curation & Metadata Terms ─────────────────────────
    "ICPSR study qualitative",
    "ICPSR curated qualitative",
    "ICPSR curation qualitative",
    "Qualitative Product Suite",
    "disclosure risk review",
    "de-identification qualitative",
    "anonymization protocol",
    "IRB approval qualitative",
    "informed consent qualitative",
    "data use agreement qualitative",
    "data management plan qualitative",
    "principal investigator qualitative",
    "CoreTrustSeal qualitative",
    "DDI metadata qualitative",
    "Data Documentation Initiative qualitative",
    "QuaDS Software",
    "QDS Toolkit",
    "DOI qualitative",
    "persistent identifier qualitative",
    "ICPSR Bibliography qualitative",
    "data-related publication qualitative",
    "ICPSR subject terms qualitative",
    "study-level metadata qualitative",

    # ─── SECTION 10 — Funding Agency Keywords ─────────────────────────────────
    "NIH qualitative",
    "National Institutes of Health qualitative",
    "NHGRI qualitative interview",
    "National Human Genome Research Institute qualitative",
    "NIMH qualitative",
    "National Institute of Mental Health qualitative",
    "NIDA qualitative",
    "National Institute on Drug Abuse qualitative",
    "NIAAA qualitative",
    "NICHD qualitative",
    "NSF qualitative",
    "National Science Foundation qualitative",
    "NIJ qualitative",
    "National Institute of Justice qualitative",
    "Bureau of Justice Statistics qualitative",
    "CDC qualitative",
    "Robert Wood Johnson Foundation qualitative",
    "Mellon Foundation qualitative",
    "Russell Sage Foundation qualitative",
    "MacArthur Foundation qualitative",
    "Ford Foundation qualitative",
    "Spencer Foundation qualitative",
    "Annie E. Casey Foundation qualitative",
    "William T. Grant Foundation qualitative",
    "Department of Justice qualitative",
    "DHHS qualitative",
    "AHRQ qualitative",
    "SAMHSA qualitative",
    "Department of Education qualitative",
    "Institute of Education Sciences qualitative",
    "AERA qualitative",
    "American Educational Research Association qualitative",

    # ─── SECTION 11 — Thematic + Method Compound Terms ───────────────────────
    "qualitative interview health",
    "qualitative interview education",
    "qualitative interview aging",
    "qualitative interview criminal justice",
    "qualitative interview substance abuse",
    "qualitative interview mental health",
    "qualitative interview poverty",
    "qualitative interview race",
    "qualitative interview immigration",
    "qualitative interview family",
    "qualitative interview community",
    "qualitative interview HIV",
    "qualitative interview cancer",
    "qualitative interview disability",
    "qualitative interview trauma",
    "qualitative interview violence",
    "qualitative interview incarceration",
    "qualitative interview housing",
    "qualitative interview employment",
    "qualitative interview women",
    "qualitative interview youth",
    "qualitative interview children",
    "focus group health",
    "focus group education",
    "focus group substance abuse",
    "focus group mental health",
    "focus group criminal justice",
    "oral history political",
    "oral history civil rights",
    "oral history veterans",
    "oral history immigration",
    "ethnography workplace",
    "ethnography community",
    "ethnography school",
    "ethnography prison",
    "ethnography hospital",
    "life history poverty",
    "life history aging",
    "life history immigration",
    "narrative health",
    "narrative illness",
    "narrative identity",
    "narrative education",
    "interview transcript health",
    "interview transcript criminal justice",
    "interview transcript education",
    "mixed methods health",
    "mixed methods education",
    "mixed methods aging",

    # ─── SECTION 12 — Named ICPSR Qualitative Study Series ───────────────────
    "Qualitative Data Sharing Project Series",
    "College and Beyond II qualitative",
    "Workplace Ethnography Project",
    "Family Life Project qualitative",
    "Vermont Study on Aid-in-Dying",
    "Identity Formation Social Problems Estonia Ukraine Uzbekistan",
    "Prostate Cancer Risk Young Black Men",
    "Generalist-Specialist Palliative Care Social Work",
    "Barriers Facilitators Treatment Psychiatric Traumatic Brain Injury",
    "Young Women Leaders Program",
    "Racialized Cues Justice Reinvestment",
    "Bullying Violence School Bus",
    "Teacher Quality Grants Texas",
    "Qualitative Data Sharing QDS",

    # ─── SECTION 13 — Scraper-Specific Filter Strings ────────────────────────
    "qualitative -openicpsr",
    "interview transcript -openicpsr",
    "focus group -openicpsr",
    "oral history -openicpsr",
    "ethnography -openicpsr",
    "qualitative public data",
    "qualitative restricted data",
    "qualitative interview public use",
    "qualitative interview restricted use",
    "qualitative interview member only",

    # ─── SECTION 14 — Access Method Keywords ─────────────────────────────────
    "public use file qualitative",
    "free download qualitative",
    "restricted-use file qualitative",
    "ICPSR member qualitative",
    "secure download qualitative data",
    "virtual data enclave interview",
    "physical data enclave qualitative",
    "data use agreement interview",
    "ICPSR Data Access Request System",
    "embargo qualitative",
    "delayed release qualitative",

    # ─── SECTION 15 — Documentation File Keywords ────────────────────────────
    "interview roster",
    "participant demographics qualitative",
    "sampling frame qualitative",
    "consent form qualitative",
    "IRB protocol qualitative",
    "data collection instrument qualitative",
    "interview schedule qualitative",
    "moderator guide",
    "focus group guide",
    "observation protocol",
    "field guide qualitative",
    "coding manual",
    "annotation guide qualitative",
    "analytic memo qualitative",
    "transcription conventions",
    "transcription notes",
    "anonymization log",
    "redaction log",
    "replacement key qualitative",
    "de-identification notes",
    "README qualitative data",
    "study overview qualitative",
    "methods report qualitative",
    "data preparation notes qualitative",
    "data file structure qualitative",
    "depositor notes qualitative",
    "processing notes qualitative",
]

CONFIG = PipelineConfig(
    query=QUERY,
    use_kind_of_data=True,
    use_collection_mode=True,
    download_headless=False,
)


# =========================================================
# LOGGING SETUP
# =========================================================
logger = logging.getLogger(__name__)


def setup_logging(level: int = CONFIG.log_level) -> None:
    """
    Configure application logging.

    Safe to call multiple times.
    """
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        logging.basicConfig(
            level=level,
            format=CONFIG.log_format,
            datefmt=CONFIG.log_date_format,
        )
    else:
        root_logger.setLevel(level)


# =========================================================
# CONSTANTS
# =========================================================
PAGE_TYPE_DISCRIMINATOR: Dict[str, int] = {
    "icpsr": 1,
    "openicpsr": 2,
    "datalumos": 3,
}

DOWNLOAD_STATUS_BUCKETS: Dict[str, str] = {
    "SUCCEEDED": "download_succeeded",
    "FAILED_LOGIN_REQUIRED": "download_failed_login_required",
    "FAILED_SERVER_UNRESPONSIVE": "download_failed_server_unresponsive",
    "FAILED_TOO_LARGE": "download_failed_too_large",
}

SUMMARY_KEYWORDS: List[str] = [
    "qualitative",
    "qualitative interview",
    "interview",
    "focus group",
    "transcript",
    "semi structured interview",
    "unstructured interview",
    "in depth interview",
    "key informant interview",
    "field note",
    "participant observation",
    "ethnograph",
    "oral history",
    "life history",
    "narrative",
    "thematic analysis",
    "open ended response",
]

KIND_OF_DATA_KEYWORDS: List[str] = [
    "text",
    "textual",
    "qualitative",
    "transcript",
    "narrative text",
]

COLLECTION_MODE_KEYWORDS: List[str] = [
    "interview",
    "telephone interview",
    "in depth interview",
    "focus group",
    "observation",
    "participant observation",
    "ethnograph",
    "oral history",
]


# =========================================================
# GENERAL HELPERS
# =========================================================
def now_utc_iso() -> str:
    """
    Return the current UTC timestamp in ISO 8601 format.
    """
    return datetime.now(timezone.utc).isoformat()


def clean_text(value: Any) -> str:
    """
    Normalize whitespace and return a clean string.
    """
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def flatten_values(value: Any) -> List[Any]:
    """
    Flatten nested values into a simple list.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        output: List[Any] = []
        for item in value:
            output.extend(flatten_values(item))
        return output
    return [value]


def clean_list(values: Iterable[Any]) -> List[str]:
    """
    Normalize, filter empties, and deduplicate string values.
    """
    output: List[str] = []
    for value in flatten_values(list(values)):
        text = clean_text(value)
        if not text:
            continue
        if text not in output:
            output.append(text)
    return output


def unique_list(values: Sequence[Any]) -> List[Any]:
    """
    Preserve order while deduplicating values.
    """
    seen: Set[str] = set()
    output: List[Any] = []
    for value in values:
        key = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
        if key in seen:
            continue
        seen.add(key)
        output.append(value)
    return output


def first_non_empty(*values: Any) -> str:
    """
    Return the first non-empty cleaned string.
    """
    for value in values:
        text = clean_text(value)
        if text:
            return text
    return ""


def safe_folder_name(name: str) -> str:
    """
    Convert an arbitrary string into a folder-safe name.
    """
    name = clean_text(name).lower()
    name = re.sub(r"[\\/]+", "-", name)
    name = re.sub(r"[^a-z0-9._-]+", "-", name)
    return name.strip("-_")


def get_search_terms(query: Optional[List[str]]) -> List[str]:
    """
    Normalize configured query values into a list of search terms.

    Rules:
    - None or [] => search all studies using '*'
    - string => wrap in list
    - list => return as list
    """
    if query is None or query == []:
        return ["*"]
    if isinstance(query, str):
        return [query]
    return list(query)


def join_text_parts(parts: Iterable[Any]) -> str:
    """
    Join multiple text values into one normalized string.
    """
    cleaned = [clean_text(part) for part in flatten_values(list(parts)) if clean_text(part)]
    return " ".join(cleaned).strip()


def singularize_token(token: str) -> str:
    """
    Convert very common plural surface forms to a singular-ish token.
    """
    token = clean_text(token).lower()
    if len(token) <= 3:
        return token
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("ses") and len(token) > 4:
        return token[:-2]
    if token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def normalize_for_match(value: Any) -> str:
    """
    Normalize text for conservative keyword matching.
    """
    text = clean_text(value).lower()
    text = text.replace("_", " ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    tokens = [singularize_token(token) for token in text.split()]
    return " ".join(tokens).strip()


def find_phrase_matches(text: Any, phrases: Sequence[str]) -> List[str]:
    """
    Return base phrases that appear in normalized text.
    """
    haystack = normalize_for_match(text)
    if not haystack:
        return []

    matches: List[str] = []
    for phrase in phrases:
        normalized_phrase = normalize_for_match(phrase)
        if normalized_phrase and normalized_phrase in haystack:
            matches.append(phrase)
    return unique_list(matches)


def normalize_url_for_dedupe(url: str) -> str:
    """
    Normalize a study URL for deduplication.
    """
    text = clean_text(url)
    text = re.sub(r"/+$", "", text)
    return text


def parse_int(value: Any) -> Optional[int]:
    """
    Parse an integer when possible.
    """
    text = clean_text(value)
    if not text:
        return None
    match = re.search(r"\d+", text)
    if not match:
        return None
    try:
        return int(match.group(0))
    except Exception:
        return None


def get_page_type(metadata: Dict[str, Any]) -> str:
    """
    Return normalized page_type from metadata.
    """
    return clean_text(metadata.get("page_type")).lower()


def get_file_extension(file_name: str) -> str:
    """
    Return lowercase extension or 'unknown'.
    """
    text = clean_text(file_name)
    if not text or "." not in text:
        return "unknown"
    return text.rsplit(".", 1)[-1].lower().strip()


def build_minimal_downloader_metadata(metadata: Dict[str, Any], study_url: str) -> Dict[str, Any]:
    """
    Build a minimal downloader metadata object.

    This intentionally strips extractor file hints so the downloader behaves
    like the direct standalone test that already succeeded.
    """
    source_or_final = first_non_empty(
        metadata.get("final_url"),
        metadata.get("source_url"),
        study_url,
    )
    study_id = clean_text(metadata.get("study_id")) or (
        re.search(r"/studies/(\d+)", source_or_final).group(1)
        if re.search(r"/studies/(\d+)", source_or_final)
        else ""
    )

    return {
        "source_url": source_or_final,
        "final_url": source_or_final,
        "study_id": study_id,
        "page_type": get_page_type(metadata) or "icpsr",
        "version": first_non_empty(metadata.get("version"), "1.0"),
        "files": {
            "project_download_options": [],
            "filesets": [],
            "unique_files": [],
        },
    }


# =========================================================
# LOCKED MODULE ADAPTERS
# =========================================================
def load_callable(module_path: str, callable_name: str) -> Callable[..., Any]:
    """
    Dynamically import and return a callable.
    """
    module = importlib.import_module(module_path)
    fn = getattr(module, callable_name)
    if not callable(fn):
        raise TypeError(f"Loaded object is not callable: {module_path}.{callable_name}")
    return fn


def call_study_search(search_fn: Callable[..., Any], keyword: str) -> Any:
    """
    Call the locked study-search extractor using a few common signatures.
    """
    attempts = [
        lambda: search_fn(keyword),
        lambda: search_fn(query=keyword),
        lambda: search_fn(keyword=keyword),
        lambda: search_fn(search_term=keyword),
        lambda: search_fn(query_string=keyword),
        lambda: search_fn(keywords=[keyword]),
    ]

    last_error: Optional[Exception] = None
    for attempt in attempts:
        try:
            return attempt()
        except TypeError as exc:
            last_error = exc
            continue

    raise TypeError(
        "Could not call the locked study-search extractor with a supported signature. "
        f"Last error: {last_error}"
    )


def call_metadata_extractor(metadata_fn: Callable[..., Any], study_url: str) -> Dict[str, Any]:
    """
    Call the locked metadata extractor using a few common signatures.
    """
    attempts = [
        lambda: metadata_fn(study_url),
        lambda: metadata_fn(url=study_url),
        lambda: metadata_fn(source_url=study_url),
        lambda: metadata_fn(study_url=study_url),
    ]

    last_error: Optional[Exception] = None
    for attempt in attempts:
        try:
            result = attempt()
            if not isinstance(result, dict):
                raise TypeError("Metadata extractor must return a dict.")
            return result
        except TypeError as exc:
            last_error = exc
            continue

    raise TypeError(
        "Could not call the locked metadata extractor with a supported signature. "
        f"Last error: {last_error}"
    )


def normalize_search_results(raw_result: Any) -> List[Dict[str, Any]]:
    """
    Normalize the locked study-search output into a list of study items.

    Accepted shapes:
    - list[str]
    - list[dict]
    - dict with one of: items/results/studies/data
    """
    if isinstance(raw_result, list):
        items = raw_result
    elif isinstance(raw_result, dict):
        items = (
            raw_result.get("items")
            or raw_result.get("results")
            or raw_result.get("studies")
            or raw_result.get("data")
            or []
        )
    else:
        items = []

    normalized: List[Dict[str, Any]] = []
    for item in items:
        if isinstance(item, str):
            study_url = clean_text(item)
            if study_url:
                normalized.append({"study_url": study_url})
            continue

        if isinstance(item, dict):
            study_url = first_non_empty(
                item.get("study_url"),
                item.get("project_url"),
                item.get("url"),
                item.get("source_url"),
                item.get("final_url"),
                item.get("persistent_url"),
            )
            if study_url:
                normalized.append({**item, "study_url": study_url})

    return normalized


# =========================================================
# DATABASE LAYER
# =========================================================
class DatabaseManager:
    """
    Thin wrapper around SQLite persistence for this pipeline.
    """

    def __init__(self, db_path: str, schema_path: str) -> None:
        self.db_path = db_path
        self.schema_path = schema_path
        self.conn = sqlite3.connect(self.db_path)
        self.cur = self.conn.cursor()

    def initialize_schema(self) -> None:
        """
        Load and execute the schema.sql file.
        """
        with open(self.schema_path, "r", encoding="utf-8") as f:
            self.cur.executescript(f.read())
        self.conn.commit()

    def close(self) -> None:
        """
        Close the database connection.
        """
        self.conn.close()

    def commit(self) -> None:
        """
        Commit the current transaction.
        """
        self.conn.commit()

    def rollback(self) -> None:
        """
        Roll back the current transaction.
        """
        self.conn.rollback()

    def clear_project_rows(self, project_key: int) -> None:
        """
        Delete all rows linked to a project_key so inserts remain idempotent.
        """
        self.cur.execute("DELETE FROM files WHERE project_key = ?", (project_key,))
        self.cur.execute("DELETE FROM project_versions WHERE project_key = ?", (project_key,))
        self.cur.execute("DELETE FROM keywords WHERE project_key = ?", (project_key,))
        self.cur.execute("DELETE FROM person_role WHERE project_key = ?", (project_key,))
        self.cur.execute("DELETE FROM licenses WHERE project_key = ?", (project_key,))
        self.cur.execute("DELETE FROM failures WHERE project_key = ?", (project_key,))
        self.cur.execute("DELETE FROM projects WHERE project_key = ?", (project_key,))

    def save_project(self, project: Dict[str, Any]) -> None:
        """
        Save one project and its related keyword/person/license records.
        """
        self.cur.execute(
            """
            INSERT OR REPLACE INTO projects (
                project_key, project_id, query_string, repository_id, repository_url, project_url,
                title, description, language, doi, upload_date, download_date,
                download_repository_folder, download_project_folder, download_method
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project["project_key"],
                project["project_id"],
                project["query_string"],
                project["repository_id"],
                project["repository_url"],
                project["project_url"],
                project["title"],
                project["description"],
                project["language"],
                project["doi"],
                project["upload_date"],
                project["download_date"],
                project["download_repository_folder"],
                project["download_project_folder"],
                project["download_method"],
            ),
        )

        for kw in project.get("keywords", []):
            self.cur.execute(
                "INSERT INTO keywords (project_key, keyword) VALUES (?, ?)",
                (project["project_key"], kw),
            )

        for name in sorted(set(project.get("authors", []))):
            self.cur.execute(
                "INSERT INTO person_role (project_key, name, role) VALUES (?, ?, ?)",
                (project["project_key"], name, "AUTHOR"),
            )

        for name in sorted(set(project.get("contacts", []))):
            self.cur.execute(
                "INSERT INTO person_role (project_key, name, role) VALUES (?, ?, ?)",
                (project["project_key"], name, "CONTACT"),
            )

        for name in sorted(set(project.get("producers", []))):
            self.cur.execute(
                "INSERT INTO person_role (project_key, name, role) VALUES (?, ?, ?)",
                (project["project_key"], name, "PRODUCER"),
            )

        for name in sorted(set(project.get("contributors", []))):
            self.cur.execute(
                "INSERT INTO person_role (project_key, name, role) VALUES (?, ?, ?)",
                (project["project_key"], name, "CONTRIBUTOR"),
            )

        if project.get("license"):
            self.cur.execute(
                "INSERT INTO licenses (project_key, license) VALUES (?, ?)",
                (project["project_key"], project["license"]),
            )

    def save_project_version(self, version_meta: Dict[str, Any]) -> int:
        """
        Save one project version and return its database row id.
        """
        self.cur.execute(
            """
            INSERT INTO project_versions (
                project_key, version_id, version, version_state,
                publication_date, release_time, download_date, download_version_folder
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_meta["project_key"],
                version_meta["version_id"],
                version_meta["version"],
                version_meta["version_state"],
                version_meta["publication_date"],
                version_meta["release_time"],
                version_meta["download_date"],
                version_meta["download_version_folder"],
            ),
        )
        return int(self.cur.lastrowid)

    def save_files(self, project_key: int, version_key: int, file_rows: List[Dict[str, Any]]) -> None:
        """
        Save downloader-produced file rows into the fixed professor schema.
        """
        for row in file_rows:
            file_name = clean_text(row.get("file_name"))
            file_type = clean_text(row.get("file_type")) or get_file_extension(file_name)
            status = clean_text(row.get("status"))

            if not file_name:
                continue
            if status not in ALLOWED_FILE_STATUSES:
                status = "FAILED_SERVER_UNRESPONSIVE"
            if not file_type:
                file_type = "unknown"

            self.cur.execute(
                """
                INSERT INTO files (project_key, version_key, file_name, file_type, status)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    project_key,
                    version_key,
                    file_name,
                    file_type,
                    status,
                ),
            )


# =========================================================
# QUALITATIVE SIGNAL HELPERS
# =========================================================
def merge_summary_and_abstract(metadata: Dict[str, Any]) -> str:
    """
    Merge summary and abstract into one qualitative-search text field.
    """
    summary = clean_text(metadata.get("summary"))
    abstract = clean_text(metadata.get("abstract"))

    if summary and abstract and normalize_for_match(summary) == normalize_for_match(abstract):
        return summary
    return join_text_parts([summary, abstract])


def get_kind_of_data_values(metadata: Dict[str, Any]) -> List[str]:
    """
    Extract kind_of_data values from unified metadata.
    """
    methodology = metadata.get("methodology") or {}
    return clean_list(methodology.get("kind_of_data") or [])


def get_collection_mode_values(metadata: Dict[str, Any]) -> List[str]:
    """
    Extract collection_mode values from unified metadata.
    """
    methodology = metadata.get("methodology") or {}
    return clean_list(methodology.get("collection_mode") or [])


def metadata_has_files(metadata: Dict[str, Any]) -> bool:
    """
    Decide whether metadata reports any file inventory.

    This is used only for summary classification now.
    It no longer blocks download attempts.
    """
    files = metadata.get("files") or {}
    unique_files = files.get("unique_files") or []
    project_download_options = files.get("project_download_options") or []

    return bool(unique_files) or bool(project_download_options)


def detect_summary_signal(merged_summary_text: str) -> Dict[str, Any]:
    """
    Detect qualitative signal in merged summary + abstract.
    """
    matches = find_phrase_matches(merged_summary_text, SUMMARY_KEYWORDS)
    return {
        "matched": bool(matches),
        "matches": matches,
    }


def detect_kind_of_data_signal(kind_values: List[str]) -> Dict[str, Any]:
    """
    Detect qualitative signal in kind_of_data values.
    """
    text = join_text_parts(kind_values)
    matches = find_phrase_matches(text, KIND_OF_DATA_KEYWORDS)
    return {
        "matched": bool(matches),
        "matches": matches,
    }


def detect_collection_mode_signal(collection_values: List[str]) -> Dict[str, Any]:
    """
    Detect qualitative signal in collection_mode values.
    """
    text = join_text_parts(collection_values)
    matches = find_phrase_matches(text, COLLECTION_MODE_KEYWORDS)
    return {
        "matched": bool(matches),
        "matches": matches,
    }


def decide_qualitative_pass(
    *,
    summary_pass: bool,
    kind_pass: bool,
    collection_pass: bool,
    use_kind_of_data: bool,
    use_collection_mode: bool,
) -> Tuple[bool, str]:
    """
    Final study-pass decision.

    Rule:
    - PASS if any enabled signal is true:
        summary OR kind OR collection
    """
    enabled_kind_pass = use_kind_of_data and kind_pass
    enabled_collection_pass = use_collection_mode and collection_pass

    if summary_pass:
        return True, "summary_signal_pass"
    if enabled_kind_pass:
        return True, "kind_signal_pass"
    if enabled_collection_pass:
        return True, "collection_signal_pass"

    enabled_signals = ["summary"]
    if use_kind_of_data:
        enabled_signals.append("kind")
    if use_collection_mode:
        enabled_signals.append("collection")

    return False, f"no_enabled_signal_matched:{'+'.join(enabled_signals)}"


def evaluate_qualitative_checklist(metadata: Dict[str, Any], config: PipelineConfig) -> Dict[str, Any]:
    """
    Evaluate the complete qualitative checklist.
    """
    summary_text = merge_summary_and_abstract(metadata)
    kind_values = get_kind_of_data_values(metadata)
    collection_values = get_collection_mode_values(metadata)

    summary_signal = detect_summary_signal(summary_text)
    kind_signal = detect_kind_of_data_signal(kind_values)
    collection_signal = detect_collection_mode_signal(collection_values)

    qualitative_pass, decision_reason = decide_qualitative_pass(
        summary_pass=summary_signal["matched"],
        kind_pass=kind_signal["matched"],
        collection_pass=collection_signal["matched"],
        use_kind_of_data=config.use_kind_of_data,
        use_collection_mode=config.use_collection_mode,
    )

    return {
        "summary_signal": summary_signal,
        "kind_signal": kind_signal,
        "collection_signal": collection_signal,
        "qualitative_pass": qualitative_pass,
        "decision_reason": decision_reason,
        "summary_text": summary_text,
        "kind_values": kind_values,
        "collection_values": collection_values,
        "use_kind_of_data": config.use_kind_of_data,
        "use_collection_mode": config.use_collection_mode,
    }


# =========================================================
# METADATA NORMALIZATION FOR DB
# =========================================================
def normalize_version_value(raw_version: Any) -> str:
    """
    Normalize source version labels.

    Examples:
    - V1 -> 1.0
    - V2 -> 2.0
    - 1 -> 1.0
    - 1.1 -> 1.1
    - V1.2 -> 1.2
    """
    text = clean_text(raw_version).upper()
    if not text:
        return "1.0"

    text = text.lstrip("V").strip()
    if re.fullmatch(r"\d+", text):
        return f"{text}.0"
    if re.fullmatch(r"\d+\.\d+", text):
        return text

    numbers = re.findall(r"\d+", text)
    if not numbers:
        return "1.0"
    if len(numbers) == 1:
        return f"{numbers[0]}.0"
    return f"{numbers[0]}.{numbers[1]}"


def make_download_version_folder(version: str) -> str:
    """
    Convert normalized version value into a version folder name.
    """
    normalized = normalize_version_value(version)
    return f"V{normalized}"


def infer_release_state(publication_date: str) -> Optional[str]:
    """
    Return version state based on publication date availability.
    """
    return "RELEASED" if clean_text(publication_date) else None


def make_project_key(repository_id: int, page_type: str, study_id: Any) -> int:
    """
    Build a stable integer project key.
    """
    discriminator = PAGE_TYPE_DISCRIMINATOR.get(clean_text(page_type).lower(), 9)
    study_id_text = clean_text(study_id)
    if study_id_text.isdigit():
        return int(f"{repository_id}{discriminator}{study_id_text}")

    fallback_digits = re.sub(r"\D+", "", str(abs(hash(study_id_text))))[:12] or "0"
    return int(f"{repository_id}{discriminator}{fallback_digits}")


def build_project_folder_name(metadata: Dict[str, Any]) -> str:
    """
    Build a stable project download folder name.
    """
    study_id = clean_text(metadata.get("study_id") or "unknown")
    page_type = clean_text(metadata.get("page_type") or "study")
    return safe_folder_name(f"{study_id}_{page_type}")


def extract_project_people(metadata: Dict[str, Any]) -> Dict[str, List[str]]:
    """
    Extract project people into AUTHOR / CONTACT / PRODUCER / CONTRIBUTOR buckets.
    """
    authors = clean_list(metadata.get("authors") or [])
    contacts: List[str] = []
    producers: List[str] = []
    contributors: List[str] = []

    for contributor in metadata.get("contributors") or []:
        if not isinstance(contributor, dict):
            continue
        name = clean_text(contributor.get("name"))
        role = normalize_for_match(contributor.get("role"))
        if not name:
            continue
        if "contact" in role:
            contacts.append(name)
        elif "producer" in role:
            producers.append(name)
        elif "author" in role or "creator" in role:
            authors.append(name)
        else:
            contributors.append(name)

    return {
        "authors": clean_list(authors),
        "contacts": clean_list(contacts),
        "producers": clean_list(producers),
        "contributors": clean_list(contributors),
    }


def normalize_language_for_db(metadata: Dict[str, Any]) -> str:
    """
    Normalize language values to a single DB field.
    """
    language = metadata.get("language") or []
    if isinstance(language, str):
        return clean_text(language)
    return "; ".join(clean_list(language))


def normalize_license_for_db(metadata: Dict[str, Any]) -> str:
    """
    Normalize license value for the licenses table.
    """
    access = metadata.get("access") or {}
    return first_non_empty(
        access.get("license"),
        access.get("license_url"),
        access.get("terms_url"),
    )


def normalize_project_metadata_for_db(
    metadata: Dict[str, Any],
    config: PipelineConfig,
    query_string: str,
    checklist: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Convert unified metadata into the DB project shape.
    """
    page_type = clean_text(metadata.get("page_type")).lower()
    study_id = parse_int(metadata.get("study_id"))
    if study_id is None:
        raise ValueError(f"Missing or non-numeric study_id for metadata source: {metadata.get('source_url')}")

    people = extract_project_people(metadata)
    project_key = make_project_key(config.repository_id, page_type, study_id)

    published_date = first_non_empty(
        metadata.get("published_date"),
        (metadata.get("dates") or {}).get("published_date"),
        (metadata.get("dates") or {}).get("release_date"),
        (metadata.get("dates") or {}).get("created_date"),
    )

    return {
        "project_key": project_key,
        "project_id": study_id,
        "query_string": query_string,
        "repository_id": config.repository_id,
        "repository_url": config.repository_url,
        "project_url": first_non_empty(metadata.get("final_url"), metadata.get("source_url")),
        "title": clean_text(metadata.get("title")),
        "description": clean_text(checklist.get("summary_text")),
        "language": normalize_language_for_db(metadata),
        "doi": first_non_empty(metadata.get("doi"), (metadata.get("identifiers") or {}).get("doi")),
        "upload_date": published_date,
        "download_date": now_utc_iso(),
        "download_repository_folder": config.repo_name,
        "download_project_folder": build_project_folder_name(metadata),
        "download_method": config.download_method,
        "keywords": clean_list(metadata.get("keywords") or []),
        "authors": people["authors"],
        "contacts": people["contacts"],
        "producers": people["producers"],
        "contributors": people["contributors"],
        "license": normalize_license_for_db(metadata),
    }


def normalize_versions_for_db(metadata: Dict[str, Any], project_key: int) -> List[Dict[str, Any]]:
    """
    Normalize metadata versions to DB rows.

    If no explicit versions list is available, a single current version is created.
    """
    raw_versions = metadata.get("versions") or []
    normalized: List[Dict[str, Any]] = []

    if isinstance(raw_versions, list):
        for item in raw_versions:
            if not isinstance(item, dict):
                continue
            version = normalize_version_value(first_non_empty(item.get("version"), metadata.get("version")))
            publication_date = first_non_empty(item.get("published_date"), metadata.get("published_date"))
            normalized.append(
                {
                    "project_key": project_key,
                    "version_id": parse_int(item.get("version")) or parse_int(version),
                    "version": version,
                    "version_state": infer_release_state(publication_date),
                    "publication_date": publication_date,
                    "release_time": "",
                    "download_date": now_utc_iso(),
                    "download_version_folder": make_download_version_folder(version),
                    "is_current": bool(item.get("is_current")),
                }
            )

    if not normalized:
        version = normalize_version_value(metadata.get("version"))
        publication_date = first_non_empty(
            metadata.get("published_date"),
            (metadata.get("dates") or {}).get("published_date"),
        )
        normalized = [
            {
                "project_key": project_key,
                "version_id": parse_int(metadata.get("version")) or parse_int(version),
                "version": version,
                "version_state": infer_release_state(publication_date),
                "publication_date": publication_date,
                "release_time": "",
                "download_date": now_utc_iso(),
                "download_version_folder": make_download_version_folder(version),
                "is_current": True,
            }
        ]

    if normalized and not any(item.get("is_current") for item in normalized):
        current_version = normalize_version_value(metadata.get("version"))
        matched = False
        for item in normalized:
            if item["version"] == current_version:
                item["is_current"] = True
                matched = True
                break
        if not matched:
            normalized[0]["is_current"] = True

    deduped: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, str]] = set()
    for item in normalized:
        key = (item["version"], item["publication_date"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


# =========================================================
# DOWNLOAD HELPERS
# =========================================================
def build_downloader_config(config: PipelineConfig) -> DownloadConfig:
    """
    Build the downloader configuration from the main pipeline config.
    """
    return DownloadConfig(
        base_download_dir=config.base_dir,
        repository_folder=config.repo_name,
        persistent_profile_dir=config.download_profile_dir,
        headless=config.download_headless,
        timeout_ms=config.download_timeout_ms,
        max_download_size_bytes=config.download_max_size_bytes,
    )


def should_attempt_download(
    metadata: Dict[str, Any],
    checklist: Dict[str, Any],
    config: PipelineConfig,
) -> bool:
    """
    Download is attempted for every qualitative project when download integration is enabled.
    Metadata file detection does not block download anymore.
    """
    if not config.enable_downloads:
        return False
    if not checklist.get("qualitative_pass"):
        return False

    page_type = get_page_type(metadata)
    return page_type in PAGE_TYPE_DISCRIMINATOR


def make_download_result_fallback(
    metadata: Dict[str, Any],
    status: str,
    error: str,
) -> Dict[str, Any]:
    """
    Build a normalized fallback result if the downloader throws unexpectedly.
    """
    normalized_status = clean_text(status)
    if normalized_status not in ALLOWED_FILE_STATUSES:
        normalized_status = "FAILED_SERVER_UNRESPONSIVE"

    file_name = f"{clean_text(metadata.get('study_id') or 'study')}_download.bin"

    return {
        "status": normalized_status,
        "download_mode": "",
        "selected_format": "",
        "downloaded_files": [],
        "db_file_rows": [
            {
                "file_name": file_name,
                "file_type": get_file_extension(file_name),
                "status": normalized_status,
            }
        ],
        "errors": [],
        "error": clean_text(error),
        "study_id": clean_text(metadata.get("study_id")),
        "page_type": get_page_type(metadata),
        "output_dir": "",
    }


def normalize_downloader_file_rows(download_result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Normalize downloader db_file_rows to the professor schema shape.
    Deduplicate identical rows while preserving order.
    """
    rows = download_result.get("db_file_rows") or []
    output: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, str, str]] = set()

    for row in rows:
        if not isinstance(row, dict):
            continue

        file_name = clean_text(row.get("file_name"))
        file_type = clean_text(row.get("file_type")) or get_file_extension(file_name)
        status = clean_text(row.get("status"))

        if not file_name:
            continue
        if status not in ALLOWED_FILE_STATUSES:
            status = "FAILED_SERVER_UNRESPONSIVE"
        if not file_type:
            file_type = "unknown"

        key = (file_name, file_type, status)
        if key in seen:
            continue
        seen.add(key)

        output.append(
            {
                "file_name": file_name,
                "file_type": file_type,
                "status": status,
            }
        )

    return output


def choose_version_key_for_files(
    version_key_map: List[Tuple[int, Dict[str, Any]]],
    metadata: Dict[str, Any],
) -> Optional[int]:
    """
    Attach downloaded file rows to the current version row.
    """
    if not version_key_map:
        return None

    current_version = normalize_version_value(metadata.get("version"))

    for version_key, version_meta in version_key_map:
        if version_meta.get("is_current"):
            return version_key

    for version_key, version_meta in version_key_map:
        if clean_text(version_meta.get("version")) == current_version:
            return version_key

    return version_key_map[0][0]


def log_download_decision(project_id: int, result: Dict[str, Any]) -> None:
    """
    Log a compact download result line.
    """
    logger.info(
        "Project %s | download_status=%s | download_mode=%s | selected_format=%s | file_count=%s | output_dir=%s | error=%s",
        project_id,
        clean_text(result.get("status")),
        clean_text(result.get("download_mode")),
        clean_text(result.get("selected_format")),
        len(result.get("downloaded_files") or []),
        clean_text(result.get("output_dir")),
        clean_text(result.get("error")),
    )


def record_download_summary(summary: Dict[str, Any], project_id: int, metadata: Dict[str, Any], result: Dict[str, Any]) -> None:
    """
    Update download-related counts and project buckets in the summary.
    """
    status = clean_text(result.get("status"))
    if not status:
        return

    counts = summary["counts"]
    project_ids = summary["project_ids"]

    counts["download_attempted"] += 1

    bucket = DOWNLOAD_STATUS_BUCKETS.get(status)
    if bucket:
        counts[bucket] += 1
        project_ids[bucket].append(project_id)

    summary["download_results"].append(
        {
            "project_id": project_id,
            "study_id": clean_text(metadata.get("study_id")),
            "page_type": get_page_type(metadata),
            "status": status,
            "download_mode": clean_text(result.get("download_mode")),
            "selected_format": clean_text(result.get("selected_format")),
            "downloaded_file_count": len(result.get("downloaded_files") or []),
            "downloaded_files": result.get("downloaded_files") or [],
            "db_file_rows": result.get("db_file_rows") or [],
            "output_dir": clean_text(result.get("output_dir")),
            "error": clean_text(result.get("error")),
            "errors": result.get("errors") or [],
        }
    )


# =========================================================
# SUMMARY HELPERS
# =========================================================
def initialize_summary(search_terms: List[str]) -> Dict[str, Any]:
    """
    Create the final summary structure with fixed keys.
    """
    return {
        "counts": {
            "total_projects": 0,
            "projects_with_files": 0,
            "projects_with_no_files": 0,
            "identified_qualitative_projects": 0,
            "identified_qualitative_with_files": 0,
            "identified_qualitative_with_no_files": 0,
            "identified_non_qualitative_projects": 0,
            "identified_non_qualitative_with_files": 0,
            "identified_non_qualitative_with_no_files": 0,
            "saved_projects": 0,
            "download_attempted": 0,
            "download_succeeded": 0,
            "download_failed_login_required": 0,
            "download_failed_server_unresponsive": 0,
            "download_failed_too_large": 0,
        },
        "project_ids": {
            "identified_qualitative_with_files": [],
            "identified_qualitative_with_no_files": [],
            "identified_non_qualitative_with_files": [],
            "identified_non_qualitative_with_no_files": [],
            "saved_projects": [],
            "download_succeeded": [],
            "download_failed_login_required": [],
            "download_failed_server_unresponsive": [],
            "download_failed_too_large": [],
        },
        "searched_terms": search_terms,
        "generated_at_utc": "",
        "download_results": [],
    }


def classify_project_bucket(is_qualitative: bool, has_files: bool) -> str:
    """
    Return the summary bucket name for a project.
    """
    if is_qualitative and has_files:
        return "identified_qualitative_with_files"
    if is_qualitative and not has_files:
        return "identified_qualitative_with_no_files"
    if not is_qualitative and has_files:
        return "identified_non_qualitative_with_files"
    return "identified_non_qualitative_with_no_files"


def update_summary(summary: Dict[str, Any], project_id: int, checklist: Dict[str, Any], has_files: bool) -> None:
    """
    Update summary counts and project id buckets for one project.
    """
    counts = summary["counts"]
    project_ids = summary["project_ids"]
    is_qualitative = bool(checklist["qualitative_pass"])

    counts["total_projects"] += 1

    if has_files:
        counts["projects_with_files"] += 1
    else:
        counts["projects_with_no_files"] += 1

    if is_qualitative:
        counts["identified_qualitative_projects"] += 1
        if has_files:
            counts["identified_qualitative_with_files"] += 1
        else:
            counts["identified_qualitative_with_no_files"] += 1
    else:
        counts["identified_non_qualitative_projects"] += 1
        if has_files:
            counts["identified_non_qualitative_with_files"] += 1
        else:
            counts["identified_non_qualitative_with_no_files"] += 1

    bucket = classify_project_bucket(is_qualitative, has_files)
    project_ids[bucket].append(project_id)


def save_summary_json(summary: Dict[str, Any], output_path: str) -> None:
    """
    Save summary JSON to disk.
    """
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)


def log_project_decision(
    project_id: int,
    action: str,
    checklist: Dict[str, Any],
    has_files: bool,
) -> None:
    """
    Log one compact project-level decision line at INFO level.
    """
    logger.info(
        "Project %s | %s | summary_signal=%s | kind_signal=%s | collection_signal=%s | "
        "use_kind_of_data=%s | use_collection_mode=%s | decision_reason=%s | metadata_has_files=%s | qualitative=%s",
        project_id,
        action,
        checklist["summary_signal"]["matched"],
        checklist["kind_signal"]["matched"],
        checklist["collection_signal"]["matched"],
        checklist["use_kind_of_data"],
        checklist["use_collection_mode"],
        checklist["decision_reason"],
        has_files,
        checklist["qualitative_pass"],
    )


# =========================================================
# MAIN PIPELINE
# =========================================================
def run_pipeline(config: PipelineConfig) -> Dict[str, Any]:
    """
    Execute the full ICPSR-family qualitative-identification pipeline.
    """
    logger.info("Starting ICPSR-family qualitative pipeline")

    db = DatabaseManager(config.db_path, config.schema_path)
    db.initialize_schema()

    search_terms = get_search_terms(config.query)
    summary = initialize_summary(search_terms)
    seen_studies: Set[str] = set()

    search_fn = load_callable(config.study_search_module, config.study_search_callable)
    metadata_fn = load_callable(config.metadata_module, config.metadata_callable)
    downloader_config = build_downloader_config(config)

    try:
        for query in search_terms:
            logger.info("Searching query: %s", query if query != "*" else "ALL STUDIES")

            raw_search_result = call_study_search(search_fn, query)
            study_items = normalize_search_results(raw_search_result)
            logger.info("Discovered %s study candidates for query=%s", len(study_items), query)

            for item in study_items:
                study_url = clean_text(item.get("study_url"))
                if not study_url:
                    continue

                study_key = normalize_url_for_dedupe(study_url)
                if study_key in seen_studies:
                    continue
                seen_studies.add(study_key)

                try:
                    metadata = call_metadata_extractor(metadata_fn, study_url)
                    study_id = parse_int(metadata.get("study_id"))
                    if study_id is None:
                        raise ValueError(f"Metadata extractor returned invalid study_id for URL: {study_url}")

                    checklist = evaluate_qualitative_checklist(metadata, config)
                    metadata_file_inventory = metadata_has_files(metadata)

                    update_summary(
                        summary=summary,
                        project_id=study_id,
                        checklist=checklist,
                        has_files=metadata_file_inventory,
                    )

                    project = normalize_project_metadata_for_db(
                        metadata=metadata,
                        config=config,
                        query_string=query,
                        checklist=checklist,
                    )

                    # -------------------------------------------------
                    # Case 1: project is non-qualitative -> remove local DB state
                    # -------------------------------------------------
                    if not checklist["qualitative_pass"]:
                        db.clear_project_rows(project["project_key"])
                        db.commit()
                        log_project_decision(
                            project_id=study_id,
                            action="skipped_non_qualitative",
                            checklist=checklist,
                            has_files=metadata_file_inventory,
                        )
                        continue

                    # -------------------------------------------------
                    # Case 2: qualitative project -> always try downloader
                    #         even if extractor found no files
                    # -------------------------------------------------
                    download_result: Optional[Dict[str, Any]] = None
                    if should_attempt_download(metadata, checklist, config):
                        try:
                            downloader_metadata = build_minimal_downloader_metadata(metadata, study_url)
                            download_result = download_public_study_assets(
                                metadata=downloader_metadata,
                                config=downloader_config,
                            )
                        except Exception as exc:
                            logger.exception("Downloader failed unexpectedly for study_id=%s: %s", study_id, exc)
                            download_result = make_download_result_fallback(
                                metadata=metadata,
                                status="FAILED_SERVER_UNRESPONSIVE",
                                error=str(exc),
                            )

                    # -------------------------------------------------
                    # Case 3: save qualitative project + versions
                    # -------------------------------------------------
                    normalized_versions = normalize_versions_for_db(metadata, project["project_key"])

                    db.clear_project_rows(project["project_key"])
                    db.save_project(project)

                    version_key_map: List[Tuple[int, Dict[str, Any]]] = []
                    for version_meta in normalized_versions:
                        version_key = db.save_project_version(version_meta)
                        version_key_map.append((version_key, version_meta))

                    current_version_key = choose_version_key_for_files(version_key_map, metadata)

                    if download_result is not None and current_version_key is not None:
                        file_rows = normalize_downloader_file_rows(download_result)
                        if file_rows:
                            db.save_files(
                                project_key=project["project_key"],
                                version_key=current_version_key,
                                file_rows=file_rows,
                            )

                    db.commit()
                    summary["counts"]["saved_projects"] += 1
                    summary["project_ids"]["saved_projects"].append(study_id)

                    log_project_decision(
                        project_id=study_id,
                        action="saved_project_and_versions",
                        checklist=checklist,
                        has_files=metadata_file_inventory,
                    )

                    if download_result is not None:
                        record_download_summary(
                            summary=summary,
                            project_id=study_id,
                            metadata=metadata,
                            result=download_result,
                        )
                        log_download_decision(study_id, download_result)

                except Exception as exc:
                    db.rollback()
                    logger.exception("Failed processing study_url=%s: %s", study_url, exc)

        summary["generated_at_utc"] = now_utc_iso()
        save_summary_json(summary, config.summary_path)

        logger.info("Saved summary JSON: %s", config.summary_path)
        logger.info("Pipeline summary: %s", json.dumps(summary, ensure_ascii=False))
        return summary

    finally:
        db.close()


# =========================================================
# ENTRY POINT
# =========================================================
if __name__ == "__main__":
    setup_logging()

    try:
        run_pipeline(CONFIG)
    except Exception:
        logger.exception("Pipeline execution failed")
        raise