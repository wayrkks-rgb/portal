PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 30000;

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS collection_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL,
    record_count INTEGER NOT NULL DEFAULT 0,
    success_scope_json TEXT NOT NULL DEFAULT '[]',
    failed_scope_json TEXT NOT NULL DEFAULT '[]',
    error_message TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_collection_run_source_time ON collection_run(source, started_at DESC);

CREATE TABLE IF NOT EXISTS snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    collection_run_id INTEGER NOT NULL REFERENCES collection_run(id),
    status TEXT NOT NULL,
    source_scope TEXT NOT NULL DEFAULT 'ALL',
    record_count INTEGER NOT NULL,
    checksum TEXT NOT NULL,
    UNIQUE(source, collection_run_id, source_scope)
);
CREATE INDEX IF NOT EXISTS idx_snapshot_source_date ON snapshot(source, snapshot_date DESC, collected_at DESC);

CREATE TABLE IF NOT EXISTS itsm_asset_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL REFERENCES snapshot(id) ON DELETE CASCADE,
    cm_id TEXT NOT NULL,
    normalized_hostname TEXT,
    primary_ip TEXT,
    ip_json TEXT NOT NULL DEFAULT '[]',
    cpu_cores INTEGER,
    memory_mb INTEGER,
    os_family TEXT,
    os_version TEXT,
    status_code TEXT,
    server_category_code TEXT,
    environment_code TEXT,
    eos_value TEXT,
    record_hash TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    UNIQUE(snapshot_id, cm_id)
);
CREATE INDEX IF NOT EXISTS idx_itsm_snapshot_cm ON itsm_asset_snapshot(cm_id, snapshot_id);
CREATE INDEX IF NOT EXISTS idx_itsm_snapshot_host ON itsm_asset_snapshot(normalized_hostname);
CREATE INDEX IF NOT EXISTS idx_itsm_snapshot_ip ON itsm_asset_snapshot(primary_ip);

CREATE TABLE IF NOT EXISTS rv_asset_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL REFERENCES snapshot(id) ON DELETE CASCADE,
    asset_key TEXT NOT NULL,
    vm_uuid TEXT,
    smbios_uuid TEXT,
    vm_id TEXT,
    vcenter TEXT,
    vm_name TEXT,
    dns_name TEXT,
    normalized_hostname TEXT,
    primary_ip TEXT,
    ip_json TEXT NOT NULL DEFAULT '[]',
    cpus INTEGER,
    memory_mb INTEGER,
    os_family TEXT,
    os_version TEXT,
    power_state TEXT,
    datacenter TEXT,
    cluster_name TEXT,
    esxi_host TEXT,
    template_flag INTEGER NOT NULL DEFAULT 0,
    srm_placeholder INTEGER NOT NULL DEFAULT 0,
    record_hash TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    UNIQUE(snapshot_id, asset_key)
);
CREATE INDEX IF NOT EXISTS idx_rv_snapshot_key ON rv_asset_snapshot(asset_key, snapshot_id);
CREATE INDEX IF NOT EXISTS idx_rv_snapshot_uuid ON rv_asset_snapshot(vm_uuid);
CREATE INDEX IF NOT EXISTS idx_rv_snapshot_host ON rv_asset_snapshot(normalized_hostname);
CREATE INDEX IF NOT EXISTS idx_rv_snapshot_ip ON rv_asset_snapshot(primary_ip);

CREATE TABLE IF NOT EXISTS asset_ip (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    snapshot_id INTEGER NOT NULL REFERENCES snapshot(id) ON DELETE CASCADE,
    asset_key TEXT NOT NULL,
    ip_address TEXT NOT NULL,
    is_primary INTEGER NOT NULL DEFAULT 0,
    UNIQUE(source, snapshot_id, asset_key, ip_address)
);
CREATE INDEX IF NOT EXISTS idx_asset_ip_address ON asset_ip(ip_address, snapshot_id);

CREATE TABLE IF NOT EXISTS identity_map (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cm_id TEXT NOT NULL,
    vm_uuid TEXT NOT NULL,
    active_yn INTEGER NOT NULL DEFAULT 1,
    approved_by TEXT,
    approved_at TEXT,
    note TEXT,
    UNIQUE(cm_id, vm_uuid)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_identity_map_active_cm
    ON identity_map(cm_id) WHERE active_yn=1;
CREATE UNIQUE INDEX IF NOT EXISTS uq_identity_map_active_uuid
    ON identity_map(vm_uuid) WHERE active_yn=1;

CREATE TABLE IF NOT EXISTS change_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    snapshot_id INTEGER NOT NULL REFERENCES snapshot(id) ON DELETE CASCADE,
    previous_snapshot_id INTEGER REFERENCES snapshot(id),
    asset_key TEXT NOT NULL,
    event_type TEXT NOT NULL,
    field_name TEXT,
    old_value TEXT,
    new_value TEXT,
    detected_at TEXT NOT NULL,
    group_key TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_change_event_period ON change_event(source, detected_at, event_type);
CREATE INDEX IF NOT EXISTS idx_change_event_asset ON change_event(asset_key, field_name, detected_at);

CREATE TABLE IF NOT EXISTS reconciliation_result (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    itsm_snapshot_id INTEGER NOT NULL REFERENCES snapshot(id) ON DELETE CASCADE,
    rv_snapshot_id INTEGER NOT NULL REFERENCES snapshot(id) ON DELETE CASCADE,
    cm_id TEXT,
    rv_asset_key TEXT,
    match_status TEXT NOT NULL,
    match_method TEXT,
    score INTEGER NOT NULL DEFAULT 0,
    drift_json TEXT NOT NULL DEFAULT '[]',
    reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(itsm_snapshot_id, rv_snapshot_id, cm_id, rv_asset_key)
);
CREATE INDEX IF NOT EXISTS idx_recon_status ON reconciliation_result(created_at, match_status);

CREATE TABLE IF NOT EXISTS sync_result (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    asset_identity TEXT NOT NULL,
    rv_event_type TEXT,
    itsm_event_type TEXT,
    sync_status TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS data_quality_rule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_name TEXT NOT NULL UNIQUE,
    field_name TEXT NOT NULL,
    status_filter_json TEXT NOT NULL DEFAULT '[]',
    server_category_filter_json TEXT NOT NULL DEFAULT '[]',
    rule_type TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'WARNING',
    enabled INTEGER NOT NULL DEFAULT 1,
    config_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS data_quality_result (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL REFERENCES snapshot(id) ON DELETE CASCADE,
    cm_id TEXT NOT NULL,
    field_name TEXT NOT NULL,
    quality_status TEXT NOT NULL,
    message TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quality_snapshot_status ON data_quality_result(snapshot_id, quality_status);

CREATE TABLE IF NOT EXISTS manual_asset_override (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cm_id TEXT NOT NULL,
    field_name TEXT NOT NULL,
    override_value TEXT,
    reason TEXT NOT NULL,
    approval_status TEXT NOT NULL DEFAULT 'DRAFT',
    valid_from TEXT,
    valid_to TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    approved_by TEXT,
    approved_at TEXT
);

CREATE TABLE IF NOT EXISTS data_quality_exception (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cm_id TEXT NOT NULL,
    field_name TEXT,
    reason TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT,
    reason TEXT,
    before_json TEXT,
    after_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(created_at DESC);

CREATE TABLE IF NOT EXISTS daily_batch_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_date TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL DEFAULT 'RUNNING',
    itsm_run_id INTEGER REFERENCES collection_run(id),
    itsm_snapshot_id INTEGER REFERENCES snapshot(id),
    vcenter_run_id INTEGER REFERENCES collection_run(id),
    vcenter_snapshot_id INTEGER REFERENCES snapshot(id),
    reconciliation_created_at TEXT,
    resource_usage_status TEXT NOT NULL DEFAULT 'PENDING_SCRIPT',
    error_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_daily_batch_date ON daily_batch_run(batch_date DESC, started_at DESC);

CREATE TABLE IF NOT EXISTS reconciliation_exception (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exception_type TEXT NOT NULL,
    cm_id TEXT,
    rv_asset_key TEXT,
    server_name TEXT,
    reason TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    active_yn INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_by TEXT,
    updated_at TEXT,
    deactivated_by TEXT,
    deactivated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_recon_exception_active ON reconciliation_exception(active_yn, valid_from, valid_to);

CREATE TABLE IF NOT EXISTS vcenter_resource_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stat_date TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    vcenter_id TEXT,
    esxi_host TEXT,
    vm_uuid TEXT,
    vm_name TEXT,
    cpu_max_pct REAL,
    cpu_avg_pct REAL,
    mem_max_pct REAL,
    mem_avg_pct REAL,
    sample_count INTEGER NOT NULL DEFAULT 0,
    collection_status TEXT NOT NULL DEFAULT 'SUCCESS',
    source_name TEXT NOT NULL DEFAULT 'VM_ResourceUsageExport',
    raw_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vcenter_resource_daily_date ON vcenter_resource_daily(stat_date DESC, entity_type);

CREATE UNIQUE INDEX IF NOT EXISTS uq_recon_exception_active ON reconciliation_exception(exception_type, IFNULL(cm_id,''), IFNULL(rv_asset_key,'')) WHERE active_yn=1;
CREATE UNIQUE INDEX IF NOT EXISTS uq_vcenter_resource_daily ON vcenter_resource_daily(stat_date, entity_type, IFNULL(vcenter_id,''), IFNULL(esxi_host,''), IFNULL(vm_uuid,''), IFNULL(vm_name,''));

-- v8: one resource-usage execution is linked to the same 07:00 daily batch and vCenter snapshot.
CREATE TABLE IF NOT EXISTS resource_usage_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    daily_batch_id INTEGER REFERENCES daily_batch_run(id) ON DELETE SET NULL,
    vcenter_snapshot_id INTEGER REFERENCES snapshot(id) ON DELETE SET NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL DEFAULT 'RUNNING',
    success_scope_json TEXT NOT NULL DEFAULT '[]',
    failed_scope_json TEXT NOT NULL DEFAULT '{}',
    host_count INTEGER NOT NULL DEFAULT 0,
    vm_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_resource_usage_run_period ON resource_usage_run(period_start DESC, period_end DESC, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_resource_usage_run_batch ON resource_usage_run(daily_batch_id);

CREATE TABLE IF NOT EXISTS host_resource_usage_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES resource_usage_run(id) ON DELETE CASCADE,
    stat_date TEXT NOT NULL,
    vcenter_id TEXT NOT NULL,
    service_name TEXT,
    cluster_name TEXT,
    esxi_host TEXT NOT NULL,
    vm_count INTEGER NOT NULL DEFAULT 0,
    allocated_cpu_cores INTEGER,
    allocated_memory_mb INTEGER,
    cpu_max_pct REAL,
    cpu_avg_pct REAL,
    mem_max_pct REAL,
    mem_avg_pct REAL,
    sample_count INTEGER NOT NULL DEFAULT 0,
    collection_status TEXT NOT NULL DEFAULT 'SUCCESS',
    raw_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(run_id, vcenter_id, esxi_host)
);
CREATE INDEX IF NOT EXISTS idx_host_resource_usage_period ON host_resource_usage_daily(stat_date DESC, vcenter_id, esxi_host);

CREATE TABLE IF NOT EXISTS vm_resource_usage_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES resource_usage_run(id) ON DELETE CASCADE,
    stat_date TEXT NOT NULL,
    vcenter_snapshot_id INTEGER REFERENCES snapshot(id) ON DELETE SET NULL,
    asset_key TEXT,
    vcenter_id TEXT NOT NULL,
    service_name TEXT,
    cluster_name TEXT,
    esxi_host TEXT,
    vm_uuid TEXT,
    vm_name TEXT NOT NULL,
    power_state TEXT,
    allocated_cpu_cores INTEGER,
    allocated_memory_mb INTEGER,
    cpu_max_pct REAL,
    cpu_avg_pct REAL,
    mem_max_pct REAL,
    mem_avg_pct REAL,
    sample_count INTEGER NOT NULL DEFAULT 0,
    inventory_status TEXT NOT NULL DEFAULT 'CURRENT',
    collection_status TEXT NOT NULL DEFAULT 'SUCCESS',
    raw_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vm_resource_usage_period ON vm_resource_usage_daily(stat_date DESC, vcenter_id, esxi_host, vm_name);
CREATE INDEX IF NOT EXISTS idx_vm_resource_usage_uuid ON vm_resource_usage_daily(vm_uuid, stat_date DESC);

CREATE UNIQUE INDEX IF NOT EXISTS uq_vm_resource_usage_daily ON vm_resource_usage_daily(run_id, vcenter_id, vm_name, IFNULL(vm_uuid,''));

-- Cross-WAS mutual exclusion for the daily batch. Mirrored in schema_mysql.sql.
CREATE TABLE IF NOT EXISTS process_lock (
    lock_name TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
