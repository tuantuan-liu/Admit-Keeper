"""临时准入窗口（admit_window）单测：
- policy 分支（窗口放行全新身份；重开只重新纳入「窗口引入」的过期记录，不覆盖 banned / 付费过期）
- db 读写与「旧库缺表 / 缺列」降级（绝不 unavailable）
- gate 端到端（窗口内放行并落库一次、窗口外拒、窗口重开顺延）
"""

from datetime import datetime, timedelta, timezone

import pytest

from admit_keeper import db
from admit_keeper.gate import gate, is_allowed
from admit_keeper.policy import GRANTED_BY_WINDOW, decide

NOW = "2026-09-10T12:00:00Z"
WL = frozenset({"ou_perm"})


def _iso(offset_seconds: float) -> str:
    dt = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _mkdb(dbfile):
    db.open_init(str(dbfile)).close()


def _add_window(dbfile, platform, start, end, note=""):
    con = db.open_init(str(dbfile))
    try:
        now = db.now_iso()
        con.execute(
            "INSERT INTO admit_window(platform,start_at,end_at,note,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (platform, start, end, note, now, now),
        )
        con.commit()
    finally:
        con.close()


def _add_record(dbfile, platform, identity, status, expires_at=None, granted_by=None):
    con = db.open_init(str(dbfile))
    try:
        now = db.now_iso()
        con.execute(
            "INSERT INTO admit_allowed(platform,identity,status,expires_at,granted_by,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (platform, identity, status, expires_at, granted_by, now, now),
        )
        con.commit()
    finally:
        con.close()


# ---------------- policy：窗口放行 / 重入边界 ----------------


def test_no_record_with_window_allows():
    d = decide("ou_new", None, allowlist=WL, now=NOW, window_open=True)
    assert d.is_allow() and d.reason == "window_open"


def test_no_record_without_window_denies():
    d = decide("ou_new", None, allowlist=WL, now=NOW, window_open=False)
    assert d.is_skip() and d.reason == "deny:not_authorized"


def test_allowlist_beats_window_on_no_record():
    d = decide("ou_perm", None, allowlist=WL, now=NOW, window_open=True)
    assert d.is_allow() and d.reason == "allowlist"


def test_banned_ignores_window():
    d = decide("ou_x", ("banned", None, None), allowlist=WL, now=NOW, window_open=True)
    assert d.is_skip() and d.reason == "deny:banned"


def test_banned_ignores_window_even_if_window_granted():
    # 封禁恒拒：即便这条记录原本由窗口引入，窗口重开也不复活（ADR-2 优先级 / ADR-8 边界）。
    d = decide("ou_x", ("banned", None, "window"), allowlist=WL, now=NOW, window_open=True, window_reentry=True)
    assert d.is_skip() and d.reason == "deny:banned"


def test_expired_manual_grant_not_rescued_by_window():
    """付费 / 手工授权（granted_by 非 window）的过期记录：窗口重开也不放行 —— ADR-8 的精准边界。"""
    d = decide(
        "ou_x",
        ("active", "2000-01-01T00:00:00Z", "admin"),
        allowlist=WL,
        now=NOW,
        window_open=True,
        window_reentry=False,
    )
    assert d.is_skip() and d.reason == "deny:expired"


def test_expired_window_record_reenters_on_new_window():
    """窗口引入的记录（granted_by='window'）过期后，窗口重开可再进 —— 修「第二天哑火」。"""
    d = decide(
        "ou_x",
        ("active", "2000-01-01T00:00:00Z", "window"),
        allowlist=WL,
        now=NOW,
        window_open=True,
        window_reentry=True,
    )
    assert d.is_allow() and d.reason == "window_reentry"


def test_expired_window_record_without_open_window_still_denied():
    # 没有开放窗口时，窗口老面孔照样按过期拒。
    d = decide(
        "ou_x",
        ("active", "2000-01-01T00:00:00Z", "window"),
        allowlist=WL,
        now=NOW,
        window_open=False,
        window_reentry=False,
    )
    assert d.is_skip() and d.reason == "deny:expired"


def test_active_unaffected_by_window():
    d = decide("ou_x", ("active", "2100-01-01T00:00:00Z", None), allowlist=WL, now=NOW, window_open=True)
    assert d.is_allow() and d.reason == "active"


# ---------------- db：窗口查询 / 落库 / 降级 ----------------


@pytest.fixture
def wdb(tmp_path, monkeypatch):
    f = tmp_path / "w.db"
    _mkdb(f)
    monkeypatch.setenv("ADMIT_KEEPER_DB", str(f))
    return str(f)


def _lookup_window_direct(dbfile, platform):
    con = db.connect(dbfile, hot=True)
    try:
        return db.lookup_window(con, platform, db.now_iso())
    finally:
        con.close()


def test_lookup_window_hit(wdb):
    end = _iso(3600)
    _add_window(wdb, "feishu", _iso(-60), end)
    assert _lookup_window_direct(wdb, "feishu") == end


def test_lookup_window_future_miss(wdb):
    _add_window(wdb, "feishu", _iso(600), _iso(3600))
    assert _lookup_window_direct(wdb, "feishu") is None


def test_lookup_window_expired_miss(wdb):
    _add_window(wdb, "feishu", _iso(-7200), _iso(-3600))
    assert _lookup_window_direct(wdb, "feishu") is None


def test_lookup_window_other_platform_miss(wdb):
    _add_window(wdb, "wecom", _iso(-60), _iso(3600))
    assert _lookup_window_direct(wdb, "feishu") is None


def test_lookup_window_plugin_missing_db_is_none(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIT_KEEPER_DB", str(tmp_path / "nope.db"))
    assert db.lookup_window_plugin("feishu") is None


def test_lookup_window_plugin_missing_table_is_none(tmp_path, monkeypatch):
    """旧库（只有 admit_allowed，无 admit_window）-> 降级为 None，绝不抛错、绝不 unavailable。"""
    f = tmp_path / "old.db"
    con = db.connect(str(f))
    con.execute(
        "CREATE TABLE admit_allowed(id INTEGER PRIMARY KEY, platform TEXT, identity TEXT, status TEXT, expires_at TEXT)"
    )
    con.commit()
    con.close()
    monkeypatch.setenv("ADMIT_KEEPER_DB", str(f))
    assert db.lookup_window_plugin("feishu") is None


def test_grant_window_entry_creates_record(wdb):
    end = _iso(3600)
    db.grant_window_entry("feishu", "ou_new", end, db=wdb)
    record, unavailable = db.lookup_plugin("feishu", "ou_new", db=wdb)
    assert unavailable is False
    # 第三位是来源标记：窗口引入的记录必须能被 policy 认出（窗口重入的依据）。
    assert record == ("active", end, db.WINDOW_GRANTED_BY)


def test_window_marker_constants_agree():
    """policy 与本文件的 db 各自用字面量定义来源标记（policy 不能 import db，见其注释）。
    这里锁死二者一致，防止日后单改一处导致窗口重入静默失效。"""
    assert GRANTED_BY_WINDOW == db.WINDOW_GRANTED_BY


def test_grant_window_entry_never_revives_banned(wdb):
    _add_record(wdb, "feishu", "ou_bad", "banned")
    db.grant_window_entry("feishu", "ou_bad", _iso(3600), db=wdb)
    record, _ = db.lookup_plugin("feishu", "ou_bad", db=wdb)
    assert record[0] == "banned"  # 绝不复活


# ---------------- gate：端到端 ----------------


def _env(monkeypatch, dbfile, fail_open="0"):
    monkeypatch.setenv("ADMIT_KEEPER_DB", str(dbfile))
    monkeypatch.setenv("ADMIT_GATE_PLATFORMS", "feishu")
    monkeypatch.setenv("ADMIT_FAIL_OPEN", fail_open)


def test_gate_window_admits_new_and_persists(tmp_path, monkeypatch):
    f = tmp_path / "g.db"
    _mkdb(f)
    end = _iso(3600)
    _add_window(f, "feishu", _iso(-60), end)
    _env(monkeypatch, f)

    d = gate("feishu", "ou_new")
    assert d.is_allow() and d.reason == "window_open"
    # 落库留痕：到期=窗口结束，来源标记=window（窗口重开的依据）
    assert db.lookup_plugin("feishu", "ou_new", db=str(f))[0] == ("active", end, GRANTED_BY_WINDOW)


def test_gate_window_persists_once_then_normal_active(tmp_path, monkeypatch):
    f = tmp_path / "g.db"
    _mkdb(f)
    _add_window(f, "feishu", _iso(-60), _iso(3600))
    _env(monkeypatch, f)

    assert gate("feishu", "ou_new").reason == "window_open"  # 首次：窗口放行+落库
    assert gate("feishu", "ou_new").reason == "active"  # 二次：已有记录，走正常 active


def test_gate_window_closed_denies(tmp_path, monkeypatch):
    f = tmp_path / "g.db"
    _mkdb(f)
    _add_window(f, "feishu", _iso(-7200), _iso(-3600))  # 已结束
    _env(monkeypatch, f)
    d = gate("feishu", "ou_new")
    assert d.is_skip() and d.reason == "deny:not_authorized"


def test_gate_window_does_not_admit_expired(tmp_path, monkeypatch):
    # granted_by 为空（非窗口引入：如手工 grant 后到期）-> 窗口不放行。
    f = tmp_path / "g.db"
    _mkdb(f)
    _add_window(f, "feishu", _iso(-60), _iso(3600))
    _add_record(f, "feishu", "ou_old", "active", "2000-01-01T00:00:00Z")
    _env(monkeypatch, f)
    d = gate("feishu", "ou_old")
    assert d.is_skip() and d.reason == "deny:expired"


# ---------------- 窗口重入：窗口老面孔在**新窗口**里可再进（修「第二天哑火」） ----------------


def test_gate_window_reentry_next_day(tmp_path, monkeypatch):
    """核心回归：昨天从窗口进来的用户，今天窗口重开应能再进，且到期顺延到今天的窗口结束。
    修复前他会带一条 expires=昨天 的记录 -> 恒 deny:expired，「每晚开放体验」第二天必然哑火。"""
    f = tmp_path / "g.db"
    _mkdb(f)
    _add_record(f, "feishu", "ou_re", "active", "2000-01-01T00:00:00Z", GRANTED_BY_WINDOW)
    new_end = _iso(3600)
    _add_window(f, "feishu", _iso(-60), new_end)
    _env(monkeypatch, f)

    d = gate("feishu", "ou_re")
    assert d.is_allow() and d.reason == "window_reentry"
    # 到期顺延到**新**窗口结束，来源标记保持 window
    assert db.lookup_plugin("feishu", "ou_re", db=str(f))[0] == ("active", new_end, GRANTED_BY_WINDOW)
    # 每窗口只写一次：再次调用已有有效记录 -> 走正常 active
    assert gate("feishu", "ou_re").reason == "active"


def test_gate_manual_grant_expired_not_reentered_by_window(tmp_path, monkeypatch):
    """精准边界：付费 / 手工授权的过期用户，窗口重开**不**放行（只重新纳入窗口引入的人）。"""
    f = tmp_path / "g.db"
    _mkdb(f)
    _add_record(f, "feishu", "ou_paid", "active", "2000-01-01T00:00:00Z", "admin")
    _add_window(f, "feishu", _iso(-60), _iso(3600))
    _env(monkeypatch, f)

    d = gate("feishu", "ou_paid")
    assert d.is_skip() and d.reason == "deny:expired"


def test_gate_window_reentry_never_revives_banned(tmp_path, monkeypatch):
    """窗口老面孔被封禁后，窗口重开也绝不复活（banned 恒拒）。"""
    f = tmp_path / "g.db"
    _mkdb(f)
    _add_record(f, "feishu", "ou_re", "banned", "2000-01-01T00:00:00Z", GRANTED_BY_WINDOW)
    _add_window(f, "feishu", _iso(-60), _iso(3600))
    _env(monkeypatch, f)

    d = gate("feishu", "ou_re")
    assert d.is_skip() and d.reason == "deny:banned"


def test_gate_window_does_not_admit_banned(tmp_path, monkeypatch):
    f = tmp_path / "g.db"
    _mkdb(f)
    _add_window(f, "feishu", _iso(-60), _iso(3600))
    _add_record(f, "feishu", "ou_bad", "banned")
    _env(monkeypatch, f)
    d = gate("feishu", "ou_bad")
    assert d.is_skip() and d.reason == "deny:banned"


def test_gate_old_db_without_window_table_not_fail_open(tmp_path, monkeypatch):
    """回归（守住关键约束）：旧库无 admit_window 表 + fail-closed 时，
    未知身份应拒于 deny:not_authorized，而**不是**因 `no such table` 变成
    unavailable -> deny:gate_unavailable（若实现把窗口查询混进 lookup 的 try 就会踩到）。"""
    f = tmp_path / "old.db"
    con = db.connect(str(f))
    con.execute(
        "CREATE TABLE admit_allowed(id INTEGER PRIMARY KEY, platform TEXT, identity TEXT, status TEXT, expires_at TEXT)"
    )
    con.commit()
    con.close()
    _env(monkeypatch, f, fail_open="0")

    d = gate("feishu", "ou_new")
    assert d.is_skip() and d.reason == "deny:not_authorized"


def test_is_allowed_wrapper_with_window(tmp_path, monkeypatch):
    f = tmp_path / "g.db"
    _mkdb(f)
    _add_window(f, "feishu", _iso(-60), _iso(3600))
    _env(monkeypatch, f)
    assert is_allowed("feishu", "ou_new") is True


# ---------------- 通配窗口（platform="*"）：一次开窗，全平台生效 ----------------


def test_lookup_window_wildcard_matches_any_platform(wdb):
    end = _iso(3600)
    _add_window(wdb, "*", _iso(-60), end)
    for p in ("feishu", "wecom", "telegram", "任意平台"):
        assert _lookup_window_direct(wdb, p) == end


def test_lookup_window_picks_latest_end(wdb):
    sooner, later = _iso(1800), _iso(3600)
    _add_window(wdb, "*", _iso(-60), sooner)  # 通配：到 1800
    _add_window(wdb, "feishu", _iso(-60), later)  # 平台专属：到 3600
    assert _lookup_window_direct(wdb, "feishu") == later  # 取更晚者
    assert _lookup_window_direct(wdb, "wecom") == sooner  # 只有通配 -> 1800


def test_gate_wildcard_window_admits_across_platforms(tmp_path, monkeypatch):
    f = tmp_path / "g.db"
    _mkdb(f)
    _add_window(f, "*", _iso(-60), _iso(3600))
    monkeypatch.setenv("ADMIT_KEEPER_DB", str(f))
    monkeypatch.setenv("ADMIT_GATE_PLATFORMS", "feishu,wecom")
    monkeypatch.setenv("ADMIT_FAIL_OPEN", "0")
    assert gate("feishu", "ou_a").reason == "window_open"
    assert gate("wecom", "ou_b").reason == "window_open"
    # 落库仍按各自平台隔离
    assert db.lookup_plugin("feishu", "ou_a", db=str(f))[0][0] == "active"
    assert db.lookup_plugin("wecom", "ou_b", db=str(f))[0][0] == "active"


def test_gate_wildcard_window_does_not_gate_unmanaged_platform(tmp_path, monkeypatch):
    # 通配窗口不改变「非受管平台直接放行」：不在 ADMIT_GATE_PLATFORMS 内的平台仍放行，
    # 且不对其落库。
    f = tmp_path / "g.db"
    _mkdb(f)
    _add_window(f, "*", _iso(-60), _iso(3600))
    monkeypatch.setenv("ADMIT_KEEPER_DB", str(f))
    monkeypatch.setenv("ADMIT_GATE_PLATFORMS", "feishu")
    monkeypatch.setenv("ADMIT_FAIL_OPEN", "0")
    assert gate("telegram", "ou_t").reason == "unmanaged_platform"
    assert db.lookup_plugin("telegram", "ou_t", db=str(f))[0] is None
