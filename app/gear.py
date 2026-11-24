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


def gear_entities(module: float, teeth: int, pressure_angle: float = 20.0,
                  addendum: float = 1.0, dedendum: float = 1.25,
                  flank_pts: int = 6, backlash: float = 0.0):
    """Native-curve gear outline: one involute spline per flank, true arcs at
    tip/root. Returns [("spline", [(x,y),...]) | ("arc", (cx,cy),(x1,y1),(x2,y2))].

    CAD 侧据此用草图自带的样条与圆弧建齿形，不再用上百段直线拼接。
    """
    z = int(teeth)
    m = float(module)
    pa = math.radians(pressure_angle)
    rp = m * z / 2.0
    rb = rp * math.cos(pa)
    ra = rp + addendum * m
    rd = max(rp - dedendum * m, 0.15 * rp)
    inv_a = _inv(pa)
    tooth_angle = 2.0 * math.pi / z
    half_tooth = math.pi / (2.0 * z)

    def half_angle_at(r: float) -> float:
        r_use = rb if r <= rb else r
        t = 0.0 if r <= rb else math.sqrt(max((r_use / rb) ** 2 - 1.0, 0.0))
        inv_r = t - math.atan(t)
        return half_tooth + inv_a - inv_r + backlash / (2.0 * rp) * 0.5

    def pol(r: float, a: float):
        return (r * math.cos(a), r * math.sin(a))

    r_root_start = max(rd, rb * 0.98)
    radii = [rd] + [r_root_start + (ra - r_root_start) * k / (flank_pts + 1)
                    for k in range(flank_pts + 1)] + [ra]
    radii = sorted(set(round(r, 6) for r in radii))
    ents = []
    for i in range(z):
        centre = i * tooth_angle
        right = [pol(r, centre - half_angle_at(r)) for r in radii]
        a_tip = half_angle_at(ra)
        left = [pol(r, centre + half_angle_at(r)) for r in reversed(radii)]
        a0 = centre + half_angle_at(rd)
        a1 = centre + tooth_angle - half_angle_at(rd)
        ents.append(("spline", right))
        ents.append(("arc", (0.0, 0.0), pol(ra, centre - a_tip), pol(ra, centre + a_tip)))
        ents.append(("spline", left))
        ents.append(("arc", (0.0, 0.0), pol(rd, a0), pol(rd, a1)))
    return ents


def polygon_area(pts: List[Pt]) -> float:
    a = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0


def gear_volume(module: float, teeth: int, face_width: float, bore: float = 0.0,
                keyway_w: float = 0.0, keyway_d: float = 0.0,
                hub_dia: float = 0.0, hub_len: float = 0.0,
                bore_depth: float = 0.0, small_bore: float = 0.0,
                chamfer: float = 0.0, extra_holes=None, **kw) -> float:
    """Volume of a spur gear body (mm^3).

    hub_dia/hub_len: hub boss protruding from the base face of the gear.
    bore: big bore; bore_depth > 0 means it is a counterbore measured from the
          far (top) face, otherwise it goes through.
    small_bore: through hole of a stepped bore (Ø26J7 x 8 + Ø23 through, part23).
    """
    pts = gear_profile(module, teeth, **kw)
    area = polygon_area(pts)
    w = float(face_width)
    hub_l = max(float(hub_len or 0.0), 0.0)
    hub_d = float(hub_dia or 0.0)
    v = area * w
    total = w + hub_l
    if hub_d > 0 and hub_l > 0:
        v += math.pi * (hub_d / 2.0) ** 2 * hub_l
    d_big = total if float(bore_depth or 0.0) <= 0 else min(float(bore_depth), total)
    if small_bore > 0:
        v -= math.pi * (small_bore / 2.0) ** 2 * total
        if bore > small_bore and d_big > 0:
            v -= math.pi * ((bore / 2.0) ** 2 - (small_bore / 2.0) ** 2) * d_big
        if keyway_w > 0 and keyway_d > 0 and bore > 0:
            h = keyway_d + (bore / 2.0 - math.sqrt(
                max((bore / 2.0) ** 2 - (keyway_w / 2.0) ** 2, 0.0)))
            v -= keyway_w * h * d_big * 0.85
    elif bore > 0:
        v -= math.pi * (bore / 2.0) ** 2 * d_big
        if keyway_w > 0 and keyway_d > 0:
            h = keyway_d + (bore / 2.0 - math.sqrt(
                max((bore / 2.0) ** 2 - (keyway_w / 2.0) ** 2, 0.0)))
            v -= keyway_w * h * d_big * 0.85
    # 两端齿顶轮廓 C 倒角：每端去料 = 轮廓周长 * c^2 / 2（90 度等距倒角三角截面）。
    # 实测 part23（C0.5，齿廓周长 250.1）SW 一次倒 152 条齿廓边去料 63.49 mm3，
    # 解析值 250.1*0.25 = 62.5，差 1.5%（倒角端部的圆弧修正）。
    if chamfer and chamfer > 0:
        per = 0.0
        n = len(pts)
        for i in range(n):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % n]
            per += math.hypot(x2 - x1, y2 - y1)
        v -= per * float(chamfer) ** 2
    # ????????? 3xM5 EQS ???depth=0 ???????
    for _h in (extra_holes or []):
        try:
            _d = float(_h.get("diameter") or 0.0) if isinstance(_h, dict) else float(_h[0])
            _dep = float(_h.get("depth") or 0.0) if isinstance(_h, dict) else float(_h[1])
        except Exception:
            continue
        if _d <= 0:
            continue
        _hh = total if _dep <= 0 else min(_dep, total)
        v -= math.pi * (_d / 2.0) ** 2 * _hh
    return max(v, 0.0)


def pitch_diameter(module: float, teeth: int) -> float:
    return module * teeth


def outer_diameter(module: float, teeth: int, addendum: float = 1.0) -> float:
    return module * (teeth + 2 * addendum)