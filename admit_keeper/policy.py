"""纯决策核心 —— 框架无关、无 IO、可单测。

准入层（热路径）与 MCP（管理端）共用这一份语义，避免两侧判词漂移。
所有字符串均用固定 UTC ISO-8601 格式，比较为字典序（格式一致时不失真）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import AbstractSet, Literal, Optional

STATUS_ACTIVE = "active"
STATUS_BANNED = "banned"

ALLOW: Literal["allow"] = "allow"
SKIP: Literal["skip"] = "skip"


@dataclass(frozen=True)
class Decision:
    action: str  # "allow" | "skip"
    reason: str

    def is_allow(self) -> bool:
        return self.action == ALLOW

    def is_skip(self) -> bool:
        return self.action == SKIP


def decide(
    identity: str,
    record: Optional[tuple[str, Optional[str]]],
    allowlist: AbstractSet[str],
    now: str,
    fail_open: bool = False,
    unavailable: bool = False,
) -> Decision:
    """裁定 (platform, identity) 是否放行。

    record:  (status, expires_at) 或 None（表存在但无该身份记录）。
    unavailable: True 表示判定所需数据不可得（DB 缺失 / 读取异常 / 表未建），
                 该方向由 fail_open 决定（默认放行，安全模式改拒绝）。
    allowlist: 永久白名单（env ADMIT_ALLOWED_USERS）。优先级：
               banned（最高）> 过期 > 无记录；白名单覆盖“过期”，但不覆盖 banned。
    """
    if unavailable:
        # 数据不可得：无法判定 → 由 fail_open 决定。放行时务必大声告警，避免静默失效。
        return Decision(ALLOW if fail_open else SKIP, "fail_open" if fail_open else "deny:gate_unavailable")
    if record is None:
        if identity in allowlist:
            return Decision(ALLOW, "allowlist")
        return Decision(SKIP, "deny:not_authorized")

    status, expires_at = record
    if status == STATUS_BANNED:
        return Decision(SKIP, "deny:banned")
    if status != STATUS_ACTIVE:
        # 未知 status（防御）：宁拒绝不误放。
        return Decision(SKIP, "deny:unknown_status")
    if expires_at is not None and expires_at <= now:
        if identity in allowlist:
            return Decision(ALLOW, "allowlist_overrides_expired")
        return Decision(SKIP, "deny:expired")
    return Decision(ALLOW, "active")


_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off", ""}


def parse_bool(value: str | None, default: bool = False) -> bool:
    """宽松解析 env 布尔串。未设/空串/非预期输入一律回落 default；
    仅明确 truthy/falsy 关键字才覆盖。"""
    if value is None:
        return default
    v = value.strip().lower()
    if v == "":
        return default
    if v in _TRUTHY:
        return True
    if v in _FALSY:
        return False
    return default
