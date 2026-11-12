# -*- coding: utf-8 -*-
"""回转母线零件的车削程序：外圆 G71/G70 + 内孔镗削 G71/G70。

母线只允许原生直线/圆弧。程序里圆弧输出 G02/G03 + R，不做点列拟合。
坐标系：X = 直径（2r），Z = 负的高度（z=0 为右端面）。
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Dict, List, Tuple

from .turning import TurnOptions, TurnResult

_EPS = 1e-6


def _pair(q) -> Tuple[float, float]:
    return float(q[0]), float(q[1])


def _seg_points(seg: dict, n: int = 64) -> List[Tuple[float, float]]:
    """把一段母线采样成 (r, z) 点，仅用于包络分类/体积积分，不用于建几何。"""
    a = _pair(seg.get("p1") or (0.0, 0.0))
    b = _pair(seg.get("p2") or (0.0, 0.0))
    if str(seg.get("type")) != "arc":
        return [(a[0] + (b[0] - a[0]) * i / n,
                 a[1] + (b[1] - a[1]) * i / n) for i in range(n + 1)]
    c = _pair(seg.get("center") or (0.0, 0.0))
    rad = math.hypot(a[0] - c[0], a[1] - c[1])
    t1 = math.atan2(a[1] - c[1], a[0] - c[0])
    t2 = math.atan2(b[1] - c[1], b[0] - c[0])
    d = (t2 - t1) % (2.0 * math.pi)
    if int(seg.get("direction") or 1) < 0:
        d = -((t1 - t2) % (2.0 * math.pi))
    out = []
    for i in range(n + 1):
        t = t1 + d * i / n
        out.append((c[0] + rad * math.cos(t), c[1] + rad * math.sin(t)))
    return out


def _swap(seg: dict) -> dict:
    q = dict(seg)
    q["p1"], q["p2"] = seg.get("p2"), seg.get("p1")
    if str(seg.get("type")) == "arc":
        q["direction"] = -int(seg.get("direction") or 1)
    return q


def _zinc(seg: dict) -> Tuple[dict, bool]:
    a = _pair(seg.get("p1") or (0.0, 0.0))
    b = _pair(seg.get("p2") or (0.0, 0.0))
    if a[1] > b[1] + _EPS:
        return _swap(seg), True
    return dict(seg), False


def _seg_intersections(seg: dict, z: float) -> List[float]:
    """Horizontal-line intersections with one profile segment.

    Only used for topology classification.  The returned radii are exact for
    lines and circle arcs; the arc angular window honours ``direction``.
    """
    a = _pair(seg.get("p1") or (0.0, 0.0))
    b = _pair(seg.get("p2") or (0.0, 0.0))
    if str(seg.get("type")) != "arc":
        if abs(a[1] - b[1]) <= _EPS:
            return []
        lo, hi = min(a[1], b[1]), max(a[1], b[1])
        if z < lo - _EPS or z > hi + _EPS:
            return []
        t = (z - a[1]) / (b[1] - a[1])
        return [a[0] + (b[0] - a[0]) * t]

    c = _pair(seg.get("center") or (0.0, 0.0))
    rad = math.hypot(a[0] - c[0], a[1] - c[1])
    dz = z - c[1]
    if abs(dz) > rad + 1e-7:
        return []
    dx = math.sqrt(max(rad * rad - dz * dz, 0.0))
    t1 = math.atan2(a[1] - c[1], a[0] - c[0])
    t2 = math.atan2(b[1] - c[1], b[0] - c[0])
    sweep = (t2 - t1) % (2.0 * math.pi)
    if int(seg.get("direction") or 1) < 0:
        sweep = -((t1 - t2) % (2.0 * math.pi))
    out: List[float] = []
    for r in (c[0] - dx, c[0] + dx):
        ang = math.atan2(dz, r - c[0])
        if sweep >= 0.0:
            on_arc = ((ang - t1) % (2.0 * math.pi)) <= sweep + 1e-7
        else:
            on_arc = ((t1 - ang) % (2.0 * math.pi)) <= (-sweep) + 1e-7
        if on_arc:
            out.append(r)
    return out


def _classify(profile: List[dict]) -> Tuple[List[dict], List[dict], List[dict]]:
    """Split a closed (r,z) profile into outer skin / bore wall / end faces.

    A local radial-maximum test fails on curved outer profiles: the midpoint of
    a quarter-arc is not the maximum radius in a narrow z-band, so a valid
    outer arc can be dropped entirely (and then no G02/G03 is emitted).  Test
    each candidate against the exact cross-section at the SAME z instead:
    the largest positive intersection is the outer skin, the smallest is the
    bore wall.
    """
    outer, inner, faces = [], [], []
    for seg in profile:
        a = _pair(seg.get("p1") or (0.0, 0.0))
        b = _pair(seg.get("p2") or (0.0, 0.0))
        if abs(a[1] - b[1]) <= _EPS:
            faces.append(dict(seg))
            continue

        pts = _seg_points(seg, 32)
        votes_o = votes_i = 0
        for frac in (0.25, 0.5, 0.75):
            q = pts[min(len(pts) - 1, max(0, int(round(frac * (len(pts) - 1)))))]
            rs: List[float] = []
            for other in profile:
                rs.extend(_seg_intersections(other, q[1]))
            if not rs:
                continue
            rmax, rmin = max(rs), min(rs)
            if rmax > 0.3 and abs(q[0] - rmax) <= 0.2:
                votes_o += 1
            if rmin > 0.3 and abs(q[0] - rmin) <= 0.2:
                votes_i += 1
        if votes_o and votes_o >= votes_i:
            outer.append(dict(seg))
        elif votes_i:
            inner.append(dict(seg))
    return outer, inner, faces

def _nc_move(seg: dict, reversed_flag: bool = False) -> str:
    a = _pair(seg.get("p1") or (0.0, 0.0))
    b = _pair(seg.get("p2") or (0.0, 0.0))
    if str(seg.get("type")) != "arc":
        return "G01 X%.3f Z%.3f" % (2.0 * b[0], -b[1])
    c = _pair(seg.get("center") or (0.0, 0.0))
    rad = math.hypot(a[0] - c[0], a[1] - c[1])
    t1 = math.atan2(a[1] - c[1], a[0] - c[0])
    t2 = math.atan2(b[1] - c[1], b[0] - c[0])
    d = (t2 - t1) % (2.0 * math.pi)
    if int(seg.get("direction") or 1) < 0:
        d = -((t1 - t2) % (2.0 * math.pi))
    sweep = abs(d)
    if sweep > math.pi + 1e-9:
        rad = -rad
    # (r,z) -> (X,Z)=(2r,-z) is a reflection, so a CCW arc in (r,z)
    # becomes CW in the X/Z plane.  ``_swap`` already flips ``direction``;
    # applying ``reversed_flag`` here as well would cancel that flip.
    cw = int(seg.get("direction") or 1) >= 0
    return "%s X%.3f Z%.3f R%.3f" % ("G02" if cw else "G03", 2.0 * b[0], -b[1], rad)


def _validate_profile(profile: List[dict], tol: float = 1e-3) -> None:
    """Reject profiles that cannot be turned, before any NC is written.

    Two classes of bad input used to slip through and silently produce a
    program that no longer matches the drawing:

    * an open loop (p2 of one segment != p1 of the next) - the tool would
      cut material that is not in the profile;
    * an arc whose two endpoints sit at different radii from its stated
      centre - ``R`` in G02/G03 cannot represent it, so the emitted arc ends
      up somewhere else entirely.
    """
    n = len(profile)
    for i, seg in enumerate(profile):
        t = str(seg.get("type") or "line")
        if t not in ("line", "arc"):
            raise ValueError("母线第 %d 段类型不支持：%s（只允许 line/arc）" % (i + 1, t))
        a = _pair(seg.get("p1") or (0.0, 0.0))
        b = _pair(seg.get("p2") or (0.0, 0.0))
        if math.hypot(a[0], a[1]) < tol and math.hypot(b[0], b[1]) < tol:
            raise ValueError("母线第 %d 段退化为一个点" % (i + 1))
        if t == "arc":
            c = _pair(seg.get("center") or (0.0, 0.0))
            r1 = math.hypot(a[0] - c[0], a[1] - c[1])
            r2 = math.hypot(b[0] - c[0], b[1] - c[1])
            if r1 < 1e-6 or r2 < 1e-6:
                raise ValueError("母线第 %d 段圆弧半径为零" % (i + 1))
            if abs(r1 - r2) > max(tol, 1e-3 * max(r1, r2)):
                raise ValueError(
                    "母线第 %d 段圆弧不合法：起点半径 %.3f、终点半径 %.3f 必须以同一圆心等距"
                    "（G02/G03 的 R 无法表达这种输入）" % (i + 1, r1, r2))
        nxt = profile[(i + 1) % n]
        nb = _pair(nxt.get("p1") or (0.0, 0.0))
        if math.hypot(b[0] - nb[0], b[1] - nb[1]) > tol:
            raise ValueError(
                "母线不闭合：第 %d 段终点 (%.3f, %.3f) 与第 %d 段起点 (%.3f, %.3f) 不重合"
                % (i + 1, b[0], b[1], (i + 1) % n + 1, nb[0], nb[1]))


def _outer_block(profile: List[dict]) -> Tuple[List[str], float, float]:
    outer, _inner, faces = _classify(profile)
    if not outer:
        raise ValueError("找不到外轮廓段")
    items = []
    for seg in outer:
        s2, rev = _zinc(seg)
        a = _pair(s2.get("p1") or (0.0, 0.0))
        b = _pair(s2.get("p2") or (0.0, 0.0))
        items.append((a[1], b[1], s2, rev))
    items.sort(key=lambda t: t[0])
    z0 = items[0][0]
    r0 = _pair(items[0][2].get("p1") or (0.0, 0.0))[0]
    lines = ["N10 G00 X0.0 Z%.3f" % (-z0), "G01 X%.3f Z%.3f F0.15" % (2.0 * r0, -z0)]
    last = (r0, z0)
    for _z1, _z2, seg, rev in items:
        a = _pair(seg.get("p1") or (0.0, 0.0))
        if abs(a[0] - last[0]) > 0.05 or abs(a[1] - last[1]) > 0.05:
            lines.append("G01 X%.3f Z%.3f" % (2.0 * a[0], -a[1]))
        lines.append(_nc_move(seg, rev))
        last = _pair(seg.get("p2") or (0.0, 0.0))
    z_face = last[1]
    # Blank diameter must cover the largest outer radius and blank length the
    # full axial extent, not merely the last (right-end) contour point.
    # Curved/stepped shafts can be widest/longest in the middle, and a profile
    # drawn from right face towards the left often ends at Z~0 while the real
    # blank runs to Z=-L.
    pts = [q for seg in outer for q in _seg_points(seg, 64)]
    r_out = max((q[0] for q in pts), default=last[0])
    blank_len = max((abs(q[1]) for q in pts), default=abs(z_face))
    inner_top = 0.0
    for f in faces:
        fa = _pair(f.get("p1") or (0.0, 0.0))
        fb = _pair(f.get("p2") or (0.0, 0.0))
        if abs(fa[1] - z_face) <= 0.05 and abs(fb[1] - z_face) <= 0.05:
            cand = min(fa[0], fb[0])
            if 0.0 <= cand < r_out - 0.05:
                inner_top = max(inner_top, cand)
    lines.append("N20 G01 X%.3f Z%.3f" % (2.0 * inner_top, -z_face))
    return lines, blank_len, r_out


def _inner_block(profile: List[dict], zmax_outer: float) -> Tuple[List[str], float, float]:
    _outer, inner, faces = _classify(profile)
    if not inner:
        return [], 0.0, 0.0
    items = []
    for seg in inner:
        s2, rev = _zinc(seg)
        a = _pair(s2.get("p1") or (0.0, 0.0))
        b = _pair(s2.get("p2") or (0.0, 0.0))
        items.append((a[1], b[1], s2, rev))
    items.sort(key=lambda t: t[0])
    z_bot = items[0][0]
    r_bot = _pair(items[0][2].get("p1") or (0.0, 0.0))[0]
    bottom_face = None
    for f in faces:
        fa = _pair(f.get("p1") or (0.0, 0.0))
        fb = _pair(f.get("p2") or (0.0, 0.0))
        if abs(fa[1] - z_bot) <= 0.05 and abs(fb[1] - z_bot) <= 0.05:
            if min(fa[0], fb[0]) <= 0.2 and max(fa[0], fb[0]) >= r_bot - 0.2:
                bottom_face = f
                break
    if bottom_face is not None:
        lines = ["N30 G00 X0.0 Z%.3f" % (-z_bot),
                 "G01 X%.3f Z%.3f F0.15" % (2.0 * r_bot, -z_bot)]
    else:
        lines = ["N30 G00 X%.3f Z%.3f" % (2.0 * r_bot, -z_bot)]
    last = (r_bot, z_bot)
    for _z1, _z2, seg, rev in items:
        a = _pair(seg.get("p1") or (0.0, 0.0))
        if abs(a[0] - last[0]) > 0.05 or abs(a[1] - last[1]) > 0.05:
            lines.append("G01 X%.3f Z%.3f" % (2.0 * a[0], -a[1]))
        lines.append(_nc_move(seg, rev))
        last = _pair(seg.get("p2") or (0.0, 0.0))
    lines.append("N40 G01 X%.3f Z%.3f" % (2.0 * last[0], -last[1]))
    return lines, z_bot, r_bot


def generate_revolve_turning(spec, out_dir: str, opts: TurnOptions | None = None,
                             post: str = "GENERIC") -> TurnResult:
    o = opts or TurnOptions()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    res = TurnResult()
    prof = [q for q in (getattr(spec, "revolve_profile", None) or []) if isinstance(q, dict)]
    if len(prof) < 3:
        res.message = "不是回转母线零件"
        return res
    try:
        _validate_profile(prof)
        outer_lines, blank_len, r_out = _outer_block(prof)
        inner_lines, z_bot, r_bot = _inner_block(prof, blank_len)
    except Exception as e:
        res.message = "母线转车削失败：%s" % e
        return res

    blank_d = 2.0 * r_out + 4.0
    blank_l = blank_len + 4.0
    L: List[str] = []
    L.append("%")
    L.append("O%04d (REVOLVE %s)" % (o.program_no, spec.part_name))
    L.append("(BLANK D%.1f x %.1f   MATERIAL %s)" % (blank_d, blank_l, spec.material))
    L.append("(POST: %s / FANUC G71+G70, native LINE/ARC)" % post)
    L.append("G21 G40 G99 G90")
    L.append("G54")
    L.append("")
    L.append("(T0101  外圆粗车刀 R0.8)")
    L.append("T0101")
    L.append("S%d M03" % o.spindle)
    L.append("M08")
    L.append("G00 X%.1f Z3.0" % (blank_d + 2.0))
    L.append("(--- 端面 ---)")
    L.append("G01 Z0 F0.2")
    L.append("G01 X-1.0 F0.15")
    L.append("G00 X%.1f Z2.0" % (blank_d + 2.0))
    L.append("")
    L.append("(--- 外圆 G71 粗车 / G70 精车 ---)")
    L.append("G71 U%g R0.5" % o.rough_depth)
    L.append("G71 P10 Q20 U%g W%g F%g" % (o.finish_allow_x, o.finish_allow_z, o.rough_feed))
    L.extend(outer_lines)
    L.append("G70 P10 Q20 F%g" % o.finish_feed)
    L.append("")
    ops = ["端面车削", "外圆 G71 粗车", "外圆 G70 精车"]
    if inner_lines:
        L.append("(T0202  内孔镗刀 R0.4)")
        L.append("T0202")
        L.append("S%d M03" % (o.spindle + 100))
        L.append("G00 X%.3f Z2.0" % max(2.0 * r_bot - 2.0, 1.0))
        L.append("G71 U%g R0.5" % o.rough_depth)
        L.append("G71 P30 Q40 U-%g W%g F%g" % (o.finish_allow_x, o.finish_allow_z, o.rough_feed))
        L.extend(inner_lines)
        L.append("G70 P30 Q40 F%g" % o.finish_feed)
        ops.extend(["内孔 G71 粗镗", "内孔 G70 精镗"])
    L.append("")
    L.append("G00 X%.1f Z%.1f" % (blank_d + 20.0, blank_l + 20.0))
    L.append("M09")
    L.append("M05")
    L.append("M30")
    L.append("%")

    nc = out / ("%s_车削.nc" % spec.file_stem)
    nc.write_text("\n".join(L) + "\n", encoding="utf-8")
    res.nc_path = str(nc)
    res.lines = len(L)
    res.ops = ops
    res.minutes = (blank_len + blank_d / 2.0) / 90.0
    res.ok = True
    res.message = "回转母线车削程序生成完成"
    rep = out / ("%s_车削说明.md" % spec.file_stem)
    rep.write_text(
        "# %s · 回转母线车削\n\n"
        "- 毛坯：Ø%.1f × %.1f mm\n"
        "- 外轮廓：G71 P10 Q20 + G70 精车（原生直线/圆弧）\n"
        "- 内孔：%s\n"
        "- 最大外径 Ø%.1f，总长 %.1f mm\n"
        "- 圆弧输出 G02/G03 + R，不使用点列拟合\n"
        % (spec.part_name, blank_d, blank_l,
           "G71 P30 Q40 + G70 精镗" if inner_lines else "无内孔",
           2.0 * r_out, blank_len),
        encoding="utf-8")
    res.report_path = str(rep)
    return res


# ------------------------------------------------------------------ NC 反算
_WORD = re.compile(r"([XZR])([+-]?(?:\d+\.?\d*|\.\d+))")


def parse_nc_contours(text: str) -> Dict[str, List[dict]]:
    """解析 N10..N20 / N30..N40 的 P/Q 轮廓，返回 {outer: [...], inner: [...]}。"""
    out: Dict[str, List[dict]] = {"outer": [], "inner": []}
    cur = None
    prev = None
    mode = "G00"
    for raw in text.splitlines():
        line = raw.split("(")[0].strip()
        if not line:
            continue
        if re.search(r"\bN10\b", line):
            cur, prev = "outer", None
        elif re.search(r"\bN30\b", line):
            cur, prev = "inner", None
        for g in ("G00", "G01", "G02", "G03"):
            if g in line:
                mode = g
                break
        words = dict((k, float(v)) for k, v in _WORD.findall(line))
        if cur and ("X" in words or "Z" in words):
            x = words.get("X", prev[0] if prev else 0.0)
            z = words.get("Z", prev[1] if prev else 0.0)
            if prev is not None and mode != "G00":
                out[cur].append({"mode": mode, "x1": prev[0], "z1": prev[1],
                                 "x2": x, "z2": z, "r": words.get("R")})
            prev = (x, z)
        if cur == "outer" and re.search(r"\bN20\b", line):
            cur, prev = None, None
        elif cur == "inner" and re.search(r"\bN40\b", line):
            cur, prev = None, None
    return out


def _arc_points(x1, z1, x2, z2, rad, cw, n=200):
    dx, dz = x2 - x1, z2 - z1
    d = math.hypot(dx, dz)
    if d < 1e-9:
        return [(x1, z1)]
    rr = abs(rad)
    if d > 2.0 * rr + 1e-6:
        return [(x1 + dx * i / n, z1 + dz * i / n) for i in range(n + 1)]
    mx, mz = (x1 + x2) / 2.0, (z1 + z2) / 2.0
    h = math.sqrt(max(rr * rr - (d / 2.0) ** 2, 0.0))
    ux, uz = -dz / d, dx / d
    cands = [(mx + ux * h, mz + uz * h), (mx - ux * h, mz - uz * h)]
    best = None
    for cx, cz in cands:
        cross = (x1 - cx) * (z2 - cz) - (z1 - cz) * (x2 - cx)
        if (cw and cross < 0) or ((not cw) and cross > 0):
            best = (cx, cz)
            break
    cx, cz = best or cands[0]
    a1 = math.atan2(z1 - cz, x1 - cx)
    a2 = math.atan2(z2 - cz, x2 - cx)
    da = math.atan2(math.sin(a2 - a1), math.cos(a2 - a1))
    if cw and da > 0:
        da -= 2.0 * math.pi
    elif (not cw) and da < 0:
        da += 2.0 * math.pi
    return [(cx + rr * math.cos(a1 + da * i / n),
             cz + rr * math.sin(a1 + da * i / n)) for i in range(n + 1)]


def contour_volume(segs: List[dict]) -> float:
    """按母线反算回转体积。X 是直径；圆弧插补在物理半径坐标里算。"""
    vol = 0.0
    for s in segs:
        if s.get("mode") in ("G02", "G03") and s.get("r") is not None:
            pts = _arc_points(s["x1"] / 2.0, s["z1"], s["x2"] / 2.0, s["z2"],
                              s["r"], s["mode"] == "G02")
        else:
            pts = [(s["x1"] / 2.0, s["z1"]), (s["x2"] / 2.0, s["z2"])]
        for (r1, z1), (r2, z2) in zip(pts, pts[1:]):
            vol += math.pi * (z2 - z1) * (r1 * r1 + r1 * r2 + r2 * r2) / 3.0
    return abs(vol)