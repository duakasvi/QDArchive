import json
import logging
import os
import re
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import requests


# =========================================================
# CONFIGURATION
# =========================================================
@dataclass(frozen=True)
class PipelineConfig:
    """
    Configuration for the DANS Dataverse qualitative-project pipeline.
    """
    base_url: str = "https://ssh.datastations.nl/api"
    search_url: str = "https://ssh.datastations.nl/api/search"
    dataset_url: str = "https://ssh.datastations.nl/api/datasets/:persistentId"

    query: Optional[List[str]] = None  # Example: ["qualitative"]
    per_page: int = 1000
    timeout: int = 60

    base_dir: str = "downloads"
    repo_name: str = "dans"
    db_path: str = "23048230-seeding.db"

    repository_id: int = 5
    repository_url: str = "https://ssh.datastations.nl"
    download_method: str = "API-CALL"

    summary_filename: str = "dans_repo5_summary.json"
    temp_dir_name: str = "_tmp"

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
    def repo_dir(self) -> str:
        return os.path.join(self.base_dir, self.repo_name)

    @property
    def repo_temp_dir(self) -> str:
        return os.path.join(self.repo_dir, self.temp_dir_name)

    @property
    def summary_path(self) -> str:
        return os.path.join(self.script_dir, self.summary_filename)


CONFIG = PipelineConfig(query=[
    "qualitative",
    "qualitative research",
    "qualitative data",
    "interview",
    "interviews",
    "interview transcript",
    "transcripts",
    "focus group",
    "focus groups",
    "focus group discussion",
    "ethnography",
    "ethnographic study",
    "case study",
    "grounded theory",
    "thematic analysis",
    "narrative analysis",
    "discourse analysis",
    "phenomenology",
    "content analysis",
    "field notes",
    "fieldnotes",
    "diary",
    "diaries",
    "observation",
    "observational data",
    "oral history",
    "research interviews",
    "semi-structured interview",
    "in-depth interview",
    ".qdpx",
    ".qde",
    ".qdc",
    ".nvpx",
    ".nvp",
    ".atlproj",
    ".mx",
    ".mx22",
    ".mx24",
    ".mqda",
    ".doc",
    ".docx",
    ".pdf",
    ".txt",
    ".rtf",
    ".csv",
    ".xlsx",
    ".zip",
    "NVivo",
    "Atlas.ti",
    "MAXQDA",
    "CAQDAS",
    "QDA software",
    "qualitative analysis software",
    "codebook",
    "coding scheme",
    "metadata",
    "research data",
    "dataset",
    "data package",
    "qualitative dataset",
    "interview dataset",
    "qualitative data repository",
    "research project qualitative",
    "interview_",
    "transcript_",
    "focusgroup_",
    "fieldnote_",
    "memo_",
    "participant_",
    "qualitative interview dataset",
    "interview transcript qualitative",
    "focus group transcript data",
    "ethnographic interview data"
])


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
QDA_QUALITATIVE_EXTENSIONS: Set[str] = {
    "f4p", "hpr7", "loa", "m2k", "mc24", "mex22", "mex24", "mod", "mqbac", "mqda",
    "mqex", "mqmtr", "mqtc", "mtr", "mx11", "mx12", "mx18", "mx2", "mx20", "mx22",
    "mx24", "mx24bac", "mx3", "mx4", "mx5", "nvp", "nvpx", "ppj", "pprj", "qdc",
    "qdpx", "qlt", "qpd", "sea"
}

DESCRIPTION_KEYWORDS: List[str] = [
    "qualitative",
    "interview",
    "interviews",
    "transcript",
    "transcripts",
    "focus group",
    "focus groups",
]

FILENAME_KEYWORDS: List[str] = [
    "qualitative",
    "interview",
    "interviews",
    "transcript",
    "transcripts",
    "focus group",
    "focus groups",
]

ALLOWED_FILE_STATUSES: Set[str] = {
    "SUCCEEDED",
    "FAILED_SERVER_UNRESPONSIVE",
    "FAILED_LOGIN_REQUIRED",
    "FAILED_TOO_LARGE",
}


# =========================================================
# GENERAL HELPERS
# =========================================================
def now_utc_iso() -> str:
    """
    Return the current UTC timestamp in ISO 8601 format.
    """
    return datetime.now(timezone.utc).isoformat()


def ensure_directory(path: str) -> None:
    """
    Create a directory if it does not already exist.
    """
    os.makedirs(path, exist_ok=True)


def clean_text(value: Any) -> str:
    """
    Normalize whitespace and return a clean string.
    """
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_text(value: Any) -> str:
    """
    Normalize strings, lists, and dictionaries into a lowercased searchable string.
    """
    if value is None:
        return ""

    if isinstance(value, list):
        parts: List[str] = []
        for item in value:
            text = normalize_text(item)
            if text:
                parts.append(text)
        return " ".join(parts).lower().strip()

    if isinstance(value, dict):
        if "value" in value:
            return normalize_text(value["value"])
        return " ".join(str(v) for v in value.values() if v is not None).lower().strip()

    return str(value).lower().strip()


def safe_folder_name(name: str) -> str:
    """
    Convert an arbitrary string into a folder-safe name.
    """
    name = clean_text(name).lower()
    name = re.sub(r"[\\/]+", "-", name)
    name = re.sub(r"[^a-z0-9._-]+", "-", name)
    return name.strip("-_")


def safe_filename(name: str) -> str:
    """
    Convert an arbitrary string into a filename-safe name.
    """
    name = clean_text(name)
    return re.sub(r"[\\/]+", "_", name)


def get_file_extension(filename: str) -> str:
    """
    Return the lowercase file extension from a filename, or 'unknown'.
    """
    if not filename or "." not in filename:
        return "unknown"
    return filename.rsplit(".", 1)[-1].lower().strip()


def make_project_key(repository_id: int, project_id: int) -> int:
    """
    Build a stable integer project key by concatenating repository_id and project_id.
    """
    return int(f"{repository_id}{project_id}")


def get_search_terms(query: Optional[List[str]]) -> List[str]:
    """
    Normalize configured query values into a list of search terms.

    Rules:
    - None or [] => search all datasets using '*'
    - string => wrap in list
    - list => return as list
    """
    if query is None or query == []:
        return ["*"]
    if isinstance(query, str):
        return [query]
    return list(query)


def remove_directory_if_exists(path: str) -> None:
    """
    Remove a directory tree if it exists.
    """
    if path and os.path.exists(path):
        shutil.rmtree(path)


def build_temp_project_root(config: PipelineConfig, project: Dict[str, Any]) -> str:
    """
    Build a unique temp folder for one project refresh cycle.
    """
    ensure_directory(config.repo_temp_dir)
    unique_suffix = uuid.uuid4().hex
    temp_name = f"{project['download_project_folder']}__{project['project_id']}__{unique_suffix}"
    return os.path.join(config.repo_temp_dir, temp_name)


def build_final_project_root(config: PipelineConfig, project_folder_name: str) -> str:
    """
    Build the final project download root path from a project folder name.
    """
    return os.path.join(config.repo_dir, project_folder_name)


def build_removal_candidates(
    config: PipelineConfig,
    current_project_folder_name: str,
    existing_project_folder_name: Optional[str],
) -> List[str]:
    """
    Build all folder paths that should be removed when refreshing local state.

    This includes:
    - the current folder name derived from the latest metadata
    - the old folder name stored in the database, if different
    """
    candidates: List[str] = []

    current_root = build_final_project_root(config, current_project_folder_name)
    candidates.append(current_root)

    if existing_project_folder_name and existing_project_folder_name != current_project_folder_name:
        candidates.append(build_final_project_root(config, existing_project_folder_name))

    deduped: List[str] = []
    for path in candidates:
        if path not in deduped:
            deduped.append(path)

    return deduped


def replace_project_folder_from_temp(
    config: PipelineConfig,
    current_project_folder_name: str,
    existing_project_folder_name: Optional[str],
    temp_project_root: str,
) -> str:
    """
    Replace old project folder state with a freshly downloaded temp folder.

    Steps:
    - remove old candidate folders
    - move temp folder into final location

    Returns:
        The final project root path.
    """
    removal_candidates = build_removal_candidates(
        config=config,
        current_project_folder_name=current_project_folder_name,
        existing_project_folder_name=existing_project_folder_name,
    )

    for path in removal_candidates:
        remove_directory_if_exists(path)

    final_project_root = build_final_project_root(config, current_project_folder_name)
    ensure_directory(config.repo_dir)
    shutil.move(temp_project_root, final_project_root)

    return final_project_root


# =========================================================
# REQUEST SESSION
# =========================================================
def build_session() -> requests.Session:
    """
    Build and configure a reusable HTTP session.
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/146.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
    })
    return session


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

    def get_existing_project_download_folder(self, project_key: int) -> Optional[str]:
        """
        Return the existing download_project_folder stored in the database for this project,
        if present.
        """
        self.cur.execute(
            "SELECT download_project_folder FROM projects WHERE project_key = ?",
            (project_key,),
        )
        row = self.cur.fetchone()
        if row and row[0]:
            return str(row[0])
        return None

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
        self.cur.execute("""
            INSERT OR REPLACE INTO projects (
                project_key, project_id, query_string, repository_id, repository_url, project_url,
                title, description, language, doi, upload_date, download_date,
                download_repository_folder, download_project_folder, download_method
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
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
        ))

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
        self.cur.execute("""
            INSERT INTO project_versions (
                project_key, version_id, version, version_state,
                publication_date, release_time, download_date, download_version_folder
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            version_meta["project_key"],
            version_meta["version_id"],
            version_meta["version"],
            version_meta["version_state"],
            version_meta["publication_date"],
            version_meta["release_time"],
            version_meta["download_date"],
            version_meta["download_version_folder"],
        ))
        return int(self.cur.lastrowid)

    def save_files(
        self,
        project_key: int,
        version_key: int,
        files: List[Dict[str, Any]],
        download_statuses: Dict[str, str],
    ) -> None:
        """
        Save all file rows for a project version.
        """
        for idx, file_info in enumerate(files, 1):
            file_name = file_info.get("file_name") or ""
            key = f"{idx}:{safe_filename(file_name or 'download.bin')}"
            status = download_statuses.get(key, "FAILED_SERVER_UNRESPONSIVE")

            if status not in ALLOWED_FILE_STATUSES:
                status = "FAILED_SERVER_UNRESPONSIVE"

            self.cur.execute("""
                INSERT INTO files (project_key, version_key, file_name, file_type, status)
                VALUES (?, ?, ?, ?, ?)
            """, (
                project_key,
                version_key,
                file_name,
                file_info.get("file_type") or "unknown",
                status,
            ))


# =========================================================
# DATAVERSE FIELD EXTRACTION HELPERS
# =========================================================
def get_field_value(fields: List[Dict[str, Any]], type_name: str) -> Any:
    """
    Return the raw field value for a Dataverse metadata field typeName.
    """
    for field in fields:
        if field.get("typeName") == type_name:
            return field.get("value")
    return None


def extract_compound_values(value: Any, key: str) -> List[str]:
    """
    Extract nested compound values like authorName, producerName, etc.
    """
    results: List[str] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                inner = item.get(key, {})
                if isinstance(inner, dict):
                    nested_value = inner.get("value")
                    if nested_value:
                        results.append(str(nested_value).strip())
    return results


def get_first_text(fields: List[Dict[str, Any]], type_name: str) -> str:
    """
    Extract a simple text field from Dataverse metadata blocks.
    """
    value = get_field_value(fields, type_name)

    if isinstance(value, list):
        flat: List[str] = []
        for item in value:
            if isinstance(item, dict) and "value" in item:
                flat.extend([str(x).strip() for x in item["value"] if x])
            elif item:
                flat.append(str(item).strip())
        return " ".join([x for x in flat if x]).strip()

    if value is None:
        return ""

    return str(value).strip()


def get_title(fields: List[Dict[str, Any]]) -> str:
    """
    Extract dataset title.
    """
    return get_first_text(fields, "title")


def get_description(fields: List[Dict[str, Any]]) -> str:
    """
    Extract concatenated dataset description text.
    """
    values = get_field_value(fields, "dsDescription")
    descriptions: List[str] = []

    if isinstance(values, list):
        for item in values:
            if not isinstance(item, dict):
                continue
            text = item.get("dsDescriptionValue", {}).get("value")
            if text:
                descriptions.append(str(text).strip())

    return " ".join(descriptions).strip()


def get_authors(fields: List[Dict[str, Any]]) -> List[str]:
    """
    Extract author names.
    """
    return extract_compound_values(get_field_value(fields, "author"), "authorName")


def get_contacts(fields: List[Dict[str, Any]]) -> List[str]:
    """
    Extract dataset contact names.
    """
    return extract_compound_values(get_field_value(fields, "datasetContact"), "datasetContactName")


def get_producers(fields: List[Dict[str, Any]]) -> List[str]:
    """
    Extract producer names.
    """
    return extract_compound_values(get_field_value(fields, "producer"), "producerName")


def get_contributors(fields: List[Dict[str, Any]]) -> List[str]:
    """
    Extract contributor names.
    """
    return extract_compound_values(get_field_value(fields, "contributor"), "contributorName")


def get_keywords(fields: List[Dict[str, Any]]) -> List[str]:
    """
    Extract dataset keywords.
    """
    keyword_values = get_field_value(fields, "keyword")
    keywords: List[str] = []

    if isinstance(keyword_values, list):
        for item in keyword_values:
            if not isinstance(item, dict):
                continue
            kw = item.get("keywordValue", {}).get("value")
            if kw:
                keywords.append(str(kw).strip())

    return sorted(set([x for x in keywords if x]))


def get_language(fields: List[Dict[str, Any]]) -> str:
    """
    Extract language field.
    """
    return get_first_text(fields, "language")


def get_license(latest_version: Dict[str, Any]) -> str:
    """
    Extract license name from latestVersion.
    """
    license_obj = latest_version.get("license") or {}
    return str(license_obj.get("name") or "").strip()


def get_version_label(version_obj: Dict[str, Any]) -> str:
    """
    Build a version label like '1.0' or '2.3' from Dataverse version numbers.
    """
    version_number = version_obj.get("versionNumber")
    version_minor = version_obj.get("versionMinorNumber")

    if version_number is None:
        return ""
    if version_minor is None:
        return str(version_number)

    return f"{version_number}.{version_minor}"


# =========================================================
# API LAYER
# =========================================================
def fetch_search_page(session: requests.Session, config: PipelineConfig, start: int, query_string: str) -> Dict[str, Any]:
    """
    Fetch one search page from the Dataverse search API.
    """
    params = {
        "q": query_string,
        "type": "dataset",
        "start": start,
        "per_page": config.per_page,
    }
    response = session.get(config.search_url, params=params, timeout=config.timeout)
    response.raise_for_status()
    return response.json()


def fetch_dataset(session: requests.Session, config: PipelineConfig, persistent_id: str) -> Dict[str, Any]:
    """
    Fetch one dataset record by persistentId.
    """
    params = {"persistentId": persistent_id}
    response = session.get(config.dataset_url, params=params, timeout=config.timeout)
    response.raise_for_status()
    return response.json()


def list_dataset_versions(session: requests.Session, config: PipelineConfig, dataset_id: int) -> List[Dict[str, Any]]:
    """
    List all versions for a dataset.
    """
    url = f"{config.base_url}/datasets/{dataset_id}/versions"
    response = session.get(url, timeout=config.timeout)
    response.raise_for_status()
    data = response.json()
    return data.get("data", []) or []


# =========================================================
# QUALITATIVE SIGNAL DETECTION
# =========================================================
def detect_description_keywords(description: Any) -> Tuple[bool, str]:
    """
    Detect whether a dataset description contains qualitative keywords.
    """
    text = normalize_text(description)
    if not text:
        return False, "Description missing"

    matches = [kw for kw in DESCRIPTION_KEYWORDS if kw in text]
    if matches:
        return True, f"Description matched: {', '.join(sorted(set(matches)))}"

    return False, "No qualitative keywords found in description"


def detect_files_signals(files: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Detect qualitative file evidence using:
    - known qualitative software extensions
    - filename keywords
    """
    extensions_found: Set[str] = set()
    keyword_hits: Set[str] = set()

    for file_meta in files:
        filename = str(file_meta.get("file_name") or "")
        lower_name = filename.lower()

        extension = get_file_extension(filename)
        if extension in QDA_QUALITATIVE_EXTENSIONS:
            extensions_found.add(extension)

        for keyword in FILENAME_KEYWORDS:
            if keyword in lower_name:
                keyword_hits.add(keyword)

    extension_found = len(extensions_found) > 0
    filename_keyword_found = len(keyword_hits) > 0

    if extension_found and filename_keyword_found:
        reason = (
            f"Matched qualitative extension(s): {', '.join(sorted(extensions_found))}; "
            f"matched filename keyword(s): {', '.join(sorted(keyword_hits))}"
        )
    elif extension_found:
        reason = f"Matched qualitative extension(s): {', '.join(sorted(extensions_found))}"
    elif filename_keyword_found:
        reason = f"Matched filename keyword(s): {', '.join(sorted(keyword_hits))}"
    else:
        reason = "No qualitative file signal found"

    return {
        "extension_found": extension_found,
        "qualitative_keyword_found": filename_keyword_found,
        "reason": reason,
    }


# =========================================================
# METADATA EXTRACTION
# =========================================================
def extract_files_from_version(version_obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Extract normalized file metadata from a Dataverse version object.
    """
    output: List[Dict[str, Any]] = []

    for version_file in version_obj.get("files", []) or []:
        data_file = version_file.get("dataFile", {}) or {}
        filename = data_file.get("filename") or version_file.get("label") or ""
        content_type = data_file.get("contentType") or ""
        file_id = data_file.get("id")

        output.append({
            "file_id": file_id,
            "file_name": filename,
            "file_type": get_file_extension(filename),
            "restricted": bool(version_file.get("restricted")),
            "content_type": content_type,
            "file_access_request": bool(data_file.get("fileAccessRequest")),
        })

    return output


def extract_project_metadata(dataset_json: Dict[str, Any], config: PipelineConfig, query_string: str) -> Dict[str, Any]:
    """
    Extract normalized project-level metadata from a Dataverse dataset response.
    """
    data = dataset_json["data"]
    latest_version = data["latestVersion"]

    metadata_blocks = latest_version.get("metadataBlocks", {}) or {}
    citation_fields = metadata_blocks.get("citation", {}).get("fields", [])

    project_id = int(data["id"])
    project_key = make_project_key(config.repository_id, project_id)

    identifier = data.get("identifier") or ""
    persistent_url = data.get("persistentUrl") or ""

    return {
        "project_key": project_key,
        "project_id": project_id,
        "query_string": query_string,
        "repository_id": config.repository_id,
        "repository_url": config.repository_url,
        "project_url": persistent_url,
        "title": get_title(citation_fields),
        "description": get_description(citation_fields),
        "language": get_language(citation_fields),
        "doi": persistent_url,
        "upload_date": latest_version.get("publicationDate") or data.get("publicationDate") or "",
        "download_date": now_utc_iso(),
        "download_repository_folder": config.repo_name,
        "download_project_folder": safe_folder_name(identifier),
        "download_method": config.download_method,
        "identifier": identifier,
        "keywords": get_keywords(citation_fields),
        "authors": get_authors(citation_fields),
        "contacts": get_contacts(citation_fields),
        "producers": get_producers(citation_fields),
        "contributors": get_contributors(citation_fields),
        "license": get_license(latest_version),
    }


def extract_version_metadata(version_obj: Dict[str, Any], project_key: int) -> Dict[str, Any]:
    """
    Extract normalized version-level metadata.
    """
    version_label = get_version_label(version_obj)

    return {
        "project_key": project_key,
        "version_id": version_obj.get("id"),
        "version": version_label,
        "version_state": version_obj.get("versionState") or "",
        "publication_date": version_obj.get("publicationDate") or "",
        "release_time": version_obj.get("releaseTime") or "",
        "download_date": now_utc_iso(),
        "download_version_folder": f"v{version_label}" if version_label else "",
    }


# =========================================================
# DOWNLOAD LAYER
# =========================================================
def download_file(session: requests.Session, config: PipelineConfig, url: str, path: str) -> str:
    """
    Download a file and return a normalized status string.
    """
    try:
        ensure_directory(os.path.dirname(path))

        with session.get(url, stream=True, timeout=config.timeout) as response:
            if response.status_code in (401, 403):
                return "FAILED_LOGIN_REQUIRED"
            if response.status_code == 413:
                return "FAILED_TOO_LARGE"
            if response.status_code != 200:
                return "FAILED_SERVER_UNRESPONSIVE"

            with open(path, "wb") as f:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)

        return "SUCCEEDED"

    except requests.RequestException:
        return "FAILED_SERVER_UNRESPONSIVE"
    except Exception:
        return "FAILED_SERVER_UNRESPONSIVE"


def download_version_files_to_root(
    session: requests.Session,
    config: PipelineConfig,
    project_root: str,
    version_label: str,
    files: List[Dict[str, Any]],
) -> Dict[str, str]:
    """
    Download all files for one version into a given project root.

    Files are placed under:
        <project_root>/v<version_label>/
    """
    version_root = os.path.join(project_root, f"v{version_label}")
    ensure_directory(version_root)

    statuses: Dict[str, str] = {}

    for idx, file_info in enumerate(files, 1):
        file_id = file_info.get("file_id")
        filename = safe_filename(file_info.get("file_name") or "download.bin")

        if not file_id:
            statuses[f"{idx}:{filename}"] = "FAILED_SERVER_UNRESPONSIVE"
            continue

        target_path = os.path.join(version_root, filename)
        download_url = f"{config.base_url}/access/datafile/{file_id}?format=original"

        status = download_file(session, config, download_url, target_path)
        if status not in ALLOWED_FILE_STATUSES:
            status = "FAILED_SERVER_UNRESPONSIVE"

        statuses[f"{idx}:{filename}"] = status

    return statuses


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
        },
        "project_ids": {
            "identified_qualitative_with_files": [],
            "identified_qualitative_with_no_files": [],
            "identified_non_qualitative_with_files": [],
            "identified_non_qualitative_with_no_files": [],
        },
        "searched_terms": search_terms,
        "generated_at_utc": "",
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


def update_summary(summary: Dict[str, Any], project_id: int, is_qualitative: bool, has_files: bool) -> None:
    """
    Update summary counts and project id buckets for one project.
    """
    counts = summary["counts"]
    project_ids = summary["project_ids"]

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
    description_signal: bool,
    file_extension_signal: bool,
    file_name_kw_signal: bool,
    has_files: bool,
    qualitative: bool,
) -> None:
    """
    Log one compact project-level decision line at INFO level.
    """
    logger.info(
        "Project %s | %s | description_signal=%s | file_extension_signal=%s | "
        "file_name_kw_signal=%s | has_files=%s | qualitative=%s",
        project_id,
        action,
        description_signal,
        file_extension_signal,
        file_name_kw_signal,
        has_files,
        qualitative,
    )


# =========================================================
# MAIN PIPELINE
# =========================================================
def run_pipeline(config: PipelineConfig) -> Dict[str, Any]:
    """
    Execute the full DANS Dataverse qualitative-identification pipeline.

    Refresh-safe workflow:
    - Every encountered project is re-evaluated from the live source.
    - If project is now non-qualitative or has no files:
        - old DB rows are removed
        - old download folder(s) are removed
    - If project is now qualitative:
        - files are downloaded into a temp folder
        - old DB rows are removed
        - old final folder(s) are removed
        - temp folder is moved into final location
        - fresh DB rows are inserted

    Logging rules:
    - Project final decision => INFO
    - Version-by-version detail => DEBUG
    """
    logger.info("Starting DANS Dataverse pipeline")
    ensure_directory(config.repo_dir)
    ensure_directory(config.repo_temp_dir)

    session = build_session()
    db = DatabaseManager(config.db_path, config.schema_path)
    db.initialize_schema()

    seen: Set[str] = set()
    search_terms = get_search_terms(config.query)
    summary = initialize_summary(search_terms)

    try:
        for query in search_terms:
            logger.info("Searching query: %s", query if query != "*" else "ALL DATASETS")
            start = 0

            while True:
                try:
                    search_data = fetch_search_page(session, config, start, query)
                except Exception as exc:
                    logger.warning("Search failed for query=%s start=%s: %s", query, start, exc)
                    break

                data = search_data.get("data", {})
                items = data.get("items", [])
                total_count = data.get("total_count", 0)

                if not items:
                    logger.info("No items returned for query=%s start=%s; stopping query", query, start)
                    break

                logger.info(
                    "Fetched %s items at start=%s / total_count=%s for query=%s",
                    len(items),
                    start,
                    total_count,
                    query,
                )

                for item in items:
                    global_id = item.get("global_id")
                    if not global_id or global_id in seen:
                        continue
                    seen.add(global_id)

                    temp_project_root: Optional[str] = None

                    try:
                        dataset_json = fetch_dataset(session, config, global_id)
                        project = extract_project_metadata(dataset_json, config, query_string=query)
                        existing_project_folder_name = db.get_existing_project_download_folder(project["project_key"])

                        desc_ok, desc_reason = detect_description_keywords(project.get("description"))

                        dataset_id = int(dataset_json["data"]["id"])
                        versions = list_dataset_versions(session, config, dataset_id)

                        version_blocks: List[Dict[str, Any]] = []
                        project_has_files = False
                        any_extension_signal = False
                        any_filename_kw_signal = False

                        for version_obj in versions:
                            version_label = get_version_label(version_obj)
                            if not version_label:
                                continue

                            version_files = extract_files_from_version(version_obj)
                            if version_files:
                                project_has_files = True

                            file_signal = detect_files_signals(version_files)
                            version_blocks.append({
                                "version_label": version_label,
                                "version_meta": extract_version_metadata(version_obj, project["project_key"]),
                                "files": version_files,
                                "file_signal": file_signal,
                                "download_statuses": {},
                            })

                            if file_signal["extension_found"]:
                                any_extension_signal = True
                            if file_signal["qualitative_keyword_found"]:
                                any_filename_kw_signal = True

                            logger.debug(
                                "Project %s | Version v%s | extension_found=%s | filename_keyword_found=%s | %s",
                                project["project_id"],
                                version_label,
                                file_signal["extension_found"],
                                file_signal["qualitative_keyword_found"],
                                file_signal["reason"],
                            )

                        is_qualitative_project = desc_ok and (any_extension_signal or any_filename_kw_signal)

                        update_summary(
                            summary=summary,
                            project_id=project["project_id"],
                            is_qualitative=is_qualitative_project,
                            has_files=project_has_files,
                        )

                        logger.debug(
                            "Project %s | title=%s | description_reason=%s",
                            project["project_id"],
                            project["title"],
                            desc_reason,
                        )

                        # -------------------------------------------------
                        # Case 1: project has no files -> remove local state
                        # -------------------------------------------------
                        if not project_has_files:
                            db.clear_project_rows(project["project_key"])

                            for path in build_removal_candidates(
                                config=config,
                                current_project_folder_name=project["download_project_folder"],
                                existing_project_folder_name=existing_project_folder_name,
                            ):
                                remove_directory_if_exists(path)

                            db.commit()

                            log_project_decision(
                                project_id=project["project_id"],
                                action="skipped",
                                description_signal=desc_ok,
                                file_extension_signal=any_extension_signal,
                                file_name_kw_signal=any_filename_kw_signal,
                                has_files=False,
                                qualitative=is_qualitative_project,
                            )
                            continue

                        # -------------------------------------------------
                        # Case 2: project is non-qualitative -> remove local state
                        # -------------------------------------------------
                        if not is_qualitative_project:
                            db.clear_project_rows(project["project_key"])

                            for path in build_removal_candidates(
                                config=config,
                                current_project_folder_name=project["download_project_folder"],
                                existing_project_folder_name=existing_project_folder_name,
                            ):
                                remove_directory_if_exists(path)

                            db.commit()

                            log_project_decision(
                                project_id=project["project_id"],
                                action="skipped",
                                description_signal=desc_ok,
                                file_extension_signal=any_extension_signal,
                                file_name_kw_signal=any_filename_kw_signal,
                                has_files=True,
                                qualitative=False,
                            )
                            continue

                        # -------------------------------------------------
                        # Case 3: project is qualitative -> rebuild via temp folder
                        # -------------------------------------------------
                        temp_project_root = build_temp_project_root(config, project)
                        ensure_directory(temp_project_root)

                        for block in version_blocks:
                            block["download_statuses"] = download_version_files_to_root(
                                session=session,
                                config=config,
                                project_root=temp_project_root,
                                version_label=block["version_label"],
                                files=block["files"],
                            )

                        final_project_root = replace_project_folder_from_temp(
                            config=config,
                            current_project_folder_name=project["download_project_folder"],
                            existing_project_folder_name=existing_project_folder_name,
                            temp_project_root=temp_project_root,
                        )
                        temp_project_root = None  # moved successfully

                        logger.debug(
                            "Project %s | replaced local folder with refreshed snapshot at %s",
                            project["project_id"],
                            final_project_root,
                        )

                        db.clear_project_rows(project["project_key"])
                        db.save_project(project)

                        for block in version_blocks:
                            version_key = db.save_project_version(block["version_meta"])
                            db.save_files(
                                project_key=project["project_key"],
                                version_key=version_key,
                                files=block["files"],
                                download_statuses=block["download_statuses"],
                            )

                        db.commit()

                        log_project_decision(
                            project_id=project["project_id"],
                            action="saved",
                            description_signal=desc_ok,
                            file_extension_signal=any_extension_signal,
                            file_name_kw_signal=any_filename_kw_signal,
                            has_files=True,
                            qualitative=True,
                        )

                    except Exception as exc:
                        db.rollback()
                        logger.exception("Failed processing dataset global_id=%s: %s", global_id, exc)
                    finally:
                        if temp_project_root and os.path.exists(temp_project_root):
                            remove_directory_if_exists(temp_project_root)

                start += config.per_page
                if start >= total_count:
                    break

        summary["generated_at_utc"] = now_utc_iso()
        save_summary_json(summary, config.summary_path)

        logger.info("Saved summary JSON: %s", config.summary_path)
        logger.info("Pipeline summary: %s", json.dumps(summary, ensure_ascii=False))

        return summary

    finally:
        db.close()
        session.close()


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