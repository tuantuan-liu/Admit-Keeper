#!/usr/bin/env bash
# ===== admit-keeper 安装脚本 =====
# 把仓库中的插件 / MCP 复制到 ~/.hermes 运行时布局，并补齐 mcp[cli]。
# 用法: bash scripts/install.sh [profile]     # profile 默认 default
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILE="${1:-default}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"

PLUGIN_SRC="$ROOT/admit_keeper"
PLUGIN_DST="$HERMES_HOME/plugins/admit-keeper"
PROFILE_DST="$HERMES_HOME/profiles/$PROFILE"

echo "== 安装插件到 $PLUGIN_DST =="
mkdir -p "$PLUGIN_DST"
cp "$PLUGIN_SRC"/plugin.yaml "$PLUGIN_SRC"/__init__.py "$PLUGIN_SRC"/gate.py "$PLUGIN_SRC"/db.py "$PLUGIN_SRC"/policy.py "$PLUGIN_DST"/

echo "== 安装 MCP 到 $PROFILE_DST =="
mkdir -p "$PROFILE_DST"
cp "$ROOT/mcp"/admit_keeper_mcp.py "$PROFILE_DST"/
# MCP 侧需要同源 db/policy（与 mcp_server.py 同目录 import）
cp "$PLUGIN_SRC"/db.py "$PLUGIN_SRC"/policy.py "$PROFILE_DST"/

echo "== 补全 mcp[cli]（钉 mcp<2：2.x 已移除 FastMCP，需 v1 API）=="
python3 -m pip install -U "mcp[cli]>=1.0,<2"

echo
echo "✔ 安装完成。接下来手动做两件事："
echo "  1) 编辑 $PROFILE_DST/.env    —— 参考 config/env.example（ADMIT_KEEPER_DB 等）"
echo "  2) 编辑 $PROFILE_DST/config.yaml —— 参考 config/config.example.yaml（plugins + mcp_servers）"
echo "  然后: hermes plugins enable admit-keeper"
echo "        hermes -p $PROFILE gateway restart"
