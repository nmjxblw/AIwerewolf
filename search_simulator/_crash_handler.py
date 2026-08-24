"""全局崩溃 / 致命错误处理器。

把未捕获异常与 C 级致命错误（Py_FatalError / 段错误等）的 trace 落到文件，
方便事后 debug。进程入口调用一次 ``install_crash_handlers()`` 即可：

- ``sys.excepthook``：主线程未捕获异常
- ``threading.excepthook``：线程内未捕获异常
- ``sys.unraisablehook``：不可引发的异常（如 ``__del__`` 中抛出）
- ``faulthandler``：C 级致命错误的线程栈转储（如 CPython 解释器
  ``_PyEval_EvalFrameDefault: Executing a cache`` 这类非 Python 异常）

程序启动时在模块根目录创建 ``crash_log/``，并为本次启动生成只含一个
高精度时间戳的 ``crash_YYYYMMDD_HHMMSS_ffffff.log``。父进程通过环境变量把同一路径传给
派生 worker，因此一次运行只对应一个 crash 文件，不与历史错误混写。
"""

from __future__ import annotations

import datetime
import faulthandler
import logging
import os
import sys
import threading
import traceback
from pathlib import Path
from typing import Any
from typing import Mapping

_handle = None
_CRASH_LOG_ENV = "SEARCH_SIMULATOR_CRASH_LOG"
_CRASH_SESSION_ENV = "SEARCH_SIMULATOR_CRASH_SESSION"
_NOTIFIED_MARKER = ".last_notified.log"

# CPython 3.14.0 / 3.14.1 存在多个崩溃回归（随机 Windows access violation），
# 已在 3.14.2 修复；见 Python 3.14.2 release notes。
_BUGGY_PY_VERSIONS = {(3, 14, 0), (3, 14, 1)}


def crash_log_directory() -> Path:
    """返回并创建模块根目录下的 crash 日志文件夹。"""

    directory = Path(__file__).resolve().parent / "crash_log"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def prepare_crash_log_path() -> Path:
    """为本次启动准备唯一 crash 日志路径并写入进程环境。

    已存在环境变量时说明当前进程是派生 worker，直接复用父进程路径。
    """

    inherited = os.environ.get(_CRASH_LOG_ENV)
    inherited_session = os.environ.get(_CRASH_SESSION_ENV)
    if inherited and inherited_session:
        path = Path(inherited).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = crash_log_directory() / f"crash_{timestamp}.log"
    os.environ[_CRASH_LOG_ENV] = str(path)
    os.environ[_CRASH_SESSION_ENV] = timestamp
    return path


def _log_path() -> Path:
    return prepare_crash_log_path()


def crash_log_path() -> Path:
    """返回 C 级致命错误日志的绝对路径。"""

    return _log_path()


def previous_unreported_crash_log() -> Path | None:
    """返回最近一个非空且尚未向 GUI 提示的历史 crash 日志。"""

    current = crash_log_path()
    directory = crash_log_directory()
    candidates = sorted(
        (
            path
            for path in directory.glob("crash_*.log")
            if path.resolve() != current.resolve()
            and path.is_file()
            and path.stat().st_size > 0
        ),
        key=lambda path: path.name,
        reverse=True,
    )
    if not candidates:
        return None
    marker = directory / _NOTIFIED_MARKER
    try:
        notified = marker.read_text(encoding="utf-8").strip()
    except OSError:
        notified = ""
    latest = candidates[0].resolve()
    if notified == str(latest):
        return None
    return latest


def mark_crash_log_reported(path: Path) -> None:
    """记录已经向用户展示过的历史 crash 日志。"""

    marker = crash_log_directory() / _NOTIFIED_MARKER
    marker.write_text(str(path.resolve()), encoding="utf-8")


def caught_failure_is_recorded(exc: BaseException) -> bool:
    """返回该异常是否已经由可捕获失败路径写入本次 crash 文件。"""

    return bool(getattr(exc, "_search_simulator_crash_recorded", False))


def record_caught_failure(
    exc: BaseException,
    *,
    category: str,
    context: Mapping[str, Any] | None = None,
) -> Path:
    """把已被业务层捕获的失败追加到本次 crash 日志。

    全局异常钩子只会收到未捕获异常；进程池会把 worker 的 Python 异常
    序列化给父进程，GUI 后台线程也会主动捕获异常。两种路径都必须显式
    调用本函数，否则运行会被标记为 ``failed``，crash 文件却保持为空。

    参数：
        exc: 导致运行失败的原始异常，保留远端异常链和 traceback。
        category: 失败分类，例如 ``python_exception`` 或 ``worker_crash``。
        context: 运行标识、检查点和下一站位等结构化上下文。

    返回：
        本次运行共用的时间戳 crash 日志绝对路径。
    """

    path = _log_path()
    if caught_failure_is_recorded(exc):
        return path

    values = dict(context or {})
    context_text = " ".join(
        f"{str(key)}={str(value)}" for key, value in values.items()
    )
    try:
        exception_text = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
    except BaseException:
        exception_text = f"{type(exc).__name__}: {exc}\n"
    record = (
        "\n"
        + "=" * 72
        + "\n"
        + f"[{datetime.datetime.now().isoformat()}] pid={os.getpid()} "
        + f"thread={threading.current_thread().name}\n"
        + f"Caught failure category={category}"
        + (f" {context_text}" if context_text else "")
        + "\n"
        + exception_text
    )
    try:
        # 每次使用追加模式单次写入完整记录。父进程和 worker 即使共享同一
        # 路径，也不会依赖彼此的 Python 文件句柄或缓冲区状态。
        with path.open("a", encoding="utf-8") as failure_handle:
            failure_handle.write(record)
            failure_handle.flush()
        try:
            exc._search_simulator_crash_recorded = True
        except (AttributeError, TypeError):
            pass
    except BaseException:
        logging.getLogger(__name__).critical(
            "CRASH_LOG_WRITE_FAILED pid=%s crash_log=%s original_error_type=%s",
            os.getpid(),
            path,
            type(exc).__name__,
            exc_info=True,
        )
    return path


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
        logging.getLogger(__name__).critical(
            "UNCAUGHT_EXCEPTION pid=%s",
            os.getpid(),
            exc_info=(exc_type, exc_value, exc_tb),
        )
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    def _thread_excepthook(args) -> None:
        _header()
        _handle.write("Uncaught exception in thread (threading.excepthook):\n")
        traceback.print_exception(
            args.exc_type, args.exc_value, args.exc_traceback, file=_handle
        )
        _handle.flush()
        logging.getLogger(__name__).critical(
            "THREAD_CRASH pid=%s thread=%s",
            os.getpid(),
            getattr(args.thread, "name", "unknown"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    def _unraisablehook(args) -> None:
        _header()
        _handle.write("Unraisable exception (sys.unraisablehook):\n")
        traceback.print_exception(
            args.exc_type, args.exc_value, args.exc_traceback, file=_handle
        )
        _handle.flush()
        logging.getLogger(__name__).error(
            "UNRAISABLE_EXCEPTION pid=%s object=%r",
            os.getpid(),
            args.object,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    sys.unraisablehook = _unraisablehook
    faulthandler.enable(_handle)
    return path
