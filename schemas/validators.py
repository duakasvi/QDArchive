from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from schemas.normalizers import (
    canonicalize_doi,
    clean_list,
    clean_text,
    normalize_metadata_record,
)
from schemas.unified_metadata import new_metadata_record


MetadataRecord = Dict[str, Any]


# =========================================================
# VALIDATION RESULT
# =========================================================
@dataclass
class ValidationResult:
    """Structured validation output for a metadata record."""
    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    missing_required_fields: List[str] = field(default_factory=list)
    type_mismatches: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        """Return the validation result as a serializable dictionary."""
        return {
            "valid": self.valid,
            "errors": self.errors,
            "warnings": self.warnings,
            "missing_required_fields": self.missing_required_fields,
            "type_mismatches": self.type_mismatches,
        }


# =========================================================
# REQUIRED FIELDS
# =========================================================
REQUIRED_TOP_LEVEL_FIELDS = [
    "schema_version",
    "source_url",
    "page_type",
    "repository",
    "repository_url",
    "study_id",
    "title",
    "generated_at_utc",
]

REQUIRED_NESTED_FIELDS = [
    ("identifiers", "study_id"),
    ("provenance", "repository"),
    ("provenance", "repository_url"),
    ("provenance", "page_type"),
    ("provenance", "schema_version"),
]

DOI_OPTIONAL_BUT_VALIDATABLE = True


# =========================================================
# SHAPE / TYPE HELPERS
# =========================================================
def _type_name(value: Any) -> str:
    return type(value).__name__


def _expected_type_name(template_value: Any) -> str:
    return type(template_value).__name__


def _get_nested(record: Dict[str, Any], path: Tuple[str, ...]) -> Any:
    current: Any = record
    for part in path:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _is_acceptable_scalar(actual: Any, template: Any) -> bool:
    """
    Determine whether actual is acceptable for the scalar type implied by template.

    Rules:
    - None is tolerated for optional fields
    - exact template scalar types are expected when values are present
    """
    if actual is None:
        return True

    if isinstance(template, bool):
        return isinstance(actual, bool)
    if isinstance(template, str):
        return isinstance(actual, str)
    if isinstance(template, int):
        return isinstance(actual, int)
    if isinstance(template, float):
        return isinstance(actual, (int, float))

    return True


def _collect_shape_mismatches(
    actual: Any,
    template: Any,
    path: str = "",
) -> List[str]:
    """
    Recursively compare the record shape against a schema template.

    This is intentionally conservative:
    - list item shapes are not enforced because the schema template typically
      uses empty lists for variable-length collections
    - dict keys present in actual but absent in template are allowed
    """
    mismatches: List[str] = []

    if isinstance(template, dict):
        if not isinstance(actual, dict):
            mismatches.append(
                f"{path or '<root>'}: expected dict, got {_type_name(actual)}"
            )
            return mismatches

        for key, template_value in template.items():
            next_path = f"{path}.{key}" if path else key
            if key not in actual:
                mismatches.append(f"{next_path}: missing key")
                continue
            mismatches.extend(_collect_shape_mismatches(actual[key], template_value, next_path))
        return mismatches

    if isinstance(template, list):
        if not isinstance(actual, list):
            mismatches.append(
                f"{path or '<root>'}: expected list, got {_type_name(actual)}"
            )
        return mismatches

    if not _is_acceptable_scalar(actual, template):
        mismatches.append(
            f"{path or '<root>'}: expected {_expected_type_name(template)}, got {_type_name(actual)}"
        )

    return mismatches


# =========================================================
# DOMAIN VALIDATORS
# =========================================================
def _validate_required_fields(record: MetadataRecord) -> Tuple[List[str], List[str]]:
    """Validate required top-level and nested fields."""
    missing: List[str] = []
    errors: List[str] = []

    for field in REQUIRED_TOP_LEVEL_FIELDS:
        if not clean_text(record.get(field)):
            missing.append(field)

    for path in REQUIRED_NESTED_FIELDS:
        value = _get_nested(record, path)
        if not clean_text(value):
            missing.append(".".join(path))

    if missing:
        errors.extend([f"Missing required field: {field}" for field in missing])

    return missing, errors


def _validate_identifier_consistency(record: MetadataRecord) -> List[str]:
    """Validate consistency across identifier fields."""
    warnings: List[str] = []

    doi_top = canonicalize_doi(record.get("doi"))
    identifiers = record.get("identifiers", {}) if isinstance(record.get("identifiers"), dict) else {}
    doi_nested = canonicalize_doi(identifiers.get("doi"))
    doi_canonical = canonicalize_doi(identifiers.get("doi_canonical"))

    if doi_top and doi_nested and doi_top != doi_nested:
        warnings.append("Top-level doi and identifiers.doi differ after normalization.")
    if doi_top and doi_canonical and doi_top != doi_canonical:
        warnings.append("Top-level doi and identifiers.doi_canonical differ after normalization.")

    study_id_top = clean_text(record.get("study_id"))
    study_id_nested = clean_text(identifiers.get("study_id"))
    if study_id_top and study_id_nested and study_id_top != study_id_nested:
        warnings.append("Top-level study_id and identifiers.study_id differ.")

    return warnings


def _validate_doi_format(record: MetadataRecord) -> List[str]:
    """Validate DOI format when DOI is present."""
    warnings: List[str] = []
    doi = clean_text(record.get("doi"))
    if not doi and DOI_OPTIONAL_BUT_VALIDATABLE:
        return warnings

    if doi and not doi.startswith("https://doi.org/"):
        warnings.append("DOI is present but is not in canonical https://doi.org/... form.")
    return warnings


def _validate_access_section(record: MetadataRecord) -> Tuple[List[str], List[str]]:
    """Validate access section consistency."""
    errors: List[str] = []
    warnings: List[str] = []

    access = record.get("access")
    if not isinstance(access, dict):
        return ["Field 'access' must be a dict."], warnings

    restricted_types = access.get("restricted_data_types")
    if not isinstance(restricted_types, dict):
        errors.append("Field 'access.restricted_data_types' must be a dict.")
        return errors, warnings

    if access.get("has_restricted_files") is True and not any(bool(v) for v in restricted_types.values()):
        warnings.append(
            "access.has_restricted_files is True but no restricted_data_types flag is True."
        )

    if access.get("access_restricted_data_button") is True and access.get("has_restricted_files") is False:
        warnings.append(
            "Restricted-data action button was detected while access.has_restricted_files is False."
        )

    if access.get("open_access") is True and access.get("has_restricted_files") is True:
        warnings.append(
            "Record indicates both open access and restricted files."
        )

    return errors, warnings


def _validate_publications_section(record: MetadataRecord) -> Tuple[List[str], List[str]]:
    """Validate publications count vs items."""
    errors: List[str] = []
    warnings: List[str] = []

    publications = record.get("publications")
    if not isinstance(publications, dict):
        return ["Field 'publications' must be a dict."], warnings

    count = publications.get("count")
    items = publications.get("items")

    if items is not None and not isinstance(items, list):
        errors.append("Field 'publications.items' must be a list.")
        return errors, warnings

    if isinstance(count, int) and isinstance(items, list) and count < len(items):
        warnings.append(
            "publications.count is smaller than the number of parsed publications.items."
        )

    return errors, warnings


def _validate_files_section(record: MetadataRecord) -> Tuple[List[str], List[str]]:
    """Validate files section structure and summary consistency."""
    errors: List[str] = []
    warnings: List[str] = []

    files = record.get("files")
    if not isinstance(files, dict):
        return ["Field 'files' must be a dict."], warnings

    raw_files = files.get("raw_files")
    unique_files = files.get("unique_files")
    summary = files.get("summary")

    if raw_files is not None and not isinstance(raw_files, list):
        errors.append("Field 'files.raw_files' must be a list.")
    if unique_files is not None and not isinstance(unique_files, list):
        errors.append("Field 'files.unique_files' must be a list.")
    if summary is not None and not isinstance(summary, dict):
        errors.append("Field 'files.summary' must be a dict.")

    if errors:
        return errors, warnings

    raw_files = raw_files or []
    unique_files = unique_files or []
    summary = summary or {}

    raw_count = summary.get("raw_entry_count")
    unique_count = summary.get("unique_file_count")
    documentation_count = summary.get("documentation_file_count")
    data_count = summary.get("data_file_count")
    setup_count = summary.get("setup_file_count")
    other_count = summary.get("other_file_count")

    if isinstance(raw_count, int) and raw_count != len(raw_files):
        warnings.append(
            f"files.summary.raw_entry_count={raw_count} but len(files.raw_files)={len(raw_files)}."
        )

    if isinstance(unique_count, int) and unique_count != len(unique_files):
        warnings.append(
            f"files.summary.unique_file_count={unique_count} but len(files.unique_files)={len(unique_files)}."
        )

    bucket_checks = [
        ("documentation_files", documentation_count),
        ("data_files", data_count),
        ("setup_files", setup_count),
        ("other_files", other_count),
    ]
    for bucket_name, expected_count in bucket_checks:
        bucket = files.get(bucket_name, [])
        if not isinstance(bucket, list):
            errors.append(f"Field 'files.{bucket_name}' must be a list.")
            continue
        if isinstance(expected_count, int) and expected_count != len(bucket):
            warnings.append(
                f"files.summary.{bucket_name.replace('_files', '_file_count')}={expected_count} but len(files.{bucket_name})={len(bucket)}."
            )

    return errors, warnings


def _validate_contributors_funding_shapes(record: MetadataRecord) -> Tuple[List[str], List[str]]:
    """Validate that contributors and funding collections contain dict entries."""
    errors: List[str] = []
    warnings: List[str] = []

    for field_name in ["contributors", "funding"]:
        value = record.get(field_name)
        if value is None:
            continue
        if not isinstance(value, list):
            errors.append(f"Field '{field_name}' must be a list.")
            continue
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                errors.append(f"Field '{field_name}[{index}]' must be a dict.")

    return errors, warnings


def _validate_dates(record: MetadataRecord) -> List[str]:
    """Validate basic date-field consistency."""
    warnings: List[str] = []

    published = clean_text(record.get("published_date"))
    modified = clean_text(record.get("modified_date"))
    dates = record.get("dates", {}) if isinstance(record.get("dates"), dict) else {}

    if published and clean_text(dates.get("published_date")) and published != clean_text(dates.get("published_date")):
        warnings.append("Top-level published_date and dates.published_date differ.")
    if modified and clean_text(dates.get("modified_date")) and modified != clean_text(dates.get("modified_date")):
        warnings.append("Top-level modified_date and dates.modified_date differ.")

    return warnings


# =========================================================
# PUBLIC VALIDATION API
# =========================================================
def validate_metadata_record(record: MetadataRecord) -> ValidationResult:
    """
    Validate a unified metadata record.

    Behavior:
    - first runs shared normalization
    - validates required fields
    - validates shape against the shared schema template
    - validates key semantic consistency rules
    """
    normalized = normalize_metadata_record(record)
    schema_template = new_metadata_record(normalized.get("source_url", ""))

    missing_required, required_errors = _validate_required_fields(normalized)
    type_mismatches = _collect_shape_mismatches(normalized, schema_template)

    errors: List[str] = []
    warnings: List[str] = []

    errors.extend(required_errors)

    access_errors, access_warnings = _validate_access_section(normalized)
    errors.extend(access_errors)
    warnings.extend(access_warnings)

    publications_errors, publications_warnings = _validate_publications_section(normalized)
    errors.extend(publications_errors)
    warnings.extend(publications_warnings)

    files_errors, files_warnings = _validate_files_section(normalized)
    errors.extend(files_errors)
    warnings.extend(files_warnings)

    shape_errors, shape_warnings = _validate_contributors_funding_shapes(normalized)
    errors.extend(shape_errors)
    warnings.extend(shape_warnings)

    warnings.extend(_validate_identifier_consistency(normalized))
    warnings.extend(_validate_doi_format(normalized))
    warnings.extend(_validate_dates(normalized))

    for mismatch in type_mismatches:
        if mismatch.endswith(": missing key"):
            # missing keys are already handled by required field logic or optionality
            # so keep them as warnings only
            warnings.append(f"Schema shape notice: {mismatch}")
        else:
            errors.append(f"Schema type mismatch: {mismatch}")

    errors = clean_list(errors)
    warnings = clean_list(warnings)
    missing_required = clean_list(missing_required)
    type_mismatches = clean_list(type_mismatches)

    return ValidationResult(
        valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        missing_required_fields=missing_required,
        type_mismatches=type_mismatches,
    )


def annotate_record_with_validation(record: MetadataRecord) -> ValidationResult:
    """
    Validate a record and write the validation outcome back into diagnostics.

    This is the most practical function to call from extractors.
    """
    normalized = normalize_metadata_record(record)
    result = validate_metadata_record(normalized)

    diagnostics = normalized.setdefault("diagnostics", {})
    diagnostics["validation_warnings"] = clean_list(
        diagnostics.get("validation_warnings", []) + result.warnings
    )

    completeness = diagnostics.setdefault("completeness", {})
    completeness["missing_required_fields"] = result.missing_required_fields
    completeness["required_fields_present"] = len(result.missing_required_fields) == 0

    # update original object in place
    record.clear()
    record.update(normalized)

    return result


def raise_for_validation_errors(record: MetadataRecord) -> None:
    """
    Validate a record and raise ValueError if it is not valid.
    """
    result = validate_metadata_record(record)
    if not result.valid:
        joined = "\n".join(result.errors)
        raise ValueError(f"Metadata record validation failed:\n{joined}")