from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


# =========================================================
# OPTIONAL LOCAL CREDENTIAL IMPORT
# =========================================================
try:
    from QDArchive.icpsr_utils.cradential import ICPSR_EMAIL as LOCAL_ICPSR_EMAIL  # type: ignore
    from QDArchive.icpsr_utils.cradential import ICPSR_PASSWORD as LOCAL_ICPSR_PASSWORD  # type: ignore
except Exception:
    LOCAL_ICPSR_EMAIL = ""
    LOCAL_ICPSR_PASSWORD = ""


# =========================================================
# CONSTANTS
# =========================================================
ALLOWED_FILE_STATUSES: Set[str] = {
    "SUCCEEDED",
    "FAILED_SERVER_UNRESPONSIVE",
    "FAILED_LOGIN_REQUIRED",
    "FAILED_TOO_LARGE",
}

DEFAULT_ICPSR_PACKAGE_PRIORITY: List[str] = [
    "Qualitative Data",
    "Stata",
    "SPSS",
    "SAS",
    "R",
    "ASCII",
    "Delimited",
    "Documentation Only",
]

DEFAULT_TIMEOUT_MS = 90000
DEFAULT_MAX_DOWNLOAD_SIZE_BYTES = 5 * 1024 * 1024 * 1024  # 5 GB

LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

BRACKET_EXTENSION_MAP: Dict[str, str] = {
    "pdf": "pdf",
    "excel 2007 spreadsheet": "xlsx",
    "excel spreadsheet": "xlsx",
    "excel": "xlsx",
    "csv": "csv",
    "text": "txt",
    "txt": "txt",
    "zip": "zip",
}

_INTERNAL_LOGIN_REDIRECT = "__LOGIN_REDIRECT__"

logger = logging.getLogger(__name__)


# =========================================================
# CONFIG
# =========================================================
@dataclass(frozen=True)
class DownloadConfig:
    base_download_dir: str = "downloads"
    repository_folder: str = "icpsr"
    persistent_profile_dir: str = "icpsr_browser_profile"
    headless: bool = True
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    max_download_size_bytes: int = DEFAULT_MAX_DOWNLOAD_SIZE_BYTES

    email_env_var: str = "ICPSR_EMAIL"
    password_env_var: str = "ICPSR_PASSWORD"

    # Force fresh login attempt at study start
    force_login: bool = False

    # If True, prefer cradential.py over env vars
    prefer_local_credential_file: bool = True


# =========================================================
# LOGGING
# =========================================================
def setup_logging(level: int = logging.INFO) -> None:
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        logging.basicConfig(level=level, format=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
    else:
        root_logger.setLevel(level)


# =========================================================
# TEXT / PATH HELPERS
# =========================================================
def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def first_non_empty(*values: Any) -> str:
    for value in values:
        text = clean_text(value)
        if text:
            return text
    return ""


def safe_filename(name: str) -> str:
    text = clean_text(name)
    text = re.sub(r"[^\w.\- ]+", "_", text)
    text = re.sub(r"\s+", "_", text)
    text = text.strip("._ ")
    return text or "download.bin"


def ensure_dir(path: str) -> str:
    Path(path).mkdir(parents=True, exist_ok=True)
    return path


def normalize_status(status: str) -> str:
    normalized = clean_text(status)
    if normalized not in ALLOWED_FILE_STATUSES:
        raise ValueError(f"Invalid final status: {normalized}")
    return normalized


def parse_int(value: Any) -> Optional[int]:
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


def get_file_extension(filename: str) -> str:
    name = clean_text(filename)
    if not name or "." not in name:
        return "unknown"
    return name.rsplit(".", 1)[-1].lower().strip()


def normalize_version_value(raw_version: Any) -> str:
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


def make_version_folder(metadata: Dict[str, Any]) -> str:
    version = first_non_empty(metadata.get("version"), "1.0")
    return f"V{normalize_version_value(version)}"


def infer_study_id(metadata: Dict[str, Any]) -> str:
    study_id = clean_text(metadata.get("study_id"))
    if study_id:
        return study_id

    for value in [metadata.get("final_url"), metadata.get("source_url")]:
        text = clean_text(value)
        match = re.search(r"/studies/(\d+)", text)
        if match:
            return match.group(1)
    return ""


def get_page_type(metadata: Dict[str, Any]) -> str:
    return clean_text(metadata.get("page_type")).lower()


def build_icpsr_urls(metadata: Dict[str, Any]) -> Dict[str, str]:
    study_id = infer_study_id(metadata)
    if not study_id:
        raise ValueError("Could not infer ICPSR study_id from metadata.")

    base = f"https://www.icpsr.umich.edu/web/ICPSR/studies/{study_id}"
    return {
        "main": base,
        "datadocumentation": f"{base}/datadocumentation",
    }


def get_study_output_dir(metadata: Dict[str, Any], config: DownloadConfig) -> str:
    study_id = infer_study_id(metadata) or "unknown_study"
    page_type = get_page_type(metadata) or "study"
    version_folder = make_version_folder(metadata)

    project_folder = safe_filename(f"{study_id}_{page_type}")
    output_dir = os.path.join(
        config.base_download_dir,
        config.repository_folder,
        project_folder,
        version_folder,
    )
    return ensure_dir(output_dir)


# =========================================================
# CREDENTIAL HELPERS
# =========================================================
def resolve_credentials(config: DownloadConfig) -> Tuple[str, str]:
    env_email = os.getenv(config.email_env_var, "").strip()
    env_password = os.getenv(config.password_env_var, "").strip()

    local_email = clean_text(LOCAL_ICPSR_EMAIL)
    local_password = clean_text(LOCAL_ICPSR_PASSWORD)

    if config.prefer_local_credential_file:
        email = local_email or env_email
        password = local_password or env_password
    else:
        email = env_email or local_email
        password = env_password or local_password

    return email, password


# =========================================================
# PLAYWRIGHT HELPERS
# =========================================================
def locator_is_visible(locator: Any, timeout: int = 1500) -> bool:
    try:
        locator.wait_for(state="visible", timeout=timeout)
        return True
    except Exception:
        return False


def iter_roots(page: Any) -> Iterable[Any]:
    yield page
    for frame in page.frames:
        try:
            if frame != page.main_frame:
                yield frame
        except Exception:
            continue


def root_text(root: Any) -> str:
    try:
        return clean_text(root.locator("body").inner_text(timeout=3000)).lower()
    except Exception:
        return ""


def page_text(page: Any) -> str:
    texts: List[str] = []
    for root in iter_roots(page):
        text = root_text(root)
        if text:
            texts.append(text)
    return "\n".join(texts)


def first_visible(page: Any, selectors: Sequence[str], timeout: int = 1500):
    for root in iter_roots(page):
        for selector in selectors:
            try:
                loc = root.locator(selector).first
                if locator_is_visible(loc, timeout=timeout):
                    return loc
            except Exception:
                continue
    return None


def first_visible_role(page: Any, role: str, names: Sequence[str], timeout: int = 2000):
    for root in iter_roots(page):
        for name in names:
            rx = re.compile(rf"^\s*{re.escape(name)}\s*$", re.IGNORECASE)
            candidates = [
                root.get_by_role(role, name=rx).first,
                root.get_by_text(rx).first,
            ]
            for loc in candidates:
                try:
                    if locator_is_visible(loc, timeout=timeout):
                        return loc
                except Exception:
                    continue
    return None


def first_visible_text(page: Any, text_value: str, timeout: int = 2000):
    rx = re.compile(rf"^\s*{re.escape(text_value)}\s*$", re.IGNORECASE)
    for root in iter_roots(page):
        candidates = [
            root.get_by_text(rx).first,
            root.get_by_text(re.compile(re.escape(text_value), re.IGNORECASE)).first,
        ]
        for loc in candidates:
            try:
                if locator_is_visible(loc, timeout=timeout):
                    return loc
            except Exception:
                continue
    return None


# =========================================================
# PAGE STATE
# =========================================================
def page_has_login_link(page: Any) -> bool:
    return first_visible(page, ["text=Log In", "text=Login"], timeout=1200) is not None


def page_requires_login(page: Any) -> bool:
    url = clean_text(page.url).lower()
    if any(token in url for token in ["login", "signin", "sign-in", "openid-connect", "oauth", "realms/icpsr"]):
        return True

    strong_selectors = [
        "input[type='password']",
        "input[type='email']",
        "input[name='username']",
        "input[id='username']",
        "text=Sign in with email",
        "text=Sign In With Email",
    ]
    return first_visible(page, strong_selectors, timeout=1200) is not None


def page_shows_terms(page: Any) -> bool:
    text = page_text(page)
    url = clean_text(page.url).lower()
    return (
        "/terms" in url
        or "terms of use" in text
        or "if you agree to them" in text
        or "by clicking on the" in text
        or "you agree to the following conditions" in text
        or "\nagree\n" in text
        or "disagree" in text
    )


# =========================================================
# LOGIN FLOW
# =========================================================
def maybe_login(page: Any, config: DownloadConfig) -> bool:
    """
    Login only when truly needed.

    Behavior:
    - If already on login flow, fill credentials.
    - If not on login flow, but a login link is visible and caller wants a login,
      click it and proceed.
    - Otherwise do nothing.
    """
    if not page_requires_login(page):
        login_link = first_visible(page, ["text=Log In", "text=Login"], timeout=1200)
        if login_link is None:
            return False
        try:
            login_link.click()
            page.wait_for_timeout(1500)
        except Exception:
            pass

    email_gate = first_visible(page, ["text=Sign in with email", "text=Sign In With Email"], timeout=1200)
    if email_gate is not None:
        try:
            email_gate.click()
            page.wait_for_timeout(1500)
        except Exception:
            pass

    email, password = resolve_credentials(config)
    if not email or not password:
        raise RuntimeError(
            "Login is required, but no credentials were found. "
            "Provide them in cradential.py or environment variables."
        )

    email_selectors = [
        "input[type='email']",
        "input[name='email']",
        "input[id='email']",
        "input[name='username']",
        "input[id='username']",
    ]
    password_selectors = [
        "input[type='password']",
        "input[name='password']",
        "input[id='password']",
    ]
    submit_selectors = [
        "button[type='submit']",
        "input[type='submit']",
        "button:has-text('Sign In')",
        "button:has-text('Log In')",
        "button:has-text('Login')",
    ]

    email_input = first_visible(page, email_selectors, timeout=3000)
    password_input = first_visible(page, password_selectors, timeout=3000)

    if email_input is None or password_input is None:
        raise RuntimeError("Could not find email/password fields on the ICPSR login page.")

    email_input.fill(email)
    password_input.fill(password)

    submit = first_visible(page, submit_selectors, timeout=3000)
    if submit is None:
        raise RuntimeError("Could not find the login submit button.")

    submit.click()

    try:
        page.wait_for_load_state("domcontentloaded", timeout=30000)
    except Exception:
        pass

    page.wait_for_timeout(3000)

    if page_requires_login(page):
        raise RuntimeError("Still on ICPSR login flow after automatic login attempt.")

    return True


# =========================================================
# DOWNLOAD SIZE
# =========================================================
def get_download_file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except Exception:
        return 0


def enforce_size_limit(path: str, config: DownloadConfig) -> None:
    size = get_download_file_size(path)
    if size > config.max_download_size_bytes:
        try:
            os.remove(path)
        except Exception:
            pass
        raise ValueError(
            f"Downloaded file exceeds configured size limit: {size} > {config.max_download_size_bytes}"
        )


# =========================================================
# LABEL / FILE NAME HELPERS
# =========================================================
def parse_bracketed_visible_label(label: str) -> Tuple[str, str, str]:
    text = clean_text(label)
    match = re.match(r"^\s*(.*?)\s*\[([^\]]+)\]\s*(.*?)\s*$", text)
    if match:
        prefix = clean_text(match.group(1))
        bracket = clean_text(match.group(2))
        suffix = clean_text(match.group(3))
        return prefix, bracket, suffix
    return text, "", ""


def guess_extension_from_label(label: str) -> str:
    _, bracket, _ = parse_bracketed_visible_label(label)
    bracket_lower = bracket.lower()
    if bracket_lower in BRACKET_EXTENSION_MAP:
        return BRACKET_EXTENSION_MAP[bracket_lower]
    return "unknown"


def guess_filename_from_label(label: str) -> str:
    prefix, _, suffix = parse_bracketed_visible_label(label)
    ext = guess_extension_from_label(label)

    base = suffix or prefix or clean_text(label)
    base = safe_filename(base)

    if ext != "unknown" and not base.lower().endswith(f".{ext}"):
        return safe_filename(f"{base}.{ext}")
    return base or "download.bin"


def build_package_placeholder_name(metadata: Dict[str, Any]) -> str:
    page_type = get_page_type(metadata) or "study"
    study_id = infer_study_id(metadata) or "unknown"
    version = normalize_version_value(metadata.get("version"))

    prefix = {
        "icpsr": "ICPSR",
        "openicpsr": "OpenICPSR",
        "datalumos": "DataLumos",
    }.get(page_type, "Study")

    return safe_filename(f"{prefix}_{study_id}-V{version}.zip")


def make_db_file_row(file_name: str, status: str) -> Dict[str, str]:
    normalized_status = normalize_status(status)
    cleaned_name = safe_filename(file_name or "download.bin")
    return {
        "file_name": cleaned_name,
        "file_type": get_file_extension(cleaned_name),
        "status": normalized_status,
    }


def make_db_file_row_from_path(path: str, status: str) -> Dict[str, str]:
    return make_db_file_row(os.path.basename(path), status)


# =========================================================
# NAVIGATION HELPERS
# =========================================================
def reload_page(page: Any, url: str, timeout_ms: int) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    page.wait_for_timeout(1200)


# =========================================================
# REUSABLE BROWSER SESSION
# =========================================================
class ICPSRBrowserSession:
    """
    Reusable persistent Playwright browser session.

    This keeps:
    - browser open
    - cookies/session in the same profile directory
    - one page reused across multiple studies
    """

    def __init__(self, config: DownloadConfig) -> None:
        self.config = config
        self._playwright = None
        self.context = None
        self.page = None

    def open(self) -> "ICPSRBrowserSession":
        if self.context is not None:
            return self

        ensure_dir(self.config.persistent_profile_dir)

        self._playwright = sync_playwright().start()
        self.context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=self.config.persistent_profile_dir,
            headless=self.config.headless,
            accept_downloads=True,
            ignore_https_errors=True,
        )

        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.set_default_timeout(self.config.timeout_ms)
        return self

    def close(self) -> None:
        if self.context is not None:
            try:
                self.context.close()
            except Exception:
                pass
            self.context = None

        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

        self.page = None

    def __enter__(self) -> "ICPSRBrowserSession":
        return self.open()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


# =========================================================
# TOP-LEVEL DOWNLOAD MENU
# =========================================================
def open_package_download_menu(page: Any) -> None:
    preferred_targets = [
        "#quickDownload button",
        "#quickDownload a",
        "button:has-text('Download')",
        "a:has-text('Download')",
        "[aria-label*='Download']",
        "[title*='Download']",
    ]

    button = first_visible(page, preferred_targets, timeout=3000)
    if button is None:
        raise RuntimeError("Could not find the top-level Download button.")

    button.click()
    page.wait_for_timeout(1200)


def accept_terms_and_capture_download(page: Any, target_dir: str, config: DownloadConfig) -> str:
    deadline = time.time() + 20
    agree_btn = None

    while time.time() < deadline:
        agree_btn = first_visible_role(page, "button", ["Agree", "I Agree", "Accept"], timeout=2000)
        if agree_btn is not None:
            break
        page.wait_for_timeout(1000)

    if agree_btn is None:
        screenshot_path = os.path.join(target_dir, "icpsr_terms_button_not_found.png")
        try:
            page.screenshot(path=screenshot_path, full_page=True)
        except Exception:
            pass
        raise RuntimeError("Terms page appeared, but no Agree button was found.")

    try:
        agree_btn.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass

    with page.expect_download(timeout=config.timeout_ms) as info:
        agree_btn.click()

    download = info.value
    suggested_name = safe_filename(download.suggested_filename or "download.bin")
    final_path = os.path.join(target_dir, suggested_name)
    download.save_as(final_path)
    enforce_size_limit(final_path, config)
    return final_path


# =========================================================
# PACKAGE DOWNLOAD
# =========================================================
def _locate_package_option(page: Any, format_name: str):
    option = first_visible_role(page, "menuitem", [format_name], timeout=2000)
    if option is None:
        option = first_visible_role(page, "link", [format_name], timeout=2000)
    if option is None:
        option = first_visible_role(page, "button", [format_name], timeout=2000)
    if option is None:
        option = first_visible_text(page, format_name, timeout=2000)
    return option


def _single_package_click_attempt(
    page: Any,
    option: Any,
    format_name: str,
    target_dir: str,
    config: DownloadConfig,
) -> Dict[str, Any]:
    try:
        with page.expect_download(timeout=8000) as info:
            option.click(no_wait_after=True)

        download = info.value
        suggested_name = safe_filename(download.suggested_filename or f"{safe_filename(format_name)}.bin")
        final_path = os.path.join(target_dir, suggested_name)
        download.save_as(final_path)
        enforce_size_limit(final_path, config)

        return {
            "status": "SUCCEEDED",
            "selected_format": format_name,
            "downloaded_files": [
                {
                    "path": final_path,
                    "file_name": os.path.basename(final_path),
                    "file_type": get_file_extension(os.path.basename(final_path)),
                    "size_bytes": get_download_file_size(final_path),
                    "source_type": "package",
                    "label": format_name,
                }
            ],
            "error": "",
            "attempt_type": "package",
        }

    except PlaywrightTimeoutError:
        page.wait_for_timeout(2500)

        if page_shows_terms(page):
            try:
                final_path = accept_terms_and_capture_download(page, target_dir, config)
                return {
                    "status": "SUCCEEDED",
                    "selected_format": format_name,
                    "downloaded_files": [
                        {
                            "path": final_path,
                            "file_name": os.path.basename(final_path),
                            "file_type": get_file_extension(os.path.basename(final_path)),
                            "size_bytes": get_download_file_size(final_path),
                            "source_type": "package",
                            "label": format_name,
                        }
                    ],
                    "error": "",
                    "attempt_type": "package",
                }
            except ValueError as exc:
                return {
                    "status": "FAILED_TOO_LARGE",
                    "selected_format": format_name,
                    "downloaded_files": [],
                    "error": str(exc),
                    "attempt_type": "package",
                }
            except Exception as exc:
                return {
                    "status": "FAILED_SERVER_UNRESPONSIVE",
                    "selected_format": format_name,
                    "downloaded_files": [],
                    "error": str(exc),
                    "attempt_type": "package",
                }

        if page_requires_login(page) or page_has_login_link(page):
            return {
                "status": _INTERNAL_LOGIN_REDIRECT,
                "selected_format": format_name,
                "downloaded_files": [],
                "error": "Redirected to login during package selection.",
                "attempt_type": "package",
            }

        return {
            "status": "FAILED_SERVER_UNRESPONSIVE",
            "selected_format": format_name,
            "downloaded_files": [],
            "error": f"No download event for package format: {format_name}",
            "attempt_type": "package",
        }

    except ValueError as exc:
        return {
            "status": "FAILED_TOO_LARGE",
            "selected_format": format_name,
            "downloaded_files": [],
            "error": str(exc),
            "attempt_type": "package",
        }

    except Exception as exc:
        if page_requires_login(page) or page_has_login_link(page):
            return {
                "status": _INTERNAL_LOGIN_REDIRECT,
                "selected_format": format_name,
                "downloaded_files": [],
                "error": str(exc),
                "attempt_type": "package",
            }

        return {
            "status": "FAILED_SERVER_UNRESPONSIVE",
            "selected_format": format_name,
            "downloaded_files": [],
            "error": str(exc),
            "attempt_type": "package",
        }


def try_package_format(
    page: Any,
    format_name: str,
    target_dir: str,
    config: DownloadConfig,
    study_url: str,
) -> Dict[str, Any]:
    try:
        open_package_download_menu(page)
    except Exception as exc:
        return {
            "status": "FAILED_SERVER_UNRESPONSIVE",
            "selected_format": format_name,
            "downloaded_files": [],
            "error": str(exc),
            "attempt_type": "package",
        }

    option = _locate_package_option(page, format_name)
    if option is None:
        return {
            "status": "FAILED_SERVER_UNRESPONSIVE",
            "selected_format": format_name,
            "downloaded_files": [],
            "error": f"Package format not visible: {format_name}",
            "attempt_type": "package",
        }

    result = _single_package_click_attempt(page, option, format_name, target_dir, config)
    if result["status"] != _INTERNAL_LOGIN_REDIRECT:
        return result

    try:
        maybe_login(page, config)
    except Exception as exc:
        return {
            "status": "FAILED_LOGIN_REQUIRED",
            "selected_format": format_name,
            "downloaded_files": [],
            "error": f"Login failed during package selection: {exc}",
            "attempt_type": "package",
        }

    page.wait_for_timeout(1500)

    if page_shows_terms(page):
        try:
            final_path = accept_terms_and_capture_download(page, target_dir, config)
            return {
                "status": "SUCCEEDED",
                "selected_format": format_name,
                "downloaded_files": [
                    {
                        "path": final_path,
                        "file_name": os.path.basename(final_path),
                        "file_type": get_file_extension(os.path.basename(final_path)),
                        "size_bytes": get_download_file_size(final_path),
                        "source_type": "package",
                        "label": format_name,
                    }
                ],
                "error": "",
                "attempt_type": "package",
            }
        except ValueError as exc:
            return {
                "status": "FAILED_TOO_LARGE",
                "selected_format": format_name,
                "downloaded_files": [],
                "error": str(exc),
                "attempt_type": "package",
            }
        except Exception as exc:
            return {
                "status": "FAILED_SERVER_UNRESPONSIVE",
                "selected_format": format_name,
                "downloaded_files": [],
                "error": str(exc),
                "attempt_type": "package",
            }

    try:
        reload_page(page, study_url, config.timeout_ms)
        open_package_download_menu(page)
        option = _locate_package_option(page, format_name)
        if option is None:
            return {
                "status": "FAILED_SERVER_UNRESPONSIVE",
                "selected_format": format_name,
                "downloaded_files": [],
                "error": f"Package format not visible after login retry: {format_name}",
                "attempt_type": "package",
            }

        retry_result = _single_package_click_attempt(page, option, format_name, target_dir, config)
        if retry_result["status"] == _INTERNAL_LOGIN_REDIRECT:
            retry_result["status"] = "FAILED_LOGIN_REQUIRED"
            retry_result["error"] = "Session still redirected to login after retry."
        return retry_result

    except Exception as exc:
        return {
            "status": "FAILED_SERVER_UNRESPONSIVE",
            "selected_format": format_name,
            "downloaded_files": [],
            "error": f"Retry after login failed: {exc}",
            "attempt_type": "package",
        }


def get_package_format_priority(metadata: Dict[str, Any]) -> List[str]:
    output: List[str] = []
    files_block = metadata.get("files") or {}
    project_download_options = files_block.get("project_download_options") or []

    for item in project_download_options:
        if not isinstance(item, dict):
            continue
        label = clean_text(item.get("label"))
        if label and label not in output:
            output.append(label)

    for label in DEFAULT_ICPSR_PACKAGE_PRIORITY:
        if label not in output:
            output.append(label)

    return output


def attempt_icpsr_package_download(
    page: Any,
    metadata: Dict[str, Any],
    output_dir: str,
    config: DownloadConfig,
) -> Dict[str, Any]:
    formats = get_package_format_priority(metadata)
    errors: List[Dict[str, str]] = []
    study_url = build_icpsr_urls(metadata)["main"]

    for format_name in formats:
        logger.info("Trying ICPSR package format: %s", format_name)
        result = try_package_format(page, format_name, output_dir, config, study_url)

        if result["status"] == "SUCCEEDED":
            result["download_mode"] = "package"
            result["errors"] = errors
            result["db_file_rows"] = [
                make_db_file_row_from_path(item["path"], "SUCCEEDED")
                for item in result.get("downloaded_files", [])
            ]
            return result

        errors.append(
            {
                "phase": "package",
                "format": format_name,
                "status": clean_text(result["status"]),
                "error": clean_text(result.get("error")),
            }
        )

        if result["status"] in {"FAILED_LOGIN_REQUIRED", "FAILED_TOO_LARGE"}:
            result["download_mode"] = "package"
            result["errors"] = errors
            result["db_file_rows"] = [
                make_db_file_row(build_package_placeholder_name(metadata), result["status"])
            ]
            return result

        try:
            reload_page(page, study_url, config.timeout_ms)
        except Exception:
            pass

    return {
        "status": "FAILED_SERVER_UNRESPONSIVE",
        "download_mode": "package",
        "selected_format": "",
        "downloaded_files": [],
        "db_file_rows": [],
        "errors": errors,
        "error": "All ICPSR package format attempts failed.",
    }


# =========================================================
# FALLBACK FILE DOWNLOAD
# =========================================================
def extract_fallback_candidates(metadata: Dict[str, Any]) -> List[Dict[str, Any]]:
    files_block = metadata.get("files") or {}
    filesets = files_block.get("filesets") or []
    unique_files = files_block.get("unique_files") or []

    by_description: Dict[str, Dict[str, Any]] = {}
    for file_entry in unique_files:
        if not isinstance(file_entry, dict):
            continue
        key = clean_text(file_entry.get("file_description"))
        if key:
            by_description[key] = file_entry

    priority_map = {
        "data": 1,
        "setup": 2,
        "documentation": 3,
        "other": 4,
    }

    candidates: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, str]] = set()

    for fileset in filesets:
        if not isinstance(fileset, dict):
            continue

        dataset_number = fileset.get("dataset_number")
        dataset_identifier = clean_text(fileset.get("identifier"))
        dataset_title = clean_text(fileset.get("title"))
        download_items = fileset.get("download_items") or []

        for label in download_items:
            label_text = clean_text(label)
            if not label_text:
                continue

            dedupe_key = (dataset_identifier, label_text)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)

            matched_file = by_description.get(label_text, {})
            matched_name = clean_text(matched_file.get("file_name")) or guess_filename_from_label(label_text)
            matched_type = clean_text(matched_file.get("file_type")) or clean_text(matched_file.get("file_extension"))
            if not matched_type:
                matched_type = get_file_extension(matched_name)

            file_category = clean_text(matched_file.get("file_category")).lower() or "other"

            candidates.append(
                {
                    "dataset_number": dataset_number if isinstance(dataset_number, int) else parse_int(dataset_number),
                    "dataset_identifier": dataset_identifier,
                    "dataset_title": dataset_title,
                    "label": label_text,
                    "file_category": file_category,
                    "priority": priority_map.get(file_category, 9),
                    "file_name": matched_name,
                    "file_type": matched_type or "unknown",
                }
            )

    candidates.sort(
        key=lambda item: (
            item["dataset_number"] if item["dataset_number"] is not None else 999999,
            item["priority"],
            item["label"].lower(),
        )
    )
    return candidates


def find_dataset_row(page: Any, candidate: Dict[str, Any]):
    dataset_number = candidate.get("dataset_number")
    dataset_identifier = clean_text(candidate.get("dataset_identifier"))
    dataset_title = clean_text(candidate.get("dataset_title"))

    table = page.locator("table#data-doc")
    if not locator_is_visible(table.first, timeout=5000):
        return None

    if isinstance(dataset_number, int):
        row = table.locator(f"tr.dataset-{dataset_number}").first
        if locator_is_visible(row, timeout=1200):
            return row

    row_candidates: List[re.Pattern[str]] = []
    if dataset_identifier:
        row_candidates.append(re.compile(rf"\b{re.escape(dataset_identifier)}\b", re.IGNORECASE))
    if dataset_title:
        row_candidates.append(re.compile(re.escape(dataset_title), re.IGNORECASE))

    for pattern in row_candidates:
        try:
            row = table.locator("tr").filter(has_text=pattern).first
            if locator_is_visible(row, timeout=1200):
                return row
        except Exception:
            continue

    return None


def open_row_download_menu(row: Any) -> None:
    candidates = [
        row.get_by_role("button", name=re.compile(r"^\s*download\s*$", re.IGNORECASE)).first,
        row.get_by_role("link", name=re.compile(r"^\s*download\s*$", re.IGNORECASE)).first,
        row.locator("button:has-text('download')").first,
        row.locator("a:has-text('download')").first,
        row.get_by_text(re.compile(r"^\s*download\s*$", re.IGNORECASE)).first,
    ]

    for loc in candidates:
        try:
            if locator_is_visible(loc, timeout=1200):
                loc.click()
                row.page.wait_for_timeout(1000)
                return
        except Exception:
            continue

    raise RuntimeError("Could not find the dataset-row download button.")


def _locate_fallback_option(page: Any, label: str):
    option = first_visible_role(page, "menuitem", [label], timeout=2000)
    if option is None:
        option = first_visible_role(page, "link", [label], timeout=2000)
    if option is None:
        option = first_visible_role(page, "button", [label], timeout=2000)
    if option is None:
        option = first_visible_text(page, label, timeout=2000)
    return option


def _single_fallback_click_attempt(
    page: Any,
    option: Any,
    candidate: Dict[str, Any],
    target_dir: str,
    config: DownloadConfig,
) -> Dict[str, Any]:
    try:
        with page.expect_download(timeout=8000) as info:
            option.click(no_wait_after=True)

        download = info.value
        suggested_name = safe_filename(download.suggested_filename or candidate["file_name"] or "download.bin")
        final_path = os.path.join(target_dir, suggested_name)
        download.save_as(final_path)
        enforce_size_limit(final_path, config)

        return {
            "status": "SUCCEEDED",
            "label": candidate["label"],
            "downloaded_files": [
                {
                    "path": final_path,
                    "file_name": os.path.basename(final_path),
                    "file_type": get_file_extension(os.path.basename(final_path)),
                    "size_bytes": get_download_file_size(final_path),
                    "source_type": "file_fallback",
                    "dataset_identifier": candidate["dataset_identifier"],
                    "dataset_title": candidate["dataset_title"],
                    "label": candidate["label"],
                    "file_category": candidate["file_category"],
                }
            ],
            "db_file_row": make_db_file_row_from_path(final_path, "SUCCEEDED"),
            "error": "",
        }

    except PlaywrightTimeoutError:
        page.wait_for_timeout(2500)

        if page_shows_terms(page):
            try:
                final_path = accept_terms_and_capture_download(page, target_dir, config)
                return {
                    "status": "SUCCEEDED",
                    "label": candidate["label"],
                    "downloaded_files": [
                        {
                            "path": final_path,
                            "file_name": os.path.basename(final_path),
                            "file_type": get_file_extension(os.path.basename(final_path)),
                            "size_bytes": get_download_file_size(final_path),
                            "source_type": "file_fallback",
                            "dataset_identifier": candidate["dataset_identifier"],
                            "dataset_title": candidate["dataset_title"],
                            "label": candidate["label"],
                            "file_category": candidate["file_category"],
                        }
                    ],
                    "db_file_row": make_db_file_row_from_path(final_path, "SUCCEEDED"),
                    "error": "",
                }
            except ValueError as exc:
                return {
                    "status": "FAILED_TOO_LARGE",
                    "label": candidate["label"],
                    "downloaded_files": [],
                    "db_file_row": make_db_file_row(candidate["file_name"], "FAILED_TOO_LARGE"),
                    "error": str(exc),
                }
            except Exception as exc:
                return {
                    "status": "FAILED_SERVER_UNRESPONSIVE",
                    "label": candidate["label"],
                    "downloaded_files": [],
                    "db_file_row": make_db_file_row(candidate["file_name"], "FAILED_SERVER_UNRESPONSIVE"),
                    "error": str(exc),
                }

        if page_requires_login(page) or page_has_login_link(page):
            return {
                "status": _INTERNAL_LOGIN_REDIRECT,
                "label": candidate["label"],
                "downloaded_files": [],
                "db_file_row": make_db_file_row(candidate["file_name"], "FAILED_LOGIN_REQUIRED"),
                "error": "Redirected to login during fallback download.",
            }

        return {
            "status": "FAILED_SERVER_UNRESPONSIVE",
            "label": candidate["label"],
            "downloaded_files": [],
            "db_file_row": make_db_file_row(candidate["file_name"], "FAILED_SERVER_UNRESPONSIVE"),
            "error": f"No download event for fallback label: {candidate['label']}",
        }

    except ValueError as exc:
        return {
            "status": "FAILED_TOO_LARGE",
            "label": candidate["label"],
            "downloaded_files": [],
            "db_file_row": make_db_file_row(candidate["file_name"], "FAILED_TOO_LARGE"),
            "error": str(exc),
        }

    except Exception as exc:
        if page_requires_login(page) or page_has_login_link(page):
            return {
                "status": _INTERNAL_LOGIN_REDIRECT,
                "label": candidate["label"],
                "downloaded_files": [],
                "db_file_row": make_db_file_row(candidate["file_name"], "FAILED_LOGIN_REQUIRED"),
                "error": str(exc),
            }

        return {
            "status": "FAILED_SERVER_UNRESPONSIVE",
            "label": candidate["label"],
            "downloaded_files": [],
            "db_file_row": make_db_file_row(candidate["file_name"], "FAILED_SERVER_UNRESPONSIVE"),
            "error": str(exc),
        }


def try_fallback_download_candidate(
    page: Any,
    candidate: Dict[str, Any],
    target_dir: str,
    config: DownloadConfig,
    datadoc_url: str,
) -> Dict[str, Any]:
    row = find_dataset_row(page, candidate)
    if row is None:
        return {
            "status": "FAILED_SERVER_UNRESPONSIVE",
            "label": candidate["label"],
            "downloaded_files": [],
            "db_file_row": make_db_file_row(candidate["file_name"], "FAILED_SERVER_UNRESPONSIVE"),
            "error": f"Could not find row for {candidate['dataset_identifier']} / {candidate['label']}",
        }

    try:
        open_row_download_menu(row)
    except Exception as exc:
        return {
            "status": "FAILED_SERVER_UNRESPONSIVE",
            "label": candidate["label"],
            "downloaded_files": [],
            "db_file_row": make_db_file_row(candidate["file_name"], "FAILED_SERVER_UNRESPONSIVE"),
            "error": str(exc),
        }

    option = _locate_fallback_option(page, candidate["label"])
    if option is None:
        return {
            "status": "FAILED_SERVER_UNRESPONSIVE",
            "label": candidate["label"],
            "downloaded_files": [],
            "db_file_row": make_db_file_row(candidate["file_name"], "FAILED_SERVER_UNRESPONSIVE"),
            "error": f"Fallback option not visible: {candidate['label']}",
        }

    result = _single_fallback_click_attempt(page, option, candidate, target_dir, config)
    if result["status"] != _INTERNAL_LOGIN_REDIRECT:
        return result

    try:
        maybe_login(page, config)
    except Exception as exc:
        return {
            "status": "FAILED_LOGIN_REQUIRED",
            "label": candidate["label"],
            "downloaded_files": [],
            "db_file_row": make_db_file_row(candidate["file_name"], "FAILED_LOGIN_REQUIRED"),
            "error": f"Login failed during fallback download: {exc}",
        }

    page.wait_for_timeout(1500)

    if page_shows_terms(page):
        try:
            final_path = accept_terms_and_capture_download(page, target_dir, config)
            return {
                "status": "SUCCEEDED",
                "label": candidate["label"],
                "downloaded_files": [
                    {
                        "path": final_path,
                        "file_name": os.path.basename(final_path),
                        "file_type": get_file_extension(os.path.basename(final_path)),
                        "size_bytes": get_download_file_size(final_path),
                        "source_type": "file_fallback",
                        "dataset_identifier": candidate["dataset_identifier"],
                        "dataset_title": candidate["dataset_title"],
                        "label": candidate["label"],
                        "file_category": candidate["file_category"],
                    }
                ],
                "db_file_row": make_db_file_row_from_path(final_path, "SUCCEEDED"),
                "error": "",
            }
        except ValueError as exc:
            return {
                "status": "FAILED_TOO_LARGE",
                "label": candidate["label"],
                "downloaded_files": [],
                "db_file_row": make_db_file_row(candidate["file_name"], "FAILED_TOO_LARGE"),
                "error": str(exc),
            }
        except Exception as exc:
            return {
                "status": "FAILED_SERVER_UNRESPONSIVE",
                "label": candidate["label"],
                "downloaded_files": [],
                "db_file_row": make_db_file_row(candidate["file_name"], "FAILED_SERVER_UNRESPONSIVE"),
                "error": str(exc),
            }

    try:
        reload_page(page, datadoc_url, config.timeout_ms)
        row = find_dataset_row(page, candidate)
        if row is None:
            return {
                "status": "FAILED_SERVER_UNRESPONSIVE",
                "label": candidate["label"],
                "downloaded_files": [],
                "db_file_row": make_db_file_row(candidate["file_name"], "FAILED_SERVER_UNRESPONSIVE"),
                "error": f"Could not find row after login retry for {candidate['dataset_identifier']} / {candidate['label']}",
            }

        open_row_download_menu(row)
        option = _locate_fallback_option(page, candidate["label"])
        if option is None:
            return {
                "status": "FAILED_SERVER_UNRESPONSIVE",
                "label": candidate["label"],
                "downloaded_files": [],
                "db_file_row": make_db_file_row(candidate["file_name"], "FAILED_SERVER_UNRESPONSIVE"),
                "error": f"Fallback option not visible after login retry: {candidate['label']}",
            }

        retry_result = _single_fallback_click_attempt(page, option, candidate, target_dir, config)
        if retry_result["status"] == _INTERNAL_LOGIN_REDIRECT:
            return {
                "status": "FAILED_LOGIN_REQUIRED",
                "label": candidate["label"],
                "downloaded_files": [],
                "db_file_row": make_db_file_row(candidate["file_name"], "FAILED_LOGIN_REQUIRED"),
                "error": "Session still redirected to login after retry.",
            }
        return retry_result

    except Exception as exc:
        return {
            "status": "FAILED_SERVER_UNRESPONSIVE",
            "label": candidate["label"],
            "downloaded_files": [],
            "db_file_row": make_db_file_row(candidate["file_name"], "FAILED_SERVER_UNRESPONSIVE"),
            "error": f"Retry after login failed: {exc}",
        }


def choose_final_status_from_rows(file_rows: List[Dict[str, str]]) -> str:
    statuses = [clean_text(row.get("status")) for row in file_rows if clean_text(row.get("status"))]

    if any(status == "SUCCEEDED" for status in statuses):
        return "SUCCEEDED"
    if any(status == "FAILED_LOGIN_REQUIRED" for status in statuses):
        return "FAILED_LOGIN_REQUIRED"
    if any(status == "FAILED_TOO_LARGE" for status in statuses):
        return "FAILED_TOO_LARGE"
    return "FAILED_SERVER_UNRESPONSIVE"


def attempt_icpsr_file_fallback(
    page: Any,
    metadata: Dict[str, Any],
    output_dir: str,
    config: DownloadConfig,
) -> Dict[str, Any]:
    candidates = extract_fallback_candidates(metadata)
    if not candidates:
        return {
            "status": "FAILED_SERVER_UNRESPONSIVE",
            "download_mode": "file_fallback",
            "selected_format": "",
            "downloaded_files": [],
            "db_file_rows": [],
            "errors": [],
            "error": "No file-level fallback candidates were found in metadata.",
        }

    errors: List[Dict[str, str]] = []
    successes: List[Dict[str, Any]] = []
    db_file_rows: List[Dict[str, str]] = []
    datadoc_url = build_icpsr_urls(metadata)["datadocumentation"]

    for candidate in candidates:
        try:
            reload_page(page, datadoc_url, config.timeout_ms)
        except Exception as exc:
            errors.append(
                {
                    "phase": "file_fallback",
                    "label": candidate["label"],
                    "status": "FAILED_SERVER_UNRESPONSIVE",
                    "error": f"Could not reload datadocumentation page: {exc}",
                }
            )
            db_file_rows.append(make_db_file_row(candidate["file_name"], "FAILED_SERVER_UNRESPONSIVE"))
            continue

        logger.info(
            "Trying ICPSR fallback file download | dataset=%s | label=%s",
            candidate["dataset_identifier"],
            candidate["label"],
        )
        result = try_fallback_download_candidate(page, candidate, output_dir, config, datadoc_url)

        row = result.get("db_file_row")
        if isinstance(row, dict):
            db_file_rows.append(row)

        if result["status"] == "SUCCEEDED":
            successes.extend(result["downloaded_files"])
            continue

        errors.append(
            {
                "phase": "file_fallback",
                "label": candidate["label"],
                "status": clean_text(result["status"]),
                "error": clean_text(result.get("error")),
            }
        )

    final_status = choose_final_status_from_rows(db_file_rows)

    if final_status == "SUCCEEDED":
        return {
            "status": "SUCCEEDED",
            "download_mode": "file_fallback",
            "selected_format": "",
            "downloaded_files": successes,
            "db_file_rows": db_file_rows,
            "errors": errors,
            "error": "",
        }

    if not db_file_rows:
        return {
            "status": "FAILED_SERVER_UNRESPONSIVE",
            "download_mode": "file_fallback",
            "selected_format": "",
            "downloaded_files": [],
            "db_file_rows": [],
            "errors": errors,
            "error": "Package download failed and all file-level fallback attempts failed.",
        }

    return {
        "status": final_status,
        "download_mode": "file_fallback",
        "selected_format": "",
        "downloaded_files": [],
        "db_file_rows": db_file_rows,
        "errors": errors,
        "error": "Package download failed and file-level fallback did not produce a successful download.",
    }


# =========================================================
# FINAL RESULT HELPERS
# =========================================================
def build_login_required_result(metadata: Dict[str, Any], error: str, output_dir: str = "") -> Dict[str, Any]:
    page_type = get_page_type(metadata)
    study_id = infer_study_id(metadata)

    return {
        "status": normalize_status("FAILED_LOGIN_REQUIRED"),
        "download_mode": "",
        "selected_format": "",
        "downloaded_files": [],
        "db_file_rows": [
            make_db_file_row(build_package_placeholder_name(metadata), "FAILED_LOGIN_REQUIRED")
        ],
        "errors": [],
        "error": clean_text(error),
        "study_id": study_id,
        "page_type": page_type,
        "output_dir": output_dir,
    }


def build_server_failure_result(metadata: Dict[str, Any], error: str, output_dir: str = "") -> Dict[str, Any]:
    page_type = get_page_type(metadata)
    study_id = infer_study_id(metadata)

    return {
        "status": normalize_status("FAILED_SERVER_UNRESPONSIVE"),
        "download_mode": "",
        "selected_format": "",
        "downloaded_files": [],
        "db_file_rows": [
            make_db_file_row(build_package_placeholder_name(metadata), "FAILED_SERVER_UNRESPONSIVE")
        ],
        "errors": [],
        "error": clean_text(error),
        "study_id": study_id,
        "page_type": page_type,
        "output_dir": output_dir,
    }


# =========================================================
# INTERNAL CORE DOWNLOAD
# =========================================================
def _download_public_study_assets_with_session(
    metadata: Dict[str, Any],
    config: DownloadConfig,
    browser_session: ICPSRBrowserSession,
) -> Dict[str, Any]:
    page_type = get_page_type(metadata)

    if page_type in {"openicpsr", "datalumos"}:
        return {
            "status": normalize_status("FAILED_LOGIN_REQUIRED"),
            "download_mode": "",
            "selected_format": "",
            "downloaded_files": [],
            "db_file_rows": [
                make_db_file_row(build_package_placeholder_name(metadata), "FAILED_LOGIN_REQUIRED")
            ],
            "errors": [],
            "error": f"{page_type} download is security-gated in current production flow.",
            "study_id": infer_study_id(metadata),
            "page_type": page_type,
            "output_dir": "",
        }

    if page_type != "icpsr":
        return build_server_failure_result(
            metadata=metadata,
            error=f"Unsupported page_type for downloader: {page_type}",
            output_dir="",
        )

    study_id = infer_study_id(metadata)
    if not study_id:
        return build_server_failure_result(
            metadata=metadata,
            error="Could not infer ICPSR study_id from metadata.",
            output_dir="",
        )

    urls = build_icpsr_urls(metadata)
    output_dir = get_study_output_dir(metadata, config)

    if browser_session.page is None:
        raise RuntimeError("Browser session is not open.")

    page = browser_session.page

    logger.info("Opening ICPSR study page: %s", urls["main"])
    reload_page(page, urls["main"], config.timeout_ms)

    # Use saved cookies/session first.
    # Only log in now if we are actually on a login page or if force_login is set.
    if config.force_login or page_requires_login(page):
        logger.info("Session requires login. Attempting automatic login.")
        maybe_login(page, config)
        reload_page(page, urls["main"], config.timeout_ms)

    if page_requires_login(page):
        return build_login_required_result(
            metadata=metadata,
            error="Still on login flow after automatic login attempt.",
            output_dir=output_dir,
        )

    package_result = attempt_icpsr_package_download(page, metadata, output_dir, config)

    if package_result["status"] == "SUCCEEDED":
        package_result["status"] = normalize_status(package_result["status"])
        package_result["study_id"] = study_id
        package_result["page_type"] = page_type
        package_result["output_dir"] = output_dir
        return package_result

    if package_result["status"] in {"FAILED_LOGIN_REQUIRED", "FAILED_TOO_LARGE"}:
        package_result["status"] = normalize_status(package_result["status"])
        package_result["study_id"] = study_id
        package_result["page_type"] = page_type
        package_result["output_dir"] = output_dir
        return package_result

    logger.info("Package download failed. Starting file-level fallback.")
    fallback_result = attempt_icpsr_file_fallback(page, metadata, output_dir, config)

    fallback_result["status"] = normalize_status(fallback_result["status"])
    fallback_result["study_id"] = study_id
    fallback_result["page_type"] = page_type
    fallback_result["output_dir"] = output_dir

    package_errors = package_result.get("errors", [])
    if package_errors:
        existing_errors = fallback_result.get("errors", [])
        fallback_result["errors"] = package_errors + existing_errors

    if not fallback_result.get("db_file_rows"):
        fallback_result["db_file_rows"] = [
            make_db_file_row(build_package_placeholder_name(metadata), fallback_result["status"])
        ]

    return fallback_result


# =========================================================
# PUBLIC ENTRYPOINTS
# =========================================================
def download_public_study_assets(
    metadata: Dict[str, Any],
    config: Optional[DownloadConfig] = None,
    browser_session: Optional[ICPSRBrowserSession] = None,
) -> Dict[str, Any]:
    """
    Download one study.

    If browser_session is provided, the same browser stays open and is reused.
    If browser_session is None, this function creates and closes its own session.
    """
    config = config or DownloadConfig()

    if browser_session is not None:
        return _download_public_study_assets_with_session(metadata, config, browser_session)

    with ICPSRBrowserSession(config).open() as session:
        return _download_public_study_assets_with_session(metadata, config, session)


def download_many_public_study_assets(
    metadata_items: List[Dict[str, Any]],
    config: Optional[DownloadConfig] = None,
) -> List[Dict[str, Any]]:
    """
    Download many studies using one persistent browser session.
    """
    config = config or DownloadConfig()
    results: List[Dict[str, Any]] = []

    with ICPSRBrowserSession(config).open() as session:
        for metadata in metadata_items:
            try:
                results.append(download_public_study_assets(metadata, config=config, browser_session=session))
            except Exception as exc:
                results.append(
                    build_server_failure_result(
                        metadata=metadata,
                        error=str(exc),
                        output_dir="",
                    )
                )

    return results


# =========================================================
# CLI HELPERS
# =========================================================
def load_metadata_from_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as file_handle:
        data = json.load(file_handle)
    if not isinstance(data, dict):
        raise ValueError("Metadata JSON must contain a top-level object.")
    return data


def build_minimal_metadata_from_url(study_url: str) -> Dict[str, Any]:
    study_id_match = re.search(r"/studies/(\d+)", clean_text(study_url))
    study_id = study_id_match.group(1) if study_id_match else ""
    return {
        "source_url": study_url,
        "final_url": study_url,
        "study_id": study_id,
        "page_type": "icpsr",
        "version": "1.0",
        "files": {
            "project_download_options": [],
            "filesets": [],
            "unique_files": [],
        },
    }


# =========================================================
# CLI
# =========================================================
def main() -> None:
    parser = argparse.ArgumentParser(description="ICPSR public downloader")
    parser.add_argument("--metadata-json", help="Path to unified metadata JSON")
    parser.add_argument("--study-url", help="ICPSR study URL for package-only or metadata-light download")
    parser.add_argument("--headful", action="store_true", help="Run browser in visible mode")
    parser.add_argument("--download-dir", default="downloads", help="Base download directory")
    parser.add_argument("--profile-dir", default="icpsr_browser_profile", help="Persistent browser profile directory")
    parser.add_argument("--max-size-gb", type=float, default=5.0, help="Maximum allowed file size in GB")
    parser.add_argument("--force-login", action="store_true", help="Force fresh login attempt")
    parser.add_argument("--prefer-env", action="store_true", help="Prefer environment credentials over cradential.py")
    parser.add_argument("--log-level", default="INFO", help="Logging level")
    args = parser.parse_args()

    setup_logging(getattr(logging, clean_text(args.log_level).upper(), logging.INFO))

    if not args.metadata_json and not args.study_url:
        raise SystemExit("Provide either --metadata-json or --study-url")

    if args.metadata_json:
        metadata = load_metadata_from_json(args.metadata_json)
    else:
        metadata = build_minimal_metadata_from_url(args.study_url)

    config = DownloadConfig(
        base_download_dir=args.download_dir,
        persistent_profile_dir=args.profile_dir,
        headless=not args.headful,
        max_download_size_bytes=int(args.max_size_gb * 1024 * 1024 * 1024),
        force_login=bool(args.force_login),
        prefer_local_credential_file=not bool(args.prefer_env),
    )

    result = download_public_study_assets(metadata, config=config)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()