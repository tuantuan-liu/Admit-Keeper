"""共享数据层 —— admit_keeper.db 的连接、建表、路径解析、读取。

插件（热路径只读）与 MCP（低频读写）共用这一份，保证两端读写同一个库、同一套判词。
仅标准库（sqlite3 / pathlib / datetime），插件侧无需额外依赖。
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
    """打开连接。hot=True（插件热路径）跳过 WAL pragma，避免每条消息都写盘。
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
    con.execute(SCHEMA_SQL)


def open_init(db: Optional[str] = None) -> sqlite3.Connection:
    """打开并确保建表（幂等）。=== MCP 写路径入口 ===。"""
    con = connect(db)
    ensure_schema(con)
    con.commit()
    return con


def lookup(con: sqlite3.Connection, platform: str, identity: str) -> Optional[tuple[str, Optional[str]]]:
    """取 (status, expires_at)，无记录返回 None。仅读。"""
    row = con.execute(
        "SELECT status, expires_at FROM admit_allowed WHERE platform=? AND identity=?",
        (platform, identity),
    ).fetchone()
    if row is None:
        return None
    return row["status"], row["expires_at"]


def lookup_plugin(platform: str, identity: str, db: Optional[str] = None) -> tuple[Optional[tuple[str, Optional[str]]], bool]:
    """插件热路径只读入口。

    返回 (record, unavailable)：
      - record      None 表示表存在但无该身份记录；
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
