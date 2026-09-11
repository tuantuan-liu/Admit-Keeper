"""pytest 夹具：把仓库根、admit_keeper/ 与 mcp/ 目录加到 sys.path，使测试既可像运行时插件/MCP
一样以顶层模块方式导入（`from policy import decide`、`import admit_keeper_mcp`），也能以包方式
导入框架无关共享实现（`from admit_keeper.gate import gate`）。"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _dir in (_ROOT, os.path.join(_ROOT, "admit_keeper"), os.path.join(_ROOT, "mcp")):
    _p = os.path.join(_ROOT, _dir)
    if _p not in sys.path:
        sys.path.insert(0, _p)
