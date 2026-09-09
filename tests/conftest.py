"""pytest 夹具：把 admit_keeper/ 与 mcp/ 目录加到 sys.path，使测试像运行时插件/MCP 一样
以顶层模块方式导入（`from policy import decide`、`import admit_keeper_mcp`）。"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _dir in ("admit_keeper", "mcp"):
    _p = os.path.join(_ROOT, _dir)
    if _p not in sys.path:
        sys.path.insert(0, _p)
