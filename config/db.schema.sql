-- admit_keeper.db 参考 Schema（与 admit_keeper/db.py 的 SCHEMA_SQL 一致）
-- 供手动建库 / 迁移核对。运行时由 MCP open_init() 幂等建表，一般无需手动执行。

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
