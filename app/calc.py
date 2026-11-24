"""Analytic geometry / volume helpers (ASCII-only source to avoid encoding issues)."""
from __future__ import annotations

import math
from typing import List, Tuple

from .geom import (Seg, circle, flatten, outline_area, outline_area_exact,
                   rect_outline, segs_from_profile, segs_perimeter,
                   slot_outline)
from .models import Feature, PartSpec


def _x_base_area_legacy(spec: PartSpec) -> float:
    if spec.base_kind == "round":
        return math.pi * (spec.diameter / 2.0) ** 2
    return outline_area(rect_outline(spec.length, spec.width, spec.corner_radius))


def base_outline(spec: PartSpec) -> List[Seg]:
    bp = getattr(spec, "base_profile", None)
    if bp:
        return segs_from_profile(bp)
    if spec.base_kind == "round":
        return circle(0.0, 0.0, spec.diameter / 2.0)
    left = str(getattr(spec, "end_left", "square") or "square").lower()
    right = str(getattr(spec, "end_right", "square") or "square").lower()
    if left != "arc" and right != "arc":
        return rect_outline(spec.length, spec.width, spec.corner_radius)
    # 圆弧头：该端换成半径 W/2 的半圆（U 形叉 / 摇臂 / 连杆）
    L, W = spec.length, spec.width
    r = W / 2.0
    xi = max(0.0, L / 2.0 - r)
    xl = -xi if left == "arc" else -L / 2.0
    xr = xi if right == "arc" else L / 2.0
    segs: List[Seg] = [("line", xl, -r, xr, -r)]
    if right == "arc":
        segs.append(("arc", xi, 0.0, r, -90.0, 90.0))
    else:
        segs.append(("line", xr, -r, xr, r))
    segs.append(("line", xr, r, xl, r))
    if left == "arc":
        segs.append(("arc", -xi, 0.0, r, 90.0, 270.0))
    else:
        segs.append(("line", xl, r, xl, -r))
    return segs


def offset_outline(spec: PartSpec, d: float) -> List[Seg]:
    """外轮廓法向偏置 d（mm，d>0 外扩，d<0 内缩），保留原生圆弧。

    geometry 刀心轨迹、抽壳内腔都用它：不把圆弧拆成一串点。
    """
    d = float(d)
    if spec.base_kind == "round":
        return circle(0.0, 0.0, max(spec.diameter / 2.0 + d, 0.0))
    L = float(spec.length) + 2.0 * d
    W = float(spec.width) + 2.0 * d
    left = str(getattr(spec, "end_left", "square") or "square").lower()
    right = str(getattr(spec, "end_right", "square") or "square").lower()
    if left != "arc" and right != "arc":
        cr = min(float(spec.corner_radius) + d, max(min(L, W) / 2.0 - 1e-6, 0.0))
        return rect_outline(L, W, max(cr, 0.0))
    r = float(spec.width) / 2.0 + d
    if r <= 0 or L <= 0:
        return []
    xi = max(0.0, float(spec.length) / 2.0 - float(spec.width) / 2.0)
    xl = -xi if left == "arc" else -L / 2.0
    xr = xi if right == "arc" else L / 2.0
    segs: List[Seg] = [("line", xl, -r, xr, -r)]
    if right == "arc":
        segs.append(("arc", xi, 0.0, r, -90.0, 90.0))
    else:
        segs.append(("line", xr, -r, xr, r))
    segs.append(("line", xr, r, xl, r))
    if left == "arc":
        segs.append(("arc", -xi, 0.0, r, 90.0, 270.0))
    else:
        segs.append(("line", xl, r, xl, -r))
    return segs


def inner_outline(spec: PartSpec, t: float) -> List[Seg]:
    """抽壳内腔轮廓（mm）：外轮廓均匀内缩 t，与 shell_inner_area() 同口径。

    圆：R-t；矩形：L-2t / W-2t，四角半径 cr-t；
    圆弧头：半圆半径 W/2-t，圆心 x 不动，方端边线内缩 t。
    """
    t = max(float(t), 0.0)
    if spec.base_kind == "round":
        return circle(0.0, 0.0, max(spec.diameter / 2.0 - t, 0.0))
    L, W = float(spec.length), float(spec.width)
    left = str(getattr(spec, "end_left", "square") or "square").lower()
    right = str(getattr(spec, "end_right", "square") or "square").lower()
    if left == "arc" or right == "arc":
        r = max(W / 2.0 - t, 0.0)
        xi = max(0.0, L / 2.0 - W / 2.0)
        xl = -xi if left == "arc" else -(L / 2.0 - t)
        xr = xi if right == "arc" else (L / 2.0 - t)
        segs: List[Seg] = [("line", xl, -r, xr, -r)]
        if right == "arc":
            segs.append(("arc", xi, 0.0, r, -90.0, 90.0))
        else:
            segs.append(("line", xr, -r, xr, r))
        segs.append(("line", xr, r, xl, r))
        if left == "arc":
            segs.append(("arc", -xi, 0.0, r, 90.0, 270.0))
        else:
            segs.append(("line", xl, r, xl, -r))
        return segs
    cr = min(float(spec.corner_radius), min(L, W) / 2.0) - t
    cr = max(min(cr, min(L - 2 * t, W - 2 * t) / 2.0), 0.0)
    return rect_outline(max(L - 2 * t, 0.0), max(W - 2 * t, 0.0), cr)


def base_area(spec: PartSpec) -> float:
    bp = getattr(spec, "base_profile", None)
    if bp:
        return outline_area_exact(segs_from_profile(bp))
    if spec.base_kind == "round":
        return math.pi * (spec.diameter / 2.0) ** 2
    left = str(getattr(spec, "end_left", "square") or "square").lower()
    right = str(getattr(spec, "end_right", "square") or "square").lower()
    a = _rect_area(spec.length, spec.width, spec.corner_radius)
    if left == "arc" or right == "arc":
        r = spec.width / 2.0
        per_end = 2.0 * r * r - math.pi * r * r / 2.0
        a -= per_end * ((1 if left == "arc" else 0) + (1 if right == "arc" else 0))
    return a


def feature_outline(f: Feature, grow: float = 0.0) -> List[Seg]:
    """grow：薄壁冲突时把孔外扩的量（见 app/conflict.py），保证理论体积与 CAD 一致。"""
    if f.type == "profile_cut":
        return segs_from_profile(f.profile)
    if f.type == "hole":
        return circle(f.x, f.y, f.diameter / 2.0 + grow)
    if f.type == "slot":
        L, W = (f.length or f.width), f.width + 2 * grow
        if L <= W + 1e-6:            # 长宽相等 → 实际是个圆，腰形轮廓会自交
            return circle(f.x, f.y, W / 2.0)
        return slot_outline(L, W, f.x, f.y, f.angle)
    return rect_outline(f.length + 2 * grow, f.width + 2 * grow, f.corner_radius, f.x, f.y)


def round_slot_band_area(spec, f) -> float:
    """圆盘上**横贯全径**的直槽截面积（槽两端被外圆裁掉，不是腰形两端半圆）。

    十字节 Ø90×36：18 宽槽铣穿整个盘面，CAD 里用一个两端伸出盘外的方块切。
    截面积是圆被两条平行弦切出的弓形带，不是弦长×槽宽：

        A = 2·[ a·√(R²−a²) + R²·asin(a/R) ],  a = W/2

    对 R=45、W=18 解得 1609.13；旧式 2·W·√(R²−(W/2)²) 只给 1587.27，
    少算 21.9 mm²，体积高估约 0.36%。
    返回 0 表示"长度不足以横贯全径"，走普通腰形公式。
    """
    if str(getattr(spec, "base_kind", "")) != "round":
        return 0.0
    R = float(spec.diameter or 0.0) / 2.0
    w = float(f.width or 0.0)
    if R <= 0 or w <= 0 or w >= 2.0 * R:
        return 0.0
    half_chord = math.sqrt(max(R * R - (w / 2.0) ** 2, 0.0))
    if float(getattr(f, "length", 0.0) or 0.0) < 2.0 * half_chord - 1e-6:
        return 0.0
    a = w / 2.0
    return 2.0 * (a * half_chord + R * R * math.asin(min(a / R, 1.0)))


def sphere_cap_volume(R: float, h: float) -> float:
    """球冠体积：h 为切深（≤R 时是球冠，>R 时按半球算）。"""
    h = min(max(h, 0.0), R)
    return math.pi * h * h * (3.0 * R - h) / 3.0


def feature_area(f: Feature, grow: float = 0.0) -> float:
    if f.type == "profile_cut":
        return outline_area_exact(segs_from_profile(f.profile))
    if f.type == "sphere":
        # 球坑按"最大截面圆"参与面积统计（体积另有解析式，见 part_volume_exact）
        r = f.diameter / 2.0
        h = min(f.depth or r, r)
        return math.pi * (2.0 * r * h - h * h)
    if f.type == "hole":
        return math.pi * (f.diameter / 2.0 + grow) ** 2
    if f.type == "slot":
        w = (f.width or f.diameter) + 2 * grow
        return (max(f.length, w) - w) * w + math.pi * (w / 2.0) ** 2
    return outline_area(rect_outline(f.length + 2 * grow, f.width + 2 * grow, f.corner_radius))


def feature_depth(spec: PartSpec, f: Feature) -> float:
    """通/盲统一用 Feature.is_through()（深度优先），避免 CAD 切通、geometry 钻盲两套口径。"""
    try:
        through = f.is_through()
    except Exception:
        through = bool(f.through) or f.depth <= 0
    if through:
        return spec.thickness
    return min(max(float(f.depth or 0.0), 0.0), spec.thickness)


def _poly_pts(f: Feature, grow: float = 0.0):
    # 圆弧用 2° 采样：腰形槽端部若用 10° 弦近似，面积会少 2.6%
    return flatten(feature_outline(f, grow), arc_step_deg=1.5)[0]


def _mask(poly, X, Y):
    import numpy as np
    inside = np.zeros(X.shape, dtype=bool)
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        cond = ((yi > Y) != (yj > Y)) & (X < (xj - xi) * (Y - yi) / ((yj - yi) or 1e-12) + xi)
        inside ^= cond
        j = i
    return inside


def _pt_in_poly(pt, poly, tol=1e-6) -> bool:
    """点是否落在多边形内或边界上（射线法 + 边界距离容差）。"""
    x, y = pt
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        # 到线段的距离
        dx, dy = xj - xi, yj - yi
        _l2 = dx * dx + dy * dy
        if _l2 > 0.0:
            t = max(0.0, min(1.0, ((x - xi) * dx + (y - yi) * dy) / _l2))
        else:
            t = 0.0
        px, py = xi + t * dx, yi + t * dy
        if (x - px) ** 2 + (y - py) ** 2 <= tol * tol:
            return True
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def _poly_all_inside(outer, inner, tol=1e-6) -> bool:
    return all(_pt_in_poly(pt, outer, tol) for pt in inner)


def perimeter(spec: PartSpec) -> float:
    bp = getattr(spec, "base_profile", None)
    if bp:
        return segs_perimeter(segs_from_profile(bp))
    if spec.base_kind == "round":
        return math.pi * spec.diameter
    left = str(getattr(spec, "end_left", "square") or "square").lower()
    right = str(getattr(spec, "end_right", "square") or "square").lower()
    if left == "arc" or right == "arc":
        # 圆弧端把 W 那条直边换成 πW/2 的弧，两侧直边各短 W/2
        W = spec.width
        ends = (1 if left == "arc" else 0) + (1 if right == "arc" else 0)
        return 2.0 * spec.length - ends * W + ends * math.pi * W / 2.0 + \
            (0.0 if ends == 2 else W)
    r = min(spec.corner_radius, min(spec.length, spec.width) / 2.0)
    if r <= 1e-9:
        return 2 * (spec.length + spec.width)
    return 2 * (spec.length + spec.width - 4 * r) + 2 * math.pi * r


def shell_inner_area(spec: PartSpec, t: float) -> float:
    """抽壳内腔截面积：外轮廓均匀内缩 t 后的面积。

    说明：
      - 圆：π(R−t)²
      - 矩形：L−2t / W−2t，四角圆角半径 cr−t（cr 先按外轮廓钳制到 min(L,W)/2）
      - 圆弧头：半圆半径 W/2−t，圆心 x 不变；方端边线内缩 t
    """
    t = max(float(t), 0.0)
    if spec.base_kind == "round":
        return math.pi * max(spec.diameter / 2.0 - t, 0.0) ** 2
    left = str(getattr(spec, "end_left", "square") or "square").lower()
    right = str(getattr(spec, "end_right", "square") or "square").lower()
    n_arc = (1 if left == "arc" else 0) + (1 if right == "arc" else 0)
    if n_arc:
        W = float(spec.width)
        Lo = float(spec.length)
        r = max(W / 2.0 - t, 0.0)
        xi = max(0.0, Lo / 2.0 - W / 2.0)
        xl = -xi if left == "arc" else -(Lo / 2.0 - t)
        xr = xi if right == "arc" else (Lo / 2.0 - t)
        if xr <= xl or r <= 0:
            return 0.0
        return 2.0 * r * (xr - xl) + n_arc * math.pi * r * r / 2.0
    L = max(float(spec.length) - 2.0 * t, 0.0)
    W = max(float(spec.width) - 2.0 * t, 0.0)
    cr = min(float(spec.corner_radius), min(float(spec.length), float(spec.width)) / 2.0) - t
    cr = max(min(cr, min(L, W) / 2.0), 0.0)
    return max(L * W - (4.0 - math.pi) * cr * cr, 0.0)


def chamfer_v(spec: PartSpec, cc: float | None = None) -> float:
    """倒角去除量（mm³）。直边段 (L+W-4c)c² + 四角 8c³/3。"""
    c = float(spec.chamfer or 0.0) if cc is None else float(cc)
    if c <= 0:
        return 0.0
    if spec.base_kind == "rect":
        L, W = spec.length, spec.width
        return max(0.0, (L + W - 4.0 * c)) * c * c + 8.0 * c ** 3 / 3.0
    return perimeter(spec) * c * c / 2.0


def fillet_limit(spec: PartSpec) -> float:
    """顶面外圆角的物理上限。

    圆角刀按刀尖编程，刀尖最低到刃口下 2R；R > T/2 就会扎穿底面。
    内偏轮廓最多内缩 R，R >= min(L,W)/2 时轮廓退化为空。
    取两者较小值再留 1e-3 mm 余量，兼顾抽壳/薄板/圆弧端。
    """
    if spec.base_kind == "round":
        half = float(spec.diameter) / 2.0
    else:
        half = min(float(spec.length), float(spec.width)) / 2.0
    return max(min(float(spec.thickness) / 2.0, half - 1e-3), 0.0)


def fillet_effective(spec: PartSpec) -> float:
    """实际可加工的圆角半径（图纸值超上限时钳制）。"""
    f = float(spec.fillet or 0.0)
    if f <= 0:
        return 0.0
    return min(f, fillet_limit(spec))


def fillet_v(spec: PartSpec, ff: float | None = None) -> float:
    """顶面外轮廓圆角去除量（扫掠体并集，四角扣重叠）。"""
    lim = fillet_limit(spec)
    f = fillet_effective(spec) if ff is None else min(float(ff), lim)
    if f <= 0:
        return 0.0
    v = perimeter(spec) * f * f * (1.0 - math.pi / 4.0)
    if spec.base_kind == "rect":
        v -= 4.0 * f ** 3 * (5.0 / 3.0 - math.pi / 2.0)
    return max(v, 0.0)


def edge_feature_volume(spec: PartSpec) -> float:
    """沿顶面外一圈棱去除的体积（倒角 c / 圆角 f）。

    倒角：精确式；圆角：标称式（各内核角部过渡略有差别）。
    两个分量已抽成模块级 chamfer_v / fillet_v，
    供 geometry 回灌按“有无对应工序”分别判定。
    """
    c = float(spec.chamfer or 0.0)
    f = fillet_effective(spec)
    if f > 0 and c > 0:
        # 同一圈顶棱上同时给倒角与圆角：C 小于 R 时倒角完全落在圆角去除区内
        if c <= f:
            return fillet_v(spec, f)
        return fillet_v(spec, f) + chamfer_v(spec, c - f)
    return chamfer_v(spec, c) + fillet_v(spec, f)


def sweep_segments(sw) -> list:
    """把扫掠路径统一成原生段：line / arc（dict 段）。

    兼容两种写法：
      - 标准：{"type":"line","p1":[x,y,z],"p2":[...]} / {"type":"arc",...,"center":[...]}
      - 简写：[[x,y,z], [x,y,z], ...]（折线，逐点连线；只用于直线路径）
    不做点列拟合：简写里的点只作为直线端点，原生成弧必须显式给 center。
    """
    path = list((sw or {}).get("path") or [])
    if not path:
        return []
    if isinstance(path[0], dict):
        return [dict(x) for x in path]
    pts = []
    for q in path:
        try:
            pts.append([float(v) for v in list(q)[:3]])
        except Exception:
            return []
    segs = []
    for a, b in zip(pts, pts[1:]):
        segs.append({"type": "line", "p1": a, "p2": b})
    return segs


def _seg_len(seg):
    """一段路径的长度（mm）：直线或真圆弧。"""
    import math as _m
    p1 = [float(v) for v in (seg.get("p1") or [0, 0, 0])]
    p2 = [float(v) for v in (seg.get("p2") or [0, 0, 0])]
    if str(seg.get("type") or "line").lower() == "arc":
        c = [float(v) for v in (seg.get("center") or [0, 0, 0])]
        v1 = [p1[k] - c[k] for k in range(3)]
        v2 = [p2[k] - c[k] for k in range(3)]
        n1 = _m.sqrt(sum(x * x for x in v1)) or 1.0
        n2 = _m.sqrt(sum(x * x for x in v2)) or 1.0
        cs = sum(v1[k] * v2[k] for k in range(3)) / (n1 * n2)
        ang = _m.acos(max(-1.0, min(1.0, cs)))
        return n1 * ang
    return _m.sqrt(sum((p2[k] - p1[k]) ** 2 for k in range(3)))


def sweep_length(path) -> float:
    class _Wrap(dict):
        pass
    _sw = {"path": path}
    return float(sum(_seg_len(s) for s in sweep_segments(_sw)))


def sweep_volume(spec: PartSpec) -> float:
    """扫掠加料体积 = Σ 截面面积 × 路径长度（等截面、无自交）。"""
    v = 0.0
    for sw in (getattr(spec, "sweeps", None) or []):
        w = float(sw.get("profile_w") or 0.0)
        h = float(sw.get("profile_h") or 0.0)
        if w <= 0 or h <= 0:
            continue
        v += w * h * sweep_length(sw.get("path") or [])
    return v


def _poly_area2(pts) -> float:
    a = 0.0
    n = len(pts)
    for i in range(n):
        x1, z1 = pts[i]
        x2, z2 = pts[(i + 1) % n]
        a += x1 * z2 - x2 * z1
    return 0.5 * abs(a)


def _clip_above_z(poly, zmin: float):
    """Sutherland-Hodgman：把 XZ 多边形裁到 z >= zmin 一侧。"""
    out = []
    n = len(poly)
    for i in range(n):
        x1, z1 = poly[i]
        x2, z2 = poly[(i + 1) % n]
        in1 = z1 >= zmin - 1e-12
        in2 = z2 >= zmin - 1e-12
        if in1:
            out.append((x1, z1))
        if in1 != in2:
            t = (zmin - z1) / (z2 - z1)
            out.append((x1 + (x2 - x1) * t, zmin))
    return out


def _line_strip_area_above(seg, h: float, zmin: float) -> float:
    """直线段条带（宽 h、长 L 的平行四边形）在 z>=zmin 一侧的面积，mm²。"""
    p1 = [float(v) for v in (seg.get("p1") or [0, 0, 0])[:3]]
    p2 = [float(v) for v in (seg.get("p2") or [0, 0, 0])[:3]]
    dx, dz = p2[0] - p1[0], p2[2] - p1[2]
    ln = math.hypot(dx, dz)
    if ln <= 1e-12:
        return 0.0
    nx, nz = -dz / ln * h / 2.0, dx / ln * h / 2.0
    quad = [(p1[0] + nx, p1[2] + nz), (p2[0] + nx, p2[2] + nz),
            (p2[0] - nx, p2[2] - nz), (p1[0] - nx, p1[2] - nz)]
    cp = _clip_above_z(quad, zmin)
    return _poly_area2(cp) if len(cp) >= 3 else 0.0


def _arc_strip_area_above(seg, h: float, zmin: float) -> float:
    """圆弧段条带（圆环扇区）在 z>=zmin 一侧的面积，mm²。

    按角度做一维中点积分：被积函数是 r²(θ) 的差，绝对光滑，除了 z=zmin
    的切点外没有奇点；每度 8 段足以把误差压到 1e-5 量级，且不做点拟合。
    """
    p1 = [float(v) for v in (seg.get("p1") or [0, 0, 0])[:3]]
    p2 = [float(v) for v in (seg.get("p2") or [0, 0, 0])[:3]]
    c = seg.get("center") or []
    if len(c) < 3:
        return 0.0
    cx, cz = float(c[0]), float(c[2])
    R = math.hypot(p1[0] - cx, p1[2] - cz)
    if R <= 1e-9:
        return 0.0
    a0 = math.degrees(math.atan2(p1[2] - cz, p1[0] - cx))
    a1 = math.degrees(math.atan2(p2[2] - cz, p2[0] - cx))
    d = int(seg.get("direction") or 1)
    sweep = a1 - a0
    if d > 0:
        while sweep <= 0:
            sweep += 360.0
    else:
        while sweep >= 0:
            sweep -= 360.0
    r_in = max(R - h / 2.0, 0.0)
    r_out = R + h / 2.0
    if r_out <= r_in:
        return 0.0
    n = max(32, int(abs(sweep) * 8.0))
    dth = math.radians(sweep) / n
    need = zmin - cz
    area = 0.0
    for k in range(n):
        th = math.radians(a0) + dth * (k + 0.5)
        sn = math.sin(th)
        lo, hi = r_in, r_out
        if abs(sn) < 1e-12:
            if cz < zmin:
                continue
        elif sn > 0.0:
            lo = max(lo, need / sn)
        else:
            hi = min(hi, need / sn)
        if hi <= lo:
            continue
        area += 0.5 * (hi * hi - lo * lo) * abs(dth)
    return area


def sweep_added_volume(spec) -> float:
    """扫掠体真正「加到零件上」的体积：只算基体顶面 z=T 以上的那部分。

    直接拿 w*h*长度 当加料体积，会把肋根埋进基体的那一段算两遍
    （基体里本来就有），而 CAD 布尔并集只保留一份；实测浅弧肋因此虚高。
    """
    T = float(getattr(spec, "thickness", 0.0) or 0.0)
    v = 0.0
    for sw in (getattr(spec, "sweeps", None) or []):
        w = float(sw.get("profile_w") or 0.0)
        h = float(sw.get("profile_h") or 0.0)
        if w <= 0 or h <= 0:
            continue
        for seg in sweep_segments(sw):
            if str(seg.get("type") or "line").lower() == "arc" and seg.get("center"):
                v += w * _arc_strip_area_above(seg, h, T)
            else:
                v += w * _line_strip_area_above(seg, h, T)
    return v


def _sweep_top_area(sw, T: float, nx: int = 2001) -> float:
    """解析顶面在条带 XY 包络内、基体顶面以上的截面面积（mm²）。

    用来识别倒扣：高度场只能多算（把封闭空腔当实体），不会少算。
    """
    import types as _types
    import numpy as np
    bb = sweep_bbox(_types.SimpleNamespace(sweeps=[sw]))
    if not bb:
        return 0.0
    x0, x1 = float(bb[0]), float(bb[1])
    if x1 - x0 <= 1e-9:
        return 0.0
    X = np.linspace(x0, x1, max(64, int(nx)))
    zt = sweep_top_z(sw, X)
    zz = np.where(zt > -1e8, np.maximum(zt - T, 0.0), 0.0)
    return float(np.sum((zz[1:] + zz[:-1]) * 0.5 * np.diff(X)))


def sweep_bbox(spec: PartSpec):
    """扫掠体的世界包围盒 (xmin,xmax,ymin,ymax,zmin,zmax)，mm。

    仅覆盖已在 CAD 侧支持的平面路径（所有点同一 Y，路径在 XZ 平面）：
    截面宽 w 沿 Y、截面高 h 沿 XZ 内法向，因此 Y 扩 w/2、X/Z 扩 h/2。
    非平面路径退化为各轴扩 max(w,h)/2（保守）。
    """
    boxes = []
    for sw in (getattr(spec, "sweeps", None) or []):
        w = float(sw.get("profile_w") or 0.0)
        h = float(sw.get("profile_h") or 0.0)
        path = sweep_segments(sw)
        if w <= 0 or h <= 0 or not path:
            continue
        xs, ys, zs = [], [], []
        plane_y = []
        arc_boxes = []
        for s in path:
            for k in ("p1", "p2"):
                q = s.get(k)
                if q:
                    xs.append(float(q[0])); ys.append(float(q[1])); zs.append(float(q[2]))
            if str(s.get("type") or "").lower() == "arc" and s.get("center"):
                c = s["center"]
                r = ((float(s["p1"][0]) - float(c[0])) ** 2 +
                     (float(s["p1"][1]) - float(c[1])) ** 2 +
                     (float(s["p1"][2]) - float(c[2])) ** 2) ** 0.5
                # 只取圆弧真实扫过的角度范围，不能用整圆包围盒：
                # 60° 的浅弧若按整圆算，包围盒会变成直径 2R，扫掠肋的「岛」
                # 比整个零件还宽，顶面清平会留下整条没铣到的毛坯。
                _cx, _cz = float(c[0]), float(c[2])
                _a0 = math.atan2(float(s["p1"][2]) - _cz,
                                 float(s["p1"][0]) - _cx)
                _a1 = math.atan2(float(s["p2"][2]) - _cz,
                                 float(s["p2"][0]) - _cx)
                _sd = int(s.get("direction") or 1)
                _sp = _a1 - _a0
                if _sd > 0:
                    while _sp <= 0.0:
                        _sp += 2.0 * math.pi
                else:
                    while _sp >= 0.0:
                        _sp -= 2.0 * math.pi
                # 扫掠体是半径 [R-h/2, R+h/2] 的环带。X/Z 极值只可能出现在
                # 两端或区间内的 0/90/180/270°，按真实角度枚举即可，绝不采样。
                _rin = max(r - h / 2.0, 0.0)
                _rout = r + h / 2.0
                _lo, _hi = sorted((_a0, _a0 + _sp))
                _k0 = int(math.floor(_lo / (math.pi / 2.0)))
                _k1 = int(math.ceil(_hi / (math.pi / 2.0)))
                _angs = [_a0, _a0 + _sp]
                for _k in range(_k0, _k1 + 1):
                    _a = _k * (math.pi / 2.0)
                    if _lo - 1e-12 <= _a <= _hi + 1e-12:
                        _angs.append(_a)
                _ext = []
                for _a in _angs:
                    for _rr in (_rin, _rout):
                        _ext.append((_cx + _rr * math.cos(_a),
                                     _cz + _rr * math.sin(_a)))
                if _rin <= 1e-12:
                    _ext.append((_cx, _cz))
                arc_boxes.append((min(q[0] for q in _ext), max(q[0] for q in _ext),
                                  min(q[1] for q in _ext), max(q[1] for q in _ext)))
                plane_y.append(float(c[1]))
        plane_y.extend(ys)
        if not xs:
            continue
        planar = (max(plane_y) - min(plane_y)) <= 1e-6
        if not planar:
            e = max(w, h) / 2.0
            boxes.append((min(xs) - e, max(xs) + e,
                          min(ys) - e, max(ys) + e,
                          min(zs) - e, max(zs) + e))
            continue
        # 截面宽 w 始终沿 Y。XZ 内截面高 h 的方向随路径切向旋转：
        # 水平段沿 Z、竖直段沿 X。按每段法向扩 h/2，不能用全轴扩 h/2
        # （否则竖直末端会凭空多出 h/2 的 Z 跨度）。
        bb = [min(xs), max(xs), min(ys) - w / 2.0, max(ys) + w / 2.0,
              min(zs), max(zs)]
        for _ab in arc_boxes:
            bb[0] = min(bb[0], _ab[0]); bb[1] = max(bb[1], _ab[1])
            bb[4] = min(bb[4], _ab[2]); bb[5] = max(bb[5], _ab[3])
        for seg in path:
            typ = str(seg.get("type") or "line").lower()
            if typ == "arc":
                continue
            p1 = seg.get("p1") or []
            p2 = seg.get("p2") or []
            if len(p1) < 3 or len(p2) < 3:
                continue
            x1, z1 = float(p1[0]), float(p1[2])
            x2, z2 = float(p2[0]), float(p2[2])
            dx, dz = x2 - x1, z2 - z1
            ln = math.hypot(dx, dz)
            if ln <= 1e-12:
                continue
            nx, nz = -dz / ln, dx / ln
            ex, ez = abs(nx) * h / 2.0, abs(nz) * h / 2.0
            bb[0] = min(bb[0], x1 - ex, x2 - ex)
            bb[1] = max(bb[1], x1 + ex, x2 + ex)
            bb[4] = min(bb[4], z1 - ez, z2 - ez)
            bb[5] = max(bb[5], z1 + ez, z2 + ez)
        boxes.append(tuple(bb))
    if not boxes:
        return None
    return (min(b[0] for b in boxes), max(b[1] for b in boxes),
            min(b[2] for b in boxes), max(b[3] for b in boxes),
            min(b[4] for b in boxes), max(b[5] for b in boxes))


def _sweep_band_in_base(spec, bb, n: int = 5):
    """扫掠体 XY 条带是否落在基体轮廓内（多点判定，用于肋根有没有料托住）。"""
    import numpy as np
    x0, x1, y0, y1 = float(bb[0]), float(bb[1]), float(bb[2]), float(bb[3])
    X = np.linspace(x0 + 1e-3, x1 - 1e-3, n)
    Y = np.linspace(y0 + 1e-3, y1 - 1e-3, n)
    GX, GY = np.meshgrid(X, Y)
    try:
        m = _polys_mask(GX, GY, flatten_polys(base_outline(spec)))
    except Exception:
        return False
    return bool(m is not None and m.all())


def sweep_machinability(spec, sw) -> dict:
    """单个扫掠体的三轴单面可加工性（解析判定，绝不为了消告警假报已加工）。

    返回 dict：
      ok        True = 一次装夹（Z 从上方）就能做出来
      mode      "fin" 肋 / "floating" 悬空 / "unsupported" 形态不支持
      plane     路径所在平面："XZ" / "other"
      w/h       截面宽(Y) / 截面高(路径法向)，mm
      zmin/zmax 扫掠体 Z 包络，mm
      clearance 肋根到可支撑面的净空（>0 = 悬空）
      reason    给报告看的人话
    """
    import types as _types
    sw = sw or {}
    w = float(sw.get("profile_w") or 0.0)
    h = float(sw.get("profile_h") or 0.0)
    out = {"ok": False, "w": w, "h": h, "plane": "?", "zmin": None, "zmax": None,
           "clearance": None, "mode": "", "reason": "", "bbox": None,
           "heightfield": False, "added_volume": None, "top_volume": None}
    segs = sweep_segments(sw)
    if not segs or w <= 0 or h <= 0:
        out["mode"] = "unsupported"
        out["reason"] = "截面或路径为空"
        return out
    ys = []
    for s in segs:
        for k in ("p1", "p2", "center"):
            q = s.get(k)
            if q:
                ys.append(float(q[1]))
    if (max(ys) - min(ys)) > 1e-6:
        out["plane"] = "other"
        out["mode"] = "unsupported"
        out["reason"] = ("路径不在 XZ 平面（Y 跨度 %.2fmm）：CAD 适配器与三轴刀路都只支持 XZ 平面路径"
                         % (max(ys) - min(ys)))
        return out
    out["plane"] = "XZ"
    bb = sweep_bbox(_types.SimpleNamespace(sweeps=[sw]))
    if not bb:
        out["mode"] = "unsupported"
        out["reason"] = "扫掠体包络算不出来"
        return out
    out["bbox"] = tuple(float(v) for v in bb)
    out["zmin"], out["zmax"] = float(bb[4]), float(bb[5])
    T = float(spec.thickness)
    # 肋根下面有没有料托住：条带整体在基体轮廓内 → 顶面肋（基体顶面 z=0 托住）
    # 条带在轮廓外 → 只能靠零件底面 z=-T 托住，也就是肋必须贯穿到底。
    # 坐标口径：CAD 世界里基体占 z=0..T（零件底面 z=0、基体顶面 z=T）。
    # 条带在基体轮廓内 → 肋根只能落在基体顶面 z=T；条带在轮廓外 → 只能靠
    # 零件底面 z=0 托住（也就是肋必须贯穿到底）。
    if _sweep_band_in_base(spec, bb):
        need, why = T, "顶面肋（下方由基体托住）"
    else:
        need, why = 0.0, "贯穿肋（条带在基体外，靠零件底面托住）"
    clr = out["zmin"] - need
    out["clearance"] = clr
    if clr > 0.05:
        out["mode"] = "floating"
        if need <= 1e-9:
            _why = "条带落在基体轮廓外，肋根悬在 z=%.2fmm，下方直通零件底面 z=0 之间全是空气" % (
                out["zmin"],)
        else:
            _why = "肋根 z=%.2fmm 高于基体顶面 z=%.2fmm，肋下是空气" % (out["zmin"], T)
        out["reason"] = ("悬空肋：%s；三轴单面从上方够不到肋底（要从下方掏空），"
                         "须翻面装夹或第4轴" % _why)
        return out
    # Z-map 只能表达「每个 X 一个顶面高度」。路径在 XZ 内折返/回转时，
    # 上、下两段之间可能有空气，高度场会把空气填成实体 —— 三轴从上方做不出
    # 这种倒扣/侧向开口。用解析净加料体积 vs 顶面包络体积判定，绝不放行。
    try:
        import types as _types
        _one = _types.SimpleNamespace(sweeps=[sw])
        _v_add = float(sweep_added_volume(_one))
        _v_top = float(_sweep_top_area(sw, T) * w)
        out["added_volume"] = _v_add
        out["top_volume"] = _v_top
        if _v_add > 1e-6 and _v_top > _v_add * 1.02 and (_v_top - _v_add) > max(0.002 * _v_add, 1.0):
            out["mode"] = "undercut"
            out["ok"] = False
            out["reason"] = (
                "扫掠路径在 XZ 平面内折返/回转：顶面包络体积 %.0f mm³ 大于真实净加料 %.0f mm³"
                "（+%.1f%%），上下两段之间存在高度场填不满的空气；从上方三轴加工会过切或留下倒扣，"
                "须侧铣、翻面装夹或第4轴"
                % (_v_top, _v_add, 100.0 * (_v_top / _v_add - 1.0)))
            return out
        out["heightfield"] = True
    except Exception:
        out["heightfield"] = False
        out["mode"] = "unsupported"
        out["ok"] = False
        out["reason"] = "扫掠顶面可表达性判定失败，不能假定三轴可加工"
        return out
    out["mode"] = "fin"
    out["ok"] = True
    out["reason"] = why + "，三轴单面可加工"
    return out


def _arc_split_bands(a0: float, span: float):
    """把圆弧按 sinθ 的符号切成若干段：[(起始角, 带符号张角, sgn)]，角度为度。"""
    s0, s1 = a0, a0 + span
    lo, hi = (s0, s1) if span >= 0 else (s1, s0)
    cuts = [s0]
    k0 = int(math.floor(lo / 180.0))
    k1 = int(math.ceil(hi / 180.0))
    for k in range(k0, k1 + 1):
        c = 180.0 * float(k)
        if lo + 1e-9 < c < hi - 1e-9:
            cuts.append(c)
    cuts.append(s1)
    out = []
    for i in range(len(cuts) - 1):
        a, b = cuts[i], cuts[i + 1]
        if abs(b - a) < 1e-9:
            continue
        mid = math.radians(a + (b - a) / 2.0)
        out.append((a, b - a, 1.0 if math.sin(mid) >= 0.0 else -1.0))
    return out


def sweep_top_segments(sw) -> list:
    """扫掠体「从 +Z 能看到的顶面」的 XZ 解析段（不采样、不点拟合）。

    返回 [(kind, ...)]：
      ("line", x1, z1, x2, z2)
      ("cap",  xc, ztop, half_x)      竖直段的平顶
      ("arc",  cx, cz, R, a0, a1)     角度单位为度，XZ 内 atan2(z-cz, x-cx)
    截面半高 h/2 沿路径法向**向上**偏移。
    """
    h = float((sw or {}).get("profile_h") or 0.0)
    if h <= 0:
        return []
    segs = sweep_segments(sw)
    out = []
    for s in segs:
        p1 = [float(v) for v in (s.get("p1") or [0, 0, 0])[:3]]
        p2 = [float(v) for v in (s.get("p2") or [0, 0, 0])[:3]]
        typ = str(s.get("type") or "line").lower()
        if typ == "arc" and s.get("center"):
            c = [float(v) for v in s["center"][:3]]
            v1 = (p1[0] - c[0], p1[2] - c[2])
            R = (v1[0] ** 2 + v1[1] ** 2) ** 0.5
            if R <= 1e-9:
                continue
            a0 = math.degrees(math.atan2(v1[1], v1[0]))
            a1 = math.degrees(math.atan2(p2[2] - c[2], p2[0] - c[0]))
            d = int(s.get("direction") or 1)
            sweep = a1 - a0
            if d > 0:
                while sweep <= 0:
                    sweep += 360.0
            else:
                while sweep >= 0:
                    sweep -= 360.0
            # 顶面 = 路径法向 +h/2 的包络：sinθ≥0 一侧半径 R+h/2（凸），
            # sinθ<0 一侧半径 R-h/2（凹）。两端在 sinθ=0 处相切，整条顶面连续。
            # 不能拿「弧中点在上还是在下」一刀切，那会把整段弧都算成外偏 3mm（实测体积 +45%）。
            _bands = _arc_split_bands(a0, sweep)
            if not _bands:
                continue

            def _at_zero(_ang):
                return abs(math.sin(math.radians(_ang))) <= 1e-9

            def _x_at(_ang, _rr):
                return c[0] + _rr * math.cos(math.radians(_ang))

            for _bi, (_s0, _ss, _sg) in enumerate(_bands):
                _Rp = R + _sg * h / 2.0
                if _Rp <= 1e-6:
                    continue
                # 圆弧进入/离开 sinθ=0 时，截面高度方向在 X 上占一段宽度。
                # 只在半径 Rp 的圆弧上取样会留下 cx±(R−h/2)..cx±(R+h/2)
                # 的缺口（实测 U 形凹弧 x=-30..-27 的 want 被算成 0）。
                # 端部/分段边界是一条真正水平的顶面短直线，必须解析补上。
                if _bi == 0 and _at_zero(_s0):
                    _ro = R - _sg * h / 2.0
                    out.append(("line", _x_at(_s0, _ro), c[2],
                                _x_at(_s0, _Rp), c[2]))
                elif _bi > 0 and _at_zero(_s0):
                    _pg = _bands[_bi - 1][2]
                    _ro = R + _pg * h / 2.0
                    out.append(("line", _x_at(_s0, _ro), c[2],
                                _x_at(_s0, _Rp), c[2]))
                out.append(("arc", c[0], c[2], _Rp, _s0, _ss, _sg))
            _end = _bands[-1][0] + _bands[-1][1]
            if _at_zero(_end):
                _sg = _bands[-1][2]
                _rp = R + _sg * h / 2.0
                _ro = R - _sg * h / 2.0
                out.append(("line", _x_at(_end, _rp), c[2],
                            _x_at(_end, _ro), c[2]))
            continue
        dx, dz = p2[0] - p1[0], p2[2] - p1[2]
        ln = math.hypot(dx, dz)
        if ln <= 1e-12:
            continue
        nx, nz = -dz / ln, dx / ln
        if nz < 0:
            nx, nz = -nx, -nz
        if abs(nx) > 0.999 and abs(dz) < 1e-9:
            # 竖直段（X 不变）：顶面是该段端部的一块平顶
            out.append(("cap", p1[0] + nx * h / 2.0, max(p1[2], p2[2]) + nz * h / 2.0, h / 2.0))
            continue
        out.append(("line", p1[0] + nx * h / 2.0, p1[2] + nz * h / 2.0,
                    p2[0] + nx * h / 2.0, p2[2] + nz * h / 2.0))
    return _connect_top_vertical_gaps(_trim_top_lines(out))


def _top_seg_point(sg, end: bool):
    if sg[0] == "line":
        return (float(sg[3]), float(sg[4])) if end else (float(sg[1]), float(sg[2]))
    if sg[0] == "cap":
        _, xc, ztop, half = sg
        return (float(xc) + (float(half) if end else -float(half)), float(ztop))
    _, cx, cz, R, a0, sw, _sgn = sg
    a = float(a0) + (float(sw) if end else 0.0)
    return float(cx) + float(R) * math.cos(math.radians(a)), float(cz) + float(R) * math.sin(math.radians(a))


def _connect_top_vertical_gaps(segs):
    """相邻顶面段端点若同 X、不同 Z，补一条原生竖直段。

    这是 U 形凹弧两端的真实侧壁；没有它，偏置刀路只会在 X 上斜连，
    球头刀会把台阶两侧本应保留的材料一起切掉。
    """
    out = []
    for sg in segs:
        if out:
            px, pz = _top_seg_point(out[-1], True)
            sx, sz = _top_seg_point(sg, False)
            if abs(px - sx) <= 1e-7 and abs(pz - sz) > 1e-7:
                out.append(("line", px, pz, px, sz))
        out.append(sg)
    return out


def _trim_top_lines(segs):
    """把被更高直线覆盖的低位顶面短直线裁掉（只处理 line 段）。

    圆弧端部的水平短顶面会与相邻水平段在 X 上重叠；高度场取 max 时高段
    有效，但刀路若照原低段走会切掉本应保留的材料。这里解析裁剪，不做采样。
    """
    def _z_at(sg, x):
        _, x1, z1, x2, z2 = sg
        if abs(x2 - x1) <= 1e-12:
            return z1
        return z1 + (z2 - z1) * (x - x1) / (x2 - x1)

    out = []
    for i, sg in enumerate(segs):
        if sg[0] != "line" or abs(sg[3] - sg[1]) <= 1e-12:
            out.append(sg)
            continue
        x1, z1, x2, z2 = float(sg[1]), float(sg[2]), float(sg[3]), float(sg[4])
        lo, hi = sorted((x1, x2))
        parts = [(lo, hi)]
        for j, og in enumerate(segs):
            if j == i or og[0] != "line" or abs(og[3] - og[1]) <= 1e-12:
                continue
            olo, ohi = sorted((float(og[1]), float(og[3])))
            a, b = max(lo, olo), min(hi, ohi)
            if b - a <= 1e-8:
                continue
            d1 = _z_at(og, a) - _z_at(sg, a)
            d2 = _z_at(og, b) - _z_at(sg, b)
            cuts = []
            if d1 >= -1e-7 and d2 >= -1e-7:
                cuts = [(a, b)]
            elif d1 * d2 < -1e-12:
                xc = a + (b - a) * (-d1) / (d2 - d1)
                cuts = [(a, xc)] if d1 > 0.0 else [(xc, b)]
            if not cuts:
                continue
            nxt = []
            for l, r in parts:
                for ca, cb in cuts:
                    if cb <= l + 1e-9 or ca >= r - 1e-9:
                        continue
                    l, r = l, r
                # subtract all cuts one by one
            for ca, cb in cuts:
                nxt = []
                for l, r in parts:
                    if cb <= l + 1e-9 or ca >= r - 1e-9:
                        nxt.append((l, r))
                    else:
                        if l < ca - 1e-9:
                            nxt.append((l, ca))
                        if cb < r - 1e-9:
                            nxt.append((cb, r))
                parts = nxt
                if not parts:
                    break
        for l, r in parts:
            if r - l <= 1e-8:
                continue
            zl, zr = _z_at(sg, l), _z_at(sg, r)
            if x1 <= x2:
                out.append(("line", l, zl, r, zr))
            else:
                out.append(("line", r, zr, l, zl))
    return out


def sweep_top_z(sw, X):
    """扫掠体顶面高度（相对基体顶面 z=0，向上为正）在 X 网格上的解析值。"""
    import numpy as np
    X = np.asarray(X, dtype=np.float64)
    z = np.full(X.shape, -1e9, dtype=np.float64)
    for sg in sweep_top_segments(sw):
        if sg[0] == "line":
            _, x1, z1, x2, z2 = sg
            if abs(x2 - x1) <= 1e-9:
                continue
            t = (X - x1) / (x2 - x1)
            sel = (t >= -1e-9) & (t <= 1.0 + 1e-9)
            zz = z1 + (z2 - z1) * np.clip(t, 0.0, 1.0)
            z = np.where(sel, np.maximum(z, zz), z)
        elif sg[0] == "cap":
            _, xc, ztop, half = sg
            sel = np.abs(X - xc) <= half + 1e-9
            z = np.where(sel, np.maximum(z, ztop), z)
        else:
            _, cx, cz, Rp, a0, span, sgn = sg
            # 每段的偏置半径 Rp 已由 sweep_top_segments() 按 sinθ 的符号算好
            # （sinθ≥0 用 R+h/2，sinθ<0 用 R−h/2）。这里绝不能再按符号加减 h/2，
            # 否则会把偏置算两遍（曾因 h 未定义直接 NameError）。
            dx = (X - cx) / max(Rp, 1e-12)
            sel = np.abs(dx) <= 1.0 + 1e-9
            if not sel.any():
                continue
            # 圆弧必须按真实角度范围取，不能拿整圆算：半圆弧的「另一半」
            # 在同一个 X 上也有交点，会把扫掠体凭空加宽（实测 1.37% 基线差）。
            sin_abs = np.sqrt(np.maximum(1.0 - dx * dx, 0.0))
            theta = np.degrees(np.arctan2(sgn * sin_abs, dx))
            t = (theta - a0) if span >= 0 else (a0 - theta)
            t = np.mod(t, 360.0)
            ok = sel & (t <= abs(span) + 1e-9)
            zz = cz + sgn * Rp * sin_abs
            z = np.where(ok, np.maximum(z, zz), z)
    return z


def _xz_as_line(sg):
    """把 line/cap 统一成 XZ 直线参数 (x1,z1,x2,z2)，圆弧返回 None。"""
    if sg[0] == "line":
        return float(sg[1]), float(sg[2]), float(sg[3]), float(sg[4])
    if sg[0] == "cap":
        _, xc, ztop, half = sg
        return float(xc) - float(half), float(ztop), float(xc) + float(half), float(ztop)
    return None


def _xz_seg_point(sg, end: bool):
    ln = _xz_as_line(sg)
    if ln:
        return (ln[2], ln[3]) if end else (ln[0], ln[1])
    _, cx, cz, R, a0, sw, _sgn = sg
    a = math.radians(a0 + (sw if end else 0.0))
    return float(cx) + float(R) * math.cos(a), float(cz) + float(R) * math.sin(a)


def _xz_arc_angle_ok(sg, ang: float) -> bool:
    _, _cx, _cz, _R, a0, sw, _sgn = sg
    if abs(sw) <= 1e-12:
        return False
    if sw > 0:
        while ang < a0 - 1e-9:
            ang += 360.0
        return ang <= a0 + sw + 1e-9
    while ang > a0 + 1e-9:
        ang -= 360.0
    return ang >= a0 + sw - 1e-9


def _xz_seg_frac(sg, pt) -> float:
    x, z = pt
    ln = _xz_as_line(sg)
    if ln:
        x1, z1, x2, z2 = ln
        ux, uz = x2 - x1, z2 - z1
        L2 = ux * ux + uz * uz
        if L2 <= 1e-18:
            return 0.0
        return ((x - x1) * ux + (z - z1) * uz) / L2
    _, cx, cz, _R, a0, sw, _sgn = sg
    ang = math.degrees(math.atan2(z - cz, x - cx))
    if sw > 0:
        while ang < a0 - 1e-9:
            ang += 360.0
    else:
        while ang > a0 + 1e-9:
            ang -= 360.0
    return (ang - a0) / sw


def _xz_intersections(a, b):
    """解析求两条 XZ 原生段的交点（只用于相邻段的斜接修剪，不做点拟合）。"""
    la, lb = _xz_as_line(a), _xz_as_line(b)
    out = []
    if la and lb:
        ax, az, bx, bz = la
        cx, cz, dx, dz = lb
        ux, uz = bx - ax, bz - az
        vx, vz = dx - cx, dz - cz
        den = ux * vz - uz * vx
        if abs(den) <= 1e-12:
            return out
        qx, qz = cx - ax, cz - az
        t = (qx * vz - qz * vx) / den
        s = (qx * uz - qz * ux) / den
        if -1e-8 <= t <= 1.0 + 1e-8 and -1e-8 <= s <= 1.0 + 1e-8:
            out.append((ax + t * ux, az + t * uz))
        return out
    if la and not lb:
        ax, az, bx, bz = la
        _, cx, cz, R, a0, sw, _sgn = b
        ux, uz = bx - ax, bz - az
        fx, fz = ax - cx, az - cz
        qa = ux * ux + uz * uz
        qb = 2.0 * (fx * ux + fz * uz)
        qc = fx * fx + fz * fz - R * R
        disc = qb * qb - 4.0 * qa * qc
        if qa <= 1e-18 or disc < -1e-9:
            return out
        roots = [(-qb) / (2.0 * qa)] if disc <= 1e-9 else [
            (-qb - math.sqrt(disc)) / (2.0 * qa),
            (-qb + math.sqrt(disc)) / (2.0 * qa),
        ]
        for t in roots:
            if -1e-8 <= t <= 1.0 + 1e-8:
                x, z = ax + t * ux, az + t * uz
                ang = math.degrees(math.atan2(z - cz, x - cx))
                if _xz_arc_angle_ok(b, ang):
                    out.append((x, z))
        return out
    if not la and lb:
        return _xz_intersections(b, a)
    _, c1x, c1z, r1, _a01, _sw1, _s1 = a
    _, c2x, c2z, r2, _a02, _sw2, _s2 = b
    dx, dz = c2x - c1x, c2z - c1z
    dcen = math.hypot(dx, dz)
    if dcen <= 1e-12 or dcen > r1 + r2 + 1e-9 or dcen < abs(r1 - r2) - 1e-9:
        return out
    aa = (r1 * r1 - r2 * r2 + dcen * dcen) / (2.0 * dcen)
    h2 = r1 * r1 - aa * aa
    if h2 < -1e-9:
        return out
    hh = math.sqrt(max(h2, 0.0))
    mx, mz = c1x + aa * dx / dcen, c1z + aa * dz / dcen
    ox, oz = -dz / dcen * hh, dx / dcen * hh
    pts = [(mx + ox, mz + oz)]
    if hh > 1e-9:
        pts.append((mx - ox, mz - oz))
    for x, z in pts:
        a1 = math.degrees(math.atan2(z - c1z, x - c1x))
        a2 = math.degrees(math.atan2(z - c2z, x - c2x))
        if _xz_arc_angle_ok(a, a1) and _xz_arc_angle_ok(b, a2):
            out.append((x, z))
    return out


def _xz_trim_seg(sg, pt, at_end: bool):
    x, z = pt
    ln = _xz_as_line(sg)
    if ln:
        x1, z1, x2, z2 = ln
        return ("line", x1, z1, x, z) if at_end else ("line", x, z, x2, z2)
    _, cx, cz, R, a0, sw, sgn = sg
    ang = math.degrees(math.atan2(z - cz, x - cx))
    if at_end:
        target = a0 + sw
        while ang - target > 180.0:
            ang -= 360.0
        while ang - target < -180.0:
            ang += 360.0
        new_sw = ang - a0
        if (sw > 0 and new_sw < -1e-8) or (sw < 0 and new_sw > 1e-8):
            return sg
        return ("arc", cx, cz, R, a0, new_sw, sgn)
    target = a0
    while ang - target > 180.0:
        ang -= 360.0
    while ang - target < -180.0:
        ang += 360.0
    new_sw = (a0 + sw) - ang
    if (sw > 0 and new_sw < -1e-8) or (sw < 0 and new_sw > 1e-8):
        return sg
    return ("arc", cx, cz, R, ang, new_sw, sgn)


def _xz_join_segments(segs, joints=None, d: float = 0.0):
    """把相邻偏置段的交点作为斜接点，消除圆弧起终点处的台阶。

    交点不存在时（例如水平短顶面与凹弧成 90° 转角），刀心不能直接斜连线：
    那会把转角处的材料留成斜坡。此时以原轮廓拐点为圆心、刀半径为半径补一段
    原生 G02/G03 桥接圆弧，仍是解析几何，不拆点。
    """
    out = list(segs)
    bridges = {}
    for i in range(len(out) - 1):
        cand = _xz_intersections(out[i], out[i + 1])
        scored = []
        for pt in (cand or []):
            fa = _xz_seg_frac(out[i], pt)
            fb = _xz_seg_frac(out[i + 1], pt)
            if -1e-6 <= fa <= 1.0 + 1e-6 and -1e-6 <= fb <= 1.0 + 1e-6:
                scored.append(((1.0 - fa) + fb, pt))
        if scored:
            pt = min(scored, key=lambda q: q[0])[1]
            out[i] = _xz_trim_seg(out[i], pt, True)
            out[i + 1] = _xz_trim_seg(out[i + 1], pt, False)
            continue
        if not joints or i >= len(joints) or d <= 1e-9:
            continue
        jx, jz = joints[i]
        p1 = _xz_seg_point(out[i], True)
        p2 = _xz_seg_point(out[i + 1], False)
        r1 = math.hypot(p1[0] - jx, p1[1] - jz)
        r2 = math.hypot(p2[0] - jx, p2[1] - jz)
        if abs(r1 - d) > 2e-4 or abs(r2 - d) > 2e-4:
            continue
        a1 = math.degrees(math.atan2(p1[1] - jz, p1[0] - jx))
        a2 = math.degrees(math.atan2(p2[1] - jz, p2[0] - jx))
        sw = (a2 - a1 + 180.0) % 360.0 - 180.0
        if abs(sw) > 1e-7:
            bridges[i] = ("arc", jx, jz, d, a1, sw, 1.0)
    # 去掉修剪后退化的零长度段，并插入拐点桥接弧
    clean = []
    for i, sg in enumerate(out):
        if sg[0] == "line":
            if math.hypot(sg[3] - sg[1], sg[4] - sg[2]) <= 1e-8:
                pass
            else:
                clean.append(sg)
        elif sg[0] == "arc" and abs(sg[5]) <= 1e-8:
            pass
        else:
            clean.append(sg)
        if i in bridges:
            clean.append(bridges[i])
    return clean


def xz_offset_segments(segs, d: float) -> list:
    """把 XZ 内的段沿「向上法向」整体偏置 d（mm），并把相邻段斜接。

    球头刀刀心必须走偏置面，刀面才与理论曲面相切；平底刀走同一路会留下一整圈台阶。
    单独的线段偏置会在直线-圆弧相切/折角处留下 0.1~0.2mm 的 Z 台阶，
    CAD 从台阶低点继续走下一段会把整条直线拉成斜线（实测浅弧过切 1.44%）。
    相邻段的交点在这里用解析求交修剪，刀路仍是原生 G01/G02/G03。
    """
    out = []
    for sg in segs:
        if sg[0] == "line":
            _, x1, z1, x2, z2 = sg
            dx, dz = x2 - x1, z2 - z1
            ln = math.hypot(dx, dz)
            if ln <= 1e-12:
                continue
            nx, nz = -dz / ln, dx / ln
            if nz < 0:
                nx, nz = -nx, -nz
            out.append(("line", x1 + nx * d, z1 + nz * d, x2 + nx * d, z2 + nz * d))
        elif sg[0] == "cap":
            _, xc, ztop, half = sg
            out.append(("line", xc - half, ztop + d, xc + half, ztop + d))
        else:
            _, cx, cz, R, a0, sw, sgn = sg
            Rp = (R + d) if sgn > 0 else max(R - d, 1e-6)
            out.append(("arc", cx, cz, Rp, a0, sw, sgn))
    joints = []
    for a, b in zip(segs, segs[1:]):
        pa = _xz_seg_point(a, True)
        pb = _xz_seg_point(b, False)
        joints.append(((pa[0] + pb[0]) / 2.0, (pa[1] + pb[1]) / 2.0))
    return _xz_join_segments(out, joints=joints, d=float(d))


def xz_clamp_zmin(segs, zmin: float) -> list:
    """把球头刀刀心轨迹限制在 z >= zmin（刀尖不得低于基体顶面）。

    凹形扫掠肋的顶面会低于基体顶面（那里已经和基体融合成一块料），
    按偏置曲线原样下刀会把基体顶面多切一刀（实测凹弧最低点过切 1 mm）。
    这里只做解析裁剪：圆弧在 z=zmin 的弦用原生水平 G01 代替，
    圆弧本体仍是 G02/G03，不生成任何拟合点。
    """
    out = []
    zmin = float(zmin)
    for sg in segs:
        if sg[0] == "line":
            _, x1, z1, x2, z2 = sg
            hi1 = z1 >= zmin - 1e-9
            hi2 = z2 >= zmin - 1e-9
            if hi1 and hi2:
                out.append(sg)
                continue
            if (not hi1) and (not hi2):
                if abs(x2 - x1) > 1e-9:
                    out.append(("line", x1, zmin, x2, zmin))
                continue
            t = (zmin - z1) / (z2 - z1)
            xc = x1 + (x2 - x1) * t
            if hi1:
                out.append(("line", x1, z1, xc, zmin))
                if abs(x2 - xc) > 1e-9:
                    out.append(("line", xc, zmin, x2, zmin))
            else:
                if abs(xc - x1) > 1e-9:
                    out.append(("line", x1, zmin, xc, zmin))
                out.append(("line", xc, zmin, x2, z2))
            continue
        if sg[0] == "cap":
            _, xc, zt, half = sg
            out.append(("line", xc - half, max(zt, zmin), xc + half, max(zt, zmin)))
            continue
        _, cx, cz, R, a0, span, sgn = sg
        if R <= 1e-9 or abs(span) <= 1e-9:
            continue
        ratio = (zmin - cz) / R
        if ratio <= -1.0 + 1e-12:
            out.append(sg)
            continue
        ts = [0.0, 1.0]
        if -1.0 < ratio < 1.0:
            base = math.degrees(math.asin(ratio))
            for cand in (base, 180.0 - base):
                for k in range(-2, 3):
                    ang = cand + 360.0 * k
                    t = (ang - a0) / span
                    if 1e-9 < t < 1.0 - 1e-9:
                        ts.append(t)
        ts = sorted(set(round(t, 12) for t in ts))
        for ta, tb in zip(ts, ts[1:]):
            if tb - ta <= 1e-12:
                continue
            am = a0 + span * (0.5 * (ta + tb))
            zm = cz + R * math.sin(math.radians(am))
            if zm >= zmin - 1e-9:
                out.append(("arc", cx, cz, R, a0 + span * ta, span * (tb - ta), sgn))
            else:
                xa = cx + R * math.cos(math.radians(a0 + span * ta))
                xb = cx + R * math.cos(math.radians(a0 + span * tb))
                if abs(xb - xa) > 1e-9:
                    out.append(("line", xa, zmin, xb, zmin))
    return out


def sweep_problems(spec) -> list:
    """扫掠体的工艺限制（三轴单面做不出来时给报告用）。"""
    out = []
    for i, sw in enumerate((getattr(spec, "sweeps", None) or []), 1):
        info = sweep_machinability(spec, sw)
        if not info["ok"]:
            out.append(f"扫掠体 {i}：{info['reason']}")
    return out


# ------------------------------------------------------------------ 通用回转
def _rev_profile(spec: PartSpec):
    prof = getattr(spec, "revolve_profile", None) or []
    return [dict(x) for x in prof if isinstance(x, dict)]


def _ang_in(t: float, t0: float, span: float) -> bool:
    if abs(span) >= 2.0 * math.pi - 1e-12:
        return True
    d = (t - t0) if span >= 0 else (t0 - t)
    return (-1e-12) <= (d % (2.0 * math.pi)) <= (abs(span) + 1e-12)


def _rev_arc_geom(seg):
    """(rc, zc, R, a1, span)：圆心、半径、起点角、带符号张角。"""
    rc = float(seg["center"][0])
    zc = float(seg["center"][1])
    r1 = float(seg["p1"][0])
    z1 = float(seg["p1"][1])
    r2 = float(seg["p2"][0])
    z2 = float(seg["p2"][1])
    R = math.hypot(r1 - rc, z1 - zc)
    a1 = math.atan2(z1 - zc, r1 - rc)
    a2 = math.atan2(z2 - zc, r2 - rc)
    if int(seg.get("direction") or 1) >= 0:
        span = (a2 - a1) % (2.0 * math.pi)
    else:
        span = -((a1 - a2) % (2.0 * math.pi))
    return rc, zc, R, a1, span


def _rev_arc_r2z(seg) -> float:
    """∫ r² dz 沿 (r,z) 圆弧段（带符号，绝无点列采样）。"""
    rc, zc, R, a1, span = _rev_arc_geom(seg)
    if R <= 1e-12:
        return 0.0

    def F(t):
        return (rc * rc * math.sin(t)
                + 2.0 * rc * R * (t / 2.0 + math.sin(2.0 * t) / 4.0)
                + R * R * (math.sin(t) - math.sin(t) ** 3 / 3.0))

    return R * (F(a1 + span) - F(a1))


# 8 点 Gauss-Legendre（[-1,1]）
_GL_X = (-0.9602898564975363, -0.7966664774136267, -0.5255324099163290,
         -0.1834346424956498, 0.1834346424956498, 0.5255324099163290,
         0.7966664774136267, 0.9602898564975363)
_GL_W = (0.1012285362903763, 0.2223810344533745, 0.3137066458778873,
         0.3626837833783620, 0.3626837833783620, 0.3137066458778873,
         0.2223810344533745, 0.1012285362903763)


def _gauss(fn, t0, t1) -> float:
    half = 0.5 * (t1 - t0)
    mid = 0.5 * (t1 + t0)
    return half * sum(w * fn(mid + half * x) for x, w in zip(_GL_X, _GL_W))


def _rev_seg_moment(seg):
    """返回 (∫r²dz, ∫r²·z dz) 沿一段母线（带符号）。"""
    typ = str(seg.get("type") or "line").lower()
    if typ == "arc":
        rc, zc, R, a1, span = _rev_arc_geom(seg)
        if R <= 1e-12:
            return 0.0, 0.0

        def m2(t):
            r = rc + R * math.cos(t)
            return r * r * R * math.cos(t)

        def m2z(t):
            r = rc + R * math.cos(t)
            z = zc + R * math.sin(t)
            return r * r * z * R * math.cos(t)

        return (_gauss(m2, a1, a1 + span), _gauss(m2z, a1, a1 + span))
    r1, z1 = float(seg["p1"][0]), float(seg["p1"][1])
    r2, z2 = float(seg["p2"][0]), float(seg["p2"][1])
    dz = z2 - z1
    dr = r2 - r1
    i2 = dz * (r1 * r1 + r1 * r2 + r2 * r2) / 3.0
    # ∫ r² z dz，(r,z) 线性参数化后展开
    f = lambda u: ((r1 + dr * u) ** 2) * (z1 + dz * u)
    i2z = dz * _gauss(f, 0.0, 1.0)
    return i2, i2z


def revolve_volume(spec: PartSpec):
    """通用回转基体体积（mm³）= π·|∮ r² dz|，母线为 (r,z) 平面闭合轮廓。"""
    prof = _rev_profile(spec)
    if not prof:
        return None
    acc = 0.0
    for seg in prof:
        acc += _rev_seg_moment(seg)[0]
    v = abs(math.pi * acc)
    return v if v > 1e-9 else None


def revolve_centroid(spec: PartSpec):
    """回转体质心 (0,0,zc)；用同一套解析积分，不做点列采样。"""
    prof = _rev_profile(spec)
    if not prof:
        return None
    i2 = 0.0
    i2z = 0.0
    for seg in prof:
        a, b = _rev_seg_moment(seg)
        i2 += a
        i2z += b
    # 闭合母线的绕向由图纸/识图决定，可能是顺时针也可能是逆时针。
    # ∫r²dz 与 ∫z·r²dz 会同时变号；这里先把两者统一到正体积方向，
    # 否则质心 z 会被 abs() 单独翻错（p14 实测 −21.43，正确 +21.43）。
    if abs(i2) <= 1e-12:
        return None
    if i2 < 0.0:
        i2 = -i2
        i2z = -i2z
    zc = i2z / i2
    return (0.0, 0.0, zc)


def revolve_bbox(spec: PartSpec):
    """通用回转体世界包围盒 (x0,x1,y0,y1,z0,z1)，mm。"""
    prof = _rev_profile(spec)
    if not prof:
        return None
    rmax = 0.0
    zmin = None
    zmax = None
    def _put(z):
        nonlocal zmin, zmax
        zmin = z if zmin is None else min(zmin, z)
        zmax = z if zmax is None else max(zmax, z)
    for seg in prof:
        for k in ("p1", "p2"):
            q = seg.get(k)
            if q:
                rmax = max(rmax, float(q[0]))
                _put(float(q[1]))
        if str(seg.get("type") or "").lower() == "arc" and seg.get("center"):
            rc, zc, R, a1, span = _rev_arc_geom(seg)
            if _ang_in(0.0, a1, span):
                rmax = max(rmax, rc + R)
            if _ang_in(math.pi, a1, span):
                rmax = max(rmax, abs(rc - R))
            if _ang_in(math.pi / 2.0, a1, span):
                _put(zc + R)
            if _ang_in(-math.pi / 2.0, a1, span):
                _put(zc - R)
    if zmin is None:
        return None
    return (-rmax, rmax, -rmax, rmax, zmin, zmax)


def dome_volume(spec: PartSpec) -> float:
    """圆顶面增加的料体积（mm³）。

    仅对圆盘基体有解析式：
      - 椭圆顶面（SW InsertDome 默认）= 半个旋转椭球体 = 2/3·πa²h
      - 球面帽 = πh(3a²+h²)/6
    矩形基体上的椭圆顶面没有闭式解，返回 0，
    让 part_volume_exact 退回 None（报告里会显式标出量测不了）。
    """
    h = float(getattr(spec, "dome_h", 0.0) or 0.0)
    if h <= 0:
        return 0.0
    if spec.base_kind == "round":
        a = float(spec.diameter) / 2.0
    else:
        a = min(float(spec.length or 0.0), float(spec.width or 0.0)) / 2.0
    if a <= 0:
        return 0.0
    if bool(getattr(spec, "dome_elliptic", True)):
        return (2.0 / 3.0) * math.pi * a * a * h
    return math.pi * h * (3.0 * a * a + h * h) / 6.0


def dome_cut_overlap_volume(spec: PartSpec) -> float:
    """圆顶区域内被特征切掉的那部分料体积（mm³）。

    part_volume_grid 先按平板算基体、再整体加 dome_volume；但落在
    孔/腔/球坑轮廓里的圆顶材料其实已经被切掉了，整体加回会让
    「dome + 特征」的理论体积偏大（实测 dome+球坑 +3.4%）。
    这里用与回灌侧 verify._expected_top 相同的顶面模型：
    特征从基体顶面 Z=0 往下切，圆顶在 Z>0，因此特征轮廓内的
    圆顶高度要整体从加回量里扣除。

    积分网格跟随圆顶半径自适应，最多 401×401；被积函数光滑，
    实测与中心球坑的解析值相差 <0.01%。
    """
    import numpy as np
    h = float(getattr(spec, "dome_h", 0.0) or 0.0)
    if h <= 0.0:
        return 0.0
    if spec.base_kind == "round":
        a = float(spec.diameter) / 2.0
    else:
        a = min(float(spec.length or 0.0), float(spec.width or 0.0)) / 2.0
    if a <= 1e-9:
        return 0.0
    feats = spec.build_features()
    if not feats:
        return 0.0
    n = 401
    d = 2.0 * a / n
    xs = -a + (np.arange(n) + 0.5) * d
    X, Y = np.meshgrid(xs, xs)
    rho2 = X * X + Y * Y
    inside = rho2 <= a * a
    if not inside.any():
        return 0.0
    if bool(getattr(spec, "dome_elliptic", True)):
        z = h * np.sqrt(np.maximum(1.0 - rho2 / (a * a), 0.0))
    else:
        R = (a * a + h * h) / (2.0 * h)
        z = (h - R) + np.sqrt(np.maximum(R * R - rho2, 0.0))
    cut = np.zeros(X.shape, dtype=bool)
    any_cut = False
    for f in feats:
        if f.type == "sphere":
            RR = max(float(f.diameter or 0.0) / 2.0, 0.0)
            hh = min(max(float(f.depth or RR), 0.0), RR)
            if RR <= 0.0 or hh <= 0.0:
                continue
            rr2 = (X - f.x) ** 2 + (Y - f.y) ** 2
            cap2 = max(RR * RR - (RR - hh) ** 2, 0.0)
            sel = rr2 <= cap2
        elif f.type in ("hole", "pocket", "slot", "step"):
            if float(feature_depth(spec, f)) <= 0.0:
                continue
            try:
                g = spec.grow_of(f)
            except Exception:
                g = 0.0
            sel = _feature_mask(f, X, Y, g, spec)
            if f.type == "hole" and f.counterbore_depth > 0 and f.counterbore_dia > f.diameter:
                cb = Feature(type="hole", x=f.x, y=f.y, diameter=f.counterbore_dia)
                sel = sel | _feature_mask(cb, X, Y, spec=spec)
        else:
            continue
        cut |= sel
        any_cut = True
    if not any_cut:
        return 0.0
    m = cut & inside
    if not m.any():
        return 0.0
    return float(z[m].sum() * d * d)


def shaft_volume(spec: PartSpec) -> float:
    v = 0.0
    for seg in spec.shaft_segments:
        d = float(seg.get("dia") or 0)
        L = float(seg.get("length") or 0)
        v += math.pi * (d / 2.0) ** 2 * L
    return v


def _feature_mask(f, X, Y, grow: float = 0.0, spec: PartSpec | None = None):
    """特征轮廓的解析掩膜（向量化）。比 _mask(flatten(...)) 快两个数量级，
    而且没有弦近似误差（原来 1.5° 离散还会让面积偏 0.02%）。"""
    import numpy as np
    if f.type == "profile_cut":
        # 任意闭合直线/圆弧轮廓：按 1.5° 弦离散做点在多边形内判定。
        # 只用于数值积分掩膜；CAD 侧始终用原生 line/arc 建曲线。
        _poly = flatten(segs_from_profile(f.profile), 1.5)
        _m = _polys_mask(X, Y, _poly) if _poly else None
        return _m if _m is not None else np.zeros(X.shape, bool)
    if f.type == "hole":
        r = f.diameter / 2.0 + grow
        return (X - f.x) ** 2 + (Y - f.y) ** 2 <= r * r
    if f.type == "slot":
        L = (f.length or f.width) + 2 * grow
        W = f.width + 2 * grow
        a = math.radians(f.angle or 0.0)
        ca, sa = math.cos(a), math.sin(a)
        dx, dy = X - f.x, Y - f.y
        u = dx * ca + dy * sa
        v = -dx * sa + dy * ca
        # ????????????????????????
        # ???? p28 ? 18 ?????????? 2.4%?
        # ????????? 0.6%????????????????
        # ???????????????
        if spec is not None and round_slot_band_area(spec, f) > 0.0:
            return (np.abs(u) <= L / 2.0) & (np.abs(v) <= W / 2.0)
        half = max(0.0, (L - W) / 2.0)
        uu = np.clip(u, -half, half)
        return (u - uu) ** 2 + v * v <= (W / 2.0) ** 2
    # pocket / step / boss：矩形
    hl = f.length / 2.0 + grow
    hw = f.width / 2.0 + grow
    return (np.abs(X - f.x) <= hl) & (np.abs(Y - f.y) <= hw)


def _rect_mask(l: float, w: float, r: float, X, Y):
    """矩形（可带圆角）的解析掩膜。"""
    import numpy as np
    hl, hw = l / 2.0, w / 2.0
    m = (np.abs(X) <= hl) & (np.abs(Y) <= hw)
    if r <= 0:
        return m
    r = min(r, min(l, w) / 2.0)
    # 四角之外：|x|>hl-r 且 |y|>hw-r 的区域要按圆判
    cx, cy = hl - r, hw - r
    in_corner = (np.abs(X) > cx) & (np.abs(Y) > cy)
    dist = (np.abs(X) - cx) ** 2 + (np.abs(Y) - cy) ** 2
    return m & (~in_corner | (dist <= r * r))


def _grid(spec: PartSpec, step: float):
    import numpy as np
    # 单元中心采样（midpoint rule）：节点采样会把边界格点按整格计权，
    # 矩形基体因此系统性偏大 O(step)（60×40、step=0.2 时 +0.59%，
    # 正好解释了抽壳解析值与栅格值的固定差）。单元中心对称布置、间距
    # 恒等于 step；矩形边落在半格处时面积精确，其余情形只有 O(step²)
    # 的边界残差，不会再随 step 线性漂移。
    def _centers(half: float):
        n = int(math.ceil((half + step) / step))
        return (np.arange(2 * n, dtype=float) + 0.5 - n) * step

    if getattr(spec, "base_profile", None):
        _x0, _y0, _x1, _y1 = spec.profile_bbox()
        xs = np.arange(math.floor((_x0 - step) / step) * step + step / 2.0,
                       _x1 + step, step)
        ys = np.arange(math.floor((_y0 - step) / step) * step + step / 2.0,
                       _y1 + step, step)
        X, Y = np.meshgrid(xs, ys)
        _poly = flatten(segs_from_profile(spec.base_profile), 1.5)
        base = _polys_mask(X, Y, _poly)
        if base is None:
            base = np.zeros(X.shape, bool)
    elif spec.base_kind == "round":
        r = spec.diameter / 2.0
        xs = _centers(r)
        X, Y = np.meshgrid(xs, xs)
        base = (X ** 2 + Y ** 2) <= r ** 2
    else:
        xs = _centers(spec.length / 2.0)
        ys = _centers(spec.width / 2.0)
        X, Y = np.meshgrid(xs, ys)
        base = _rect_mask(spec.length, spec.width, spec.corner_radius, X, Y)
        _l = str(getattr(spec, "end_left", "square") or "square").lower()
        _r = str(getattr(spec, "end_right", "square") or "square").lower()
        if _l == "arc" or _r == "arc":
            import numpy as _np
            _hw = spec.width / 2.0
            _xi = max(0.0, spec.length / 2.0 - _hw)
            _xl = -_xi if _l == "arc" else -spec.length / 2.0
            _xr = _xi if _r == "arc" else spec.length / 2.0
            base = (X >= _xl) & (X <= _xr) & (Y >= -_hw) & (Y <= _hw)
            if _l == "arc":
                base |= ((X + _xi) ** 2 + Y ** 2 <= _hw ** 2) & (X <= -_xi)
            if _r == "arc":
                base |= ((X - _xi) ** 2 + Y ** 2 <= _hw ** 2) & (X >= _xi)
    # 顶面切除深度 / 底面切除深度分开累计。face=bottom 的特征（十字节正反两条槽）
    # 从底面往上切，若也记到 top 里，两条槽在中心相交处只会扣一次深度，
    # 理论体积会系统性偏大（Ø90×36 十字节实测 +4.5%）。材料区间是
    # [bot, T-top]，两边都切穿时自动截零。
    top = np.zeros(X.shape, dtype=float)
    bot = np.zeros(X.shape, dtype=float)
    for f in spec.build_features():          # 重复轮廓已剔除，薄壁孔按外扩算
        _fb = str(getattr(f, "face", "top") or "top").lower() == "bottom"
        if f.type == "sphere":
            # 球坑：半径 R 的球心在顶面上方 (R-h) 处，削出高 h 的球冠。
            # 逐格切深 depth(r) = h - R + sqrt(R^2 - r^2)：r=0 时等于 h，
            # 冠边 r=sqrt(2Rh-h^2) 时为 0，冠外为负、截零。
            # 旧写法 h - sqrt(R^2-r^2) 少了 -R（冠边变成 2h-R），与
            # part_volume_exact 的球冠解析式对不上，且 T 未定义直接 NameError。
            R = max(float(f.diameter or 0.0) / 2.0, 0.0)
            h = min(max(float(f.depth or R), 0.0), R)
            if R > 0.0 and h > 0.0:
                rr2 = (X - f.x) ** 2 + (Y - f.y) ** 2
                z_off = np.sqrt(np.maximum(R * R - rr2, 0.0))
                top = np.maximum(top, np.maximum(h - R + z_off, 0.0))
            continue
        if f.type in ("hole", "pocket", "slot", "step", "profile_cut"):
            d = float(feature_depth(spec, f))
            g = spec.grow_of(f)
            m = _feature_mask(f, X, Y, g, spec)
            tgt = bot if _fb else top
            tgt[m] = np.maximum(tgt[m], d)
            if f.type == "hole" and f.counterbore_depth > 0 and f.counterbore_dia > f.diameter:
                cb = Feature(type="hole", x=f.x, y=f.y, diameter=f.counterbore_dia)
                m2 = _feature_mask(cb, X, Y, spec=spec)
                tgt[m2] = np.maximum(tgt[m2], f.counterbore_depth)
    return X, Y, base, top, bot


def _grid_depth(spec: PartSpec, step: float):
    """栅格体积/质心共用：返回 (X, Y, base, lo, hi)。

    lo/hi 是每根柱上**剩余材料**的 z 区间（[lo, hi]），底面切除抬 lo、
    顶面切除降 hi；两者相交时厚度自动截零。旧版只返回顶面深度，
    face=bottom 的特征会被静默按顶面处理。
    """
    import numpy as np
    X, Y, base, top, bot = _grid(spec, step)
    T = float(spec.thickness)
    lo = np.clip(bot, 0.0, T)
    hi = np.maximum(T - np.clip(top, 0.0, T), lo)
    return X, Y, base, lo, hi


def _rect_area(l: float, w: float, r: float = 0.0) -> float:
    r = min(max(r, 0.0), min(l, w) / 2.0)
    return l * w - (4.0 - math.pi) * r * r


def shell_cavity_band(spec: PartSpec):
    """抽壳内腔沿板厚方向的深度区间（从顶面往下量，mm）。

    开口朝上（默认）：(0, T-t)；封闭：(t, T-t)。区间内是空气，不扣材料。
    """
    T = float(spec.thickness)
    t = max(float(getattr(spec, "shell_t", 0.0) or 0.0), 0.0)
    if t <= 0 or T <= 0:
        return (0.0, 0.0)
    _open = str(getattr(spec, "shell_open", "top") or "top").lower()
    if _open == "none":
        lo = min(t, T)
        hi = min(t + max(T - 2.0 * t, 0.0), T)
        return (lo, max(hi, lo))
    return (0.0, max(T - t, 0.0))


def _span_inside(depth: float, band) -> float:
    """从顶面往下切 depth 时，落在空腔区间里的那段深度。"""
    lo, hi = band
    return max(0.0, min(float(depth), hi) - lo)


def _shell_scope_poly(poly, ip) -> str:
    from .conflict import contained, gap_between
    try:
        if contained(poly, ip, 0.05):
            return "in"
        if gap_between(poly, ip) > 0:
            return "out"
    except Exception:
        return "mix"
    return "mix"


def _shell_scopes(spec: PartSpec, feats, t: float):
    """每个特征相对内腔的位置：in / out / mix。mix 表示解析法不适用。"""
    from .conflict import outline as _ol
    from .geom import flatten
    polys = flatten(inner_outline(spec, t), 6.0)
    if not polys:
        return None
    ip = polys[0]
    return [_shell_scope_poly(_ol(f, 48), ip) for f in feats]


def _shell_scope_of_circle(spec: PartSpec, f: Feature, r: float, t: float):
    from .conflict import outline as _ol
    from .geom import circle as _circle, flatten
    polys = flatten(inner_outline(spec, t), 6.0)
    if not polys:
        return None
    ring = Feature(type="hole", x=f.x, y=f.y, diameter=2.0 * r)
    return _shell_scope_poly(_ol(ring, 48), polys[0])


def keyway_volume(f, spec):
    """键槽（非轴对称）扣除体积。

    截面：孔半径 r、键槽宽 w、径向深 d（从孔壁量起），
    A = w*(r+d) - [ b*sqrt(r^2-b^2) + r^2*asin(b/r) ],  b = w/2
    轴向长度 L 取特征的 length，缺省用 spec.thickness。
    """
    r = float(getattr(f, "diameter", 0.0) or 0.0) / 2.0
    if r <= 0:
        r = float(getattr(spec, "bore", 0.0) or 0.0) / 2.0
    w = float(getattr(f, "width", 0.0) or 0.0)
    d = float(getattr(f, "depth", 0.0) or 0.0)
    L = float(getattr(f, "length", 0.0) or 0.0)
    if L <= 0:
        L = float(getattr(spec, "thickness", 0.0) or 0.0)
    if r <= 0 or w <= 0 or d <= 0 or L <= 0:
        return 0.0
    b = w / 2.0
    if b >= r:
        area = w * d
    else:
        area = w * (r + d) - (b * math.sqrt(r * r - b * b)
                              + r * r * math.asin(b / r))
    return max(area, 0.0) * L


def _round_cross_slot_exact(spec: PartSpec, feats):
    """?? + ???? + ??????????

    p28 ?????90?36??? ?32 ????/???? 18 ??? 14 ?
    ???????????????????????????
    ???????????? ? ???????????? Z
    ????????????????????????????
    None??????????
    """
    if str(getattr(spec, "base_kind", "")) != "round":
        return None
    T = float(getattr(spec, "thickness", 0.0) or 0.0)
    R = float(getattr(spec, "diameter", 0.0) or 0.0) / 2.0
    if T <= 0.0 or R <= 0.0:
        return None
    if (float(getattr(spec, "shell_t", 0.0) or 0.0) > 0.0
            or float(getattr(spec, "dome_h", 0.0) or 0.0) > 0.0
            or float(getattr(spec, "fillet", 0.0) or 0.0) > 0.0
            or float(getattr(spec, "chamfer", 0.0) or 0.0) > 0.0
            or getattr(spec, "sweep_profile", None)):
        return None

    holes = [f for f in feats if f.type == "hole"]
    slots = [f for f in feats if f.type == "slot"]
    if len(feats) != len(holes) + len(slots) or len(holes) != 1 or not slots:
        return None
    hf = holes[0]
    rh = float(hf.diameter or 0.0) / 2.0
    if (rh <= 0.0 or abs(float(hf.x or 0.0)) > 1e-6
            or abs(float(hf.y or 0.0)) > 1e-6 or not hf.is_through()):
        return None
    try:
        if spec.grow_of(hf) > 1e-9:
            return None
    except Exception:
        pass

    intervals = []
    v = math.pi * R * R * T - math.pi * rh * rh * T
    for f in slots:
        if abs(float(f.x or 0.0)) > 1e-6 or abs(float(f.y or 0.0)) > 1e-6:
            return None
        band = round_slot_band_area(spec, f)
        d = float(feature_depth(spec, f))
        if band <= 0.0 or d <= 0.0 or d > T + 1e-9:
            return None
        try:
            if spec.grow_of(f) > 1e-9:
                return None
        except Exception:
            pass
        a = float(f.width or 0.0) / 2.0
        if a >= rh:
            overlap = math.pi * rh * rh
        else:
            overlap = 2.0 * (a * math.sqrt(max(rh * rh - a * a, 0.0))
                             + rh * rh * math.asin(min(1.0, a / rh)))
        is_bottom = str(getattr(f, "face", "top") or "top").lower() == "bottom"
        z0, z1 = (0.0, d) if is_bottom else (T - d, T)
        for a0, a1 in intervals:
            if max(z0, a0) < min(z1, a1) - 1e-9:
                return None
        intervals.append((z0, z1))
        v -= max(band - overlap, 0.0) * d
    return max(v, 0.0)


def feature_bbox(f: Feature):
    """特征 XY 包围盒 (x0,y0,x1,y1)。profile_cut 用真实轮廓（含圆弧极值）。"""
    if f.type == "profile_cut":
        try:
            from .geom import segs_bbox
            bb = segs_bbox(segs_from_profile(f.profile))
            if bb[2] - bb[0] > 1e-9 and bb[3] - bb[1] > 1e-9:
                return bb
        except Exception:
            pass
    if f.type in ("hole", "sphere"):
        r = f.diameter / 2.0
        return (f.x - r, f.y - r, f.x + r, f.y + r)
    rx = max(f.length, f.width or 0.0) / 2.0
    ry = (f.width or f.length) / 2.0
    return (f.x - rx, f.y - ry, f.x + rx, f.y + ry)


def part_volume_exact(spec: PartSpec):
    """解析体积；算不了（特征互相交叠 / 有凸台 / 超出覆盖范围）时返回 None。

    存在的意义：栅格法 part_volume(step) 在特征边界落在格子中间时**不收敛**
    （实测 step 1.0→0.05 误差在 -0.018%~-0.074% 间随机游走），
    拿它当"理论值"会把参考值自己的偏差算成 CAD 建模误差。
    """
    try:
        if spec.sheet_t > 0 and spec.sheet_L > 0:
            from .sheet import sheet_volume
            return sheet_volume(spec.sheet_L, spec.sheet_W, spec.sheet_t, spec.flange_h)
        if spec.teeth > 0 and spec.module > 0:
            from .gear import gear_volume
            _gh = []
            for _f in (getattr(spec, "features", None) or []):
                try:
                    if str(getattr(_f, "type", "")).lower() == "hole" and float(getattr(_f, "diameter", 0) or 0) > 0:
                        _gh.append({"diameter": float(_f.diameter),
                                    "depth": float(getattr(_f, "depth", 0) or 0),
                                    "x": float(getattr(_f, "x", 0) or 0),
                                    "y": float(getattr(_f, "y", 0) or 0)})
                except Exception:
                    pass
            return gear_volume(spec.module, spec.teeth, spec.face_width,
                               bore=spec.bore, keyway_w=spec.keyway_w, keyway_d=spec.keyway_d,
                               hub_dia=spec.hub_dia, hub_len=spec.hub_len,
                               bore_depth=spec.bore_depth, small_bore=spec.small_bore,
                               chamfer=spec.chamfer, extra_holes=_gh)
        if spec.shaft_segments:
            v = 0.0
            total = 0.0
            for s in spec.shaft_segments:
                d = float(s.get("dia") or 0)
                ln = float(s.get("length") or 0)
                total += ln
                v += math.pi * (d / 2.0) ** 2 * ln
            for _f in (getattr(spec, "features", None) or []):
                try:
                    _t = str(getattr(_f, "type", "")).lower()
                    if _t == "keyway":
                        v -= max(float(_f.width or 0), 0.0) * max(float(_f.depth or 0), 0.0) * max(float(_f.length or 0), 0.0)
                    elif _t == "hole" and str(getattr(_f, "face", "")).lower() == "right":
                        _d = float(getattr(_f, "diameter", 0) or 0)
                        _dep = float(getattr(_f, "depth", 0) or 0)
                        v -= math.pi * (_d / 2.0) ** 2 * (total if _dep <= 0 else min(_dep, total))
                except Exception:
                    pass
            return max(v, 0.0)
        if getattr(spec, "revolve_profile", None):
            # 回转体：键槽 / 中心孔 / 端面贯通直槽（联轴器十字槽）可解析扣除；
            # 其它非轴对称特征仍如实退回栅格法。
            _rv = revolve_volume(spec) or 0.0
            _prof = [x for x in (spec.revolve_profile or []) if isinstance(x, dict)]
            _rs = []
            for _sg in _prof:
                for _k in ("p1", "p2"):
                    _q = _sg.get(_k)
                    if isinstance(_q, (list, tuple)) and len(_q) >= 2:
                        _rs.append(float(_q[0]))
            _Rmax = max(_rs) if _rs else spec.diameter / 2.0
            _Rin = (float(getattr(spec, "bore", 0.0) or 0.0) / 2.0) or (
                min([r for r in _rs if r > 1e-6] or [0.0]))
            _T = float(spec.thickness or 0.0)

            def _slice(Rad, a):
                """圆盘 |x|<=a 那一带的面积（含两侧）。"""
                a = max(0.0, min(a, Rad))
                return 2.0 * (a * math.sqrt(max(Rad * Rad - a * a, 0.0))
                              + Rad * Rad * math.asin(min(1.0, a / Rad)))

            for _f in spec.features:
                _t = str(getattr(_f, "type", "")).lower()
                if _t == "keyway":
                    _rv -= keyway_volume(_f, spec)
                    continue
                _d = min(feature_depth(spec, _f), _T)
                if _t == "hole" and abs(_f.x) < 1e-6 and abs(_f.y) < 1e-6:
                    _rv -= math.pi * (_f.diameter / 2.0) ** 2 * _d
                    continue
                if (_t in ("step", "pocket") and abs(_f.x) < 1e-6
                        and abs(_f.y) < 1e-6 and _f.length >= 2.0 * _Rmax - 1e-6
                        and 0.0 < _f.width < 2.0 * _Rmax):
                    # 端面贯通直槽：宽 width、径向量 length，从端面切深 d
                    _a = _f.width / 2.0
                    _rv -= max(_slice(_Rmax, _a) - _slice(_Rin, _a), 0.0) * _d
                    continue
                return None
            # 回转体同样要扣顶棱圆角/倒角：建模侧 SW/NX 都会在回转实体上
            # 施加 spec.fillet（两者同时给出时倒角被圆角覆盖，见 sw.py/nx.py 的
            # 「倒角被覆盖，跳过」分支）。此前这里只扣了特征，expected 系统性偏大。
            # 实测 p14 限位器（Ø76.5 回转 + R2）：理论 55,617.4 / CAD 55,413.5，
            # 差值 203.9 与 fillet_v=206.3 同量级；扣掉后 0.004%。
            _efv = fillet_v(spec) if float(getattr(spec, 'fillet', 0.0) or 0.0) > 0 else chamfer_v(spec)
            return max(_rv - _efv, 0.0)
        # 圆顶面上的切除：基体按平板算，再整体加圆顶体积，最后把落在
        # 特征轮廓内的圆顶材料扣掉（dome_cut_overlap_volume，与回灌侧
        # verify._expected_top 同一顶面模型）。这样 dome+球坑/孔/腔
        # 不再被迫退回栅格，避免 0.1mm 网格边界半个单元格带来的 0.5% 假差。
        T = spec.thickness
        feats = spec.build_features()
        _cross = _round_cross_slot_exact(spec, feats)
        if _cross is not None:
            return _cross
        _sh = float(getattr(spec, "shell_t", 0.0) or 0.0)
        if any(f.type == "boss" for f in feats):
            return None
        if spec.base_kind == "round":
            R = spec.diameter / 2.0
            base = math.pi * R * R * T
            for f in feats:
                if round_slot_band_area(spec, f) > 0:
                    continue          # 横贯全径的直槽，两端本来就在圆外
                rr = (f.diameter / 2.0) if f.type == "hole" else max(f.length, f.width) / 2.0
                if math.hypot(f.x, f.y) + rr > R + 1e-6:
                    return None
        elif getattr(spec, "base_profile", None):
            base = base_area(spec) * T
            _bx0, _by0, _bx1, _by1 = spec.profile_bbox()
            _base_poly = flatten(segs_from_profile(spec.base_profile), arc_step_deg=1.5)[0]
            for f in feats:
                _fx0, _fy0, _fx1, _fy1 = feature_bbox(f)
                if (_fx0 < _bx0 - 1e-6 or _fy0 < _by0 - 1e-6
                        or _fx1 > _bx1 + 1e-6 or _fy1 > _by1 + 1e-6):
                    return None
                # 包围盒在基体内不代表轮廓在基体内：凹轮廓（缺口、内凹圆弧）
                # 里，特征可能一部分落在空气上。解析式只会整块相减，
                # 这种情形如实退回栅格法，由真实材料判定，绝不重复/超额扣体积。
                _fpts = flatten(feature_outline(f), arc_step_deg=1.5)[0]
                if not _poly_all_inside(_base_poly, _fpts, 1e-4):
                    return None
        else:
            base = base_area(spec) * T
            hl, hw = spec.length / 2.0, spec.width / 2.0
            for f in feats:
                if f.type == "profile_cut":
                    _fx0, _fy0, _fx1, _fy1 = feature_bbox(f)
                    if (_fx0 < -hl - 1e-6 or _fx1 > hl + 1e-6
                            or _fy0 < -hw - 1e-6 or _fy1 > hw + 1e-6):
                        return None
                    continue
                rx = (f.diameter / 2.0) if f.type == "hole" else max(f.length, f.width) / 2.0
                ry = (f.diameter / 2.0) if f.type == "hole" else (f.width or f.length) / 2.0
                if abs(f.x) + rx > hl + 1e-6 or abs(f.y) + ry > hw + 1e-6:
                    return None
        _band = (0.0, 0.0)
        _scopes = None
        if _sh > 0:
            # 抽壳：先把基体换成「外壳 − 内腔」，再按特征与内腔的位置关系扣除。
            # 旧实现直接 return，把同一 spec 上的孔/槽/腔/倒角全部丢掉了。
            _band = shell_cavity_band(spec)
            if _band[1] - _band[0] > 0:
                inner_v = shell_inner_area(spec, _sh) * (_band[1] - _band[0])
                base = max(base - inner_v, 0.0)
            _scopes = _shell_scopes(spec, feats, _sh)
            if _scopes is None or any(sc == "mix" for sc in _scopes):
                # 特征跨在内腔壁上：解析面积需要裁剪，如实退回栅格法
                return None
            if any(f.type == "sphere" for f in feats):
                # 球坑在抽壳件上的起算面是内腔底，球冠解析式未覆盖
                return None
        # 特征之间若有交叠/包含，面积不能直接相加（会重复扣）→ 退回栅格法
        if len(feats) > 1:
            if any(f.type == "profile_cut" for f in feats):
                # 任意轮廓的解析裁剪未实现：只保证互不重叠时可直接相加；
                # 包围盒一旦相交就如实退回栅格法，绝不重复扣体积。
                for i in range(len(feats)):
                    bi = feature_bbox(feats[i])
                    for j in range(i + 1, len(feats)):
                        bj = feature_bbox(feats[j])
                        if (min(bi[2], bj[2]) - max(bi[0], bj[0]) > 1e-6
                                and min(bi[3], bj[3]) - max(bi[1], bj[1]) > 1e-6):
                            return None
            else:
                from .conflict import gap_between, outline as _ol
                polys = [_ol(f, 48) for f in feats]
                for i in range(len(feats)):
                    for j in range(i + 1, len(feats)):
                        if gap_between(polys[i], polys[j]) < 0:
                            return None
        v = base

        def _ded(dd, scope):
            """特征切深 dd 里真正落在实心材料上的部分。"""
            if _sh <= 0 or scope == "out":
                return dd
            return max(dd - _span_inside(dd, _band), 0.0)

        for _i, f in enumerate(feats):
            if f.type == "sphere":
                R = f.diameter / 2.0
                h = min(f.depth or R, R)
                v -= sphere_cap_volume(R, h)
                continue
            _scope = "out" if not _scopes else _scopes[_i]
            d = min(feature_depth(spec, f), T)
            if f.type == "hole":
                a = math.pi * (f.diameter / 2.0) ** 2
            elif f.type == "slot":
                a = round_slot_band_area(spec, f)
                if a <= 0.0:
                    w = f.width or f.diameter
                    a = (max(f.length, w) - w) * w + math.pi * (w / 2.0) ** 2
            elif f.type == "profile_cut":
                a = feature_area(f)
            else:
                a = _rect_area(f.length, f.width, f.corner_radius)
            v -= a * _ded(d, _scope)
            # 沉孔：主孔之外那一圈环形材料要单独扣。_grid_depth_map() 一直有这一步，
            # 解析法漏了，导致带沉孔的零件 expected 系统性偏大
            # （实测法兰盘 Ø30 通孔 + Ø90 沉孔：158,854.6 vs CAD 74,220.1，+114%）。
            if (f.type == "hole" and f.counterbore_depth > 0
                    and f.counterbore_dia > f.diameter):
                r_in = f.diameter / 2.0
                r_out = f.counterbore_dia / 2.0
                cb_d = min(f.counterbore_depth, T)
                cb_scope = _scope
                if _sh > 0:
                    cb_scope = _shell_scope_of_circle(spec, f, r_out, _sh) or "mix"
                    if cb_scope == "mix":
                        return None
                v -= math.pi * (r_out * r_out - r_in * r_in) * _ded(cb_d, cb_scope)
        v -= edge_feature_volume(spec)
        v += dome_volume(spec) - dome_cut_overlap_volume(spec)
        v += sweep_added_volume(spec)
        return max(v, 0.0)
    except Exception:
        return None


def volume_reference(spec: PartSpec, step: float = 0.1):
    """返回 (体积, 来源)。来源 = "exact" / "grid"。"""
    ex = part_volume_exact(spec)
    if ex is not None:
        return ex, "exact"
    return part_volume_grid(spec, step), "grid"


def flatten_polys(segs, arc_step_deg: float = 6.0):
    from .geom import flatten
    return flatten(segs, arc_step_deg)


def _polys_mask(X, Y, polys):
    """点集状态的多边形掩码（射线法）。"""
    import numpy as np
    if not polys:
        return None
    m = np.zeros(X.shape, bool)
    for p in polys:
        if len(p) < 3:
            continue
        px = np.array([q[0] for q in p], float)
        py = np.array([q[1] for q in p], float)
        inside = np.zeros(X.shape, bool)
        j = len(px) - 1
        for i in range(len(px)):
            yi, yj = py[i], py[j]
            cond = ((yi > Y) != (yj > Y))
            if cond.any():
                den = (yj - yi)
                if abs(den) < 1e-12:
                    den = 1e-12
                xi = px[i] + (px[j] - px[i]) * (Y - yi) / den
                inside ^= cond & (X < xi)
            j = i
        m |= inside
    return m


def part_volume_grid(spec: PartSpec, step: float = 0.1) -> float:
    """栅格体积（旧实现，保留给解析式覆盖不到的零件 / 收敛检查用）。"""
    import numpy as np
    if spec.sheet_t > 0 and spec.sheet_L > 0:
        from .sheet import sheet_volume
        return sheet_volume(spec.sheet_L, spec.sheet_W, spec.sheet_t, spec.flange_h)
    if spec.teeth > 0 and spec.module > 0:
        from .gear import gear_volume
        return gear_volume(spec.module, spec.teeth, spec.face_width,
                           bore=spec.bore, keyway_w=spec.keyway_w, keyway_d=spec.keyway_d,
                           hub_dia=spec.hub_dia, hub_len=spec.hub_len,
                           bore_depth=spec.bore_depth, small_bore=spec.small_bore, chamfer=spec.chamfer)
    if spec.shaft_segments:
        return shaft_volume(spec)
    if getattr(spec, "revolve_profile", None):
        # 通用回转体：XY 栅格表达不了变半径母线，解析值严格更好。
        return revolve_volume(spec) or 0.0
    X, Y, base, _lo, _hi = _grid_depth(spec, step)
    height = np.maximum(_hi - _lo, 0.0)
    _sh = float(getattr(spec, "shell_t", 0.0) or 0.0)
    if _sh > 0:
        # 抽壳：内腔区域剩余材料只剩底/壁那一层（闭合两层合计 T-cav）
        _lo, _hi = shell_cavity_band(spec)
        _cav = max(_hi - _lo, 0.0)
        if _cav > 0:
            inner = inner_outline(spec, _sh)
            m = _polys_mask(X, Y, flatten_polys(inner))
            if m is not None and m.any():
                height[m] = np.minimum(height[m], max(spec.thickness - _cav, 0.0))
    # 单元面积要用真实 dx*dy：xs 是 linspace(-L, L, nx)，dx 不总等于 step
    # （step=0.05 时 dx/step=0.99983，用 step*step 会让面积差 0.035%，体积回升）
    dx = float(X[0, 1] - X[0, 0]) if X.shape[1] > 1 else step
    dy = float(Y[1, 0] - Y[0, 0]) if Y.shape[0] > 1 else step
    v = float((height[base]).sum() * dx * dy) - edge_feature_volume(spec)
    v += dome_volume(spec) - dome_cut_overlap_volume(spec)
    v += sweep_added_volume(spec)
    return max(v, 0.0)


def part_volume(spec: PartSpec, step: float = 0.1) -> float:
    """理论体积：能用解析式的零件就用解析式（不收敛问题不存在），否则退回栅格。"""
    return volume_reference(spec, step)[0]


def _part_volume_grid_impl(spec: PartSpec, step: float = 0.1) -> float:
    import numpy as np
    if spec.sheet_t > 0 and spec.sheet_L > 0:
        from .sheet import sheet_volume
        return sheet_volume(spec.sheet_L, spec.sheet_W, spec.sheet_t, spec.flange_h)
    if spec.teeth > 0 and spec.module > 0:
        from .gear import gear_volume
        return gear_volume(spec.module, spec.teeth, spec.face_width,
                           bore=spec.bore, keyway_w=spec.keyway_w, keyway_d=spec.keyway_d,
                           hub_dia=spec.hub_dia, hub_len=spec.hub_len,
                           bore_depth=spec.bore_depth, small_bore=spec.small_bore, chamfer=spec.chamfer)
    if spec.shaft_segments:
        return shaft_volume(spec)
    X, Y, base, _lo, _hi = _grid_depth(spec, step)
    height = np.maximum(_hi - _lo, 0.0)
    # 单元面积要用真实 dx*dy：xs 是 linspace(-L, L, nx)，dx 不总等于 step
    # （step=0.05 时 dx/step=0.99983，用 step*step 会让面积差 0.035%，体积回升）
    dx = float(X[0, 1] - X[0, 0]) if X.shape[1] > 1 else step
    dy = float(Y[1, 0] - Y[0, 0]) if Y.shape[0] > 1 else step
    v = float((height[base]).sum() * dx * dy) - edge_feature_volume(spec)
    v += dome_volume(spec) - dome_cut_overlap_volume(spec)
    return max(v, 0.0)


def centroid(spec: PartSpec, step: float = 0.1):
    import numpy as np
    if spec.sheet_t > 0 and spec.sheet_L > 0:
        from .sheet import sheet_volume
        return sheet_volume(spec.sheet_L, spec.sheet_W, spec.sheet_t, spec.flange_h)
    if spec.teeth > 0 and spec.module > 0:
        return (0.0, 0.0, spec.face_width / 2.0)
    if getattr(spec, "revolve_profile", None):
        c = revolve_centroid(spec)
        if c is not None:
            # 母线只描述轴对称本体；键槽 / 中心孔 / 端面贯通直槽都会把质心往
            # 深处拉。不扣的话实测质心与理论差 0.78mm（半联轴器 18×12 端面槽）。
            T = float(spec.thickness or 0.0)
            _rs = []
            for _sg in (spec.revolve_profile or []):
                if not isinstance(_sg, dict):
                    continue
                for _k in ("p1", "p2"):
                    _q = _sg.get(_k)
                    if isinstance(_q, (list, tuple)) and len(_q) >= 2:
                        _rs.append(float(_q[0]))
            _Rmax = max(_rs) if _rs else spec.diameter / 2.0
            _Rin = (float(getattr(spec, "bore", 0.0) or 0.0) / 2.0) or (
                min([r for r in _rs if r > 1e-6] or [0.0]))

            def _slice(Rad, a):
                a = max(0.0, min(a, Rad))
                return 2.0 * (a * math.sqrt(max(Rad * Rad - a * a, 0.0))
                              + Rad * Rad * math.asin(min(1.0, a / Rad)))

            num, den = (revolve_volume(spec) or 0.0) * float(c[2]), \
                (revolve_volume(spec) or 0.0)
            for _f in spec.features:
                _t = str(getattr(_f, "type", "")).lower()
                _d = min(feature_depth(spec, _f), T)
                if _t == "keyway":
                    _v, _z = keyway_volume(_f, spec), T / 2.0
                elif _t == "hole" and abs(_f.x) < 1e-6 and abs(_f.y) < 1e-6:
                    _v = math.pi * (_f.diameter / 2.0) ** 2 * _d
                    _z = T - _d / 2.0 if _f.depth and _f.depth > 0 else T / 2.0
                elif (_t in ("step", "pocket") and abs(_f.x) < 1e-6
                        and abs(_f.y) < 1e-6 and _f.length >= 2.0 * _Rmax - 1e-6
                        and 0.0 < _f.width < 2.0 * _Rmax):
                    _v = max(_slice(_Rmax, _f.width / 2.0)
                             - _slice(_Rin, _f.width / 2.0), 0.0) * _d
                    _z = T - _d / 2.0
                else:
                    return c
                num -= _v * _z
                den -= _v
            return (0.0, 0.0, num / den if den > 1e-9 else float(c[2]))
    if spec.shaft_segments:
        z = 0.0
        num = 0.0
        vol = 0.0
        for seg in spec.shaft_segments:
            d = float(seg.get("dia") or 0)
            L = float(seg.get("length") or 0)
            v = math.pi * (d / 2.0) ** 2 * L
            num += v * (z + L / 2.0)
            vol += v
            z += L
        return (0.0, 0.0, num / vol if vol else 0.0)
    X, Y, base, _lo, _hi = _grid_depth(spec, step)
    hh = np.maximum(_hi - _lo, 0.0) * base
    tot = float(hh.sum())
    if tot <= 0:
        return (0.0, 0.0, 0.0)
    # 不再减"基体掩膜质心"：对称零件它恒为 0（所以长期没暴露），
    # 非对称零件（左圆弧头 + 右开口槽）会把它平移走 —— 实测 U 形叉
    # 解析/NX/SW 三家都是 +0.26，减了 bias 反而变成 −0.47。
    # Z 也要按材料真算：每根"柱"从 0 到 h，形心在 h/2，所以 z = Σ(h²/2)/Σh。
    # 原来直接返回 thickness/2，顶面开腔的零件（泵体 Ø125×85、腔深 50）会差 10mm+，
    # 质心核对必然误报。
    # 每根柱的形心在剩余材料区间中点 (lo+hi)/2 —— face=bottom 的件必须这样算，
    # 直接用 h/2 会把底面切除当成顶面切除，z 质心偏移半个切深。
    zc = float((hh * ((_lo + _hi) / 2.0)).sum() / tot) if tot > 0 else 0.0
    return (float((X * hh).sum() / tot),
            float((Y * hh).sum() / tot),
            zc)


def part_volume_simple(spec: PartSpec) -> float:
    v = base_area(spec) * spec.thickness
    for f in spec.build_features():
        if f.type in ("hole", "pocket", "slot", "step"):
            v -= feature_area(f) * feature_depth(spec, f)
        elif f.type == "boss":
            v += feature_area(f) * min(f.depth or 0.0, spec.thickness * 4)
    return max(v - edge_feature_volume(spec), 0.0)


def stock_size(spec: PartSpec, margin: float = 4.0):
    """毛坯尺寸（mm）。圆顶和扫掠体都必须包进毛坯，否则刀路一出去就撞夹具。

    高度口径：Z0 = 毛坯顶面，基体顶面在 Z=thickness-毛坯高（geometry 里换算）。
    """
    _dh = max(float(getattr(spec, "dome_h", 0.0) or 0.0), 0.0)
    _bb = sweep_bbox(spec)
    _top = _dh
    if _bb:
        # 扫掠体可能伸到基体之外/之上：X/Y 半径和高都要一起扩。
        _half_l = max(float(spec.length) / 2.0, abs(_bb[0]), abs(_bb[1]))
        _half_w = max(float(spec.width) / 2.0, abs(_bb[2]), abs(_bb[3]))
        _top = max(_top, float(_bb[5]) - float(spec.thickness))
        if spec.base_kind == "round":
            _half_l = max(_half_l, abs(_bb[0]), abs(_bb[1]))
            _half_w = max(_half_w, abs(_bb[2]), abs(_bb[3]))
        return (2.0 * (_half_l + margin), 2.0 * (_half_w + margin),
                spec.thickness + max(_top, 0.0))
    if spec.base_kind == "round":
        d = spec.diameter
        # 平圆件保留 0.5 端面余量；有圆顶时余量会在圆顶之上永远切不到。
        _extra = 0.5 if _dh <= 1e-9 else 0.0
        return (d + 2 * margin, d + 2 * margin, spec.thickness + _dh + _extra)
    return (spec.length + 2 * margin, spec.width + 2 * margin, spec.thickness + _dh)


def extrusion_depth(spec: PartSpec, f: Feature) -> float:
    return feature_depth(spec, f)
