"""Hermes 接入层（admit_keeper/__init__.py）插件单测。

重构后 __init__ 退化为纯适配层：只做「事件 -> 平台/身份」的协议映射，判定全部委托给框架无关的
gate.gate()。本文件针对适配层做完整覆盖：short-circuit / 协议映射 / 判定分支 / fail-open 告警 /
异常兜底 / register()，覆盖 main 自带 test_gate.py 未覆盖的告警、register、异常三条链路。

判定分支本身（gate 层）在 test_gate.py 已测；此处再经适配层过一遍，保证整条插件链路端到端正确。
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

import admit_keeper as ak
from admit_keeper import _on_pre_gateway_dispatch

# ---------- 辅助构造 ----------


def _event(platform="feishu", user_id="ou_1", *, no_source=False):
    """构造伪事件；platform 可传字符串或带 .value 的枚举对象。"""
    if no_source:
        return SimpleNamespace(source=None)
    return SimpleNamespace(source=SimpleNamespace(platform=platform, user_id=user_id))


def _mkdb(dbfile, rows):
    """建库并插入 rows=[(platform,identity,status,expires_at), ...]，commit 后关闭。"""
    con = ak.db.open_init(str(dbfile))
    try:
        for platform, identity, status, expires_at in rows:
            con.execute(
                "INSERT INTO admit_allowed"
                "(platform,identity,status,expires_at,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?)",
                (
                    platform,
                    identity,
                    status,
                    expires_at,
                    ak.db.now_iso(),
                    ak.db.now_iso(),
                ),
            )
        con.commit()
    finally:
        con.close()


@pytest.fixture
def feishu_env(tmp_path, monkeypatch):
    """受管平台=feishu，fail-closed，库里有一个 active、一个 banned、一条已过期。"""
    dbfile = tmp_path / "a.db"
    _mkdb(
        dbfile,
        [
            ("feishu", "u_ok", "active", "2099-12-31T00:00:00Z"),
            ("feishu", "u_bad", "banned", None),
            ("feishu", "u_exp", "active", "2000-01-01T00:00:00Z"),
        ],
    )
    monkeypatch.setenv("ADMIT_KEEPER_DB", str(dbfile))
    monkeypatch.setenv("ADMIT_GATE_PLATFORMS", "feishu")
    monkeypatch.setenv("ADMIT_FAIL_OPEN", "0")
    return dbfile


# ---------- 1. 放行/协议短路 ----------


def test_no_source_allows():
    """无 source -> 放行（避免误拦）。"""
    assert _on_pre_gateway_dispatch(_event(no_source=True), gateway=None) is None


def test_non_managed_platform_allows(feishu_env):
    """平台不在受管列表 -> gate 放行（unmanaged_platform），适配层返回 None。"""
    assert _on_pre_gateway_dispatch(_event("wecom", "u_bad"), None) is None


def test_empty_identity_allows(feishu_env):
    """身份为空 -> gate 放行（no_identity），不应误拦。"""
    assert _on_pre_gateway_dispatch(_event("feishu", ""), None) is None


def test_enum_platform_resolves_value(feishu_env):
    """平台是枚举对象（.value）-> 解析出平台名并经适配层到达判定。"""
    out = _on_pre_gateway_dispatch(_event(platform=SimpleNamespace(value="FEISHU"), user_id="u_bad"), None)
    assert out == {"action": "skip", "reason": "deny:banned"}


# ---------- 2. 判定分支（经适配层端到端） ----------


def test_banned_skips(feishu_env):
    assert _on_pre_gateway_dispatch(_event("feishu", "u_bad"), None) == {
        "action": "skip",
        "reason": "deny:banned",
    }


def test_active_allows(feishu_env):
    assert _on_pre_gateway_dispatch(_event("feishu", "u_ok"), None) is None


def test_expired_denies(feishu_env):
    assert _on_pre_gateway_dispatch(_event("feishu", "u_exp"), None) == {
        "action": "skip",
        "reason": "deny:expired",
    }


def test_no_record_denies(feishu_env):
    assert _on_pre_gateway_dispatch(_event("feishu", "u_nobody"), None) == {
        "action": "skip",
        "reason": "deny:not_authorized",
    }


def test_no_record_in_allowlist_allows(feishu_env, monkeypatch):
    monkeypatch.setenv("ADMIT_ALLOWED_USERS", "ou_perm")
    assert _on_pre_gateway_dispatch(_event("feishu", "ou_perm"), None) is None


# ---------- 3. DB 不可得 -> fail-open 放行并告警 / fail-closed 拒收 ----------


def test_missing_db_fail_open_allows_and_warns(tmp_path, monkeypatch, caplog):
    """库不存在 + 默认 fail-open -> 放行，并必须告警（ADR-1，本修复的核心断言）。"""
    monkeypatch.setenv("ADMIT_KEEPER_DB", str(tmp_path / "nope.db"))
    monkeypatch.setenv("ADMIT_GATE_PLATFORMS", "feishu")
    monkeypatch.delenv("ADMIT_FAIL_OPEN", raising=False)
    with caplog.at_level(logging.WARNING, logger="admit-keeper"):
        assert _on_pre_gateway_dispatch(_event("feishu", "u_any"), None) is None
    assert any("fail-open" in rec.getMessage() for rec in caplog.records)


def test_missing_db_fail_closed_denies(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIT_KEEPER_DB", str(tmp_path / "nope.db"))
    monkeypatch.setenv("ADMIT_GATE_PLATFORMS", "feishu")
    monkeypatch.setenv("ADMIT_FAIL_OPEN", "0")
    assert _on_pre_gateway_dispatch(_event("feishu", "u_any"), None) == {
        "action": "skip",
        "reason": "deny:gate_unavailable",
    }


# ---------- 4. 异常兜底（gate 抛出时不外抛） ----------


def test_plugin_exception_fail_open_allows_and_warns(monkeypatch, caplog):
    """gate.gate 抛异常 + 默认 fail-open -> 放行并告警，绝不外抛。"""
    monkeypatch.setenv("ADMIT_GATE_PLATFORMS", "feishu")
    monkeypatch.delenv("ADMIT_FAIL_OPEN", raising=False)

    def boom(*args, **kwargs):
        raise RuntimeError("gate 炸了")

    monkeypatch.setattr(ak.gate, "gate", boom)
    with caplog.at_level(logging.WARNING, logger="admit-keeper"):
        assert _on_pre_gateway_dispatch(_event("feishu", "u_any"), None) is None
    assert any("fail-open" in rec.getMessage() for rec in caplog.records)


def test_plugin_exception_fail_closed_denies(monkeypatch):
    monkeypatch.setenv("ADMIT_GATE_PLATFORMS", "feishu")
    monkeypatch.setenv("ADMIT_FAIL_OPEN", "0")

    def boom(*args, **kwargs):
        raise RuntimeError("gate 炸了")

    monkeypatch.setattr(ak.gate, "gate", boom)
    assert _on_pre_gateway_dispatch(_event("feishu", "u_any"), None) == {
        "action": "skip",
        "reason": "deny:plugin_error",
    }


# ---------- 5. register ----------


def test_register_registers_pre_gateway_dispatch_hook():
    ctx = SimpleNamespace()
    ctx.register_hook = lambda name, cb: ctx.__dict__.update(last_name=name, last_cb=cb)
    ak.register(ctx)
    assert ctx.last_name == "pre_gateway_dispatch"
    assert ctx.last_cb is _on_pre_gateway_dispatch
