"""全局崩溃 / 致命错误处理器。

把未捕获异常与 C 级致命错误（Py_FatalError / 段错误等）的 trace 落到文件，
方便事后 debug。进程入口调用一次 ``install_crash_handlers()`` 即可：

- ``sys.excepthook``：主线程未捕获异常
- ``threading.excepthook``：线程内未捕获异常
- ``sys.unraisablehook``：不可引发的异常（如 ``__del__`` 中抛出）
- ``faulthandler``：C 级致命错误的线程栈转储（如 CPython 解释器
  ``_PyEval_EvalFrameDefault: Executing a cache`` 这类非 Python 异常）

日志文件路径可用环境变量 ``SEARCH_SIMULATOR_CRASH_LOG`` 覆盖，默认
``search_simulator_crash.log``（已被 ``.gitignore`` 的 ``*.log`` 规则忽略）。
"""

from __future__ import annotations

import datetime
import faulthandler
import os
import sys
import threading
import traceback
from pathlib import Path

_handle = None

# CPython 3.14.0 / 3.14.1 存在多个崩溃回归（随机 Windows access violation），
# 已在 3.14.2 修复；见 Python 3.14.2 release notes。
_BUGGY_PY_VERSIONS = {(3, 14, 0), (3, 14, 1)}


def _log_path() -> Path:
    return Path(
        os.environ.get("SEARCH_SIMULATOR_CRASH_LOG", "search_simulator_crash.log")
    )


def _warn_if_buggy_python() -> None:
    """在 CPython 3.14.0/3.14.1 上打印升级警告（这两个版本有崩溃回归）。"""
    version = sys.version_info[:3]
    if version not in _BUGGY_PY_VERSIONS:
        return
    version_text = ".".join(str(part) for part in version)
    warning = (
        f"[警告] 当前 Python {version_text} 存在崩溃回归（随机 Windows access "
        f"violation，如 copy/json/sqlalchemy/流网络处的段错误），建议升级到 "
        f"Python 3.14.2+ 或改用 3.12/3.13。"
    )
    print(warning, file=sys.stderr)


def install_crash_handlers() -> Path:
    """安装全局崩溃处理器并返回日志文件路径（幂等）。"""
    global _handle
    _warn_if_buggy_python()
    if _handle is not None and not _handle.closed:
        return _log_path()

    path = _log_path()
    _handle = open(path, "a", encoding="utf-8")

    def _header() -> None:
        _handle.write("\n" + "=" * 72 + "\n")
        _handle.write(f"[{datetime.datetime.now().isoformat()}] pid={os.getpid()}\n")

    def _excepthook(exc_type, exc_value, exc_tb) -> None:
        _header()
        _handle.write("Uncaught exception (sys.excepthook):\n")
        traceback.print_exception(exc_type, exc_value, exc_tb, file=_handle)
        _handle.flush()
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    def _thread_excepthook(args) -> None:
        _header()
        _handle.write("Uncaught exception in thread (threading.excepthook):\n")
        traceback.print_exception(
            args.exc_type, args.exc_value, args.exc_traceback, file=_handle
        )
        _handle.flush()

    def _unraisablehook(args) -> None:
        _header()
        _handle.write("Unraisable exception (sys.unraisablehook):\n")
        traceback.print_exception(
            args.exc_type, args.exc_value, args.exc_traceback, file=_handle
        )
        _handle.flush()

    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    sys.unraisablehook = _unraisablehook
    faulthandler.enable(_handle)
    return path
