"""MCP 工具级集成测试 —— 直接调用 FastMCP 的 grant/ban/unban/extend/query/…
跑真实 SQLite（临时文件），验证「工具 → db → 插件可读」整条链路。

前置：uv 环境含 mcp[cli]（钉 mcp<2）。运行：uv run pytest tests/test_mcp_integration.py
"""
from __future__ import annotations

import os
import re

import pytest

import admit_keeper_mcp as mcp  # 顶层模块导入，与运行时一致
from db import lookup_plugin


# FastMCP v1 的 @mcp.tool() 返回原函数，可直接以普通函数调用。
grant = mcp.grant
ban = mcp.ban
unban = mcp.unban
extend = mcp.extend
query = mcp.query
get_expired = mcp.get_expired
list_all = mcp.list_all
remove = mcp.remove


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
    assert "banned" in ban("feishu", "ou_1", note="滥用")
    assert query("feishu", "ou_1").startswith("status=banned")
    assert "unbanned" in unban("feishu", "ou_1")
    assert query("feishu", "ou_1").startswith("status=active")
    assert "expires=None" in query("feishu", "ou_1")  # 解封清空到期


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
