"""CAD 版本探测与兼容层。

不写死安装路径/版本号：从注册表、环境变量、常见目录里找，
并把"最低支持版本"与"实际版本"比对，供界面显示和适配器选择调用方式。
"""
from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

MIN_VERSIONS = {"NX": "12.0", "SolidWorks": "2022", "CATIA": "R21"}


@dataclass
class CadInfo:
    key: str
    name: str
    version: str = ""
    numeric: float = 0.0
    path: str = ""
    template: str = ""
    extra: dict = field(default_factory=dict)
    found: bool = False
    notes: str = ""

    @property
    def meets_min(self) -> bool:
        need = {"NX": 12.0, "SolidWorks": 2022.0, "CATIA": 21.0}[self.key]
        return self.found and self.numeric >= need

    def line(self) -> str:
        if not self.found:
            return f"{self.name}：未检测到"
        flag = "✅ 满足最低要求" if self.meets_min else f"⚠️ 低于最低要求 {MIN_VERSIONS[self.key]}"
        return f"{self.name} {self.version} · {flag} · {self.path}"


def _reg(path: str) -> dict:
    try:
        import winreg
    except Exception:
        return {}
    try:
        h = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path)
    except Exception:
        return {}
    out = {}
    try:
        i = 0
        while True:
            try:
                n, v, _ = winreg.EnumValue(h, i)
                out[n] = v
                i += 1
            except OSError:
                break
    finally:
        winreg.CloseKey(h)
    return out


def _reg_subkeys(path: str) -> List[str]:
    try:
        import winreg
    except Exception:
        return []
    out = []
    try:
        h = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path)
    except Exception:
        return []
    try:
        i = 0
        while True:
            try:
                out.append(winreg.EnumKey(h, i))
                i += 1
            except OSError:
                break
    finally:
        winreg.CloseKey(h)
    return out


# --------------------------------------------------------------------- NX
def detect_nx() -> CadInfo:
    info = CadInfo("NX", "UG/NX")
    cands: List[tuple] = []
    # 1) 卸载表（NX12 及经典版本）
    try:
        import winreg
        base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
        for sub in _reg_subkeys(base):
            d = _reg(base + "\\" + sub)
            name = str(d.get("DisplayName") or "")
            if "NX" in name and ("Siemens" in name or "UG" in name.upper()):
                loc = str(d.get("InstallLocation") or "")
                ver = str(d.get("DisplayVersion") or "")
                if loc:
                    cands.append((loc, ver))
    except Exception:
        pass
    # 2) 环境变量
    env = os.environ.get("UGII_ROOT_DIR") or os.environ.get("UGII_BASE_DIR")
    if env:
        cands.append((env, ""))
    # 3) 常见目录（含 NX1847+ 连续版本）
    for pat in (r"D:\ug\NX *", r"C:\Program Files\Siemens\NX *",
                r"D:\Program Files\Siemens\NX *", r"E:\Siemens\NX *"):
        for p in glob.glob(pat):
            cands.append((p, ""))

    best = None
    for loc, ver in cands:
        rj = Path(loc) / "NXBIN" / "run_journal.exe"
        if not rj.exists():
            continue
        m = re.search(r"NX\s*([0-9.]+)", str(loc)) or re.search(r"([0-9]{2,4})", ver)
        num = 0.0
        if m:
            try:
                num = float(m.group(1).split(".")[0])
            except Exception:
                num = 0.0
        if best is None or num > best[1]:
            best = (loc, num, ver)
    if best:
        loc, num, ver = best
        info.found = True
        info.path = loc
        info.numeric = num
        info.version = f"NX {int(num)}" if num else (ver or "NX")
        if num >= 1847:
            info.notes = "NX 连续发布版（1847+），API 与 NX12 基本一致"
    return info


# ------------------------------------------------------------- SolidWorks
def detect_sw() -> CadInfo:
    info = CadInfo("SolidWorks", "SolidWorks")
    best = None
    for key in _reg_subkeys(r"SOFTWARE\SolidWorks"):
        m = re.match(r"SOLIDWORKS\s+(\d{4})", key)
        if not m:
            continue
        year = int(m.group(1))
        d = _reg(f"SOFTWARE\\SolidWorks\\{key}\\Setup")
        folder = str(d.get("SolidWorks Folder") or "")
        if not folder:
            continue
        if best is None or year > best[0]:
            best = (year, folder)
    if best:
        year, folder = best
        info.found = True
        info.path = folder
        info.version = f"SOLIDWORKS {year}"
        info.numeric = float(year)
        # 模板：优先同版本，其次最新的
        tpl = None
        for pat in (rf"C:\ProgramData\SolidWorks\SOLIDWORKS {year}\templates\*.prtdot",
                    r"C:\ProgramData\SolidWorks\SOLIDWORKS *\templates\*.prtdot"):
            files = sorted(glob.glob(pat))
            if files:
                zh = [f for f in files if "零件" in f] or files
                tpl = zh[0]
                break
        info.template = tpl or ""
        if not tpl:
            info.notes = "未找到零件模板（.prtdot），可在设置里手工指定"
    return info


# ------------------------------------------------------------------ CATIA
def detect_catia() -> CadInfo:
    info = CadInfo("CATIA", "CATIA V5")
    best = None
    for k in _reg_subkeys(r"SOFTWARE\Dassault Systemes"):
        m = re.match(r"B(\d+)$", k)
        if not m:
            continue
        rel = int(m.group(1))
        d = _reg(f"SOFTWARE\\Dassault Systemes\\{k}\\0")
        dest = str(d.get("DEST_FOLDER") or "")
        if not dest:
            continue
        if best is None or rel > best[0]:
            best = (rel, k, dest)
    if best:
        rel, k, dest = best
        start = Path(dest) / "intel_a" / "code" / "bin" / "CATSTART.exe"
        info.found = start.exists()
        info.path = dest
        info.numeric = float(rel)
        info.version = f"CATIA V5 R{rel}"
        info.extra["catstart"] = str(start)
        envdir = Path(r"C:\ProgramData\DassaultSystemes\CATEnv")
        envname = ""
        if envdir.exists():
            files = sorted(envdir.glob(f"CATIA_P3.V5R{rel}.B{rel}.txt"))
            if files:
                envname = files[0].stem
        info.extra["env_name"] = envname
        info.extra["env_dir"] = str(envdir)
    return info


def detect_all() -> Dict[str, CadInfo]:
    return {"NX": detect_nx(), "SolidWorks": detect_sw(), "CATIA": detect_catia()}


def apply_to_settings(s) -> Dict[str, CadInfo]:
    """把探测结果写回配置（只在原配置不可用时覆盖）。"""
    info = detect_all()
    nx = info["NX"]
    if nx.found and not (Path(s.nx_root) / "NXBIN" / "run_journal.exe").exists():
        s.nx_root = nx.path
    sw = info["SolidWorks"]
    if sw.found and not Path(s.sw_template).exists() and sw.template:
        s.sw_template = sw.template
    ct = info["CATIA"]
    if ct.found:
        if not Path(s.catia_exe).exists():
            s.catia_exe = ct.extra.get("catstart", s.catia_exe)
        if not getattr(s, "catia_env_name", ""):
            s.catia_env_name = ct.extra.get("env_name", "")
        if not getattr(s, "catia_env_dir", ""):
            s.catia_env_dir = ct.extra.get("env_dir", "")
    return info


def report_text() -> str:
    info = detect_all()
    lines = ["CAD 兼容性自检", ""]
    for k in ("NX", "SolidWorks", "CATIA"):
        i = info[k]
        lines.append(i.line())
        if i.template:
            lines.append(f"  模板：{i.template}")
        if i.notes:
            lines.append(f"  备注：{i.notes}")
        lines.append("")
    lines.append("最低要求：NX 12.0+ / SOLIDWORKS 2022+ / CATIA V5 R21+")
    lines.append("说明：调用方式按版本自动选择（参数个数、模板路径、启动参数均自适应）")
    return "\n".join(lines)