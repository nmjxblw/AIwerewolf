"""Search Simulator 控制台与 UTF-8 文件日志配置。"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

LOG_FORMAT = "[%(asctime)s][%(pathname)s:%(lineno)d][%(levelname)s]" + os.linesep + "%(message)s" + os.linesep


def runtime_log_path() -> Path:
    """返回运行日志绝对路径。

    环境变量 ``SEARCH_SIMULATOR_LOG`` 可覆盖默认的
    ``search_simulator.log``。相对路径以当前工作目录为基准。
    """

    configured = os.environ.get("SEARCH_SIMULATOR_LOG", "search_simulator.log")
    return Path(configured).expanduser().resolve()


def configure_runtime_logging(*, level: int = logging.INFO) -> Path:
    """配置根 logger，同时写入控制台和 UTF-8 文件。

    参数：
        level: 控制台与文件共同使用的最低日志级别。

    返回：
        当前运行日志的绝对路径。
    """

    path = runtime_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # 子进程继承绝对路径，避免其工作目录变化后写入另一份运行日志。
    os.environ["SEARCH_SIMULATOR_LOG"] = str(path)
    formatter = logging.Formatter(LOG_FORMAT)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    file_handler = logging.FileHandler(path, mode="a", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logging.basicConfig(
        level=level,
        handlers=(console, file_handler),
        force=True,
    )
    return path
