"""框架无关的准入判定助手 —— 任意框架复用同一套逻辑。

Hermes 接入层（``__init__.py`` 的 ``_on_pre_gateway_dispatch``）与任何其它框架都应调用这里，
而不是各自把「受管平台 / 白名单 / fail-open / ``decide``」再拼一遍。本模块只依赖 stdlib 与
同包 ``policy`` / ``db``，不含任何框架概念。

典型用法（任何框架的"消息进 agent 前"）：

    from admit_keeper.gate import gate, is_allowed

    if not is_allowed(event.platform, event.user_id):
        return drop()          # 框架各自的丢弃动作
    return dispatch(event)

    # 或拿完整 Decision 看 reason：
    d = gate(event.platform, event.user_id)
    if not d.is_allow():
        log(d.reason)
"""
from __future__ import annotations

import os
from typing import Optional, Set

from . import policy
from .db import lookup_plugin, now_iso
from .policy import Decision


def managed_platforms() -> Set[str]:
    """哪些平台走准入。默认 feishu；列表之外的平台直接放行（全平台扩展点）。"""
    raw = os.environ.get("ADMIT_GATE_PLATFORMS", "feishu")
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def env_allowlist() -> Set[str]:
    """永久白名单，跨平台按 identity 匹配；只覆盖过期，不覆盖 banned。"""
    raw = os.environ.get("ADMIT_ALLOWED_USERS", "")
    return {x.strip() for x in raw.split(",") if x.strip()}


def fail_open() -> bool:
    """数据不可得时的默认方向：true=放行(与无准入一致)，false=拒绝(更安全)。"""
    return policy.parse_bool(os.environ.get("ADMIT_FAIL_OPEN"), default=True)


def gate(platform: Optional[str], identity: Optional[str], *,
         allowlist: Optional[Set[str]] = None,
         fail_open_flag: Optional[bool] = None,
         database: Optional[str] = None) -> Decision:
    """判定 ``(platform, identity)`` 是否有资格进入，返回一个 ``Decision``。

    - platform 为空 / 非受管 → 放行（``Decision(ALLOW, 'unmanaged_platform')``）
    - identity 为空 → 放行（无法判定谁进来，不误拦）
    - 否则 ``lookup_plugin`` + ``policy.decide``（banned > 过期 > 无记录 > 白名单）

    ``allowlist`` / ``fail_open_flag`` / ``database`` 均可不传：未传则分别从环境变量
    （``ADMIT_ALLOWED_USERS`` / ``ADMIT_FAIL_OPEN``）与默认库路径取。
    返回 ``d.is_allow()``：True=放行，False=丢弃。
    """
    platform = (platform or "").lower()
    if not platform or platform not in managed_platforms():
        return Decision(policy.ALLOW, "unmanaged_platform")

    identity = (identity or "").strip()
    if not identity:
        return Decision(policy.ALLOW, "no_identity")

    allow = set(allowlist) if allowlist is not None else env_allowlist()
    fo = fail_open() if fail_open_flag is None else fail_open_flag
    record, unavailable = lookup_plugin(platform, identity, db=database)
    return policy.decide(
        identity=identity, record=record, allowlist=allow,
        now=now_iso(), fail_open=fo, unavailable=unavailable,
    )


def is_allowed(platform: Optional[str], identity: Optional[str], **kw) -> bool:
    """便捷版：直接返回布尔（True=放行，False=丢弃）。"""
    return gate(platform, identity, **kw).is_allow()
