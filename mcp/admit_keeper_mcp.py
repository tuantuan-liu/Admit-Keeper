#!/usr/bin/env python3
"""admit-keeper MCP server —— 授权管理层。

写 admit_keeper.db，与准入插件共用同一库、同一份判定语义（db.py / policy.py）。

启动方式：由 Hermes 的 mcp_servers 以 stdio 启动。
前置：pip install -U "mcp[cli]>=1.0,<2"   （mcp 2.x 已把 FastMCP 改名/移除，需钉 <2 用 v1 API）
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

# ---- 路径引导：优先同目录（运行时：db.py/policy.py 与 mcp 同放 profile 目录），
#      否则退回仓库包（开发/测试：../admit_keeper 目录）。 ----
_HERE = os.path.dirname(os.path.abspath(__file__))
for _cand in (_HERE, os.path.join(os.path.dirname(_HERE), "admit_keeper")):
    if _cand not in sys.path:
        sys.path.insert(0, _cand)

from db import now_iso, open_init  # noqa: E402
from policy import STATUS_ACTIVE, STATUS_BANNED  # noqa: E402

try:
    from mcp.server.fastmcp import FastMCP  # noqa: E402
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "缺少 mcp[cli]。请先执行: pip install -U \"mcp[cli]\"\n"
        f"(根因: {exc})"
    ) from exc

mcp = FastMCP("admit-keeper")


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _platform(value: str) -> str:
    value = (value or "").strip().lower()
    if not value:
        raise ValueError("platform 不能为空")
    return value


def _identity(value: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError("identity 不能为空")
    return value


def _upsert(con, platform: str, identity: str, *, status: str, granted_at: str,
            expires_at: Optional[str], granted_by: str, note: str) -> None:
    now = now_iso()
    con.execute(
        """
        INSERT INTO admit_allowed
            (platform, identity, status, granted_at, expires_at, granted_by, note, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(platform, identity) DO UPDATE SET
            status=excluded.status,
            granted_at=excluded.granted_at,
            expires_at=excluded.expires_at,
            granted_by=excluded.granted_by,
            note=excluded.note,
            updated_at=excluded.updated_at
        """,
        (platform, identity, status, granted_at, expires_at, granted_by, note, now, now),
    )


@mcp.tool()
def grant(platform: str, identity: str, days: Optional[float] = None,
          by: str = "admin", note: str = "") -> str:
    """授权/续期身份为 active。days 为空=永久授权。platform: feishu/wecom/telegram/任意。"""
    platform = _platform(platform)
    identity = _identity(identity)
    expires_at = _iso(days) if days is not None else None
    con = open_init()
    try:
        _upsert(con, platform, identity, status=STATUS_ACTIVE, granted_at=now_iso(),
                expires_at=expires_at, granted_by=by, note=note)
        con.commit()
    finally:
        con.close()
    return f"granted {platform}:{identity} days={days} expires={expires_at or 'never'} by={by}"


@mcp.tool()
def ban(platform: str, identity: str, note: str = "") -> str:
    """封禁（立即拒绝，无视到期时间）。note 建议填封禁原因以备审计。"""
    platform = _platform(platform)
    identity = _identity(identity)
    con = open_init()
    now = now_iso()
    try:
        deleted = con.execute(
            "UPDATE admit_allowed SET status='banned', updated_at=?, note=? WHERE platform=? AND identity=?",
            (now, note, platform, identity),
        ).rowcount
        if deleted == 0:
            # 无既有记录 → 显式插一条 banned（granted_by 记为 system）
            con.execute(
                "INSERT INTO admit_allowed(platform,identity,status,granted_by,note,created_at,updated_at) "
                "VALUES(?,?,?,'system',?,?,?)",
                (platform, identity, STATUS_BANNED, note, now, now),
            )
        con.commit()
    finally:
        con.close()
    return f"banned {platform}:{identity} note={note or '-'}"


@mcp.tool()
def unban(platform: str, identity: str) -> str:
    """解封并清除到期时间，恢复 active。"""
    platform = _platform(platform)
    identity = _identity(identity)
    con = open_init()
    try:
        con.execute("UPDATE admit_allowed SET status='active', expires_at=NULL, updated_at=? "
                    "WHERE platform=? AND identity=?", (now_iso(), platform, identity))
        con.commit()
    finally:
        con.close()
    return f"unbanned {platform}:{identity}"


@mcp.tool()
def extend(platform: str, identity: str, days: float) -> str:
    """在现有到期时间基础上追加 days 天；无记录则新建（视为新授权）。
    b>注意：对 banned 身份不生效，需先 unban。</b>"""
    platform = _platform(platform)
    identity = _identity(identity)
    if days < 0:
        raise ValueError("days 不能为负")

    con = open_init()
    try:
        row = con.execute("SELECT status, expires_at FROM admit_allowed WHERE platform=? AND identity=?",
                          (platform, identity)).fetchone()
        if row and row["status"] == STATUS_BANNED:
            raise ValueError(f"{platform}:{identity} 当前为 banned，请先 unban 再续期")
        base = datetime.now(timezone.utc)
        if row and row["expires_at"]:
            try:
                base = datetime.strptime(row["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                base = datetime.now(timezone.utc)
        new_exp = (base + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _upsert(con, platform, identity, status=STATUS_ACTIVE, granted_at=now_iso(),
                expires_at=new_exp, granted_by="admin", note="extend")
        con.commit()
    finally:
        con.close()
    return f"extended {platform}:{identity} +{days}d -> {new_exp}"


@mcp.tool()
def query(platform: str, identity: str) -> str:
    """查询单个身份状态。"""
    platform = _platform(platform)
    identity = _identity(identity)
    con = open_init()
    try:
        row = con.execute("SELECT * FROM admit_allowed WHERE platform=? AND identity=?",
                          (platform, identity)).fetchone()
    finally:
        con.close()
    if row is None:
        return f"NOT_FOUND {platform}:{identity}"
    return (f"status={row['status']} expires={row['expires_at']} by={row['granted_by']} "
            f"note={row['note']} updated={row['updated_at']}")


@mcp.tool()
def get_expired() -> str:
    """列出所有已过期或被封禁的身份。"""
    con = open_init()
    try:
        rows = con.execute(
            "SELECT platform,identity,status,expires_at FROM admit_allowed "
            "WHERE status='banned' OR (expires_at IS NOT NULL AND expires_at <= ?)",
            (now_iso(),),
        ).fetchall()
    finally:
        con.close()
    if not rows:
        return "无过期/封禁记录"
    return "\n".join(f"{r['platform']}:{r['identity']} [{r['status']}] exp={r['expires_at']}" for r in rows)


@mcp.tool()
def list_all(platform: Optional[str] = None) -> str:
    """列出全部记录（可按平台过滤）。"""
    con = open_init()
    try:
        if platform:
            platform = _platform(platform)
            rows = con.execute("SELECT platform,identity,status,expires_at FROM admit_allowed WHERE platform=?", (platform,)).fetchall()
        else:
            rows = con.execute("SELECT platform,identity,status,expires_at FROM admit_allowed").fetchall()
    finally:
        con.close()
    if not rows:
        return "空"
    return "\n".join(f"{r['platform']}:{r['identity']} [{r['status']}] exp={r['expires_at']}" for r in rows)


@mcp.tool()
def remove(platform: str, identity: str) -> str:
    """彻底删除一条记录。删除后若无白名单兜底，将被拒收。"""
    platform = _platform(platform)
    identity = _identity(identity)
    con = open_init()
    try:
        con.execute("DELETE FROM admit_allowed WHERE platform=? AND identity=?", (platform, identity))
        con.commit()
    finally:
        con.close()
    return f"removed {platform}:{identity}"


if __name__ == "__main__":
    mcp.run()
