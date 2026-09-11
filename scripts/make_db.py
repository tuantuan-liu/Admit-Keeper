#!/usr/bin/env python3
"""手动建库 / 迁移辅助脚本。

用法:
    python3 scripts/make_db.py [db_path]

不传路径则用默认 ADMIT_KEEPER_DB / ~/.hermes/admit_keeper.db。
可单独用来预先建表（幂等），不依赖 MCP 首次调用。
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG = os.path.join(os.path.dirname(_HERE), "admit_keeper")
if _PKG not in sys.path:
    sys.path.insert(0, _PKG)

from db import db_path, open_init  # noqa: E402


def main() -> None:
    target = sys.argv[1] if len(sys.argv) > 1 else db_path()
    con = open_init(target)
    con.close()
    print(f"[完成] 建表就绪: {target}")


if __name__ == "__main__":
    main()
