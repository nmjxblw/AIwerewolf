"""跨平台系统物理内存守卫；仅使用 Python 标准库。"""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass

GIB = 1024**3


@dataclass(frozen=True)
class MemorySnapshot:
    total_bytes: int
    available_bytes: int

    @property
    def available_ratio(self) -> float:
        """返回当前可用物理内存占总物理内存的比例。"""

        if self.total_bytes <= 0:
            return 1.0
        return self.available_bytes / self.total_bytes


def _windows_memory_snapshot() -> MemorySnapshot | None:
    """通过 Windows 内核 API 读取物理内存，不引入第三方原生依赖。"""

    class MemoryStatusEx(ctypes.Structure):
        _fields_ = (
            ("length", ctypes.c_ulong),
            ("memory_load", ctypes.c_ulong),
            ("total_physical", ctypes.c_ulonglong),
            ("available_physical", ctypes.c_ulonglong),
            ("total_page_file", ctypes.c_ulonglong),
            ("available_page_file", ctypes.c_ulonglong),
            ("total_virtual", ctypes.c_ulonglong),
            ("available_virtual", ctypes.c_ulonglong),
            ("available_extended_virtual", ctypes.c_ulonglong),
        )

    status = MemoryStatusEx()
    status.length = ctypes.sizeof(status)
    try:
        succeeded = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    except (AttributeError, OSError):
        return None
    if not succeeded:
        return None
    return MemorySnapshot(
        total_bytes=int(status.total_physical),
        available_bytes=int(status.available_physical),
    )


def _posix_memory_snapshot() -> MemorySnapshot | None:
    """通过 POSIX sysconf 读取物理内存页统计。"""

    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        total_pages = int(os.sysconf("SC_PHYS_PAGES"))
        available_pages = int(os.sysconf("SC_AVPHYS_PAGES"))
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    return MemorySnapshot(
        total_bytes=page_size * total_pages,
        available_bytes=page_size * available_pages,
    )


def system_memory_snapshot() -> MemorySnapshot | None:
    """返回系统物理内存快照；平台不支持时返回 ``None``。"""

    if os.name == "nt":
        return _windows_memory_snapshot()
    return _posix_memory_snapshot()


def memory_pressure_snapshot(
    *,
    reserve_ratio: float,
    reserve_gib: float,
) -> tuple[MemorySnapshot, int] | None:
    """判断系统是否进入内存安全保留区。

    参数：
        reserve_ratio: 总物理内存保留比例，自动限制在 ``[0, 1]``。
        reserve_gib: 至少保留的可用物理内存容量，单位为 GiB。

    返回：
        触发保护时返回内存快照和最终阈值字节数，否则返回 ``None``。
    """

    snapshot = system_memory_snapshot()
    if snapshot is None:
        return None
    ratio = max(0.0, min(1.0, float(reserve_ratio)))
    reserve_bytes = max(0, int(float(reserve_gib) * GIB))
    threshold = max(reserve_bytes, int(snapshot.total_bytes * ratio))
    if snapshot.available_bytes >= threshold:
        return None
    return snapshot, threshold
