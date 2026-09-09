"""框架无关判定助手 gate.py 单测：gate()/is_allowed() 的完整裁定分支，
并覆盖重构成共享模块后的 Hermes 接入层 __init__._on_pre_gateway_dispatch。"""
from types import SimpleNamespace

import pytest

from admit_keeper import gate
from admit_keeper import _on_pre_gateway_dispatch  # Hermes 接入层（重构后委托 gate.gate）
from admit_keeper import db


def _mkdb(dbfile, rows):
    """建库并插入 rows=[(platform,identity,status,expires_at), ...]，commit 后关闭。"""
    con = db.open_init(str(dbfile))
    try:
        for platform, identity, status, expires_at in rows:
            con.execute(
                "INSERT INTO admit_allowed(platform,identity,status,expires_at,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (platform, identity, status, expires_at, db.now_iso(), db.now_iso()),
            )
        con.commit()
    finally:
        con.close()


@pytest.fixture
def feishu_env(monkeypatch, tmp_path):
    """受管平台=feishu，fail-closed，库里有一个 active、一个 banned。"""
    dbfile = tmp_path / "gate.db"
    _mkdb(dbfile, [
        ("feishu", "u_ok", "active", "2027-01-01T00:00:00Z"),
        ("feishu", "u_bad", "banned", None),
    ])
    monkeypatch.setenv("ADMIT_KEEPER_DB", str(dbfile))
    monkeypatch.setenv("ADMIT_GATE_PLATFORMS", "feishu")
    monkeypatch.setenv("ADMIT_FAIL_OPEN", "0")
    return str(dbfile)


def test_active_is_allowed(feishu_env):
    assert gate.is_allowed("feishu", "u_ok") is True


def test_banned_is_denied(feishu_env):
    assert gate.is_allowed("feishu", "u_bad") is False


def test_unknown_identity_denied_when_fail_closed(feishu_env):
    d = gate.gate("feishu", "u_nobody")
    assert d.is_skip() and d.reason == "deny:not_authorized"


def test_unmanaged_platform_allowed(feishu_env):
    # 平台维度：feishu 受管，wecom 未受管 → 直接放行（全平台扩展点）。
    assert gate.is_allowed("wecom", "u_bad") is True


def test_empty_platform_or_identity_allowed(feishu_env):
    assert gate.is_allowed("", "u_bad") is True
    assert gate.is_allowed("feishu", "") is True


def test_platform_is_lowercased(feishu_env):
    assert gate.is_allowed("FEISHU", "u_bad") is False  # 大写也要走受管判定


def test_fail_open_allows_when_db_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIT_KEEPER_DB", str(tmp_path / "nope.db"))
    monkeypatch.setenv("ADMIT_GATE_PLATFORMS", "feishu")
    monkeypatch.setenv("ADMIT_FAIL_OPEN", "1")
    assert gate.is_allowed("feishu", "u_any") is True


def test_fail_closed_denies_when_db_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIT_KEEPER_DB", str(tmp_path / "nope.db"))
    monkeypatch.setenv("ADMIT_GATE_PLATFORMS", "feishu")
    monkeypatch.setenv("ADMIT_FAIL_OPEN", "0")
    assert gate.is_allowed("feishu", "u_any") is False


def test_allowlist_overrides_expired(monkeypatch, tmp_path):
    dbfile = tmp_path / "exp.db"
    _mkdb(dbfile, [("feishu", "u_exp", "active", "2000-01-01T00:00:00Z")])
    monkeypatch.setenv("ADMIT_KEEPER_DB", str(dbfile))
    monkeypatch.setenv("ADMIT_GATE_PLATFORMS", "feishu")
    monkeypatch.setenv("ADMIT_FAIL_OPEN", "0")
    # 显式传 allowlist → 覆盖过期；不传则拒绝。
    assert gate.gate("feishu", "u_exp", allowlist={"u_exp"}).is_allow()
    assert gate.gate("feishu", "u_exp").is_skip()


def test_explicit_db_arg(feishu_env):
    # database 显式传入（不依赖环境变量）也应工作。
    assert gate.is_allowed("feishu", "u_ok", database=feishu_env) is True


# ---- 重构后的 Hermes 接入层：委托共享 gate，仅做协议映射 ----


def _event(platform, user_id):
    return SimpleNamespace(source=SimpleNamespace(platform=platform, user_id=user_id))


def test_hermes_adapter_skips_banned(feishu_env):
    assert _on_pre_gateway_dispatch(_event("feishu", "u_bad"), None) == {
        "action": "skip",
        "reason": "deny:banned",
    }


def test_hermes_adapter_allows_active(feishu_env):
    assert _on_pre_gateway_dispatch(_event("feishu", "u_ok"), None) is None


def test_hermes_adapter_allows_empty_source(feishu_env):
    # 无 source → 放行（不误拦）。
    assert _on_pre_gateway_dispatch(SimpleNamespace(source=None), None) is None
