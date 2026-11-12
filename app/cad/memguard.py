# -*- coding: utf-8 -*-
"""启动 CAD 前的内存体检。

为什么需要这个：NX / CATIA 启动要 1~2 GB 连续内存。系统可用内存接近 0 时，
run_journal.exe 不会报错，而是卡在初始化阶段（实测 240 s 后只能超时强杀），
nx_run.log 是 0 字节。这种失败没有任何可直接读的线索，极易被误判成
「几何有问题」「许可有问题」「NX 坏了」，从而浪费大量时间。

2026-11-09 实测记录（本机）：非分页池 12.9 GB（驱动泄漏），可用内存 0.0 MB，
NX 命令行与 GUI 均卡在启动（GUI 工作集被换页到 0.4 MB），许可服务正常、
lmstat 显示 500 张许可 0 占用 —— 说明与许可无关，纯粹是内存被打爆。

处置：可用内存不足时立刻返回带处置建议的失败，而不是先花 240 s 等超时。
环境变量 EA_ALLOW_LOW_MEM=1 可强制放行（用于确认是别的原因时）。
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


class _MEMSTAT(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class _PERFINFO(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("CommitTotal", ctypes.c_size_t),
        ("CommitLimit", ctypes.c_size_t),
        ("CommitPeak", ctypes.c_size_t),
        ("PhysicalTotal", ctypes.c_size_t),
        ("PhysicalAvailable", ctypes.c_size_t),
        ("SystemCache", ctypes.c_size_t),
        ("KernelTotal", ctypes.c_size_t),
        ("KernelPaged", ctypes.c_size_t),
        ("KernelNonpaged", ctypes.c_size_t),
        ("PageSize", ctypes.c_size_t),
        ("HandleCount", wintypes.DWORD),
        ("ProcessCount", wintypes.DWORD),
        ("ThreadCount", wintypes.DWORD),
    ]


def kernel_pool_mb():
    """返回 (nonpaged_mb, paged_mb)；取不到返回 (None, None)。

    非分页池是驱动占的、不可换出的内存。正常机器几百 MB。实测某台机器
    一晚上涨到 12.9 GB，直接把 16 GB 内存吃到只剩 0 MB —— 表现就是
    NX/CATIA 启动器单核 100% 空转、日志 0 字节、许可却一切正常。
    这种情况唯一的解法是重启（并找出泄漏的驱动），关程序没用。
    """
    try:
        pi = _PERFINFO()
        pi.cb = ctypes.sizeof(_PERFINFO)
        if not ctypes.windll.psapi.GetPerformanceInfo(ctypes.byref(pi), pi.cb):
            return None, None
        # 注意：GetPerformanceInfo 的这些字段单位是「页」，不是字节。
        # 直接用会得到 2.3「MB」这种荒谬值（真实是 2.3M 页 = 9.5 GB）。
        page = float(pi.PageSize or 4096)
        mb = 1024.0 * 1024.0
        return float(pi.KernelNonpaged) * page / mb, float(pi.KernelPaged) * page / mb
    except Exception:
        return None, None


def snapshot():
    """返回 {total_mb, avail_mb, commit_used_pct}；取不到返回 None。"""
    try:
        st = _MEMSTAT()
        st.dwLength = ctypes.sizeof(_MEMSTAT)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return None
        mb = 1024.0 * 1024.0
        total_pf = float(st.ullTotalPageFile)
        avail_pf = float(st.ullAvailPageFile)
        pct = 100.0 * (1.0 - avail_pf / total_pf) if total_pf > 0 else 0.0
        np_mb, p_mb = kernel_pool_mb()
        return {
            "total_mb": float(st.ullTotalPhys) / mb,
            "avail_mb": float(st.ullAvailPhys) / mb,
            "commit_used_pct": pct,
            "nonpaged_mb": np_mb,
            "paged_mb": p_mb,
        }
    except Exception:
        return None


def check(min_free_mb: float = 1200.0, max_commit_pct: float = 92.0,
          who: str = "CAD"):
    """返回 (ok, message)。message 为空串表示正常。"""
    s = snapshot()
    if not s:
        return True, ""
    force = str(os.environ.get("EA_ALLOW_LOW_MEM") or "").strip().lower()
    if force in ("1", "true", "yes", "on"):
        return True, ("⚠ 内存偏低（可用 %.0f MB，提交 %.0f%%），已按 EA_ALLOW_LOW_MEM 放行"
                      % (s["avail_mb"], s["commit_used_pct"]))
    np_mb = s.get("nonpaged_mb")
    pool_bad = bool(np_mb and s["total_mb"] and np_mb > 0.30 * s["total_mb"])
    if s["avail_mb"] >= min_free_mb and s["commit_used_pct"] <= max_commit_pct and not pool_bad:
        return True, ""
    if pool_bad:
        return False, (
            "内核非分页池已占 %.0f MB（总内存 %.0f MB 的 %.0f%%），这是驱动泄漏，"
            "不是本程序占的：可用内存只剩 %.0f MB，%s 会在启动阶段卡死、日志 0 字节。"
            "关程序无效，必须重启电脑；重启后先跑 CAD，别等池子再涨上来。"
            % (np_mb, s["total_mb"], 100.0 * np_mb / s["total_mb"],
               s["avail_mb"], who))
    return False, (
        "系统可用内存只有 %.0f MB（提交 %.0f%%），低于 %s 启动需要的 %.0f MB。"
        "这种状态下启动器会卡在初始化、只能超时强杀，日志为空——不是几何或许可问题。"
        "请先关闭其它程序；若可用内存仍接近 0，说明有驱动泄漏（非分页池异常大），需要重启电脑。"
        % (s["avail_mb"], s["commit_used_pct"], who, min_free_mb)
    )


def describe():
    s = snapshot()
    if not s:
        return "内存状态未知"
    np_mb = s.get("nonpaged_mb")
    pool = ("，内核非分页池 %.0f MB（%.0f%%）"
            % (np_mb, 100.0 * np_mb / s["total_mb"])) if np_mb and s.get("total_mb") else ""
    return "可用 %.0f MB / 总 %.0f MB（提交 %.0f%%）%s" % (
        s["avail_mb"], s["total_mb"], s["commit_used_pct"], pool)