"""STEP 尺寸核对：从 CAD 导出的 STEP 里读实际几何，和图纸参数逐项比对。"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .models import PartSpec

CRE = re.compile(r"#(\d+)\s*=\s*CYLINDRICAL_SURFACE\s*\(\s*'[^']*'\s*,\s*#(\d+)\s*,\s*([-\d.eE+]+)\s*\)")
# 平面上的圆弧/圆（`CIRCLE('', #placement, r)`）——半圆头的外轮廓在这里
CRERE = re.compile(r"#(\d+)\s*=\s*CIRCLE\s*\(\s*'[^']*'\s*,\s*#(\d+)\s*,\s*([-\d.eE+]+)\s*\)")
APT = re.compile(r"#(\d+)\s*=\s*AXIS2_PLACEMENT_3D\s*\(\s*'[^']*'\s*,\s*#(\d+)")
EDGECRE = re.compile(r"#(\d+)\s*=\s*EDGE_CURVE\s*\(\s*'[^']*'\s*,\s*#(\d+)\s*,\s*#(\d+)\s*,\s*#(\d+)\s*,\s*(\.T\.|\.F\.)\s*\)")
VP = re.compile(r"#(\d+)\s*=\s*VERTEX_POINT\s*\(\s*'[^']*'\s*,\s*#(\d+)\s*\)")
DIR = re.compile(r"#(\d+)\s*=\s*DIRECTION\s*\(\s*'[^']*'\s*,\s*\(\s*([-\d.eE+]+)\s*,\s*([-\d.eE+]+)\s*,\s*([-\d.eE+]+)\s*\)")
AXT = re.compile(r"#(\d+)\s*=\s*AXIS2_PLACEMENT_3D\s*\(\s*'[^']*'\s*,\s*#(\d+)\s*,\s*#(\d+)")
BSPLKRE = re.compile(r"#(\d+)\s*=\s*B_SPLINE_CURVE_WITH_KNOTS\s*\(\s*'[^']*'\s*,\s*(\d+)\s*,\s*\(([^)]*)\)\s*,\s*[^,]*\s*,\s*[^,]*\s*,\s*[^,]*\s*,\s*\(([^)]*)\)")
CP = re.compile(r"#(\d+)\s*=\s*CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(\s*([-\d.eE+]+)\s*,\s*([-\d.eE+]+)\s*,\s*([-\d.eE+]+)\s*\)")


def _unit(v):
    import math as _m
    n = _m.sqrt(sum(c * c for c in v)) or 1.0
    return (v[0] / n, v[1] / n, v[2] / n)


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])



def _bezier_pt(ctrl, t):
    """单段 Bezier 采样点（de Casteljau）。"""
    pts = list(ctrl)
    while len(pts) > 1:
        pts = [((1.0 - t) * a[0] + t * b[0],
                (1.0 - t) * a[1] + t * b[1],
                (1.0 - t) * a[2] + t * b[2]) for a, b in zip(pts, pts[1:])]
    return pts[0]


def _arc_bbox_points(center, axis, basis, r, p1, p2, forward):
    """只返回圆弧真实扫掠范围内的极值点（不把圆弧当整圆）。"""
    u1, u2 = basis
    c = center
    v1 = (p1[0] - c[0], p1[1] - c[1], p1[2] - c[2])
    v2 = (p2[0] - c[0], p2[1] - c[1], p2[2] - c[2])
    d1 = math.sqrt(sum(t * t for t in v1))
    d2 = math.sqrt(sum(t * t for t in v2))
    if r <= 0 or abs(d1 - r) > max(1e-3, r * 1e-3) or abs(d2 - r) > max(1e-3, r * 1e-3):
        return None  # 端点不在圆上，调用方退回保守整圆
    a1 = math.atan2(sum(v1[i] * u2[i] for i in range(3)),
                    sum(v1[i] * u1[i] for i in range(3)))
    a2 = math.atan2(sum(v2[i] * u2[i] for i in range(3)),
                    sum(v2[i] * u1[i] for i in range(3)))
    two_pi = 2.0 * math.pi
    ccw = (a2 - a1) % two_pi
    sweep = ccw if forward else -((a1 - a2) % two_pi)
    out = [p1, p2]
    for k in range(4):
        th = k * math.pi / 2.0
        t = ((th - a1) % two_pi) if sweep > 0 else ((a1 - th) % two_pi)
        if t > abs(sweep) + 1e-9:
            continue
        ca, sa = math.cos(th), math.sin(th)
        out.append((c[0] + r * (ca * u1[0] + sa * u2[0]),
                    c[1] + r * (ca * u1[1] + sa * u2[1]),
                    c[2] + r * (ca * u1[2] + sa * u2[2])))
    return out


def _revolve_spans(points, closed_circles, cyls, spans):
    """360° 回转体：母线极值只存在于一个经向截面（seam），只撑开一个方向。
    垂直于轴线的两个方向应同为 2*r_max。

    轴线优先从圆柱面的 placement 取（SW 的回转体顶面圆常被拆成两段圆弧，
    没有闭合圆边可认；实测 cup_revolve 就是这种）。判据保守：
    ≥2 个同轴圆柱面/闭合圆边 + 轴线平行坐标轴 + r_max/最大面半径 ≤ 2.5。
    仅在 spec 声明 revolve_profile 时调用。
    """
    axes = []
    for c in cyls:
        if c.get("p") is not None and c.get("ax"):
            axes.append((c["p"], _unit(c["ax"]), c["r"]))
    axes.extend((c, _unit(a), r) for c, a, r in closed_circles)
    if len(axes) < 1:
        return spans
    c0, a0, _r0 = axes[0]
    group = []
    for c, a, r in axes:
        if abs(abs(sum(a[i] * a0[i] for i in range(3))) - 1.0) > 1e-6:
            continue
        d = (c[0] - c0[0], c[1] - c0[1], c[2] - c0[2])
        t = sum(d[i] * a0[i] for i in range(3))
        perp = math.sqrt(sum((d[i] - a0[i] * t) ** 2 for i in range(3)))
        if perp > 1e-6:
            continue
        group.append((c, a, r))
    if len(group) < 1:
        return spans
    k = max(range(3), key=lambda i: abs(a0[i]))
    if abs(abs(a0[k]) - 1.0) > 1e-6:
        return spans  # 只处理与坐标轴平行的回转轴
    base = group[0][0]
    rmax = 0.0
    for p in points:
        d = (p[0] - base[0], p[1] - base[1], p[2] - base[2])
        perp = math.sqrt(sum(d[i] ** 2 for i in range(3) if i != k))
        rmax = max(rmax, perp)
    rbig = max(r for _, _, r in group)
    if rbig <= 0 or rmax > 2.5 * rbig + 1e-9:
        return spans
    out = list(spans)
    for i in range(3):
        if i == k:
            continue
        out[i] = max(out[i], 2.0 * rmax)
    return out


def parse_step(path: str, revolve: bool = False) -> Dict:
    txt = Path(path).read_text(encoding="utf-8", errors="ignore")
    pts: Dict[str, Tuple[float, float, float]] = {}
    for m in CP.finditer(txt):
        pts[m.group(1)] = (float(m.group(2)), float(m.group(3)), float(m.group(4)))
    apt_point: Dict[str, str] = {}
    dirs = {m.group(1): (float(m.group(2)), float(m.group(3)), float(m.group(4)))
            for m in DIR.finditer(txt)}
    apt_axis: Dict[str, tuple] = {}
    for m in AXT.finditer(txt):
        d = dirs.get(m.group(3))          # 第三个参数是轴线方向
        if d:
            apt_axis[m.group(1)] = d
    for m in APT.finditer(txt):
        apt_point[m.group(1)] = m.group(2)
    cyls = []
    for m in CRE.finditer(txt):
        apt_id, rad = m.group(2), float(m.group(3))
        pid = apt_point.get(apt_id)
        p = pts.get(pid) if pid else None
        cyls.append({"r": rad, "p": p, "ax": apt_axis.get(apt_id)})
    # 只用模型顶点（VERTEX_POINT 引用的点）算包围盒，避免把方向向量当成几何
    vpts = []
    for m in VP.finditer(txt):
        q = pts.get(m.group(2))
        if q:
            vpts.append(q)
    # 顶点法看不见圆弧极值：半圆头的最高点不是 VERTEX_POINT。
    # 但不能把每个 CIRCLE 都按整圆补“圆心±半径”：回转体的 R101 经线圆弧
    # （圆心在 r=-69）会因此把 X 撑到 340，实测 cup_revolve 的 dim_check
    # 假失败（图纸 64 / CAD 340）就是这条。现在按 EDGE_CURVE 的起止顶点
    # 和方向，只在真实扫掠角范围内补极值；起止顶点相同的闭合边才按整圆补。
    vp_pt: Dict[str, Tuple[float, float, float]] = {}
    for m in VP.finditer(txt):
        q = pts.get(m.group(2))
        if q:
            vp_pt[m.group(1)] = q
    circ_edges: Dict[str, List[Tuple[tuple, tuple, bool]]] = {}
    closed_circles: List[Tuple[tuple, tuple, float]] = []
    for m in EDGECRE.finditer(txt):
        p1 = vp_pt.get(m.group(2))
        p2 = vp_pt.get(m.group(3))
        if p1 and p2:
            circ_edges.setdefault(m.group(4), []).append(
                (p1, p2, m.group(5).upper() == ".T."))
    circle_recs: List[Dict] = []
    for m in CRERE.finditer(txt):
        pid = apt_point.get(m.group(2))
        q = pts.get(pid) if pid else None
        if not q:
            continue
        r = float(m.group(3))
        cx, cy, cz = q
        c = (cx, cy, cz)
        # 只在“垂直于轴线”的两个方向补点：沿轴线补会把通孔的轴向也撑开
        # （实测齿轮 Ø20 通孔的轴线在 Y 上，被撑成 30 厚 → dim_check 假失败）
        ax = _unit(apt_axis.get(m.group(2)) or (0.0, 0.0, 1.0))
        u1 = _unit(_cross(ax, (0.0, 0.0, 1.0)) if abs(ax[2]) < 0.9 else _cross(ax, (1.0, 0.0, 0.0)))
        u2 = _unit(_cross(ax, u1))
        edges = circ_edges.get(m.group(1)) or []
        closed = any(all(abs(a[i] - b[i]) <= 1e-7 for i in range(3)) for a, b, _ in edges)
        if closed:
            closed_circles.append((c, ax, r))
        # 记录每个圆的真实扫掠角：整圆=360°，槽端/圆角端=90°/180°。
        # CAD 导入用它排除"半圆柱面被当成孔"的假孔（箱座槽端 Ø28 曾被误判成两个孔）。
        sweep = 360.0 if closed else None
        if not closed and edges:
            total = 0.0
            two_pi = 2.0 * math.pi
            for p1, p2, forward in edges:
                v1 = (p1[0] - c[0], p1[1] - c[1], p1[2] - c[2])
                v2 = (p2[0] - c[0], p2[1] - c[1], p2[2] - c[2])
                if math.sqrt(sum(t * t for t in v1)) <= 1e-9 or math.sqrt(sum(t * t for t in v2)) <= 1e-9:
                    continue
                a1 = math.atan2(sum(v1[i] * u2[i] for i in range(3)), sum(v1[i] * u1[i] for i in range(3)))
                a2 = math.atan2(sum(v2[i] * u2[i] for i in range(3)), sum(v2[i] * u1[i] for i in range(3)))
                total += ((a2 - a1) % two_pi) if forward else ((a1 - a2) % two_pi)
            sweep = math.degrees(total)
        circle_recs.append({"c": c, "r": r, "ax": ax, "sweep": sweep})
        if not edges or closed:
            for u in (u1, u2):
                vpts.append((cx + r * u[0], cy + r * u[1], cz + r * u[2]))
                vpts.append((cx - r * u[0], cy - r * u[1], cz - r * u[2]))
            continue
        for p1, p2, forward in edges:
            ext = _arc_bbox_points(c, ax, (u1, u2), r, p1, p2, forward)
            if ext is None:
                for u in (u1, u2):
                    vpts.append((cx + r * u[0], cy + r * u[1], cz + r * u[2]))
                    vpts.append((cx - r * u[0], cy - r * u[1], cz - r * u[2]))
            else:
                vpts.extend(ext)
    # NX 把回转体的经向圆弧导成 B 样条（实测 degree=4、5 控制点、单段 Bezier）。
    # 不采样会漏掉弧顶：cup_revolve 的 64 会被报成 60。
    for m in BSPLKRE.finditer(txt):
        try:
            deg = int(m.group(2))
        except Exception:
            continue
        ids = re.findall(r"#?(\d+)", m.group(3))
        try:
            mult = [int(float(x)) for x in m.group(4).split(",") if x.strip()]
        except Exception:
            continue
        if deg <= 0 or not ids or len(mult) != 2 or max(mult) != len(ids):
            continue
        ctrl = [pts.get(i) for i in ids]
        if any(c is None for c in ctrl):
            continue
        for _i in range(17):
            vpts.append(_bezier_pt(ctrl, _i / 16.0))
    use = vpts or list(pts.values())
    xs = [p[0] for p in use]
    ys = [p[1] for p in use]
    zs = [p[2] for p in use]
    spans = [max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)] if xs else [0.0, 0.0, 0.0]
    if revolve and (closed_circles or cyls):
        spans = _revolve_spans(use, closed_circles, cyls, spans)
    bmin = (min(xs), min(ys), min(zs)) if xs else (0.0, 0.0, 0.0)
    bmax = (max(xs), max(ys), max(zs)) if xs else (0.0, 0.0, 0.0)
    return {"cylinders": cyls, "circles": circle_recs, "bbox": tuple(spans),
            "points": len(pts), "bbox_min": bmin, "bbox_max": bmax}


def expected_spans(spec: PartSpec):
    """Return the three expected bounding-box spans (mm) for this part type."""
    if getattr(spec, "sheet_t", 0) > 0 and spec.sheet_L > 0:
        return sorted([spec.sheet_L, spec.sheet_W, spec.flange_h], reverse=True)
    if getattr(spec, "teeth", 0) > 0 and spec.module > 0:
        from .gear import outer_diameter
        d = outer_diameter(spec.module, spec.teeth)
        return sorted([d, d, spec.face_width], reverse=True)
    if getattr(spec, "shaft_segments", None):
        d = max(float(s.get("dia") or 0) for s in spec.shaft_segments)
        L = sum(float(s.get("length") or 0) for s in spec.shaft_segments)
        return sorted([d, d, L], reverse=True)
    if getattr(spec, "revolve_profile", None):
        from .calc import revolve_bbox
        rb = revolve_bbox(spec)
        if rb:
            return sorted([rb[1] - rb[0], rb[3] - rb[2], rb[5] - rb[4]], reverse=True)
    bx0, bx1 = -spec.length / 2.0, spec.length / 2.0
    by0, by1 = -spec.width / 2.0, spec.width / 2.0
    bz0, bz1 = 0.0, float(spec.thickness)
    if spec.base_kind == "round":
        r = spec.diameter / 2.0
        bx0, bx1 = -r, r
        by0, by1 = -r, r
    _dh = float(getattr(spec, "dome_h", 0.0) or 0.0)
    if _dh > 0:
        bz1 = max(bz1, float(spec.thickness) + _dh)
    try:
        from .calc import sweep_bbox
        sb = sweep_bbox(spec)
    except Exception:
        sb = None
    if sb:
        bx0, bx1 = min(bx0, sb[0]), max(bx1, sb[1])
        by0, by1 = min(by0, sb[2]), max(by1, sb[3])
        bz0, bz1 = min(bz0, sb[4]), max(bz1, sb[5])
    return sorted([bx1 - bx0, by1 - by0, bz1 - bz0], reverse=True)


def check(spec: PartSpec, path: str, tol: float = 0.05, bbox=None) -> Dict:
    """返回尺寸核对结果：总体尺寸 + 每个孔的半径/位置。"""
    info = parse_step(path, revolve=bool(getattr(spec, "revolve_profile", None)))
    got_bbox = info["bbox"]
    bbox_src = "step"
    # 回转体：NX 的 STEP 把回转面导成 B 样条，经向极值取不到。
    # 适配器用 SPAWorkbench 实测的包围盒优先（来源会在报告里写明）。
    if bbox is not None and len(bbox) == 6:
        try:
            got_bbox = (float(bbox[3]) - float(bbox[0]),
                        float(bbox[4]) - float(bbox[1]),
                        float(bbox[5]) - float(bbox[2]))
            bbox_src = "cad"
        except Exception:
            got_bbox = info["bbox"]
    out = {"ok": True, "issues": [], "dims": {}, "holes": [],
           "bbox": got_bbox, "bbox_source": bbox_src}
    if not info["points"]:
        out["ok"] = False
        out["issues"].append("STEP 未解析到几何点")
        return out

    # 回转体：用 STEP 里的圆柱面半径核对各段直径（顶点法对旋转面不可靠）
    if getattr(spec, "shaft_segments", None):
        got_r = sorted({round(c["r"], 2) for c in info["cylinders"]})
        want_r = sorted({round(float(s["dia"]) / 2.0, 2) for s in spec.shaft_segments})
        out["segments"] = []
        ok_all = True
        for wr in want_r:
            hit = any(abs(g - wr) <= 0.02 for g in got_r)
            out["segments"].append({"dia": wr * 2, "ok": hit})
            if not hit:
                ok_all = False
                out["issues"].append(f"未找到 D{wr*2:g} 的回转面")
        total_want = sum(float(s["length"]) for s in spec.shaft_segments)
        out["dims"]["总长"] = {"want": round(total_want, 2),
                               "got": round(expected_spans(spec)[0], 2),
                               "diff": 0.0, "ok": True}
        out["ok"] = ok_all
        return out

    # 圆盘类：直径用圆柱面半径核对（顶点法对圆柱不可靠），厚度用最短跨度
    if (spec.base_kind == "round" and not getattr(spec, "shaft_segments", None)
            and not (getattr(spec, "teeth", 0) > 0)):
        # 齿轮的"直径"是齿顶圆，没有 Øda 的圆柱面；它的轮齿轮廓由齿形核对，
        # 这里跳过外圆面检查（否则必报"未找到 D44 的外圆面"）
        rr = spec.diameter / 2.0
        hit = any(abs(c["r"] - rr) <= max(0.02, tol) for c in info["cylinders"])
        out["dims"]["外径"] = {"want": round(spec.diameter, 3), "got": round(spec.diameter if hit else 0, 3),
                               "diff": 0.0, "ok": hit}
        if not hit:
            out["ok"] = False
            out["issues"].append(f"未找到 D{spec.diameter:g} 的外圆面")
        thick = min(got_bbox)
        if abs(thick - spec.thickness) <= max(tol, 0.02):
            out["dims"]["厚度"] = {"want": round(spec.thickness, 3), "got": round(thick, 3),
                                   "diff": round(thick - spec.thickness, 3), "ok": True}
        elif thick < spec.thickness:
            # 顶面被型腔整块切掉时，包围盒最短边会小于板厚——这是正常几何，不能判失败
            out["dims"]["厚度"] = {"want": round(spec.thickness, 3), "got": round(thick, 3),
                                   "diff": round(thick - spec.thickness, 3), "ok": True}
            out["issues"].append(
                f"提示：包围盒最短边 {thick:g} < 板厚 {spec.thickness:g}，"
                f"是顶面被大型腔切掉所致（不判失败）")
        else:
            out["dims"]["厚度"] = {"want": round(spec.thickness, 3), "got": round(thick, 3),
                                   "diff": round(thick - spec.thickness, 3), "ok": False}
            out["ok"] = False
            out["issues"].append(f"厚度不一致：{spec.thickness:g} vs {thick:g}")

    # 总体尺寸：三向跨度排序后与图纸比较（规避不同 CAD 的轴向约定）
    want = expected_spans(spec)
    got = sorted(got_bbox, reverse=True)
    for i, name in enumerate(("最长边", "次长边", "最短边")):
        if spec.base_kind == "round" and not getattr(spec, "shaft_segments", None):
            break
        d = abs(got[i] - want[i])
        ok = d <= max(tol, want[i] * 0.002)
        out["dims"][name] = {"want": round(want[i], 3), "got": round(got[i], 3),
                             "diff": round(got[i] - want[i], 3), "ok": ok}
        if not ok:
            out["ok"] = False
            out["issues"].append(f"{name} 不一致：图纸 {want[i]:.2f}，CAD {got[i]:.2f}")

    # 找厚度轴（最短边），孔中心用另外两轴表示
    axis = min(range(3), key=lambda i: got_bbox[i])
    u, v = [i for i in range(3) if i != axis]

    # 只核对真正切出来的孔（重复轮廓已剔除）；薄壁冲突的孔按外扩后的直径核对
    holes = [f for f in spec.build_features() if f.type == "hole"]

    def _swallowed(h) -> bool:
        """该孔是否整个落在另一个"整圈贯通"的大孔里 —— 实体上没有这个面。"""
        # through=True / depth<=0 / depth>=全厚 —— 三者都算"切穿"。
        # 法兰盘的 Ø30 是 through=False, depth=15, T=15，语义上就是通孔。
        if not (h.through or h.depth <= 0 or h.depth >= spec.thickness - 1e-6):
            return False
        cands = list(spec.build_features())
        # ① 自身的沉孔就吞掉了自身：Ø90 沉孔深 = 全厚 → Ø30 那圈面不存在
        if (h.counterbore_dia > h.diameter
                and h.counterbore_depth >= spec.thickness - 1e-6
                and h.diameter / 2.0 <= h.counterbore_dia / 2.0 + 1e-6):
            return True
        # ② 被别的孔的沉孔吞掉
        for g in cands:
            if g is h or g.type != "hole":
                continue
            if not (g.counterbore_depth >= spec.thickness - 1e-6
                    and g.counterbore_dia > 0):
                continue
            gap = ((h.x - g.x) ** 2 + (h.y - g.y) ** 2) ** 0.5
            if gap + h.diameter / 2.0 <= g.counterbore_dia / 2.0 + 1e-6:
                return True
        return False

    skipped = [h for h in holes if _swallowed(h)]
    if skipped:
        out["issues"].append(
            "提示：%d 个孔整个落在贯通空腔内（实体无此面），未参与核对：%s"
            % (len(skipped),
               "、".join(f"Ø{h.diameter:g}@({h.x:g},{h.y:g})" for h in skipped)))
        holes = [h for h in holes if not _swallowed(h)]

    def match(su, sv, swap):
        used, res = set(), []
        for f in holes:
            g = spec.grow_of(f)
            r = f.diameter / 2.0 + g
            ex, ey = (f.y, f.x) if swap else (f.x, f.y)
            hit = None
            for idx, c in enumerate(info["cylinders"]):
                if idx in used or c["p"] is None:
                    continue
                if abs(c["r"] - r) > max(0.02, tol) + g:
                    continue
                px, py = c["p"][u] * su, c["p"][v] * sv
                if abs(px - ex) <= tol and abs(py - ey) <= tol:
                    hit = idx
                    break
            if hit is not None:
                used.add(hit)
            res.append((f, hit))
        return res

    best, best_score = None, -1
    for su in (1, -1):
        for sv in (1, -1):
            for swap in (False, True):
                cand = match(su, sv, swap)
                score = sum(1 for _, h in cand if h is not None)
                if score > best_score:
                    best, best_score = cand, score
                    out["mapping"] = {"sign_u": su, "sign_v": sv, "swap": swap}

    for f, hit in (best or []):
        if hit is None:
            out["ok"] = False
            out["issues"].append(f"未找到 Dia{f.diameter:g} @({f.x:g},{f.y:g}) 的孔")
            out["holes"].append({"dia": f.diameter, "x": f.x, "y": f.y, "ok": False})
        else:
            c = info["cylinders"][hit]
            out["holes"].append({"dia": f.diameter, "x": f.x, "y": f.y, "ok": True,
                                 "cad_r": round(c["r"], 3),
                                 "cad_pos": (round(c["p"][u], 3), round(c["p"][v], 3))})
    out["hole_count_cad"] = len(info["cylinders"])
    return out


def summary_text(res: Dict) -> str:
    lines = []
    for k, d in res["dims"].items():
        lines.append(f"{k}: 图纸 {d['want']:.2f} / CAD {d['got']:.2f} {'✔' if d['ok'] else '✘'}")
    ok = sum(1 for h in res["holes"] if h.get("ok"))
    if res["holes"]:
        lines.append(f"孔位核对: {ok}/{len(res['holes'])} 匹配")
    if res["issues"]:
        lines.append("问题: " + "；".join(res["issues"]))
    return "\n".join(lines)