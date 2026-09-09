# Admit-Keeper

**平台无关的准入控制组件** —— 在任何网关/框架的"消息进 agent 前"验票放行。源自「飞书体验用户限时封禁」，但设计为**全平台 + 可移植**。这里"全平台"指两个正交维度：

- **接入框架**：Hermes、FastAPI、自研 bot 框架、RAG 流水线……项目内核不含任何框架代码，任何框架只需写一个几行的"门卫接入层"（即调用共享的 `admit_keeper.gate`）即可接上。
- **消息渠道**：飞书 / 企微 / Telegram 等只是"已接入渠道"，由 `ADMIT_GATE_PLATFORMS` 决定哪些渠道走准入，与框架无关。

`admit`（准许/准入）+ `keeper`（看守/保管）：keeper 既对应**准入层（门卫适配器）**的拦截看守，又对应**管理层（MCP）**的资格保管管理。

> GitHub 同名仓库无占用，不撞 OPA Gatekeeper 等知名项目。下文 Hermes 只是**其中一个具体接入示例**。

## 框架无关核心（先懂它）

内核 + 门卫助手，全部**不含任何框架代码**，任何框架都可直接用：

- `admit_keeper/policy.py` → `decide(identity, record, allowlist, now, fail_open, unavailable)` —— 纯决策、无 IO、可单测。
- `admit_keeper/db.py` → `lookup_plugin(platform, identity)` / `db_path()` —— 只读共享名册，与 MCP 解耦。
- `admit_keeper/gate.py` → `gate(...)` / `is_allowed(...)` —— **框架无关门卫助手**：读环境变量、调 `lookup_plugin` + `decide`，一个函数给出放行/丢弃判定。这就是"换框架只写接入层"的那份共享实现。
- `mcp/admit_keeper_mcp.py` —— 管理层（FastMCP），低频读写同一库。

任何框架的"消息进 agent 前"接线，就是**几行**调 `gate.is_allowed()`（平台/身份/环境变量/白名单/fail-open 全在门卫助手内处理）：

```python
from admit_keeper.gate import gate, is_allowed

def on_message(event):
    if not is_allowed(event.platform, event.user_id):
        return drop()          # 框架各自的"丢弃"动作
    return dispatch(event)     # 否则正常进入 agent
```

> 若想看到**被判**的原因（用于日志/告警），用完整版：
> ```python
> d = gate(event.platform, event.user_id)   # d.reason ∈ deny:banned / deny:expired / allowlist ...
> if not d.is_allow():
>     return drop()
> ```

> "丢弃动作" `drop()` 因框架而异（Hermes 返回 `{"action":"skip"}`、FastAPI 返回 `403`、
> bot 库返回"不处理"）——内核只做判定，把"如何丢弃"交给接入层。

## 接入 Hermes（示例）

Hermes 是其中一个**具体接入示例**。下面以"单配置部署"为例（配置即 `~/.hermes/config.yaml`；若用命名 profile，则把
`~/.hermes` 换成 `~/.hermes/profiles/<profile>`，命令加 `-p <profile>`）。

**1) 复制文件**（插件 + MCP 到 `~/.hermes`）
```bash
bash scripts/install.sh                # 插件→~/.hermes/plugins/admit-keeper/；MCP→~/.hermes/profiles/default/
```

> ⚠️ `install.sh` 里那行 `python3 -m pip install -U "mcp[cli]>=1,<2"` 会在 `python3` 对应
> 的环境里装 mcp。**别指望它**——MCP 服务端最好放进独立 venv（见第 2 步），既隔离又不污染
> gateway 环境。若 `python3` 会装进 gateway 进程用的环境，请直接跳过大可不必。

**2) 给 MCP 服务端建独立 venv**（本机 Hermes env **没有装 mcp**，需自备一个带 `mcp<2` 的解释器；Windows 用 `Scripts\`，Linux/macOS 用 `bin/`）
```bash
python -m venv ~/.hermes/mcp-venv
~/.hermes/mcp-venv/Scripts/python -m pip install -U "mcp[cli]>=1.0,<2"   # Windows
# ~/.hermes/mcp-venv/bin/python -m pip install -U "mcp[cli]>=1.0,<2"      # Linux/macOS

# 把 MCP 脚本 + 同源 db/policy 放到 MCP 能 import 的位置（自包含目录）
cp mcp/admit_keeper_mcp.py ~/.hermes/
cp admit_keeper/db.py admit_keeper/policy.py ~/.hermes/
```

**3) 让 MCP 与插件指向同一个库**：插件在 gateway 进程里读 env，MCP 是子进程
（Hermes 启动它时只透传 `PATH/HOME/...` + `XDG_*` + 你在 `mcp_servers.<name>.env` **显式写的变量**）。
```bash
# 插件侧：只配平台与 fail-open，不要在 .env 设 ADMIT_KEEPER_DB
cat >> ~/.hermes/.env <<'EOF'
ADMIT_GATE_PLATFORMS=feishu
# ADMIT_FAIL_OPEN=0        # 默认放行；要更安全才置 0
EOF
```
> 插件不设 `ADMIT_KEEPER_DB` 的原因：让它在网关进程里用 `$HERMES_HOME/admit_keeper.db`
> 默认值（HERMES_HOME 在网关进程一定存在）。**不要用 `~` 写路径**——插件用
> `os.path.exists()` 判库在不在，Python 不展开 `~`，会误判库不存在 → fail-open 全部放行，
> 闸门静默失效。MCP 侧的路径见下，取**绝对路径**、并指向与插件同一个文件。

**4) 编辑 `~/.hermes/config.yaml`**，在顶层追加（保留原有键）。命令路径、DB 路径都替换成
你机器上的**绝对路径**（Windows 通常 `C:/Users/<用户名>/.hermes`，Linux/macOS `$HOME/.hermes`）：
```yaml
plugins:
  enabled:
    - admit-keeper
mcp_servers:
  admit-keeper:
    command: "C:/Users/你的用户名/.hermes/mcp-venv/Scripts/python.exe"
    args: ["C:/Users/你的用户名/.hermes/admit_keeper_mcp.py"]
    env:
      ADMIT_KEEPER_DB: "C:/Users/你的用户名/.hermes/admit_keeper.db"
```
> `plugins.enabled` 必须是**列表**（顶层 `plugins:` → `enabled:`）；缺省/畸形 = 什么都不启用，
> 这就是"装了但没生效"的常见原因。也可用 `hermes plugins enable admit-keeper` 写入。
> MCP 的 `ADMIT_KEEPER_DB` 要能落到与插件同一个文件：插件用 `$HERMES_HOME/admit_keeper.db`，
> MCP 子进程里 HERMES_HOME 被过滤掉了，所以此处写死为同等绝对路径。

**5) 重启网关**
```bash
hermes gateway restart               # 默认 profile
# hermes -p <profile> gateway restart   # 命名 profile
```

**6) 验证**
```bash
hermes plugins                      # 列表里 admit-keeper 应为 enabled
tail -f ~/.hermes/logs/gateway.log # 启动后应无 pre_gateway_dispatch 注册失败
```
端到端自测：用 MCP `ban("feishu", "ou_xxx")` 封一个飞书用户，再由该用户发一条消息，
应被丢弃且日志出现 `pre_gateway_dispatch skip: reason=deny:banned`。

> **mcp 版本坑**：mcp 2.x 已把 `FastMCP` 改名/移除为 `MCPServer`。本组件基于 v1 的 `FastMCP`，
> 因此 MCP 服务端依赖一律钉 `mcp[cli]>=1.0,<2`，且必须跑在一个装有 mcp<2 的解释器下
> （上文独立 venv）。`pyproject`/`install.sh` 均已如此处理。

## 使用（通过 MCP 工具管理）

```python
grant("feishu", "ou_xxx", days=7)     # 开通 7 天
grant("feishu", "ou_yyy")             # 永久授权
extend("feishu", "ou_xxx", days=3)    # 续期 3 天（banned 需先 unban）
ban("feishu", "ou_zzz", note="滥用")   # 立即封禁
unban("feishu", "ou_zzz")             # 解封
get_expired()                         # 列出过期/封禁
query("feishu", "ou_xxx")             # 查单个状态
list_all()                            # 全部记录
remove("feishu", "ou_xxx")            # 删记录（删除后无白名单兜底则拒收）
```

## 目录结构

```
Admit-Keeper/
├── admit_keeper/                  # 准入内核 + Hermes 接入层（此目录 = ~/.hermes/plugins/admit-keeper/ 内容）
│   ├── plugin.yaml                # Hermes 插件元数据（仅 Hermes 接入用）
│   ├── __init__.py                # Hermes 接入层（register + pre_gateway_dispatch 钩子），委托共享 gate
│   ├── gate.py                    # 框架无关门卫助手 gate()/is_allowed() —— 换框架唯一要调用的入口
│   ├── policy.py                  # 纯决策核心（可单测）—— 框架无关
│   └── db.py                      # 共享 SQLite 数据层（WAL）—— 框架无关
├── mcp/
│   └── admit_keeper_mcp.py        # 授权管理服务端（FastMCP）—— 框架无关
├── config/
│   ├── env.example                # Hermes 接入：该 profile 的 .env 参考
│   ├── config.example.yaml        # Hermes 接入：该 profile 的 config.yaml 参考
│   └── db.schema.sql              # 建表 DDL 参考
├── tests/
│   ├── conftest.py
│   ├── test_policy.py             # 决策矩阵单测（框架无关）
│   ├── test_db.py                 # db 读写 + 去重（框架无关）
│   ├── test_gate.py               # 共享门卫助手 gate()/is_allowed()（框架无关）
│   ├── test_mcp_integration.py    # MCP 工具级集成（grant/ban/unban/...）
│   └── test_plugin.py             # Hermes 接入层插件钩子（含 fail-open 告警断言）
├── scripts/
│   ├── install.sh                 # 复制到 ~/.hermes + 补 mcp[cli]（仅 Hermes 接入用）
│   └── make_db.py                 # 手动建库/迁移（框架无关）
└── docs/
    ├── architecture.md            # 三层架构 + 共享名册（框架无关）
    └── design-decisions.md        # 关键决策记录（ADR，框架无关）+ Hermes API 核验附录
```

### 运行时布局（Hermes 接入示例）

单配置（默认 profile，配置即 `~/.hermes/config.yaml`）：

```
~/.hermes/
    plugins/admit-keeper/                       # 准入插件（加载为 hermes_plugins.admit_keeper）
        plugin.yaml  __init__.py  gate.py  policy.py  db.py
    admit_keeper_mcp.py  policy.py  db.py       # MCP 服务端 + 同源副本（自包含目录）
    mcp-venv/                                   # 独立 venv，含 mcp[cli]<2 + FastMCP
    config.yaml                                 # plugins.enabled + mcp_servers 在此
    .env                                        # ADMIT_KEEPER_DB 等（插件进程读 env）
    admit_keeper.db                             # 共享名册，首次写入自动建
```

命名 profile 则把 `~/.hermes` 换成 `~/.hermes/profiles/<profile>`，命令加 `-p <profile>`。

## 接入其他框架（移植）

内核与框架无关，移植 = 把共享的 `admit_keeper.gate` 接进**自己框架的"消息进入点"**。
判定、库、管理层、环境变量全部不动。Hermes 只是示例之一：

**FastAPI 示例**（其它框架同理——找"请求进 handler 前"的那个中间件/钩子/router）：
```python
from fastapi import HTTPException
from admit_keeper.gate import is_allowed      # 共享门卫助手

@app.middleware("http")
async def admit(req, call_next):
    platform = req.headers.get("x-platform")   # 从你的事件里取出 platform / identity
    identity = req.headers.get("x-user-id")
    if not is_allowed(platform, identity):     # 内核判定
        raise HTTPException(403, "not_authorized")   # 框架的"丢弃"动作
    return await call_next(req)
```

- **接入框架**：仅需重写"门卫接入层"（Hermes 用 `_on_pre_gateway_dispatch`、FastAPI 用中间件、
  bot 库用 router 钩子），统一调 `admit_keeper.gate.is_allowed()`，内核不动。
- **渠道平台**：`ADMIT_GATE_PLATFORMS` 决定哪些渠道走准入，列表外**直接放行**。加渠道 = 改这一个配置，无需改代码。
- **判定语义**（固定）：`banned > 过期 > 无记录 > 白名单`，白名单只覆盖过期不覆盖 banned，
  见 [docs/design-decisions.md](docs/design-decisions.md)。

## 关键环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `ADMIT_KEEPER_DB` | `$HERMES_HOME/admit_keeper.db` | 共享名册 DB 路径 |
| `ADMIT_GATE_PLATFORMS` | `feishu` | 受管平台；列表外放行 |
| `ADMIT_ALLOWED_USERS` | 空 | 永久白名单（只覆盖过期，不覆盖 banned） |
| `ADMIT_FAIL_OPEN` | `1` | 数据不可得时放行；`0` 为拒绝（更安全） |

## 测试

```bash
uv sync                                  # 安装 dev 依赖（uv 环境，含 mcp[cli]<2 + pytest）
uv run pytest                            # 全量：单元 + MCP 工具级集成 + 插件钩子，共 66 项
```

详见 [docs/architecture.md](docs/architecture.md) 与 [docs/design-decisions.md](docs/design-decisions.md)。
