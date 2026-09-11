"""纯决策核心 —— 框架无关、无 IO、可单测。

准入层（热路径）与 MCP（管理端）共用这一份语义，避免两侧判词漂移。
所有字符串均用固定 UTC ISO-8601 格式，比较为字典序（格式一致时不失真）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import AbstractSet, Literal, Optional

STATUS_ACTIVE = "active"
STATUS_BANNED = "banned"

# 「该记录由临时准入窗口引入」的来源标记。与 db.WINDOW_GRANTED_BY 必须一致 ——
# 故意用字面量而非 import：policy.py 会被 MCP 以**顶层模块**方式导入（`from policy import …`），
# 一旦这里出现包内相对导入，MCP 侧就会 ImportError。二者一致性由单测锁死（见 test_window）。
GRANTED_BY_WINDOW = "window"

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
    record: Optional[tuple[str, Optional[str], Optional[str]]],
    allowlist: AbstractSet[str],
    now: str,
    fail_open: bool = False,
    unavailable: bool = False,
    window_open: bool = False,
    window_reentry: bool = False,
) -> Decision:
    """裁定 (platform, identity) 是否放行。

    record:  (status, expires_at, granted_by) 或 None（表存在但无该身份记录）。
    unavailable: True 表示判定所需数据不可得（DB 缺失 / 读取异常 / 表未建），
                 该方向由 fail_open 决定（默认放行，安全模式改拒绝）。
    allowlist: 永久白名单（env ADMIT_ALLOWED_USERS）。优先级：
               banned（最高）> 过期 > 无记录；白名单覆盖“过期”，但不覆盖 banned。
               **数据不可得时白名单先于 fail_open 生效**（见 ADR-16）：它是纯 env 配置、
               不依赖 DB，故 DB 故障时依然放行；代价是此刻无法校验封禁态。
    window_open: 当前有临时准入窗口开放（见 ADR-8）。**仅** 对“无记录”的全新身份放行
                 （reason ``window_open``），不覆盖 banned、也不放行“已过期”者。
    window_reentry: 该记录由**上一次窗口**引入（granted_by == 'window'），且当前又有窗口开放。
                 此时允许“窗口老面孔”再进（reason ``window_reentry``），由调用方把到期顺延到
                 新窗口结束 —— 否则“每晚定点开放体验”第二天必然哑火。**仅**对窗口引入的记录生效：
                 付费 / 手工授权的过期记录不受影响。同样不覆盖 banned。

    注意：``window_open`` / ``window_reentry`` 都只在“过期”及“无记录”这两个分支生效，
    ``banned`` 分支在其之前，天然恒拒。
    """
    if unavailable:
        # 数据不可得：无法判定 -> 由 fail_open 决定。放行时务必大声告警，避免静默失效。
        # 白名单例外（ADR-16）：它是纯 env 配置、不依赖 DB，故此刻依然生效，且先于 fail_open。
        # 代价：此刻无法校验封禁态，白名单内的**已封禁者**也会被放行 —— 与 fail_open 同源的
        # 取舍（要绝对安全请设 ADMIT_FAIL_OPEN=0 并同时收紧白名单）。告警由 gate 侧发出。
        # 注意此处**只**认白名单，不认窗口：窗口依赖 DB，数据不可得时无从判断是否开放。
        if identity in allowlist:
            return Decision(ALLOW, "allowlist")
        return Decision(ALLOW if fail_open else SKIP, "fail_open" if fail_open else "deny:gate_unavailable")

    if record is None:
        if identity in allowlist:
            return Decision(ALLOW, "allowlist")
        if window_open:
            # 临时窗口：放行全新身份（落库交由调用方），白名单优先于窗口。
            return Decision(ALLOW, "window_open")
        return Decision(SKIP, "deny:not_authorized")

    status, expires_at, granted_by = record
    if status == STATUS_BANNED:
        return Decision(SKIP, "deny:banned")
    if status != STATUS_ACTIVE:
        # 未知 status（防御）：宁拒绝不误放。
        return Decision(SKIP, "deny:unknown_status")
    if expires_at is not None and expires_at <= now:
        if identity in allowlist:
            return Decision(ALLOW, "allowlist_overrides_expired")
        if window_open and granted_by == GRANTED_BY_WINDOW:
            return Decision(ALLOW, "window_reentry")
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
