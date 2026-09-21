# -*- coding: utf-8 -*-
"""ima 桌面客户端入口。"""
import sys
import os

# 保证同目录模块可被导入
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _install_debug_log():
    """冻结态（无控制台）时，把异常/输出重定向到 exe 同级 debug.log。"""
    if not getattr(sys, "frozen", False):
        return
    try:
        log_path = os.path.join(os.path.dirname(os.path.abspath(sys.executable)),
                                "debug.log")
        f = open(log_path, "a", encoding="utf-8", buffering=1)
        sys.stderr = f
        sys.stdout = f
    except Exception:
        pass


_install_debug_log()

from ui_main import main

if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        raise
