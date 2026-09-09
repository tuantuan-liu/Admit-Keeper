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
2. `banned` → 拒绝（即便在永久白名单也拒绝）——封禁是最高权限
3. `status != active`（未知 status，防御）→ 拒绝
4. `expired` → 拒绝，**但**若在 `ADMIT_ALLOWED_USERS` 白名单 → 放行
5. `active` 且未过期 → 放行
6. 无记录 → 白名单放行，否则拒绝（deny-by-default，即"收紧"）

**白名单只覆盖"过期"，不覆盖"banned"**：永久白名单不会意外复活被封禁者。

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
- 测试用 uv：`uv sync`（装 `mcp[cli]<2` + `pytest`）+ `uv run pytest`（39 项）。

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
