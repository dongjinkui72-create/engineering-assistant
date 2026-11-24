"""Shared 2D geometry helpers.

Segments are plain tuples so every CAD adapter can translate them into its own
sketch API:

    ("line", x1, y1, x2, y2)
    ("arc",  cx, cy, r, a1_deg, a2_deg)      # always counter-clockwise a1 -> a2
    ("circle", cx, cy, r)
"""
from __future__ import annotations

import math
from typing import List, Tuple

Seg = Tuple


def rect_outline(l: float, w: float, r: float = 0.0, cx: float = 0.0, cy: float = 0.0) -> List[Seg]:
    """Rectangle centred on (cx, cy); r > 0 rounds the four corners."""
    x0, x1 = cx - l / 2.0, cx + l / 2.0
    y0, y1 = cy - w / 2.0, cy + w / 2.0
    if r <= 1e-9:
        return [("line", x0, y0, x1, y0), ("line", x1, y0, x1, y1),
                ("line", x1, y1, x0, y1), ("line", x0, y1, x0, y0)]
    r = min(r, min(l, w) / 2.0 - 1e-6)
    segs: List[Seg] = [
        ("line", x0 + r, y0, x1 - r, y0),
        ("arc", x1 - r, y0 + r, r, 270.0, 360.0),
        ("line", x1, y0 + r, x1, y1 - r),
        ("arc", x1 - r, y1 - r, r, 0.0, 90.0),
        ("line", x1 - r, y1, x0 + r, y1),
        ("arc", x0 + r, y1 - r, r, 90.0, 180.0),
        ("line", x0, y1 - r, x0, y0 + r),
        ("arc", x0 + r, y0 + r, r, 180.0, 270.0),
    ]
    return segs


def circle(cx: float, cy: float, r: float) -> List[Seg]:
    return [("circle", cx, cy, r)]


def slot_outline(length: float, width: float, cx: float = 0.0, cy: float = 0.0,
                 angle_deg: float = 0.0) -> List[Seg]:
    """Stadium shape: overall length x width, semicircular ends."""
    r = width / 2.0
    half = max(0.0, (length - width) / 2.0)
    base: List[Seg] = [
        ("line", -half, -r, half, -r),
        ("arc", half, 0.0, r, 270.0, 450.0),
        ("line", half, r, -half, r),
        ("arc", -half, 0.0, r, 90.0, 270.0),
    ]
    if abs(angle_deg) < 1e-9 and abs(cx) < 1e-12 and abs(cy) < 1e-12:
        return base
    return rotate_translate(base, angle_deg, cx, cy)


def rotate_translate(segs: List[Seg], angle_deg: float, dx: float, dy: float) -> List[Seg]:
    a = math.radians(angle_deg)
    ca, sa = math.cos(a), math.sin(a)

    def p(x, y):
        return (x * ca - y * sa + dx, x * sa + y * ca + dy)

    out: List[Seg] = []
    for s in segs:
        k = s[0]
        if k == "line":
            x1, y1 = p(s[1], s[2])
            x2, y2 = p(s[3], s[4])
            out.append(("line", x1, y1, x2, y2))
        elif k == "arc":
            cxx, cyy = p(s[1], s[2])
            out.append(("arc", cxx, cyy, s[3], s[4] + angle_deg, s[5] + angle_deg))
        elif k == "circle":
            cxx, cyy = p(s[1], s[2])
            out.append(("circle", cxx, cyy, s[3]))
    return out


def seg_length(s: Seg) -> float:
    if s[0] == "line":
        return math.hypot(s[3] - s[1], s[4] - s[2])
    if s[0] == "arc":
        return abs(math.radians(s[5] - s[4])) * s[3]
    if s[0] == "circle":
        return 2 * math.pi * s[3]
    return 0.0


def seg_points(s: Seg, step_deg: float = 15.0):
    """Sample a segment into points (used by G-code + SVG preview)."""
    if s[0] == "line":
        return [(s[1], s[2]), (s[3], s[4])]
    if s[0] == "arc":
        cx, cy, r, a1, a2 = s[1], s[2], s[3], s[4], s[5]
        n = max(2, int(abs(a2 - a1) / step_deg) + 1)
        pts = []
        for i in range(n + 1):
            a = math.radians(a1 + (a2 - a1) * i / n)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        return pts
    if s[0] == "circle":
        cx, cy, r = s[1], s[2], s[3]
        return [(cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a)))
                for a in range(0, 361, 10)]
    return []


def outline_area(segs: List[Seg]) -> float:
    """Signed area via polygon sampling (sufficient for stock/feature area)."""
    pts: List[Tuple[float, float]] = []
    for s in segs:
        p = seg_points(s, 5.0)
        if pts and p and abs(pts[-1][0] - p[0][0]) < 1e-9 and abs(pts[-1][1] - p[0][1]) < 1e-9:
            pts.extend(p[1:])
        else:
            pts.extend(p)
    if len(pts) < 3:
        return 0.0
    a = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0

def flatten(segs: List[Seg], arc_step_deg: float = 10.0) -> List[List[Tuple[float, float]]]:
    """Convert a closed segment loop into one closed polyline (arcs sampled)."""
    pts: List[Tuple[float, float]] = []
    for s in segs:
        if s[0] == "circle":
            cx, cy, r = s[1], s[2], s[3]
            loop = [(cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a)))
                    for a in range(0, 360, 5)]
            return [loop]
        p = seg_points(s, arc_step_deg)
        if not p:
            continue
        if pts and abs(pts[-1][0] - p[0][0]) < 1e-7 and abs(pts[-1][1] - p[0][1]) < 1e-7:
            p = p[1:]
        pts.extend(p)
    if len(pts) > 2 and abs(pts[0][0] - pts[-1][0]) < 1e-7 and abs(pts[0][1] - pts[-1][1]) < 1e-7:
        pts.pop()
    return [pts]

# ---------------------------------------------------------------- profiles
# 通用闭合二维轮廓（p19/p20 这类异形棱柱）。
# 数据格式（绝对 mm 坐标）：
#   {"type":"line","p1":[x,y],"p2":[x,y]}
#   {"type":"arc","p1":[x,y],"p2":[x,y],"center":[cx,cy],"dir":1}
# dir=+1 表示从 p1 沿逆时针到 p2，-1 表示顺时针；缺省取劣弧。
# 也接受 {"type":"arc","center":[cx,cy],"r":r,"a0":度,"a1":度}。
# 约束：只用直线 + 真圆弧，绝不用点列拟合（点列会把圆弧变成多段弦）。


def _pt2(v):
    if isinstance(v, dict):
        if "x" in v or "y" in v:
            return (float(v.get("x") or 0.0), float(v.get("y") or 0.0))
        return None
    if isinstance(v, (list, tuple)) and len(v) >= 2:
        try:
            return (float(v[0]), float(v[1]))
        except Exception:
            return None
    return None


def norm_profile(raw) -> List[dict]:
    """把轮廓段统一成 line / arc 两种，非法段直接丢掉。"""
    out: List[dict] = []
    if not raw:
        return out
    for sg in raw:
        if not isinstance(sg, dict):
            continue
        t = str(sg.get("type") or sg.get("kind") or sg.get("op") or "").strip().lower()
        p1, p2 = _pt2(sg.get("p1")), _pt2(sg.get("p2"))
        c = _pt2(sg.get("center"))
        if t in ("line", "ln", "直线", "seg") or (not t and p1 and p2 and not c):
            if p1 and p2:
                if abs(p1[0] - p2[0]) < 1e-9 and abs(p1[1] - p2[1]) < 1e-9:
                    continue
                out.append({"type": "line", "p1": [round(p1[0], 6), round(p1[1], 6)],
                            "p2": [round(p2[0], 6), round(p2[1], 6)]})
            continue
        # arc
        if c and p1 and p2:
            r = math.hypot(p1[0] - c[0], p1[1] - c[1])
            if r <= 1e-9:
                continue
            # p2 不在圆上时沿径向吸附到圆上，避免生成开口轮廓
            d2 = math.hypot(p2[0] - c[0], p2[1] - c[1])
            if d2 > 1e-9 and abs(d2 - r) > 1e-6:
                p2 = (c[0] + (p2[0] - c[0]) * r / d2, c[1] + (p2[1] - c[1]) * r / d2)
            d = sg.get("dir", sg.get("direction", sg.get("ccw")))
            if isinstance(d, str):
                d = -1.0 if d.strip().lower() in ("cw", "-1", "顺时针") else 1.0
            a0 = math.atan2(p1[1] - c[1], p1[0] - c[0])
            a1 = math.atan2(p2[1] - c[1], p2[0] - c[0])
            if d is None:
                sw = (a1 - a0 + math.pi) % (2.0 * math.pi) - math.pi
                d = 1.0 if sw >= 0 else -1.0
            out.append({"type": "arc", "p1": [round(p1[0], 6), round(p1[1], 6)],
                        "p2": [round(p2[0], 6), round(p2[1], 6)],
                        "center": [round(c[0], 6), round(c[1], 6)],
                        "dir": 1 if float(d) >= 0 else -1})
            continue
        if c and sg.get("r") is not None:
            r = float(sg.get("r") or 0.0)
            if r <= 1e-9:
                continue
            a0 = float(sg.get("a0") or sg.get("start") or 0.0)
            a1 = float(sg.get("a1") or sg.get("end") or 360.0)
            P1 = (c[0] + r * math.cos(math.radians(a0)), c[1] + r * math.sin(math.radians(a0)))
            P2 = (c[0] + r * math.cos(math.radians(a1)), c[1] + r * math.sin(math.radians(a1)))
            out.append({"type": "arc", "p1": [round(P1[0], 6), round(P1[1], 6)],
                        "p2": [round(P2[0], 6), round(P2[1], 6)],
                        "center": [round(c[0], 6), round(c[1], 6)],
                        "dir": 1 if a1 >= a0 else -1})
    return out


def segs_from_profile(profile, dx: float = 0.0, dy: float = 0.0,
                      ccw: bool = True) -> List[Seg]:
    """轮廓 dict -> geom.Seg 列表（可整体平移）。

    圆弧保留**带符号**的扫掠角：a1 < a0 即顺时针段（凹弧）。面积积分依赖方向。
    ccw=True（默认）时把闭合轮廓规范成逆时针，CAD 草图不会画出反向大弧。
    """
    segs: List[Seg] = []
    for sg in norm_profile(profile):
        if sg["type"] == "line":
            segs.append(("line", sg["p1"][0] + dx, sg["p1"][1] + dy,
                         sg["p2"][0] + dx, sg["p2"][1] + dy))
            continue
        cx, cy = sg["center"][0] + dx, sg["center"][1] + dy
        x1, y1 = sg["p1"][0] + dx, sg["p1"][1] + dy
        x2, y2 = sg["p2"][0] + dx, sg["p2"][1] + dy
        r = math.hypot(x1 - cx, y1 - cy)
        if r <= 1e-9:
            continue
        a0 = math.degrees(math.atan2(y1 - cy, x1 - cx))
        a1 = math.degrees(math.atan2(y2 - cy, x2 - cx))
        d = 1 if int(sg.get("dir", 1)) >= 0 else -1
        if d > 0:
            while a1 <= a0 + 1e-9:
                a1 += 360.0
        else:
            while a1 >= a0 - 1e-9:
                a1 -= 360.0
        segs.append(("arc", cx, cy, r, a0, a1))
    return segs_ccw(segs) if ccw else segs


def reverse_segs(segs: List[Seg]) -> List[Seg]:
    """把闭合轮廓整体反向（同一几何，遍历方向相反）。圆弧扫掠角随之取反。"""
    out: List[Seg] = []
    for s in reversed(segs):
        if s[0] == "line":
            out.append(("line", s[3], s[4], s[1], s[2]))
        elif s[0] == "arc":
            out.append(("arc", s[1], s[2], s[3], s[5], s[4]))
        elif s[0] == "circle":
            out.append(s)
    return out


def segs_ccw(segs: List[Seg]) -> List[Seg]:
    """保证闭合轮廓按逆时针遍历（面积为正）。已是逆时针时原样返回。"""
    if len(segs) < 2:
        return list(segs)
    try:
        if outline_area_signed(segs) < 0.0:
            return reverse_segs(segs)
    except Exception:
        pass
    return list(segs)


def segs_perimeter(segs: List[Seg]) -> float:
    """轮廓周长：直线段长 + 圆弧解析弧长。"""
    tot = 0.0
    for s in segs:
        if s[0] == "line":
            tot += math.hypot(s[3] - s[1], s[4] - s[2])
        elif s[0] == "arc":
            tot += abs(math.radians(s[5] - s[4])) * abs(s[3])
        elif s[0] == "circle":
            tot += 2.0 * math.pi * abs(s[3])
    return tot


def seg_midpoint(s: Seg):
    """线段中点 / 圆弧上扫掠角中点（按带符号扫掠角），供三点式 CreateArc 使用。"""
    if s[0] == "line":
        return ((s[1] + s[3]) / 2.0, (s[2] + s[4]) / 2.0)
    if s[0] == "arc":
        _, cx, cy, r, a0, a1 = s[:6]
        am = math.radians((a0 + a1) / 2.0)
        return (cx + r * math.cos(am), cy + r * math.sin(am))
    if s[0] == "circle":
        return (s[1] + s[3], s[2])
    return (0.0, 0.0)


def seg_endpoints(s: Seg):
    if s[0] == "line":
        return (s[1], s[2]), (s[3], s[4])
    if s[0] == "arc":
        _, cx, cy, r, a0, a1 = s[:6]
        return ((cx + r * math.cos(math.radians(a0)), cy + r * math.sin(math.radians(a0))),
                (cx + r * math.cos(math.radians(a1)), cy + r * math.sin(math.radians(a1))))
    if s[0] == "circle":
        return (s[1] + s[3], s[2]), (s[1] + s[3], s[2])
    return (0.0, 0.0), (0.0, 0.0)


def outline_area_signed(segs: List[Seg]) -> float:
    """闭合轮廓的**带符号**面积（逆时针为正），Green 公式 + 圆弧解析积分。"""
    tot = 0.0
    for s in segs:
        if s[0] == "line":
            tot += s[1] * s[4] - s[3] * s[2]
        elif s[0] == "arc":
            _, cx, cy, r, a0, a1 = s[:6]
            r0, r1 = math.radians(a0), math.radians(a1)
            tot += (r * cx * (math.sin(r1) - math.sin(r0))
                    - r * cy * (math.cos(r1) - math.cos(r0))
                    + r * r * (r1 - r0))
        elif s[0] == "circle":
            tot += 2.0 * math.pi * s[3] * s[3]
    return tot / 2.0


def outline_area_exact(segs: List[Seg]) -> float:
    """闭合轮廓的精确面积（取带符号面积的绝对值）。"""
    return abs(outline_area_signed(segs))


def profile_from_segs(segs: List[Seg]) -> List[dict]:
    """geom.Seg 列表 -> 可存盘的轮廓 dict（line / arc）。"""
    out: List[dict] = []
    for s in segs:
        if s[0] == "line":
            out.append({"type": "line", "p1": [round(s[1], 6), round(s[2], 6)],
                        "p2": [round(s[3], 6), round(s[4], 6)]})
        elif s[0] == "arc":
            cx, cy, r, a0, a1 = s[1], s[2], s[3], s[4], s[5]
            p1 = (cx + r * math.cos(math.radians(a0)), cy + r * math.sin(math.radians(a0)))
            p2 = (cx + r * math.cos(math.radians(a1)), cy + r * math.sin(math.radians(a1)))
            out.append({"type": "arc", "p1": [round(p1[0], 6), round(p1[1], 6)],
                        "p2": [round(p2[0], 6), round(p2[1], 6)],
                        "center": [round(cx, 6), round(cy, 6)],
                        "dir": 1 if a1 >= a0 else -1})
    return out


def segs_bbox(segs: List[Seg]):
    """(xmin, ymin, xmax, ymax)；圆弧取端点 + 四个象限极值点。"""
    xs: List[float] = []
    ys: List[float] = []
    for s in segs:
        if s[0] == "line":
            xs.extend([s[1], s[3]])
            ys.extend([s[2], s[4]])
        elif s[0] == "arc":
            _, cx, cy, r, a0, a1 = s[:6]
            for a in (a0, a1):
                xs.append(cx + r * math.cos(math.radians(a)))
                ys.append(cy + r * math.sin(math.radians(a)))
            lo, hi = (a0, a1) if a1 >= a0 else (a1, a0)
            k = math.ceil(lo / 90.0)
            while k * 90.0 <= hi:
                a = k * 90.0
                xs.append(cx + r * math.cos(math.radians(a)))
                ys.append(cy + r * math.sin(math.radians(a)))
                k += 1
        elif s[0] == "circle":
            xs.extend([s[1] - s[3], s[1] + s[3]])
            ys.extend([s[2] - s[3], s[2] + s[3]])
    if not xs:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(xs), min(ys), max(xs), max(ys))


def profile_closed(profile, tol: float = 1e-3) -> bool:
    """首尾是否闭合（相邻段端点相接）。"""
    segs = segs_from_profile(profile)
    if len(segs) < 3:
        return False
    def ends(s):
        if s[0] == "line":
            return (s[1], s[2]), (s[3], s[4])
        _, cx, cy, r, a0, a1 = s[:6]
        return ((cx + r * math.cos(math.radians(a0)), cy + r * math.sin(math.radians(a0))),
                (cx + r * math.cos(math.radians(a1)), cy + r * math.sin(math.radians(a1))))
    for i, s in enumerate(segs):
        nxt = segs[(i + 1) % len(segs)]
        e = ends(s)[1]
        b = ends(nxt)[0]
        if math.hypot(e[0] - b[0], e[1] - b[1]) > tol:
            return False
    return True

