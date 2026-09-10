"""db 数据层单测：建表、写入、唯一键去重、lookup / lookup_plugin 的 unavailable 分支。"""

import db


def test_open_init_creates_schema(tmp_path):
    dbfile = tmp_path / "a.db"
    con = db.open_init(str(dbfile))
    try:
        assert con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='admit_allowed'").fetchone()
    finally:
        con.close()
    assert dbfile.exists()


def test_lookup_roundtrip_and_upsert(tmp_path):
    con = db.open_init(str(tmp_path / "a.db"))
    try:
        con.execute(
            "INSERT INTO admit_allowed(platform,identity,status,expires_at,created_at,updated_at) "
            "VALUES('feishu','ou_1','active','2026-10-01T00:00:00Z',?,?)",
            (db.now_iso(), db.now_iso()),
        )
        rec = db.lookup(con, "feishu", "ou_1")
        assert rec == ("active", "2026-10-01T00:00:00Z", None)  # (status, expires_at, granted_by)
        assert db.lookup(con, "feishu", "ou_2") is None
        # 平台隔离：同 identity 不同平台互不影响
        assert db.lookup(con, "wecom", "ou_1") is None
    finally:
        con.close()


def test_unique_platform_identity(tmp_path):
    con = db.open_init(str(tmp_path / "a.db"))
    try:
        con.execute(
            "INSERT INTO admit_allowed(platform,identity,status,created_at,updated_at) VALUES('feishu','ou_1','active',?,?)",
            (db.now_iso(), db.now_iso()),
        )
        con.execute(
            "INSERT INTO admit_allowed(platform,identity,status,created_at,updated_at) "
            "VALUES('feishu','ou_1','active',?,?) ON CONFLICT(platform,identity) DO UPDATE SET status='banned'",
            (db.now_iso(), db.now_iso()),
        )
        con.commit()
        rec = db.lookup(con, "feishu", "ou_1")
        assert rec == ("banned", None, None)
    finally:
        con.close()


def test_lookup_plugin_missing_db_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIT_KEEPER_DB", str(tmp_path / "missing.db"))
    record, unavailable = db.lookup_plugin("feishu", "ou_1")
    assert record is None
    assert unavailable is True


def test_lookup_plugin_no_record_but_table_exists(tmp_path, monkeypatch):
    dbfile = tmp_path / "a.db"
    con = db.open_init(str(dbfile))
    con.close()
    monkeypatch.setenv("ADMIT_KEEPER_DB", str(dbfile))
    record, unavailable = db.lookup_plugin("feishu", "ou_1")
    assert record is None
    assert unavailable is False


def test_lookup_plugin_matches(tmp_path, monkeypatch):
    dbfile = tmp_path / "a.db"
    con = db.open_init(str(dbfile))
    try:
        con.execute(
            "INSERT INTO admit_allowed(platform,identity,status,created_at,updated_at) VALUES('feishu','ou_1','banned',?,?)",
            (db.now_iso(), db.now_iso()),
        )
        con.commit()
    finally:
        con.close()
    monkeypatch.setenv("ADMIT_KEEPER_DB", str(dbfile))
    record, unavailable = db.lookup_plugin("feishu", "ou_1")
    assert record == ("banned", None, None)
    assert unavailable is False


# ---------------- 旧库兼容：表缺 granted_by 列 ----------------

def test_lookup_on_legacy_table_without_granted_by(tmp_path, monkeypatch):
    """旧库 admit_allowed 缺 granted_by 列 → 读路径降级为 (status, expires_at, None)。

    若直接 SELECT granted_by 会抛 `no such column`，被 lookup_plugin 的
    `except sqlite3.Error` 吃成 unavailable → 整条准入滑进 fail-open / fail-closed
    （ADR-1 最坏情形）。故读路径必须降级而不是失败。
    """
    f = tmp_path / "legacy.db"
    con = db.connect(str(f))
    con.execute(
        "CREATE TABLE admit_allowed(id INTEGER PRIMARY KEY, platform TEXT, identity TEXT, "
        "status TEXT, expires_at TEXT)"
    )
    con.execute("INSERT INTO admit_allowed(platform,identity,status,expires_at) "
                "VALUES('feishu','ou_1','active',NULL)")
    con.commit()
    con.close()

    monkeypatch.setenv("ADMIT_KEEPER_DB", str(f))
    record, unavailable = db.lookup_plugin("feishu", "ou_1")
    assert record == ("active", None, None)
    assert unavailable is False


def test_ensure_schema_migrates_legacy_table(tmp_path):
    """ensure_schema（MCP 写路径）应给旧表补上 granted_by，使热路径回到正常单查询、
    不再每条消息都走异常降级路径。"""
    f = tmp_path / "legacy.db"
    con = db.connect(str(f))
    con.execute(
        "CREATE TABLE admit_allowed(id INTEGER PRIMARY KEY, platform TEXT, identity TEXT, "
        "status TEXT, expires_at TEXT)"
    )
    con.execute("INSERT INTO admit_allowed(platform,identity,status) VALUES('feishu','ou_1','active')")
    con.commit()
    con.close()

    db.open_init(str(f)).close()  # 触发迁移

    con = db.connect(str(f))
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(admit_allowed)")}
        assert "granted_by" in cols
        rec = db.lookup(con, "feishu", "ou_1")
    finally:
        con.close()
    assert rec == ("active", None, None)  # 旧行补成 NULL = 非窗口引入（安全默认）
