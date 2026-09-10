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

from . import db
from . import policy
from .db import lookup_plugin, now_iso
from .policy import Decision


def _warn(msg: str) -> None:
    try:
        import logging

        logging.getLogger("admit-keeper").warning(msg)
    except Exception:
        pass


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
    - 若存在开放的临时准入窗口（``admit_window``），放行**全新**（无记录）身份，
      并落一条 ``expires_at = 窗口结束`` 的记录；banned 不受窗口影响（见 ADR-8）。
      对**由上一次窗口引入**（``granted_by == 'window'``）且已过期的记录，窗口重开时可再进
      （``window_reentry``），到期顺延到新窗口结束 —— 使「每晚定点开放体验」可复用；
      付费 / 手工授权的过期记录**不**受此影响。

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

    now = now_iso()
    record, unavailable = lookup_plugin(platform, identity, db=database)

    # 窗口只可能在两种情形影响判定，且都属「非热路径」，故仅此时多查一次窗口（保持常见路径单查询）：
    #   ① 全新身份（无记录）→ window_open；② 由**上一次窗口**引入、现已过期的记录 → window_reentry。
    # 其余（banned / 有效 active / 付费等手工授权的过期记录）不查。
    window_end: Optional[str] = None
    if not unavailable:
        is_new = record is None
        is_expired_window_rec = (
            record is not None
            and record[0] == policy.STATUS_ACTIVE
            and record[1] is not None
            and record[1] <= now
            and record[2] == policy.GRANTED_BY_WINDOW
        )
        if is_new or is_expired_window_rec:
            window_end = db.lookup_window_plugin(platform, now=now, db=database)

    window_open = window_end is not None
    window_reentry = window_open and record is not None and record[2] == policy.GRANTED_BY_WINDOW

    d = policy.decide(
        identity=identity, record=record, allowlist=allow,
        now=now, fail_open=fo, unavailable=unavailable,
        window_open=window_open, window_reentry=window_reentry,
    )

    if window_end is not None and d.reason in ("window_open", "window_reentry"):
        # 落库留痕：到期=窗口结束，供审计与后续自动过期（window_reentry 即把到期顺延到新窗口）。
        # 落库失败不阻断放行。
        try:
            db.grant_window_entry(platform, identity, window_end, db=database)
        except Exception as exc:  # noqa: BLE001
            _warn(f"admit-keeper 窗口进入落库失败（不影响放行）: {exc!r}")

    return d


def is_allowed(platform: Optional[str], identity: Optional[str], **kw) -> bool:
    """便捷版：直接返回布尔（True=放行，False=丢弃）。"""
    return gate(platform, identity, **kw).is_allow()
