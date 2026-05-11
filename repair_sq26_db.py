#!/usr/bin/env python3
"""
Repair an SQ26 SQLite submission database by aligning it with the grader schema.

This script:
- renames the PROJECTS primary key to `id`
- renames child foreign keys to `project_id`
- removes non-schema columns such as `version_key`
- normalizes PERSON_ROLE values to allowed enum values
- normalizes common Creative Commons license strings

Usage:
    python repair_sq26_db.py input.db output.db
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path


def normalize_role(role: str) -> str:
    role = (role or "").strip().upper()
    if role == "AUTHOR":
        return "AUTHOR"
    if role in {"CONTACT", "CONTRIBUTOR", "PRODUCER"}:
        return "OTHER"
    if role in {"OWNER", "UPLOADER", "OTHER", "UNKNOWN"}:
        return role
    return "UNKNOWN"


def normalize_license(value: str) -> str:
    s = (value or "").strip()
    if not s:
        return s

    low = s.lower()
    key = low.replace("https://", "").replace("http://", "")

    mapping = {
        "cc-by-4.0": "CC BY 4.0",
        "cc-by-sa-4.0": "CC BY-SA 4.0",
        "cc-by-nc-4.0": "CC BY-NC 4.0",
        "cc-by-nd-4.0": "CC BY-ND 4.0",
        "cc-by-nc-nd-4.0": "CC BY-NC-ND 4.0",
        "cc-by-nc-sa-4.0": "CC BY-NC-SA 4.0",
        "cc0-1.0": "CC0 1.0",
        "creativecommons.org/licenses/by/4.0": "CC BY 4.0",
        "creativecommons.org/licenses/by-sa/4.0": "CC BY-SA 4.0",
        "creativecommons.org/licenses/by-nc/4.0": "CC BY-NC 4.0",
        "creativecommons.org/licenses/by-nd/4.0": "CC BY-ND 4.0",
        "creativecommons.org/licenses/by-nc-sa/4.0": "CC BY-NC-SA 4.0",
        "creativecommons.org/licenses/by-nc-nd/4.0": "CC BY-NC-ND 4.0",
        "creativecommons.org/share-your-work/public-domain/pdm": "CC0 1.0",
        "cc by 4.0": "CC BY 4.0",
        "cc by-sa 4.0": "CC BY-SA 4.0",
        "cc by-nc 4.0": "CC BY-NC 4.0",
        "cc by-nd 4.0": "CC BY-ND 4.0",
        "cc by-nc-nd 4.0": "CC BY-NC-ND 4.0",
        "cc by-nc-sa 4.0": "CC BY-NC-SA 4.0",
        "cc0 1.0": "CC0 1.0",
    }

    return mapping.get(key, mapping.get(low, s))


def repair_db(src_path: Path, dst_path: Path) -> None:
    if dst_path.exists():
        dst_path.unlink()

    src = sqlite3.connect(str(src_path))
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(str(dst_path))
    dst.execute("PRAGMA foreign_keys = OFF;")

    dst.executescript(
        """
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY,
            query_string TEXT,
            repository_id INTEGER,
            repository_url TEXT,
            project_url TEXT,
            version TEXT,
            title TEXT,
            description TEXT,
            language TEXT,
            doi TEXT,
            upload_date TEXT,
            download_date TEXT,
            download_repository_folder TEXT,
            download_project_folder TEXT,
            download_version_folder TEXT,
            download_method TEXT
        );

        CREATE TABLE files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER,
            file_name TEXT,
            file_type TEXT,
            status TEXT
        );

        CREATE TABLE keywords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER,
            keyword TEXT
        );

        CREATE TABLE person_role (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER,
            name TEXT,
            role TEXT
        );

        CREATE TABLE licenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER,
            license TEXT
        );
        """
    )

    # Build a lookup from project -> latest version metadata if available.
    version_lookup: dict[int, dict[str, str | None]] = {}
    for row in src.execute(
        """
        SELECT project_key, version, download_version_folder, download_date
        FROM project_versions
        ORDER BY project_key, download_date DESC, version_key DESC
        """
    ):
        pk = int(row["project_key"])
        version_lookup.setdefault(pk, {"version": None, "download_version_folder": None})
        if version_lookup[pk]["version"] is None and row["version"]:
            version_lookup[pk]["version"] = row["version"]
        if version_lookup[pk]["download_version_folder"] is None and row["download_version_folder"]:
            version_lookup[pk]["download_version_folder"] = row["download_version_folder"]

    # projects
    for row in src.execute(
        """
        SELECT project_key, query_string, repository_id, repository_url, project_url,
               title, description, language, doi, upload_date, download_date,
               download_repository_folder, download_project_folder, download_method
        FROM projects
        ORDER BY project_key
        """
    ):
        pk = int(row["project_key"])
        version_meta = version_lookup.get(pk, {})
        dst.execute(
            """
            INSERT INTO projects (
                id, query_string, repository_id, repository_url, project_url, version,
                title, description, language, doi, upload_date, download_date,
                download_repository_folder, download_project_folder, download_version_folder, download_method
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pk,
                row["query_string"],
                row["repository_id"],
                row["repository_url"],
                row["project_url"],
                version_meta.get("version"),
                row["title"],
                row["description"],
                row["language"],
                row["doi"],
                row["upload_date"],
                row["download_date"],
                row["download_repository_folder"],
                row["download_project_folder"],
                version_meta.get("download_version_folder"),
                row["download_method"],
            ),
        )

    # files
    for row in src.execute(
        "SELECT id, project_key, file_name, file_type, status FROM files ORDER BY id"
    ):
        dst.execute(
            "INSERT INTO files (id, project_id, file_name, file_type, status) VALUES (?, ?, ?, ?, ?)",
            (row["id"], row["project_key"], row["file_name"], row["file_type"], row["status"]),
        )

    # keywords
    for row in src.execute("SELECT id, project_key, keyword FROM keywords ORDER BY id"):
        dst.execute(
            "INSERT INTO keywords (id, project_id, keyword) VALUES (?, ?, ?)",
            (row["id"], row["project_key"], row["keyword"]),
        )

    # person_role
    for row in src.execute("SELECT id, project_key, name, role FROM person_role ORDER BY id"):
        dst.execute(
            "INSERT INTO person_role (id, project_id, name, role) VALUES (?, ?, ?, ?)",
            (row["id"], row["project_key"], row["name"], normalize_role(row["role"])),
        )

    # licenses
    for row in src.execute("SELECT id, project_key, license FROM licenses ORDER BY id"):
        dst.execute(
            "INSERT INTO licenses (id, project_id, license) VALUES (?, ?, ?)",
            (row["id"], row["project_key"], normalize_license(row["license"])),
        )

    dst.commit()
    src.close()
    dst.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_db", type=Path)
    parser.add_argument("output_db", type=Path)
    args = parser.parse_args()

    repair_db(args.input_db, args.output_db)
    print(f"Wrote repaired database to {args.output_db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
