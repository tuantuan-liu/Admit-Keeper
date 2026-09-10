# 设计决策记录

> 以下 ADR 均为**框架无关**的判定语义 / 依赖决策，与具体接入框架无关。
> 文末「已核对」一节是**针对 Hermes 这一接入框架**的 API 核验附录，不影响其他框架接入。

## ADR-1 数据不可得时默认 fail-open（可切 fail-closed）

- **决策**：`ADMIT_FAIL_OPEN` 默认 `1`（放行）。DB 缺失 / 读取异常 / 表未建时，
  无法判定身份 → 放行并高声告警，方向与"无插件时一致"。
- **原因**：插件在网关热路径上，瞬时 DB 锁或首启未建表若导致全量拒绝，等于整网关
  DoS。放行 + 告警比静默丢消息对可用性更友好。
- **代价**：DB 故障瞬间，**已封禁者可能漏放**。若要安全性优先，
  `ADMIT_FAIL_OPEN=0` 一键切换为拒绝。
- **备注**：插件自身**捕获异常**仍不抛给网关，绝不因准入拖垮网关。
  默认放行时必打 `warning` 日志，避免闸门静默失效。

## ADR-2 拒绝判定优先级：banned > 过期 > 无记录 > 白名单

`policy.decide()` 顺序：

1. `unavailable`（数据不可得）→ 由 `ADMIT_FAIL_OPEN` 决定
2. `banned` → 拒绝（即便在永久白名单、即便由窗口引入）——封禁是最高权限
3. `status != active`（未知 status，防御）→ 拒绝
4. `expired` → 拒绝，**但** 若在 `ADMIT_ALLOWED_USERS` 白名单 → `allowlist_overrides_expired`；
   若 `granted_by='window'` 且当前有窗口开放 → `window_reentry`（见 ADR-8）
5. `active` 且未过期 → 放行
6. 无记录 → 白名单放行 / 窗口开放则 `window_open` 放行 / 否则拒绝（deny-by-default）

**白名单只覆盖"过期"，不覆盖"banned"**：永久白名单不会意外复活被封禁者。
**窗口重入同理只覆盖"窗口引入的过期"**，不覆盖 `banned`、也不覆盖付费/手工授权的过期。

## ADR-3 extend() 不再隐式解封

- 原实现 `extend` 会把 `status` 覆盖为 `active`，对 banned 身份执行续期会**悄悄复活**。
- **决策**：`extend` 遇到 `banned` 记录直接报错，要求先 `unban` 再续期。
- **理由**：续期是"时间维度"操作，不应改变"封禁态"这一更重的决策。

## ADR-4 决策逻辑集中化，避免双处漂移

- 判定语义只写在 `policy.py`，插件与 MCP 都 import 同一份。
- `db.py` 提供唯一的数据读取/路径解析。两侧永远一致，改一处即生效。

## ADR-5 白名单 `ADMIT_ALLOWED_USERS` 跨平台共享（知悉）

- env 白名单按 identity 匹配，**不分平台**；而 DB 记录按 `(platform, identity)` 隔离。
- 若多个平台上线，需确认 id 格式不会跨平台误命中（如飞书 `ou_*` 与企微 id）。
- 若需按平台区分，可扩展为 `ADMIT_ALLOWED_USERS` 支持 `platform:identity` 语法
  （当前未实现）。

## ADR-6 时间戳比较基于固定 UTC ISO-8601 字符串

- 两端统一 `%Y-%m-%dT%H:%M:%SZ`，字典序与时间序一致。
- **脆弱点**：任何一方写入不同格式会静默失效；已用单测锁死格式（见 test_db）。

## ADR-7 MCP 依赖钉 `mcp<2`（v1 `FastMCP`）

- mcp 2.x 已将 `mcp.server.fastmcp.FastMCP` **改名/移除**为 `mcp.server.mcpserver.MCPServer`。
  本组件 `mcp/admit_keeper_mcp.py` 基于 v1 `FastMCP`，因此在 `install.sh`、
  `pyproject`（`mcp[cli]>=1.0,<2`）统一**钉 `<2`**，避免装到 2.x 后 `FastMCP` import 崩溃。
- 若日后要升级到 mcp 2.x，需把 `FastMCP` 迁移到 `MCPServer`（API 多处变动），届时再单独处理。
- 测试用 uv：`uv sync`（装 `mcp[cli]<2` + `pytest`）+ `uv run pytest`（118 项）。

## ADR-8 临时准入窗口：放行「无记录」新身份 + 重开时重新纳入「窗口引入」的老面孔

- **决策**：新增 `admit_window` 表 + `open_window` / `close_window` / `list_windows` 工具。
  窗口 `[start_at, end_at)` 内，**记录为 `None`** 的身份可进，并由 `gate` 落一条 `active`、
  `expires_at = end_at`、`granted_by='window'` 的记录；`banned` 不受影响。
  `platform` 可为**任意渠道名**，或通配 `*`（一次开窗即覆盖所有平台；`lookup_window` 匹配
  `platform=? OR platform='*'`，多窗口共存时取更晚的 `end_at`）。
- **窗口重开（`window_reentry`）**：窗口引入的记录（`granted_by='window'`）过期后，**新窗口
  开放时重新放行**并把到期顺延到新窗口结束 —— 修「每日限时开放第二天哑火」。判据是记录里的
  **来源标记**，不是"过期"本身。
  - **只重新纳入「窗口引入的」**：手工 / 付费授权（`granted_by` 非 `window`，或旧库迁移出的
    `NULL`）过期后**照旧拒**（`deny:expired`）。窗口是"体验名额"，不能顺带复活别人的付费到期。
  - **`banned` 恒拒**：来源标记不影响封禁优先级（ADR-2），封过的窗口老面孔一律不再放行。
- **原因**：需要「限时开放体验」——某时段让新面孔进来试用，时段一过自动失效，且不触碰已有
  授权 / 封禁。落库使「谁在窗口期进来过」可审计。
- **理由（窗口覆盖的始终是「未付费」这条线）**：窗口管两种人 —— ① 从未授权的新面孔
  （`window_open`）；② **它自己**上次放进来、到期后掉出去的老面孔（`window_reentry`）。
  两者都不涉及付费。付费 / 手工授权的过期记录**永不被窗口覆盖**，与「白名单只覆盖过期、
  不覆盖 banned」的既有分寸一致 —— 窗口是"体验名额"，不是"顺带复活别人的付费到期"。
- **代价**：热路径出现**唯一一次写**（每用户每窗口仅首次进入 / 重入时写）。窗口记录形状由
  2 元组扩为 **3 元组** `(status, expires_at, granted_by)`，波及 `db.lookup` 的全部调用点；
  读路径对**缺 `granted_by` 列的旧库**自动降级为 2 列查询（`granted_by` 视作 `None` = 非窗口
  引入，正是安全默认），写路径由 `_migrate()` 补列（`CREATE TABLE IF NOT EXISTS` 不会给已存在
  的表加列）。且窗口查询 `db.lookup_window_plugin()` 采用**与 `lookup_plugin` 刻意不同的降级
  语义**：DB 缺失 / 表未建 / 任何异常一律视作「无窗口」(None)，**绝不** 返回 `unavailable`
  ——否则旧库（无 `admit_window` 表）会因 `no such table` 把整条判定拖进 fail-open 全放行
  （ADR-1 最坏情形）。已用回归测试 `test_gate_old_db_without_window_table_not_fail_open` 锁死。
- **备注**：时间统一存 UTC；MCP 侧 `_parse_dt` 支持完整 ISO-8601（带偏移按偏移、不带按本机
  本地时区）与 `HH:MM` 简写（今天本地，跨零点顺延次日）。`ensure_schema()` 因新增第二张表
  改用 `executescript()`（`execute()` 一次只允许一条语句）。来源标记字面量 `"window"` 在
  `policy.GRANTED_BY_WINDOW` 与 `db.WINDOW_GRANTED_BY` 各写一份（**故意不互相 import**：
  `policy.py` 会被 MCP 以顶层模块方式导入，一旦出现包内相对导入即 ImportError），
  二者一致性由 `test_window_marker_constants_agree` 锁死。

## ADR-9 unban 只解封，保留原授权期限（不制造授权）

- **决策**：`unban` 只把 `status` 由 `banned` 翻回 `active`，**绝不改动 `expires_at`**。
  无 `banned` 记录时返回 `NOT_FOUND` 且**不建任何记录**。
- **原因**：旧实现 `unban` 走 grant 路径 `SET expires_at=NULL`，等于**永久提权**。最危险的
  一支：封禁一个**从未有过记录**的人再解封 → 凭空造出 `active + expires=NULL` = 永久授权。
  另一支：给已过期用户解封 → 把他变成永久。"解封"是"撤销惩罚"，不是"授予权限"。
- **代价**：想把封禁用户真正放进来，需**显式**再 `grant`（或 `extend`）。这是刻意的两步：
  两个决策分开，避免一次误操作同时完成"解封 + 永久授权"。
- **备注**：解封**永久用户**仍是永久（解封不改变授权维度，只翻封禁态）；回归测试
  `test_unban_does_not_grant_permanent_to_expired` / `test_ban_then_unban_unknown_user_does_not_create_access`
  锁死。配合此语义，`ban` 的**新建**记录写成**已过期**的期限（`expires_at=now`）而非
  `NULL`——否则"封一个新人再解封"仍会留下永久记录。

## ADR-10 extend 以 `max(现在, 原到期)` 为基准

- **决策**：`extend(platform, identity, days)` 的起算基准 = `max(now, 原 expires_at)`：
  - 记录仍有效（原到期 > 现在）→ 在**原到期**上叠加，不吞掉剩余时间；
  - 已过期 / 无期限记录 → 从**现在**起算；
  - 返回串显式回显 `基准=…` 与是否**立即生效**，新到期仍在过去时打 `⚠ 未生效`。
- **原因**：旧实现一律以旧到期为基准。给**已过期**用户续期时，新到期仍落在过去，
  工具却报"成功"——运营最常用的场景（"这人过期了，再给 7 天"）恰好静默失效，且反馈骗人。
- **代价**：无。基准规则更符合"续期"直觉，且返回串把基准与生效性摊开，避免再次静默。
- **备注**：回归测试 `test_extend_expired_takes_effect_immediately` /
  `test_extend_active_stacks_on_existing_expiry` 锁死。`extend` 遇 `banned` 仍**报错**
  （ADR-3 不变）。

## 已核对：Hermes 插件 / MCP 配置 API（对照真实源码）

以下假设均已对照本机安装的 Hermes 源码核实（`envs/hermes/Lib/site-packages/`）：

| 假设 | 结论 | 依据 |
|---|---|---|
| `kind: standalone` | ✅ 合法 | `hermes_cli/plugins.py` `_VALID_PLUGIN_KINDS` |
| 插件目录 `~/.hermes/plugins/<name>/` 含 `plugin.yaml`+`__init__.py` | ✅ 用户插件目录 | 同上 `get_bundled_plugins_dir`/发现逻辑 |
| `plugin.yaml` 字段 `name/kind/version/description` | ✅ `kind` 缺省即 standalone | `_parse_manifest` |
| `register(ctx)`（`ctx=PluginContext`） | ✅ | `_load_plugin` |
| `ctx.register_hook("pre_gateway_dispatch", cb)` | ✅ 合法钩子 | `VALID_HOOKS` |
| 回调以关键字 `event/gateway/session_store` 调用 | ✅ | `PluginManager.invoke_hook`→`cb(**kwargs)` |
| 返回 `None`/`{"action":"allow"}`→放行；`{"action":"skip","reason"}`→丢弃 | ✅ | `gateway/run.py` `_handle_message` |
| 钩子触发点位于应用内鉴权之前 | ✅（比方案文档"平台白名单之后"更靠前） | `gateway/run.py` `_handle_message` |
| 顶层 `mcp_servers:` key + `command/args/env`(stdio) | ✅ | `tools/mcp_tool.py` `MCPServerTask` stdio 分支 |
| `plugins.enabled` 门控 | ✅ | `hermes_cli/plugins.py` |
| **MCP 子进程 env 被过滤**：只透传 `PATH/HOME/USER/LANG/LC_ALL/TERM/SHELL/TMPDIR`+`XDG_*`+`mcp_servers.<name>.env` 显式写的变量 | ✅ | `tools/mcp_tool.py` `_build_safe_env()`（`ADMIT_KEEPER_DB` 须在 `env:` 里显式给出） |
| **Hermes env(运行 gateway 的 python)本身没装 `mcp` 包** | ✅ | `envs/hermes/Lib/site-packages/` 无 `mcp`/`mcp*.dist-info`（有 hermes_cli/acp_adapter/anyio/httpx） |
| 插件判库在否用 `os.path.exists()`，不展开 `~` | ✅ | `admit_keeper/db.py` `lookup_plugin`（`ADMIT_KEEPER_DB` 用绝对路径，别用 `~`，否则误判库不存在→fail-open 全放行） |

**推论**：方案 A（`FEISHU_ALLOW_ALL_USERS=true` + 插件当唯一门卫）成立——钩子在
Hermes 自身鉴权之前、又在平台 adapter 之后，插件确实是准入唯一门卫。

**注**：插件被加载为包 `hermes_plugins.<slug>`，故插件用**包内相对导入**引入共享的
`db.py`/`policy.py`；MCP 是独立脚本，用**顶层导入**（`install.sh` 将同源 `db/policy`
复制到 profile 目录）。二者语义完全一致。
