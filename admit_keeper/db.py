"""共享数据层 —— admit_keeper.db 的连接、建表、路径解析、读取。

准入层（热路径只读）与 MCP（低频读写）共用这一份，保证两端读写同一个库、同一套判词。

数据库后端可切换：随引擎而异的部分（连接参数、DDL、迁移、异常类型、参数占位符、UPSERT
语法）全部收在 ``Backend`` 接口里，当前仅内置 SQLite（标准库，零额外依赖）。要适配 MySQL：
实现 ``class MySQLBackend(Backend)`` 并 ``register_backend(MySQLBackend)``，用环境变量
``ADMIT_DB_BACKEND=mysql`` 选择即可，``db.py`` 的调用方（gate / mcp）无需改动。

说明（为何后端抽象就地放本模块、而不拆成兄弟模块）：本模块被**两种方式**导入 —— 插件包内
``admit_keeper.db``，以及 MCP / 脚本的顶层 ``db``。一旦出现包内相对导入，顶层导入方式即
ImportError（同类坑见 docs/design-decisions.md ADR-8）。把后端接口留在本模块内，两种导入
都成立。
"""

from __future__ import annotations

import os
import sqlite3
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ============================ 后端抽象 ============================


class Backend(ABC):
    """数据库后端接口：把「随引擎而异」的部分收敛在此，便于后续适配 MySQL 等。"""

    name: str = ""
    placeholder: str = "?"  # SQL 参数占位符：SQLite '?'，MySQL '%s'
    error: type[Exception] = Exception  # 「数据库异常」类型，用于可用性降级判定

    @abstractmethod
    def connect(self, target: str, *, hot: bool) -> Any:
        """打开到 target 的连接。hot=True（热路径只读）跳过写盘型设置。"""

    @abstractmethod
    def ensure_schema(self, con: Any) -> None:
        """幂等建表，并把旧库迁移到当前 schema。"""

    @abstractmethod
    def upsert_allowed(
        self,
        con: Any,
        platform: str,
        identity: str,
        *,
        status: str,
        granted_at: Optional[str],
        expires_at: Optional[str],
        granted_by: Optional[str],
        note: Optional[str],
        keep_banned: bool = False,
    ) -> None:
        """UPSERT 一条 admit_allowed 记录（各引擎的 upsert 语法不同，故收在后端）。

        keep_banned=True 时需保证**绝不复活被封禁者**（即不更新 status=='banned' 的行）。
        """


class SQLiteBackend(Backend):
    """SQLite 后端（标准库 sqlite3，零额外依赖，当前默认）。"""

    name = "sqlite"
    placeholder = "?"
    error = sqlite3.Error

    SCHEMA = """
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

    def connect(self, target: str, *, hot: bool = False) -> sqlite3.Connection:
        """打开连接。hot=True（准入层热路径）跳过 WAL pragma，避免每条消息都写盘；
        非 hot（MCP 写路径）启用 WAL + busy_timeout，降低读写锁冲突。
        """
        con = sqlite3.connect(target, timeout=2)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=2500")
        if not hot:
            try:
                con.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error:
                pass
        return con

    def ensure_schema(self, con: sqlite3.Connection) -> None:
        # executescript 而非 execute：SCHEMA 含多条语句，execute 一次只允许一条。
        con.executescript(self.SCHEMA)
        self._migrate(con)

    def _migrate(self, con: sqlite3.Connection) -> None:
        """把旧库补齐到当前 schema —— `CREATE TABLE IF NOT EXISTS` 不会给**已存在**的表加列。

        目前只需补 `granted_by`（ADR-8 的来源标记）。旧记录补出来是 NULL = 非窗口引入，
        即不会被窗口重入放行，正是安全默认。读路径另有降级兜底（见 `lookup`）。
        """
        cols = {r[1] for r in con.execute("PRAGMA table_info(admit_allowed)")}
        if "granted_by" not in cols:
            con.execute("ALTER TABLE admit_allowed ADD COLUMN granted_by TEXT")

    def upsert_allowed(
        self,
        con: sqlite3.Connection,
        platform: str,
        identity: str,
        *,
        status: str,
        granted_at: Optional[str],
        expires_at: Optional[str],
        granted_by: Optional[str],
        note: Optional[str],
        keep_banned: bool = False,
    ) -> None:
        """SQLite 的 UPSERT：`ON CONFLICT(platform, identity) DO UPDATE`。

        keep_banned=True 时在 DO UPDATE 后追加 `WHERE admit_allowed.status <> 'banned'`，
        即便并发下已有记录，也**绝不复活被封禁者**（窗口进入落库用）。
        """
        now = now_iso()
        guard = " WHERE admit_allowed.status <> 'banned'" if keep_banned else ""
        con.execute(
            "INSERT INTO admit_allowed "
            "(platform, identity, status, granted_at, expires_at, granted_by, note, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(platform, identity) DO UPDATE SET "
            "status=excluded.status, granted_at=excluded.granted_at, "
            "expires_at=excluded.expires_at, granted_by=excluded.granted_by, "
            "note=excluded.note, updated_at=excluded.updated_at" + guard,
            (platform, identity, status, granted_at, expires_at, granted_by, note, now, now),
        )


_BACKENDS: dict[str, type[Backend]] = {SQLiteBackend.name: SQLiteBackend}


def register_backend(cls: type[Backend]) -> None:
    """注册一个后端实现（适配其它引擎时调用），然后可用 ADMIT_DB_BACKEND=<name> 选择。"""
    _BACKENDS[cls.name] = cls


def get_backend(name: Optional[str] = None) -> Backend:
    """取当前后端实例。name 缺省读环境变量 ``ADMIT_DB_BACKEND``，再缺省 ``sqlite``。"""
    key = (name or env("ADMIT_DB_BACKEND") or SQLiteBackend.name).strip().lower()
    try:
        cls = _BACKENDS[key]
    except KeyError as exc:
        raise ValueError(f"未知数据库后端: {key!r}（已注册: {sorted(_BACKENDS)}）") from exc
    return cls()


# 兼容旧名：SCHEMA_SQL 仍指向内置 SQLite 的建表语句。
SCHEMA_SQL = SQLiteBackend.SCHEMA


# ============================ 路径 / 时间 ============================


def env(key: str, default: str = "") -> str:
    """读环境变量并 strip，未设返回 default。"""
    return os.environ.get(key, default).strip()


def db_path() -> str:
    """DB 路径：优先 ADMIT_KEEPER_DB，否则 $HERMES_HOME/admit_keeper.db。"""
    p = env("ADMIT_KEEPER_DB")
    if p:
        return p
    home = env("HERMES_HOME") or str(Path.home() / ".hermes")
    return str(Path(home) / "admit_keeper.db")


def now_iso() -> str:
    """当前 UTC 时间（固定 ISO-8601 格式，字典序=时间序）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ============================ 连接 / 建表 ============================


def connect(db: Optional[str] = None, *, hot: bool = False) -> Any:
    """打开连接（委托当前后端）。hot=True 为热路径只读，跳过写盘型设置。"""
    return get_backend().connect(db or db_path(), hot=hot)


def ensure_schema(con: Any) -> None:
    """幂等建表 + 迁移到当前 schema（委托当前后端）。"""
    get_backend().ensure_schema(con)


def open_init(db: Optional[str] = None) -> Any:
    """打开并确保建表（幂等）。=== MCP 写路径入口 ===。"""
    con = connect(db)
    ensure_schema(con)
    con.commit()
    return con


def upsert_allowed(
    con: Any,
    platform: str,
    identity: str,
    *,
    status: str,
    granted_at: Optional[str] = None,
    expires_at: Optional[str] = None,
    granted_by: Optional[str] = None,
    note: Optional[str] = None,
    keep_banned: bool = False,
) -> None:
    """UPSERT 一条 admit_allowed 记录（委托当前后端，屏蔽引擎差异）。

    MCP 的 grant/extend 与窗口进入落库都走这里，避免 UPSERT 语法散落各处。
    """
    get_backend().upsert_allowed(
        con,
        platform,
        identity,
        status=status,
        granted_at=granted_at,
        expires_at=expires_at,
        granted_by=granted_by,
        note=note,
        keep_banned=keep_banned,
    )


def _select_record_sql(legacy: bool = False) -> str:
    """按后端占位符拼记录查询 SQL；legacy=True 为**旧库**（无 granted_by 列）的 2 列查询。"""
    ph = get_backend().placeholder
    cols = "status, expires_at" if legacy else "status, expires_at, granted_by"
    return f"SELECT {cols} FROM admit_allowed WHERE platform={ph} AND identity={ph}"


def lookup(con: Any, platform: str, identity: str) -> Optional[tuple[str, Optional[str], Optional[str]]]:
    """取 (status, expires_at, granted_by)，无记录返回 None。仅读。

    granted_by 是**来源标记**：``WINDOW_GRANTED_BY`` 表示该记录由临时准入窗口引入，
    窗口重开时只重新纳入这类记录（见 ADR-8），手工 / 付费授权不受影响。

    对**缺 granted_by 列的旧库**自动降级为 2 列查询（granted_by 视作 None）：
    否则 `no such column` 会被 ``lookup_plugin`` 的异常兜底吃成 ``unavailable``，把整条判定
    拖进 fail-open / fail-closed —— 即 ADR-1 的最坏情形（与 ``lookup_window_plugin`` 的降级同理）。
    新库由 ``ensure_schema`` 迁移补齐该列。
    """
    try:
        row = con.execute(_select_record_sql(), (platform, identity)).fetchone()
    except get_backend().error:  # 旧库缺列 -> 降级为 2 列
        row = con.execute(_select_record_sql(legacy=True), (platform, identity)).fetchone()
    if row is None:
        return None
    return row[0], row[1], (row[2] if len(row) > 2 else None)


def lookup_plugin(
    platform: str, identity: str, db: Optional[str] = None
) -> tuple[Optional[tuple[str, Optional[str], Optional[str]]], bool]:
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
    except get_backend().error:
        return None, True


WINDOW_ANY = "*"  # 窗口 platform 通配：一次开窗即覆盖所有平台
WINDOW_GRANTED_BY = "window"  # granted_by 的来源标记：该记录由临时窗口引入（窗口重开只重新纳入这类）


def lookup_window(con: Any, platform: str, now: str) -> Optional[str]:
    """当前开放窗口的 end_at（**本平台 或 通配 `*`**，取 end_at 最大者），无则 None。

    半开区间 [start_at, end_at)。通配窗口与平台专属窗口可共存，取更晚的结束时间。
    """
    ph = get_backend().placeholder
    row = con.execute(
        "SELECT end_at FROM admit_window "
        f"WHERE (platform={ph} OR platform={ph}) AND start_at<={ph} AND {ph}<end_at "
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
    except get_backend().error:
        return None


def grant_window_entry(
    platform: str, identity: str, end_at: str, *, db: Optional[str] = None, by: str = WINDOW_GRANTED_BY
) -> None:
    """窗口进入者落库：upsert 为 active、到期=窗口 end_at、来源标记 granted_by=``by``。

    keep_banned=True 保证即便并发下有记录，也**绝不复活被封禁者**（由后端实现该守卫）。
    窗口重开时由 gate 再次调用本函数，把到期顺延到新窗口结束（reason ``window_reentry``）。
    """
    con = open_init(db)
    try:
        upsert_allowed(
            con,
            platform,
            identity,
            status="active",
            granted_at=now_iso(),
            expires_at=end_at,
            granted_by=by,
            note="window",
            keep_banned=True,
        )
        con.commit()
    finally:
        con.close()
