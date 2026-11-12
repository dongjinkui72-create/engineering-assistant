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
# SW 的 Top Plane 法向是 +Y：拉伸后厚度沿 Y、宽度沿 Z，导出 STEP 与 CATIA
# （XY 画轮廓、Z 为厚度）差一次轴交换。实测 U 形叉：Top Plane 版 STEP 为
# X27/Y6/Z14，Front Plane 版为 X27/Y14/Z6，与 CATIA 逐轴一致。
BASE_PLANES = ["前视基准面", "Front Plane", "上视基准面", "Top Plane"]
TOP_PLANES = ["上视基准面", "Top Plane"]   # 回转体备用


def _dismiss_startup_prompts(wait_s: float = 8.0) -> int:
    """关掉 SOLIDWORKS 启动期的模态提示（例如「工具栏信息不一致」）。

    为什么要做：这类框会把所有 COM 调用卡成 E_UNEXPECTED（实测 100% 复现），
    建模端表现为「基体拉伸失败」，实际上一行几何代码都没跑到。

    安全约束（同 CATIA 覆盖守卫的教训）：
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

    def _arc(s):
        nonlocal n
        _, cx, cy, r, a0, a1 = s[:6]
        if r <= 1e-12:
            return
        r0, r1 = _m.radians(a0), _m.radians(a1)
        x1, y1 = cx + r * _m.cos(r0), cy + r * _m.sin(r0)
        x2, y2 = cx + r * _m.cos(r1), cy + r * _m.sin(r1)
        # 端点吸到 1e-9 m（1 µm），避免 cos(90°) 的 6e-17 残留让 SW
        # 把轮廓判成开口。geom 约定 arc 为 CCW a0->a1；SW CreateArc
        # 的 direction=+1 才得到 a0->a1 的小弧（R5 直角弧实测 7.854，
        # -1 仍是 23.562 的大弧）。
        cx, cy, x1, y1, x2, y2 = [round(v, 9) for v in (cx, cy, x1, y1, x2, y2)]
        sm.CreateArc(cx, cy, 0.0, x1, y1, 0.0, x2, y2, 0.0, 1)
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
        """Involute gear: polyline profile -> extrude -> bore & keyway cut."""
        from ..gear import gear_profile
        sm, fm = doc.SketchManager, doc.FeatureManager
        plane = self._plane(doc, ["上视基准面", "Top Plane"])
        if plane is None:
            raise RuntimeError("no top plane")
        pts = gear_profile(spec.module, spec.teeth)
        doc.ClearSelection2(True)
        plane.Select2(False, 0)
        sm.InsertSketch(True)
        for i in range(len(pts)):
            a = pts[i]
            b = pts[(i + 1) % len(pts)]
            sm.CreateLine(a[0] / 1000.0, a[1] / 1000.0, 0, b[0] / 1000.0, b[1] / 1000.0, 0)
        sm.InsertSketch(True)
        f = fm.FeatureExtrusion2(True, False, False, 0, 0, spec.face_width / 1000.0,
                                 spec.face_width / 1000.0, False, False, False, False, A, A,
                                 False, False, False, False, True, True, True, 0, 0, False)
        if f is None:
            raise RuntimeError("gear extrude failed")
        log(f"gear profile: {len(pts)} points extruded")

        def cut_circle(r_mm):
            doc.ClearSelection2(True)
            plane.Select2(False, 0)
            sm.InsertSketch(True)
            sm.CreateCircleByRadius(0, 0, 0, r_mm / 1000.0)
            sm.InsertSketch(True)
            return fm.FeatureCut4(True, False, True, 1, 1, 0, 0, False, False, False, False, A, A,
                                  False, False, False, False, False, True, True, True, True, True,
                                  0, 0.0, False, True)

        if spec.bore > 0:
            cut_circle(spec.bore / 2.0)
            log(f"bore D{spec.bore:g} cut")
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
            log(f"keyway {spec.keyway_w:g}x{spec.keyway_d:g} cut")

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

        def _draw(mark_axis: bool):
            doc.ClearSelection2(True)
            plane.Select2(False, 0)
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
            sm.CreateCenterLine(0.0, -z0, 0.0, 0.0, -z1, 0.0)
            sm.InsertSketch(True)
            return nline, narc

        err = ""
        for mark_axis in (False, True):
            nline, narc = _draw(mark_axis)
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
            if ok and vexp > 0 and vgot > 0:
                if abs(vgot - vexp) / vexp > 0.01:
                    ok = False
                    err = (f"实体 {vgot:,.1f} vs 母线解析 {vexp:,.1f}"
                           f"（{(vgot - vexp) / vexp * 100.0:+.2f}%）")
            if ok:
                log(f"回转基体完成：母线 {len(prof)} 段（直线 {nline}"
                    + (f" + 真圆弧 {narc}" if narc else "") + "）绕竖轴 360°，"
                    f"实测 {vgot:,.1f} mm³（母线解析 {vexp:,.1f}）")
                return True
            try:
                doc.EditUndo2(2)
            except Exception:
                pass
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

    def build(self, spec: PartSpec, out_dir: str,
              log: Callable[[str], None] = lambda m: None) -> BuildResult:
        t0 = time.time()
        res = BuildResult(cad="SolidWorks", expected=0.0)
        _ok, _msg = _mem_check(1200.0, 92.0, "SolidWorks")
        if not _ok:
            # 内存不够时，先把**其它** CAD 关掉（用户要求同时只开一个），
            # 再重判一次；否则 SW 占着的 500+ MB 会让 CATIA/NX 永远起不来。
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
              elif spec.corner_radius > 0 and not self._arc_ends(spec):
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
            if not spec.shaft_segments and not getattr(spec, "revolve_profile", None):
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

            failed = list(corner_fail)
            def cut(depth_m: float, name: str):
                sm.InsertSketch(True)
                full = [True, False, False, 0, 0, depth_m, depth_m,
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
                    log(f"⚠ 切除失败：{name}" + ("｜" + " / ".join(dict.fromkeys(errs)) if errs else "（特征返回空）"))
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
                if f.type == "hole":
                    sm.CreateCircleByRadius(f.x / 1000.0, f.y / 1000.0, 0,
                                            f.diameter / 2000.0 + g)
                elif f.type == "slot":
                    # 腰形槽：真圆弧 + 直线，不使用折线采样
                    if (f.length or f.width) <= f.width + 2 * g * 1000 + 1e-6:
                        sm.CreateCircleByRadius(f.x / 1000.0, f.y / 1000.0, 0, f.width / 2000.0 + g)
                        return
                    _draw_segs(sm, slot_outline((f.length or f.width) / 1000.0 + 2 * g,
                                                f.width / 1000.0 + 2 * g,
                                                f.x / 1000.0, f.y / 1000.0, f.angle))
                else:
                    # 矩形/型腔：四角圆角也用真圆弧
                    _draw_segs(sm, rect_outline(f.length / 1000.0 + 2 * g,
                                                f.width / 1000.0 + 2 * g,
                                                f.corner_radius / 1000.0,
                                                f.x / 1000.0, f.y / 1000.0))

            def extent(f):
                return max(f.length or 0.0, f.width or 0.0, f.diameter or 0.0)

            # 冲突判定：完全落在更大更深轮廓里的特征只切一次；
            # 与槽只差零点几毫米的孔按"并入大轮廓"外扩，避免零厚度壁导致切除失败。
            _conf = spec.conflicts()
            for _w in _conf.get("warnings", []):
                log("⚠ " + _w)
            feats = [] if (spec.shaft_segments or spec.teeth or spec.sheet_t > 0) \
                else list(spec.build_features())
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
                if f.type == "hole":
                    depth = d + 1e-3 if f.through or f.depth <= 0 else d
                    label = f"孔 φ{f.diameter:g}"
                elif f.type == "slot":
                    depth = d + 1e-3 if f.through and f.depth <= 0 else d
                    label = f"腰形槽 {f.length:g}×{f.width:g}"
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
                _g0 = spec.grow_of(f)

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

                for attempt, extra in enumerate((0.0, 0.0, 0.02, 0.05)):
                    try:
                        _close_sketch()
                        sketch_on_top()
                        draw_shape(f, grow=_g0 + extra)
                        got = cut(depth, label)
                    except Exception as e:
                        got = None
                        log(f"⚠ {label} 第 {attempt + 1} 次尝试异常：{str(e)[:80]}")
                    if got is not None:
                        if extra:
                            log(f"⚠ {label} 贴壁重试成功（外扩 {extra:g}mm，"
                                f"该处几何按合并开口处理）")
                        break
                _close_sketch()

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
            res.message = f"SolidWorks 建模失败：{e}"
            log(res.message)
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
