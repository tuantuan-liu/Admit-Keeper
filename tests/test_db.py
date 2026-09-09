"""db 数据层单测：建表、写入、唯一键去重、lookup / lookup_plugin 的 unavailable 分支。"""
import os

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
        assert rec == ("active", "2026-10-01T00:00:00Z")
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
        assert rec == ("banned", None)
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
    assert record == ("banned", None)
    assert unavailable is False
