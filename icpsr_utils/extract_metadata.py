from typing import Any, Dict

from QDArchive.icpsr_utils.datalumos_metadata_extractor import extract_datalumos_metadata
from QDArchive.icpsr_utils.icpsr_metadata_extractor import extract_icpsr_metadata
from QDArchive.icpsr_utils.openicpsr_metadata_extractor import extract_openicpsr_metadata

DEFAULT_SAVE_OUTPUT_JSON = False

def infer_page_type_from_url(url: str) -> str:
    normalized = (url or "").strip().lower()

    if "datalumos.org/datalumos/project/" in normalized:
        return "datalumos"
    if "openicpsr.org/openicpsr/project/" in normalized:
        return "openicpsr"
    if "icpsr.umich.edu/web/icpsr/studies/" in normalized:
        return "icpsr"

    return "unknown"


def extract_metadata_by_url(source_url: str) -> Dict[str, Any]:
    if not source_url or not str(source_url).strip():
        raise ValueError("source_url is required")

    page_type = infer_page_type_from_url(source_url)

    extractor_map = {
        "datalumos": extract_datalumos_metadata,
        "openicpsr": extract_openicpsr_metadata,
        "icpsr": extract_icpsr_metadata,
    }

    extractor = extractor_map.get(page_type)
    if extractor is None:
        raise ValueError(f"Unsupported study URL for metadata extraction: {source_url}")

    metadata = extractor(source_url, save_output_json=DEFAULT_SAVE_OUTPUT_JSON)

    if not isinstance(metadata, dict):
        raise TypeError(f"Metadata extractor returned non-dict result for URL: {source_url}")

    return metadata