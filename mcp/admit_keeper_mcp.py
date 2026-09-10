#!/usr/bin/env python3
"""admit-keeper MCP server —— 授权管理层。

写 admit_keeper.db，与准入层共用同一库、同一份判定语义（db.py / policy.py）。

启动方式：由接入框架以 stdio 拉起（如 Hermes 的 mcp_servers）。
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


# ---- 临时准入窗口（admit_window）时间解析 ----
# 存库统一 UTC ISO-8601（与 db.now_iso() 同格式，字典序=时间序，见 ADR-6）。

def _to_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local(utc_iso: str) -> str:
    """把库里的 UTC ISO 串转本机本地时间，仅用于回显。"""
    dt = datetime.strptime(utc_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def _parse_one(value: str) -> tuple[datetime, bool]:
    """解析单个时间 → (aware datetime, 是否为 HH:MM 简写)。

    - `HH:MM` / `HH:MM:SS`（无日期）→ 今天本地该时刻，返回 time_only=True；
    - 完整 ISO-8601（`2026-09-10T14:00:00` 或带偏移 `…+08:00` / 末尾 `Z`）→ 按给定时刻；
      不带偏移者视为**本机本地时区**。
    """
    v = (value or "").strip()
    if not v:
        raise ValueError("时间不能为空")

    if "T" not in v and " " not in v:  # 疑似纯时间简写
        for fmt in ("%H:%M", "%H:%M:%S"):
            try:
                t = datetime.strptime(v, fmt).time()
            except ValueError:
                continue
            d = datetime.now().replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0)
            return d.astimezone(), True

    iso = v[:-1] + "+00:00" if v.endswith("Z") else v  # 兼容 Python 3.10 的 fromisoformat
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise ValueError(
            f"无法解析时间 {value!r}：用 ISO-8601（如 2026-09-10T14:00:00[+08:00]）或 HH:MM"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.astimezone()  # naive → 视为本机本地时区
    return dt, False


def _parse_window(start: str, end: str) -> tuple[str, str]:
    """解析窗口起止为 (start_utc, end_utc)。简写跨零点（如 22:00→02:00）时 end 顺延次日。"""
    s, _s_timeonly = _parse_one(start)
    e, e_timeonly = _parse_one(end)
    if e_timeonly and e <= s:
        e = e + timedelta(days=1)
    if s >= e:
        raise ValueError(f"start 必须早于 end（{s.isoformat()} >= {e.isoformat()}）")
    return _to_utc(s), _to_utc(e)


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


@mcp.tool()
def open_window(platform: str, start: str, end: str, note: str = "", by: str = "admin") -> str:
    """开设临时准入窗口 [start, end)：窗口内**全新身份**可进并自动落库到窗口结束；
    banned / 已过期身份不受影响。

    platform: 任意平台名（feishu/wecom/telegram/…），或 **通配 `*` = 所有平台一次生效**。
    时间可用 ISO-8601（2026-09-10T14:00:00[+08:00]）或简写 HH:MM（今天本地）。
    存库统一 UTC；跨零点简写（22:00→02:00）自动顺延次日。"""
    platform = _platform(platform)
    start_utc, end_utc = _parse_window(start, end)
    con = open_init()
    now = now_iso()
    try:
        cur = con.execute(
            "INSERT INTO admit_window(platform,start_at,end_at,note,created_by,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (platform, start_utc, end_utc, note, by, now, now),
        )
        con.commit()
        wid = cur.lastrowid
    finally:
        con.close()
    scope = "所有平台" if platform == "*" else platform
    return (f"opened window #{wid} {platform} [{scope}] {start_utc}->{end_utc} "
            f"(local {_local(start_utc)}->{_local(end_utc)}) note={note or '-'}")


@mcp.tool()
def close_window(platform: str, id: Optional[int] = None) -> str:
    """不给 id → 立即关闭该 platform **当前开放**的窗口（置 end_at=now）；
    给 id → 删除该条窗口记录（可取消尚未开始的未来窗口）。

    platform 需与开设时一致：关闭通配窗口用 `close_window("*")`。"""
    platform = _platform(platform)
    now = now_iso()
    con = open_init()
    try:
        if id is not None:
            n = con.execute("DELETE FROM admit_window WHERE id=? AND platform=?", (id, platform)).rowcount
            con.commit()
            return f"deleted window #{id} {platform}" if n else f"未找到 window #{id} ({platform})"
        n = con.execute(
            "UPDATE admit_window SET end_at=?, updated_at=? WHERE platform=? AND start_at<=? AND ?<end_at",
            (now, now, platform, now, now),
        ).rowcount
        con.commit()
    finally:
        con.close()
    return f"closed {n} open window(s) for {platform}" if n else f"{platform} 当前无开放窗口"


@mcp.tool()
def list_windows(platform: Optional[str] = None) -> str:
    """列出窗口及状态（open=进行中 / upcoming=未开始 / closed=已结束）。时间同时给 UTC 与本地。

    platform 省略=全部；给定则精确过滤（通配窗口以 `*` 存储，用 `list_windows("*")` 查）。
    注意：通配窗口对**任意平台**生效，但平台须在 `ADMIT_GATE_PLATFORMS` 内才走准入。"""
    now = now_iso()
    con = open_init()
    try:
        if platform:
            platform = _platform(platform)
            rows = con.execute("SELECT * FROM admit_window WHERE platform=? ORDER BY start_at", (platform,)).fetchall()
        else:
            rows = con.execute("SELECT * FROM admit_window ORDER BY platform, start_at").fetchall()
    finally:
        con.close()
    if not rows:
        return "无窗口"
    lines = []
    for r in rows:
        if r["start_at"] <= now < r["end_at"]:
            state = "open"
        elif now < r["start_at"]:
            state = "upcoming"
        else:
            state = "closed"
        lines.append(f"#{r['id']} {r['platform']} [{state}] {r['start_at']}->{r['end_at']} "
                     f"(local {_local(r['start_at'])}->{_local(r['end_at'])}) note={r['note'] or '-'}")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
