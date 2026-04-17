CREATE TABLE IF NOT EXISTS projects (
    project_key INTEGER PRIMARY KEY,
    project_id INTEGER,
    query_string TEXT,
    repository_id INTEGER,
    repository_url TEXT,
    project_url TEXT,
    title TEXT,
    description TEXT,
    language TEXT,
    doi TEXT,
    upload_date TEXT,
    download_date TEXT,
    download_repository_folder TEXT,
    download_project_folder TEXT,
    download_method TEXT
);

CREATE TABLE IF NOT EXISTS project_versions (
    version_key INTEGER PRIMARY KEY AUTOINCREMENT,
    project_key INTEGER,
    version_id INTEGER,
    version TEXT,
    version_state TEXT,
    publication_date TEXT,
    release_time TEXT,
    download_date TEXT,
    download_version_folder TEXT
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_key INTEGER,
    version_key INTEGER,
    file_name TEXT,
    file_type TEXT,
    status TEXT
);

CREATE TABLE IF NOT EXISTS keywords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_key INTEGER,
    keyword TEXT
);

CREATE TABLE IF NOT EXISTS person_role (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_key INTEGER,
    name TEXT,
    role TEXT
);

CREATE TABLE IF NOT EXISTS licenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_key INTEGER,
    license TEXT
);

CREATE TABLE IF NOT EXISTS failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_key INTEGER,
    reason TEXT,
    timestamp TEXT
);