# -*- coding: utf-8 -*-
# ---------------------------------------------------------------------------
# 2026-11-21 事故说明（务必保留）：
#   本文件的源码在一次失败的整文件改写中被截断成 0 字节，磁盘上只剩
#   PyInstaller 包里的编译副本：
#       build\工程助手\PYZ-00.pyz  (2026-11-21 14:56 构建)
#   该副本仍包含 analyze / ocr / sanitize_spec / _scale_check /
#   _rebuild_flange_sleeve / _repair_revolve_profile / _inject_revolve_chamfers /
#   _absorb_revolve_holes / _feature_consistency / _revolve_check /
#   _image_hole_count / _derive_round_cuts 等全部方法（pycdc / unpyc 均无法
#   完整反编译 3.10 字节码，只能载入）。因此这里采用「载入编译副本 + 源码补丁」：
#       app\_vision_impl.marshal   ← 从 PYZ 原样取出的 code object
#   下方 _patch 段是纯源码补丁（齿轮参数表确定性重建、阶梯孔深纠正）。
#   日后若重写出真正的 vision.py，可直接删除本文件里的 exec 段与 marshal 副本。
# ---------------------------------------------------------------------------
from __future__ import annotations

import marshal
from pathlib import Path

_IMPL = Path(__file__).with_name("_vision_impl.marshal")
exec(marshal.loads(_IMPL.read_bytes()), globals())

# ===========================================================================
# 源码补丁 A：齿轮（模数/齿数/压力角 参数表）→ 确定性重建 spec
#
# 背景：视觉模型对同一张齿轮图会漂移（一次给 42×42×12 + 齿轮参数，一次把
# Ø42 当轴向总长做成回转体）。只要图上出现「模数 m / 齿数 z / 压力角 α」
# 参数表，几何就是可完全证明的，不必再由模型自由发挥：
#   d_a = m (z + 2)                       ← 齿顶圆，和图上 Ø42 对得上
#   轴向尺寸链（裸尺寸）：总宽 = 齿宽 + 轮毂长
#   阶梯孔：带孔公差带(J7/H7)的直径 = 大孔，比它小的最大直径 = 通孔小孔
#          比齿宽小的最大裸尺寸 = 大孔深度（part23 的 8；技术要求编号 3. 不算）
# ===========================================================================
import re as _re

_DIA = "[ØøΦφ⌀]"


def _g_num(s: str) -> float:
    return float(str(s).replace(",", "."))


def _gear_table(ocr_text: str):
    """从 OCR 文本里读齿轮参数表；读不到返回 None。"""
    t = ocr_text or ""
    m = _re.search(r"模数\s*[:：=]?\s*([0-9]+(?:[.,][0-9]+)?)", t)
    if not m:
        return None
    z = _re.search(r"齿数\s*[:：=]?\s*([0-9]+)", t)
    if not z:
        return None
    out = {"module": _g_num(m.group(1)), "teeth": int(z.group(1))}
    a = _re.search(r"压力角\s*[:：=]?\s*([0-9]+(?:[.,][0-9]+)?)", t)
    if a:
        out["pressure_angle"] = _g_num(a.group(1))
    return out


def _gear_diameters(ocr_text: str):
    """[(直径, 公差带)]，按图上出现顺序去重。"""
    out, seen = [], set()
    pat = _DIA + r"\s*([0-9]+(?:[.,][0-9]+)?)\s*([A-Za-z]{0,2}[0-9]{0,2})?"
    for m in _re.finditer(pat, ocr_text or ""):
        d = _g_num(m.group(1))
        tol = (m.group(2) or "").strip()
        if (d, tol) in seen:
            continue
        seen.add((d, tol))
        out.append((d, tol))
    return out


def _gear_norm_text(t: str) -> str:
    """OCR 有时返回 {"视图": "", "标注": "...\\n..."} 这种 JSON 包装。

    直接用原文按行切会把整段标注当成一行（含 Ø），轴向尺寸全部被过滤掉，
    齿轮重建静默跳过 -> 退回 LLM 的近似回转体（part23 final2 实测）。
    这里先解 JSON / 还原转义换行，保证逐行解析看到的是原始标注行。
    """
    s = str(t or "")
    st = s.strip()
    if st.startswith("{"):
        try:
            import json as _json
            d = _json.loads(st)
            if isinstance(d, dict):
                s = "\n".join(str(v) for v in d.values())
        except Exception:
            pass
    return s.replace("\\n", "\n")


def _gear_axial_numbers(ocr_text: str):
    """轴向裸尺寸：排除 Ø/R/SR/C/Ra/HRC/精度/角度/技术要求编号（1. 2. 3.）。"""
    vals = []
    for raw in (ocr_text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if _re.match(r"^[0-9]+\s*[.、)）]\s*", line):      # 技术要求 1. 2. 3.
            continue
        if _re.search(r"Ra|HRC|精度|模数|齿数|压力角|°|GB|[×xX]|" + _DIA, line):
            continue
        if _re.match(r"^[CRＳS]\s*[0-9]", line, _re.I):     # C1 / R8
            continue
        for m in _re.finditer(r"(?<![\d.])([0-9]+(?:[.,][0-9]+)?)(?![\d.])", line):
            vals.append(_g_num(m.group(1)))
    return vals


def _rebuild_gear_spec(spec, ocr_text: str):
    """按参数表重建齿轮 spec；无法证明时返回 None（不动 spec）。"""
    ocr_text = _gear_norm_text(ocr_text)
    tab = _gear_table(ocr_text)
    if not tab:
        return None
    m, z = tab["module"], tab["teeth"]
    od = m * (z + 2.0)

    dias = _gear_diameters(ocr_text)
    uniq = sorted({d for d, _t in dias if 0 < d <= od + 0.05})

    # 大孔：优先取带 J7/H7 这类「孔」公差带的直径
    bore = 0.0
    for d, tol in dias:
        if tol and tol[0].upper() in ("J", "H"):
            bore = d
    if bore <= 0:
        cand = [d for d in uniq if d < od * 0.85]
        bore = max(cand) if cand else 0.0

    small = max([d for d in uniq if d < bore - 1e-6], default=0.0)

    mids = [d for d in uniq if bore + 1e-6 < d < od - 1e-6]
    hub = 0.0
    if mids:
        # 轮毂取「介于孔与齿顶之间的最小直径」：part23 的 Ø32。
        # 不要跟随 LLM 的 hub_dia —— 同一张图两次分别给 32/38，模型会差 10% 体积。
        hub = min(mids)

    ax = [v for v in _gear_axial_numbers(ocr_text) if 0 < v <= od + 1e-6]
    if not ax:
        return None
    total = max(ax)
    rest = [v for v in ax if v < total - 1e-6]
    # 齿宽 = 轴向链里「小于总宽的最大值」。不要用 LLM 的 face_width 做优先：
    # part23 实测 LLM 时而把阶梯孔深 8 当成齿宽（8/10/12 都在图上），
    # 一信任它就得到 齿宽8 + 轮毂4 + 孔深0 的错误组合。
    face = max(rest) if rest else total
    hub_len = max(total - face, 0.0)
    deep = [v for v in ax if v < face - 1e-6]
    bore_depth = max(deep) if deep else 0.0

    # 倒角：优先「未注倒角 Cx」，否则取图中最小 C 值
    ch = 0.0
    mc = _re.search(r"未注倒角[^0-9\n]{0,8}C\s*([0-9]+(?:[.,][0-9]+)?)", ocr_text or "", _re.I)
    if mc:
        ch = _g_num(mc.group(1))
    else:
        cs = [_g_num(x) for x in _re.findall(r"\bC\s*([0-9]+(?:[.,][0-9]+)?)", ocr_text or "")]
        ch = min(cs) if cs else 0.0

    spec.part_type = "gear"
    spec.base_kind = "round"
    spec.module = float(m)
    spec.teeth = int(z)
    spec.face_width = float(face)
    spec.hub_len = float(hub_len)
    spec.thickness = float(total)
    spec.bore = float(bore)
    spec.small_bore = float(small)
    spec.bore_depth = float(bore_depth)
    spec.hub_dia = float(hub)
    spec.keyway_w = 0.0
    spec.keyway_d = 0.0
    spec.chamfer = float(ch)
    spec.fillet = 0.0
    spec.dome_h = 0.0
    spec.corner_radius = 0.0
    spec.revolve_profile = []
    spec.base_profile = []
    spec.shaft_segments = []
    spec.sweeps = []
    spec.features = []
    spec.validate()

    note = ("齿轮参数表确定性重建：模数 %g、齿数 %d、齿顶圆 Ø%g；"
            "总宽 %g = 齿宽 %g + 轮毂 %g；阶梯孔 Ø%g 深 %g + Ø%g 通；轮毂 Ø%g；C%g"
            % (m, z, od, total, face, hub_len, bore, bore_depth, small, hub, ch))
    return {"note": note, "od": od, "total": total, "face": face}


_ORIG_ANALYZE = DrawingAnalyzer.analyze


def _analyze_with_gear(self, *args, **kwargs):
    res = _ORIG_ANALYZE(self, *args, **kwargs)
    try:
        spec = getattr(res, "spec", None)
        txt = str(getattr(res, "ocr_text", "") or "")
        # 视图区 OCR 可能裁掉了右侧参数表；齿轮图必须补一次整页 OCR，
        # 否则模数/齿数读不到，确定性重建会静默跳过（part23 recog5 实测）。
        try:
            if not _gear_table(txt):
                _files = args[0] if args else kwargs.get("files")
                if _files:
                    _extra = self.ocr(list(_files))
                    if _extra and _gear_table(_extra):
                        txt = (txt + "\n" + _extra).strip()
        except Exception:
            pass
        if txt:
            self._last_gear_ocr = txt
            try:
                _dd = Path(__file__).resolve().parents[1] / "out" / "_contest"
                _dd.mkdir(parents=True, exist_ok=True)
                (_dd / "_last_gear_ocr.txt").write_text(txt, encoding="utf-8")
            except Exception:
                pass
        if spec is not None and txt:
            info = _rebuild_gear_spec(spec, txt)
            if info:
                # 旧诊断基于「42 当轴向总长」的错 spec，已失效，先清掉再重算
                def _stale(msg):
                    s = str(msg)
                    return ("比例" in s) or ("轴向总长" in s) or ("回转母线" in s)
                res.uncertain = [x for x in (getattr(res, "uncertain", None) or []) if not _stale(x)]
                res.blocking = [x for x in (getattr(res, "blocking", None) or []) if not _stale(x)]
                try:
                    for _m in DrawingAnalyzer._scale_check(txt, spec):
                        if _m not in res.uncertain:
                            res.uncertain.append(_m)
                except Exception:
                    pass
                if info["note"] not in res.uncertain:
                    res.uncertain.append(info["note"])
                try:
                    from .steps import spec_to_steps as _s2s
                    res.steps = _s2s(spec)
                except Exception:
                    pass
            elif _gear_table(txt):
                # 参数表读到了，但轴向链没重建成功：绝不允许退回"近似回转体"。
                _msg = ("齿轮参数表已读到（模数/齿数），但轴向尺寸链未能确定性重建，"
                        "禁止按近似回转体建模")
                if _msg not in (getattr(res, "blocking", None) or []):
                    res.blocking.append(_msg)
                res.spec.confidence = min(float(res.spec.confidence or 0.0), 0.3)
    except Exception as _e:
        try:
            res.uncertain.append("齿轮确定性重建跳过（%s）" % _e)
        except Exception:
            pass
    return res


DrawingAnalyzer.analyze = _analyze_with_gear

# ---------------------------------------------------------------- verify 后重建
# 二次复核会整体替换 spec，齿轮参数表的确定性结果必须重新注入，
# 否则会退回 shaft/revolve 的错读法（实测 part23 recog4）。
_ORIG_VERIFY = DrawingAnalyzer.verify


def _verify_with_gear(self, imgs, spec, *args, **kwargs):
    txt = str(getattr(self, "_last_gear_ocr", "") or "")
    out = _ORIG_VERIFY(self, imgs, spec, *args, **kwargs)
    if txt:
        try:
            info = _rebuild_gear_spec(out, txt)
            if info:
                out._gear_rebuilt = True
                out._gear_ocr = txt
                try:
                    for _m in DrawingAnalyzer._scale_check(txt, out):
                        if _m not in (getattr(self, "_gear_warn", []) or []):
                            pass
                except Exception:
                    pass
        except Exception:
            pass
    return out


DrawingAnalyzer.verify = _verify_with_gear


# ---------------------------------------------------------------- 齿轮倒角保真
# sanitize_spec 里有一条「回转母线已注入倒角 → spec.chamfer 归零」的规则，
# 对 gear 不适用（齿轮倒角由 CAD 适配器的 152 条棱边倒角实现）。
# 实测：gear spec 的 C0.5 会在 spec_to_steps 的清洗副本里被清零，
# 导致步骤只剩 gear、往返体积少 152 条倒角。
_ORIG_SANITIZE = DrawingAnalyzer.sanitize_spec


def _sanitize_keep_gear_chamfer(spec, *args, **kwargs):
    _is_gear = (str(getattr(spec, "part_type", "")).lower() == "gear"
                and float(getattr(spec, "teeth", 0) or 0) > 0)
    _ch = float(getattr(spec, "chamfer", 0.0) or 0.0) if _is_gear else 0.0
    out = _ORIG_SANITIZE(spec, *args, **kwargs)
    if _is_gear and _ch > 0:
        spec.chamfer = _ch
    return out


DrawingAnalyzer.sanitize_spec = staticmethod(_sanitize_keep_gear_chamfer)


# ---------------------------------------------------------------- 实心阶梯轴母线纠偏
# 实测 part04（三星轮小轴）：AI 把「Ø6.2 退刀槽」误当通孔，用 r=3.1 的长内壁
# 把母线闭合，实体变成薄壁管。判据：part_type=shaft，母线最小半径 >0，
# 且该半径同时以「短台阶」形式存在，但另有一条长度≈总长的竖直段贴在该半径上
# → 那条是误判的内孔壁，删掉并沿轴线闭合。
def _solidify_shaft_profile(spec):
    try:
        if spec is None or str(getattr(spec, "part_type", "") or "").lower() != "shaft":
            return ""
        prof = list(getattr(spec, "revolve_profile", None) or [])
        if len(prof) < 4:
            return ""
        pts = []
        for sg in prof:
            for k in ("p1", "p2"):
                q = sg.get(k)
                if isinstance(q, (list, tuple)) and len(q) >= 2:
                    pts.append((float(q[0]), float(q[1])))
        if not pts:
            return ""
        rmin = min(p[0] for p in pts)
        zmax = max(p[1] for p in pts)
        if rmin <= 0.05 or zmax <= 0:
            return ""
        short_step = False
        for sg in prof:
            a, b = sg.get("p1"), sg.get("p2")
            if not a or not b:
                continue
            if abs(a[0] - rmin) < 1e-6 and abs(b[0] - rmin) < 1e-6:
                if 0.0 < abs(float(b[1]) - float(a[1])) <= 0.5 * zmax:
                    short_step = True
        if not short_step:
            return ""
        kept = []
        removed = 0
        for sg in prof:
            a, b = sg.get("p1"), sg.get("p2")
            if not a or not b:
                continue
            dz = abs(float(b[1]) - float(a[1]))
            if (abs(a[0] - rmin) < 1e-6 and abs(b[0] - rmin) < 1e-6
                    and dz > 0.75 * zmax):
                removed += 1
                continue
            kept.append(dict(sg))
        if not removed:
            return ""
        end = kept[-1].get("p2") if kept else None
        if end and abs(float(end[0])) > 1e-6:
            kept.append({"type": "line", "p1": [float(end[0]), float(end[1])],
                         "p2": [0.0, float(end[1])]})
        kept.append({"type": "line", "p1": [0.0, float(zmax)], "p2": [0.0, 0.0]})
        first = kept[0].get("p1")
        if first and abs(float(first[0])) > 1e-6:
            kept.append({"type": "line", "p1": [0.0, 0.0],
                         "p2": [float(first[0]), float(first[1])]})
        spec.revolve_profile = kept
        spec.bore = 0.0
        return "实心阶梯轴母线纠偏：删除误判内孔壁 r=%.3g，沿轴线闭合" % rmin
    except Exception:
        return ""


# 法兰套重建只适用于「套/衬/环/盘」，不能把多台阶实心轴（含"轴"字、type=shaft）
# 误判成「法兰 + 凸台 + 通孔」。
_ORIG_REBUILD_SLEEVE = getattr(DrawingAnalyzer, "_rebuild_flange_sleeve", None)
if _ORIG_REBUILD_SLEEVE is not None:
    def _rebuild_sleeve_guarded(spec, text=""):
        name = str(getattr(spec, "part_name", "") or "")
        ptype = str(getattr(spec, "part_type", "") or "").lower()
        if "轴" in name or ptype == "shaft":
            return ""
        return _ORIG_REBUILD_SLEEVE(spec, text)
    DrawingAnalyzer._rebuild_flange_sleeve = staticmethod(_rebuild_sleeve_guarded)

_ORIG_ANALYZE_SOLID = DrawingAnalyzer.analyze


def _analyze_fix_solid(self, *args, **kwargs):
    res = _ORIG_ANALYZE_SOLID(self, *args, **kwargs)
    try:
        msg = _solidify_shaft_profile(getattr(res, "spec", None))
        if msg:
            try:
                res.uncertain.append(msg)
            except Exception:
                pass
            try:
                from .steps import spec_to_steps as _s2s
                res.steps = _s2s(res.spec)
            except Exception:
                pass
    except Exception:
        pass
    return res


DrawingAnalyzer.analyze = _analyze_fix_solid

_ORIG_VERIFY_SOLID = DrawingAnalyzer.verify


def _verify_fix_solid(self, imgs, spec, *args, **kwargs):
    out = _ORIG_VERIFY_SOLID(self, imgs, spec, *args, **kwargs)
    try:
        msg = _solidify_shaft_profile(out)
        if msg:
            out.notes = (str(getattr(out, "notes", "") or "") + "；" + msg)
    except Exception:
        pass
    return out


DrawingAnalyzer.verify = _verify_fix_solid
