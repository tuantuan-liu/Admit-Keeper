-- admit_keeper.db 参考 Schema（与 admit_keeper/db.py 的 SCHEMA_SQL 一致）
-- 供手动建库 / 迁移核对。运行时由 MCP open_init() 幂等建表，一般无需手动执行。
-- 注：`CREATE TABLE IF NOT EXISTS` **不会**给已存在的表加列。缺 `granted_by` 的旧库由
--     db.ensure_schema()->_migrate() 自动 `ALTER TABLE ADD COLUMN granted_by TEXT` 补齐
--     （旧行补为 NULL = 非窗口引入，安全默认）；读路径另有 2 列降级兜底。

CREATE TABLE IF NOT EXISTS admit_allowed (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    platform   TEXT NOT NULL,
    identity   TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active',   -- active | banned
    granted_at TEXT,
    expires_at TEXT,                             -- UTC ISO-8601；NULL=永久
    granted_by TEXT,
    note       TEXT,
    created_at TEXT,
    updated_at TEXT,
    UNIQUE(platform, identity)
);

-- 临时准入窗口（限时开放）：窗口内全新身份可进，落库到期=窗口结束。见 ADR-8。
CREATE TABLE IF NOT EXISTS admit_window (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    platform   TEXT NOT NULL,
    start_at   TEXT NOT NULL,   -- UTC ISO-8601，含边界
    end_at     TEXT NOT NULL,   -- UTC ISO-8601，不含（半开区间 [start, end)）
    note       TEXT,
    created_by TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_admit_window_platform ON admit_window(platform, start_at, end_at);
