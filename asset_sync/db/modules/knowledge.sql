-- 지식 저장소 (module_id = knowledge). SQLite 용. MySQL 은 knowledge.mysql.sql.
--
-- 자료(dataset) 하나에 버전이 여럿 붙는다. 같은 자료를 고쳐 다시 올리면 새 버전이
-- 되고, 검색은 현재 버전만 본다. 표는 시트 → 행으로 쪼개 저장한다. 행의
-- search_text 는 지금은 키워드 검색에 쓰고, 나중에 LLM 을 붙이면 그대로 임베딩할
-- 조각(chunk)이 된다.

CREATE TABLE IF NOT EXISTS knowledge_dataset (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    source_note TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'TABLE',
    current_version_id INTEGER,
    created_by TEXT NOT NULL DEFAULT '',
    created_by_name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    deleted_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_knowledge_dataset_category ON knowledge_dataset(category);

CREATE TABLE IF NOT EXISTS knowledge_version (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id INTEGER NOT NULL,
    version_no INTEGER NOT NULL,
    file_name TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    file_size INTEGER NOT NULL DEFAULT 0,
    file_sha256 TEXT NOT NULL,
    file_type TEXT NOT NULL DEFAULT '',
    sheet_count INTEGER NOT NULL DEFAULT 0,
    row_count INTEGER NOT NULL DEFAULT 0,
    searchable INTEGER NOT NULL DEFAULT 1,
    note TEXT NOT NULL DEFAULT '',
    uploaded_by TEXT NOT NULL DEFAULT '',
    uploaded_by_name TEXT NOT NULL DEFAULT '',
    uploaded_at TEXT NOT NULL,
    FOREIGN KEY(dataset_id) REFERENCES knowledge_dataset(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_version_no ON knowledge_version(dataset_id, version_no);
CREATE INDEX IF NOT EXISTS idx_knowledge_version_sha ON knowledge_version(file_sha256);

CREATE TABLE IF NOT EXISTS knowledge_sheet (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL,
    sheet_index INTEGER NOT NULL,
    sheet_name TEXT NOT NULL,
    header_row INTEGER NOT NULL DEFAULT 0,
    columns_json TEXT NOT NULL DEFAULT '[]',
    row_count INTEGER NOT NULL DEFAULT 0,
    truncated INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(version_id) REFERENCES knowledge_version(id)
);
CREATE INDEX IF NOT EXISTS idx_knowledge_sheet_version ON knowledge_sheet(version_id, sheet_index);

CREATE TABLE IF NOT EXISTS knowledge_row (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL,
    sheet_id INTEGER NOT NULL,
    row_no INTEGER NOT NULL,
    cells_json TEXT NOT NULL,
    search_text TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(version_id) REFERENCES knowledge_version(id),
    FOREIGN KEY(sheet_id) REFERENCES knowledge_sheet(id)
);
CREATE INDEX IF NOT EXISTS idx_knowledge_row_sheet ON knowledge_row(sheet_id, row_no);
CREATE INDEX IF NOT EXISTS idx_knowledge_row_version ON knowledge_row(version_id);

-- 누가 무엇을 올리고 · 고치고 · 지우고 · 내려받았는지. 권한 · 감사(2단계)의 바탕이다.
CREATE TABLE IF NOT EXISTS knowledge_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id INTEGER,
    version_id INTEGER,
    action TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    actor_name TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_knowledge_event_dataset ON knowledge_event(dataset_id, created_at);
