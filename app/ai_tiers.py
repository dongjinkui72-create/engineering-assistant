"""Local vision-model tiers selected by installed RAM.

The GUI ships the Ollama runtime and the *code* that talks to it, but not
the multi-gigabyte model weights.  This module is the single source of
truth for "which model should this machine download/run?".
"""
from __future__ import annotations

import ctypes
import os
import sys
from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class ModelTier:
    min_ram_gb: int
    model: str
    download_gb: float
    label: str
    note: str


# Ordered from largest to smallest.  Keep the conservative thresholds: the
# CAD application (NX / SolidWorks) has to stay open at the same time.
TIERS: tuple[ModelTier, ...] = (
    ModelTier(95, "qwen2.5vl:72b", 49.0, "96 GB 及以上",
              "高精度大模型；模型文件约 49 GB，加载后内存占用更高。"),
    ModelTier(47, "qwen2.5vl:32b", 21.0, "48–95 GB",
              "复杂图纸档；不建议 32 GB 机器默认使用，叠加 CAD 后容易吃紧。"),
    ModelTier(15, "qwen2.5vl:7b", 6.0, "16–47 GB",
              "当前实机重点验证档；NX / SolidWorks 同时运行较稳妥。"),
    ModelTier(8, "qwen2.5vl:3b", 3.2, "8–15 GB",
              "基础识图档；复杂三视图、剖视图准确率有限，建议人工复核。"),
)

MIN_RAM_GB = 8


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def total_memory_gb() -> Optional[float]:
    """Installed physical RAM in GB, or None if it cannot be detected."""
    if os.name == "nt":
        try:
            st = _MemoryStatusEx()
            st.dwLength = ctypes.sizeof(_MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return round(st.ullTotalPhys / (1024 ** 3), 1)
        except Exception:
            pass
    try:
        if hasattr(os, "sysconf"):
            pages = os.sysconf("SC_PHYS_PAGES")
            size = os.sysconf("SC_PAGE_SIZE")
            return round(pages * size / (1024 ** 3), 1)
    except Exception:
        pass
    return None


def tier_for_ram(ram_gb: Optional[float]) -> ModelTier:
    if ram_gb is None:
        # Unknown hardware: stay on the validated, middle-of-the-road model.
        return next(t for t in TIERS if t.model == "qwen2.5vl:7b")
    for t in TIERS:
        if ram_gb >= t.min_ram_gb:
            return t
    return TIERS[-1]


def recommended_model(ram_gb: Optional[float] = None) -> str:
    if ram_gb is None:
        ram_gb = total_memory_gb()
    return tier_for_ram(ram_gb).model


def describe(ram_gb: Optional[float] = None) -> dict:
    if ram_gb is None:
        ram_gb = total_memory_gb()
    tier = tier_for_ram(ram_gb)
    return {
        "ram_gb": ram_gb,
        "min_ram_gb": MIN_RAM_GB,
        "low_memory": bool(ram_gb is not None and ram_gb < MIN_RAM_GB),
        "model": tier.model,
        "download_gb": tier.download_gb,
        "label": tier.label,
        "note": tier.note,
        "tiers": [
            {"min_ram_gb": t.min_ram_gb, "model": t.model,
             "download_gb": t.download_gb, "label": t.label, "note": t.note}
            for t in TIERS
        ],
    }


def format_table() -> str:
    lines: List[str] = []
    for t in TIERS:
        lines.append(f"{t.label:>10}  {t.model:<18} 约 {t.download_gb:>4.1f} GB")
    return "\n".join(lines)


if __name__ == "__main__":
    print(format_table())
    print()
    print("本机内存：", total_memory_gb(), "GB")
    print("推荐模型：", recommended_model())
