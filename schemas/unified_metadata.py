from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Optional


SCHEMA_VERSION = "1.0.0"


def default_restricted_data_types() -> Dict[str, bool]:
    return {
        "idars": False,
        "useAgreement": False,
        "restricted": False,
        "vde": False,
        "enclave": False,
    }


def default_time_section() -> Dict[str, List[str]]:
    return {
        "time_method": [],
        "collection_dates": [],
        "temporal_coverage": [],
    }


def default_coverage_section() -> Dict[str, List[str]]:
    return {
        "geographic_coverage": [],
        "geographic_unit": [],
        "country": [],
        "state": [],
        "city": [],
        "spatial_coverage": [],
    }


def default_methodology_section() -> Dict[str, List[str]]:
    return {
        "analysis_units": [],
        "kind_of_data": [],
        "collection_mode": [],
        "sampling_procedure": [],
        "mode_of_observation": [],
        "data_source": [],
    }


def default_notes_section() -> Dict[str, List[str]]:
    return {
        "collection_notes": [],
        "collection_changes": [],
        "cleaning_notes": [],
        "processing_notes": [],
        "quality_notes": [],
    }


def default_variables_section() -> Dict[str, Any]:
    return {
        "count": None,
        "groups": [],
        "overview": "",
    }


def default_publication_item() -> Dict[str, Any]:
    return {
        "title": "",
        "doi": "",
        "journal": "",
        "year": "",
        "authors": [],
        "url": "",
        "citation": "",
    }


def default_publications_section() -> Dict[str, Any]:
    return {
        "count": None,
        "items": [],
    }


def default_version_item() -> Dict[str, Any]:
    return {
        "version": "",
        "published_date": "",
        "modified_date": "",
        "url": "",
        "label": "",
        "is_current": False,
    }


def default_funding_item() -> Dict[str, str]:
    return {
        "funder_name": "",
        "identifier": "",
        "award_number": "",
        "grant_number": "",
        "display": "",
        "url": "",
    }


def default_contributor_item() -> Dict[str, Any]:
    return {
        "name": "",
        "role": "",
        "affiliation": "",
        "orcid": "",
        "url": "",
    }


def default_access_section() -> Dict[str, Any]:
    return {
        "license": "",
        "license_url": "",
        "terms_url": "",
        "restrictions": "",
        "access_notes": [],
        "restricted_data_types": default_restricted_data_types(),
        "has_restricted_files": False,
        "has_public_files": None,
        "has_downloadable_files": False,
        "documentation_only_download": False,
        "access_restricted_data_button": False,
        "analyze_online": False,
        "open_access": None,
        "embargoed": None,
        "authentication_required": None,
    }


def default_file_item() -> Dict[str, Any]:
    return {
        "file_id": None,
        "dataset_number": None,
        "dataset_identifier": "",
        "title": "",
        "file_name": "",
        "file_stem": "",
        "file_format": "",
        "file_extension": "",
        "mime_type": "",
        "file_content": "",
        "file_category": "",
        "file_description": "",
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
        "public": None,
        "listed_on_public_page": None,
        "downloadable": None,
        "deliver_download": None,
        "include_in_bundle": [],
        "required_for_bundle": "",
        "dataset_format_flags": [],
        "terms_of_use": [],
        "resource_uuid": "",
        "uri": "",
        "download_url": "",
        "api_url": "",
        "parent_path": "",
        "folder_path": "",
        "last_modified": "",
        "display_size": "",
        "language": "",
        "source": "",
        "raw_occurrence_count": 1,
    }


def default_fileset_item() -> Dict[str, Any]:
    return {
        "dataset_number": None,
        "identifier": "",
        "title": "",
        "description": "",
        "file_count": 0,
        "bundles": {},
        "sda_components": None,
        "files": [],
    }


def default_file_summary() -> Dict[str, Any]:
    return {
        "raw_entry_count": 0,
        "unique_file_count": 0,
        "raw_public_entries": 0,
        "unique_public_files": 0,
        "raw_restricted_or_nonpublic_entries": 0,
        "unique_restricted_or_nonpublic_files": 0,
        "raw_downloadable_entries": 0,
        "unique_downloadable_files": 0,
        "downloadable_public_files": 0,
        "downloadable_documentation_files": 0,
        "downloadable_data_files": 0,
        "documentation_file_count": 0,
        "data_file_count": 0,
        "setup_file_count": 0,
        "other_file_count": 0,
        "raw_formats": {},
        "unique_formats": {},
        "raw_file_contents": {},
        "unique_file_contents": {},
    }


def default_files_section() -> Dict[str, Any]:
    return {
        "endpoint": "",
        "status_code": None,
        "content_type": "",
        "source": "",
        "counting_basis": "unique_resources",
        "filesets": [],
        "raw_files": [],
        "unique_files": [],
        "downloadable_files": [],
        "documentation_files": [],
        "data_files": [],
        "setup_files": [],
        "other_files": [],
        "summary": default_file_summary(),
    }


def default_identifiers_section() -> Dict[str, Any]:
    return {
        "study_id": "",
        "source_record_id": "",
        "doi": "",
        "doi_canonical": "",
        "handle": "",
        "ark": "",
        "isbn": "",
        "issn": "",
        "other_identifiers": [],
    }


def default_dates_section() -> Dict[str, str]:
    return {
        "published_date": "",
        "modified_date": "",
        "created_date": "",
        "deposit_date": "",
        "release_date": "",
    }


def default_relationships_section() -> Dict[str, Any]:
    return {
        "series": [],
        "projects": [],
        "collections": [],
        "related_datasets": [],
        "related_materials": [],
        "replaces": [],
        "is_replaced_by": [],
        "supplements": [],
        "is_supplement_to": [],
    }


def default_provenance_section() -> Dict[str, Any]:
    return {
        "repository": "",
        "repository_url": "",
        "page_type": "",
        "source_urls_visited": [],
        "api_endpoints_called": [],
        "html_pages_fetched": [],
        "fetch_methods": [],
        "parser_version": "",
        "schema_version": SCHEMA_VERSION,
    }


def default_diagnostics_section() -> Dict[str, Any]:
    return {
        "fetch_warnings": [],
        "field_warnings": [],
        "validation_warnings": [],
        "metadata_sources": [],
        "completeness": {
            "required_fields_present": True,
            "missing_required_fields": [],
        },
    }


def default_source_specific_section() -> Dict[str, Any]:
    return {
        "raw_jsonld": None,
        "raw_meta_tags": {},
        "raw_page_blocks": {},
        "raw_api_payloads": {},
        "raw_export_signals": [],
        "raw_action_signals": [],
        "extra": {},
    }


def default_metadata_record(source_url: str = "") -> Dict[str, Any]:
    """
    Canonical unified metadata schema for all repository/source extractors.

    Every extractor should:
    1. start from this factory
    2. populate known fields
    3. leave unknown fields empty/default
    4. optionally place source-only leftovers in source_specific.extra
    """
    return {
        "schema_version": SCHEMA_VERSION,

        # Core source/page identity
        "source_url": source_url,
        "final_url": "",
        "page_title": "",
        "page_type": "",

        # Repository-level context
        "repository": "",
        "repository_url": "",

        # Canonical identifiers
        "study_id": "",
        "title": "",
        "subtitle": "",
        "alternate_titles": [],
        "version": "",

        # Structured identifiers
        "identifiers": default_identifiers_section(),

        # Dates
        "published_date": "",
        "modified_date": "",
        "dates": default_dates_section(),

        # Citation / creator metadata
        "authors": [],
        "author_affiliations": [],
        "contributors": [],
        "publisher": "",
        "distributors": [],
        "citation": "",

        # Descriptive content
        "summary": "",
        "abstract": "",
        "purpose": "",
        "study_design": "",
        "sample": "",
        "universe": "",
        "unit_of_analysis": "",
        "language": [],
        "keywords": [],
        "topics": [],
        "subjects": [],

        # Funding / relationships
        "funding": [],
        "relationships": default_relationships_section(),

        # Time / place / methodology
        "time": default_time_section(),
        "coverage": default_coverage_section(),
        "methodology": default_methodology_section(),

        # Access / licensing
        "access": default_access_section(),

        # Notes / processing
        "notes": default_notes_section(),

        # Variables / publications / exports
        "variables": default_variables_section(),
        "publications": default_publications_section(),
        "export_formats": [],

        # Versions / files
        "versions": [],
        "files": default_files_section(),

        # Provenance / diagnostics / source leftovers
        "provenance": default_provenance_section(),
        "diagnostics": default_diagnostics_section(),
        "source_specific": default_source_specific_section(),

        # Final timestamp
        "generated_at_utc": "",
    }


def new_metadata_record(source_url: str = "") -> Dict[str, Any]:
    """
    Safe public factory.
    Returns a completely fresh record every time.
    """
    return deepcopy(default_metadata_record(source_url))


def required_top_level_fields() -> List[str]:
    return [
        "schema_version",
        "source_url",
        "final_url",
        "page_type",
        "study_id",
        "title",
        "version",
        "authors",
        "publisher",
        "access",
        "files",
        "diagnostics",
        "generated_at_utc",
    ]