-- 지식 저장소 (module_id = knowledge). MySQL 용. 뜻은 knowledge.sql 과 같다.
-- MySQL 은 CREATE INDEX IF NOT EXISTS 가 없어 인덱스를 테이블 안에 둔다.
-- DEFAULT 가 있는 TEXT 는 MySQL 이 막으므로 VARCHAR 로 둔다.

CREATE TABLE IF NOT EXISTS knowledge_dataset (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    title VARCHAR(300) NOT NULL,
    category VARCHAR(100) NOT NULL DEFAULT '',
    description VARCHAR(4000) NOT NULL DEFAULT '',
    source_note VARCHAR(500) NOT NULL DEFAULT '',
    tags VARCHAR(500) NOT NULL DEFAULT '',
    kind VARCHAR(20) NOT NULL DEFAULT 'TABLE',
    current_version_id BIGINT NULL,
    created_by VARCHAR(100) NOT NULL DEFAULT '',
    created_by_name VARCHAR(100) NOT NULL DEFAULT '',
    created_at VARCHAR(32) NOT NULL,
    updated_at VARCHAR(32) NOT NULL,
    deleted_at VARCHAR(32) NULL,
    deleted_by VARCHAR(100) NULL,
    INDEX idx_knowledge_dataset_category (category)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS knowledge_version (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    dataset_id BIGINT NOT NULL,
    version_no INT NOT NULL,
    file_name VARCHAR(300) NOT NULL,
    stored_name VARCHAR(400) NOT NULL,
    file_size BIGINT NOT NULL DEFAULT 0,
    file_sha256 CHAR(64) NOT NULL,
    file_type VARCHAR(20) NOT NULL DEFAULT '',
    sheet_count INT NOT NULL DEFAULT 0,
    row_count INT NOT NULL DEFAULT 0,
    searchable TINYINT NOT NULL DEFAULT 1,
    note VARCHAR(1000) NOT NULL DEFAULT '',
    uploaded_by VARCHAR(100) NOT NULL DEFAULT '',
    uploaded_by_name VARCHAR(100) NOT NULL DEFAULT '',
    uploaded_at VARCHAR(32) NOT NULL,
    UNIQUE KEY uq_knowledge_version_no (dataset_id, version_no),
    INDEX idx_knowledge_version_sha (file_sha256),
    CONSTRAINT fk_knowledge_version_dataset FOREIGN KEY (dataset_id) REFERENCES knowledge_dataset(id)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS knowledge_sheet (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    version_id BIGINT NOT NULL,
    sheet_index INT NOT NULL,
    sheet_name VARCHAR(200) NOT NULL,
    header_row INT NOT NULL DEFAULT 0,
    columns_json LONGTEXT NOT NULL,
    row_count INT NOT NULL DEFAULT 0,
    truncated TINYINT NOT NULL DEFAULT 0,
    INDEX idx_knowledge_sheet_version (version_id, sheet_index),
    CONSTRAINT fk_knowledge_sheet_version FOREIGN KEY (version_id) REFERENCES knowledge_version(id)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS knowledge_row (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    version_id BIGINT NOT NULL,
    sheet_id BIGINT NOT NULL,
    row_no INT NOT NULL,
    cells_json LONGTEXT NOT NULL,
    search_text LONGTEXT NOT NULL,
    INDEX idx_knowledge_row_sheet (sheet_id, row_no),
    INDEX idx_knowledge_row_version (version_id),
    CONSTRAINT fk_knowledge_row_version FOREIGN KEY (version_id) REFERENCES knowledge_version(id),
    CONSTRAINT fk_knowledge_row_sheet FOREIGN KEY (sheet_id) REFERENCES knowledge_sheet(id)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS knowledge_event (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    dataset_id BIGINT NULL,
    version_id BIGINT NULL,
    action VARCHAR(30) NOT NULL,
    actor VARCHAR(100) NOT NULL DEFAULT '',
    actor_name VARCHAR(100) NOT NULL DEFAULT '',
    detail VARCHAR(2000) NOT NULL DEFAULT '',
    created_at VARCHAR(32) NOT NULL,
    INDEX idx_knowledge_event_dataset (dataset_id, created_at)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
