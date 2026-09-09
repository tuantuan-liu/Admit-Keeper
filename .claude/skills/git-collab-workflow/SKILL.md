---
name: git-collab-workflow
description: "标准化 Git 协作流程：分支管理、代码检查、格式化、提交、推送、创建 PR。适用于 Gitee/GitHub 项目。"
version: 1.6.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [git, gitee, github, pr, code-review, workflow, collaboration]
    related_skills: [gitee-pr-workflow, github-pr-workflow, code-review]
---

# Git 协作流程 Skill

标准化的 Git 协作工作流：分支 → 修改 → 检查 → 提交 → 推送 → PR → 清理。

## 触发条件

当用户说以下关键词时加载此 skill：
- 提交代码 / commit / push
- 创建 PR / 合并请求
- 代码检查 / lint / format
- 提交到 Gitee / GitHub
- 提 issue / 创建 issue / 反馈 bug / 报告问题

## 绝对铁律（必须遵守）

1. **创建 PR / issue、回复 issue、回复或发表 PR 审查评论，body 末尾必须署名** —— 用 `Created by **<AGENT_NAME>**`（创建）、`Replied by **<AGENT_NAME>**`（回复 issue / 评论 / PR 审查评论）、`Reviewed by **<AGENT_NAME>**`（PR 审查汇总 / 审查报告）。`<AGENT_NAME>` 按 Phase 6 约定在运行时解析（环境变量 `AGENT_NAME` > agent 类型名 > 询问兜底，最后才匿名 `Nameless Agent`）；每个执行该流程的 agent（Hermes / Claude Code / Codex）须署名自己的可区分名称，不得跳过或冒用他人。原因：这些操作用的是用户本人的 MCP token，会以用户身份对外显示，必须如实标注来源。这也是 pr-review skill 的既有约定，两个 skill 保持一致。
2. **永远不在主分支上直接改代码**；**不自行合并 PR**；**不跳过代码检查**。

## 前置检查

在执行任何操作前，先确认：

```bash
# 1. 确认在 Git 仓库内
git rev-parse --is-inside-work-tree 2>&1

# 2. 确认远程仓库地址
git remote -v 2>&1
```

若不在 Git 仓库内，终止流程并告知用户。

## 完整流程

### Phase 1: 分支管理

```bash
# 检查当前分支
git branch --show-current

# 若不在 main/master，切换回主分支
git checkout main 2>&1 || git checkout master 2>&1

# 拉取最新代码
git pull origin main 2>&1 || git pull origin master 2>&1
```

**规则**：
- 主分支名称以 `git branch --show-current` 的实际结果为准（可能是 `main`、`master`、`dev` 等）
- 切换前检查工作区是否干净（`git status --porcelain`），有未提交更改则提示用户先 stash 或 commit

**冲突处理**：若 `git pull` 时出现合并冲突，列出冲突文件让用户决定如何解决（手动编辑 / `git merge --abort` 放弃合并）。

### Phase 2: 创建功能分支

根据用户意图选择分支前缀：

| 意图 | 前缀 | 示例 |
|------|------|------|
| 修 bug | `fix/` | `fix/login-timeout` |
| 新功能 | `feat/` | `feat/user-avatar` |
| 重构 | `refactor/` | `refactor/api-layer` |
| 文档 | `docs/` | `docs/api-readme` |
| 测试 | `test/` | `test/auth-cases` |
| 杂项 | `chore/` | `chore/dependencies` |

```bash
# 从主分支创建功能分支
git checkout -b <prefix>/<branch-name>
```

**分支命名规则**：
- 全小写，用 `-` 连接
- 语义化描述，如 `fix/cron-provider-error`、`feat/health-check-profiles`
- 不超过 50 个字符

**确认**：创建前向用户确认分支名是否正确。

### Phase 3: 代码修改

此阶段由用户或 agent 完成实际代码修改。修改完成后进入 Phase 4。

### Phase 4: 代码检查与格式化

**必须在提交前执行**。

**优先检查 pre-commit hooks**：若项目有 `.pre-commit-config.yaml`，直接运行 `pre-commit run --all-files`，跳过下面的手动检查。

#### Python 项目

```bash
# 代码检查 + 自动修复
ruff check --fix . 2>&1

# 代码格式化
ruff format . 2>&1

# 或使用 autopep8（若项目用 autopep8）
autopep8 --in-place --recursive . 2>&1

# 类型检查（如有 mypy 配置）
mypy . 2>&1 || true
```

#### Node.js / Vue / React 项目

```bash
# 代码格式化
npm run format 2>&1

# 代码检查
npm run lint 2>&1

# 类型检查（如有）
npm run type-check 2>&1 || npx vue-tsc --noEmit 2>&1
```

#### Shell 脚本

```bash
# Shell 脚本检查（如有 shellcheck）
shellcheck scripts/*.sh 2>&1 || true
```

#### 混合项目（前后端）

```bash
# 后端（Python）
cd backend/ && ruff check --fix . && ruff format . && cd ..

# 前端（Node.js）
cd frontend/ && npm run format && npm run lint && cd ..
```

**检查失败处理**：
- 若 lint/format 报错，先尝试自动修复（`ruff check --fix`、`npm run lint -- --fix`）
- 自动修复后仍有错误，列出错误信息让用户决定如何处理
- **禁止跳过检查直接提交**

### Phase 4.5: .gitignore 检查

**提交前检查是否有不该跟踪的文件被添加了**：

```bash
# 1. 检查是否有敏感文件被跟踪
git ls-files | grep -iE "\.env$|\.env\.local$|\.pem$|\.key$|id_rsa|credentials|secret" 2>/dev/null

# 2. 检查是否有构建产物/缓存被跟踪
git ls-files | grep -iE "node_modules/|__pycache__/|\.pyc$|dist/|build/|\.DS_Store|Thumbs\.db|\.idea/|\.vscode/|\.cache/" 2>/dev/null

# 3. 检查是否有大文件（>1MB）
git ls-files -z | xargs -0 -I{} sh -c 'size=$(wc -c < "{}" 2>/dev/null); [ "$size" -gt 1048576 ] && echo "{} ($((size/1024))KB)"' 2>/dev/null

# 4. 查看当前 .gitignore 内容（如有）
cat .gitignore 2>/dev/null || echo "(no .gitignore found)"
```

**发现异常时的处理**：
- **敏感文件**（.env、.pem、密钥等）→ 立即从 git 中移除：`git rm --cached <file>`，并添加到 `.gitignore`
- **构建产物**（node_modules、__pycache__、dist 等）→ 添加到 `.gitignore`
- **大文件**（>1MB）→ 提醒用户确认是否应该提交，考虑用 Git LFS
- **无 .gitignore** → 根据项目类型生成基础 `.gitignore`（Python / Node.js / 混合项目模板）

**常见需要忽略的文件（按项目类型）**：

| 项目类型 | 典型忽略项 |
|---------|-----------|
| Python | `__pycache__/`, `*.pyc`, `.venv/`, `dist/`, `*.egg-info/`, `.mypy_cache/`, `.ruff_cache/` |
| Node.js | `node_modules/`, `dist/`, `.nuxt/`, `.next/`, `*.log` |
| 通用 | `.env`, `.env.local`, `.DS_Store`, `Thumbs.db`, `.idea/`, `.vscode/`, `*.swp` |

### Phase 5: 提交代码

```bash
# 查看变更文件
git status
git diff --stat

# 添加变更文件
git add <files>

# 提交（Conventional Commits 规范）
git commit -m "<type>(<scope>): <description>"
```

**提交前确认**：展示 `git diff --staged` 的变更摘要给用户，确认无误后再 commit。

**提交信息规范**（Conventional Commits）：

格式：`<英文type>(<scope>): <中文或英文描述>`

```
<type>(<scope>): <subject>

[可选 body]

[可选 footer]
```

| type | 说明 |
|------|------|
| `feat` | 新功能 |
| `fix` | 修复 bug |
| `refactor` | 重构（不改变功能） |
| `docs` | 文档更新 |
| `test` | 测试相关 |
| `chore` | 构建/工具/依赖 |
| `style` | 代码格式（不影响逻辑） |
| `perf` | 性能优化 |

**描述语言**：type 和 scope 必须用英文，描述可以用中文或英文：
- 中文描述：`feat(auth): 添加登录超时重试机制`
- 英文描述：`feat(auth): add login timeout retry mechanism`
- 混用也行：`fix(api): 修复 timeout error on slow network`

**示例**：

```
feat(health-check): 新增 Phase 9 Profile 健康检查

Add Phase 9: Profile Health Check to hermes-health-check skill.
Checks disk usage, broken skills, and gateway status per profile.

Closes #42
```

```
fix(gateway): 修复审批卡片点击无响应

The _allow_group_message check was missing default_group_policy
for DM chats, causing all approval clicks to be rejected.

Fix: set platforms.feishu.extra.default_group_policy = open
```

```
chore(deps): bump hermes-health-check to v2.2.1

Update watchdog scripts to output structured JSON.
```

### Phase 6: 推送、创建 PR / issue

**署名约定（必须）**：

**为什么署名**：创建 PR / issue、回复 issue、写评论用的都是**用户本人的 MCP token**（GitHub/Gitee 账号），对外内容会以**用户身份**显示。必须在 body 底部标注"由哪个 Agent 创建/回复"，避免读者误以为作者本人手动提交。

**署名名称 `AGENT_NAME`（只显示 Agent 名称本身）**：
- 取值优先级：环境变量 `AGENT_NAME` > 当前运行 agent 的类型名 > 询问 Agent 自己是谁（兜底）。
- 兜底时**不要写死某个预设名称**，而是让 Agent 根据当前运行环境声明自己的身份——如按当前 profile / 实例 / 宿主标识给出可区分的名称（例：`Hermes Agent（jzyq-pdj）`）。连身份都无从确认时，才退为通用的匿名身份 `Nameless Agent`。
- 格式为 Agent 名称，不附加其他身份描述。例如：`Hermes Agent（jzyq-pdj）`、`Claude Code`、`Codex`。
- 当前 Hermes 已通过 `AGENT_NAME` 环境变量设为 `Hermes Agent（jzyq-pdj）`。

**署名格式（body 末尾固定追加，英文）**：
- 创建 PR：`Created by **<AGENT_NAME>**`
- 创建 issue：`Created by **<AGENT_NAME>**`
- 回复 issue / 评论：`Replied by **<AGENT_NAME>**`
- 回复 PR 审查评论（review comment / 行级 inline 评论 / 回复某条审查意见）：`Replied by **<AGENT_NAME>**`
- 发表 PR 审查汇总 / 审查报告（create_pull_request_review, event=COMMENT 等）：`Reviewed by **<AGENT_NAME>**`

示例（body 末尾追加一段）：
```
<正文内容>

---
Created by **<AGENT_NAME>**
```
其中 `<AGENT_NAME>` 替换为实际值，不要带硬编码前缀。

**issue 使用场景**：创建 issue 时 body 末尾追加 `Created by **<AGENT_NAME>**`；回复 issue 时追加 `Replied by **<AGENT_NAME>**`。映射到 MCP 工具：`mcp__gitee__gitee_create_issue` / `mcp__gitee__gitee_create_issue_comment`（Gitee），`mcp__github__create_issue` / `mcp__github__add_issue_comment`（GitHub）。

**PR 审查评论使用场景（重点，勿因工具不同而跳过）**：回复 PR 上的审查评论 / 行级 inline 评论 / 回复某条审查意见时，body 末尾追加 `Replied by **<AGENT_NAME>**`；发表 PR 审查汇总 / 审查报告时追加 `Reviewed by **<AGENT_NAME>**`。映射到 MCP 工具：`mcp__gitee__gitee_create_pull_comment`（Gitee，带 path/position 做行级定位，不带则写汇总）、`mcp__github__create_pull_request_review`（GitHub，event=COMMENT，body 放汇总 + comments 数组挂行级 inline）；回复单条 review 评论用对应平台的 reply 工具。工具名不写死，以当前环境实际可用者为准；Hermes 优先 `mcp__*`，Claude Code / Codex 等其它 agent 用其自身可用的等价写回工具，并同样在末尾署名。

#### 方式 A：有 GitHub MCP

优先使用 MCP 工具创建 PR：

```python
# GitHub MCP（如已配置）
mcp__github__create_pull_request(
    owner="<owner>",
    repo="<repo>",
    title="<PR标题>",
    head="<分支名>",
    base="main",
    body="""<PR描述>

---
Created by **<AGENT_NAME>**
"""
)
```

#### 方式 B：无 MCP，使用 HTTPS + Token

检查远程仓库 URL 格式：

```bash
git remote -v
```

**若 URL 已包含 Token**（格式：`https://用户名:私人令牌@gitee.com/用户名/仓库名.git`）：

```bash
# 直接推送
git push origin <branch-name>
```

然后通过 API 创建 PR：

```bash
# Gitee API
curl -X POST "https://gitee.com/api/v5/repos/<owner>/<repo>/pulls" \
  -H "Content-Type: application/json" \
  -d '{
    "access_token": "<token>",
    "title": "<PR标题>",
    "head": "<分支名>",
    "base": "main",
    "body": "<PR描述>\n\n---\nCreated by **<AGENT_NAME>**"
  }'
```

**若 URL 不含 Token**：

1. 检查是否有全局 credential helper 存储了 token
2. 若都没有，向用户索要 Gitee/GitHub 私人令牌
3. 获取 token 后，更新远程 URL 并推送

```bash
# 更新远程 URL 加入 token
git remote set-url origin https://<username>:<token>@gitee.com/<username>/<repo>.git

# 推送
git push origin <branch-name>
```

#### 方式 C：无法自动创建 PR

推送代码后，生成 PR 链接供用户手动创建：

```
请手动创建 PR：
Gitee: https://gitee.com/<owner>/<repo>/pulls/new?source=<branch>&target=main
GitHub: https://github.com/<owner>/<repo>/compare/main...<branch>
```

### Phase 7: 通知用户

完成后输出汇总：

```
Git 协作流程完成

分支: feat/xxx → main
提交: feat(scope): description
推送: 已推送到远程
PR: <PR链接或手动创建指引>

请前往 Gitee/GitHub 审核代码。
```

**重要**：PR 创建后提醒用户去平台审核，**不要自行合并**。

### Phase 8: 清理（PR 合并后）

PR 被合并后，清理本地和远程的功能分支：

```bash
# 删除本地功能分支
git checkout main
git branch -d <branch-name>

# 删除远程功能分支
git push origin --delete <branch-name>
```

**注意**：仅在用户确认 PR 已合并后执行此步骤。

## 注意事项

- **永远不要在主分支上直接修改代码** — 必须创建功能分支
- **永远不要跳过代码检查** — lint/format 失败不能提交
- **永远不要自行合并 PR** — 由用户 review 后决定
- **commit 前确认变更范围** — 用 `git diff --staged` 检查暂存内容
- **敏感信息不能提交** — 检查是否有 API key、密码等被意外包含
- **大文件不能提交** — 检查 `git diff --stat` 中是否有 >1MB 的文件

## Pitfalls

### Token 相关
- Gitee 私人令牌需要 `projects` 权限 scope
- GitHub Personal Access Token 需要 `repo` scope
- Token 有效期有限，过期后需要重新生成
- 不要在日志或输出中暴露完整 token，用 `sk-8...xxxx` 格式截断

### 分支相关
- 若功能分支已有远程同名分支，`git push` 需要加 `--force-with-lease`（谨慎使用）
- PR 创建前确保分支已推送到远程
- 合并冲突时不要强行 push，先解决冲突再提交

### 检查相关
- `ruff check` 和 `ruff format` 可能有不同的配置（`pyproject.toml`、`ruff.toml`）
- `npm run lint` 可能包含 `--max-warnings 0`，导致 warning 也被视为失败
- 混合项目需要分别在子目录中执行检查
- 有 `.pre-commit-config.yaml` 的项目优先用 `pre-commit run --all-files`

### 提交相关
- `git add .` 可能添加不需要的文件，优先用 `git add <具体文件>`
- 多个无关修改不要放在一个 commit 里，应该分开提交
- commit message 的 type 和 scope 必须用英文，描述可以用中文或英文
