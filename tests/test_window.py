"""临时准入窗口（admit_window）单测：
- policy 分支（窗口只放行全新身份，不覆盖 banned / 过期）
- db 读写与「旧库缺表」降级（绝不 unavailable）
- gate 端到端（窗口内放行并落库一次、窗口外拒）
"""
from datetime import datetime, timedelta, timezone

import pytest

from admit_keeper import db
from admit_keeper.gate import gate, is_allowed
from admit_keeper.policy import decide

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


def _add_record(dbfile, platform, identity, status, expires_at=None):
    con = db.open_init(str(dbfile))
    try:
        now = db.now_iso()
        con.execute(
            "INSERT INTO admit_allowed(platform,identity,status,expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (platform, identity, status, expires_at, now, now),
        )
        con.commit()
    finally:
        con.close()


# ---------------- policy：窗口只放行全新身份 ----------------

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
    d = decide("ou_x", ("banned", None), allowlist=WL, now=NOW, window_open=True)
    assert d.is_skip() and d.reason == "deny:banned"


def test_expired_not_rescued_by_window():
    # 仅全新用户：已过期者即便窗口开也拒。
    d = decide("ou_x", ("active", "2000-01-01T00:00:00Z"), allowlist=WL, now=NOW, window_open=True)
    assert d.is_skip() and d.reason == "deny:expired"


def test_active_unaffected_by_window():
    d = decide("ou_x", ("active", "2100-01-01T00:00:00Z"), allowlist=WL, now=NOW, window_open=True)
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
    """旧库（只有 admit_allowed，无 admit_window）→ 降级为 None，绝不抛错、绝不 unavailable。"""
    f = tmp_path / "old.db"
    con = db.connect(str(f))
    con.execute("CREATE TABLE admit_allowed(id INTEGER PRIMARY KEY, platform TEXT, identity TEXT, status TEXT, expires_at TEXT)")
    con.commit()
    con.close()
    monkeypatch.setenv("ADMIT_KEEPER_DB", str(f))
    assert db.lookup_window_plugin("feishu") is None


def test_grant_window_entry_creates_record(wdb):
    end = _iso(3600)
    db.grant_window_entry("feishu", "ou_new", end, db=wdb)
    record, unavailable = db.lookup_plugin("feishu", "ou_new", db=wdb)
    assert unavailable is False
    assert record == ("active", end)


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
    # 落库留痕：到期=窗口结束
    assert db.lookup_plugin("feishu", "ou_new", db=str(f))[0] == ("active", end)


def test_gate_window_persists_once_then_normal_active(tmp_path, monkeypatch):
    f = tmp_path / "g.db"
    _mkdb(f)
    _add_window(f, "feishu", _iso(-60), _iso(3600))
    _env(monkeypatch, f)

    assert gate("feishu", "ou_new").reason == "window_open"   # 首次：窗口放行+落库
    assert gate("feishu", "ou_new").reason == "active"        # 二次：已有记录，走正常 active


def test_gate_window_closed_denies(tmp_path, monkeypatch):
    f = tmp_path / "g.db"
    _mkdb(f)
    _add_window(f, "feishu", _iso(-7200), _iso(-3600))  # 已结束
    _env(monkeypatch, f)
    d = gate("feishu", "ou_new")
    assert d.is_skip() and d.reason == "deny:not_authorized"


def test_gate_window_does_not_admit_expired(tmp_path, monkeypatch):
    f = tmp_path / "g.db"
    _mkdb(f)
    _add_window(f, "feishu", _iso(-60), _iso(3600))
    _add_record(f, "feishu", "ou_old", "active", "2000-01-01T00:00:00Z")
    _env(monkeypatch, f)
    d = gate("feishu", "ou_old")
    assert d.is_skip() and d.reason == "deny:expired"


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
    unavailable → deny:gate_unavailable（若实现把窗口查询混进 lookup 的 try 就会踩到）。"""
    f = tmp_path / "old.db"
    con = db.connect(str(f))
    con.execute("CREATE TABLE admit_allowed(id INTEGER PRIMARY KEY, platform TEXT, identity TEXT, status TEXT, expires_at TEXT)")
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
    _add_window(wdb, "*", _iso(-60), sooner)          # 通配：到 1800
    _add_window(wdb, "feishu", _iso(-60), later)      # 平台专属：到 3600
    assert _lookup_window_direct(wdb, "feishu") == later   # 取更晚者
    assert _lookup_window_direct(wdb, "wecom") == sooner   # 只有通配 → 1800


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
