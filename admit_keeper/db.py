"""共享数据层 —— admit_keeper.db 的连接、建表、路径解析、读取。

准入层（热路径只读）与 MCP（低频读写）共用这一份，保证两端读写同一个库、同一套判词。
仅标准库（sqlite3 / pathlib / datetime），准入层侧无需额外依赖。
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SCHEMA_SQL = """
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
"""


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def db_path() -> str:
    """DB 路径：优先 ADMIT_KEEPER_DB，否则 $HERMES_HOME/admit_keeper.db。"""
    p = env("ADMIT_KEEPER_DB")
    if p:
        return p
    home = env("HERMES_HOME") or str(Path.home() / ".hermes")
    return str(Path(home) / "admit_keeper.db")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db: Optional[str] = None, *, hot: bool = False) -> sqlite3.Connection:
    """打开连接。hot=True（准入层热路径）跳过 WAL pragma，避免每条消息都写盘。
    非 hot（MCP 写路径）启用 WAL + busy_timeout，降低读写锁冲突。
    """
    con = sqlite3.connect(db or db_path(), timeout=2)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=2500")
    if not hot:
        try:
            con.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass
    return con


def ensure_schema(con: sqlite3.Connection) -> None:
    # executescript 而非 execute：SCHEMA_SQL 含多条语句，execute 一次只允许一条。
    con.executescript(SCHEMA_SQL)
    _migrate(con)


def _migrate(con: sqlite3.Connection) -> None:
    """把旧库补齐到当前 schema —— `CREATE TABLE IF NOT EXISTS` 不会给**已存在**的表加列。

    目前只需补 `granted_by`（ADR-8 的来源标记）。旧记录补出来是 NULL = 非窗口引入，
    即不会被窗口重入放行，正是安全默认。读路径另有降级兜底（见 `lookup`）。
    """
    cols = {r[1] for r in con.execute("PRAGMA table_info(admit_allowed)")}
    if "granted_by" not in cols:
        con.execute("ALTER TABLE admit_allowed ADD COLUMN granted_by TEXT")


def open_init(db: Optional[str] = None) -> sqlite3.Connection:
    """打开并确保建表（幂等）。=== MCP 写路径入口 ===。"""
    con = connect(db)
    ensure_schema(con)
    con.commit()
    return con


_SELECT_RECORD = "SELECT status, expires_at, granted_by FROM admit_allowed WHERE platform=? AND identity=?"
_SELECT_RECORD_LEGACY = "SELECT status, expires_at FROM admit_allowed WHERE platform=? AND identity=?"


def lookup(con: sqlite3.Connection, platform: str, identity: str) -> Optional[tuple[str, Optional[str], Optional[str]]]:
    """取 (status, expires_at, granted_by)，无记录返回 None。仅读。

    granted_by 是**来源标记**：``WINDOW_GRANTED_BY`` 表示该记录由临时准入窗口引入，
    窗口重开时只重新纳入这类记录（见 ADR-8），手工 / 付费授权不受影响。

    对**缺 granted_by 列的旧库**自动降级为 2 列查询（granted_by 视作 None）：
    否则 `no such column` 会被 ``lookup_plugin`` 的 `except sqlite3.Error` 兜底吃成
    ``unavailable``，把整条判定拖进 fail-open / fail-closed —— 即 ADR-1 的最坏情形
    （与 ``lookup_window_plugin`` 的降级同理）。新库由 ``ensure_schema`` 迁移补齐该列。
    """
    try:
        row = con.execute(_SELECT_RECORD, (platform, identity)).fetchone()
    except sqlite3.OperationalError:  # 旧库缺列 → 降级
        row = con.execute(_SELECT_RECORD_LEGACY, (platform, identity)).fetchone()
    if row is None:
        return None
    return row[0], row[1], (row[2] if len(row) > 2 else None)


def lookup_plugin(platform: str, identity: str, db: Optional[str] = None) -> tuple[Optional[tuple[str, Optional[str], Optional[str]]], bool]:
    """准入层热路径只读入口。

    返回 (record, unavailable)：
      - record      None 表示表存在但无该身份记录；否则为 (status, expires_at, granted_by)；
      - unavailable True 表示 DB 缺失 / 读取异常 / 表未建（无法判定，交给 fail_open）。
    DB 文件不存在时直接视为 unavailable，不创建空文件。
    """
    d = db or db_path()
    if not os.path.exists(d):
        return None, True
    try:
        con = connect(d, hot=True)
        try:
            return lookup(con, platform, identity), False
        finally:
            con.close()
    except sqlite3.Error:
        return None, True


WINDOW_ANY = "*"  # 窗口 platform 通配：一次开窗即覆盖所有平台
WINDOW_GRANTED_BY = "window"  # granted_by 的来源标记：该记录由临时窗口引入（窗口重开只重新纳入这类）


def lookup_window(con: sqlite3.Connection, platform: str, now: str) -> Optional[str]:
    """当前开放窗口的 end_at（**本平台 或 通配 `*`**，取 end_at 最大者），无则 None。

    半开区间 [start_at, end_at)。通配窗口与平台专属窗口可共存，取更晚的结束时间。
    """
    row = con.execute(
        "SELECT end_at FROM admit_window "
        "WHERE (platform=? OR platform=?) AND start_at<=? AND ?<end_at "
        "ORDER BY end_at DESC LIMIT 1",
        (platform, WINDOW_ANY, now, now),
    ).fetchone()
    return row["end_at"] if row is not None else None


def lookup_window_plugin(platform: str, now: Optional[str] = None, db: Optional[str] = None) -> Optional[str]:
    """准入层热路径：查当前开放窗口，返回 end_at，无则 None。

    **与 lookup_plugin 的 unavailable 语义刻意不同**：窗口是可选特性，DB 缺失 / 表未建 /
    任何读取异常一律降级为「无窗口」(None)，**绝不** 返回 unavailable —— 否则旧库（无
    admit_window 表）会因 `no such table` 把整条准入判定拖进 fail-open 全放行（最坏情形）。
    """
    d = db or db_path()
    if not os.path.exists(d):
        return None
    try:
        con = connect(d, hot=True)
        try:
            return lookup_window(con, platform, now or now_iso())
        finally:
            con.close()
    except sqlite3.Error:
        return None


def grant_window_entry(platform: str, identity: str, end_at: str, *,
                       db: Optional[str] = None, by: str = WINDOW_GRANTED_BY) -> None:
    """窗口进入者落库：upsert 为 active、到期=窗口 end_at、来源标记 granted_by=``by``。

    ON CONFLICT 带 `WHERE status <> 'banned'`：即便并发下有记录，也**绝不复活被封禁者**。
    窗口重开时由 gate 再次调用本函数，把到期顺延到新窗口结束（reason ``window_reentry``）。
    """
    now = now_iso()
    con = open_init(db)
    try:
        con.execute(
            """
            INSERT INTO admit_allowed
                (platform, identity, status, granted_at, expires_at, granted_by, note, created_at, updated_at)
            VALUES (?,?, 'active', ?, ?, ?, 'window', ?, ?)
            ON CONFLICT(platform, identity) DO UPDATE SET
                status     = 'active',
                granted_at = excluded.granted_at,
                expires_at = excluded.expires_at,
                granted_by = excluded.granted_by,
                note       = excluded.note,
                updated_at = excluded.updated_at
            WHERE admit_allowed.status <> 'banned'
            """,
            (platform, identity, now, end_at, by, now, now),
        )
        con.commit()
    finally:
        con.close()
