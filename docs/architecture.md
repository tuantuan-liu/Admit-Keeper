# 架构

## 目标

组件与**接入框架无关**：它对任何"消息进 agent 前"的网关 / 框架提供同一套判定。框架只负责把
消息的 `platform + identity` 交给内核，并在返回"拒绝"时执行各自框架的丢弃动作。

## 三层模型

```
┌───────────────────────────────────────────────────────────┐
│  ① 准入层（框架接入点 · 每个框架各写各的）                 │
│     同步 / 热路径 / 只读                                     │
│     任意框架的"消息进入点"调 admit_keeper.gate 判准入   │
│     Hermes = pre_gateway_dispatch 钩子 · FastAPI = 中间件 … │
└──────────────┬────────────────────────────────────────────┘
               │ 只读
               ▼
        ┌─────────────────────┐
        │  admit_keeper.db     │   ← ② 共享名册（SQLite，WAL）
        │  admit_allowed 表     │      platform + identity + status + expires_at
        └──────────────┬──────┘
               │ 读写
               ▼
┌───────────────────────────────────────────────────────────┐
│  ③ 管理层（MCP server · 框架无关）                          │
│     异步 / 低频 / 读写                                      │
│     grant/ban/unban/extend/query/get_expired/list/remove  │
└───────────────────────────────────────────────────────────┘
```

- ① 准入层：每次消息进 agent 前判定，只读库。**每个框架只需写这一层**（几行调共享的
  `admit_keeper.gate`，判定 / 库 / 环境变量全在门卫助手里处理，见 README）。
- ② 共享名册：唯一数据来源，靠同一个 SQLite 解耦。
- ③ 管理层：动态授权 / 封禁 / 续期 / 查询，读写库。**框架无关**。

职责单向：③ 写、① 读，互不入侵。

`gate.py` 就是 ① 里**跨框架复用**的那部分门卫逻辑——它读环境变量（受管平台 / 白名单 /
fail-open）、调 `db.lookup_plugin()` + `policy.decide()`，对外只暴露 `gate()` / `is_allowed()`。
各框架（Hermes `__init__.py`、FastAPI 中间件、bot router…）都调它，自身只剩各自的"丢弃动作"。

## 决策逻辑集中化（框架无关）

判定语义（banned > 过期 > 无记录 > 白名单）统一收在 `admit_keeper/policy.py:decide()`，
三个层面**共同 import 同一份**，只有一处实现，不会因框架不同而判词漂移。
数据读写收在 `admit_keeper/db.py`；框架无关的门卫助手收在 `admit_keeper/gate.py`。

## 为什么用独立准入层，而不是改框架源码

| | 改框架源码 / 打补丁 | 独立准入层（推荐） |
|---|---|---|
| 框架升级后 | 被打回，需重打 | 无感，永不失效 |
| 到期封禁 | 实时 | 实时（每条消息都过判定） |
| 是否改框架源码 | 是 | 否 |
| 维护 | 每次升级重打 | 一次装好，不管 |
| 风险 | — | 默认 fail-open，与无准入时一致 |

## 接入其他框架（移植）

移植 = 调 `admit_keeper.gate`（见 README）接进**自己框架的"消息进入点"**，判定 / 库 / 管理层 /
环境变量全不动。示例：FastAPI 中间件、bot 库 router 钩子、RAG 流水线入口。

## Hermes 接入具体实现（示例）

Hermes 只是示例框架之一。钩子挂在 `gateway/run.py` 的 `pre_gateway_dispatch`，位于各平台 adapter
（如 feishu `_admit()` 平台白名单）**之后、Hermes 自身鉴权之前**（已对照源码核验）。因此采用：

| 设计 | 效果 |
|------|------|
| A（推荐）：`FEISHU_ALLOW_ALL_USERS=true` + 准入层当唯一门卫 | 只写 DB 即实时生效，维护量零 |
| B：`allow_all=false` + 改 `FEISHU_ALLOWED_USERS` | 每次动 env + 重启网关，维护高 |

选 A——把「全体放行」收紧成「只有 DB 活跃用户或白名单可见」，安全更好。

## 数据层

SQLite，`PRAGMA journal_mode=WAL` + `busy_timeout`，准入层热路径只读、管理层低频写，读写锁
冲突概率低。单一来源，无外部服务。
