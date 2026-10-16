CREATE TABLE publications (
    reference_id TEXT PRIMARY KEY,
    title TEXT,
    year TEXT,
    doi TEXT,
    arxiv_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE citation_keys (
    citation_key TEXT PRIMARY KEY,
    reference_id TEXT NOT NULL,
    entry_type TEXT,
    title TEXT,
    filename_stem TEXT NOT NULL,
    status TEXT NOT NULL,
    error_category TEXT,
    error_detail TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (reference_id) REFERENCES publications(reference_id)
);
CREATE TABLE files (
    reference_id TEXT NOT NULL,
    role TEXT NOT NULL,
    version TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    source_url TEXT,
    provider TEXT,
    retrieved_at TEXT NOT NULL,
    PRIMARY KEY (reference_id, role),
    FOREIGN KEY (reference_id) REFERENCES publications(reference_id)
);
CREATE TABLE resolution_cache (
    cache_key TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE INDEX idx_citation_reference ON citation_keys(reference_id);

INSERT INTO publications VALUES (
    'internal:9add8da7600e43ea1579',
    '数据密集型系统',
    '2024',
    NULL,
    NULL,
    '2025-01-01T00:00:00+00:00',
    '2025-01-01T00:00:00+00:00'
);
INSERT INTO citation_keys VALUES (
    'zhang2024',
    'internal:9add8da7600e43ea1579',
    'article',
    '数据密集型系统',
    'zhang2024',
    'downloaded',
    NULL,
    NULL,
    '2025-01-02T00:00:00+00:00'
);
INSERT INTO files VALUES (
    'internal:9add8da7600e43ea1579',
    'pdf',
    'published',
    'papers/zhang2024.pdf',
    'a2b4945b6565b105cbf8ab4dc1d86bfc925d1e3eaa91cfb6ea770d292c333d28',
    414,
    'https://legacy.example/paper.pdf',
    'legacy-provider',
    '2025-01-02T03:04:05+00:00'
);
INSERT INTO resolution_cache VALUES (
    'legacy-cache',
    '{"preserved":true}',
    '2025-01-03T00:00:00+00:00'
);
PRAGMA user_version = 0;
