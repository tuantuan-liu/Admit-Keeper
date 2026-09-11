# 设计决策记录

> 以下 ADR 均为**框架无关**的判定语义 / 依赖决策，与具体接入框架无关。
> 文末「已核对」一节是**针对 Hermes 这一接入框架**的 API 核验附录，不影响其他框架接入。

## ADR-1 数据不可得时默认 fail-open（可切 fail-closed）

- **决策**：`ADMIT_FAIL_OPEN` 默认 `1`（放行）。DB 缺失 / 读取异常 / 表未建时，
  无法判定身份 -> 放行并高声告警，方向与"无插件时一致"。
- **原因**：插件在网关热路径上，瞬时 DB 锁或首启未建表若导致全量拒绝，等于整网关
  DoS。放行 + 告警比静默丢消息对可用性更友好。
- **代价**：DB 故障瞬间，**已封禁者可能漏放**。若要安全性优先，
  `ADMIT_FAIL_OPEN=0` 一键切换为拒绝。
- **备注**：插件自身**捕获异常**仍不抛给网关，绝不因准入拖垮网关。
  默认放行时必打 `warning` 日志，避免闸门静默失效。

## ADR-2 拒绝判定优先级：banned > 过期 > 无记录 > 白名单

`policy.decide()` 顺序：

1. `unavailable`（数据不可得）-> **白名单内先放行**（ADR-16），其余由 `ADMIT_FAIL_OPEN` 决定
2. `banned` -> 拒绝（即便在永久白名单、即便由窗口引入）——封禁是最高权限
3. `status != active`（未知 status，防御）-> 拒绝
4. `expired` -> 拒绝，**但** 若在 `ADMIT_ALLOWED_USERS` 白名单 -> `allowlist_overrides_expired`；
   若 `granted_by='window'` 且当前有窗口开放 -> `window_reentry`（见 ADR-8）
5. `active` 且未过期 -> 放行
6. 无记录 -> 白名单放行 / 窗口开放则 `window_open` 放行 / 否则拒绝（deny-by-default）

**白名单只覆盖"过期"，不覆盖"banned"**：永久白名单不会意外复活被封禁者。
**窗口重入同理只覆盖"窗口引入的过期"**，不覆盖 `banned`、也不覆盖付费/手工授权的过期。
**唯一的例外是第 1 步**：数据不可得时白名单能继续放行（但那时**校验不到**封禁态，见 ADR-16）。

## ADR-3 extend() 不再隐式改变状态

- 原实现 `extend` 会把 `status` 覆盖为 `active`，对 banned 身份执行续期会**悄悄复活**。
- **决策**：`extend` 遇到**任何非 `active`** 的记录（`banned` / 未知 status）一律报错，不代改状态：
  `banned` 要求先 `unban` 再续期，未知 status 要求显式 `grant` 覆盖。
- **理由**：续期是"时间维度"操作，不应改变"封禁态"这一更重的决策。**只拦 `banned` 是不够的**：
  `policy.decide` 对未知 status 明确 fail-closed（`deny:unknown_status`），若 `extend` 把它改写成
  `active`，等于绕过那层防御。已用 `test_extend_rejects_unknown_status` 锁死。

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
- 测试用 uv：`uv sync`（装 `mcp[cli]<2` + `pytest`）+ `uv run pytest`（137 项）。

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
  一支：封禁一个**从未有过记录**的人再解封 -> 凭空造出 `active + expires=NULL` = 永久授权。
  另一支：给已过期用户解封 -> 把他变成永久。"解封"是"撤销惩罚"，不是"授予权限"。
- **代价**：想把封禁用户真正放进来，需**显式**再 `grant`（或 `extend`）。这是刻意的两步：
  两个决策分开，避免一次误操作同时完成"解封 + 永久授权"。
- **备注**：解封**永久用户**仍是永久（解封不改变授权维度，只翻封禁态）；回归测试
  `test_unban_does_not_grant_permanent_to_expired` / `test_ban_then_unban_unknown_user_does_not_create_access`
  锁死。配合此语义，`ban` 的**新建**记录写成**已过期**的期限（`expires_at=now`）而非
  `NULL`——否则"封一个新人再解封"仍会留下永久记录。

## ADR-10 extend 以 `max(现在, 原到期)` 为基准

- **决策**：`extend(platform, identity, days)` 的起算基准 = `max(now, 原 expires_at)`：
  - 记录仍有效（原到期 > 现在）-> 在**原到期**上叠加，不吞掉剩余时间；
  - 已过期 / 无期限记录 -> 从**现在**起算；
  - 返回串显式回显 `基准=…` 与是否**立即生效**，新到期仍在过去时打 `[警告] 未生效`。
- **原因**：旧实现一律以旧到期为基准。给**已过期**用户续期时，新到期仍落在过去，
  工具却报"成功"——运营最常用的场景（"这人过期了，再给 7 天"）恰好静默失效，且反馈骗人。
- **代价**：无。基准规则更符合"续期"直觉，且返回串把基准与生效性摊开，避免再次静默。
- **备注**：回归测试 `test_extend_expired_takes_effect_immediately` /
  `test_extend_active_stacks_on_existing_expiry` 锁死。`extend` 遇 `banned` 仍**报错**
  （ADR-3 不变）。

## ADR-11 日志优先 loguru，但保持「可选依赖 + 回退」

- **决策**：统一告警出口 `gate.warn()` **优先 loguru**；未安装则回退标准库 `logging`，最后退
  stderr。loguru 列为**可选** extra（`pyproject` 的 `[log]`，`pip install -e ".[log]"` 启用），
  **不进必需依赖**。
- **原因**：CLAUDE.md 要求日志优先 loguru；但准入层跑在网关进程热路径内、刻意保持「零必需依赖」
  （见 ADR-1 / db.py 顶部说明）。把 loguru 设为可选，既可满足「优先 loguru」，又不会因网关环境
  没装 loguru 而 import 崩溃。回退链保证任何环境下告警都不丢、且 `warn()` 绝不外抛。
- **代价**：装了 loguru 时告警只进 loguru sink（不再进标准库 logging）；两者并存会产生重复，
  故只走一条。测试环境不装 loguru，走标准库回退，`caplog` 可直接断言。

## ADR-12 数据库后端抽象：`Backend` 接口 + SQLite 默认，为 MySQL 留接入点

- **决策**：`db.py` 内定义 `Backend` 抽象（连接 `connect`、建表迁移 `ensure_schema`、异常类型
  `error`、参数占位符 `placeholder`、UPSERT `upsert_allowed`），内置 `SQLiteBackend` 为默认；
  用 `ADMIT_DB_BACKEND` 选择后端。MCP 的 grant/extend 与窗口落库都改走 `db.upsert_allowed()`，
  UPSERT 语法不再散落。
- **原因**：CLAUDE.md 要求「数据库连接与操作代码不要写死，后续可能适配 MySQL」。把随引擎而异的
  部分收敛到一个接口，将来实现 `MySQLBackend` + `register_backend()` 即可，调用方（gate / mcp）
  无需改动。
- **为何后端抽象**就地**放 `db.py` 而非拆成兄弟模块**：`db.py` 被两种方式导入 —— 插件包内
  `admit_keeper.db` 与 MCP/脚本顶层 `db`；一旦出现包内相对导入，顶层导入即 ImportError（同类坑
  见本文件 ADR-8）。留在同模块内，两种导入都成立。
- **未做**：不内置 MySQL 实现（无驱动、无法验证），仅留接口与文档；MCP 侧另有若干简单
  SELECT/UPDATE 仍用 `?` 占位符，适配 MySQL 时需改为后端占位符 —— 已在该处注释标注。

## ADR-14 撤销封禁必须显式确认（`remove` 需 `force=True`）

- **决策**：`remove` 对 `status='banned'` 的记录**默认拒绝**并报错，要求显式传 `force=True`；
  无记录时返回 `NOT_FOUND`（与 `unban` 口径一致），不再谎报成功。
- **原因**：`remove` 会把记录**彻底删掉**，该身份随即按「无记录」重新判定 —— 于是开放中的临时
  窗口（`window_open`）会**立刻**把他放进来，封禁还被从 `get_expired` 里抹掉、审计断层。
  实测过这条链：`ban` -> `remove` -> 开窗 -> `gate()` 返回 `window_open`（放行）。即 `remove`
  是一条**比 `grant` 更重**的操作（`grant` 至少还留一条记录），却原本没有任何门槛。
- **代价**：删除封禁记录多一步确认。只是想让某人恢复访问，应改用 `unban`（保留记录与期限，ADR-9）。
- **备注**：`grant` 对 banned 的同类门槛见 ADR-17；工具级的 `force` 只影响「是否允许这次操作」，
  不改变判定优先级（ADR-2），`banned` 依然是最高优先级。

## ADR-15 `granted_by` 身兼「来源」与「操作者」两职，`by` 拒绝保留字

- **决策**：`granted_by` 一列同时承担两个含义 —— **来源标记**（`window` = 该记录由临时准入窗口
  引入，ADR-8 的窗口重入判据）与**操作者**（`admin` / `system` / 调用方自定义）。工具参数 `by`
  经 `_by()` 校验：空串归一为 `admin`，**拒绝等于 `window` 的值**，且校验失败不落库。
- **原因**：`window` 是窗口重入的**唯一判据**（`policy.decide` 只看 `granted_by`）。而 `by` 是
  外部（含 LLM agent 的幻觉/拼错参数）可直接指定的。实测过这条链：
  `grant("feishu","ou_x", days=-1, by="window")` 会写入 `granted_by='window'`，此后**每一次**
  窗口重开都会把他当作「窗口老面孔」自动放行并顺延到期 —— 等于无限自动续期。`by` 因此是
  **决策输入**，不是纯注释字段。
- **未做**：不新增 `source` 列把两个概念拆开（需 schema 迁移 + `db.lookup` 元组 3->4 列，波及
  全部读路径与测试）。当前用「保留字校验 + 文档说明」控制风险；若日后 `by` 还需要承载更多
  来源语义，再拆列。
- **备注**：`open_window` 的 `by` 落在 `admit_window.created_by` 列、**不参与任何判定**，故不校验。

## ADR-16 数据不可得时，白名单先于 `fail_open` 生效

- **决策**：`policy.decide` 的 `unavailable` 分支内，先判 `identity in allowlist` 则放行
  （reason `allowlist`），否则才按 `ADMIT_FAIL_OPEN` 决定方向。
- **原因**：`ADMIT_ALLOWED_USERS` 是**纯 env 配置、不依赖 DB**，而「永久白名单」的语义就是
  「这些人永远该放行」。DB 故障不该让白名单失效 —— 否则一次 DB 抖动会把确定该放行的人也拒掉。
- **代价**：此刻**无法校验封禁态**，白名单里的已封禁者也会被放行。要绝对安全，应设
  `ADMIT_FAIL_OPEN=0` **并同时**收紧白名单（两个旋钮都拧紧才有效）。放行时 `gate` 侧必定发告警，
  使这次降级可见。
- **注意（不对称，勿误当遗漏）**：同一情形下**窗口不生效** —— 窗口是否开放依赖 DB，数据不可得时
  无从判断；只有不依赖 DB 的白名单能继续生效。
- **实现位置**：判定在 `policy.decide`（保持无 IO、可单测），告警在 `gate.gate()`（`policy` 不碰
  IO，且放 `gate` 处使所有框架接入层都受益，不止 Hermes）。
- **护栏**：白名单的优先**只能**存在于 `unavailable` 分支内。若把它提到函数最前，`banned` 铁律
  （ADR-2）与 `allowlist_overrides_expired` 语义会一起被破 —— 已用
  `test_unavailable_allowlist_does_not_leak_into_normal_path` 一次性锁死三个正常路径的判词。

## 已核对：Hermes 插件 / MCP 配置 API（对照真实源码）

以下假设均已对照本机安装的 Hermes 源码核实（`envs/hermes/Lib/site-packages/`）：

| 假设 | 结论 | 依据 |
|---|---|---|
| `kind: standalone` | [是] 合法 | `hermes_cli/plugins.py` `_VALID_PLUGIN_KINDS` |
| 插件目录 `~/.hermes/plugins/<name>/` 含 `plugin.yaml`+`__init__.py` | [是] 用户插件目录 | 同上 `get_bundled_plugins_dir`/发现逻辑 |
| `plugin.yaml` 字段 `name/kind/version/description` | [是] `kind` 缺省即 standalone | `_parse_manifest` |
| `register(ctx)`（`ctx=PluginContext`） | [是] | `_load_plugin` |
| `ctx.register_hook("pre_gateway_dispatch", cb)` | [是] 合法钩子 | `VALID_HOOKS` |
| 回调以关键字 `event/gateway/session_store` 调用 | [是] | `PluginManager.invoke_hook`->`cb(**kwargs)` |
| 返回 `None`/`{"action":"allow"}`->放行；`{"action":"skip","reason"}`->丢弃 | [是] | `gateway/run.py` `_handle_message` |
| 钩子触发点位于应用内鉴权之前 | [是]（比方案文档"平台白名单之后"更靠前） | `gateway/run.py` `_handle_message` |
| 顶层 `mcp_servers:` key + `command/args/env`(stdio) | [是] | `tools/mcp_tool.py` `MCPServerTask` stdio 分支 |
| `plugins.enabled` 门控 | [是] | `hermes_cli/plugins.py` |
| **MCP 子进程 env 被过滤**：只透传 `PATH/HOME/USER/LANG/LC_ALL/TERM/SHELL/TMPDIR`+`XDG_*`+`mcp_servers.<name>.env` 显式写的变量 | [是] | `tools/mcp_tool.py` `_build_safe_env()`（`ADMIT_KEEPER_DB` 须在 `env:` 里显式给出） |
| **Hermes env(运行 gateway 的 python)本身没装 `mcp` 包** | [是] | `envs/hermes/Lib/site-packages/` 无 `mcp`/`mcp*.dist-info`（有 hermes_cli/acp_adapter/anyio/httpx） |
| 插件判库在否用 `os.path.exists()`，不展开 `~` | [是] | `admit_keeper/db.py` `lookup_plugin`（`ADMIT_KEEPER_DB` 用绝对路径，别用 `~`，否则误判库不存在->fail-open 全放行） |

**推论**：方案 A（`FEISHU_ALLOW_ALL_USERS=true` + 插件当唯一门卫）成立——钩子在
Hermes 自身鉴权之前、又在平台 adapter 之后，插件确实是准入唯一门卫。

**注**：插件被加载为包 `hermes_plugins.<slug>`，故插件用**包内相对导入**引入共享的
`db.py`/`policy.py`；MCP 是独立脚本，用**顶层导入**（`install.sh` 将同源 `db/policy`
复制到 profile 目录）。二者语义完全一致。
