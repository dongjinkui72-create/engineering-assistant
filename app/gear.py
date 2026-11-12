"""Involute spur gear profile generator (ASCII-only source)."""
from __future__ import annotations

import math
from typing import List, Tuple

Pt = Tuple[float, float]


def _inv(a: float) -> float:
    return math.tan(a) - a


def gear_profile(module: float, teeth: int, pressure_angle: float = 20.0,
                 addendum: float = 1.0, dedendum: float = 1.25,
                 flank_pts: int = 5, backlash: float = 0.0) -> List[Pt]:
    """Closed involute outline, counter-clockwise, centred at origin (mm)."""
    z = int(teeth)
    m = float(module)
    pa = math.radians(pressure_angle)
    rp = m * z / 2.0
    rb = rp * math.cos(pa)
    ra = rp + addendum * m
    rd = max(rp - dedendum * m, 0.15 * rp)

    inv_a = _inv(pa)
    tooth_angle = 2.0 * math.pi / z
    half_tooth = math.pi / (2.0 * z)      # half tooth thickness angle at pitch circle

    def half_angle_at(r: float) -> float:
        """Half angular width of the tooth at radius r."""
        if r <= rb:
            r_use = rb
            t = 0.0
        else:
            r_use = r
            t = math.sqrt(max((r_use / rb) ** 2 - 1.0, 0.0))
        inv_r = t - math.atan(t)
        a = half_tooth + inv_a - inv_r
        if r < rb:                                  # radial drop below base circle
            a += 0.0
        return a + backlash / (2.0 * rp) * 0.5

    pts: List[Pt] = []
    r_root_start = max(rd, rb * 0.98)
    for i in range(z):
        centre = i * tooth_angle
        # right flank: root -> tip
        radii = [rd] + [r_root_start + (ra - r_root_start) * k / (flank_pts + 1)
                        for k in range(flank_pts + 1)] + [ra]
        radii = sorted(set(round(r, 6) for r in radii))
        for r in radii:
            a = centre - half_angle_at(r)
            pts.append((r * math.cos(a), r * math.sin(a)))
        # tip arc
        a_tip = half_angle_at(ra)
        for k in range(1, 3):
            a = centre - a_tip + 2 * a_tip * k / 3.0
            pts.append((ra * math.cos(a), ra * math.sin(a)))
        # left flank: tip -> root
        for r in reversed(radii):
            a = centre + half_angle_at(r)
            pts.append((r * math.cos(a), r * math.sin(a)))
        # root arc to next tooth
        a0 = centre + half_angle_at(rd)
        a1 = centre + tooth_angle - half_angle_at(rd)
        for k in range(1, 3):
            a = a0 + (a1 - a0) * k / 3.0
            pts.append((rd * math.cos(a), rd * math.sin(a)))
    return pts


def polygon_area(pts: List[Pt]) -> float:
    a = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0


def gear_volume(module: float, teeth: int, face_width: float, bore: float = 0.0,
                keyway_w: float = 0.0, keyway_d: float = 0.0, **kw) -> float:
    pts = gear_profile(module, teeth, **kw)
    area = polygon_area(pts)
    v = area * face_width
    if bore > 0:
        v -= math.pi * (bore / 2.0) ** 2 * face_width
        if keyway_w > 0 and keyway_d > 0:
            # keyway = rectangle from bore wall outward, depth measured from bore surface
            h = keyway_d + (bore / 2.0 - math.sqrt(max((bore / 2.0) ** 2 - (keyway_w / 2.0) ** 2, 0.0)))
            v -= keyway_w * h * face_width * 0.85
    return max(v, 0.0)


def pitch_diameter(module: float, teeth: int) -> float:
    return module * teeth


def outer_diameter(module: float, teeth: int, addendum: float = 1.0) -> float:
    return module * (teeth + 2 * addendum)