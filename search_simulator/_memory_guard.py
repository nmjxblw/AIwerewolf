"""跨平台系统物理内存守卫；仅使用 Python 标准库。"""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass

GIB = 1024**3


class _WindowsMemoryStatusEx(ctypes.Structure):
    """与 Win32 ``MEMORYSTATUSEX`` ABI 严格对应的静态结构体。

    该类型必须在模块加载时只创建一次。历史实现把结构体类定义放在
    ``_windows_memory_snapshot`` 热路径内，并通过未声明函数签名的
    ``ctypes.windll`` 代理反复跨越 ABI；Windows CPython 3.12 长时间调用后
    会出现访问冲突。静态类型与显式签名共同保证指针布局不随调用变化。
    """

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


def _bind_windows_memory_status() -> object | None:
    """一次性绑定 ``GlobalMemoryStatusEx`` 并声明完整调用约定。

    非 Windows 平台不加载 ``kernel32``，从而保持模块可导入。返回对象在
    Windows 上是带 ``argtypes``/``restype`` 的 ``ctypes`` 函数代理。
    """

    if os.name != "nt":
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        function = kernel32.GlobalMemoryStatusEx
    except (AttributeError, OSError):
        return None
    function.argtypes = (ctypes.POINTER(_WindowsMemoryStatusEx),)
    function.restype = ctypes.c_int
    return function


_GLOBAL_MEMORY_STATUS_EX = _bind_windows_memory_status()


@dataclass(frozen=True, slots=True)
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
    """通过已声明 ABI 的 Windows 内核 API 读取物理内存。"""

    function = _GLOBAL_MEMORY_STATUS_EX
    if function is None:
        return None
    status = _WindowsMemoryStatusEx()
    status.length = ctypes.sizeof(status)
    try:
        succeeded = function(ctypes.byref(status))
    except (AttributeError, OSError, ctypes.ArgumentError):
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
