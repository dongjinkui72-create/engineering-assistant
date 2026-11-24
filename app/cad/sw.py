"""SolidWorks adapter (COM / late binding).

Sketch -> extrude for the base, then one shared reference plane at the top face
and blind cuts for every feature.
"""
from __future__ import annotations

import math
import os
import time
from pathlib import Path
from typing import Callable

from ..calc import feature_area, feature_depth, inner_outline, part_volume
from ..geom import flatten, rect_outline, slot_outline
from ..models import PartSpec
from .base import BuildResult
from .guard import acquire as _cad_acquire, release as _cad_release
from .memguard import check as _mem_check

A = 0.0174532925199433  # 1 degree in radians

# 基体草图建在「前视基准面 XY」，不是「上视基准面 XZ」。
# SW 的 Top Plane 法向是 +Y：拉伸后厚度沿 Y、宽度沿 Z，导出 STEP 与 NX
# （XY 画轮廓、Z 为厚度）差一次轴交换。实测 U 形叉：Top Plane 版 STEP 为
# X27/Y6/Z14，Front Plane 版为 X27/Y14/Z6，与 NX 逐轴一致。
BASE_PLANES = ["前视基准面", "Front Plane", "上视基准面", "Top Plane"]
TOP_PLANES = ["上视基准面", "Top Plane"]   # 回转体备用


def _dismiss_startup_prompts(wait_s: float = 8.0) -> int:
    """关掉 SOLIDWORKS 启动期的模态提示（例如「工具栏信息不一致」）。

    为什么要做：这类框会把所有 COM 调用卡成 E_UNEXPECTED（实测 100% 复现），
    建模端表现为「基体拉伸失败」，实际上一行几何代码都没跑到。

    安全约束：
      ① 只处理 pid 属于 SLDWORKS.exe 的窗口，取不到 pid 就一个都不点；
      ② 只认 #32770 且子控件里恰好一个「确定/OK」；
      ③ 按 hwnd 去重；④ 点之前不猜测内容，只按上述结构判定。
    """
    try:
        import subprocess
        import win32con
        import win32gui
        import win32process
    except Exception:
        return 0
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq SLDWORKS.exe", "/FO", "CSV"])
        txt = out.decode("mbcs", "ignore")
        pids = set()
        for line in txt.splitlines()[1:]:
            parts = line.split('","')
            if len(parts) >= 2:
                digits = "".join(ch for ch in parts[1] if ch.isdigit())
                if digits:
                    pids.add(int(digits))
        if not pids:
            return 0
    except Exception:
        return 0

    def children(hwnd):
        acc = []

        def cb(h, _):
            acc.append((h, win32gui.GetClassName(h),
                        win32gui.GetWindowText(h).strip(),
                        bool(win32gui.IsWindowVisible(h))))
            return True

        try:
            win32gui.EnumChildWindows(hwnd, cb, None)
        except Exception:
            pass
        return acc

    clicked = 0
    t0 = time.time()
    seen = set()
    while time.time() - t0 < wait_s:
        found = []

        def each(hwnd, acc):
            _, wpid = win32process.GetWindowThreadProcessId(hwnd)
            if wpid in pids and win32gui.IsWindowVisible(hwnd):
                acc.append((hwnd, win32gui.GetClassName(hwnd)))

        win32gui.EnumWindows(each, found)
        for hwnd, cls in found:
            if cls != "#32770" or hwnd in seen:
                continue
            kids = children(hwnd)
            btns = [k for k in kids
                    if k[1] == "Button" and k[3] and k[2] in ("确定", "OK")]
            if len(btns) != 1:
                continue
            seen.add(hwnd)
            try:
                win32gui.PostMessage(btns[0][0], win32con.BM_CLICK, 0, 0)
                clicked += 1
            except Exception:
                pass
        if clicked:
            time.sleep(1.0)
        else:
            time.sleep(0.4)
    return clicked


def _segs_to_pts(segs, step_deg=2.0):
    """把 seg 列表（米）采样成闭合点列。

    SolidWorks 通过 API 画的圆弧与直线端点是“坐标重合”但不是“重合关系”，
    拉伸/切除时 SW 不认它是闭合轮廓（实测 8 段圆角矩形、4 段腰形全部失败）。
    折线没有这个问题；2° 采样在 R3 内腔上的体积误差 < 0.001%。
    """
    import math as _m
    pts = []
    for s in segs:
        if not s:
            continue
        if s[0] == "line":
            pts.append((s[1], s[2]))
        elif s[0] == "circle":
            _, cx, cy, r = s[:4]
            n = max(16, int(360.0 / step_deg))
            for i in range(n):
                a = _m.radians(360.0 * i / n)
                pts.append((cx + r * _m.cos(a), cy + r * _m.sin(a)))
        else:
            _, cx, cy, r, a0, a1 = s[:6]
            n = max(2, int(abs(a1 - a0) / step_deg) + 1)
            for i in range(n + 1):
                a = _m.radians(a0 + (a1 - a0) * i / n)
                pts.append((cx + r * _m.cos(a), cy + r * _m.sin(a)))
    out = []
    for x, y in pts:
        x, y = round(x, 9), round(y, 9)
        # 去掉退化小段（<0.1 µm）：三角函数的 1e-16 级抖动会在角点附近
        # 造出方向反转的极短线段，SW 会因此判定轮廓自交并整体拒绝。
        if not out or abs(x - out[-1][0]) > 1e-7 or abs(y - out[-1][1]) > 1e-7:
            out.append((x, y))
    if len(out) > 2 and abs(out[0][0] - out[-1][0]) < 1e-7 and abs(out[0][1] - out[-1][1]) < 1e-7:
        out.pop()
    return out


def _draw_segs(sm, segs):
    """按 seg 描述画**真圆弧 + 直线**（不含折线近似）。

    折线有两个已踩过的坑：
      1. 弦高误差：内腔 R3 用 5° 折线时，同一次抽壳实测偏 +2.16%；
      2. 轮廓闭合：折线点列在 SolidWorks 里偶发被闭合成三角形（削去半块料），
         同一份点列在别的上下文又正常 —— 不可依赖。
    真圆弧没有这两个问题，且 bbox 精确（不会出现 118.10 那种弦高缩水）。
    """
    import math as _m
    n = 0
    # SolidWorks 默认「草图实体延迟入库」(AddToDB=False)：多点/多段圆弧在延迟状态下
    # 由 SW 自己补算几何，实测会把 R3 三点弧解析成"以弦为直径"的退化半圆
    # (圆心 (18.5,8.5)、r=2.1213)，外形被撑到 40.62×20.62、体积 +0.64%。
    # 打开 AddToDB 让每段弧按给定三点立即定形，混合凹凸轮廓实测 bbox/体积全对。
    # 画完恢复原值，避免影响后续特征的草图行为。
    _prev_addtodb = None
    try:
        _prev_addtodb = sm.AddToDB
        sm.AddToDB = True
    except Exception:
        _prev_addtodb = None

    def _arc(s):
        nonlocal n
        _, cx, cy, r, a0, a1 = s[:6]
        if r <= 1e-12:
            return
        sweep = a1 - a0
        # 180° 整半圆在 SW 里有两种退化：
        #   CreateArc(center,start,end,dir) 两侧都是 180°，dir 定不了往哪边；
        #   Create3PointArc 的"弧上点"也正好落在两个半圆的分界上，实测凹口
        #   被画到凸侧（Y 20→25.7、体积 +16.9%）。
        # 一律拆成 ≤179° 的子弧：此时"小弧"唯一，direction=+1 就是唯一解。
        nsub = max(1, int(abs(sweep) // 179.0) + (1 if abs(sweep) % 179.0 > 1e-9 else 0))
        if nsub < 1:
            nsub = 1
        d = sweep / nsub
        for k in range(nsub):
            b0 = _m.radians(a0 + d * k)
            b1 = _m.radians(a0 + d * (k + 1))
            x1, y1 = cx + r * _m.cos(b0), cy + r * _m.sin(b0)
            x2, y2 = cx + r * _m.cos(b1), cy + r * _m.sin(b1)
            # 端点吸到 1e-9 m（1 µm），避免 cos(90°) 的 6e-17 残留让 SW 判成开口
            bm = _m.radians(a0 + d * (k + 0.5))
            xm, ym = cx + r * _m.cos(bm), cy + r * _m.sin(bm)
            cx2, cy2, x1, y1, x2, y2, xm, ym = [round(v, 9) for v in
                                               (cx, cy, x1, y1, x2, y2, xm, ym)]
            try:
                sm.Create3PointArc(x1, y1, 0.0, x2, y2, 0.0, xm, ym, 0.0)
            except Exception:
                sm.CreateArc(cx2, cy2, 0.0, x1, y1, 0.0, x2, y2, 0.0, 1)
            n += 1

    def _circle(s):
        nonlocal n
        _, cx, cy, r = s[:4]
        if r <= 1e-12:
            return
        sm.CreateCircleByRadius(cx, cy, 0.0, r)
        n += 1

    def _line(s):
        nonlocal n
        _, x1, y1, x2, y2 = s[:5]
        x1, y1, x2, y2 = [round(v, 9) for v in (x1, y1, x2, y2)]
        if abs(x1 - x2) < 1e-12 and abs(y1 - y2) < 1e-12:
            return
        sm.CreateLine(x1, y1, 0.0, x2, y2, 0.0)
        n += 1

    # 必须先画圆弧/圆，再画直线。实测：3 直线 + 半圆按轮廓顺序画时，
    # FeatureExtrusion2 报「服务器出现意外情况」或返回空；弧先画则
    # 弧端板体积 2,141.814、圆角板 47,570.796、腰形槽 11,141.593，
    # 三者均与解析值一致。
    for s in segs:
        if s and s[0] in ("arc", "circle"):
            (_circle if s[0] == "circle" else _arc)(s)
    for s in segs:
        if s and s[0] == "line":
            _line(s)
    if _prev_addtodb is not None:
        try:
            sm.AddToDB = _prev_addtodb
        except Exception:
            pass
    return n


def _base_segs_m(spec):
    """基体轮廓（米，真圆弧）：支持四角圆角与单/双端圆弧头。

    不再用折线采样：圆弧必须由 SolidWorks 原生 CreateArc 生成，
    否则 bbox 会因弦高缩水（R12 分 44 段时每段弦高约 0.031 mm）。
    """
    from ..calc import base_outline
    return SwAdapter._mm_to_seg(base_outline(spec))


def _base_pts_m(spec):
    """兼容旧调用：仅用于确实需要点列的场合，基体不再使用。"""
    return flatten(_base_segs_m(spec), arc_step_deg=1.5)


class SwAdapter:
    name = "SolidWorks"

    def __init__(self, template: str):
        self.template = template

    def _tpl(self) -> str:
        if self.template and os.path.exists(self.template):
            return self.template
        try:
            from ..compat import detect_sw
            info = detect_sw()
            if info.template:
                self.template = info.template
        except Exception:
            pass
        return self.template

    def available(self) -> bool:
        return bool(self._tpl()) and os.path.exists(self._tpl())

    # ------------------------------------------------------------------ utils
    @staticmethod
    def _plane(doc, names):
        for n in names:
            try:
                f = doc.FeatureByName(n)
                if f is not None:
                    return f
            except Exception:
                pass
        return None

    @staticmethod
    def _draw_polyline(sm, pts):
        for i in range(len(pts)):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % len(pts)]
            if abs(x1 - x2) < 1e-12 and abs(y1 - y2) < 1e-12:
                continue
            sm.CreateLine(x1, y1, 0.0, x2, y2, 0.0)


    def _build_sheet(self, doc, spec, log):
        """U-channel sheet part: base plate + two upstand flanges (united)."""
        sm, fm = doc.SketchManager, doc.FeatureManager
        plane = self._plane(doc, ["上视基准面", "Top Plane"])
        L = spec.sheet_L / 1000.0
        W = spec.sheet_W / 1000.0
        t = spec.sheet_t / 1000.0
        h = spec.flange_h / 1000.0

        def box(cx, cy, sx, sy, depth):
            doc.ClearSelection2(True)
            plane.Select2(False, 0)
            sm.InsertSketch(True)
            sm.CreateCenterRectangle(cx, cy, 0, cx + sx / 2.0, cy + sy / 2.0, 0)
            sm.InsertSketch(True)
            return fm.FeatureExtrusion2(True, False, False, 0, 0, depth, depth,
                                        False, False, False, False, A, A, False, False,
                                        False, False, True, True, True, 0, 0, False)

        f0 = box(0, 0, L, W, t)
        f1 = box(0, W / 2.0 - t / 2.0, L, t, h)
        f2 = box(0, -(W / 2.0 - t / 2.0), L, t, h)
        if not all((f0, f1, f2)):
            raise RuntimeError("sheet metal build failed")
        log(f"sheet: base {spec.sheet_L:g}x{spec.sheet_W:g}x{spec.sheet_t:g}, flange h={spec.flange_h:g}")

    def _build_gear(self, doc, spec, log):
        """Involute gear: native spline/arc profile -> extrude -> hub -> stepped bore."""
        from ..gear import gear_profile
        sm, fm = doc.SketchManager, doc.FeatureManager
        plane = self._plane(doc, ["上视基准面", "Top Plane"])
        if plane is None:
            raise RuntimeError("no top plane")
        from ..gear import gear_entities
        ents = gear_entities(spec.module, spec.teeth)
        doc.ClearSelection2(True)
        plane.Select2(False, 0)
        sm.InsertSketch(True)
        # SW2024: without AddToDB the teeth stay as several open contours and
        # the extrusion fails. AddToDB merges splines/arcs into one closed loop.
        prev_add = None
        try:
            prev_add = bool(sm.AddToDB)
        except Exception:
            prev_add = None
        sm.AddToDB = True
        n_spl = n_arc = 0
        for kind, *rest in ents:
            if kind == "spline":
                flat = []
                for (x, y) in rest[0]:
                    flat += [x / 1000.0, y / 1000.0, 0.0]
                # SW2024: SketchManager.CreateSpline ???CreateSpline2 ?
                # ????????? SAFEARRAY(VT_R8)???? list ????? None?
                from win32com.client import VARIANT
                import pythoncom
                pd = VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, flat)
                seg = sm.CreateSpline2(pd, False)
                if seg is None:
                    raise RuntimeError("CreateSpline2 failed")
                n_spl += 1
            else:
                _c, p1, p2 = rest
                sm.CreateArc(0, 0, 0, p1[0] / 1000.0, p1[1] / 1000.0, 0,
                             p2[0] / 1000.0, p2[1] / 1000.0, 0, 1)
                n_arc += 1
        sm.InsertSketch(True)
        if prev_add is not None:
            try:
                sm.AddToDB = prev_add
            except Exception:
                pass
        f = fm.FeatureExtrusion2(True, False, False, 0, 0, spec.face_width / 1000.0,
                                 spec.face_width / 1000.0, False, False, False, False, A, A,
                                 False, False, False, False, True, True, True, 0, 0, False)
        if f is None:
            raise RuntimeError("gear extrude failed")
        log(f"gear profile: {n_spl} involute splines + {n_arc} arcs extruded")

        def _gvol():
            try:
                m = doc.GetMassProperties
            except Exception:
                return 0.0
            return float(m[3]) * 1e9 if m and len(m) > 3 else 0.0

        W = float(spec.face_width)
        hub_d = float(getattr(spec, "hub_dia", 0.0) or 0.0)
        hub_l = float(getattr(spec, "hub_len", 0.0) or 0.0)
        bore = float(spec.bore or 0.0)
        bore_d = float(getattr(spec, "bore_depth", 0.0) or 0.0)
        small = float(getattr(spec, "small_bore", 0.0) or 0.0)
        total = W + max(hub_l, 0.0)

        def _plane_at(z_mm):
            """平行于上视基准面的参考面（z 米）。返回平面特征或 None。"""
            try:
                doc.ClearSelection2(True)
            except Exception:
                pass
            try:
                plane.Select2(False, 0)
            except Exception:
                pass
            try:
                rp = fm.InsertRefPlane(8, z_mm / 1000.0, 0, 0, 0, 0)
            except Exception:
                rp = None
            if rp is not None:
                return rp
            # 兜底：直接按名称扫特征树里的基准面
            try:
                ft = doc.FirstFeature
                while ft is not None:
                    nm = ""
                    try:
                        nm = ft.Name
                    except Exception:
                        pass
                    if abs(z_mm) > 1e-9 and str(z_mm).split(".")[0] + "" and "基准面" in str(nm):
                        pass
                    ft = ft.GetNextFeature
            except Exception:
                pass
            return None

        def _top_face(z_mm, cands):
            """在 z 高度上朝下打射线找真实平面。cands 为 (x,y) 毫米，按优先级。"""
            for (x, y) in cands:
                try:
                    if doc.Extension.SelectByRay(x / 1000.0, y / 1000.0,
                                                 (z_mm + 1.0) / 1000.0,
                                                 0.0, 0.0, -1.0, 0.0006, 2, False, 0, 0):
                        return doc.SelectionManager.GetSelectedObject6(1, -1)
                except Exception:
                    continue
            return None

        def _bbox_mm():
            try:
                bs = doc.GetBodies2(0, True) or []
                lo = [1e18] * 3
                hi = [-1e18] * 3
                for b in bs:
                    bb = b.GetBodyBox()
                    for i in range(3):
                        lo[i] = min(lo[i], bb[i] * 1000.0)
                        hi[i] = max(hi[i], bb[i + 3] * 1000.0)
                return lo + hi
            except Exception:
                return None

        # ---- hub boss: 参考面 + 逐向试探 + 包围盒校验 ----------
        # 实测该草图基准面的法向是全局 +Y：零件厚度沿 Y，不是 Z。
        if hub_d > 0 and hub_l > 0:
            base_plane = _plane_at(W)
            if base_plane is None:
                raise RuntimeError("gear hub: ref plane %g failed" % W)
            got = None
            for flip in (False, True):
                doc.ClearSelection2(True)
                try:
                    base_plane.Select2(False, 0)
                except Exception:
                    pass
                sm.InsertSketch(True)
                sm.CreateCircleByRadius(0, 0, 0, hub_d / 2000.0)
                sm.InsertSketch(True)
                hf = None
                try:
                    hf = fm.FeatureExtrusion2(True, flip, False, 0, 0, hub_l / 1000.0,
                                              hub_l / 1000.0, False, False, False, False, A, A,
                                              False, False, False, False, True, True, True, 0, 0, False)
                except Exception:
                    hf = None
                try:
                    doc.EditRebuild3()
                except Exception:
                    pass
                bb = _bbox_mm()
                top = bb[4] if bb else -1.0     # max Y
                log("hub try flip=%s -> maxY %.3f (want %.3f)" % (flip, top, W + hub_l))
                if hf is not None and abs(top - (W + hub_l)) < 0.05:
                    got = hf
                    break
                # 方向反了：删掉再试另一个方向
                if hf is not None:
                    try:
                        hf.Select2(False, 0)
                        doc.EditDelete()
                    except Exception:
                        pass
                    try:
                        doc.EditRebuild3()
                    except Exception:
                        pass
            if got is None:
                raise RuntimeError("gear hub did not extend outward")
            log("hub D%g x %g extruded (vol %.1f)" % (hub_d, hub_l, _gvol()))

        def _counterbore_revolve(r_out_mm, r_in_mm, depth_mm):
            """台阶大径段：轮毂端面画 Ø26 草图，原生盲切 depth_mm 深。

            原回转实现实测只去掉 715.8（应 923.6）且把包围盒顶到 Y=13，
            改用"端面草图 + 定深切除"，与 cut_circle 同一条已验证的通路；
            去料量按环形面积校验，方向反了就撤销换向。
            """
            exp = math.pi * (r_out_mm ** 2 - r_in_mm ** 2) * depth_mm
            for flip in (False, True):
                doc.ClearSelection2(True)
                rp = _plane_at(total)
                if rp is None:
                    raise RuntimeError("counterbore: ref plane %g failed" % total)
                if not rp.Select2(False, 0):
                    raise RuntimeError("counterbore: ref plane not selectable")
                sm.InsertSketch(True)
                sm.CreateCircleByRadius(0, 0, 0, r_out_mm / 1000.0)
                sm.InsertSketch(True)
                v0 = _gvol()
                f = None
                try:
                    f = fm.FeatureCut4(True, False, flip, 0, 0,
                                       depth_mm / 1000.0, depth_mm / 1000.0,
                                       False, False, False, False, A, A,
                                       False, False, False, False, False,
                                       True, True, True, True, True,
                                       0, 0.0, False, True)
                except Exception:
                    f = None
                try:
                    doc.ClearSelection2(True)
                except Exception:
                    pass
                try:
                    doc.EditRebuild3()
                except Exception:
                    pass
                dv = v0 - _gvol()
                if f is not None and 0.55 * exp <= dv <= 1.45 * exp:
                    log("counterbore D%g x %g extruded (removed %.1f / want %.1f)" %
                        (r_out_mm * 2, depth_mm, dv, exp))
                    return
                try:
                    doc.EditUndo2(2)
                except Exception:
                    pass
                try:
                    doc.EditRebuild3()
                except Exception:
                    pass
            raise RuntimeError("counterbore D%g x %g failed" % (r_out_mm * 2, depth_mm))

        def cut_circle(r_mm, depth_mm=0.0, from_top=True, tag=""):
            """depth_mm=0 -> 从基面双向通切；>0 -> 从轮毂端面向内盲切 depth_mm。"""
            doc.ClearSelection2(True)
            if from_top:
                rp = _plane_at(total)
                if rp is None:
                    raise RuntimeError("cut_circle: ref plane %g failed" % total)
                if not rp.Select2(False, 0):
                    raise RuntimeError("cut_circle: ref plane %g not selectable" % total)
            else:
                plane.Select2(False, 0)
            sm.InsertSketch(True)
            sm.CreateCircleByRadius(0, 0, 0, r_mm / 1000.0)
            sm.InsertSketch(True)
            for flip in (True, False):
                out = fm.FeatureCut4(True, False, flip, 1, 1, 0, 0, False, False, False, False, A, A,
                                     False, False, False, False, False, True, True, True, True, True,
                                     0, 0.0, False, True)
                if out is not None:
                    try:
                        doc.EditRebuild3()
                    except Exception:
                        pass
                    return out
            raise RuntimeError("through cut D%g failed" % (r_mm * 2))

        if small > 0:
            # 阶梯孔：Ø26J7 深8 是"大径段在轮毂端"、Ø23 贯通。
            # 顺序必须是先 Ø23 通（小径，整根），再用原生回转切掉
            # 轮毂端 Ø26 大径那一圈（r 11.5→13，深 8）。
            # 反过来先 Ø26 通会把大径段本来就该留的材料一起掏空。
            c = cut_circle(small / 2.0, from_top=False)
            log("through bore D%g cut: vol %.1f" % (small, _gvol()))
            if bore_d > 0:
                _counterbore_revolve(bore / 2.0, small / 2.0, bore_d)
                log("counterbore done: vol %.1f" % _gvol())
        elif bore > 0:
            c = cut_circle(bore / 2.0, from_top=False)
            log("bore D%g cut" % bore)

        if spec.keyway_w > 0 and spec.keyway_d > 0:
            doc.ClearSelection2(True)
            plane.Select2(False, 0)
            sm.InsertSketch(True)
            y0 = spec.bore / 2.0 - spec.keyway_d
            sm.CreateLine(-spec.keyway_w / 2000.0, y0 / 1000.0, 0, spec.keyway_w / 2000.0, y0 / 1000.0, 0)
            sm.CreateLine(spec.keyway_w / 2000.0, y0 / 1000.0, 0, spec.keyway_w / 2000.0,
                          (spec.bore / 2.0 + spec.keyway_d) / 1000.0, 0)
            sm.CreateLine(spec.keyway_w / 2000.0, (spec.bore / 2.0 + spec.keyway_d) / 1000.0, 0,
                          -spec.keyway_w / 2000.0, (spec.bore / 2.0 + spec.keyway_d) / 1000.0, 0)
            sm.CreateLine(-spec.keyway_w / 2000.0, (spec.bore / 2.0 + spec.keyway_d) / 1000.0, 0,
                          -spec.keyway_w / 2000.0, y0 / 1000.0, 0)
            sm.InsertSketch(True)
            fm.FeatureCut4(True, False, True, 1, 1, 0, 0, False, False, False, False, A, A,
                           False, False, False, False, False, True, True, True, True, True,
                           0, 0.0, False, True)
            log("keyway %gx%g cut" % (spec.keyway_w, spec.keyway_d))

        # ????????? 3xM5 EQS??? spec.features ?? hole ????????????
        # ?????? feats ??????????????????????
        extra_holes = []
        for _f in (getattr(spec, "features", None) or []):
            try:
                if str(getattr(_f, "type", "")).lower() != "hole":
                    continue
                _d = float(getattr(_f, "diameter", 0.0) or 0.0)
                if _d <= 0:
                    continue
                extra_holes.append((float(_f.x), float(_f.y), _d / 2.0,
                                    float(getattr(_f, "depth", 0.0) or 0.0)))
            except Exception:
                continue
        if extra_holes:
            rp = _plane_at(total)
            if rp is None:
                raise RuntimeError("gear accessory holes: ref plane %g failed" % total)
            doc.ClearSelection2(True)
            if not rp.Select2(False, 0):
                raise RuntimeError("gear accessory holes: ref plane not selectable")
            sm.InsertSketch(True)
            for _x, _y, _r, _dep in extra_holes:
                sm.CreateCircleByRadius(_x / 1000.0, _y / 1000.0, 0.0, _r / 1000.0)
            sm.InsertSketch(True)
            _v0 = _gvol()
            _ok = None
            for _flip in (True, False):
                _ok = fm.FeatureCut4(True, False, _flip, 1, 1, 0, 0, False, False,
                                     False, False, A, A, False, False, False,
                                     False, False, True, True, True, True, True,
                                     0, 0.0, False, True)
                if _ok is not None:
                    break
            if _ok is None:
                raise RuntimeError("gear accessory holes through-cut failed")
            try:
                doc.EditRebuild3()
            except Exception:
                pass
            log("gear accessory holes=%d cut (removed %.1f mm3)" % (len(extra_holes), _v0 - _gvol()))

        try:
            doc.EditRebuild3()
        except Exception:
            pass
        try:
            _bs = doc.GetBodies2(0, True) or []
            log("gear bodies=%d vol=%.1f" % (len(_bs), _gvol()))
            for _b in _bs:
                _bb = _b.GetBodyBox()
                log("  body box mm %s" % [round(v * 1000, 3) for v in _bb])
        except Exception as _e:
            log("gear body probe failed: %s" % _e)

    def _build_shaft(self, doc, spec, log):
        """回转体：前视基准面画阶梯截面 + 中心线，旋转 360°。"""
        sm, fm = doc.SketchManager, doc.FeatureManager
        plane = self._plane(doc, ["前视基准面", "Front Plane", "上视基准面", "Top Plane"])
        if plane is None:
            raise RuntimeError("找不到旋转用基准面")
        doc.ClearSelection2(True)
        plane.Select2(False, 0)
        sm.InsertSketch(True)
        segs = [(float(s["dia"]) / 1000.0, float(s["length"]) / 1000.0) for s in spec.shaft_segments]
        total = sum(l for _, l in segs)
        sm.CreateCenterLine(0, 0, 0, total, 0, 0)
        pts, x = [], 0.0
        for (d, L) in segs:
            r = d / 2.0
            pts.append((x, r))
            pts.append((x + L, r))
            x += L
        pts.append((x, 0.0))
        pts.append((0.0, 0.0))
        for i in range(len(pts)):
            a, b = pts[i], pts[(i + 1) % len(pts)]
            sm.CreateLine(a[0], a[1], 0, b[0], b[1], 0)
        sm.InsertSketch(True)
        f = fm.FeatureRevolve2(True, True, False, False, False, False, 0, 0,
                               2 * math.pi, 0, False, False, 0, 0, 0, 0, 0,
                               True, True, True)
        if f is None:
            raise RuntimeError("旋转特征创建失败")
        log(f"回转体完成：{len(segs)} 段，总长 {total*1000:g} mm")

        # Shaft accessories: axial keyways and axial end holes.
        # keyway: x=axial start, length=axial length, width=slot width, depth=radial depth.
        # hole with face="right": depth measured from right end face (0 = through).
        feats = list(getattr(spec, "features", None) or [])
        cum = []
        _x = 0.0
        for _d, _l in segs:
            cum.append((_x, _x + _l, _d * 1000.0))
            _x += _l

        def _dia_at(x_mm):
            for a, b, dm in cum:
                if a - 1e-9 <= x_mm <= b + 1e-9:
                    return dm
            return cum[-1][2] if cum else 0.0

        for _f in feats:
            _t = str(getattr(_f, "type", "")).lower()
            if _t == "keyway":
                _x0 = float(_f.x or 0.0) / 1000.0
                _ln = float(_f.length or 0.0) / 1000.0
                _wd = float(_f.width or 0.0) / 1000.0
                _dp = float(_f.depth or 0.0) / 1000.0
                _rad = _dia_at(float(_f.x or 0.0) + float(_f.length or 0.0) * 0.5) / 2000.0
                if _ln <= 0 or _wd <= 0 or _dp <= 0:
                    continue
                # A real keyway is a width-limited end-milled slot, not a
                # through-width slab. Sketch the native stadium on a plane
                # tangent to the shaft and cut radially by depth.
                _top = self._plane(doc, ["\u4e0a\u89c6\u57fa\u51c6\u9762", "Top Plane"])
                if _top is None:
                    raise RuntimeError("shaft keyway: no top plane")
                doc.ClearSelection2(True)
                _top.Select2(False, 0)
                _kp = fm.InsertRefPlane(8, _rad, 0, 0, 0, 0)
                if _kp is None:
                    raise RuntimeError("shaft keyway: tangent plane failed")
                doc.ClearSelection2(True)
                _kp.Select2(False, 0)
                sm.InsertSketch(True)
                _hw = _wd / 2.0
                _xa = _x0 + _hw
                _xb = _x0 + _ln - _hw
                if _xb < _xa:
                    _xa = _xb = _x0 + _ln / 2.0
                sm.CreateLine(_xa, _hw, 0, _xb, _hw, 0)
                sm.CreateLine(_xb, -_hw, 0, _xa, -_hw, 0)
                sm.Create3PointArc(_xb, _hw, 0, _xb, -_hw, 0, _x0 + _ln, 0, 0)
                sm.Create3PointArc(_xa, -_hw, 0, _xa, _hw, 0, _x0, 0, 0)
                sm.InsertSketch(True)
                _cut = None
                for _flip in (True, False):
                    _cut = fm.FeatureCut4(True, False, _flip, 0, 0, _dp, _dp,
                                          False, False, False, False, A, A,
                                          False, False, False, False, False,
                                          True, True, True, True, True,
                                          0, 0.0, False, True)
                    if _cut is not None:
                        break
                if _cut is None:
                    raise RuntimeError("shaft keyway cut failed: %gx%g" % (float(_f.width or 0), float(_f.length or 0)))
                log("shaft keyway %gx%g depth%g done (x=%g..%g)" %
                    (float(_f.width or 0), float(_f.length or 0), float(_f.depth or 0),
                     float(_f.x or 0), float(_f.x or 0) + float(_f.length or 0)))
            elif _t == "hole" and str(getattr(_f, "face", "")).lower() == "right":
                _r = float(_f.diameter or 0.0) / 2000.0
                _dep = float(_f.depth or 0.0) / 1000.0
                if _r <= 0:
                    continue
                doc.ClearSelection2(True)
                _rp = self._plane(doc, ["\u53f3\u89c6\u57fa\u51c6\u9762", "Right Plane"])
                if _rp is None:
                    raise RuntimeError("shaft end hole: no right plane")
                _rp.Select2(False, 0)
                _ref = fm.InsertRefPlane(8, total, 0, 0, 0, 0)
                if _ref is None:
                    raise RuntimeError("shaft end hole: ref plane failed")
                doc.ClearSelection2(True)
                _ref.Select2(False, 0)
                sm.InsertSketch(True)
                sm.CreateCircleByRadius(0, 0, 0, _r)
                sm.InsertSketch(True)
                _cut = None
                for _flip in (True, False):
                    if _dep > 0:
                        _cut = fm.FeatureCut4(True, False, _flip, 0, 0, _dep, _dep,
                                              False, False, False, False, A, A,
                                              False, False, False, False, False,
                                              True, True, True, True, True,
                                              0, 0.0, False, True)
                    else:
                        _cut = fm.FeatureCut4(True, False, _flip, 1, 1, 0, 0,
                                              False, False, False, False, A, A,
                                              False, False, False, False, False,
                                              True, True, True, True, True,
                                              0, 0.0, False, True)
                    if _cut is not None:
                        break
                if _cut is None:
                    raise RuntimeError("shaft end hole cut failed: D%g" % (float(_f.diameter or 0)))
                log("shaft end hole D%g depth%g done" % (float(_f.diameter or 0), float(_f.depth or 0)))

    def _gear_end_edges(self, doc, spec):
        """齿轮：收集两个端面（Y=0 / 齿圈端 / 轮毂端）上的全部实边。

        齿轮齿廓是 38 条样条 + 38 条圆弧组成的闭环，顶面被切成多个面，
        旧的射线取面法一条都取不到；这里直接遍历实体边，按两端点 Y 值
        判定是否落在端面上，天然覆盖齿顶轮廓、轮毂外圆与内孔口。
        """
        W = float(getattr(spec, "face_width", 0.0) or 0.0) / 1000.0
        hl = float(getattr(spec, "hub_len", 0.0) or 0.0) / 1000.0
        total = W + max(hl, 0.0)
        levels = [0.0]
        if W > 0:
            levels.append(W)
        if hl > 0:
            levels.append(total)
        tol = 3e-4
        out = []
        edges = []
        for b in (doc.GetBodies2(0, True) or []):
            try:
                e = b.GetEdges
                e = e() if callable(e) else e
                edges.extend(list(e or []))
            except Exception:
                continue
        for e in edges:
            try:
                p1 = e.GetStartVertex.GetPoint
                p2 = e.GetEndVertex.GetPoint
                p1 = [float(v) for v in p1]
                p2 = [float(v) for v in p2]
            except Exception:
                continue
            if abs(p1[1] - p2[1]) > tol:
                continue
            if any(abs(p1[1] - y0) <= tol for y0 in levels):
                out.append(e)
        return out

    # ------------------------------------------------------------ edge features
    def _top_face_edges(self, doc, spec):
        """顶面外轮廓边。

        基体草图建在 Front Plane(XY)、沿 +Z 拉伸，所以零件高度沿 Z，顶面在 Z=T。
        （SQL 早期版本把草图放在 Top Plane，那时顶面在 Y=T，两种约定不能混用。）
        """
        T = spec.thickness / 1000.0
        L, W = spec.length / 2000.0, spec.width / 2000.0
        if spec.base_kind == "round":
            # 圆盘要从"外缘附近"朝上打射线。原先固定用 0.6R，而法兰盘切了
            # Ø90 贯通沉孔（半径 45 > 0.6×60=36）后射线落进孔里，
            # 一条顶面边都取不到 → 倒角"共找到 0 条外轮廓边"。
            # 改多档半径：0.98R 最贴外缘，逐步内缩兜底。
            _R = spec.diameter / 2000.0
            pts = [(_R * f, 0.0) for f in (0.98, 0.90, 0.75, 0.55, 0.35)]
        else:
            pts = [(L * 0.55, W * 0.55), (0.0, 0.0), (-L * 0.55, W * 0.55)]
        if str(getattr(spec, "base_kind", "")) == "gear" or str(getattr(spec, "part_type", "")) == "gear":
            return self._gear_end_edges(doc, spec)
        out = []
        for (x, y) in pts:
            doc.ClearSelection2(True)
            try:
                if not doc.Extension.SelectByRay(x, y, T + 0.01, 0.0, 0.0, -1.0,
                                                 0.0006, 2, False, 0, 0):
                    continue
                face = doc.SelectionManager.GetSelectedObject6(1, -1)
                edges = list(face.GetEdges)
                if not edges:
                    continue
                keep = []
                for e in edges:
                    try:
                        c = e.GetCurve
                        if spec.base_kind != "round" and not c.IsLine:
                            continue
                        if spec.base_kind == "round":
                            # 圆盘：只留"外圆"那条边。切了孔/型腔后顶面会分成多个面，
                            # 一口气拿到 10 条边（含内孔边），全选去倒角必然失败。
                            # 判据：中点半径≈D/2 且 z≈T（外圆边就在顶面外缘）。
                            R = spec.diameter / 2000.0
                            cp = e.GetCurveParams2
                            mx = (cp[0] + cp[3]) / 2.0
                            my = (cp[1] + cp[4]) / 2.0
                            mz = (cp[2] + cp[5]) / 2.0
                            if abs(mz - T) > 0.0005:
                                continue
                            if abs((mx * mx + my * my) ** 0.5 - R) > 0.001:
                                continue
                        keep.append(e)
                    except Exception:
                        # 取不到参数就保守地保留（宁可多选一层由上层失败兜底）
                        if spec.base_kind != "round":
                            keep.append(e)
                if spec.base_kind == "round":
                    if keep:
                        return keep
                elif len(keep) >= 2:
                    return keep
                out.extend(keep)
            except Exception:
                continue
        return [] if spec.base_kind == "round" else out

    @staticmethod
    def _edge_mid(e):
        try:
            cp = e.GetCurveParams2
            return ((cp[0] + cp[3]) / 2.0, (cp[1] + cp[4]) / 2.0, (cp[2] + cp[5]) / 2.0)
        except Exception:
            return None

    @staticmethod
    def _target_mids(spec):
        """顶面外轮廓 4 条边的中点（米），用于逐条定位（顶面在 Z=T）。"""
        T = spec.thickness / 1000.0
        if spec.base_kind == "round":
            r = spec.diameter / 2000.0
            return [(0.0, r, T), (0.0, -r, T), (r, 0.0, T), (-r, 0.0, T)]
        L, W = spec.length / 2000.0, spec.width / 2000.0
        return [(0.0, -W, T), (0.0, W, T), (-L, 0.0, T), (L, 0.0, T)]

    @staticmethod
    def _mm_to_seg(segs):
        out = []
        for s in segs:
            if s[0] == "line":
                out.append(("line", s[1] / 1000.0, s[2] / 1000.0,
                            s[3] / 1000.0, s[4] / 1000.0))
            elif s[0] == "arc":
                out.append(("arc", s[1] / 1000.0, s[2] / 1000.0, s[3] / 1000.0, s[4], s[5]))
            elif s[0] == "circle":
                out.append(("circle", s[1] / 1000.0, s[2] / 1000.0, s[3] / 1000.0))
        return out

    @staticmethod
    def _arc_ends(spec):
        """该零件是否有单/双端圆弧头（U 形叉这类）。"""
        return (str(getattr(spec, "end_left", "square") or "square").lower() == "arc"
                or str(getattr(spec, "end_right", "square") or "square").lower() == "arc")

    def _fillet_corners(self, doc, spec, log):
        """四角圆角：直角矩形拉伸后给 4 条竖棱倒圆角。

        不用"草图折线拼圆弧"：R≥10 时 1920 段折线让 SW 内核结果不稳定
        （同一 spec 两次跑差 181 mm³）。也不用"角部月牙补切"：那是重复去料。
        实测 R3/R12 各跑 3 次，体积全部与解析值一致到 0.000%。
        """
        import math as _m
        T = spec.thickness / 1000.0
        hl, hw = spec.length / 2000.0, spec.width / 2000.0
        r = min(spec.corner_radius / 1000.0, min(hl, hw) * 0.999)
        fm = doc.FeatureManager

        def _probe():
            """朝天取 4 条竖棱。失败时先重建、清选择、退出活动草图再试。"""
            got = []
            for sx in (1, -1):
                for sy in (1, -1):
                    doc.ClearSelection2(True)
                    ox = sx * (hl + 0.006)
                    oy = sy * (hw + 0.006)
                    try:
                        ok = doc.Extension.SelectByRay(
                            ox, oy, T / 2.0,
                            -sx / _m.sqrt(2.0), -sy / _m.sqrt(2.0), 0.0,
                            0.0015, 1, True, 0, 0)
                    except Exception:
                        ok = False
                    if ok:
                        try:
                            got.append(doc.SelectionManager.GetSelectedObject6(1, -1))
                        except Exception:
                            pass
            doc.ClearSelection2(True)
            return got

        edges = _probe()
        made = 0
        if len(edges) == 4:
            # 一次性选中 4 条边建一个特征：逐条建时第一条会改拓扑，
            # 后三条的边引用可能失效（实测 r4 R25/厚80 逐条 0/4，批量 4/4）
            for e in edges:
                try:
                    e.Select2(True, 0)      # append=True 累积选择
                except Exception:
                    pass
            n_sel = 0
            try:
                n_sel = doc.SelectionManager.GetSelectedObjectCount2(-1)
            except Exception:
                n_sel = len(edges)
            if n_sel >= 4:
                for fn, args in ((fm.FeatureFillet3, (195, r) + (0,) * 12),
                                 (fm.FeatureFillet2, (195, r) + (0,) * 9)):
                    try:
                        if fn(*args) is not None:
                            made = 4
                            break
                    except Exception:
                        pass
        if made < 4:
            # 批量失败才退回逐条（小件逐条是稳的）
            doc.ClearSelection2(True)
            made = 0
            for e in edges:
                doc.ClearSelection2(True)
                try:
                    if not e.Select2(False, 0):
                        continue
                    ok_one = False
                    for fn, args in ((fm.FeatureFillet3, (195, r) + (0,) * 12),
                                     (fm.FeatureFillet2, (195, r) + (0,) * 9)):
                        try:
                            if fn(*args) is not None:
                                ok_one = True
                                break
                        except Exception:
                            pass
                    if ok_one:
                        made += 1
                except Exception:
                    pass
        # 兜底：走完整流水线时 SW 会话偶发瞬时异常（实测 r4 单独跑 4/4、
        # 连续建 m1/m2/m3 后跑 0/4）。每轮强制重建 + 清状态后重探、重倒，最多 3 轮。
        for _round in range(2):
            if made >= 4:
                break
            doc.ClearSelection2(True)
            try:
                if doc.SketchManager.ActiveSketch is not None:
                    doc.SketchManager.InsertSketch(True)
            except Exception:
                pass
            try:
                doc.EditRebuild3()
            except Exception:
                pass
            de = _probe()
            if len(de) < 4:
                log(f"四角 R{spec.corner_radius:g}：重探只拿到 {len(de)} 条竖棱")
                continue
            edges = de
            doc.ClearSelection2(True)
            for e in edges:
                try:
                    e.Select2(True, 0)
                except Exception:
                    pass
            for fn, args in ((fm.FeatureFillet3, (195, r) + (0,) * 12),
                             (fm.FeatureFillet2, (195, r) + (0,) * 9)):
                try:
                    if fn(*args) is not None:
                        made = 4
                        break
                except Exception:
                    pass
            doc.ClearSelection2(True)
            if made >= 4:
                log(f"四角 R{spec.corner_radius:g}：第 {_round + 2} 轮重试成功")

        doc.ClearSelection2(True)
        log(f"四角 R{spec.corner_radius:g} 完成（{made}/{len(edges)} 条竖棱）")
        return made

    def _cut_cavity(self, doc, spec, log):
        """开口抽壳：从顶面把内腔整块切掉（深度 T−t），在倒外圆角之前做。

        基体都是等截面拉伸，这个实体与"真抽壳"完全等价；踩过的坑：
          - 用 SelectByRay 选顶面 + InsertFeatureShell：同一模型有时成功、
            有时报"非选择性的参数"、有时静默不生效，不可用；
          - 全封闭壳 + 切顶壁：切口与内腔壁共面，圆角矩形实测必失败；
          - 先倒外圆角再切同心内腔：圆角矩形必失败（"非选择性的参数"）；
          - 内腔折线取样 1.5°/3°（128~248 段）切除会失败，5°（约 80 段）稳定，
            面积误差 <0.001%（弦高 0.003mm 量级）。
        """
        t_mm = float(spec.shell_t)
        T = spec.thickness / 1000.0
        sm, fm = doc.SketchManager, doc.FeatureManager
        # 基体建在“前视基准面”（厚度沿 +Z），顶面是平行于前视面的平面。
        # 若这里用“上视基准面”偏移，偏移面与前视面垂直，切除方向会横穿“宽度”，
        # 实测 60×40×20 t2 会切掉 56×18×18=18,144（看似半深，实为方向错）。
        plane = self._plane(doc, BASE_PLANES)
        doc.ClearSelection2(True)
        plane.Select2(False, 0)
        tp = fm.InsertRefPlane(8, T, 0, 0, 0, 0)
        if tp is None:
            log("⚠ 抽壳内腔：参考面创建失败")
            return False
        doc.ClearSelection2(True)
        tp.Select2(False, 0)
        sm.InsertSketch(True)
        if spec.base_kind == "round":
            sm.CreateCircleByRadius(0.0, 0.0, 0.0,
                                    max(spec.diameter / 2.0 - t_mm, 0.05) / 1000.0)
        else:
            # 真圆弧 + 直线（不再做 5° 折线采样）
            _segs = self._mm_to_seg(inner_outline(spec, t_mm))
            _nseg = _draw_segs(sm, _segs)
            _got = -1
            try:
                _sk = sm.ActiveSketch
                _sksegs = _sk.GetSketchSegments
                _got = len(_sksegs) if _sksegs is not None else 0
            except Exception:
                _got = -1
            log("内腔轮廓：%d 段真圆弧/直线，草图里 %s 段" % (_nseg, _got if _got >= 0 else "?"))
        sm.InsertSketch(True)
        depth = max(T - t_mm / 1000.0, 1e-4)
        _v0 = 0.0
        try:
            _mp = doc.GetMassProperties
            _v0 = float(_mp[3]) * 1e9 if _mp else 0.0
        except Exception:
            _v0 = 0.0
        full = [True, False, False, 0, 0, depth, depth, False, False, False, False, 0, 0,
                False, False, False, False, False, True, True, True, True, True, 0, 0.0, False, True]
        f = None
        errs = []
        # 只允许完整 27 参数。短参数版本会静默生成半深切除，
        # 实测 26/25/24 不抛异常但只削掉 depth/2，绝不能当回退。
        try:
            f = fm.FeatureCut4(*full)
            log("FeatureCut4 参数数=27 返回 %s" % ("ok" if f is not None else "None"))
        except Exception as e:
            errs.append(str(e)[:70])
            f = None
        doc.ClearSelection2(True)
        if f is None:
            try:
                sm.InsertSketch(True)
            except Exception:
                pass
            log("⚠ 抽壳内腔切除失败" + ("｜" + " / ".join(dict.fromkeys(errs)) if errs else ""))
            return False
        _v1 = 0.0
        try:
            _mp = doc.GetMassProperties
            _v1 = float(_mp[3]) * 1e9 if _mp else 0.0
        except Exception:
            _v1 = 0.0
        _cut_v = _v0 - _v1
        log("抽壳 t%g（开口朝上：内腔切除，深 %.1f mm，削去 %s mm³）"
            % (t_mm, depth * 1000.0, f"{_cut_v:,.1f}"))
        return True

    def _shell(self, doc, spec, log):
        """抽壳。返回 "closed" / "open" / ""（失败）。

        封闭壳：InsertFeatureShell（不选面，稳定，实测体积与解析值一致到 0.000%）。
        开口壳：不用 InsertFeatureShell。原因是"全封闭壳 + 切顶壁"要求切口与内腔壁
        共面，SW 对 R5 圆角内腔实测必报"非选择性的参数"（外扩 5µm 也不行）。
        基体本身是等截面拉伸，所以"从顶面把内腔切掉 T−t 深"与抽壳是同一个实体，
        且切的是实心料、不存在共面退化。理论体积口径（shell_inner_area）完全一致。
        """
        t_mm = float(spec.shell_t)
        t_m = t_mm / 1000.0
        open_top = str(getattr(spec, "shell_open", "top") or "top").lower() != "none"
        doc.ClearSelection2(True)
        if open_top:
            return "open"
        v0 = 0.0
        try:
            mp = doc.GetMassProperties
            v0 = float(mp[3]) * 1e9 if mp else 0.0
        except Exception:
            pass
        last_err = ""
        for args in ((t_m, False), (t_m,)):
            try:
                doc.InsertFeatureShell(*args)
            except Exception as e:
                last_err = str(e)
                continue
            try:
                mp = doc.GetMassProperties
                v1 = float(mp[3]) * 1e9 if mp else 0.0
            except Exception:
                v1 = 0.0
            if v1 and v0 and v1 < v0 - 1.0:
                doc.ClearSelection2(True)
                log("抽壳 t%g 完成（全封闭）" % t_mm)
                return "closed"
            last_err = "体积未变化（%.1f）" % v1
        doc.ClearSelection2(True)
        log("⚠ 抽壳 t%g 未生效：%s" % (t_mm, last_err or "未知原因"))
        return ""

    def _corner_chips(self, doc, spec, log):
        """矩形基体的四角圆角：切 4 个"角点 − 四分之一圆"的月牙，厚度方向贯通。

        不用草图折线画圆角：SW 拉伸折线圆弧会让外形缩水（实测 120 → 118.10）。
        月牙多边形每角 60 段弦，弦高误差 R(1−cos(θ/2)) ≈ 0.0001mm。
        """
        import math as _m
        hl = spec.length / 2000.0
        hw = spec.width / 2000.0
        r = min(spec.corner_radius / 1000.0, min(hl, hw) * 0.999)
        sm, fm = doc.SketchManager, doc.FeatureManager
        plane = self._plane(doc, BASE_PLANES)
        made = 0
        for sx in (1, -1):
            for sz in (1, -1):
                C = (sx * hl, sz * hw)
                A = (sx * (hl - r), sz * hw)
                B = (sx * hl, sz * (hw - r))
                O = (sx * (hl - r), sz * (hw - r))
                a0 = _m.atan2(A[1] - O[1], A[0] - O[0])
                a1 = _m.atan2(B[1] - O[1], B[0] - O[0])
                if sx < 0 and sz < 0:
                    a1 = -_m.pi
                pts = [C, A]
                n = 60
                for i in range(1, n + 1):
                    a = a0 + (a1 - a0) * i / float(n)
                    pts.append((O[0] + r * _m.cos(a), O[1] + r * _m.sin(a)))
                pts.append(B)
                doc.ClearSelection2(True)
                try:
                    plane.Select2(False, 0)
                    sm.InsertSketch(True)
                    for i in range(len(pts)):
                        a, b = pts[i], pts[(i + 1) % len(pts)]
                        if abs(a[0] - b[0]) < 1e-9 and abs(a[1] - b[1]) < 1e-9:
                            continue
                        sm.CreateLine(a[0], a[1], 0, b[0], b[1], 0)
                    sm.InsertSketch(True)
                    f = fm.FeatureCut4(False, False, False, 1, 1, 0, 0,
                                       False, False, False, False, 0, 0,
                                       False, False, False, False, False,
                                       True, True, True, True, True, 0, 0.0, False, True)
                    if f is not None:
                        made += 1
                except Exception:
                    try:
                        sm.InsertSketch(True)
                    except Exception:
                        pass
                doc.ClearSelection2(True)
        log(f"四角 R{spec.corner_radius:g} 完成（{made}/4 角）")
        return made

    def _chamfer_by_cut(self, doc, spec, c_mm, log):
        """直角板顶面一圈倒角：原生 InsertFeatureChamfer + 4 条边一次全选。

        为什么不是"三角截面 + 布尔切"（旧实现的思路）：缺料的三角形整体落在
        实体外，SW 判草图无效、FeatureCut4 返回空，去料 0。

        实测（120×80×35，C1/C2，见 scripts/sw_cham_full.py）：
            去料 198.67 / 789.33，与精确式 (L+W−4c)c² + 8c³/3 完全一致。
        逐条边建只成 2/4（第一条改了拓扑后其余引用失效），必须一次全选。
        """
        c = c_mm / 1000.0
        T = spec.thickness / 1000.0
        fm = doc.FeatureManager
        edges = self._top_face_edges(doc, spec)
        if not edges:
            log(f"倒角 C{c_mm:g}：找不到顶面外轮廓边")
            return 0
        doc.ClearSelection2(True)
        n_sel = 0
        for e in edges:
            try:
                if e.Select2(True, 0):        # append=True 累积
                    n_sel += 1
            except Exception:
                pass
        try:
            n_sel = doc.SelectionManager.GetSelectedObjectCount2(-1) or n_sel
        except Exception:
            pass
        f = None
        # 8 个参数：(Options, ChamferType, Width, Angle, OtherDist, V1, V2, V3)
        # swChamferEqualDistance = 2，距离放 OtherDist。
        for args in ((0, 2, 0.0, 0.0, c, 0.0, 0.0, 0.0),
                     (0, 2, c, A, c, 0.0, 0.0, 0.0)):
            try:
                f = fm.InsertFeatureChamfer(*args)
                if f is not None:
                    break
            except Exception:
                f = None
        doc.ClearSelection2(True)
        if f is None:
            log(f"倒角 C{c_mm:g}：原生倒角建特征失败（选中 {n_sel} 条边）")
            return 0
        try:
            doc.ForceRebuild3(False)
        except Exception:
            pass
        log(f"倒角 C{c_mm:g} 完成（{n_sel} 条顶面棱，一次成型）")
        return n_sel

    def _apply_edge_feature(self, doc, spec, kind, size_mm, log):
        """对顶面所有外轮廓边做倒角/圆角：**先批量、失败再逐条**。

        逐条建的问题：第一条特征改了拓扑，后续边的引用失效。
        实测 m3 顶面圆角 R3 逐条只成 3/4；四角竖棱倒圆同样从 0/4 变 4/4（改批量后）。
        """
        val = size_mm / 1000.0
        done = 0
        failed = 0
        edges = self._top_face_edges(doc, spec)
        try:
            _m0 = doc.GetMassProperties
            _v0 = float(_m0[3]) * 1e9 if _m0 and len(_m0) > 3 else 0.0
        except Exception:
            _v0 = 0.0

        # ---- 批量：全部选中，建一个特征
        # 圆盘只有 1 条外圆边，也要走这里（圆盘单个边同样有"建成但不去料"的问题）
        if len(edges) >= 1:
            doc.ClearSelection2(True)
            for e in edges:
                try:
                    e.Select2(True, 0)          # append=True 累积
                except Exception:
                    pass
            try:
                n_sel = doc.SelectionManager.GetSelectedObjectCount2(-1)
            except Exception:
                n_sel = len(edges)
            f = None
            if kind == "chamfer":
                for args in ((0, 2, 0.0, 0.0, val, 0.0, 0.0, 0.0),
                             (0, 2, val, A, val, 0.0, 0.0, 0.0)):
                    try:
                        f = doc.FeatureManager.InsertFeatureChamfer(*args)
                        if f is not None:
                            break
                    except Exception:
                        f = None
            else:
                for fn, args in ((doc.FeatureManager.FeatureFillet3, (195, val) + (0,) * 12),
                                 (doc.FeatureManager.FeatureFillet2, (195, val) + (0,) * 9)):
                    try:
                        f = fn(*args)
                        if f is not None:
                            break
                    except Exception:
                        f = None
            doc.ClearSelection2(True)
            if f is not None:
                # 复核：SolidWorks 有时"建成特征但一点料都不去"（圆盘外圆 C1 实测如此）。
                # 只信 f is not None 会把漏做当成完成，必须量体积。
                try:
                    doc.ForceRebuild3(False)
                    _m = doc.GetMassProperties
                    _v1 = float(_m[3]) * 1e9 if _m and len(_m) > 3 else 0.0
                except Exception:
                    _v1 = 0.0
                if _v0 > 0 and _v1 > 0 and (_v0 - _v1) > 1e-6:
                    log(f"{'倒角' if kind == 'chamfer' else '圆角'} {size_mm:g} 完成"
                        f"（{n_sel} 条边，一次成型，去料 {_v0 - _v1:,.3f} mm³）")
                    return n_sel
                if _v0 > 0 and _v1 > 0:
                    log(f"⚠ {'倒角' if kind == 'chamfer' else '圆角'} {size_mm:g}："
                        f"特征建成但未去料（体积 {_v1:,.1f} 未变），改逐条重试")

        # ---- 兜底：逐条建
        for e in edges:
            doc.ClearSelection2(True)
            try:
                if not e.Select2(False, 0):
                    continue
                f = None
                if kind == "chamfer":
                    # InsertFeatureChamfer(Options, ChamferType, Width, Angle,
                    #                      OtherDist, V1, V2, V3)  ← 必须 8 个参数
                    # 等距倒角（45°）用 swChamferEqualDistance=2 + OtherDist=C；
                    # 旧代码传 7 个参数会报 DISP_E_PARAMNOTOPTIONAL。
                    for args in ((0, 2, 0.0, 0.0, val, 0.0, 0.0, 0.0),
                                 (0, 2, val, A, val, 0.0, 0.0, 0.0),
                                 (0, 0, val, A, val, 0.0, 0.0, 0.0)):
                        try:
                            f = doc.FeatureManager.InsertFeatureChamfer(*args)
                            if f is not None:
                                break
                        except Exception:
                            f = None
                else:
                    for fn, args in ((doc.FeatureManager.FeatureFillet3, (195, val) + (0,) * 12),
                                     (doc.FeatureManager.FeatureFillet2, (195, val) + (0,) * 9)):
                        try:
                            f = fn(*args)
                            if f is not None:
                                break
                        except Exception:
                            f = None
                if f is not None:
                    done += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
        doc.ClearSelection2(True)
        log(f"{'倒角' if kind == 'chamfer' else '圆角'} {size_mm:g} 完成（{done} 条边）"
            + (f"，{failed} 条失败" if failed else f"，共找到 {len(edges)} 条外轮廓边"))
        return done


    # ---------------------------------------------------------------- 圆顶面
    def _dome_vol(self, doc):
        try:
            doc.EditRebuild3()
        except Exception:
            pass
        try:
            m = doc.GetMassProperties
        except Exception:
            return 0.0
        return float(m[3]) * 1e9 if m and len(m) > 3 else 0.0

    def _dome_bbox(self, doc):
        """全部实体的包围盒（mm）：[xmin,ymin,zmin,xmax,ymax,zmax]。"""
        try:
            bodies = doc.GetBodies2(0, True)
        except Exception:
            return None
        if not bodies:
            return None
        lo = [1e9, 1e9, 1e9]
        hi = [-1e9, -1e9, -1e9]
        for b in bodies:
            try:
                bb = b.GetBodyBox()
            except Exception:
                continue
            if not bb or len(bb) < 6:
                continue
            for i in range(3):
                lo[i] = min(lo[i], float(bb[i]) * 1000.0)
                hi[i] = max(hi[i], float(bb[i + 3]) * 1000.0)
        return lo + hi if hi[0] >= lo[0] else None

    def _dome_revolve(self, doc, T, h, a, ell):
        """在「上视基准面」画母线并回转 360°，返回 (feature, err)。

        上视基准面实测局部 v -> 全局 -Z（MT ArrayData 第 8 位 = -1），
        所以剖面用 v = -z 表达；sign=+1 仅作模板差异兜底。

        椭圆顶：原生椭圆弧（CreateEllipticalArc，半轴 a×h）从轮缘 (a,zb)
                到顶点 (0,zt) 的四分之一弧 + 轴线，回转即上半椭球。
                坐标实测为「绝对点」而非向量；Direction=-1 选中经顶点的那一半。
        球冠  ：圆弧母线（R=(a²+h²)/2h，圆心在轮缘一侧)+轴线回转。
        两条路径都 Merge=True，直接与基体合成一个实体。
        """
        sm, fm = doc.SketchManager, doc.FeatureManager
        plane = self._plane(doc, ["上视基准面", "Top Plane"])
        if plane is None:
            return None, "找不到上视基准面"
        err = ""
        bb0 = self._dome_bbox(doc)
        for sign in (-1.0, 1.0):
            doc.ClearSelection2(True)
            plane.Select2(False, 0)
            sm.InsertSketch(True)
            zb = sign * T          # 底（零件顶面高度）
            zt = sign * (T + h)    # 顶（拱高）
            try:
                if ell:
                    # 四分之一椭圆弧：中心(0,zb)、长半轴 a 沿 u、短半轴 h 沿 v
                    # 参数为绝对点：major=(a,zb)，minor=(0,zt)；Direction 选经顶点的一半
                    sm.CreateEllipticalArc(
                        0.0, zb, 0.0,          # 椭圆中心
                        a, zb, 0.0,            # 长轴端点（绝对）
                        0.0, zt, 0.0,          # 短轴端点（绝对）
                        a, zb, 0.0,            # 弧起点（轮缘）
                        0.0, zt, 0.0,          # 弧终点（顶点）
                        -1 if sign < 0 else 1)  # 经顶点的那一半
                    sm.CreateLine(0.0, zt, 0.0, 0.0, zb, 0.0)
                else:
                    R = (a * a + h * h) / (2.0 * h)
                    vc = zb - sign * (R - h)   # 圆心在轮缘一侧（远离拱顶）
                    sm.CreateArc(0.0, vc, 0.0, 0.0, zt, 0.0, a, zb, 0.0,
                                 1 if sign < 0 else -1)
                    sm.CreateLine(0.0, zb, 0.0, a, zb, 0.0)
                    sm.CreateLine(0.0, zt, 0.0, 0.0, zb, 0.0)
                sm.CreateCenterLine(0.0, zb + sign * (-0.002), 0.0,
                                    0.0, zt - sign * (-0.002), 0.0)
            except Exception as e:
                err = str(e)[:120]
                try:
                    sm.InsertSketch(True)
                except Exception:
                    pass
                continue
            sm.InsertSketch(True)
            feat = None
            for rev in (False, True):
                try:
                    f = fm.FeatureRevolve2(True, True, False, False, rev, False,
                                           0, 0, 2 * math.pi, 0,
                                           False, False, 0, 0, 0, 0, 0,
                                           True, True, True)
                except Exception as e:
                    err = str(e)[:120]
                    f = None
                if f is not None:
                    feat = f
                    break
            doc.ClearSelection2(True)
            if feat is not None:
                bb = self._dome_bbox(doc)
                ok = bool(bb) and abs(bb[5] - (T + h) * 1000.0) < 0.05
                if ok and bb0:
                    # 矩形基体 X/Y 本来就大于拱半径；应检查回转没有把
                    # 基体外轮廓吃掉或撑大，而不是强制整件跨度等于 a。
                    ok = all(abs(bb[i] - bb0[i]) < 0.05 for i in range(5))
                    ok = ok and abs(bb[3] - bb0[3]) < 0.05
                if ok:
                    return feat, ""
                try:
                    doc.EditUndo2(2)
                except Exception:
                    pass
        return None, err

    def _dome(self, doc, spec, log):
        """圆顶面：CAD 原生曲线回转，不拆点、不拟合折线。

        椭圆顶 = 原生椭圆弧母线回转（半轴 a×h）；
        球冠   = 原生圆弧母线回转。
        InsertDome 在本机 SW2024 实测静默 no-op（特征数、体积均不变），不再使用。
        返回新增料体积（mm³），<=0 表示没做成。
        """
        h = float(getattr(spec, "dome_h", 0.0) or 0.0) / 1000.0
        if h <= 0:
            return 0.0
        T = float(spec.thickness) / 1000.0
        ell = bool(getattr(spec, "dome_elliptic", True))
        if spec.base_kind == "round":
            a = float(spec.diameter) / 2000.0
        else:
            a = min(float(spec.length or 0.0), float(spec.width or 0.0)) / 2000.0
        if a <= 0:
            log("⚠ 圆顶面：基体半径无效")
            return 0.0

        v0 = self._dome_vol(doc)
        feat, err = self._dome_revolve(doc, T, h, a, ell)
        if feat is None:
            log("⚠ 圆顶面：回转失败%s" % (("｜" + err) if err else ""))
            return 0.0

        dv = self._dome_vol(doc) - v0
        bb = self._dome_bbox(doc)
        if bb and abs(bb[5] - (T + h) * 1000.0) > 0.05:
            log("⚠ 圆顶面：包围盒顶高 %.2f，应 %.2f" % (bb[5], (T + h) * 1000.0))
            return 0.0

        # 体积必须来自 CAD 实体实测；与解析值互校，防「特征建出来但形状错」
        v_theory = ((2.0 / 3.0) * math.pi * a * a * h if ell
                    else math.pi * h * (3.0 * a * a + h * h) / 6.0) * 1e9
        if v_theory > 0 and abs(dv - v_theory) / v_theory > 0.01:
            log("⚠ 圆顶面：实测料 %.1f 与理论 %.1f 差 %.2f%%，判失败"
                % (dv, v_theory, (dv - v_theory) / v_theory * 100.0))
            return 0.0

        try:
            if len(doc.GetBodies2(0, True) or []) > 1:
                log("⚠ 圆顶面：仍有多个实体，未完全合并")
        except Exception:
            pass

        log("圆顶面完成：h=%g（%s），实测料 +%s mm³（理论 %s）"
            % (float(getattr(spec, "dome_h", 0.0)),
               "elliptic" if ell else "spherical",
               f"{dv:,.1f}", f"{v_theory:,.1f}"))
        return dv

    def _sphere_cut(self, doc, spec, f, log):
        """球坑：前视基准面偏移到球心高度，原生半圆母线回转切除 360°。

        为什么不用顶面草图：球心在顶面之上（z_c = T - h + R ≥ T），
        顶面内的圆弧绕平面内竖轴旋转不会得到球面。把草图放到 z = z_c
        的平行面上，半圆的直径线就是回转轴，旋转 360° 精确得到球体；
        球体与材料相减只剩球冠（h ≤ R 时球心在顶面之上）。
        全程 CreateArc + CreateCenterLine + FeatureRevolve2，无折线采样。
        """
        T = spec.thickness / 1000.0
        R = (f.diameter or 0.0) / 2000.0
        if R <= 0:
            log("⚠ 球坑直径无效，跳过")
            return False
        h = min(float(f.depth or f.diameter / 2.0) / 1000.0, R)
        zc = T - h + R
        cx, cy = f.x / 1000.0, f.y / 1000.0
        sm, fm = doc.SketchManager, doc.FeatureManager
        base = self._plane(doc, BASE_PLANES)
        if base is None:
            log("⚠ 球坑：找不到前视基准面")
            return False
        doc.ClearSelection2(True)
        base.Select2(False, 0)
        pl = fm.InsertRefPlane(8, zc, 0, 0, 0, 0)
        if pl is None:
            log("⚠ 球坑：球心高度参考面创建失败")
            return False
        doc.ClearSelection2(True)
        pl.Select2(False, 0)
        sm.InsertSketch(True)
        # 上半圆（y ≥ cy）+ 直径闭合线 + 回转中心线（与直径重合）
        sm.CreateArc(cx, cy, 0.0, cx + R, cy, 0.0, cx - R, cy, 0.0, 1)
        sm.CreateLine(cx - R, cy, 0.0, cx + R, cy, 0.0)
        sm.CreateCenterLine(cx - R - 0.001, cy, 0.0, cx + R + 0.001, cy, 0.0)
        sm.InsertSketch(True)
        v0 = 0.0
        try:
            mp = doc.GetMassProperties
            v0 = float(mp[3]) * 1e9 if mp else 0.0
        except Exception:
            pass
        feat = None
        err = ""
        for rev in (False, True):
            try:
                feat = fm.FeatureRevolve2(True, True, False, True, rev, False,
                                          0, 0, 2 * math.pi, 0,
                                          False, False, 0, 0, 0, 0, 0,
                                          True, True, True)
            except Exception as e:
                err = str(e)[:100]
                feat = None
            if feat is not None:
                break
            try:
                if sm.ActiveSketch is not None:
                    sm.InsertSketch(True)
            except Exception:
                pass
        try:
            doc.ClearSelection2(True)
        except Exception:
            pass
        v1 = 0.0
        try:
            mp = doc.GetMassProperties
            v1 = float(mp[3]) * 1e9 if mp else 0.0
        except Exception:
            pass
        if feat is None or v0 <= 0 or (v0 - v1) <= 1e-6:
            log("⚠ 球坑 SR%g@(%g,%g) 回转切除失败%s" %
                (f.diameter / 2.0, f.x, f.y, ("｜" + err) if err else ""))
            return False
        log("球坑 SR%g@(%g,%g) 深 %g 完成（原生半圆回转，削去 %s mm³）" %
            (f.diameter / 2.0, f.x, f.y, h * 1000.0, f"{v0 - v1:,.1f}"))
        return True

    def _revolve(self, doc, spec, log) -> bool:
        """通用回转基体：上视基准面画原生直线/真圆弧母线，绕竖轴 360°。

        母线用 (r,z) 表达，草图局部 u=r、v=-z（上视基准面实测 v→-Z）。
        圆弧走 CreateArc、直线走 CreateLine，绝不点列拟合。
        母线须闭合；r≈0 的轴线段可标为构造线，轴线由中心线承担。
        """
        from ..calc import revolve_bbox, revolve_volume
        prof = [dict(x) for x in (getattr(spec, "revolve_profile", None) or [])]
        if not prof:
            return False
        sm, fm = doc.SketchManager, doc.FeatureManager
        plane = self._plane(doc, ["上视基准面", "Top Plane"])
        if plane is None:
            log("⚠ 回转：找不到上视基准面")
            return False

        def _p(seg, key):
            q = seg.get(key) or [0.0, 0.0]
            return float(q[0]) / 1000.0, float(q[1]) / 1000.0

        zs = []
        for seg in prof:
            for k in ("p1", "p2"):
                q = seg.get(k)
                if q:
                    zs.append(float(q[1]))
        if not zs:
            log("⚠ 回转：母线为空")
            return False
        z0, z1 = min(zs) / 1000.0, max(zs) / 1000.0

        bbw = revolve_bbox(spec)
        vexp = revolve_volume(spec) or 0.0

        def _close_contour() -> int:
            """把坐标重合的草图端点并成真正的闭合轮廓。

            关掉自动推断后各段端点坐标是准的（C1 的 1mm 不会被吸掉），
            但 SW 仍把每段当独立线段，回转特征不会把它认成闭合轮廓。
            给重合端点加一次重合约束触发求解，SW 会把同坐标端点并成一个点。
            """
            try:
                sk = sm.ActiveSketch
            except Exception:
                return 0
            added = 0
            for _ in range(64):
                try:
                    pts = sk.GetSketchPoints2
                except Exception:
                    break
                groups = {}
                for p in pts:
                    try:
                        k = (round(float(p.X) * 1e6, 3),
                             round(float(p.Y) * 1e6, 3))
                    except Exception:
                        continue
                    groups.setdefault(k, []).append(p)
                pair = next(((k, v) for k, v in groups.items() if len(v) > 1), None)
                if pair is None:
                    break
                _, v = pair
                try:
                    doc.ClearSelection2(True)
                    v[0].Select2(False, 0)
                    for q in v[1:]:
                        q.Select2(True, 0)
                    doc.SketchAddConstraints("sgCOINCIDENT")
                    added += 1
                except Exception:
                    break
            return added

        def _draw(mark_axis: bool):
            doc.ClearSelection2(True)
            plane.Select2(False, 0)
            try:
                _infer0 = bool(sm.InferenceMode)
            except Exception:
                _infer0 = True
            # 草图坐标以米为单位，SW 的自动推断容差约 1mm：
            # C1 只有 1mm，会被推断吸成水平线（实测）。画母线期间关掉推断。
            try:
                sm.InferenceMode = False
            except Exception:
                pass
            sm.InsertSketch(True)
            nline = narc = 0
            for seg in prof:
                typ = str(seg.get("type") or "line").lower()
                r1, zz1 = _p(seg, "p1")
                r2, zz2 = _p(seg, "p2")
                u1, v1 = r1, -zz1
                u2, v2 = r2, -zz2
                if typ == "arc":
                    cr, cz = _p(seg, "center")
                    d = int(seg.get("direction") or 1)
                    # (r,z) 逆时针在 (u,v)=(r,-z) 里变成顺时针
                    sm.CreateArc(cr, -cz, 0.0, u1, v1, 0.0, u2, v2, 0.0, -d)
                    narc += 1
                else:
                    if abs(u1 - u2) < 1e-12 and abs(v1 - v2) < 1e-12:
                        continue
                    ln = sm.CreateLine(u1, v1, 0.0, u2, v2, 0.0)
                    if mark_axis and r1 < 1e-9 and r2 < 1e-9:
                        try:
                            ln.ConstructionGeometry = True
                        except Exception:
                            pass
                    nline += 1
            ncoin = _close_contour()
            sm.CreateCenterLine(0.0, -z0, 0.0, 0.0, -z1, 0.0)
            try:
                sm.InferenceMode = _infer0
            except Exception:
                pass
            sm.InsertSketch(True)
            return nline, narc, ncoin

        err = ""
        base_prof = list(prof)
        nseg = len(base_prof)
        rots = [0]
        for k in (1, 2, 3, nseg // 2, nseg - 1):
            if 0 < k < nseg and k not in rots:
                rots.append(k)
        best = None
        for rot in rots:
            prof = (base_prof[rot:] + base_prof[:rot]) if rot else base_prof
            attempts = (False, True) if rot == 0 else (False,)
            for mark_axis in attempts:
                nline, narc, ncoin = _draw(mark_axis)
                feat = None
                for rev in (False, True):
                    try:
                        f = fm.FeatureRevolve2(True, True, False, False, rev, False,
                                               0, 0, 2 * math.pi, 0,
                                               False, False, 0, 0, 0, 0, 0,
                                               True, True, True)
                    except Exception as e:
                        err = str(e)[:160]
                        f = None
                    if f is not None:
                        feat = f
                        break
                doc.ClearSelection2(True)
                if feat is None:
                    continue
                got = self._dome_bbox(doc)
                ok = bool(got)
                if ok and bbw:
                    ok = (abs(got[0] - bbw[0]) < 0.05 and abs(got[3] - bbw[1]) < 0.05
                          and abs(got[1] - bbw[2]) < 0.05 and abs(got[4] - bbw[3]) < 0.05
                          and abs(got[2] - bbw[4]) < 0.05 and abs(got[5] - bbw[5]) < 0.05)
                vgot = self._dome_vol(doc)
                rel = (abs(vgot - vexp) / vexp) if (vexp > 0 and vgot > 0) else 1.0
                log("revolve try rot=%d axis=%s bbox_ok=%s vol=%.1f rel=%.4f%%" %
                    (rot, mark_axis, ok, vgot, rel * 100.0))
                if not ok:
                    log("revolve bbox mismatch: body %s / profile %s" %
                        ([round(x, 3) for x in (got or [])],
                         [round(x, 3) for x in (bbw or [])]))
                    try:
                        doc.EditUndo2(2)
                    except Exception:
                        pass
                    continue
                if best is None or rel < best[0] - 1e-9:
                    best = (rel, rot, mark_axis, vgot, nline, narc)
                if rel <= 1e-3:
                    log(f"回转基体完成：母线 {nseg} 段（直线 {nline}"
                        + (f" + 真圆弧 {narc}" if narc else "") + "）绕竖轴 360°，"
                        + (f"闭合约束 {ncoin} 条，" if ncoin else "")
                        + f"实测 {vgot:,.1f} mm³（母线解析 {vexp:,.1f}）"
                        + (f"，起点旋转 {rot} 段" if rot else ""))
                    return True
                err = (f"实体 {vgot:,.1f} vs 母线解析 {vexp:,.1f}"
                       f"（{(vgot - vexp) / vexp * 100.0:+.2f}%）")
                try:
                    doc.EditUndo2(2)
                except Exception:
                    pass
        log("revolve best=%r" % (best,))
        if best is not None and best[0] <= 0.01:
            rel, rot, mark_axis, vgot, nline, narc = best
            prof = (base_prof[rot:] + base_prof[:rot]) if rot else base_prof
            log("revolve fallback redraw rot=%d axis=%s" % (rot, mark_axis))
            _draw(mark_axis)
            feat = None
            for rev in (False, True):
                try:
                    feat = fm.FeatureRevolve2(True, True, False, False, rev, False,
                                              0, 0, 2 * math.pi, 0,
                                              False, False, 0, 0, 0, 0, 0,
                                              True, True, True)
                except Exception as e:
                    err = str(e)[:160]
                    feat = None
                if feat is not None:
                    break
            doc.ClearSelection2(True)
            if feat is not None:
                vgot = self._dome_vol(doc)
                log(f"⚠ 回转基体：各起点最小误差仍 {rel * 100.0:.2f}%，"
                    f"采用旋转 {rot} 段的结果（实测 {vgot:,.1f} mm³）")
                return True
        log("⚠ 回转基体失败%s" % (("｜" + err) if err else ""))
        return False

    def _sweep(self, doc, spec, log) -> int:
        """原生扫掠：路径草图=真直线+真圆弧，截面草图=矩形，SW 原生 Sweep。

        只支持路径整体落在 XZ 平面（所有点同一 Y）、首段切向 ±X 的情形。
        截面平面用右视基准面（法向 X），局部坐标 u->Z、v->Y。
        不做点列拟合，圆弧直接 CreateArc。
        """
        sws = list(getattr(spec, "sweeps", None) or [])
        if not sws:
            return 0
        sm, fm = doc.SketchManager, doc.FeatureManager
        top = self._plane(doc, ["上视基准面", "Top Plane"])
        right = self._plane(doc, ["右视基准面", "Right Plane"])
        if top is None or right is None:
            log("⚠ 扫掠：找不到上视/右视基准面")
            return 0
        made = 0
        for idx, item in enumerate(sws, 1):
            w = float(item.get("profile_w") or 0.0) / 1000.0
            h = float(item.get("profile_h") or 0.0) / 1000.0
            path = [dict(x) for x in (item.get("path") or [])]
            if w <= 0 or h <= 0 or not path:
                log(f"⚠ 扫掠 {idx}：截面或路径为空，跳过")
                continue
            pts = []
            for seg in path:
                for k in ("p1", "p2", "center"):
                    q = seg.get(k)
                    if q:
                        pts.append([float(v) for v in q[:3]])
            if not pts:
                continue
            ys = [q[1] for q in pts]
            if max(ys) - min(ys) > 1e-6:
                log(f"⚠ 扫掠 {idx}：路径不在 XZ 平面，暂不支持")
                continue
            y0 = ys[0] / 1000.0

            def _ends(seg):
                a = [float(v) for v in (seg.get("p1") or [0, 0, 0])[:3]]
                b = [float(v) for v in (seg.get("p2") or [0, 0, 0])[:3]]
                return a, b

            a0, b0 = _ends(path[0])
            dx, dz = b0[0] - a0[0], b0[2] - a0[2]
            if abs(dx) < 1e-9 or abs(dx) < abs(dz):
                log(f"⚠ 扫掠 {idx}：起始切向不是 ±X，暂不支持")
                continue
            if dx < 0:
                rev = []
                for seg in reversed(path):
                    r = dict(seg)
                    r["p1"], r["p2"] = seg.get("p2"), seg.get("p1")
                    if str(seg.get("type") or "").lower() == "arc":
                        r["direction"] = -int(seg.get("direction") or 1)
                    rev.append(r)
                path = rev
                a0, b0 = _ends(path[0])
                dx, dz = b0[0] - a0[0], b0[2] - a0[2]
            x_start = a0[0] / 1000.0
            z_start = a0[2] / 1000.0

            # ---- 路径草图：上视基准面（法向 +Y），偏移到 y0
            doc.ClearSelection2(True)
            top.Select2(False, 0)
            pplane = top if abs(y0) < 1e-9 else fm.InsertRefPlane(8, y0, 0, 0, 0, 0)
            if pplane is None:
                log(f"⚠ 扫掠 {idx}：路径基准面创建失败")
                continue
            doc.ClearSelection2(True)
            pplane.Select2(False, 0)
            sm.InsertSketch(True)
            nseg = 0
            for seg in path:
                typ = str(seg.get("type") or "line").lower()
                a, b = _ends(seg)
                if typ == "arc":
                    c = [float(v) for v in (seg.get("center") or [0, 0, 0])[:3]]
                    ax, ay = a[0] / 1000.0, -a[2] / 1000.0
                    bx, by = b[0] / 1000.0, -b[2] / 1000.0
                    cx, cy = c[0] / 1000.0, -c[2] / 1000.0
                    # 世界 XZ 的 CCW 在草图 (x,-z) 里变 CW
                    sm.CreateArc(cx, cy, 0.0, ax, ay, 0.0, bx, by, 0.0,
                                 -int(seg.get("direction") or 1))
                else:
                    sm.CreateLine(a[0] / 1000.0, -a[2] / 1000.0, 0.0,
                                  b[0] / 1000.0, -b[2] / 1000.0, 0.0)
                nseg += 1
            sm.InsertSketch(True)
            try:
                pname = fm.GetFeatures(True)[-1].Name
            except Exception:
                pname = ""
            if not pname:
                log(f"⚠ 扫掠 {idx}：路径草图未生成")
                continue

            # ---- 截面草图：右视基准面（法向 +X），偏移到起点 x
            doc.ClearSelection2(True)
            right.Select2(False, 0)
            qplane = right if abs(x_start) < 1e-9 else fm.InsertRefPlane(8, x_start, 0, 0, 0, 0)
            if qplane is None:
                log(f"⚠ 扫掠 {idx}：截面基准面创建失败")
                continue
            doc.ClearSelection2(True)
            qplane.Select2(False, 0)
            sm.InsertSketch(True)
            u0, v0 = -z_start, y0
            sm.CreateCenterRectangle(u0, v0, 0.0,
                                     u0 + h / 2.0, v0 + w / 2.0, 0.0)
            sm.InsertSketch(True)
            try:
                sname = fm.GetFeatures(True)[-1].Name
            except Exception:
                sname = ""
            if not sname:
                log(f"⚠ 扫掠 {idx}：截面草图未生成")
                continue

            feat = None
            for mprof, mpath in ((1, 4), (4, 1), (0, 0)):
                doc.ClearSelection2(True)
                try:
                    fp = doc.FeatureByName(sname)
                    fq = doc.FeatureByName(pname)
                    fp.Select2(False, mprof)
                    fq.Select2(True, mpath)
                    feat = fm.InsertProtrusionSwept4(
                        True, 0, 0, False, True, 0, 0, False,
                        0.0, 0.0, 0, 0, True, True, True,
                        0.0, True, False, 0.0, 0)
                except Exception:
                    feat = None
                if feat is not None:
                    break
            if feat is None:
                log(f"⚠ 扫掠 {idx}：原生扫掠失败")
                continue
            made += 1
            log(f"扫掠 {idx} 完成：截面 {w * 1000:g}×{h * 1000:g}，"
                f"路径 {nseg} 段（真直线+真圆弧）")
        return made

    def export_step(self, path: Path, stp: Path,
                    log: Callable[[str], None] = lambda m: None) -> bool:
        """用 SolidWorks 把 SLDPRT / STEP / IGES 另存为 STEP。

        - 进单实例闸门（关闭 NX），保证同时只有一个 CAD 在跑
        - SLDPRT 走 OpenDoc6(type=1)，STEP/IGES 交给 SW 自己转
        - 只关闭本次打开的文档；如果文档已在 SW 里打开，直接复用
        """
        path = Path(path).resolve()
        stp = Path(stp).resolve()
        stp.parent.mkdir(parents=True, exist_ok=True)
        ok, msg = _mem_check(1200.0, 92.0, "SolidWorks")
        if not ok:
            try:
                from .guard import free_others as _free_others
                _free_others("SolidWorks", log, fast=True)
            except Exception:
                pass
            ok, msg = _mem_check(1200.0, 92.0, "SolidWorks")
        if not ok:
            log("\u26a0 " + msg)
            return False
        if msg:
            log(msg)
        if not path.exists():
            log(f"文件不存在：{path}")
            return False

        import pythoncom
        from win32com.client import VARIANT
        import win32com.client as win32

        app = None
        doc = None
        opened_here = False
        pythoncom.CoInitialize()
        try:
            _cad_acquire("SolidWorks", log)
            try:
                _dismiss_startup_prompts(6.0)
            except Exception:
                pass
            app = win32.Dispatch("SldWorks.Application")
            try:
                app.Visible = False
            except Exception:
                pass
            # 冷启动后立刻 OpenDoc6 / SaveAs3 会偶发 RPC 0x800706BE（COM 未就绪）。
            import time as _t
            _t.sleep(1.5)

            for _attempt in range(2):
                # 文档已在同一 SW 进程里打开时，OpenDoc6 会返回 err=65536
                # （swFileWithSameTitleAlreadyOpen），此时直接复用现有文档。
                try:
                    doc = app.GetOpenDocumentByName(str(path))
                except Exception:
                    doc = None
                if doc is not None:
                    log(f"SolidWorks 复用已打开文档：{path.name}")
                    opened_here = False
                    break
                err = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
                warn = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
                log(f"SolidWorks 打开 {path.name} …")
                doc = app.OpenDoc6(str(path), 1, 1, "", err, warn)
                if doc is not None:
                    opened_here = True
                    break
                ev = int(err.value or 0)
                if ev == 65536:
                    try:
                        doc = app.GetOpenDocumentByName(str(path))
                    except Exception:
                        doc = None
                    if doc is not None:
                        log(f"SolidWorks 复用已打开文档：{path.name}")
                        opened_here = False
                        break
                if _attempt == 0:
                    log(f"SolidWorks 打开失败（错误码 {ev}），2 秒后重试一次")
                    _t.sleep(2.0)
                    continue
                log(f"SolidWorks 打不开 {path.name}，错误码 {ev}")
                return False
            if doc is None:
                return False

            try:
                doc.EditRebuild3()
            except Exception:
                pass
            if stp.exists():
                try:
                    stp.unlink()
                except Exception:
                    pass
            ok_save = False
            last = None
            for _attempt in range(2):
                try:
                    # Options=1（Silent）。Options=2（Copy）对「打开已有文档再导出
                    # STEP」会让 SW 进程直接崩掉（实测 RPC 0x800706BE）。
                    r = doc.SaveAs3(str(stp), 0, 1)
                    if r in (0, None) or stp.exists():
                        ok_save = True
                        break
                    last = f"返回码 {r}"
                except Exception as e:
                    last = str(e)
                    # 进程若被重启，重取一次 app / doc 再试
                    try:
                        app = win32.GetActiveObject("SldWorks.Application")
                        app.Visible = False
                        doc = app.GetOpenDocumentByName(str(path))
                    except Exception:
                        doc = None
                    if doc is None or _attempt == 1:
                        break
                    _t.sleep(2.0)
            if not ok_save:
                log(f"SolidWorks 导出 STEP 失败：{last}")
                return False
            if stp.exists() and stp.stat().st_size > 500:
                log(f"已导出 STEP（{stp.stat().st_size // 1024} KB）")
                return True
            log("SolidWorks 导出 STEP 未生成有效文件")
            return False
        except Exception as e:
            log(f"SolidWorks 导出 STEP 异常：{e}")
            return False
        finally:
            try:
                if doc is not None and opened_here:
                    app.CloseDoc(doc.GetTitle)
            except Exception:
                pass
            try:
                _cad_release(log)
            except Exception:
                pass
            pythoncom.CoUninitialize()

    def build(self, spec: PartSpec, out_dir: str,
              log: Callable[[str], None] = lambda m: None) -> BuildResult:
        t0 = time.time()
        res = BuildResult(cad="SolidWorks", expected=0.0)
        _ok, _msg = _mem_check(1200.0, 92.0, "SolidWorks")
        if not _ok:
            # 内存不够时，先把**其它** CAD 关掉（用户要求同时只开一个），
            # 再重判一次；否则 SW 占着的 500+ MB 会让 NX 永远起不来。
            try:
                from .guard import free_others as _free_others
                _free_others("SolidWorks", log, fast=True)
            except Exception:
                pass
            _ok, _msg = _mem_check(1200.0, 92.0, "SolidWorks")
        if not _ok:
            res.message = _msg
            log("\u26a0 " + _msg)
            res.elapsed = time.time() - t0
            return res
        if _msg:
            log(_msg)
        # COM 库只在内存闸门放行后再导入：低内存时连 pythoncom 都不碰。
        import pythoncom
        import win32com.client as win32

        res.expected = part_volume(spec)
        out = Path(out_dir).resolve()
        out.mkdir(parents=True, exist_ok=True)
        app = None
        doc = None
        pythoncom.CoInitialize()
        try:
            _cad_acquire("SolidWorks", log)
            log("连接 SolidWorks…")
            _n = _dismiss_startup_prompts(6.0)
            # 清会话残留：连续建模时上一个零件的文档/选择/活动草图会污染本次
            # 的 SelectByRay（实测 r4 单独跑 4/4，在 m1/m2/m3 之后跑 0/4）。
            try:
                for _ in range(12):
                    _d = app.ActiveDoc
                    if _d is None:
                        break
                    try:
                        app.CloseDoc(_d.GetTitle)
                    except Exception:
                        break
            except Exception:
                pass
            if _n:
                log(f"已关闭 SolidWorks 启动提示 {_n} 个")
            app = win32.Dispatch("SldWorks.Application")
            try:
                app.Visible = False
            except Exception:
                pass
            doc = None
            for _try in range(3):
                try:
                    doc = app.NewDocument(self._tpl(), 0, 0, 0)
                except Exception as _e:
                    if _try == 2:
                        raise
                    # 启动提示可能在 Dispatch 之后才弹（实测 8s 延迟）
                    if _dismiss_startup_prompts(4.0):
                        log("已关闭 SolidWorks 启动提示（重试新建）")
                    time.sleep(1.5)
                if doc is not None:
                    break
            if doc is None:
                raise RuntimeError("SolidWorks 无法新建零件（模板路径可能不对）")
            sm = doc.SketchManager
            fm = doc.FeatureManager
            try:
                doc.ClearSelection2(True)
                if sm.ActiveSketch is not None:
                    sm.InsertSketch(True)
            except Exception:
                pass

            T = spec.thickness / 1000.0
            L = spec.length / 1000.0
            W = spec.width / 1000.0
            plane = self._plane(doc, BASE_PLANES)
            if plane is None:
                raise RuntimeError("找不到基准面")

            # ---------------- base
            corner_fail = []
            doc.ClearSelection2(True)
            shell_mode = ""
            if spec.sheet_t > 0 and spec.sheet_L > 0:
                self._build_sheet(doc, spec, log)
            elif spec.teeth > 0 and spec.module > 0:
                self._build_gear(doc, spec, log)
            elif spec.shaft_segments:
                self._build_shaft(doc, spec, log)
            elif getattr(spec, "revolve_profile", None):
                if not self._revolve(doc, spec, log):
                    raise RuntimeError("通用回转基体创建失败")
            else:
              plane.Select2(False, 0)
              sm.InsertSketch(True)
              if spec.base_kind == "round":
                sm.CreateCircleByRadius(0, 0, 0, spec.diameter / 2000.0)
                log(f"绘制圆盘 Ø{spec.diameter:g}")
              elif (spec.corner_radius > 0 and not self._arc_ends(spec)
                    and not getattr(spec, "base_profile", None)):
                # 直角矩形：四角圆角改用"拉伸后给 4 条竖棱倒圆角"（见 _fillet_corners）。
                # 折线把圆角画进草图在 R≥10 时结果不稳定，实测同一 spec 两次差 181 mm³。
                sm.CreateCenterRectangle(0, 0, 0, L / 2.0, W / 2.0, 0)
                log(f"绘制矩形 {spec.length:g}×{spec.width:g}（圆角 R{spec.corner_radius:g} 由竖棱倒圆实现）")
              else:
                _nseg = _draw_segs(sm, _base_segs_m(spec))
                log(f"绘制轮廓 {spec.length:g}×{spec.width:g}"
                    + f"（真圆弧+直线 {_nseg} 段）"
                    + (f"（R{spec.corner_radius:g}）" if spec.corner_radius > 0 else ""))
            if not (spec.shaft_segments or spec.teeth or (spec.sheet_t > 0 and spec.sheet_L > 0)
                    or getattr(spec, "revolve_profile", None)):
                sm.InsertSketch(True)
                boss = fm.FeatureExtrusion2(True, False, False, 0, 0, T, T,
                                            False, False, False, False, A, A,
                                            False, False, False, False,
                                            True, True, True, 0, 0, False)
                if boss is None:
                    raise RuntimeError("基体拉伸失败")
                log("基体拉伸完成")
                # 顺序：先四角倒圆，再抽壳。SW 抽壳是等壁厚偏移，
                # 外角 R 会自然带出内角 R−t；反过来内腔四角会是尖角，
                # 与理论体积差 (4−π)(R²−(R−t)²)·H。
                if float(getattr(spec, "shell_t", 0.0) or 0.0) > 0:
                    shell_mode = self._shell(doc, spec, log)
                    if shell_mode == "open" and not self._cut_cavity(doc, spec, log):
                        corner_fail.append("抽壳内腔")
                        shell_mode = ""
                # 圆角放在内腔切除之后：先切腔再倒外角，SW 对"有外圆角的体上切同心
                # 内腔"会报"非选择性的参数"（R5 实测必现），反过来则稳定。
                if spec.base_kind == "rect" and spec.corner_radius > 0 \
                        and not self._arc_ends(spec):
                    _made = self._fillet_corners(doc, spec, log)
                    if _made < 4:
                        # 角圆没做全 = 外形错。绝不能静默放过（SW 有时建成特征但一点料都不去）。
                        corner_fail.append(f"四角 R{spec.corner_radius:g}（只完成 {_made}/4）")

                # 圆顶面：赶在任何切除之前做，此时顶面还是一张完整平面。
                if float(getattr(spec, "dome_h", 0.0) or 0.0) > 0:
                    if self._dome(doc, spec, log) <= 0:
                        corner_fail.append("圆顶面 Dome")

            # ---------------- native sweeps (additive)
            if getattr(spec, "sweeps", None):
                _nsw = self._sweep(doc, spec, log)
                if _nsw < len(spec.sweeps):
                    corner_fail.append(f"扫掠 {_nsw}/{len(spec.sweeps)}")

            # ---------------- shared top plane
            doc.ClearSelection2(True)
            top_plane = None
            # ????????? Z????? z=T ??????
            # ??? revolve_profile ????????????????????
            # ?????????????? z=0 ????FeatureCut4 ????????
            if not spec.shaft_segments:
                for _try in range(2):
                    doc.ClearSelection2(True)
                    try:
                        plane.Select2(False, 0)
                        top_plane = fm.InsertRefPlane(8, T, 0, 0, 0, 0)
                    except Exception as _e:
                        top_plane = None
                        log(f"参考面创建异常：{str(_e)[:70]}")
                    if top_plane is not None:
                        break
                    try:
                        doc.ForceRebuild3(False)
                    except Exception:
                        pass
                if top_plane is None:
                    log("⚠ 顶面参考面创建失败，改为一律用射线选顶面（可能影响多特征件）")

            def sketch_on_top():
                """在 z=T 的顶面上开草图。

                优先用参考面；参考面建不出来时退回 SelectByRay 选顶面。
                之前无条件 top_plane.Select2() 会在 InsertRefPlane 返回 None 时
                让每个孔都抛 'NoneType' has no attribute 'Select2'（端盖 4 孔 ×4 次）。
                """
                doc.ClearSelection2(True)
                if top_plane is not None:
                    try:
                        top_plane.Select2(False, 0)
                        sm.InsertSketch(True)
                        return
                    except Exception:
                        pass
                    doc.ClearSelection2(True)
                # 退回：从内部朝天打射线，命中顶面后直接在新面上开草图
                _hit = False
                for _fx in (0.0, 0.25, -0.25):
                    for _fy in (0.0, 0.25, -0.25):
                        try:
                            _x = getattr(spec, "length", 0) / 2000.0 * _fx
                            _y = getattr(spec, "width", 0) / 2000.0 * _fy
                            if doc.Extension.SelectByRay(_x, _y, T + 0.01,
                                                         0.0, 0.0, -1.0,
                                                         0.0006, 2, False, 0, 0):
                                _hit = True
                                break
                        except Exception:
                            continue
                    if _hit:
                        break
                if not _hit:
                    # 最后兜底：用基体基准面本身上草图（浅特征仍可能成功）
                    try:
                        plane.Select2(False, 0)
                    except Exception:
                        pass
                sm.InsertSketch(True)

            def sketch_on_bottom():
                """在 z=0 的底面上开草图（face=bottom 的槽/腔用）。"""
                doc.ClearSelection2(True)
                try:
                    plane.Select2(False, 0)
                except Exception:
                    pass
                sm.InsertSketch(True)

            failed = list(corner_fail)
            _last_errs = []
            def cut(depth_m: float, name: str, record: bool = True,
                    quiet: bool = False, flip: bool = False):
                sm.InsertSketch(True)
                # FeatureCut4(Sd, Flip, Dir, ...)：底面开槽时草图在 z=0 平面上，
                # 要把"方向"给到第 3 个参数 Dir，而不是第 2 个 Flip（Flip 是"反向切除侧"）
                full = [True, False, bool(flip), 0, 0, depth_m, depth_m,
                        False, False, False, False, A, A,
                        False, False, False, False, False,
                        True, True, True, True, True, 0, 0.0, False, True]
                f = None
                errs = []
                # 只允许完整 27 参数。实测 26/25/24 与 FeatureCut3/2 不抛异常，
                # 但会静默生成半深切除——比失败更危险，绝不能当回退。
                try:
                    f = fm.FeatureCut4(*full)
                except Exception as e:
                    errs.append(str(e)[:90])
                    f = None
                doc.ClearSelection2(True)
                if f is None:
                    _last_errs[:] = errs
                    if not quiet:
                        log(f"⚠ 切除失败：{name}" + ("｜" + " / ".join(dict.fromkeys(errs)) if errs else "（特征返回空）"))
                    if record:
                        failed.append(name)
                    # 切除失败会把草图留在活动状态；不退出的话，下一次 InsertSketch 会变成
                    # 「关闭当前草图」，后续特征全部画在空处、必然失败。
                    try:
                        sm.InsertSketch(True)
                    except Exception:
                        pass
                    try:
                        doc.ClearSelection2(True)
                    except Exception:
                        pass
                else:
                    log(f"切除完成：{name}")
                return f

            # ---------------- features
            def draw_shape(f, grow=0.0):
                """在当前草图上画该特征的轮廓。grow 为极小的外扩量（mm）。"""
                g = grow / 1000.0
                if f.type == "profile_cut":
                    # 通用闭合轮廓切除：原生直线 + 真圆弧，绝不按点列拟合。
                    from ..calc import feature_outline as _feature_outline
                    _draw_segs(sm, SwAdapter._mm_to_seg(_feature_outline(f)))
                elif f.type == "keyway":
                    # hub keyway: rect from bore wall to r+depth
                    # y0=sqrt(r^2-(w/2)^2) matches calc.keyway_volume exactly
                    rr = (f.diameter or 0.0) / 2000.0
                    bb = (f.width or 0.0) / 2000.0
                    if rr > 1e-9 and bb > 1e-9:
                        y0 = math.sqrt(max(rr * rr - bb * bb, 0.0))
                        y1 = rr + (f.depth or 0.0) / 1000.0
                        sm.CreateLine(-bb, y0, 0, bb, y0, 0)
                        sm.CreateLine(bb, y0, 0, bb, y1, 0)
                        sm.CreateLine(bb, y1, 0, -bb, y1, 0)
                        sm.CreateLine(-bb, y1, 0, -bb, y0, 0)
                elif f.type == "hole":
                    sm.CreateCircleByRadius(f.x / 1000.0, f.y / 1000.0, 0,
                                            f.diameter / 2000.0 + g)
                elif f.type == "slot":
                    # 腰形槽：真圆弧 + 直线，不使用折线采样
                    _L = float(f.length or f.width or 0.0)
                    _W = float(f.width or f.diameter or 0.0)
                    _ang = float(f.angle or 0.0) % 180.0
                    if _L <= _W + 2 * g * 1000 + 1e-6:
                        sm.CreateCircleByRadius(f.x / 1000.0, f.y / 1000.0, 0, f.width / 2000.0 + g)
                        return
                    # 圆盘 + 横贯全径的轴对齐直槽（十字节）：必须画**矩形**，不能画腰形。
                    # 腰形两端的半圆在 (±R, 0) 处与外圆精确相切，SW 的 FeatureCut4
                    # 对相切/退化轮廓会报「非选择性的参数」；即使侥幸切出来，
                    # 端部也只切到空气。矩形两端伸出外圆，由外圆自然裁掉。
                    if str(getattr(spec, "base_kind", "")).lower() == "round":
                        _R = float(spec.diameter or 0.0) / 2.0
                        _hc = (math.sqrt(max(_R * _R - (_W / 2.0) ** 2, 0.0))
                               if 0.0 < _W < 2.0 * _R else 0.0)
                        if (_hc > 0.0 and _L >= 2.0 * _hc - 1e-6
                                and (abs(_ang) < 1e-6 or abs(_ang - 90.0) < 1e-6)):
                            # ??????????????? x=?R ?????
                            _Lcut = max(_L, 2.0 * _R + 4.0)
                            if abs(_ang - 90.0) < 1e-6:
                                # 槽长沿 Y：轮廓宽 = W，高 = L
                                _draw_segs(sm, rect_outline(_W / 1000.0 + 2 * g,
                                                            _Lcut / 1000.0 + 2 * g, 0.0,
                                                            f.x / 1000.0, f.y / 1000.0))
                            else:
                                _draw_segs(sm, rect_outline(_Lcut / 1000.0 + 2 * g,
                                                            _W / 1000.0 + 2 * g, 0.0,
                                                            f.x / 1000.0, f.y / 1000.0))
                            return
                    _draw_segs(sm, slot_outline(_L / 1000.0 + 2 * g,
                                                _W / 1000.0 + 2 * g,
                                                f.x / 1000.0, f.y / 1000.0, f.angle))
                else:
                    # 矩形/型腔：四角圆角也用真圆弧
                    _draw_segs(sm, rect_outline(f.length / 1000.0 + 2 * g,
                                                f.width / 1000.0 + 2 * g,
                                                f.corner_radius / 1000.0,
                                                f.x / 1000.0, f.y / 1000.0))

            def extent(f):
                if f.type == "profile_cut":
                    from ..calc import feature_bbox as _feature_bbox
                    _b = _feature_bbox(f)
                    return max(_b[2] - _b[0], _b[3] - _b[1])
                return max(f.length or 0.0, f.width or 0.0, f.diameter or 0.0)

            # 冲突判定：完全落在更大更深轮廓里的特征只切一次；
            # 与槽只差零点几毫米的孔按"并入大轮廓"外扩，避免零厚度壁导致切除失败。
            _conf = spec.conflicts()
            for _w in _conf.get("warnings", []):
                log("⚠ " + _w)
            feats = [] if (spec.shaft_segments or spec.teeth or spec.sheet_t > 0) \
                else list(spec.build_features())

            # ?????????????????????? r=40??
            # spec.features ???? ?35/?48 ????????????
            # ?????????????? SW ????????????????
            def _revolve_axis_void(feat) -> bool:
                if not getattr(spec, "revolve_profile", None) or feat.type != "hole":
                    return False
                if math.hypot(float(feat.x or 0.0), float(feat.y or 0.0)) > 1e-6:
                    return False
                _rs = []
                for _sg in spec.revolve_profile:
                    for _k in ("p1", "p2"):
                        _q = _sg.get(_k)
                        if isinstance(_q, (list, tuple)) and len(_q) >= 2:
                            _rs.append(float(_q[0]))
                if not _rs:
                    return False
                # ???? r=0 ????????????????
                if min(_rs) <= 1e-6:
                    return False
                _rin = min(_rs)
                _dmax = max(float(feat.diameter or 0.0),
                            float(feat.counterbore_dia or 0.0))
                return _dmax > 0.0 and _dmax / 2.0 <= _rin + 1e-6

            _void_axis = [f for f in feats if _revolve_axis_void(f)]
            if _void_axis:
                log("???????????????????"
                    + "?".join(f"?{max(f.diameter, f.counterbore_dia):g}" for f in _void_axis))
                feats = [f for f in feats if not _revolve_axis_void(f)]
            # 原始顺序会让「Φ18 孔边」与「槽边」正好相切的两个特征相邻：先切孔再切槽时，
            # SolidWorks 会以「非选择性的参数」拒绝槽的切除，导致整块料漏切（体积偏差 20%+）。
            # 按轮廓尺寸从大到小切，先让大轮廓开口、再接小特征，相切处不再产生零厚度壁。
            order = sorted(enumerate(feats), key=lambda kv: (-extent(kv[1]), kv[0]))
            for _, f in order:
                if f.type == "sphere":
                    if not self._sphere_cut(doc, spec, f, log):
                        failed.append(f"?? SR{(f.diameter or 0) / 2.0:g}")
                    continue
                d = feature_depth(spec, f) / 1000.0
                if f.type == "keyway":
                    # depth is radial, not axial: keyway is through along the axis
                    depth = T + 1e-3
                    label = f"键槽 {f.width:g}×{f.depth:g}（深）"
                elif f.type == "hole":
                    depth = d + 1e-3 if f.through or f.depth <= 0 else d
                    label = f"孔 φ{f.diameter:g}"
                elif f.type == "slot":
                    depth = d + 1e-3 if f.through and f.depth <= 0 else d
                    label = f"腰形槽 {f.length:g}×{f.width:g}"
                elif f.type == "profile_cut":
                    depth = d + 1e-3 if f.through and f.depth <= 0 else d
                    label = f"轮廓切除（{len(f.profile or [])} 段）"
                else:
                    depth = d + 1e-3 if f.through and f.depth <= 0 else d
                    label = (f"{'型腔' if f.type == 'pocket' else f.type} "
                             f"{f.length:g}×{f.width:g}×{f.depth:g}")

                # 图纸上的相邻特征常常正好相切或只留极窄的悬臂（例如 Φ18 孔边与
                # 168×28 槽边只差 0.345mm）。槽把顶面切成两块后，再把这段唯一相连的
                # 薄壁切掉会产生零厚度几何，SolidWorks 直接以「非选择性的参数」拒绝，
                # 而外扩重切只会让该处更薄、必然同样失败。所以这里只切一次，
                # 切不出来就记进 failed，由上层把这次建模判为未完成——绝不静默放过。
                # 贴壁轮廓（孔边与型腔壁只差零点几毫米）会让 FeatureCut4 报
                # 「非选择性的参数」。实测：T1 = 型腔 90×90 + 4×Ø6 距壁 0.02mm，
                # 4 个孔里 3 个切不出来。逐级重试：原样 → 重建草图 → 微小外扩。
                got = None
                _g0 = 0.0 if f.type == "keyway" else spec.grow_of(f)

                def _close_sketch():
                    try:
                        if sm.ActiveSketch is not None:
                            sm.InsertSketch(True)
                    except Exception:
                        pass
                    try:
                        doc.ClearSelection2(True)
                    except Exception:
                        pass

                _fbot = str(getattr(f, "face", "top") or "top").lower() == "bottom"
                for attempt, extra in enumerate((0.0, 0.0, 0.02, 0.05)):
                    try:
                        _close_sketch()
                        (sketch_on_bottom if _fbot else sketch_on_top)()
                        draw_shape(f, grow=_g0 + extra)
                        got = cut(depth, label, record=False, quiet=True, flip=_fbot)
                    except Exception as e:
                        got = None
                        log(f"⚠ {label} 第 {attempt + 1} 次尝试异常：{str(e)[:80]}")
                    if got is not None:
                        if extra:
                            log(f"⚠ {label} 贴壁重试成功（外扩 {extra:g}mm，"
                                f"该处几何按合并开口处理）")
                        break
                _close_sketch()

                if got is None:
                    log(f"⚠ 切除失败：{label}" + ("｜" + " / ".join(dict.fromkeys(_last_errs)) if _last_errs else "（特征返回空）"))
                    if label not in failed:
                        failed.append(label)

                if got is not None and f.type == "hole" \
                        and f.counterbore_dia > f.diameter and f.counterbore_depth > 0 \
                        and not spec.counterbore_is_void(f):
                    sketch_on_top()
                    sm.CreateCircleByRadius(f.x / 1000.0, f.y / 1000.0, 0,
                                            f.counterbore_dia / 2000.0 + spec.grow_of(f) / 1000.0)
                    cut(f.counterbore_depth / 1000.0, f"沉孔 φ{f.counterbore_dia:g}")


            # ---------------- chamfer / fillet
            if spec.chamfer > 0 and spec.fillet > 0:
                log(f"同一圈顶棱已有圆角 R{spec.fillet:g}，倒角 C{spec.chamfer:g} 被覆盖，跳过")
            if spec.chamfer > 0 and spec.fillet <= 0:
                if spec.base_kind == "rect":
                    self._chamfer_by_cut(doc, spec, spec.chamfer, log)
                else:
                    self._apply_edge_feature(doc, spec, "chamfer", spec.chamfer, log)
            if spec.fillet > 0:
                self._apply_edge_feature(doc, spec, "fillet", spec.fillet, log)

            # ---------------- measure + save
            def _vol(fresh=False):
                """实体体积（mm³）。fresh=True 时先强制重建，逼 SW 重新算。"""
                try:
                    if fresh:
                        doc.EditRebuild3()
                except Exception:
                    pass
                try:
                    m = doc.GetMassProperties
                except Exception:
                    return 0.0
                return float(m[3]) * 1e9 if m and len(m) > 3 else 0.0

            v_before_save = _vol()
            part_path = out / f"{spec.file_stem}.SLDPRT"
            try:
                doc.SaveAs3(str(part_path), 0, 2)
                res.part_path = str(part_path)
                log(f"已保存零件：{part_path.name}")
            except Exception as e:
                log(f"保存失败：{e}")

            step_path = out / f"{spec.file_stem}.STEP"
            try:
                doc.SaveAs3(str(step_path), 0, 2)
                res.step_path = str(step_path)
                log(f"已导出 STEP：{step_path.name}")
            except Exception as e:
                log(f"STEP 导出失败：{e}")

            # 保存后复测：doc.GetMassProperties 在保存前拿到的是"待重建"状态，
            # 早期版本因此把「实体少了 2.5mm 料」算成 err=0.000%（见 2026-11-08 记录）。
            v_after_save = _vol(fresh=True)
            res.volume = v_after_save or v_before_save
            res.details["volume_before_save"] = round(v_before_save, 3)
            res.details["volume_after_save"] = round(v_after_save, 3)
            res.details["volume_source"] = "measured"
            if v_before_save > 0 and v_after_save > 0:
                _drift = abs(v_after_save - v_before_save) / v_before_save * 100.0
                if _drift > 0.01:
                    log(f"⚠ 保存前后体积不一致：{v_before_save:,.1f} → {v_after_save:,.1f}"
                        f"（{_drift:.3f}%），以保存后为准")
                    res.details["volume_rebuild_drift_pct"] = round(_drift, 4)

            img = out / f"{spec.file_stem}.bmp"
            try:
                doc.ShowNamedView2("*Isometric", 7)
                doc.ViewZoomtofit2()
                doc.SaveBMP(str(img), 1600, 1000)
                res.image_path = str(img)
            except Exception:
                pass

            res.ok = bool(res.part_path) and not failed
            res.failures = list(failed)
            if res.ok and res.volume <= 0:
                # 实测体积为 0 = 没拿到实体。绝不能拿理论值当实测值（历史教训）。
                res.ok = False
                res.message = "SolidWorks 已保存，但实体体积测量为空/为 0"
            if not res.part_path:
                res.message = "SolidWorks 未能保存零件"
            elif failed:
                res.message = ("SolidWorks 建模未完成：%d 个特征切除失败（%s）"
                               % (len(failed), "、".join(failed)))
            else:
                res.message = "SolidWorks 建模完成"
        except Exception as e:
            import traceback as _tb
            res.message = f"SolidWorks 建模失败：{e}"
            log(res.message)
            log(_tb.format_exc())
        finally:
            try:
                if doc is not None:
                    app.CloseDoc(doc.GetTitle)
            except Exception:
                pass
            res.elapsed = time.time() - t0
            try:
                _cad_release(log)
            except Exception:
                pass
            pythoncom.CoUninitialize()
        return res
