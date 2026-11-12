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

