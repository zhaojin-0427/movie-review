PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL COLLATE NOCASE,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'reviewer')),
    password_hash TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    deleted_at INTEGER
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    expires INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    owner_id INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    number INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('review', 'changes', 'approved')),
    revision INTEGER NOT NULL DEFAULT 0,
    asset_dir TEXT UNIQUE NOT NULL,
    frame_count INTEGER NOT NULL CHECK (frame_count > 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    closed_at TEXT,
    UNIQUE (job_id, number)
);
CREATE TABLE IF NOT EXISTS frames (
    version_id INTEGER NOT NULL REFERENCES versions(id),
    number INTEGER NOT NULL,
    asset_name TEXT NOT NULL,
    mimetype TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    signature TEXT NOT NULL,
    time_ms INTEGER,
    duration_ms INTEGER,
    state TEXT NOT NULL CHECK (state IN ('pending', 'changes', 'approved')),
    note TEXT NOT NULL DEFAULT '',
    regions TEXT NOT NULL DEFAULT '[]',
    approved_from INTEGER REFERENCES versions(id),
    PRIMARY KEY (version_id, number)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    version_id INTEGER NOT NULL REFERENCES versions(id),
    actor_id INTEGER NOT NULL REFERENCES users(id),
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS events_by_job ON events(job_id, id DESC);
CREATE INDEX IF NOT EXISTS versions_by_job ON versions(job_id, number DESC);
CREATE TABLE IF NOT EXISTS job_controls (
    job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
    state TEXT NOT NULL CHECK (state IN ('active', 'paused', 'withdrawn')),
    reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS version_inputs (
    version_id INTEGER PRIMARY KEY REFERENCES versions(id),
    source_sha256 TEXT,
    package_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS upload_receipts (
    actor_id INTEGER NOT NULL REFERENCES users(id),
    target_key TEXT NOT NULL,
    package_sha256 TEXT NOT NULL,
    version_id INTEGER NOT NULL REFERENCES versions(id),
    PRIMARY KEY (actor_id, target_key, package_sha256)
);
CREATE TABLE IF NOT EXISTS account_events (
    id INTEGER PRIMARY KEY,
    actor_id INTEGER NOT NULL REFERENCES users(id),
    target_id INTEGER NOT NULL REFERENCES users(id),
    action TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS login_attempts (
    id INTEGER PRIMARY KEY,
    address TEXT NOT NULL,
    username TEXT NOT NULL,
    occurred_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS login_attempts_by_address ON login_attempts(address, occurred_at);
CREATE INDEX IF NOT EXISTS login_attempts_by_time ON login_attempts(occurred_at);
CREATE TABLE IF NOT EXISTS supplements (
    version_id INTEGER PRIMARY KEY REFERENCES versions(id),
    base_id INTEGER NOT NULL UNIQUE REFERENCES versions(id)
);

CREATE TABLE IF NOT EXISTS execution_nodes (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, instance TEXT, capabilities TEXT,
    seen INTEGER, slots INTEGER NOT NULL DEFAULT 0, draining INTEGER NOT NULL DEFAULT 0,
    disk_free INTEGER, disk_reserve INTEGER, failure TEXT, deleted_at INTEGER
);
CREATE TABLE IF NOT EXISTS execution_node_bindings (
    node TEXT PRIMARY KEY REFERENCES execution_nodes(id),
    instance TEXT NOT NULL,
    bound_at INTEGER NOT NULL,
    bound_by INTEGER NOT NULL REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS execution_tokens (
    id INTEGER PRIMARY KEY, node TEXT NOT NULL REFERENCES execution_nodes(id),
    hash TEXT NOT NULL UNIQUE, prefix TEXT NOT NULL, expires INTEGER NOT NULL,
    issued INTEGER NOT NULL, issued_by INTEGER NOT NULL REFERENCES users(id),
    revoked INTEGER, revoked_by INTEGER REFERENCES users(id),
    paused_at INTEGER, deleted_at INTEGER
);
CREATE TABLE IF NOT EXISTS execution_credential_submissions (
    actor INTEGER NOT NULL REFERENCES users(id), request_id TEXT NOT NULL,
    PRIMARY KEY(actor, request_id)
);
CREATE TABLE IF NOT EXISTS execution_artifacts (
    id TEXT PRIMARY KEY, role TEXT NOT NULL, sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL, media_type TEXT NOT NULL, filename TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS execution_profiles (
    hash TEXT PRIMARY KEY, name TEXT NOT NULL, profile TEXT NOT NULL,
    rules TEXT NOT NULL REFERENCES execution_artifacts(id), workflow TEXT NOT NULL REFERENCES execution_artifacts(id),
    deleted_at INTEGER
);
CREATE TABLE IF NOT EXISTS execution_jobs (
    job INTEGER PRIMARY KEY REFERENCES jobs(id), actor INTEGER NOT NULL REFERENCES users(id),
    profile TEXT REFERENCES execution_profiles(hash), source TEXT NOT NULL REFERENCES execution_artifacts(id),
    local_review INTEGER NOT NULL DEFAULT 0, review_node TEXT REFERENCES execution_nodes(id),
    review_instance TEXT, export_quality TEXT CHECK (export_quality IN ('1080p','4k'))
);
CREATE TABLE IF NOT EXISTS execution_work (
    id TEXT PRIMARY KEY, job INTEGER NOT NULL REFERENCES execution_jobs(job), kind TEXT NOT NULL,
    base INTEGER, target INTEGER NOT NULL, manifest TEXT NOT NULL, manifest_hash TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued', attempt INTEGER NOT NULL DEFAULT 0,
    node TEXT REFERENCES execution_nodes(id), token INTEGER REFERENCES execution_tokens(id),
    instance TEXT, lease TEXT, expires INTEGER, stage TEXT, completed INTEGER, total INTEGER,
    updated INTEGER NOT NULL, receipt TEXT, failure TEXT, available_at INTEGER NOT NULL DEFAULT 0,
    deleted_at INTEGER,
    UNIQUE(job,kind,target)
);
CREATE INDEX IF NOT EXISTS execution_work_queue ON execution_work(state,available_at,updated);
CREATE TABLE IF NOT EXISTS execution_attempts (
    work TEXT NOT NULL REFERENCES execution_work(id), attempt INTEGER NOT NULL,
    node TEXT NOT NULL REFERENCES execution_nodes(id), claimed_at INTEGER, returned_at INTEGER,
    returned_by INTEGER REFERENCES users(id), return_reason TEXT, PRIMARY KEY(work,attempt)
);
CREATE TABLE IF NOT EXISTS execution_requests (
    node TEXT NOT NULL REFERENCES execution_nodes(id), operation TEXT NOT NULL, key TEXT NOT NULL,
    fingerprint TEXT NOT NULL, status INTEGER NOT NULL, response TEXT, created INTEGER NOT NULL,
    PRIMARY KEY(node,operation,key)
);
CREATE TABLE IF NOT EXISTS execution_uploads (
    id TEXT PRIMARY KEY, work TEXT NOT NULL REFERENCES execution_work(id), attempt INTEGER NOT NULL,
    role TEXT NOT NULL, sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL, media_type TEXT NOT NULL,
    artifact TEXT REFERENCES execution_artifacts(id), UNIQUE(work,attempt,role,sha256)
);
CREATE TABLE IF NOT EXISTS execution_parts (
    upload TEXT NOT NULL REFERENCES execution_uploads(id) ON DELETE CASCADE, part_no INTEGER NOT NULL,
    sha256 TEXT NOT NULL, PRIMARY KEY(upload,part_no)
);
CREATE TABLE IF NOT EXISTS execution_events (
    work TEXT NOT NULL REFERENCES execution_work(id), attempt INTEGER NOT NULL, sequence INTEGER NOT NULL,
    payload TEXT NOT NULL, PRIMARY KEY(work,attempt,sequence)
);
CREATE TABLE IF NOT EXISTS execution_versions (
    job INTEGER NOT NULL REFERENCES execution_jobs(job), version INTEGER NOT NULL,
    artifacts TEXT NOT NULL, PRIMARY KEY(job,version)
);
CREATE TABLE IF NOT EXISTS execution_deliveries (
    job INTEGER NOT NULL REFERENCES execution_jobs(job), version INTEGER NOT NULL,
    artifacts TEXT NOT NULL, PRIMARY KEY(job,version)
);
CREATE TABLE IF NOT EXISTS execution_management_events (
    job INTEGER NOT NULL REFERENCES execution_jobs(job), actor INTEGER NOT NULL REFERENCES users(id),
    action TEXT NOT NULL, reason TEXT NOT NULL, occurred_at INTEGER NOT NULL
);
