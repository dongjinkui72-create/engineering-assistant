"""运行时能力探测：不看版本号，直接查 API 是否存在。

新版本（NX 1847+/2306、SW 2025/2026、CATIA R22~R33）即使接口有增减，
也能在启动时探测出来，并按结果选择调用方式或给出明确提示。
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Dict, List

CHECK_SW = ["FeatureCut4", "FeatureCut3", "FeatureCut2", "FeatureExtrusion2",
            "InsertFeatureChamfer", "FeatureFillet3", "FeatureFillet2",
            "FeatureRevolve2", "InsertRefPlane", "InsertSheetMetalBaseFlange2",
            "AddComponent5", "SelectByRay"]
CHECK_CATIA = ["AddNewPad", "AddNewPocket", "AddNewShaft", "AddNewLoft",
               "AddNewChamfer", "AddNewEdgeFilletWithConstantRadius",
               "AddNewHole", "AddNewRib"]
CHECK_NX = ["CreateChamferBuilder", "CreateEdgeBlendBuilder", "CreateExtrudeBuilder",
            "CreateRevolveBuilder", "CreateThroughCurvesBuilder", "CreateBooleanBuilder",
            "CreateHoleFeatureBuilder", "CreatePatternFeatureBuilder"]


def probe_solidworks(path: str) -> Dict:
    out = {"cad": "SolidWorks", "ok": False, "caps": {}, "version": ""}
    tlb = Path(path) / "sldworks.tlb"
    if not tlb.exists():
        out["error"] = f"未找到 {tlb}"
        return out
    try:
        import winreg
        for k in ("SOLIDWORKS 2026", "SOLIDWORKS 2025", "SOLIDWORKS 2024",
                  "SOLIDWORKS 2023", "SOLIDWORKS 2022"):
            try:
                h = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\SolidWorks" + "\\" + k + r"\Setup")
                v, _ = winreg.QueryValueEx(h, "SolidWorks Folder")
                winreg.CloseKey(h)
                out["version"] = k.replace("SOLIDWORKS ", "SW ")
                break
            except Exception:
                continue
    except Exception:
        pass
    try:
        import pythoncom
        tl = pythoncom.LoadTypeLib(str(tlb))
        names = set()
        for i in range(tl.GetTypeInfoCount()):
            try:
                ti = tl.GetTypeInfo(i)
                ta = ti.GetTypeAttr()
                for j in range(ta.cFuncs):
                    names.add(ti.GetDocumentation(ti.GetFuncDesc(j).memid)[0])
            except Exception:
                continue
        for c in CHECK_SW:
            out["caps"][c] = c in names
        out["ok"] = True
    except Exception as e:
        out["error"] = str(e)
    return out


def probe_catia(app=None) -> Dict:
    out = {"cad": "CATIA", "ok": False, "caps": {}, "version": ""}
    acquired = False
    try:
        from .cad.guard import acquire as _acquire, release as _release
        # CATIA ?????????????????? GetActiveObject
        # ??? SolidWorks/NX ????????????
        _acquire("CATIA")
        acquired = True
        import pythoncom
        import win32com.client as win32
        from win32com.client import dynamic
        pythoncom.CoInitialize()
        a = app or win32.GetActiveObject("CATIA.Application")
        out["version"] = getattr(a, "Caption", "CATIA V5")
        doc = a.Documents.Add("Part")
        sf = doc.Part.ShapeFactory
        ids = dynamic.Dispatch(sf)._oleobj_
        for c in CHECK_CATIA:
            try:
                ids.GetIDsOfNames(c)
                out["caps"][c] = True
            except Exception:
                out["caps"][c] = False
        doc.Close()
        out["ok"] = True
    except Exception as e:
        out["error"] = str(e)[:120]
    finally:
        if acquired:
            try:
                _release()
            except Exception:
                pass
    return out


NX_JOURNAL = r'''
import NXOpen, json
s = NXOpen.Session.GetSession()
part = s.Parts.NewDisplay(__TMP__, NXOpen.Part.Units.Millimeters)
res = {"cad": "NX", "ok": True, "caps": {}, "version": ""}
try:
    res["version"] = "NX " + s.GetEnvironmentVariableValue("UGII_VERSION")
except Exception:
    pass
F = part.Features
for name in __NAMES__:
    res["caps"][name] = hasattr(F, name)
json.dump(res, open(__OUT__, "w", encoding="utf-8"), ensure_ascii=False)
'''


def probe_nx(nx_root: str, work_dir: str) -> Dict:
    out = {"cad": "NX", "ok": False, "caps": {}, "version": ""}
    acquired = False
    from .cad.guard import acquire as _acquire, release as _release
    rj = Path(nx_root) / "NXBIN" / "run_journal.exe"
    if not rj.exists():
        out["error"] = f"未找到 {rj}"
        return out
    try:
        # run_journal ??? ugraf/NX ????????????????????
        _acquire("NX")
        acquired = True
        w = Path(work_dir).resolve()
        w.mkdir(parents=True, exist_ok=True)
        out_json = w / "nx_caps.json"
        tmp = w / "_caps.prt"
        script = w / "caps_journal.py"
        text = (NX_JOURNAL.replace("__TMP__", repr(str(tmp)))
                           .replace("__OUT__", repr(str(out_json)))
                           .replace("__NAMES__", repr(CHECK_NX)))
        script.write_text(text, encoding="utf-8")
        proc = subprocess.run([str(rj), str(script)], capture_output=True, timeout=180,
                              cwd=str(w), encoding="mbcs", errors="replace")
        if not out_json.exists():
            out["error"] = (proc.stdout or proc.stderr or "journal 未产生结果")[-200:]
        if out_json.exists():
            d = json.loads(out_json.read_text(encoding="utf-8"))
            d["ok"] = True
            return d
    except Exception as e:
        out["error"] = str(e)[:120]
    finally:
        if acquired:
            try:
                _release()
            except Exception:
                pass
    return out


def probe_all(settings, work_dir: str, with_catia: bool = False) -> List[Dict]:
    res = []
    try:
        from .compat import detect_sw
        info = detect_sw()
        res.append(probe_solidworks(info.path or settings.sw_template))
    except Exception as e:
        res.append({"cad": "SolidWorks", "ok": False, "error": str(e)[:120], "caps": {}})
    try:
        res.append(probe_nx(settings.nx_root, work_dir))
    except Exception as e:
        res.append({"cad": "NX", "ok": False, "error": str(e)[:120], "caps": {}})
    if with_catia:
        try:
            res.append(probe_catia())
        except Exception as e:
            res.append({"cad": "CATIA", "ok": False, "error": str(e)[:120], "caps": {}})
    return res


def format_report(results: List[Dict]) -> str:
    lines = []
    for r in results:
        if not r.get("ok"):
            lines.append(f"【{r['cad']}】探测失败：{r.get('error','未知')}")
            continue
        lines.append(f"【{r['cad']}】{r.get('version','')}")
        caps = r.get("caps") or {}
        yes = [k for k, v in caps.items() if v]
        no = [k for k, v in caps.items() if not v]
        lines.append("  ✔ " + ("、".join(yes) if yes else "无"))
        if no:
            lines.append("  ✘ " + "、".join(no))
    return "\n".join(lines)