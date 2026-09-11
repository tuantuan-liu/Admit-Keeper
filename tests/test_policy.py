"""决策矩阵单测。覆盖 banned/active/过期/无记录 × 白名单 × fail_open × unavailable。"""

import pytest

from policy import decide


NOW = "2026-09-09T00:00:00Z"
EXPIRED = "2026-09-01T00:00:00Z"
FUTURE = "2026-10-01T00:00:00Z"
WL = frozenset({"ou_perm"})  # 永久白名单


def rec(status, expires_at=None, granted_by=None):
    """构造记录三元组 (status, expires_at, granted_by) —— 与 db.lookup 返回形状一致。"""
    return (status, expires_at, granted_by)


def test_unknown_identity_deny_by_default():
    d = decide("ou_x", None, allowlist=WL, now=NOW)
    assert d.is_skip() and d.reason == "deny:not_authorized"


def test_unknown_identity_in_allowlist_allow():
    d = decide("ou_perm", None, allowlist=WL, now=NOW)
    assert d.is_allow() and d.reason == "allowlist"


def test_active_future_allow():
    d = decide("ou_a", rec("active", FUTURE), allowlist=WL, now=NOW)
    assert d.is_allow() and d.reason == "active"


def test_active_permanent_allow():
    d = decide("ou_a", rec("active", None), allowlist=WL, now=NOW)
    assert d.is_allow()
    assert d.reason == "active"


def test_banned_always_deny_even_in_allowlist():
    # banned 优先级最高：即便在永久白名单也拒绝。
    d = decide("ou_perm", rec("banned"), allowlist=WL, now=NOW)
    assert d.is_skip() and d.reason == "deny:banned"


def test_expired_deny():
    d = decide("ou_x", rec("active", EXPIRED), allowlist=WL, now=NOW)
    assert d.is_skip() and d.reason == "deny:expired"


def test_expired_in_allowlist_allow():
    # 永久白名单覆盖“过期”但不覆盖 banned。
    d = decide("ou_perm", rec("active", EXPIRED), allowlist=WL, now=NOW)
    assert d.is_allow() and d.reason == "allowlist_overrides_expired"


def test_unavailable_fail_open_allow():
    d = decide("ou_x", None, allowlist=WL, now=NOW, fail_open=True, unavailable=True)
    assert d.is_allow() and d.reason == "fail_open"


def test_unavailable_fail_closed_deny():
    d = decide("ou_x", None, allowlist=WL, now=NOW, fail_open=False, unavailable=True)
    assert d.is_skip() and d.reason == "deny:gate_unavailable"


def test_unavailable_allowlist_allows():
    """ADR-16：数据不可得时白名单先于 fail_open 生效 —— 即便 fail-closed 也必须放行。

    白名单是纯 env 配置、不依赖 DB，DB 故障不该让它失效。"""
    d = decide("ou_perm", None, allowlist=WL, now=NOW, fail_open=False, unavailable=True)
    assert d.is_allow() and d.reason == "allowlist"


def test_unavailable_allowlist_does_not_leak_into_normal_path():
    """护栏：白名单的优先只**存在于 unavailable 分支内**。

    若把它提到函数最前，banned 铁律与 allowlist_overrides_expired 语义会一起被破 ——
    这条一次性锁死三个正常路径的判词，防后人再改回去。
    """
    assert decide("ou_perm", rec("banned"), allowlist=WL, now=NOW).reason == "deny:banned"
    assert decide("ou_perm", rec("active", EXPIRED), allowlist=WL, now=NOW).reason == "allowlist_overrides_expired"
    assert decide("ou_x", None, allowlist=WL, now=NOW).reason == "deny:not_authorized"


def test_unavailable_window_still_not_applied():
    """不对称（ADR-16）：数据不可得时白名单生效、**窗口不生效**。

    窗口依赖 DB，此刻无从判断是否开放；只有不依赖 DB 的白名单能继续生效。"""
    d = decide("ou_x", None, allowlist=WL, now=NOW, fail_open=False, unavailable=True, window_open=True)
    assert d.is_skip() and d.reason == "deny:gate_unavailable"


def test_empty_allowlist_defaults():
    assert decide("ou_x", None, allowlist=frozenset(), now=NOW).is_skip()


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1", True),
        ("true", True),
        ("TRUE", True),
        ("yes", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("no", False),
        ("off", False),
    ],
)
def test_parse_bool(value, expected):
    from policy import parse_bool

    assert parse_bool(value, default=False) is expected


def test_parse_bool_empty_and_none_fall_back_to_default():
    from policy import parse_bool

    # 未设 / 空串 / 非预期输入 -> 一律回落 default
    assert parse_bool(None, default=True) is True
    assert parse_bool("", default=True) is True
    assert parse_bool("   ", default=True) is True
    assert parse_bool("maybe", default=True) is True
    assert parse_bool(None, default=False) is False


@pytest.mark.parametrize("exp", [EXPIRED, None])
def test_allowed_statuses_that_should_not_occur_still_deny(exp):
    # 防御：未知 status 一律拒绝（fail-closed on unknown status）。
    d = decide("ou_x", rec("weird", exp), allowlist=WL, now=NOW)
    assert d.is_skip()
