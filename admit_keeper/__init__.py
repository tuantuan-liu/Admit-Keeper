"""admit-keeper —— Hermes 网关准入插件（准入裁定层）。

职责：每条消息进 gateway 的 pre_gateway_dispatch 钩子，判定「谁有资格进入」。
只读共享库 admit_keeper.db；与 MCP 管理端共用一个库、一份判定语义。

运行于 Hermes 网关进程内，平台无关。管理端（MCP）见 mcp/admit_keeper_mcp.py。

说明：
- Hermes 以包 `hermes_plugins.<slug>` 加载本插件（slug 把目录名 `admit-keeper` 转
  为 `admit_keeper`，`submodule_search_locations=[插件目录]`），故用**包内相对导入**
  引入同目录的 db.py / policy.py，既规范又避免 `db`/`policy` 成为全局顶层名的碰撞。
- 钩子签名 / 返回协议已对照真实 Hermes 源码核对（gateway/run.py 的
  `pre_gateway_dispatch`：回调以 `event/gateway/session_store` 关键字调用；
  返回 `None`/`{"action":"allow"}` 放行，`{"action":"skip","reason":...}` 丢弃）。
"""
from __future__ import annotations

import os
import sys

from . import db
from . import policy
from .db import lookup_plugin, now_iso
from .policy import decide, parse_bool


def _managed_platforms() -> set[str]:
    """哪些平台走准入。默认 feishu；列表之外的平台直接放行（全平台扩展点）。"""
    raw = os.environ.get("ADMIT_GATE_PLATFORMS", "feishu")
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def _env_allowlist() -> set[str]:
    raw = os.environ.get("ADMIT_ALLOWED_USERS", "")
    return {x.strip() for x in raw.split(",") if x.strip()}


def _fail_open() -> bool:
    """数据不可得时的默认方向：true=放行(与无插件时一致)，false=拒绝(更安全)。"""
    return parse_bool(os.environ.get("ADMIT_FAIL_OPEN"), default=True)


def _on_pre_gateway_dispatch(event, gateway, session_store=None, **kw):
    """钩子主体。失败一律不抛异常给网关（只影响准入，不扰流程）。"""
    try:
        source = getattr(event, "source", None)
        if source is None:
            return None  # 无来源信息 → 放行（避免误拦）

        platform = getattr(source, "platform", None)
        if platform is not None:
            # 枚举型平台会带 .value，取之；否则退回 str。
            platform = getattr(platform, "value", None) or str(platform)
        platform = (platform or "").lower()
        if not platform or platform not in _managed_platforms():
            return None  # 非受管平台放行

        identity = getattr(source, "user_id", None)
        if not identity:
            return None

        record, unavailable = lookup_plugin(platform, identity)
        d = decide(
            identity=identity,
            record=record,
            allowlist=_env_allowlist(),
            now=now_iso(),
            fail_open=_fail_open(),
            unavailable=unavailable,
        )
        if d.is_skip():
            return {"action": "skip", "reason": d.reason}
        return None

    except Exception as exc:  # noqa: BLE001 —— 兜底，绝不让插件拖垮网关
        if _fail_open():
            # 放行但必须告警；否则闸门静默失效。
            _warn(f"admit-keeper 插件异常，fail-open 放行: {exc!r}")
            return None
        _warn(f"admit-keeper 插件异常，fail-closed 拒绝: {exc!r}")
        return {"action": "skip", "reason": "deny:plugin_error"}


def _warn(msg: str) -> None:
    try:
        import logging

        logging.getLogger("admit-keeper").warning(msg)
    except Exception:
        try:
            print("[admit-keeper]", msg, file=sys.stderr)
        except Exception:
            pass


def register(ctx):
    """Hermes 插件注册入口。ctx.register_hook(钩子名, 处理器)。"""
    ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)
