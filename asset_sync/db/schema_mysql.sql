-- MySQL schema, kept semantically identical to schema.sql (SQLite).
--
-- Three SQLite constructs have no direct MySQL equivalent and are translated as
-- follows. Changing either file means changing both.
--
--   1. CREATE INDEX IF NOT EXISTS
--      MySQL supports IF NOT EXISTS on CREATE TABLE but not on CREATE INDEX, so
--      every index is declared inline. The whole script stays idempotent.
--
--   2. Partial unique indexes (... WHERE active_yn=1)
--      Reproduced with a STORED generated column that is NULL while the row is
--      inactive. MySQL unique indexes ignore NULLs, which gives exactly the
--      "only one active row per key" rule. Works on 5.7 and 8.x.
--
--   3. Expression indexes over IFNULL(col,'')
--      Also reproduced with generated columns, because functional indexes need
--      MySQL 8.0.13+ while generated columns work from 5.7.
--
-- TEXT columns that carry a DEFAULT in SQLite are declared VARCHAR here: MySQL
-- forbids a DEFAULT on TEXT/BLOB. Sizes are set from the payload each column
-- actually holds. Columns without a DEFAULT (raw_json) stay LONGTEXT because
-- every INSERT supplies them explicitly.

CREATE TABLE IF NOT EXISTS schema_version (
    version INT NOT NULL PRIMARY KEY,
    applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS collection_run (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    source VARCHAR(32) NOT NULL,
    started_at VARCHAR(32) NOT NULL,
    ended_at VARCHAR(32),
    status VARCHAR(32) NOT NULL,
    record_count INT NOT NULL DEFAULT 0,
    success_scope_json VARCHAR(1000) NOT NULL DEFAULT '[]',
    failed_scope_json VARCHAR(1000) NOT NULL DEFAULT '[]',
    error_message VARCHAR(2000),
    metadata_json TEXT,
    KEY idx_collection_run_source_time (source, started_at DESC)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS snapshot (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    source VARCHAR(32) NOT NULL,
    snapshot_date VARCHAR(32) NOT NULL,
    collected_at VARCHAR(32) NOT NULL,
    collection_run_id BIGINT NOT NULL,
    status VARCHAR(32) NOT NULL,
    source_scope VARCHAR(128) NOT NULL DEFAULT 'ALL',
    record_count INT NOT NULL,
    checksum VARCHAR(128) NOT NULL,
    UNIQUE KEY uq_snapshot_run (source, collection_run_id, source_scope),
    KEY idx_snapshot_source_date (source, snapshot_date DESC, collected_at DESC),
    CONSTRAINT fk_snapshot_run FOREIGN KEY (collection_run_id) REFERENCES collection_run(id)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS itsm_asset_snapshot (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    snapshot_id BIGINT NOT NULL,
    cm_id VARCHAR(128) NOT NULL,
    normalized_hostname VARCHAR(255),
    primary_ip VARCHAR(64),
    ip_json VARCHAR(1000) NOT NULL DEFAULT '[]',
    cpu_cores INT,
    memory_mb BIGINT,
    os_family VARCHAR(64),
    os_version VARCHAR(128),
    status_code VARCHAR(64),
    server_category_code VARCHAR(64),
    environment_code VARCHAR(64),
    eos_value VARCHAR(64),
    record_hash VARCHAR(128) NOT NULL,
    raw_json LONGTEXT NOT NULL,
    UNIQUE KEY uq_itsm_snapshot_cm (snapshot_id, cm_id),
    KEY idx_itsm_snapshot_cm (cm_id, snapshot_id),
    KEY idx_itsm_snapshot_host (normalized_hostname),
    KEY idx_itsm_snapshot_ip (primary_ip),
    CONSTRAINT fk_itsm_snapshot FOREIGN KEY (snapshot_id) REFERENCES snapshot(id) ON DELETE CASCADE
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS rv_asset_snapshot (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    snapshot_id BIGINT NOT NULL,
    asset_key VARCHAR(255) NOT NULL,
    vm_uuid VARCHAR(128),
    smbios_uuid VARCHAR(128),
    vm_id VARCHAR(128),
    vcenter VARCHAR(255),
    vm_name VARCHAR(255),
    dns_name VARCHAR(255),
    normalized_hostname VARCHAR(255),
    primary_ip VARCHAR(64),
    ip_json VARCHAR(1000) NOT NULL DEFAULT '[]',
    cpus INT,
    memory_mb BIGINT,
    os_family VARCHAR(64),
    os_version VARCHAR(128),
    power_state VARCHAR(32),
    datacenter VARCHAR(255),
    cluster_name VARCHAR(255),
    esxi_host VARCHAR(255),
    template_flag TINYINT NOT NULL DEFAULT 0,
    srm_placeholder TINYINT NOT NULL DEFAULT 0,
    record_hash VARCHAR(128) NOT NULL,
    raw_json LONGTEXT NOT NULL,
    UNIQUE KEY uq_rv_snapshot_key (snapshot_id, asset_key),
    KEY idx_rv_snapshot_key (asset_key, snapshot_id),
    KEY idx_rv_snapshot_uuid (vm_uuid),
    KEY idx_rv_snapshot_host (normalized_hostname),
    KEY idx_rv_snapshot_ip (primary_ip),
    CONSTRAINT fk_rv_snapshot FOREIGN KEY (snapshot_id) REFERENCES snapshot(id) ON DELETE CASCADE
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS asset_ip (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    source VARCHAR(32) NOT NULL,
    snapshot_id BIGINT NOT NULL,
    asset_key VARCHAR(255) NOT NULL,
    ip_address VARCHAR(64) NOT NULL,
    is_primary TINYINT NOT NULL DEFAULT 0,
    UNIQUE KEY uq_asset_ip (source, snapshot_id, asset_key, ip_address),
    KEY idx_asset_ip_address (ip_address, snapshot_id),
    CONSTRAINT fk_asset_ip_snapshot FOREIGN KEY (snapshot_id) REFERENCES snapshot(id) ON DELETE CASCADE
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- active_cm_id / active_vm_uuid replace the SQLite partial unique indexes.
CREATE TABLE IF NOT EXISTS identity_map (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    cm_id VARCHAR(128) NOT NULL,
    vm_uuid VARCHAR(128) NOT NULL,
    active_yn TINYINT NOT NULL DEFAULT 1,
    approved_by VARCHAR(128),
    approved_at VARCHAR(32),
    note VARCHAR(1000),
    active_cm_id VARCHAR(128) GENERATED ALWAYS AS (IF(active_yn = 1, cm_id, NULL)) STORED,
    active_vm_uuid VARCHAR(128) GENERATED ALWAYS AS (IF(active_yn = 1, vm_uuid, NULL)) STORED,
    UNIQUE KEY uq_identity_map (cm_id, vm_uuid),
    UNIQUE KEY uq_identity_map_active_cm (active_cm_id),
    UNIQUE KEY uq_identity_map_active_uuid (active_vm_uuid)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS change_event (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    source VARCHAR(32) NOT NULL,
    snapshot_id BIGINT NOT NULL,
    previous_snapshot_id BIGINT,
    asset_key VARCHAR(255) NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    field_name VARCHAR(128),
    old_value VARCHAR(1000),
    new_value VARCHAR(1000),
    detected_at VARCHAR(32) NOT NULL,
    group_key VARCHAR(255),
    metadata_json TEXT,
    KEY idx_change_event_period (source, detected_at, event_type),
    KEY idx_change_event_asset (asset_key, field_name, detected_at),
    CONSTRAINT fk_change_event_snapshot FOREIGN KEY (snapshot_id) REFERENCES snapshot(id) ON DELETE CASCADE,
    CONSTRAINT fk_change_event_prev FOREIGN KEY (previous_snapshot_id) REFERENCES snapshot(id)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS reconciliation_result (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    itsm_snapshot_id BIGINT NOT NULL,
    rv_snapshot_id BIGINT NOT NULL,
    cm_id VARCHAR(128),
    rv_asset_key VARCHAR(255),
    match_status VARCHAR(64) NOT NULL,
    match_method VARCHAR(64),
    score INT NOT NULL DEFAULT 0,
    drift_json VARCHAR(2000) NOT NULL DEFAULT '[]',
    reason VARCHAR(1000),
    created_at VARCHAR(32) NOT NULL,
    UNIQUE KEY uq_recon_pair (itsm_snapshot_id, rv_snapshot_id, cm_id, rv_asset_key),
    KEY idx_recon_status (created_at, match_status),
    CONSTRAINT fk_recon_itsm FOREIGN KEY (itsm_snapshot_id) REFERENCES snapshot(id) ON DELETE CASCADE,
    CONSTRAINT fk_recon_rv FOREIGN KEY (rv_snapshot_id) REFERENCES snapshot(id) ON DELETE CASCADE
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS sync_result (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    period_start VARCHAR(32) NOT NULL,
    period_end VARCHAR(32) NOT NULL,
    asset_identity VARCHAR(255) NOT NULL,
    rv_event_type VARCHAR(64),
    itsm_event_type VARCHAR(64),
    sync_status VARCHAR(64) NOT NULL,
    detail_json VARCHAR(2000) NOT NULL DEFAULT '{}',
    created_at VARCHAR(32) NOT NULL
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS data_quality_rule (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    rule_name VARCHAR(191) NOT NULL,
    field_name VARCHAR(128) NOT NULL,
    status_filter_json VARCHAR(1000) NOT NULL DEFAULT '[]',
    server_category_filter_json VARCHAR(1000) NOT NULL DEFAULT '[]',
    rule_type VARCHAR(64) NOT NULL,
    severity VARCHAR(32) NOT NULL DEFAULT 'WARNING',
    enabled TINYINT NOT NULL DEFAULT 1,
    config_json VARCHAR(2000) NOT NULL DEFAULT '{}',
    UNIQUE KEY uq_data_quality_rule_name (rule_name)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS data_quality_result (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    snapshot_id BIGINT NOT NULL,
    cm_id VARCHAR(128) NOT NULL,
    field_name VARCHAR(128) NOT NULL,
    quality_status VARCHAR(32) NOT NULL,
    message VARCHAR(1000),
    created_at VARCHAR(32) NOT NULL,
    KEY idx_quality_snapshot_status (snapshot_id, quality_status),
    CONSTRAINT fk_quality_snapshot FOREIGN KEY (snapshot_id) REFERENCES snapshot(id) ON DELETE CASCADE
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS manual_asset_override (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    cm_id VARCHAR(128) NOT NULL,
    field_name VARCHAR(128) NOT NULL,
    override_value VARCHAR(1000),
    reason VARCHAR(1000) NOT NULL,
    approval_status VARCHAR(32) NOT NULL DEFAULT 'DRAFT',
    valid_from VARCHAR(32),
    valid_to VARCHAR(32),
    created_by VARCHAR(128) NOT NULL,
    created_at VARCHAR(32) NOT NULL,
    approved_by VARCHAR(128),
    approved_at VARCHAR(32)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS data_quality_exception (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    cm_id VARCHAR(128) NOT NULL,
    field_name VARCHAR(128),
    reason VARCHAR(1000) NOT NULL,
    valid_from VARCHAR(32),
    valid_to VARCHAR(32),
    created_by VARCHAR(128) NOT NULL,
    created_at VARCHAR(32) NOT NULL
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS audit_log (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    user_id VARCHAR(128) NOT NULL,
    action VARCHAR(64) NOT NULL,
    target_type VARCHAR(64) NOT NULL,
    target_id VARCHAR(128),
    reason VARCHAR(1000),
    before_json LONGTEXT,
    after_json LONGTEXT,
    created_at VARCHAR(32) NOT NULL,
    KEY idx_audit_time (created_at DESC)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS daily_batch_run (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    batch_date VARCHAR(32) NOT NULL,
    started_at VARCHAR(32) NOT NULL,
    ended_at VARCHAR(32),
    status VARCHAR(32) NOT NULL DEFAULT 'RUNNING',
    itsm_run_id BIGINT,
    itsm_snapshot_id BIGINT,
    vcenter_run_id BIGINT,
    vcenter_snapshot_id BIGINT,
    reconciliation_created_at VARCHAR(32),
    resource_usage_status VARCHAR(32) NOT NULL DEFAULT 'PENDING_SCRIPT',
    error_json VARCHAR(2000) NOT NULL DEFAULT '{}',
    metadata_json TEXT,
    KEY idx_daily_batch_date (batch_date DESC, started_at DESC),
    CONSTRAINT fk_batch_itsm_run FOREIGN KEY (itsm_run_id) REFERENCES collection_run(id),
    CONSTRAINT fk_batch_itsm_snapshot FOREIGN KEY (itsm_snapshot_id) REFERENCES snapshot(id),
    CONSTRAINT fk_batch_vc_run FOREIGN KEY (vcenter_run_id) REFERENCES collection_run(id),
    CONSTRAINT fk_batch_vc_snapshot FOREIGN KEY (vcenter_snapshot_id) REFERENCES snapshot(id)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- active_key replaces the SQLite partial unique index over IFNULL(...) WHERE active_yn=1.
CREATE TABLE IF NOT EXISTS reconciliation_exception (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    exception_type VARCHAR(64) NOT NULL,
    cm_id VARCHAR(128),
    rv_asset_key VARCHAR(255),
    server_name VARCHAR(255),
    reason VARCHAR(1000) NOT NULL,
    valid_from VARCHAR(32),
    valid_to VARCHAR(32),
    active_yn TINYINT NOT NULL DEFAULT 1,
    created_by VARCHAR(128) NOT NULL,
    created_at VARCHAR(32) NOT NULL,
    updated_by VARCHAR(128),
    updated_at VARCHAR(32),
    deactivated_by VARCHAR(128),
    deactivated_at VARCHAR(32),
    active_key VARCHAR(460) GENERATED ALWAYS AS (
        IF(active_yn = 1,
           CONCAT(exception_type, '\n', IFNULL(cm_id, ''), '\n', IFNULL(rv_asset_key, '')),
           NULL)
    ) STORED,
    UNIQUE KEY uq_recon_exception_active (active_key),
    KEY idx_recon_exception_active (active_yn, valid_from, valid_to)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- dedup_key replaces the SQLite expression unique index over IFNULL(...).
CREATE TABLE IF NOT EXISTS vcenter_resource_daily (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    stat_date VARCHAR(32) NOT NULL,
    entity_type VARCHAR(32) NOT NULL,
    vcenter_id VARCHAR(128),
    esxi_host VARCHAR(255),
    vm_uuid VARCHAR(128),
    vm_name VARCHAR(255),
    cpu_max_pct DOUBLE,
    cpu_avg_pct DOUBLE,
    mem_max_pct DOUBLE,
    mem_avg_pct DOUBLE,
    sample_count INT NOT NULL DEFAULT 0,
    collection_status VARCHAR(32) NOT NULL DEFAULT 'SUCCESS',
    source_name VARCHAR(128) NOT NULL DEFAULT 'VM_ResourceUsageExport',
    raw_json LONGTEXT,
    created_at VARCHAR(32) NOT NULL,
    dedup_key VARCHAR(700) GENERATED ALWAYS AS (
        CONCAT(stat_date, '\n', entity_type, '\n', IFNULL(vcenter_id, ''), '\n',
               IFNULL(esxi_host, ''), '\n', IFNULL(vm_uuid, ''), '\n', IFNULL(vm_name, ''))
    ) STORED,
    UNIQUE KEY uq_vcenter_resource_daily (dedup_key),
    KEY idx_vcenter_resource_daily_date (stat_date DESC, entity_type)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS resource_usage_run (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    daily_batch_id BIGINT,
    vcenter_snapshot_id BIGINT,
    period_start VARCHAR(32) NOT NULL,
    period_end VARCHAR(32) NOT NULL,
    started_at VARCHAR(32) NOT NULL,
    ended_at VARCHAR(32),
    status VARCHAR(32) NOT NULL DEFAULT 'RUNNING',
    success_scope_json VARCHAR(1000) NOT NULL DEFAULT '[]',
    failed_scope_json VARCHAR(1000) NOT NULL DEFAULT '{}',
    host_count INT NOT NULL DEFAULT 0,
    vm_count INT NOT NULL DEFAULT 0,
    error_message VARCHAR(2000),
    metadata_json TEXT,
    KEY idx_resource_usage_run_period (period_start DESC, period_end DESC, started_at DESC),
    KEY idx_resource_usage_run_batch (daily_batch_id),
    CONSTRAINT fk_usage_run_batch FOREIGN KEY (daily_batch_id) REFERENCES daily_batch_run(id) ON DELETE SET NULL,
    CONSTRAINT fk_usage_run_snapshot FOREIGN KEY (vcenter_snapshot_id) REFERENCES snapshot(id) ON DELETE SET NULL
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS host_resource_usage_daily (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    run_id BIGINT NOT NULL,
    stat_date VARCHAR(32) NOT NULL,
    vcenter_id VARCHAR(128) NOT NULL,
    service_name VARCHAR(255),
    cluster_name VARCHAR(255),
    esxi_host VARCHAR(255) NOT NULL,
    vm_count INT NOT NULL DEFAULT 0,
    allocated_cpu_cores INT,
    allocated_memory_mb BIGINT,
    cpu_max_pct DOUBLE,
    cpu_avg_pct DOUBLE,
    mem_max_pct DOUBLE,
    mem_avg_pct DOUBLE,
    sample_count INT NOT NULL DEFAULT 0,
    collection_status VARCHAR(32) NOT NULL DEFAULT 'SUCCESS',
    raw_json LONGTEXT,
    created_at VARCHAR(32) NOT NULL,
    UNIQUE KEY uq_host_resource_usage (run_id, vcenter_id, esxi_host),
    KEY idx_host_resource_usage_period (stat_date DESC, vcenter_id, esxi_host),
    CONSTRAINT fk_host_usage_run FOREIGN KEY (run_id) REFERENCES resource_usage_run(id) ON DELETE CASCADE
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- dedup_key replaces the SQLite expression unique index over IFNULL(vm_uuid,'').
CREATE TABLE IF NOT EXISTS vm_resource_usage_daily (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    run_id BIGINT NOT NULL,
    stat_date VARCHAR(32) NOT NULL,
    vcenter_snapshot_id BIGINT,
    asset_key VARCHAR(255),
    vcenter_id VARCHAR(128) NOT NULL,
    service_name VARCHAR(255),
    cluster_name VARCHAR(255),
    esxi_host VARCHAR(255),
    vm_uuid VARCHAR(128),
    vm_name VARCHAR(255) NOT NULL,
    power_state VARCHAR(32),
    allocated_cpu_cores INT,
    allocated_memory_mb BIGINT,
    cpu_max_pct DOUBLE,
    cpu_avg_pct DOUBLE,
    mem_max_pct DOUBLE,
    mem_avg_pct DOUBLE,
    sample_count INT NOT NULL DEFAULT 0,
    inventory_status VARCHAR(32) NOT NULL DEFAULT 'CURRENT',
    collection_status VARCHAR(32) NOT NULL DEFAULT 'SUCCESS',
    raw_json LONGTEXT,
    created_at VARCHAR(32) NOT NULL,
    dedup_key VARCHAR(700) GENERATED ALWAYS AS (
        CONCAT(run_id, '\n', vcenter_id, '\n', vm_name, '\n', IFNULL(vm_uuid, ''))
    ) STORED,
    UNIQUE KEY uq_vm_resource_usage_daily (dedup_key),
    KEY idx_vm_resource_usage_period (stat_date DESC, vcenter_id, esxi_host, vm_name),
    KEY idx_vm_resource_usage_uuid (vm_uuid, stat_date DESC),
    CONSTRAINT fk_vm_usage_run FOREIGN KEY (run_id) REFERENCES resource_usage_run(id) ON DELETE CASCADE
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Cross-WAS mutual exclusion for the daily batch. Shared by both engines.
CREATE TABLE IF NOT EXISTS process_lock (
    lock_name VARCHAR(128) NOT NULL PRIMARY KEY,
    owner VARCHAR(255) NOT NULL,
    acquired_at VARCHAR(32) NOT NULL,
    expires_at VARCHAR(32) NOT NULL
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
