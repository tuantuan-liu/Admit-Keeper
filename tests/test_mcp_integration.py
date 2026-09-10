"""MCP 工具级集成测试 —— 直接调用 FastMCP 的 grant/ban/unban/extend/query/…
跑真实 SQLite（临时文件），验证「工具 → db → 插件可读」整条链路。

前置：uv 环境含 mcp[cli]（钉 mcp<2）。运行：uv run pytest tests/test_mcp_integration.py
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

import pytest

import admit_keeper_mcp as mcp  # 顶层模块导入，与运行时一致
from db import lookup_plugin, lookup_window_plugin, now_iso
from policy import decide


# FastMCP v1 的 @mcp.tool() 返回原函数，可直接以普通函数调用。
grant = mcp.grant
ban = mcp.ban
unban = mcp.unban
extend = mcp.extend
query = mcp.query
get_expired = mcp.get_expired
list_all = mcp.list_all
remove = mcp.remove
open_window = mcp.open_window
close_window = mcp.close_window
list_windows = mcp.list_windows


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    """每个测试用独立临时 DB，避免相互污染。"""
    dbfile = tmp_path / "a.db"
    monkeypatch.setenv("ADMIT_KEEPER_DB", str(dbfile))
    monkeypatch.setenv("ADMIT_GATE_PLATFORMS", "feishu,wecom")
    return dbfile


def _expires(res: str) -> str | None:
    m = re.search(r"expires=(\S+)", res)
    return m.group(1) if m else None


def test_grant_permanent_and_query(db_env):
    assert "granted" in grant("feishu", "ou_1")
    res = query("feishu", "ou_1")
    assert res.startswith("status=active")
    assert "expires=None" in res  # 永久授权


def test_grant_with_days_sets_expiry(db_env):
    grant("feishu", "ou_1", days=7)
    exp = _expires(query("feishu", "ou_1"))
    assert exp and exp.endswith("Z")


def test_platform_isolation(db_env):
    grant("feishu", "ou_1", days=7)
    # 同一个 identity，未在 wecom 授权 → 查不到
    assert query("wecom", "ou_1").startswith("NOT_FOUND")


def test_grant_is_visible_to_plugin_read_path(db_env):
    grant("feishu", "ou_1", days=7)
    record, unavailable = lookup_plugin("feishu", "ou_1")
    assert unavailable is False
    assert record[0] == "active"
    assert record[1] is not None


def test_ban_unban_roundtrip(db_env):
    grant("feishu", "ou_1", days=7)
    exp_before = _expires(query("feishu", "ou_1"))
    assert "banned" in ban("feishu", "ou_1", note="滥用")
    assert query("feishu", "ou_1").startswith("status=banned")
    assert "unbanned" in unban("feishu", "ou_1")
    assert query("feishu", "ou_1").startswith("status=active")
    # ADR-9：解封**保留原期限**，不再清成永久（旧实现会 SET expires_at=NULL = 永久提权）
    assert exp_before and _expires(query("feishu", "ou_1")) == exp_before


# ---------------- P0 回归：unban 不得制造授权 / extend 对过期须生效 ----------------

def _judge(platform, identity) -> str:
    """走与插件同源的判定，返回 reason —— 用于断言「用户实际会不会被放行」。"""
    rec, unavail = lookup_plugin(platform, identity)
    return decide(identity, rec, allowlist=frozenset(), now=now_iso(),
                  fail_open=False, unavailable=unavail).reason


def test_unban_does_not_grant_permanent_to_expired(db_env):
    """P0-1 回归：给已过期用户解封，不得把他变成永久。"""
    grant("feishu", "ou_exp", days=-5)          # 5 天前已到期
    ban("feishu", "ou_exp")
    unban("feishu", "ou_exp")
    assert "expires=None" not in query("feishu", "ou_exp")   # 期限没被清空
    assert _judge("feishu", "ou_exp") == "deny:expired"      # 解封≠授权


def test_unban_keeps_permanent_user_permanent(db_env):
    """反之：本来就是永久的用户，解封后仍应永久（解封只翻封禁态，不改变授权维度）。"""
    grant("feishu", "ou_perm")                  # 永久授权
    ban("feishu", "ou_perm")
    unban("feishu", "ou_perm")
    assert "expires=None" in query("feishu", "ou_perm")
    assert _judge("feishu", "ou_perm") == "active"


def test_ban_then_unban_unknown_user_does_not_create_access(db_env):
    """P0-1 最危险的一支：封禁一个**从未有过记录**的人再解封，不得凭空造出永久授权。"""
    ban("feishu", "ou_ghost")
    unban("feishu", "ou_ghost")
    assert _judge("feishu", "ou_ghost") == "deny:expired"    # 曾经 `active + expires=NULL` = 永久


def test_unban_without_record_is_not_fake_success(db_env):
    """unban 对无记录身份不得伪造成功（也不再凭空建记录）。"""
    out = unban("feishu", "ou_nobody")
    assert out.startswith("NOT_FOUND")
    assert query("feishu", "ou_nobody").startswith("NOT_FOUND")   # 没建记录


def test_unban_non_banned_record_is_noop(db_env):
    grant("feishu", "ou_1", days=7)
    assert unban("feishu", "ou_1").startswith("NOT_FOUND")        # 本就不是 banned


def test_extend_expired_takes_effect_immediately(db_env):
    """P0-2 回归：给已过期用户续期，必须从**现在**起算 —— 旧实现以旧到期为基准，
    新到期仍落在过去，工具却报成功（运营最常用场景恰好失效且反馈骗人）。"""
    grant("feishu", "ou_exp", days=-10)
    out = extend("feishu", "ou_exp", 3)
    assert "立即生效" in out
    assert _judge("feishu", "ou_exp") == "active"


def test_extend_active_stacks_on_existing_expiry(db_env):
    """未到期时应在原到期上顺延（叠加），不吞掉剩余时间。"""
    grant("feishu", "ou_a", days=10)
    before = _expires(query("feishu", "ou_a"))
    extend("feishu", "ou_a", 3)
    after = _expires(query("feishu", "ou_a"))
    assert before and after and after > before
    assert "立即生效" in extend("feishu", "ou_a", 1)


def test_extend_moves_expiry_later(db_env):
    grant("feishu", "ou_1", days=7)
    before = _expires(query("feishu", "ou_1"))
    extend("feishu", "ou_1", 3)
    after = _expires(query("feishu", "ou_1"))
    assert before and after and after > before


def test_extend_banned_raises(db_env):
    grant("feishu", "ou_1", days=7)
    ban("feishu", "ou_1")
    with pytest.raises(ValueError):
        extend("feishu", "ou_1", 3)


def test_query_not_found(db_env):
    assert query("feishu", "ou_none").startswith("NOT_FOUND")


def test_get_expired_lists_banned_and_expired(db_env):
    ban("feishu", "ou_banned")           # 封禁
    grant("feishu", "ou_exp", days=-1)   # 立即过期（days 为负即昨到期）
    out = get_expired()
    assert "ou_banned" in out
    assert "ou_exp" in out
    assert "[banned]" in out


def test_list_all_and_filter(db_env):
    grant("feishu", "ou_f")
    grant("wecom", "ou_w")
    assert "feishu:ou_f" in list_all()
    assert "wecom:ou_w" in list_all()
    all_feishu = list_all("feishu")
    assert "feishu:ou_f" in all_feishu
    assert "wecom:ou_w" not in all_feishu


def test_remove_deletes_and_denies(db_env):
    grant("feishu", "ou_1")
    assert "removed" in remove("feishu", "ou_1")
    assert query("feishu", "ou_1").startswith("NOT_FOUND")
    # 删除后无记录 → 插件侧 deny-by-default（无白名单兜底）
    record, unavailable = lookup_plugin("feishu", "ou_1")
    assert record is None
    assert unavailable is False


# ---------------- 临时准入窗口（open_window / close_window / list_windows） ----------------

def _local_iso(delta_seconds: float) -> str:
    """本机本地时间戳（naive ISO，open_window 会按本地时区解释）。"""
    return (datetime.now() + timedelta(seconds=delta_seconds)).strftime("%Y-%m-%dT%H:%M:%S")


def _local_hhmm(delta_seconds: float) -> str:
    return (datetime.now() + timedelta(seconds=delta_seconds)).strftime("%H:%M")


def _win_id(res: str) -> int:
    return int(re.search(r"#(\d+)", res).group(1))


def test_open_window_and_list(db_env):
    res = open_window("feishu", start=_local_iso(-3600), end=_local_iso(3600), note="体验")
    assert "opened window #" in res
    out = list_windows()
    assert "[open]" in out and "feishu" in out and "体验" in out


def test_open_window_hhmm_shorthand(db_env):
    # 简写 HH:MM（今天本地）；含当前时刻 → 应处于 open。
    open_window("feishu", start=_local_hhmm(-3600), end=_local_hhmm(3600))
    assert "[open]" in list_windows("feishu")


def test_open_window_offset_converted_to_utc(db_env):
    res = open_window("feishu", start="2026-09-10T14:00:00+08:00", end="2026-09-10T16:00:00+08:00")
    # +08:00 14:00/16:00 → UTC 06:00/08:00
    assert "2026-09-10T06:00:00Z" in res
    assert "2026-09-10T08:00:00Z" in res


def test_open_window_start_not_before_end_raises(db_env):
    with pytest.raises(ValueError):
        open_window("feishu", start="2026-09-10T16:00:00", end="2026-09-10T14:00:00")


def test_list_windows_empty(db_env):
    assert list_windows() == "无窗口"


def test_upcoming_window_state(db_env):
    open_window("feishu", start=_local_iso(600), end=_local_iso(3600))
    assert "[upcoming]" in list_windows("feishu")


def test_close_window_closes_open(db_env):
    open_window("feishu", start=_local_iso(-3600), end=_local_iso(3600))
    assert "[open]" in list_windows("feishu")
    assert "closed 1 open window(s)" in close_window("feishu")
    assert "[closed]" in list_windows("feishu")
    # 无开放窗口后再关 → 幂等提示
    assert "无开放窗口" in close_window("feishu")


def test_close_window_by_id_deletes(db_env):
    wid = _win_id(open_window("feishu", start=_local_iso(600), end=_local_iso(3600)))  # 未来窗口
    assert "deleted window" in close_window("feishu", id=wid)
    assert list_windows("feishu") == "无窗口"


def test_window_visible_to_plugin_read_path(db_env):
    open_window("feishu", start=_local_iso(-3600), end=_local_iso(3600))
    assert lookup_window_plugin("feishu") is not None
    assert lookup_window_plugin("wecom") is None  # 其他平台无窗口


def test_open_window_wildcard_covers_all_platforms(db_env):
    res = open_window("*", start=_local_iso(-3600), end=_local_iso(3600), note="全平台")
    assert "所有平台" in res
    for p in ("feishu", "wecom", "telegram"):   # 含未在 ADMIT_GATE_PLATFORMS 的平台
        assert lookup_window_plugin(p) is not None
    assert "[open]" in list_windows("*")


def test_close_window_wildcard(db_env):
    open_window("*", start=_local_iso(-3600), end=_local_iso(3600))
    assert "closed 1 open window(s)" in close_window("*")
    assert lookup_window_plugin("feishu") is None
    # 关闭通配不影响平台专属窗口（反之亦然）
    open_window("feishu", start=_local_iso(-3600), end=_local_iso(3600))
    assert "* 当前无开放窗口" in close_window("*")
    assert lookup_window_plugin("feishu") is not None
