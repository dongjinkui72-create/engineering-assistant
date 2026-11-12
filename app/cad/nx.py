"""Siemens NX adapter.

Builds the part with native NX features (Block / Cylinder + Boolean subtract)
inside a batch journal executed by %NX_ROOT%\\NXBIN\\run_journal.exe.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Callable

from ..calc import part_volume
from ..models import PartSpec
from .base import BuildResult
from .guard import acquire as _cad_acquire, release as _cad_release
from .memguard import check as _mem_check

JOURNAL = r'''
import NXOpen, json, os, math, traceback

SPEC = json.load(open(__SPEC__, encoding="utf-8"))
RESULT = __RESULT__
TMP_PRT = __TMP__
OUT_PRT = __OUT__
OUT_STP = __STP__
OUT_PNG = __PNG__
SHEET = __SHEET__
BASE_OUTLINE = __BASE_OUTLINE__
GEAR = __GEAR__
CHAMFER = __CHAMFER__
FILLET = __FILLET__
CORNER_R = __CORNER_R__
SHELL_T = __SHELL_T__
SHELL_OPEN = __SHELL_OPEN__
DOME_H = __DOME_H__
DOME_ELL = __DOME_ELL__
SWEEPS = __SWEEPS__
REVOLVE = __REVOLVE__

class _TraceLog(list):
    """即时落盘的日志：卡死时也能看到最后一步走到哪。"""
    def append(self, msg):
        list.append(self, msg)
        try:
            with open(RESULT + ".trace", "a", encoding="utf-8") as fh:
                fh.write(str(msg) + "\n")
                fh.flush()
        except Exception:
            pass

log = _TraceLog()
res = {"ok": False, "volume": 0.0, "message": "", "log": log}


def done():
    json.dump(res, open(RESULT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def main():
    s = NXOpen.Session.GetSession()
    try:
        NULL = NXOpen.Features.Feature.Null
    except Exception:
        NULL = None
    part = s.Parts.NewDisplay(TMP_PRT, NXOpen.Part.Units.Millimeters)
    F = part.Features
    SUB = NXOpen.GeometricUtilities.BooleanOperation.BooleanType.Subtract
    T = float(SPEC["thickness"])
    if os.path.exists(TMP_PRT):
        os.remove(TMP_PRT)

    def body0():
        return list(part.Bodies)[0]

    def subtract_cylinder(x, y, z, dia, height, note="", grow=0.0):
        c = F.CreateCylinderBuilder(NULL)
        c.Diameter.Value = float(dia) + 2.0 * grow
        c.Height.Value = float(height)
        c.Origin = NXOpen.Point3d(float(x), float(y), float(z))
        c.Direction = NXOpen.Vector3d(0.0, 0.0, 1.0)
        c.BooleanOption.SetBooleanOperationAndBody(SUB, body0())
        try:
            c.Commit()
            log.append("圆柱切除 " + note)
        except Exception:
            c.Destroy()
            if grow == 0.0:
                return subtract_cylinder(x, y, z, dia, height, note, 0.05)
            log.append("跳过 " + note)
            return
        c.Destroy()

    def subtract_block(x0, y0, z0, L, W, H, note="", grow=0.0):
        b = F.CreateBlockFeatureBuilder(NULL)
        b.SetOriginAndLengths(NXOpen.Point3d(float(x0) - grow, float(y0) - grow, float(z0)),
                              str(float(L) + 2 * grow), str(float(W) + 2 * grow), str(float(H)))
        b.SetBooleanOperationAndTarget(NXOpen.Features.FeatureBooleanType.Subtract, body0())
        try:
            b.Commit()
            log.append("方块切除 " + note)
        except Exception:
            b.Destroy()
            if grow == 0.0:
                return subtract_block(x0, y0, z0, L, W, H, note, 0.05)
            log.append("跳过 " + note)
            return
        b.Destroy()

    def _inner_segs():
        """抽壳内腔轮廓：直线 + 原生圆弧段，绝不按角度采样成折线。

        返回 [("line", p1, p2) | ("arc", p1, pmid, p2)]，交给 cut_segs()
        用 Curves.CreateLine / Curves.CreateArc 建真实解析曲线。
        """
        t = float(SHELL_T)
        left = bool(BASE_OUTLINE and BASE_OUTLINE.get("left"))
        right = bool(BASE_OUTLINE and BASE_OUTLINE.get("right"))
        if not left and not right:
            L = max(float(SPEC["length"]) - 2.0 * t, 0.2)
            W = max(float(SPEC["width"]) - 2.0 * t, 0.2)
            cr = max(float(CORNER_R) - t, 0.0)
            x0, x1 = -L / 2.0, L / 2.0
            y0, y1 = -W / 2.0, W / 2.0
            if cr <= 1e-9:
                return [("line", (x0, y0), (x1, y0)),
                        ("line", (x1, y0), (x1, y1)),
                        ("line", (x1, y1), (x0, y1)),
                        ("line", (x0, y1), (x0, y0))]
            cr = min(cr, min(L, W) / 2.0 - 1e-6)
            def _arc(cx, cy, r, a1, a2):
                am = math.radians((a1 + a2) / 2.0)
                p1 = (cx + r * math.cos(math.radians(a1)), cy + r * math.sin(math.radians(a1)))
                pm = (cx + r * math.cos(am), cy + r * math.sin(am))
                p2 = (cx + r * math.cos(math.radians(a2)), cy + r * math.sin(math.radians(a2)))
                return ("arc", p1, pm, p2)
            return [
                ("line", (x0 + cr, y0), (x1 - cr, y0)),
                _arc(x1 - cr, y0 + cr, cr, 270.0, 360.0),
                ("line", (x1, y0 + cr), (x1, y1 - cr)),
                _arc(x1 - cr, y1 - cr, cr, 0.0, 90.0),
                ("line", (x1 - cr, y1), (x0 + cr, y1)),
                _arc(x0 + cr, y1 - cr, cr, 90.0, 180.0),
                ("line", (x0, y1 - cr), (x0, y0 + cr)),
                _arc(x0 + cr, y0 + cr, cr, 180.0, 270.0),
            ]
        # 圆弧端：外轮廓圆弧半径 W/2，内缩 t 后半径 W/2−t；半圆圆心不随内缩移动。
        Lo = float(SPEC["length"]); Wo = float(SPEC["width"])
        r = max(Wo / 2.0 - t, 0.2)
        xi = max(0.0, Lo / 2.0 - Wo / 2.0)
        xl = -xi if left else -(Lo / 2.0 - t)
        xr = xi if right else (Lo / 2.0 - t)
        def _half(cx, a1, a2):
            am = math.radians((a1 + a2) / 2.0)
            p1 = (cx + r * math.cos(math.radians(a1)), r * math.sin(math.radians(a1)))
            pm = (cx + r * math.cos(am), r * math.sin(am))
            p2 = (cx + r * math.cos(math.radians(a2)), r * math.sin(math.radians(a2)))
            return ("arc", p1, pm, p2)
        out = [("line", (xl, -r), (xr, -r))]
        out.append(_half(xi, -90.0, 90.0) if right
                   else ("line", (xr, -r), (xr, r)))
        out.append(("line", (xr, r), (xl, r)))
        out.append(_half(-xi, 90.0, 270.0) if left
                   else ("line", (xl, r), (xl, -r)))
        return out

    def cut_segs(segs, z0, z1, note=""):
        """把直线/圆弧段拉伸切除。曲线全部由 NX 原生 CreateLine/CreateArc 生成。"""
        curves = []
        def _ln(p1, p2):
            curves.append(part.Curves.CreateLine(NXOpen.Point3d(p1[0], p1[1], 0.0),
                                                 NXOpen.Point3d(p2[0], p2[1], 0.0)))
        def _arc3(p1, pm, p2):
            _res = part.Curves.CreateArc(NXOpen.Point3d(p1[0], p1[1], 0.0),
                                         NXOpen.Point3d(pm[0], pm[1], 0.0),
                                         NXOpen.Point3d(p2[0], p2[1], 0.0),
                                         False)
            curves.append(_res[0] if isinstance(_res, tuple) else _res)
        for sg in segs:
            if sg[0] == "line":
                _ln(sg[1], sg[2])
            else:
                _arc3(sg[1], sg[2], sg[3])
        seed = segs[0][1]
        rule = part.ScRuleFactory.CreateRuleCurveDumb(curves)
        sec = part.Sections.CreateSection(0.01, 0.01, 0.5)
        sec.AddToSection([rule], curves[0], NXOpen.NXObject.Null, NXOpen.NXObject.Null,
                         NXOpen.Point3d(seed[0], seed[1], 0.0),
                         NXOpen.Section.Mode.Create, False)
        eb = F.CreateExtrudeBuilder(NULL)
        eb.Section = sec
        d = part.Directions.CreateDirection(NXOpen.Point3d(0.0, 0.0, 0.0),
                                            NXOpen.Vector3d(0.0, 0.0, 1.0),
                                            NXOpen.SmartObject.UpdateOption.WithinModeling)
        eb.Direction = d
        eb.Limits.StartExtend.SetValue(str(float(z0)))
        eb.Limits.EndExtend.SetValue(str(float(z1)))
        eb.BooleanOperation.SetBooleanOperationAndBody(SUB, body0())
        eb.Commit()
        eb.Destroy()
        log.append("方块切除 " + note)

    def _base_with_ends(L, W, T):
        """圆弧头基体：草图（直线 + 圆弧）→ 拉伸 → 与方块求交归一化。

        不走"方块 − 端部整块 + 圆柱求和"：那个做法把 W×W 的立方体整个吃掉再补回
        半圆柱，对 R=W/2 的精确相切几何会削掉圆弧顶点（实测 X 跨度只剩 24.5/27）。

        这里一次性画出完整轮廓（左端可为半圆、右端可为半圆），单连通闭合，
        NX 对这类拉伸稳定；再与 L×W 的方块求交，保证不越界。
        """
        _r = W / 2.0
        _xi = max(0.0, L / 2.0 - _r)
        _xl = -_xi if BASE_OUTLINE.get("left") else -L / 2.0
        _xr = _xi if BASE_OUTLINE.get("right") else L / 2.0
        curves = []
        def _ln(x1, y1, x2, y2):
            curves.append(part.Curves.CreateLine(NXOpen.Point3d(x1, y1, 0.0),
                                                 NXOpen.Point3d(x2, y2, 0.0)))
        def _arc3(p1, pmid, p2):
            # 三点式重载：CreateArc(startPoint, pointOn, endPoint, alternateSolution)
            # （另两个重载要 matrix，构造麻烦；三点式对半圆最直观）
            _res = part.Curves.CreateArc(NXOpen.Point3d(p1[0], p1[1], 0.0),
                                         NXOpen.Point3d(pmid[0], pmid[1], 0.0),
                                         NXOpen.Point3d(p2[0], p2[1], 0.0),
                                         False)
            # CreateArc 返回元组（首个元素才是 NXOpen.Arc），CreateLine 返回单对象
            curves.append(_res[0] if isinstance(_res, tuple) else _res)
        _ln(_xl, -_r, _xr, -_r)
        if BASE_OUTLINE.get("right"):
            # 右端半圆：从 (xi,-r) 经 (xi+r,0) 到 (xi,r)
            _arc3((_xi, -_r), (_xi + _r, 0.0), (_xi, _r))
        else:
            _ln(_xr, -_r, _xr, _r)
        _ln(_xr, _r, _xl, _r)
        if BASE_OUTLINE.get("left"):
            # 左端半圆：从 (-xi,r) 经 (-xi-r,0) 到 (-xi,-r)
            _arc3((-_xi, _r), (-_xi - _r, 0.0), (-_xi, -_r))
        else:
            _ln(_xl, _r, _xl, -_r)
        rule = part.ScRuleFactory.CreateRuleCurveDumb(curves)
        sec = part.Sections.CreateSection(0.01, 0.01, 0.5)
        sec.AddToSection([rule], curves[0], NXOpen.NXObject.Null, NXOpen.NXObject.Null,
                         NXOpen.Point3d(curves and _xl or 0.0, -_r, 0.0),
                         NXOpen.Section.Mode.Create, False)
        eb = F.CreateExtrudeBuilder(NULL)
        eb.Section = sec
        try:
            d = part.Directions.CreateDirection(NXOpen.Point3d(0.0, 0.0, 0.0),
                                                NXOpen.Vector3d(0.0, 0.0, 1.0),
                                                NXOpen.SmartObject.UpdateOption.WithinModeling)
            eb.Direction = d
        except Exception as _e:
            log.append("extrude direction err: %s" % _e)
        eb.Limits.StartExtend.SetValue("0")
        eb.Limits.EndExtend.SetValue(str(T))
        eb.Commit()
        eb.Destroy()
        log.append("圆弧头轮廓拉伸 %d 条曲线（左 %s / 右 %s），X %g..%g"
                   % (len(curves), BASE_OUTLINE.get("left"), BASE_OUTLINE.get("right"),
                      _xl, _xr))

    # ---- base solid -------------------------------------------------------
    if SHEET:
        L = float(SHEET["L"]); W = float(SHEET["W"]); t = float(SHEET["t"]); h = float(SHEET["h"])
        UNITE0 = NXOpen.Features.FeatureBooleanType.Unite
        subtract_unused = 0
        # base plate
        bb = F.CreateBlockFeatureBuilder(NULL)
        bb.SetOriginAndLengths(NXOpen.Point3d(-L/2.0, -W/2.0, 0.0), str(L), str(W), str(t))
        bb.Commit(); bb.Destroy()
        for sy in (1.0, -1.0):
            y0 = (W/2.0 - t) if sy > 0 else (-W/2.0)
            fb = F.CreateBlockFeatureBuilder(NULL)
            fb.SetOriginAndLengths(NXOpen.Point3d(-L/2.0, y0, 0.0), str(L), str(t), str(h))
            fb.SetBooleanOperationAndTarget(UNITE0, body0())
            fb.Commit(); fb.Destroy()
        log.append("钣金 U 形件 %gx%g t%g h%g" % (L, W, t, h))
    elif GEAR:
        pts = [tuple(p) for p in GEAR["points"]]
        fw = float(GEAR["face_width"])
        curves = []
        for i in range(len(pts)):
            a = pts[i]
            b = pts[(i + 1) % len(pts)]
            curves.append(part.Curves.CreateLine(NXOpen.Point3d(a[0], a[1], 0.0),
                                                 NXOpen.Point3d(b[0], b[1], 0.0)))
        rule = part.ScRuleFactory.CreateRuleCurveDumb(curves)
        sec = part.Sections.CreateSection(0.01, 0.01, 0.5)
        sec.AddToSection([rule], curves[0], NXOpen.NXObject.Null, NXOpen.NXObject.Null,
                         NXOpen.Point3d(pts[0][0], pts[0][1], 0.0),
                         NXOpen.Section.Mode.Create, False)
        eb = F.CreateExtrudeBuilder(NULL)
        eb.Section = sec
        try:
            d = part.Directions.CreateDirection(NXOpen.Point3d(0.0, 0.0, 0.0),
                                                NXOpen.Vector3d(0.0, 0.0, 1.0),
                                                NXOpen.SmartObject.UpdateOption.WithinModeling)
            eb.Direction = d
        except Exception as _e:
            log.append("extrude direction err: %s" % _e)
        eb.Limits.StartExtend.SetValue("0")
        eb.Limits.EndExtend.SetValue(str(fw))
        eb.Commit()
        eb.Destroy()
        log.append("齿轮齿廓拉伸 %d 曲线，齿宽 %g" % (len(curves), fw))
        if GEAR.get("bore", 0) > 0:
            subtract_cylinder(0.0, 0.0, -1.0, float(GEAR["bore"]), fw + 2.0, "轴孔")
            if GEAR.get("keyway_w", 0) > 0 and GEAR.get("keyway_d", 0) > 0:
                w = float(GEAR["keyway_w"]); d = float(GEAR["keyway_d"])
                b0 = float(GEAR["bore"]) / 2.0
                subtract_block(-w / 2.0, b0 - d, -1.0, w, d * 2 + 1.0, fw + 2.0, "键槽")
    SEGS = SPEC.get("shaft_segments") or []
    if SHEET:
        pass
    elif GEAR:
        pass
    elif SEGS:
        z = 0.0
        UNITE = NXOpen.GeometricUtilities.BooleanOperation.BooleanType.Unite
        for i, seg in enumerate(SEGS):
            c = F.CreateCylinderBuilder(NULL)
            c.Diameter.Value = float(seg.get("dia") or 10.0)
            c.Height.Value = float(seg.get("length") or 10.0)
            c.Origin = NXOpen.Point3d(0.0, 0.0, z)
            c.Direction = NXOpen.Vector3d(0.0, 0.0, 1.0)
            if i > 0:
                c.BooleanOption.SetBooleanOperationAndBody(UNITE, body0())
            c.Commit()
            c.Destroy()
            z += float(seg.get("length") or 10.0)
        log.append("回转体 %d 段，总长 %g" % (len(SEGS), z))
    elif REVOLVE:
        # ??????????r,z?? Z ? 360????? CreateLine?
        # ?????? CreateArc?????????????????
        try:
            _prof = [q for q in (REVOLVE or []) if isinstance(q, dict)]
            if len(_prof) < 3:
                raise RuntimeError("?????? 3 ?")
            _curves = []
            _nl = _na = 0
            for _seg in _prof:
                _a = [float(v) for v in (_seg.get("p1") or (0.0, 0.0))[:2]]
                _b = [float(v) for v in (_seg.get("p2") or (0.0, 0.0))[:2]]
                if str(_seg.get("type")) == "arc":
                    _c = [float(v) for v in (_seg.get("center") or (0.0, 0.0))[:2]]
                    _r = math.hypot(_a[0] - _c[0], _a[1] - _c[1])
                    _t1 = math.atan2(_a[1] - _c[1], _a[0] - _c[0])
                    _t2 = math.atan2(_b[1] - _c[1], _b[0] - _c[0])
                    _d = (_t2 - _t1) % (2.0 * math.pi)
                    if int(_seg.get("direction") or 1) < 0:
                        _d = -((_t1 - _t2) % (2.0 * math.pi))
                    _tm = _t1 + _d / 2.0
                    _pm = NXOpen.Point3d(_c[0] + _r * math.cos(_tm), 0.0,
                                         _c[1] + _r * math.sin(_tm))
                    _arc = part.Curves.CreateArc(
                        NXOpen.Point3d(_a[0], 0.0, _a[1]), _pm,
                        NXOpen.Point3d(_b[0], 0.0, _b[1]), False)
                    _curves.append(_arc[0] if isinstance(_arc, tuple) else _arc)
                    _na += 1
                else:
                    _curves.append(part.Curves.CreateLine(
                        NXOpen.Point3d(_a[0], 0.0, _a[1]),
                        NXOpen.Point3d(_b[0], 0.0, _b[1])))
                    _nl += 1
            _zs = [float(q["p1"][1]) for q in _prof] + [float(q["p2"][1]) for q in _prof]
            _zmin, _zmax = min(_zs), max(_zs)
            _p0 = [float(v) for v in (_prof[0].get("p1") or (0, 0))[:2]]
            _rule = part.ScRuleFactory.CreateRuleCurveDumb(_curves)
            _sec = part.Sections.CreateSection(0.01, 0.01, 0.5)
            _sec.AddToSection([_rule], _curves[0], NXOpen.NXObject.Null,
                              NXOpen.NXObject.Null,
                              NXOpen.Point3d(_p0[0], 0.0, _p0[1]),
                              NXOpen.Section.Mode.Create, False)
            _axpt = part.Points.CreatePoint(NXOpen.Point3d(0.0, 0.0, _zmin))
            _axdir = part.Directions.CreateDirection(
                NXOpen.Point3d(0.0, 0.0, _zmin), NXOpen.Vector3d(0.0, 0.0, 1.0),
                NXOpen.SmartObject.UpdateOption.WithinModeling)
            _ax = part.Axes.CreateAxis(_axpt, _axdir,
                                       NXOpen.SmartObject.UpdateOption.WithinModeling)
            _rb = F.CreateRevolveBuilder(NULL)
            _rb.Section = _sec
            _rb.Axis = _ax
            _rb.Limits.StartExtend.SetValue("0")
            _rb.Limits.EndExtend.SetValue("360")
            _rb.Commit()
            _rb.Destroy()
            log.append("???? %g ???? %d + ??? %d???? 360?"
                       % (len(_prof), _nl, _na))
        except Exception as _e_rv:
            log.append("??????: %s" % _e_rv)
            res.setdefault("failed", []).append("???? Revolve")

    elif SPEC.get("base_kind") == "round":
        d = float(SPEC["diameter"] or SPEC["length"])
        c = F.CreateCylinderBuilder(NULL)
        c.Diameter.Value = d
        c.Height.Value = T
        c.Origin = NXOpen.Point3d(0.0, 0.0, 0.0)
        c.Direction = NXOpen.Vector3d(0.0, 0.0, 1.0)
        c.Commit()
        c.Destroy()
        log.append("圆柱基体 Ø%g x %g" % (d, T))
    else:
        L = float(SPEC["length"]); W = float(SPEC["width"])
        if BASE_OUTLINE:
            _base_with_ends(L, W, T)
        else:
            b = F.CreateBlockFeatureBuilder(NULL)
            b.SetOriginAndLengths(NXOpen.Point3d(-L / 2.0, -W / 2.0, 0.0),
                                  str(L), str(W), str(T))
            b.Commit()
            b.Destroy()
            log.append("方块基体 %g x %g x %g" % (L, W, T))
        if float(CORNER_R) > 0:
            # 四角圆角 = 4 条竖棱倒圆（与 CATIA 的做法一致，几何精确）
            try:
                vedges = []
                for e in body0().GetEdges():
                    vs = list(e.GetVertices())
                    if len(vs) != 2:
                        continue
                    a, bpt = vs[0], vs[1]      # NX 直接返回 Point3d
                    if (abs(a.X - bpt.X) < 1e-4 and abs(a.Y - bpt.Y) < 1e-4
                            and abs(a.Z - bpt.Z) > 1e-4):
                        vedges.append(e)
                if vedges:
                    sc0 = part.ScCollectors.CreateCollector()
                    sc0.ReplaceRules([part.ScRuleFactory.CreateRuleEdgeDumb(vedges)], False)
                    eb0 = F.CreateEdgeBlendBuilder(NULL)
                    eb0.AddChainset(sc0, str(float(CORNER_R)))
                    eb0.Commit()
                    eb0.Destroy()
                    log.append("四角 R%g 完成（%d 条竖棱）" % (float(CORNER_R), len(vedges)))
                else:
                    log.append("四角 R%g: 没找到竖棱" % float(CORNER_R))
            except Exception as e0:
                log.append("四角圆角失败: %s" % e0)

    # ---- shell ------------------------------------------------------------
    # 不用 F.CreateShellBuilder：本机 NX 12 在 CreateShellBuilder(NULL) 后 Commit
    # 会卡死（实测 >100s 无返回，同一几何 SW/CATIA 都正常）。改为"外壳 − 内腔"
    # 布尔切除：内腔 = 外轮廓均匀内缩 t，底面留 t；开口朝上时内腔顶到 T+0.5。
    if float(SHELL_T) > 0 and not SHEET and not GEAR and not SEGS and not REVOLVE:
        try:
            _t = float(SHELL_T)
            _open = str(SHELL_OPEN).lower() != "none"
            _z0 = _t
            _z1 = (T + 0.5) if _open else max(T - _t, _z0 + 0.05)
            if SPEC.get("base_kind") == "round":
                _d = float(SPEC.get("diameter") or SPEC.get("length")) - 2.0 * _t
                if _d > 0.2:
                    subtract_cylinder(0.0, 0.0, _z0, _d, _z1 - _z0, "抽壳内腔")
            elif abs(float(CORNER_R)) < 1e-9 and not BASE_OUTLINE:
                _L = float(SPEC["length"]) - 2.0 * _t
                _W = float(SPEC["width"]) - 2.0 * _t
                if _L > 0.2 and _W > 0.2:
                    subtract_block(-_L / 2.0, -_W / 2.0, _z0, _L, _W, _z1 - _z0, "抽壳内腔")
            else:
                cut_segs(_inner_segs(), _z0, _z1, "抽壳内腔")
            log.append("抽壳 t%g 完成（%s）" % (_t, "开口朝上" if _open else "全封闭"))
        except Exception as e0:
            log.append("抽壳失败: %s" % e0)

    # ---- 圆顶面（Dome）：原生椭圆/圆弧母线绕竖轴 360° 回转加料 ----------
    # 约束：母线必须是 CAD 原生解析曲线（CreateEllipse / CreateArc），
    # 绝不允许用点列拟合 —— 点列会把椭球面变成多段平面，包围盒和体积都会偏。
    if DOME_H > 0 and not SHEET and not GEAR and not SEGS and not REVOLVE:
        try:
            _h = float(DOME_H)
            if SPEC.get("base_kind") == "round":
                _a = float(SPEC.get("diameter") or SPEC.get("length")) / 2.0
            else:
                _a = min(float(SPEC.get("length") or 0.0),
                         float(SPEC.get("width") or 0.0)) / 2.0
            if _a <= 0.2:
                raise RuntimeError("基体半径无效 %g" % _a)
            _zb = T
            _curves = []
            if DOME_ELL:
                # 四分之一椭圆：中心(0,0,T)，长半轴 a 沿 +Y（径向），短半轴 h 沿 +Z（轴向）
                # 角度按 NX 约定为弧度：0 → 轮缘点 (a, T)，π/2 → 顶点 (0, T+h)
                _el = part.Curves.CreateEllipse(
                    NXOpen.Point3d(0.0, 0.0, _zb),
                    NXOpen.Vector3d(0.0, 1.0, 0.0),
                    NXOpen.Vector3d(0.0, 0.0, 1.0),
                    float(_a), float(_h), 0.0, math.pi / 2.0)
                _curves.append(_el)
            else:
                # 球冠：圆心在轮缘一侧，R = (a²+h²)/(2h)，三点式原生圆弧
                _R = (_a * _a + _h * _h) / (2.0 * _h)
                _zc = _zb - (_R - _h)
                _ang0 = math.atan2(_R - _h, _a)
                _angm = (_ang0 + math.pi / 2.0) / 2.0
                _p1 = NXOpen.Point3d(0.0, _a, _zb)
                _pm = NXOpen.Point3d(0.0, _R * math.cos(_angm), _zc + _R * math.sin(_angm))
                _p2 = NXOpen.Point3d(0.0, 0.0, _zb + _h)
                _arc = part.Curves.CreateArc(_p1, _pm, _p2, False)
                _curves.append(_arc[0] if isinstance(_arc, tuple) else _arc)
            # 回转截面必须闭合：补一条底边 (0,T) → (a,T)
            _curves.append(part.Curves.CreateLine(NXOpen.Point3d(0.0, 0.0, _zb),
                                                 NXOpen.Point3d(0.0, _a, _zb)))
            _rule = part.ScRuleFactory.CreateRuleCurveDumb(_curves)
            _sec = part.Sections.CreateSection(0.01, 0.01, 0.5)
            _sec.AddToSection([_rule], _curves[0], NXOpen.NXObject.Null, NXOpen.NXObject.Null,
                              NXOpen.Point3d(0.0, _a, _zb),
                              NXOpen.Section.Mode.Create, False)
            # RevolveBuilder.Axis 要 NXOpen.Axis（不是 Direction）：
            # part.Axes.CreateAxis(Point, Direction, UpdateOption)
            _axpt = part.Points.CreatePoint(NXOpen.Point3d(0.0, 0.0, _zb))
            _axdir = part.Directions.CreateDirection(
                NXOpen.Point3d(0.0, 0.0, _zb), NXOpen.Vector3d(0.0, 0.0, 1.0),
                NXOpen.SmartObject.UpdateOption.WithinModeling)
            _ax = part.Axes.CreateAxis(_axpt, _axdir,
                                       NXOpen.SmartObject.UpdateOption.WithinModeling)
            _UNITE = NXOpen.GeometricUtilities.BooleanOperation.BooleanType.Unite
            _rb = F.CreateRevolveBuilder(NULL)
            _rb.Section = _sec
            _rb.Axis = _ax
            _rb.Limits.StartExtend.SetValue("0")
            _rb.Limits.EndExtend.SetValue("360")
            _rb.BooleanOperation.SetBooleanOperationAndBody(_UNITE, body0())
            _rb.Commit()
            _rb.Destroy()
            log.append("圆顶面完成：h=%g（%s），原生曲线 + 360° 回转"
                       % (_h, "elliptic" if DOME_ELL else "spherical"))
        except Exception as e_d:
            log.append("圆顶面失败: %s" % e_d)
            res.setdefault("failed", []).append("圆顶面 Dome")

    # ---- 扫掠（Sweep）：原生直线 + 原生圆弧作路径，原生矩形作截面，SweepAlongGuide 成体 ----
    # 与 SW/CATIA 同一约定：路径整条落在 XZ 平面（所有点同一 Y），首段切向 ±X。
    # 圆弧用三点式 CreateArc（起点/途径点/终点），不做点列拟合。
    if SWEEPS and not SHEET and not GEAR and not SEGS and not REVOLVE:
        _made_sw = 0
        for _swi, _item in enumerate(SWEEPS, 1):
            try:
                _w = float(_item.get("profile_w") or 0.0)
                _h = float(_item.get("profile_h") or 0.0)
                _path = [dict(x) for x in (_item.get("path") or [])]
                if _w <= 0 or _h <= 0 or not _path:
                    log.append("扫掠 %d：截面或路径为空，跳过" % _swi)
                    continue

                def _ends(seg):
                    a = [float(v) for v in (seg.get("p1") or [0, 0, 0])[:3]]
                    b = [float(v) for v in (seg.get("p2") or [0, 0, 0])[:3]]
                    return a, b

                _ys = []
                for _seg in _path:
                    for _k in ("p1", "p2", "center"):
                        _q = _seg.get(_k)
                        if _q:
                            _ys.append(float(_q[1]))
                if not _ys or max(_ys) - min(_ys) > 1e-6:
                    log.append("扫掠 %d：路径不在 XZ 平面，暂不支持" % _swi)
                    continue
                _y0 = _ys[0]

                _a0, _b0 = _ends(_path[0])
                _dx, _dz = _b0[0] - _a0[0], _b0[2] - _a0[2]
                if abs(_dx) < 1e-9 or abs(_dx) < abs(_dz):
                    log.append("扫掠 %d：起始切向不是 ±X，暂不支持" % _swi)
                    continue
                if _dx < 0:
                    _rev = []
                    for _seg in reversed(_path):
                        _r = dict(_seg)
                        _r["p1"], _r["p2"] = _seg.get("p2"), _seg.get("p1")
                        if str(_seg.get("type") or "").lower() == "arc":
                            _r["direction"] = -int(_seg.get("direction") or 1)
                        _rev.append(_r)
                    _path = _rev
                    _a0, _b0 = _ends(_path[0])
                _xs, _zs = _a0[0], _a0[2]

                _guides = []
                _nseg = 0
                for _seg in _path:
                    _typ = str(_seg.get("type") or "line").lower()
                    _a, _b = _ends(_seg)
                    if _typ == "arc":
                        _c = [float(v) for v in (_seg.get("center") or [0, 0, 0])[:3]]
                        _r = math.hypot(_a[0] - _c[0], _a[2] - _c[2])
                        if _r <= 1e-9:
                            continue
                        _a1 = math.atan2(_a[2] - _c[2], _a[0] - _c[0])
                        _a2 = math.atan2(_b[2] - _c[2], _b[0] - _c[0])
                        _dir = 1 if int(_seg.get("direction") or 1) >= 0 else -1
                        if _dir > 0:
                            _span = (_a2 - _a1) % (2.0 * math.pi)
                        else:
                            _span = -((_a1 - _a2) % (2.0 * math.pi))
                        _am = _a1 + _span / 2.0
                        _pm = NXOpen.Point3d(_c[0] + _r * math.cos(_am), _y0,
                                             _c[2] + _r * math.sin(_am))
                        _ar = part.Curves.CreateArc(
                            NXOpen.Point3d(_a[0], _y0, _a[2]), _pm,
                            NXOpen.Point3d(_b[0], _y0, _b[2]), False)
                        _guides.append(_ar[0] if isinstance(_ar, tuple) else _ar)
                    else:
                        if abs(_a[0] - _b[0]) < 1e-12 and abs(_a[2] - _b[2]) < 1e-12:
                            continue
                        _guides.append(part.Curves.CreateLine(
                            NXOpen.Point3d(_a[0], _y0, _a[2]),
                            NXOpen.Point3d(_b[0], _y0, _b[2])))
                    _nseg += 1
                if not _guides:
                    log.append("扫掠 %d：路径曲线为空" % _swi)
                    continue

                _prof = []
                _c4 = [(_y0 - _w / 2.0, _zs - _h / 2.0), (_y0 + _w / 2.0, _zs - _h / 2.0),
                       (_y0 + _w / 2.0, _zs + _h / 2.0), (_y0 - _w / 2.0, _zs + _h / 2.0)]
                for _i in range(4):
                    _p, _q2 = _c4[_i], _c4[(_i + 1) % 4]
                    _prof.append(part.Curves.CreateLine(
                        NXOpen.Point3d(_xs, _p[0], _p[1]),
                        NXOpen.Point3d(_xs, _q2[0], _q2[1])))
                _prule = part.ScRuleFactory.CreateRuleCurveDumb(_prof)
                _psec = part.Sections.CreateSection(0.01, 0.01, 0.5)
                _psec.AddToSection([_prule], _prof[0], NXOpen.NXObject.Null, NXOpen.NXObject.Null,
                                   NXOpen.Point3d(_xs, _y0, _zs),
                                   NXOpen.Section.Mode.Create, False)
                _grule = part.ScRuleFactory.CreateRuleCurveDumb(_guides)
                _gsec = part.Sections.CreateSection(0.01, 0.01, 0.5)
                _gsec.AddToSection([_grule], _guides[0], NXOpen.NXObject.Null, NXOpen.NXObject.Null,
                                   NXOpen.Point3d(_a0[0], _y0, _a0[2]),
                                   NXOpen.Section.Mode.Create, False)

                _UNITE = NXOpen.GeometricUtilities.BooleanOperation.BooleanType.Unite
                _built = False
                _errs = []
                try:
                    # SweepAlongGuide is the dedicated sweep-along-guide feature and
                    # owns a BooleanOperation. SweptBuilder uses SectionList/GuideList
                    # and has NO boolean merge (checked against NX 12 NXOpen.xml), so
                    # it is not a drop-in fallback here.
                    def _count(_coll):
                        try:
                            return len(list(_coll))
                        except Exception:
                            return -1
                    _nf0 = _count(part.Features)
                    _nb0 = _count(part.Bodies)
                    _sb = F.CreateSweepAlongGuideBuilder(NXOpen.Features.SweepAlongGuide.Null)
                    _sb.Section = _psec
                    _sb.Guide = _gsec
                    _sb.BooleanOperation.SetBooleanOperationAndBody(_UNITE, body0())
                    _sb.Commit()
                    _sb.Destroy()
                    # Commit 不抛异常 != 真的建成了：
                    # 没生成特征 = 内核静默忽略；多出一个 body = 布尔没并进去
                    # （实体是两块，理论体积却按一块算）。两种都必须算失败。
                    _nf1 = _count(part.Features)
                    _nb1 = _count(part.Bodies)
                    if _nf0 >= 0 and _nf1 >= 0 and _nf1 <= _nf0:
                        raise RuntimeError("Commit 未生成扫掠特征（特征数 %d→%d）" % (_nf0, _nf1))
                    if _nb0 >= 0 and _nb1 >= 0 and _nb1 > _nb0:
                        raise RuntimeError("扫掠未并入基体（body 数 %d→%d，检查路径是否与基体相交）"
                                           % (_nb0, _nb1))
                    _built = True
                except Exception as _e1:
                    _errs.append("SweepAlongGuide: %s" % _e1)
                if _built:
                    _made_sw += 1
                    log.append("扫掠 %d 完成：截面 %gx%g，路径 %d 段（原生直线+圆弧）"
                               % (_swi, _w, _h, _nseg))
                else:
                    log.append("扫掠 %d 失败：%s" % (_swi, " | ".join(_errs)))
            except Exception as _e_sw:
                log.append("扫掠 %d 异常：%s" % (_swi, _e_sw))
        if _made_sw < len(SWEEPS):
            res.setdefault("failed", []).append("扫掠 %d/%d" % (_made_sw, len(SWEEPS)))
        else:
            res["sweep_built"] = "%d/%d" % (_made_sw, len(SWEEPS))

    # ---- features ---------------------------------------------------------
    feats_sorted = sorted(SPEC.get("features", []),
                          key=lambda f: {"pocket": 0, "step": 1, "slot": 2, "hole": 3}.get(f.get("type"), 4))
    for f in feats_sorted:
        t = f.get("type")
        x = float(f.get("x") or 0.0); y = float(f.get("y") or 0.0)
        depth = float(f.get("depth") or 0.0)
        through = bool(f.get("through", depth <= 0))
        grow = float(f.get("grow") or 0.0)        # 薄壁冲突时外扩，见 app/conflict.py
        if t == "hole":
            dia = float(f.get("diameter") or 6.0) + 2 * grow
            if through:
                # 通孔：从底面下方 1mm 起，切穿到底
                subtract_cylinder(x, y, -1.0, dia, T + 3.0, "Ø%g 通孔" % dia)
            else:
                # 盲孔：从 T-depth 起向上切，不能从 -1 起（那样等于整块钻穿）
                dblind = min(max(depth, 0.1), T)
                subtract_cylinder(x, y, T - dblind, dia, dblind + 1.0,
                                  "Ø%g 盲孔 深%g" % (dia, dblind))
            cbd = float(f.get("counterbore_dia") or 0.0)
            cbh = float(f.get("counterbore_depth") or 0.0)
            if cbd > dia and cbh > 0:
                subtract_cylinder(x, y, T - cbh, cbd, cbh + 1.0, "沉孔 Ø%g" % cbd)
        elif t == "sphere":
            R = float(f.get("diameter") or 16.0) / 2.0 + grow
            h = min(float(f.get("depth") or R), R)
            zc = T - h + R                     # 球心：使切深正好为 h
            try:
                # NULL 必须用 Feature.Null（和 CreateCylinderBuilder / CreateBlockFeatureBuilder
                # 同一个种子）。早先用 NXOpen.Features.Sphere.Null 会让 Commit 挂死。
                sb = F.CreateSphereBuilder(NULL)
                try:
                    sb.Type = NXOpen.Features.SphereBuilder.Types.Sphere
                except Exception:
                    pass
                try:
                    sb.BooleanOption.SetBooleanOperationAndBody(SUB, body0())
                except Exception as e_b:
                    log.append("球坑布尔设置失败: %s" % e_b)
                sb.Diameter.Value = R * 2.0
                sb.Origin = NXOpen.Point3d(x, y, zc)
                sb.Commit()
                sb.Destroy()
                log.append("球坑 SR%g 深 %g（原生球体布尔切除）" % (R, h))
            except Exception as e0:
                log.append("球坑失败: %s" % e0)
        elif t in ("pocket", "step", "boss"):
            L = float(f.get("length") or 10.0) + 2 * grow
            W = float(f.get("width") or 10.0) + 2 * grow
            d = T if (through and depth <= 0) else max(depth, 0.5)
            subtract_block(x - L / 2.0, y - W / 2.0, T - d, L, W, d + 1.0, "%s" % t)
        elif t == "slot":
            L = float(f.get("length") or 20.0); W = float(f.get("width") or 8.0) + 2 * grow
            ang = float(f.get("angle") or 0.0)
            a = math.radians(ang)
            half = max((L - W) / 2.0, 0.0)
            d = T if (through and depth <= 0) else max(depth, 0.5)
            # 腰形槽 = 中间方块 + 两端圆柱（精确）；斜槽才退化成圆柱串近似
            cx1 = x - half * math.cos(a); cy1 = y - half * math.sin(a)
            cx2 = x + half * math.cos(a); cy2 = y + half * math.sin(a)
            subtract_cylinder(cx1, cy1, T - d, W, d + 1.0, "腰槽端1")
            subtract_cylinder(cx2, cy2, T - d, W, d + 1.0, "腰槽端2")
            if abs(ang) < 1e-6 and half > 1e-6:
                subtract_block(x - half, y - W / 2.0, T - d, 2.0 * half, W, d + 1.0, "腰槽中段")
            else:
                steps = max(2, int(round(half * 2 / max(W * 0.3, 0.3))))
                for i in range(0, steps + 1):
                    tt = i / float(steps)
                    subtract_cylinder(x + (-half + 2 * half * tt) * math.cos(a),
                                      y + (-half + 2 * half * tt) * math.sin(a),
                                      T - d, W, d + 1.0, "腰槽填充")

    # ---- 倒角 / 圆角（顶面外轮廓）
    try:
        def top_edges():
            """顶面外轮廓边：只取外周边，排除孔口的封闭边。

            ⚠ 判据不能只看「顶点都在 z≈T」：孔口边同样满足。
            实测圆盘 Ø60×15 C2（理论去料 376.99）：
                无孔    1 条边 →  97.8%
                1 孔    2 条边 → 133.3%
                4 孔    5 条边 → 133.3%
                端盖    6 条边 → 169.4%   ← 孔口被一起倒角
            圆盘额外按「顶点到轴心距离 ≈ 半径」筛选，才只留外圆。
            """
            cand = []
            for e in list(body0().GetEdges()):
                vs = list(e.GetVertices())
                if vs and all(abs(v.Z - T) < 1e-6 for v in vs):
                    ln = 0.0
                    if len(vs) >= 2:
                        ln = ((vs[0].X - vs[1].X) ** 2 + (vs[0].Y - vs[1].Y) ** 2) ** 0.5
                    cand.append((e, ln, len(vs)))
            if not cand:
                return []
            # 圆盘：只保留外圆边（顶点半径 ≈ D/2）
            try:
                if str(SPEC.get("base_kind") or "") == "round":
                    _R = float(SPEC.get("diameter") or 0.0) / 2.0
                    if _R > 0:
                        outer = []
                        for (e, ln, nv) in cand:
                            vs = list(e.GetVertices())
                            if not vs:
                                continue
                            if all(abs((v.X * v.X + v.Y * v.Y) ** 0.5 - _R) < 0.5
                                   for v in vs):
                                outer.append(e)
                        if outer:
                            return outer
            except Exception:
                pass
            if SPEC.get("base_kind") == "round" and len(cand) == 1:
                return [cand[0][0]]
            longest = max(c[1] for c in cand)
            keep = [c[0] for c in cand if c[1] >= max(2.0, 0.4 * longest)]
            return keep or [cand[0][0]]

        if float(CHAMFER) > 0 and float(FILLET) > 0:
            log.append("同一圈顶棱已有圆角 R%g，倒角 C%g 被覆盖，跳过" % (float(FILLET), float(CHAMFER)))
        def _measure():
            """按 mm³ 量一次体积；失败返回 0。

            必须先 DoUpdate：NX 的 Commit 之后实体未必立刻更新，
            直接量会读到旧体积，导致"倒角建了但没去料"这种静默失败查不出来
            （实测端盖 C2：日志报 1/1 完成，体积却与不含倒角一致）。
            """
            try:
                try:
                    part.UpdateManager.DoUpdate(NXOpen.Session.GetSession().NewestVisibleMode)
                except Exception:
                    try:
                        part.UpdateManager.DoUpdate(0)
                    except Exception:
                        pass
                uc0 = part.UnitCollection
                u0 = [uc0.FindObject("SquareMilliMeter"), uc0.FindObject("CubicMilliMeter"),
                      uc0.FindObject("Kilogram"), uc0.FindObject("MilliMeter"),
                      uc0.FindObject("Newton")]
                m0 = part.MeasureManager.NewMassProperties(u0, 0.99, [body0()])
                return float(m0.Volume)
            except Exception:
                return 0.0

        if float(CHAMFER) > 0 and float(FILLET) <= 0:
            # 所有目标边放进**同一个** collector，一次建特征。
            # 逐条建时第一条改了拓扑，后续 top_edges() 的边引用失效
            # （实测安装板只成 3/5、端盖 3/4）。SW 侧同一机理已改批量。
            edges = list(top_edges())
            done = 0
            if edges:
                v0 = _measure()
                try:
                    sc = part.ScCollectors.CreateCollector()
                    sc.ReplaceRules([part.ScRuleFactory.CreateRuleEdgeDumb(edges)], False)
                    cb = F.CreateChamferBuilder(NULL)
                    cb.SmartCollector = sc
                    cb.FirstOffset = str(float(CHAMFER))
                    cb.Commit()
                    cb.Destroy()
                    done = len(edges)
                except Exception as e1:
                    log.append("倒角批量失败(%s)，改逐条" % str(e1)[:90])
                    for e in edges:
                        try:
                            sc = part.ScCollectors.CreateCollector()
                            sc.ReplaceRules([part.ScRuleFactory.CreateRuleEdgeDumb([e])], False)
                            cb = F.CreateChamferBuilder(NULL)
                            cb.SmartCollector = sc
                            cb.FirstOffset = str(float(CHAMFER))
                            cb.Commit()
                            cb.Destroy()
                            done += 1
                        except Exception:
                            pass
                v1 = _measure()
                removed = (v0 - v1) if (v0 > 0 and v1 > 0) else 0.0
                # 期望去料量：圆盘 πDc²/2；矩形板 (L+W)c²（近似，只用于量级判断）
                try:
                    _c = float(CHAMFER)
                    _PI = 3.141592653589793
                    if str(SPEC.get("base_kind") or "") == "round":
                        _want = _PI * float(SPEC.get("diameter") or 0.0) * _c * _c / 2.0
                    else:
                        _want = (float(SPEC.get("length") or 0.0) +
                                 float(SPEC.get("width") or 0.0)) * _c * _c
                except Exception:
                    _want = 0.0
                _need = max(1e-6, _want * 0.10)
                if removed < _need:
                    log.append("⚠ 倒角 C%g：去料只有 %.2f mm³（期望约 %.2f），"
                               "按未去料处理，计入失败"
                               % (float(CHAMFER), removed, _want))
                    res.setdefault("failed", []).append("倒角 C%g" % float(CHAMFER))
            log.append("倒角 C%g 完成（%d/%d 条边）" % (float(CHAMFER), done, len(edges)))

        if float(FILLET) > 0:
            edges2 = list(top_edges())
            done = 0
            if edges2:
                try:
                    sc2 = part.ScCollectors.CreateCollector()
                    sc2.ReplaceRules([part.ScRuleFactory.CreateRuleEdgeDumb(edges2)], False)
                    eb = F.CreateEdgeBlendBuilder(NULL)
                    eb.AddChainset(sc2, str(float(FILLET)))
                    eb.Commit()
                    eb.Destroy()
                    done = len(edges2)
                except Exception as e2:
                    log.append("圆角批量失败(%s)，改逐条" % str(e2)[:90])
                    for e in edges2:
                        try:
                            sc2 = part.ScCollectors.CreateCollector()
                            sc2.ReplaceRules([part.ScRuleFactory.CreateRuleEdgeDumb([e])], False)
                            eb = F.CreateEdgeBlendBuilder(NULL)
                            eb.AddChainset(sc2, str(float(FILLET)))
                            eb.Commit()
                            eb.Destroy()
                            done += 1
                        except Exception:
                            pass
            log.append("圆角 R%g 完成（%d 条边）" % (float(FILLET), done))
    except Exception as e:
        log.append("倒角/圆角失败: %s" % e)

    body = body0()
    try:
        uc = part.UnitCollection
        units = [uc.FindObject("SquareMilliMeter"), uc.FindObject("CubicMilliMeter"),
                 uc.FindObject("Kilogram"), uc.FindObject("MilliMeter"), uc.FindObject("Newton")]
        mp = part.MeasureManager.NewMassProperties(units, 0.99, [body])
        res["volume"] = float(mp.Volume)
        try:
            c = mp.Centroid
            res["centroid"] = [float(c.X), float(c.Y), float(c.Z)]
        except Exception:
            pass
    except Exception as e:
        log.append("体积测量失败: %s" % e)

    try:
        part.SaveAs(OUT_PRT)
        log.append("已保存 PRT: %s" % OUT_PRT)
    except Exception as e:
        log.append("保存失败: %s" % e)

    # STEP 由主程序在 NX 退出后调用 STEP214 独立转换器导出

    try:
        view = part.ModelingViews.WorkView
        view.Fit()
        view.Orient(NXOpen.View.Canned.Isometric, NXOpen.View.ScaleAdjustment.Fit)
        img = part.ModelingViews.CreateImageBuilder(None)
        res["image_note"] = str(img)
    except Exception:
        pass

    res["ok"] = True
    res["message"] = "NX 建模完成"


try:
    main()
except Exception:
    res["message"] = traceback.format_exc()
    log.append(res["message"])
done()
'''


# ---------------------------------------------------------------------------
# 启动健康探针
# ---------------------------------------------------------------------------
# 现象（2026-11-09 起本机复现）：run_journal.exe 和 ugraf.exe 都会单核 100%
# 空转，不建窗口、syslog 停在最后一行、nx_run.log 恒 0 字节；此时连
# 「journal 文件不存在」都不会报错，说明卡在 NX 内核初始化，跟模型几何、
# 跟我们的 journal 都无关。许可服务正常（lmstat 里 500 个 feature 全在、
# 0 in use），所以不是许可问题。
#
# 以前每个零件都要干等 45s 才发现，本进程内再等一次。现在改成：先用 20s
# 的最小 journal 探一次，结论缓存到 %LOCALAPPDATA%\EngiModeler\nx_health.json
# （默认 6 小时），期间 NX 直接标 unsupported（报告里 ⏸️ 而不是 ❌）。
# 用户修好 NX 后跑 tools/nx_health.py --force 立刻复核。

_NX_PROBE_JOURNAL = r"""# -*- coding: utf-8 -*-
import os
mark = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe_mark.txt")
open(mark, "w", encoding="utf-8").write("journal-ok")
"""


def nx_health_cache_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "EngiModeler" / "nx_health.json"


def _nx_health_ttl() -> float:
    try:
        return float(os.environ.get("EA_NX_HEALTH_TTL", "21600"))
    except Exception:
        return 21600.0


def probe_nx_startup(run_journal: Path, nx_root: str, timeout: float = 20.0,
                     log: Callable[[str], None] | None = None) -> tuple:
    """跑一个最小 journal，只看 NX 内核在 timeout 内有没有把标记写出来。

    返回 (ok, reason)。ok=False 时 reason 已带现场证据，可直接进报告。
    """
    import tempfile
    import time as _t

    run_journal = Path(run_journal)
    if not run_journal.exists():
        return False, "未找到 NX 启动器：%s" % run_journal
    work = Path(tempfile.mkdtemp(prefix="nxprobe_"))
    journal = work / "probe.py"
    journal.write_text(_NX_PROBE_JOURNAL, encoding="utf-8")
    mark = work / "probe_mark.txt"
    logf = work / "probe_out.log"
    env = dict(os.environ)
    env["UGII_BASE_DIR"] = str(nx_root)
    env["UGII_ROOT_DIR"] = str(nx_root)
    env.setdefault("UGII_LANG", "english")
    _lic = str(env.get("SPLM_LICENSE_SERVER") or "").strip()
    if _lic and os.path.isfile(_lic):
        try:
            head = open(_lic, "r", errors="ignore").read(20000)
        except Exception:
            head = ""
        if not any(l.strip().upper().startswith("SERVER") for l in head.splitlines()):
            alt = str(env.get("UGS_LICENSE_SERVER") or "").strip()
            if alt:
                env["SPLM_LICENSE_SERVER"] = alt
    ok = False
    startup_dead = False
    proc = None
    try:
        with open(logf, "wb") as fh:
            proc = subprocess.Popen([str(run_journal), str(journal)],
                                    stdin=subprocess.DEVNULL, stdout=fh,
                                    stderr=subprocess.STDOUT, cwd=str(work),
                                    close_fds=True, env=env)
            t0 = _t.time()
            while proc.poll() is None:
                if mark.exists():
                    ok = True
                    break
                if _t.time() - t0 > timeout:
                    startup_dead = True
                    break
                _t.sleep(0.25)
        if not ok and proc is not None:
            deadline = _t.time() + 2.0
            while _t.time() < deadline:
                if mark.exists():
                    ok = True
                    break
                _t.sleep(0.1)
    except Exception as e:
        return False, "NX 启动探针异常：%s" % e
    finally:
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
    if ok:
        return True, "NX 启动正常"
    try:
        sz = logf.stat().st_size
    except Exception:
        sz = -1
    if startup_dead:
        return False, ("NX 启动阶段卡死：%.0fs 内最小 journal 未执行（探针日志 %d 字节，"
                       "目录 %s）。现象为单核 100%% 空转、无窗口；与几何、与许可都无关，"
                       "属于 NX 本体或系统环境故障。" % (timeout, sz, work))
    return False, "NX 启动异常退出（探针日志 %d 字节，目录 %s）" % (sz, work)


def nx_health(run_journal: Path, nx_root: str, use_cache: bool = True,
              timeout: float = 20.0, log: Callable[[str], None] | None = None) -> tuple:
    """带缓存的启动探针。缓存按 nx_root 区分，默认 6 小时过期。"""
    key = str(nx_root).lower()
    # 系统内存不足 / 驱动泄漏时 NX 起不来是「环境」问题，不是 NX 的能力问题。
    # 这种结论绝不能写进 6 小时缓存，否则用户重启后仍被挡在门外。
    try:
        from . import memguard as _memguard
        _mem_ok, _mem_why = _memguard.check(who="NX")
    except Exception:
        _mem_ok, _mem_why = True, ""
    if not _mem_ok:
        return False, "系统内存不足，未探测 NX（此结果不缓存）：" + _mem_why
    cp = nx_health_cache_path()
    ttl = _nx_health_ttl()
    if use_cache and cp.exists():
        try:
            d = json.loads(cp.read_text(encoding="utf-8"))
            if (str(d.get("key") or "") == key
                    and (time.time() - float(d.get("ts") or 0.0)) < ttl):
                return bool(d.get("ok")), str(d.get("reason") or "")
        except Exception:
            pass
    ok, reason = probe_nx_startup(run_journal, nx_root, timeout=timeout, log=log)
    if not ok:
        # 探针期间内存掉下去导致的失败同样不可信，也不缓存。
        try:
            from . import memguard as _memguard2
            _mem_ok2, _ = _memguard2.check(who="NX")
        except Exception:
            _mem_ok2 = True
        if not _mem_ok2:
            return ok, reason
    try:
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text(json.dumps({"key": key, "ts": time.time(), "ok": bool(ok),
                                  "reason": reason}, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    except Exception:
        pass
    return ok, reason


def _q(p: str) -> str:
    return repr(str(p).replace("\\", "\\\\"))


class NxAdapter:
    name = "NX"

    def __init__(self, nx_root: str):
        self.nx_root = nx_root
        self.run_journal = Path(nx_root) / "NXBIN" / "run_journal.exe"
        # 本进程内一旦确认「NX 启动阶段死等」，后续 build 不再各等 4 分钟。
        # 现象：run_journal.exe 单核 100% 空转、无窗口、nx_run.log 恒 0 字节、
        # syslog 停在 "No user customizations read from UGII_USER_DIR"。
        self._startup_dead = ""
        if not self.run_journal.exists():
            try:
                from ..compat import detect_nx
                info = detect_nx()
                if info.found:
                    self.nx_root = info.path
                    self.run_journal = Path(info.path) / "NXBIN" / "run_journal.exe"
            except Exception:
                pass

    def available(self) -> bool:
        return self.run_journal.exists()

    def _export_step(self, prt: Path, stp: Path, log: Callable[[str], None]) -> bool:
        """用 NX 自带的 STEP214 独立转换器导出 AP214（NXOpen 内部 API 只读）。"""
        import tempfile
        step_dir = Path(self.nx_root) / "STEP214UG"
        exe = step_dir / "step214ug.exe"
        if not exe.exists():
            step_dir = Path(self.nx_root) / "NXBIN"
            exe = step_dir / "step214ug.exe"
        if not exe.exists():
            log("未找到 STEP214 转换器 step214ug.exe，跳过 STEP 导出")
            return False
        deff = Path(self.nx_root) / "STEP214UG" / "ugstep214.def"
        if not deff.exists():
            deff = step_dir / "ugstep214.def"
        logf = prt.with_name(prt.stem + ".steplog.txt")
        sep = "\\"
        env = dict(os.environ)
        env["UGII_BASE_DIR"] = str(self.nx_root)
        env["STEP214UG_DIR"] = str(step_dir) + sep
        env["ROSE_DB"] = str(step_dir) + sep
        env["ROSE"] = str(step_dir) + sep
        env["PATH"] = str(Path(self.nx_root) / "NXBIN") + os.pathsep + env.get("PATH", "")
        env.setdefault("UGII_TMP_DIR", tempfile.gettempdir())
        if stp.exists():
            try:
                stp.unlink()
            except Exception:
                pass
        cmd = [str(exe), str(prt), "o=" + str(stp), "l=" + str(logf),
               "d=" + str(deff), "m=online"]
        try:
            p = subprocess.run(cmd, cwd=str(step_dir), env=env, capture_output=True,
                               text=True, encoding="mbcs", errors="replace", timeout=600)
        except Exception as e:
            log("STEP 转换异常：%s" % e)
            return False
        if stp.exists() and stp.stat().st_size > 500:
            log("已导出 STEP（AP214，%d KB）" % (stp.stat().st_size // 1024))
            return True
        tail = ((p.stdout or "") + (p.stderr or "")).strip()[-300:]
        log("STEP 转换失败：" + " ".join(tail.split()))
        return False

    def build(self, spec: PartSpec, out_dir: str,
              log: Callable[[str], None] = lambda m: None) -> BuildResult:
        t0 = time.time()
        res = BuildResult(cad="NX", expected=0.0)
        _ok, _msg = _mem_check(1200.0, 92.0, "NX")
        if not _ok:
            # 内存不够时，先把**其它** CAD 关掉（用户要求同时只开一个），
            # 再重判一次；否则 SW 占着的 500+ MB 会让 CATIA/NX 永远起不来。
            try:
                from .guard import free_others as _free_others
                _free_others("NX", log, fast=True)
            except Exception:
                pass
            _ok, _msg = _mem_check(1200.0, 92.0, "NX")
        if not _ok:
            res.message = _msg
            log("\u26a0 " + _msg)
            res.elapsed = time.time() - t0
            return res
        if _msg:
            log(_msg)
        res.expected = part_volume(spec)
        out = Path(out_dir).resolve()
        out.mkdir(parents=True, exist_ok=True)
        work = out / "_nx"
        work.mkdir(exist_ok=True)

        spec_path = work / "spec.json"
        spec_path.write_text(spec.to_json(), encoding="utf-8")
        result_path = work / "result.json"
        if result_path.exists():
            result_path.unlink()
        prt = out / f"{spec.file_stem}.prt"
        stp = out / f"{spec.file_stem}.stp"
        try:
            for _p in (prt, stp):
                if _p.exists():
                    _p.unlink()
        except Exception:
            pass
        tmp = work / "_build.prt"
        journal = work / "journal.py"

        sheet_json = "None"
        if spec.sheet_t > 0 and spec.sheet_L > 0:
            import json as _json2
            sheet_json = _json2.dumps({"L": spec.sheet_L, "W": spec.sheet_W,
                                       "t": spec.sheet_t, "h": spec.flange_h})
        base_outline_json = "None"
        if (spec.base_kind == "rect"
                and (str(getattr(spec, "end_left", "square")).lower() == "arc"
                     or str(getattr(spec, "end_right", "square")).lower() == "arc")):
            base_outline_json = repr({
                "left": str(getattr(spec, "end_left", "square")).lower() == "arc",
                "right": str(getattr(spec, "end_right", "square")).lower() == "arc"})
        revolve_json = "None"
        if getattr(spec, "revolve_profile", None):
            # repr?journal ? Python ???json ? true/false/null ??????
            revolve_json = repr(list(spec.revolve_profile))
        sweeps_json = "None"
        if getattr(spec, "sweeps", None):
            # repr, not json.dumps: the journal is Python source, so JSON
            # true/false/null would be invalid there.
            sweeps_json = repr(list(spec.sweeps))
        gear_json = "None"
        if spec.teeth > 0 and spec.module > 0:
            from ..gear import gear_profile
            import json as _json
            gear_json = _json.dumps({"points": [[round(x, 4), round(y, 4)] for x, y in
                                                 gear_profile(spec.module, spec.teeth)],
                                     "face_width": spec.face_width, "bore": spec.bore,
                                     "keyway_w": spec.keyway_w, "keyway_d": spec.keyway_d})
        text = (JOURNAL
                .replace("__SPEC__", _q(spec_path))
                .replace("__RESULT__", _q(result_path))
                .replace("__TMP__", _q(tmp))
                .replace("__OUT__", _q(prt))
                .replace("__STP__", _q(stp))
                .replace("__PNG__", _q(out / f"{spec.file_stem}.png"))
                .replace("__CHAMFER__", repr(float(spec.chamfer)))
                .replace("__FILLET__", repr(float(spec.fillet)))
                .replace("__CORNER_R__", repr(float(spec.corner_radius)))
                .replace("__SHELL_T__", repr(float(getattr(spec, "shell_t", 0.0) or 0.0)))
                .replace("__SHELL_OPEN__", repr(str(getattr(spec, "shell_open", "top") or "top").lower()))
                .replace("__DOME_H__", repr(float(getattr(spec, "dome_h", 0.0) or 0.0)))
                .replace("__DOME_ELL__", repr(bool(getattr(spec, "dome_elliptic", True))))
                .replace("__GEAR__", gear_json)
                .replace("__SHEET__", sheet_json)
                .replace("__BASE_OUTLINE__", base_outline_json)
                .replace("__SWEEPS__", sweeps_json)
                .replace("__REVOLVE__", revolve_json))
        journal.write_text(text, encoding="utf-8")

        if not self.available():
            res.message = f"未找到 NX 启动器：{self.run_journal}"
            res.unsupported = True
            res.unsupported_reason = res.message
            return res
        if not self._startup_dead:
            # 先花 20s 探一次（结论缓存 6 小时），别让每个零件都干等 45s 甚至 4 分钟。
            # 探针会真的把 ugraf 拉起来，所以它也在单实例闸门里面跑：用户要求
            # 「启动任意一款 CAD 前先关掉另外两家」，探针不能例外。
            _probe_guarded = False
            try:
                _cad_acquire("NX", log)
                _probe_guarded = True
            except Exception as _e:
                _ok, _why = False, "单实例闸门未取得：%s" % _e
            if _probe_guarded:
                try:
                    _ok, _why = nx_health(self.run_journal, str(self.nx_root), log=log)
                except Exception as _e:
                    _ok, _why = True, ""   # 探针自身出错时不要误判 NX 不可用
                finally:
                    _cad_release(log)
            if not _ok:
                self._startup_dead = _why
                log("NX 启动探针失败：%s" % _why)
        if self._startup_dead:
            # 已经确认这台机器上 NX 起不来，别让每个零件都再等一遍超时。
            res.message = (f"NX 已在本进程内判定不可用，跳过：{self._startup_dead}"
                           f"（重试请重开界面，或清掉 EA_NX_SKIP_STARTUP）")
            res.details["startup_dead"] = True
            res.unsupported = True
            res.unsupported_reason = self._startup_dead
            return res
        log(f"启动 NX 后台建模（{self.run_journal.name}）…")
        _cad_acquire("NX", log)
        try:
            # 看门狗：run_journal 卡住（例如内核在相切几何上死循环）时，
            # 到点直接 kill，不让整条流水线挂十几分钟。
            watchdog_s = float(os.environ.get("EA_NX_TIMEOUT", "240"))
            # NX journal log goes to a FILE, never to a pipe: ugraf inherits
            # handles, so a PIPEr would never reach EOF and callers would hang
            # until NX exits (same bug fixed in catia.py).
            _nx_log_path = work / "nx_run.log"
            _nx_out = open(_nx_log_path, "wb")
            # UGII_BASE_DIR / UGII_ROOT_DIR 必须显式注入：缺这两个变量时
            # NX 12 会在启动阶段（启动画面）死等，syslog 只写到
            # "No user customizations read from UGII_USER_DIR" 就断。
            _env = dict(os.environ)
            _env["UGII_BASE_DIR"] = str(self.nx_root)
            _env["UGII_ROOT_DIR"] = str(self.nx_root)
            _env.setdefault("UGII_LANG", "simpl_chinese")
            # SPLM_LICENSE_SERVER 指向「没有 SERVER 行的裸 license 文件」时，
            # Flexera 解析不到 vendor daemon（syslog 里 Daemon Version = 0.0、
            # File Type = No Type），NX 会卡在启动阶段。此时优先连本机已在跑的
            # 许可服务（UGS_LICENSE_SERVER，实测 27800@localhost 能拿到 11.0 daemon）。
            _lic = str(_env.get("SPLM_LICENSE_SERVER") or "").strip()
            if _lic and os.path.isfile(_lic):
                try:
                    _head = open(_lic, "r", errors="ignore").read(20000)
                except Exception:
                    _head = ""
                _has_server = any(_l.strip().upper().startswith("SERVER")
                                  for _l in _head.splitlines())
                _alt = str(_env.get("UGS_LICENSE_SERVER") or "").strip()
                if not _has_server and _alt:
                    _env["SPLM_LICENSE_SERVER"] = _alt
                    log(f"许可文件无 SERVER 行，改用许可服务 {_alt}")
            proc = subprocess.Popen([str(self.run_journal), str(journal)],
                                    stdin=subprocess.DEVNULL,
                                    stdout=_nx_out, stderr=subprocess.STDOUT,
                                    cwd=str(work), close_fds=True, env=_env)
            t_start = time.time()
            killed = False
            startup_dead = False
            # 启动宽限：journal 的第一行就是写日志，超过 grace 秒日志还是 0 字节，
            # 说明 NX 根本没跑到执行 journal 那一步（卡在启动画面/内核初始化）。
            # 这种情况再等满 watchdog 只是白等，直接判死并记住。
            grace_s = float(os.environ.get("EA_NX_STARTUP_GRACE", "45"))
            grace_done = False
            while proc.poll() is None:
                el = time.time() - t_start
                if not grace_done and el > grace_s:
                    grace_done = True
                    try:
                        _sz = _nx_log_path.stat().st_size if _nx_log_path.exists() else 0
                    except Exception:
                        _sz = 0
                    if _sz == 0:
                        startup_dead = True
                        killed = True
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        break
                if el > watchdog_s:
                    killed = True
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    break
                time.sleep(0.5)
            try:
                proc.wait(timeout=20)
            except Exception:
                pass
            try:
                _nx_out.close()
            except Exception:
                pass
            try:
                out_txt = _nx_log_path.read_bytes().decode("mbcs", "replace")
            except Exception:
                out_txt = ""
            if startup_dead:
                self._startup_dead = (f"NX 启动阶段卡死（{grace_s:.0f}s 内 journal 未开始执行）；"
                                      f"syslog 与 nx_run.log 见 {work}")
                res.message = (self._startup_dead + "。已知诱因：Windows 11 25H2 上"
                               "NX 12 启动画面/图形宿主初始化挂起；请在 NX 图形界面里手动"
                               "打开一次确认，或改用 SolidWorks / CATIA 后端。")
                res.details["journal_log"] = str(_nx_log_path)
                res.details["startup_dead"] = True
                res.unsupported = True
                res.unsupported_reason = self._startup_dead
                return res
            if killed:
                res.message = (f"NX 建模超时（{watchdog_s:.0f}s）已强制终止。"
                               f"优先按 NX 启动/内核环境故障排查（残留 ugraf、"
                               f"许可、启动器），不要先怀疑几何；日志：{_nx_log_path}")
                res.details["journal_log"] = str(_nx_log_path)
                return res
            if "Execution was successful" not in out_txt and proc.returncode != 0:
                res.message = f"NX 日志执行失败：{out_txt[-400:]}"
                return res
        except subprocess.TimeoutExpired:
            res.message = "NX 建模超时（900 秒）"
            return res
        except Exception as e:
            res.message = f"NX 调用异常：{e}"
            return res
        finally:
            try:
                _cad_release(log)
            except Exception:
                pass

        if result_path.exists():
            data = json.loads(result_path.read_text(encoding="utf-8"))
            res.volume = float(data.get("volume") or 0.0)
            if data.get("centroid"):
                res.details["centroid"] = data["centroid"]
            res.message = data.get("message", "")
            res.ok = bool(data.get("ok"))
            _failed = [str(x) for x in (data.get("failed") or [])]
            if _failed:
                # NX 侧建了但没去料 / 圆顶面没做出来的特征，必须算失败：
                # 否则报告只比理论体积，实体少料也发现不了。
                res.failures.extend(_failed)
                res.ok = False
                res.message = (res.message or "") + "｜未完成：" + "、".join(_failed)
            # NX 的 volume 来自 MeasureManager.NewMassProperties（实测实体），
            # 不是理论值 —— 与 SW/CATIA 统一打标，便于上层区分。
            res.details["volume_source"] = "measured"
            if data.get("sweep_built"):
                res.details["sweep_built"] = data.get("sweep_built")
            for line in data.get("log", [])[-40:]:
                try:
                    log(line)
                except Exception:
                    pass
        if not prt.exists():
            # 中文名带 "/" 时 NX 会去写一个不存在的子目录，SaveAs 不报错、文件也没有。
            # 这种情况绝不能算成功（否则体积照用、报告全绿）。
            res.ok = False
            res.message = (res.message or "") + f"｜PRT 未落盘：{prt}"
        res.part_path = str(prt) if prt.exists() else ""
        if res.ok and prt.exists():
            try:
                self._export_step(prt, stp, log)
            except Exception as e:
                log("STEP 导出异常：%s" % e)
        res.step_path = str(stp) if stp.exists() else ""
        res.elapsed = time.time() - t0
        # 实测体积为 0 说明 MassProperties 没拿到实体 —— 不能算建成。
        # （历史教训：res.volume 曾直接塞理论值，导致"实体少了 2.5mm 料"被判 err=0.000%）
        if res.ok and res.volume <= 0:
            res.ok = False
            res.message = "NX 建模返回成功，但实体体积测量为空/为 0"
        return res




