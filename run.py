# -*- coding: utf-8 -*-
"""双击 / 命令行启动入口：用虚拟环境里的 Python 运行本文件即可。

启动过程中任何异常都会写进 logs/startup.log，
配合「启动工具.bat」可以在程序起不来时直接把日志弹出来。
"""

import os
import sys
import traceback

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

LOGS_DIR = os.path.join(ROOT, "logs")
LOG_PATH = os.path.join(LOGS_DIR, "startup.log")


def _reset_logs() -> None:
    """清掉上一次的日志与就绪标记，保证每次启动的日志都是干净的。"""
    os.makedirs(LOGS_DIR, exist_ok=True)
    for name in ("startup.log", "ui_ready.txt"):
        try:
            os.remove(os.path.join(LOGS_DIR, name))
        except OSError:
            pass


def _log_traceback() -> None:
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write("\n[启动失败] 未捕获的异常：\n")
            fh.write(traceback.format_exc())
    except Exception:
        pass


def main() -> int:
    from app.main import main as run_app

    return run_app()


if __name__ == "__main__":
    _reset_logs()
    try:
        code = main()
    except SystemExit:
        raise
    except BaseException:
        _log_traceback()
        sys.exit(1)
    sys.exit(code)
