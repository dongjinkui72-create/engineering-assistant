"""2.5D CAM: generates a generic ISO/FANUC G-code program plus an SVG preview.

Strategy
--------
* profile : contour around the part outline, multi Z-pass
* pocket  : zig-zag clearing (pockets, slots, steps)
* drill   : G81/G83 per hole diameter
All coordinates are workpiece coordinates: X0Y0 = part centre, Z0 = top face.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

from ..calc import (base_outline, feature_area, feature_depth, inner_outline,
                    offset_outline, stock_size)
from ..geom import flatten, outline_area, rect_outline, slot_outline
from ..models import Feature, PartSpec

Pt = Tuple[float, float]


@dataclass
class CamOptions:
    tool_dia: float = 8.0          # end mill for profile / pockets
    pocket_tool_dia: float = 6.0
    stepover: float = 0.45         # fraction of tool diameter
    stepdown: float = 2.0          # mm per Z pass
    feed: float = 600.0            # mm/min
    plunge: float = 150.0
    spindle: float = 3000.0
    safe_z: float = 5.0
    stock_margin: float = 6.0
    coolant: bool = True
    program_no: int = 1001
    finish_pass: bool = True       # 轮廓粗铣留 0.5，再单独一刀精铣到尺寸


@dataclass
class CamResult:
    ok: bool = False
    nc_path: str = ""
    svg_path: str = ""
    report_path: str = ""
    lines: int = 0
    path_length: float = 0.0
    minutes: float = 0.0
    operations: List[str] = field(default_factory=list)
    unplanned: List[str] = field(default_factory=list)
    message: str = ""

    def to_dict(self):
        return {"ok": self.ok, "nc_path": self.nc_path, "svg_path": self.svg_path,
                "report_path": self.report_path, "lines": self.lines,
                "path_length": round(self.path_length, 1),
                "minutes": round(self.minutes, 1),
                "operations": self.operations, "unplanned": self.unplanned,
                "message": self.message}


def _shift_z(segs, dz: float):
    """把 XZ 解析段的 Z 整体平移 dz（世界坐标 ↔ CAM 坐标换算，不改几何形状）。"""
    out = []
    for sg in segs:
        if sg[0] == "line":
            out.append(("line", sg[1], sg[2] + dz, sg[3], sg[4] + dz))
        elif sg[0] == "cap":
            out.append(("cap", sg[1], sg[2] + dz, sg[3]))
        else:
            out.append(("arc", sg[1], sg[2] + dz, sg[3], sg[4], sg[5], sg[6]))
    return out


def _span_sub(spans, a: float, b: float):
    """从一维区间集合里扣掉 [a,b]，返回剩余区间（保持顺序）。"""
    out = []
    for (x0, x1) in spans:
        if b <= x0 or a >= x1:
            out.append((x0, x1))
            continue
        if a > x0:
            out.append((x0, min(a, x1)))
        if b < x1:
            out.append((max(b, x0), x1))
    return out


def _poly_area(pts: Sequence[Pt]) -> float:
    a = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        a += x1 * y2 - x2 * y1
    return a / 2.0


def offset_polygon(pts: Sequence[Pt], dist: float) -> List[Pt]:
    """Offset a convex-ish closed polygon outward (>0) / inward (<0)."""
    n = len(pts)
    sign = 1.0 if _poly_area(pts) > 0 else -1.0
    out: List[Pt] = []
    for i in range(n):
        x0, y0 = pts[(i - 1) % n]
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        v1 = (x1 - x0, y1 - y0)
        v2 = (x2 - x1, y2 - y1)
        l1 = math.hypot(*v1) or 1e-9
        l2 = math.hypot(*v2) or 1e-9
        n1 = (v1[1] / l1, -v1[0] / l1)
        n2 = (v2[1] / l2, -v2[0] / l2)
        nx, ny = n1[0] + n2[0], n1[1] + n2[1]
        ln = math.hypot(nx, ny) or 1e-9
        nx, ny = nx / ln, ny / ln
        cosh = max(0.3, nx * n1[0] + ny * n1[1])
        out.append((x1 + sign * nx * dist / cosh, y1 + sign * ny * dist / cosh))
    return out


class GCode:
    def __init__(self, opts: CamOptions):
        self.o = opts
        self.lines: List[str] = []
        self.length = 0.0
        self.op_list: List[str] = []
        self._xyz = (0.0, 0.0, 0.0)
        self.tool = 1

    def raw(self, s: str):
        self.lines.append(s)

    def move(self, x=None, y=None, z=None, f=None, rapid=False):
        px, py, pz = self._xyz
        nx = px if x is None else x
        ny = py if y is None else y
        nz = pz if z is None else z
        if rapid:
            self.length += math.dist((px, py, pz), (nx, ny, nz))
            self.raw(f"G00 X{nx:.3f} Y{ny:.3f} Z{nz:.3f}")
        else:
            self.length += math.dist((px, py, pz), (nx, ny, nz))
            fs = f" F{f:.1f}" if f else ""
            self.raw(f"G01 X{nx:.3f} Y{ny:.3f} Z{nz:.3f}{fs}")
        self._xyz = (nx, ny, nz)

    def tool_change(self, t: int, dia: float, name: str):
        self.tool = t
        self.raw("")
        self.raw(f"(T{t}  D{dia:g}  {name})")
        self.raw(f"T{t} M06")
        self.raw(f"S{self.o.spindle:.0f} M03")
        if self.o.coolant:
            self.raw("M08")

    def contour(self, poly: Sequence[Pt], z_top: float, z_bottom: float, ramps: int = 1,
                passes: int | None = None, note: str = "轮廓铣削"):
        o = self.o
        z_passes = passes or max(1, int(math.ceil((z_top - z_bottom) / o.stepdown)))
        dz = (z_top - z_bottom) / z_passes
        start = poly[0]
        self.raw(f"({note})")
        self.move(z=o.safe_z, rapid=True)
        self.move(x=start[0], y=start[1], rapid=True)
        for i in range(z_passes):
            z = z_top - dz * (i + 1)
            self.move(z=z, f=o.plunge)
            for px, py in list(poly[1:]) + [start]:
                self.move(x=px, y=py, f=o.feed)
            self.move(z=o.safe_z, rapid=True)
        self.op_list.append(f"{note} {len(poly)} 段，Z {z_top:g}→{z_bottom:g}")

    def arc_move(self, cx: float, cy: float, r: float, a0: float, a1: float,
                 ccw: bool = True, f: float | None = None):
        """输出一段原生圆弧（G02/G03 + I/J），不拆成密集点。

        a0/a1 为度数，沿 ccw 方向从 a0 转到 a1。
        """
        o = self.o
        px, py, pz = self._xyz
        sx = cx + r * math.cos(math.radians(a0))
        sy = cy + r * math.sin(math.radians(a0))
        if math.hypot(sx - px, sy - py) > 1e-6:
            self.move(x=sx, y=sy, f=o.plunge)
            px, py, pz = self._xyz
        ex = cx + r * math.cos(math.radians(a1))
        ey = cy + r * math.sin(math.radians(a1))
        i_off = cx - px
        j_off = cy - py
        sweep = abs(a1 - a0)
        if sweep <= 1e-9:
            sweep = 360.0
        self.length += r * math.radians(sweep)
        code = "G03" if ccw else "G02"
        fs = f" F{f:.1f}" if f else ""
        self.raw(f"{code} X{ex:.3f} Y{ey:.3f} I{i_off:.3f} J{j_off:.3f}{fs}")
        self._xyz = (ex, ey, pz)

    def contour_segs(self, segs: Sequence, z_top: float, z_bottom: float,
                     passes: int | None = None, note: str = "轮廓铣削"):
        """按原生几何段走轮廓：直线 G01、圆弧 G02/G03。

        CAM 侧不得把圆弧拆成一串直线点。
        """
        o = self.o
        segs = [s for s in segs if s]
        if not segs:
            return
        z_passes = passes or max(1, int(math.ceil((z_top - z_bottom) / max(o.stepdown, 1e-6))))
        dz = (z_top - z_bottom) / z_passes
        k0 = segs[0][0]
        if k0 == "line":
            start = (segs[0][1], segs[0][2])
        elif k0 == "arc":
            start = (segs[0][1] + segs[0][3] * math.cos(math.radians(segs[0][4])),
                     segs[0][2] + segs[0][3] * math.sin(math.radians(segs[0][4])))
        else:
            start = (segs[0][1] + segs[0][3], segs[0][2])
        self.raw(f"({note})")
        self.move(z=o.safe_z, rapid=True)
        self.move(x=start[0], y=start[1], rapid=True)
        for i in range(z_passes):
            z = z_top - dz * (i + 1)
            self.move(z=z, f=o.plunge)
            for sg in segs:
                k = sg[0]
                if k == "line":
                    self.move(x=sg[3], y=sg[4], f=o.feed)
                elif k == "arc":
                    _, cx, cy, r, a0, a1 = sg
                    ccw = a1 > a0
                    self.arc_move(cx, cy, r, a0, a1, ccw=ccw, f=o.feed)
                elif k == "circle":
                    _, cx, cy, r = sg
                    self.arc_move(cx, cy, r, 0.0, 180.0, ccw=True, f=o.feed)
                    self.arc_move(cx, cy, r, 180.0, 360.0, ccw=True, f=o.feed)
            self.move(z=o.safe_z, rapid=True)
        n_arc = sum(1 for sg in segs if sg[0] in ("arc", "circle"))
        self.op_list.append(f"{note} {len(segs)} 段（圆弧 {n_arc}），Z {z_top:g}→{z_bottom:g}")

    def pocket_zigzag(self, poly: Sequence[Pt], z: float, angle: float = 0.0):
        o = self.o
        a = math.radians(angle)
        ca, sa = math.cos(a), math.sin(a)
        rot = [(x * ca + y * sa, -x * sa + y * ca) for x, y in poly]
        xs = [p[0] for p in rot]
        ys = [p[1] for p in rot]
        y0, y1 = min(ys), max(ys)
        step = o.pocket_tool_dia * o.stepover
        y = y0 + o.pocket_tool_dia * 0.3
        self.raw(f"(型腔清角 角度{angle:g}° 步距{step:g})")
        self.move(z=o.safe_z, rapid=True)
        first = True
        flip = False
        while y < y1:
            seg = self._clip(rot, y)
            if seg:
                xa, xb = seg
                if flip:
                    xa, xb = xb, xa
                px1 = xa * ca - y * sa
                py1 = xa * sa + y * ca
                px2 = xb * ca - y * sa
                py2 = xb * sa + y * ca
                if first:
                    self.move(x=px1, y=py1, rapid=True)
                    self.move(z=z, f=o.plunge)
                    first = False
                else:
                    self.move(z=o.safe_z, rapid=True)
                    self.move(x=px1, y=py1, rapid=True)
                    self.move(z=z, f=o.plunge)
                self.move(x=px2, y=py2, f=o.feed)
                flip = not flip
            y += step
        self.move(z=o.safe_z, rapid=True)
        self.op_list.append(f"型腔铣削 角度{angle:g}°")

    @staticmethod
    def _clip(poly: Sequence[Pt], y: float):
        xs = []
        n = len(poly)
        for i in range(n):
            x1, y1 = poly[i]
            x2, y2 = poly[(i + 1) % n]
            if (y1 - y) * (y2 - y) < 0 or (abs(y1 - y) < 1e-9) or (abs(y2 - y) < 1e-9):
                if abs(y2 - y1) < 1e-12:
                    xs.extend([x1, x2])
                else:
                    t = (y - y1) / (y2 - y1)
                    xs.append(x1 + t * (x2 - x1))
        if len(xs) < 2:
            return None
        xs.sort()
        return xs[0], xs[-1]

    def wall_finish(self, poly: Sequence[Pt], z: float):
        """沿型腔侧壁走一圈光刀，清掉 zigzag 步距留下的壁残料。"""
        if not poly:
            return
        o = self.o
        start = poly[0]
        self.raw("(型腔壁光刀)")
        self.move(z=o.safe_z, rapid=True)
        self.move(x=start[0], y=start[1], rapid=True)
        self.move(z=z, f=o.plunge)
        for px, py in list(poly[1:]) + [start]:
            self.move(x=px, y=py, f=o.feed)
        self.move(z=o.safe_z, rapid=True)
        self.op_list.append(f"型腔壁光刀 {len(poly)} 段")

    def sphere_cut(self, cx: float, cy: float, R: float, h: float, tool_r: float,
                   stepover: float = 0.8, stepdown: float = 1.0, note: str = "球坑"):
        """球坑加工：球头刀等高环绕。

        刀心轨迹是以球心为圆心、半径 Rc = R - r_t 的球面（球头刀半径补偿的
        精确结果）。每一层 Z 恒定、半径为 Rc*sinθ 的整圆，用原生 G02/G03
        输出，不拆成密集点。

        若 r_t == R（刀球半径等于球坑半径），一次下刀即成精确球面，直接走单刀。
        """
        o = self.o
        R = max(float(R), 0.01)
        h = max(min(float(h), R), 0.0)
        if h <= 1e-6:
            return
        zc = -h + R                     # 球心 Z
        rt = max(min(float(tool_r), R), 0.01)
        Rc = R - rt
        self.raw(f"({note} 球径{2 * R:g} 深{h:g} 球头刀 Ø{2 * rt:g})")
        self.move(z=o.safe_z, rapid=True)
        if Rc < 0.05:
            self.move(x=cx, y=cy, rapid=True)
            self.move(z=-h, f=o.plunge)
            self.move(z=o.safe_z, rapid=True)
            self.op_list.append(f"{note} 单刀成形 深{h:g}")
            return
        # 球头刀加工凹球面：刀心可行域是半径 Rc=R-r_t 的**实心球**，
        # 不是球面。每个 Z 层要把半径 rho(z)=sqrt(Rc²-zc²) 的圆盘整个覆盖，
        # 否则球心附近会留下未切的核心（实测只走边界圆时欠切 >4%）。
        # 每一圈都是 Z 恒定的整圆 → 原生 G02/G03。
        nz = max(1, int(math.ceil(h / max(stepdown, 1e-3))))
        self.op_list.append(f"{note} 分层 {nz} 层，Rc={Rc:.2f}")
        budget = 600
        for j in range(1, nz + 1):
            z_tip = -h * j / nz
            zc_c = z_tip + rt                      # 刀心 Z
            if abs(zc_c) >= Rc:
                rho = 0.0
            else:
                rho = math.sqrt(max(Rc * Rc - zc_c * zc_c, 0.0))
            k = max(1, int(math.ceil(rho / max(stepover, 1e-3)))) if rho > 0.05 else 1
            budget -= k
            if budget < 0:
                break
            for i in range(k):
                rr = rho * (1.0 - i / k) if rho > 0.05 else 0.0
                if rr < 0.05:
                    break
                self.move(z=o.safe_z, rapid=True)
                self.move(x=cx + rr, y=cy, rapid=True)
                self.move(z=z_tip, f=o.plunge)
                self.arc_move(cx, cy, rr, 0.0, 360.0, ccw=True, f=o.feed)
            # 该层中心点（球底最后一点落在这里）
            self.move(z=o.safe_z, rapid=True)
            self.move(x=cx, y=cy, rapid=True)
            self.move(z=z_tip, f=o.plunge)
        self.move(z=o.safe_z, rapid=True)

    def dome_rough(self, cx: float, cy: float, L: float, W: float, a: float,
                   h: float, tool_r: float, stepdown: float = 0.8,
                   stepover: float = 2.0, note: str = "圆顶面粗清"):
        """平底刀清除圆顶轮廓以外、基体顶面以上的毛坯。

        圆顶是凸起，不能把整个顶面铣平；这里只清圆顶径向外的区域，
        圆顶本体留给球头刀精铣。刀轨是直线（G01），不拆曲面。
        """
        o = self.o
        xmin, xmax = cx - L / 2.0 + tool_r, cx + L / 2.0 - tool_r
        ymin, ymax = cy - W / 2.0 + tool_r, cy + W / 2.0 - tool_r
        if xmax <= xmin or ymax <= ymin:
            return
        self.raw(f"({note} 平底刀 Ø{2 * tool_r:g}，毛坯顶面 Z{h:g}→0)")
        nz = max(1, int(math.ceil(h / max(stepdown, 1e-6))))
        sy = max(0.3, stepover)
        ys = []
        y = ymin
        while y <= ymax + 1e-6:
            ys.append(y)
            y += sy
        if not ys or abs(ys[-1] - ymax) > 1e-6:
            ys.append(ymax)
        rev = False
        # First pass must be at stock top Z=h; otherwise the top dz layer is never cut.
        for j in range(nz):
            z = h * (1.0 - j / max(nz - 1, 1))
            for yy in ys:
                dx = max(a * a - (yy - cy) ** 2, 0.0) ** 0.5
                intervals = [(xmin, xmax)]
                if dx > 0.0:
                    cut0, cut1 = cx - dx - tool_r, cx + dx + tool_r
                    intervals = []
                    if xmin < cut0:
                        intervals.append((xmin, min(xmax, cut0)))
                    if xmax > cut1:
                        intervals.append((max(xmin, cut1), xmax))
                for x0, x1 in intervals:
                    if x1 - x0 < 0.05:
                        continue
                    if rev:
                        x0, x1 = x1, x0
                    self.move(z=o.safe_z, rapid=True)
                    self.move(x=x0, y=yy, rapid=True)
                    self.move(z=z, f=o.plunge)
                    self.move(x=x1, y=yy, f=o.feed)
                    self.move(z=o.safe_z, rapid=True)
                    rev = not rev
        self.op_list.append(f"{note} {nz} 层")

    def dome_cut(self, cx: float, cy: float, a: float, h: float, elliptic: bool,
                 tool_r: float, stepover: float = 0.3, note: str = "圆顶面"):
        """球头刀加工凸圆顶：刀心沿曲面法向偏置面走等高圆。

        切点在回转曲面上，刀球与曲面相切，不会像“刀尖直接贴曲面”那样切过头。
        每一层都是 Z 恒定的整圆，用原生 G03 输出。
        """
        o = self.o
        a = max(float(a), 0.01)
        h = max(float(h), 0.0)
        rt = max(float(tool_r), 0.01)
        if h <= 1e-6:
            return
        if elliptic:
            slope_lim = 3.5
            q2 = (slope_lim * slope_lim * a * a) / (slope_lim * slope_lim * a * a + h * h)
            rho_max = a * math.sqrt(max(q2, 0.0))
        else:
            R = (a * a + h * h) / (2.0 * h)
            slope_lim = 3.5
            rho_max = min(a, R * slope_lim / math.sqrt(1.0 + slope_lim * slope_lim))
        rho_max = min(a * 0.9999, max(rho_max, 0.0))
        nr = max(1, int(math.ceil(rho_max / max(stepover, 1e-3))))
        _kind = '椭圆' if elliptic else '球面'
        self.raw(f"({note} 球头刀 Ø{2 * rt:g}，{_kind}顶 a={a:g} h={h:g})")
        for i in range(nr + 1):
            rho = rho_max * (1.0 - i / nr)
            if elliptic:
                q = min(rho / a, 1.0)
                z = h * math.sqrt(max(1.0 - q * q, 0.0))
                slope = -(h * rho) / (a * a * math.sqrt(max(1.0 - q * q, 1e-12)))
            else:
                R = (a * a + h * h) / (2.0 * h)
                z = (h - R) + math.sqrt(max(R * R - rho * rho, 0.0))
                slope = -rho / math.sqrt(max(R * R - rho * rho, 1e-12))
            nz = 1.0 / math.sqrt(1.0 + slope * slope)
            nrho = -slope * nz
            cr = rho + rt * nrho
            tip_z = max(0.0, z + rt * nz - rt)
            if cr < 0.02:
                self.move(z=o.safe_z, rapid=True)
                self.move(x=cx, y=cy, rapid=True)
                self.move(z=tip_z, f=o.plunge)
                continue
            self.move(z=o.safe_z, rapid=True)
            self.move(x=cx + cr, y=cy, rapid=True)
            self.move(z=tip_z, f=o.plunge)
            self.arc_move(cx, cy, cr, 0.0, 360.0, ccw=True, f=o.feed)
        self.move(z=o.safe_z, rapid=True)
        self.op_list.append(f"{note} 等高 {nr + 1} 圈")

    def arc_move_xz(self, cx: float, cz: float, r: float, a0: float, a1: float,
                    f: float | None = None):
        """XZ 平面（G18）原生圆弧，角度按 atan2(z-cz, x-cx)。

        FANUC 约定：G18 的 G03 是绕 +Y 的正向旋转（+Z→+X），也就是本函数里
        「角度减小」；角度增大走 G02。整条弧一次 G02/G03，绝不拆点。
        """
        px, py, pz = self._xyz
        sx = cx + r * math.cos(math.radians(a0))
        sz = cz + r * math.sin(math.radians(a0))
        if math.hypot(sx - px, sz - pz) > 1e-6:
            self.move(x=sx, z=sz, f=self.o.plunge)
            px, py, pz = self._xyz
        ex = cx + r * math.cos(math.radians(a1))
        ez = cz + r * math.sin(math.radians(a1))
        i_off = cx - px
        k_off = cz - pz
        sweep = a1 - a0
        self.length += r * math.radians(abs(sweep))
        code = "G02" if sweep > 0 else "G03"
        fs = f" F{f:.1f}" if f else ""
        self.raw(f"{code} X{ex:.3f} Z{ez:.3f} I{i_off:.3f} K{k_off:.3f}{fs}")
        self._xyz = (ex, py, ez)

    def face_with_island(self, xmin: float, xmax: float, ymin: float, ymax: float,
                         z_top: float, z_bot: float, islands, tool_r: float,
                         stepdown: float = 1.5, stepover: float | None = None,
                         note: str = "顶面清平（避开扫掠肋）"):
        """平底刀把毛坯清到基体顶面，保留扫掠肋的矩形岛（岛=该肋的 XY 包络）。

        只用直线 G01 往复，岛边界按刀半径让开；肋侧壁随后由同一把刀沿包络
        精修。没有岛（例如贯穿肋）时整面清平。
        """
        o = self.o
        if z_top - z_bot <= 0.05:
            return
        so = max(0.3, float(stepover if stepover else 0.45) * o.tool_dia)
        nrow = max(1, int(math.ceil((ymax - ymin) / so)))
        dz = max(0.2, float(stepdown))
        nz = max(1, int(math.ceil((z_top - z_bot) / dz)))
        self.raw(f"({note})")
        for k in range(nz + 1):
            z = z_top - (z_top - z_bot) * k / nz
            rev = (k % 2 == 1)
            rows = list(range(nrow + 1))
            if rev:
                rows.reverse()
            for i in rows:
                y = ymin + (ymax - ymin) * i / max(nrow, 1)
                spans = [(xmin, xmax)]
                for isl in (islands or []):
                    ix0, ix1, iy0, iy1 = isl
                    if (iy0 - tool_r) <= y <= (iy1 + tool_r):
                        spans = _span_sub(spans, ix0 - tool_r, ix1 + tool_r)
                for (a, b) in spans:
                    if b - a <= 1e-6:
                        continue
                    if rev:
                        a, b = b, a
                    self.move(z=o.safe_z, rapid=True)
                    self.move(x=a, y=y, rapid=True)
                    self.move(z=z, f=o.plunge)
                    self.move(x=b, f=o.feed)
                    self.move(z=o.safe_z, rapid=True)
        self.op_list.append(f"{note} Z {z_top:.2f}→{z_bot:.2f}，{nz} 层 × {nrow + 1} 行")

    def sweep_top(self, segs, ys, note: str = "扫掠肋顶面仿形"):
        """球头刀沿 XZ 内原生段仿形（Y 分道），圆弧走 G18 的 G02/G03。

        segs 来自 calc.sweep_top_segments / xz_offset_segments，已是解析几何，
        这里只负责按 mm 落刀，不生成任何拟合点。
        """
        o = self.o
        if not segs or not ys:
            return
        n_arc = sum(1 for s in segs if s[0] == "arc")
        # NC 的 Z 一律是「刀尖」高度，而 segs 是球头刀「刀心」的偏置轨迹。
        # 球头刀刀心 = 刀尖 + r（竖直方向），所以落 Z 时必须减去 r；
        # 否则回灌按刀尖解释会把整条顶面抬高一个刀半径（实测欠切 7.85%）。
        _rr = max(float(getattr(o, "tool_dia", 0.0) or 0.0) / 2.0, 0.0)
        self.raw(f"({note})")
        self.raw("G18")
        for y in ys:
            sg0 = segs[0]
            if sg0[0] == "line":
                sx, sz = sg0[1], sg0[2]
            elif sg0[0] == "cap":
                sx, sz = sg0[1], sg0[2]
            else:
                sx = sg0[1] + sg0[3] * math.cos(math.radians(sg0[4]))
                sz = sg0[2] + sg0[3] * math.sin(math.radians(sg0[4]))
            self.move(z=o.safe_z, rapid=True)
            self.move(x=sx, y=y, rapid=True)
            self.move(z=sz - _rr, f=o.plunge)
            for sg in segs:
                if sg[0] == "line":
                    # 每段必须先到自己的起点。顶面短直线被并集裁剪后，
                    # 上一段终点与下一段起点不一定重合；只发终点会在转角处
                    # 斜穿一段（实测凹弧右侧欠切 3mm）。
                    _sx, _sz = sg[1], sg[2] - _rr
                    if math.hypot(self._xyz[0] - _sx, self._xyz[2] - _sz) > 1e-6:
                        self.move(x=_sx, z=_sz, f=o.plunge)
                    self.move(x=sg[3], z=sg[4] - _rr, f=o.feed)
                elif sg[0] == "cap":
                    _, xc, ztop, half = sg
                    px = self._xyz[0]
                    self.move(z=ztop - _rr, f=o.plunge)
                    self.move(x=xc + (half if px <= xc else -half), z=ztop - _rr, f=o.feed)
                else:
                    _, cx, cz, R, a0, sw, _sgn = sg
                    _asx = cx + R * math.cos(math.radians(a0))
                    _asz = cz - _rr + R * math.sin(math.radians(a0))
                    if math.hypot(self._xyz[0] - _asx, self._xyz[2] - _asz) > 1e-6:
                        self.move(x=_asx, z=_asz, f=o.plunge)
                    _s = sw
                    while _s > 360.0:
                        _s -= 360.0
                    while _s < -360.0:
                        _s += 360.0
                    self.arc_move_xz(cx, cz - _rr, R, a0, a0 + _s, f=o.feed)
            self.move(z=self.o.safe_z, rapid=True)
        self.raw("G17")
        self.op_list.append(f"{note} {len(segs)} 段（圆弧 {n_arc}），Y 分道 {len(ys)}")

    def drill(self, dia: float, holes: List[Pt], depth: float, t: int, blind: bool = False):
        o = self.o
        self.tool_change(t, dia, f"钻头 Ø{dia:g}")
        _tag = (f"盲孔 深{depth:g}" if blind else f"通孔（钻穿，Z-{depth:g}）")
        self.raw(f"(钻孔 Ø{dia:g} {_tag} 共 {len(holes)} 个)")
        for (x, y) in holes:
            self.move(z=o.safe_z, rapid=True)
            self.move(x=x, y=y, rapid=True)
            self.raw(f"G83 X{x:.3f} Y{y:.3f} Z{-depth:.3f} R2.000 Q{max(1.0, depth / 3):.1f} F{o.plunge:.1f}")
            self.length += depth * 1.2
        self.raw("G80")
        self.op_list.append(f"钻孔 Ø{dia:g} × {len(holes)}")


def _tool_for_width(w: float) -> float:
    for d in (3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0, 16.0, 20.0):
        if d <= w + 1e-6:
            return d
    return max(1.0, w * 0.8)


def generate(spec: PartSpec, out_dir: str, opts: CamOptions | None = None,
             plan: dict | None = None, post: str = "GENERIC") -> CamResult:
    """按工艺方案生成 NC 程序；plan=None 时使用内置规则方案。"""
    import dataclasses
    from .plan import POSTS, builtin_plan

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    res = CamResult()
    T = spec.thickness
    plan = plan or builtin_plan(spec, post)
    post = plan.get("post", post)
    base = opts or CamOptions()
    r_tool = float(plan["ops"][0].get("tool_dia", base.tool_dia)) / 2.0 if plan.get("ops") else base.tool_dia / 2
    _sl, _sw, _sh = stock_size(spec, base.stock_margin)
    _stock_top = max(_sh - T, 0.0)
    if _stock_top + 4.0 > base.safe_z:
        base = dataclasses.replace(base, safe_z=_stock_top + 5.0)

    g = GCode(base)
    g.raw("%")
    g.raw(f"O{base.program_no:04d} (PART: {spec.part_name})")
    g.raw(f"(MATERIAL: {spec.material}  STOCK: {_sl:g}x{_sw:g}x{_sh:g})")
    g.raw(f"(POST: {POSTS.get(post, post)})")
    g.raw(f"(CAM PLAN: {plan.get('source', 'builtin')} - {plan.get('notes', '')})")
    g.raw("G21 G17 G40 G49 G80 G90")
    g.raw("G54")
    g.raw("M09")
    g.raw("G00 Z50.000")

    t_no = 1
    drill_no = 0
    holes = {}
    for f in spec.build_features():
        if f.type == "hole" and f.diameter > 0:
            # 薄壁冲突的孔在建模时外扩过，加工必须按外扩后的直径钻，否则仿真欠切
            d_eff = f.diameter + 2.0 * spec.grow_of(f)
            through = f.is_through()
            dep = 0.0 if through else round(min(float(f.depth), T), 2)
            holes.setdefault((round(d_eff, 2), through, dep), []).append((f.x, f.y))

    for op in plan.get("ops", []):
        kind = op.get("type")
        o = dataclasses.replace(
            base,
            tool_dia=float(op.get("tool_dia") or base.tool_dia),
            feed=float(op.get("feed") or base.feed),
            spindle=float(op.get("spindle") or base.spindle),
            stepdown=float(op.get("stepdown") or base.stepdown),
            stepover=float(op.get("stepover") or base.stepover),
            pocket_tool_dia=float(op.get("tool_dia") or base.pocket_tool_dia),
            plunge=float(op.get("plunge") or base.plunge),
        )
        g.o = o
        if kind == "profile":
            r = o.tool_dia / 2.0
            # 刀心轨迹 = 零件轮廓法向偏一个**刀半径**（偏 r+余量 会把零件车小）
            # 用 offset_outline 直接生成原生几何（直线 + 圆弧），不再把圆弧拆成密集点。
            radial_left = 0.5 if o.finish_pass else 0.0
            _ptop = 0.5 + _stock_top
            g.tool_change(t_no, o.tool_dia, op.get("tool", "立铣刀") + "(轮廓)")
            if radial_left > 0:
                g.contour_segs(offset_outline(spec, r + radial_left), _ptop, -(T + 0.5),
                               note=f"轮廓粗铣（留 {radial_left:g} 精加工余量）")
            g.contour_segs(offset_outline(spec, r), _ptop, -(T + 0.5), passes=1,
                           note="轮廓精铣（单刀到尺寸）")
            t_no += 1
        elif kind == "dome":
            # Dome CAM: flat end mill clears material outside the dome footprint,
            # then a ball-nose cutter follows the offset surface with native G03 circles.
            _dh = float(getattr(spec, "dome_h", 0.0) or 0.0)
            if _dh <= 0:
                g.raw("(DOME: height is zero, skipped)")
                continue
            if spec.base_kind == "round":
                _L = _W = float(spec.diameter)
            else:
                _L, _W = float(spec.length), float(spec.width)
            _a = min(_L, _W) / 2.0
            _rough_d = float(op.get("rough_dia") or 0.0)
            if _rough_d > 0 and spec.base_kind != "round":
                _ro = dataclasses.replace(o, tool_dia=_rough_d,
                                          feed=float(op.get("feed") or o.feed),
                                          spindle=float(op.get("spindle") or o.spindle))
                g.o = _ro
                g.tool_change(t_no, _rough_d, "立铣刀(圆顶粗清)")
                g.dome_rough(0.0, 0.0, _L, _W, _a, _dh,
                             _rough_d / 2.0,
                             stepdown=float(op.get("stepdown_mm") or 0.8),
                             stepover=max(1.0, _rough_d * 0.55),
                             note="圆顶面粗清")
                t_no += 1
                g.o = o
            g.tool_change(t_no, o.tool_dia, "球头铣刀(圆顶精铣)")
            g.dome_cut(0.0, 0.0, _a, _dh,
                       bool(getattr(spec, "dome_elliptic", True)),
                       o.tool_dia / 2.0,
                       stepover=max(0.1, float(op.get("stepover_mm") or 0.3)),
                       note="圆顶面精铣")
            t_no += 1
        elif kind in ("pocket", "slot"):
            feats = [f for f in spec.features
                     if (f.type in ("pocket", "step") if kind == "pocket" else f.type == "slot")]
            if not feats:
                continue
            g.tool_change(t_no, o.tool_dia, op.get("tool", "立铣刀") + ("(型腔)" if kind == "pocket" else "(槽)"))
            t_no += 1
            for f in feats:
                d = feature_depth(spec, f)
                rr = o.tool_dia / 2.0
                _passes = max(1, int(math.ceil(d / max(o.stepdown, 1e-6))))
                if f.type == "slot":
                    # 刀心可达区域 = 槽轮廓内缩一个刀半径（内缩直径会沿壁残留一整刀半径）。
                    # 粗清角走栅格直线（本来就只有直线），侧壁光刀必须是原生直线+圆弧，
                    # 不得 flatten 成密集点。
                    segs = slot_outline(max(1.0, (f.length or f.width) - 2 * rr),
                                        max(1.0, f.width - 2 * rr),
                                        f.x, f.y, f.angle)
                    poly = flatten(segs)[0]
                    g.pocket_zigzag(poly, -d, f.angle)
                    g.contour_segs(segs, 0.0, -d, passes=_passes, note="槽壁光刀")
                else:
                    w = f.width - 2 * rr
                    h = f.length - 2 * rr
                    if w > 0.5 and h > 0.5:
                        segs = rect_outline(h, w, max(0.0, f.corner_radius - rr), f.x, f.y)
                        poly = flatten(segs)[0]
                        g.pocket_zigzag(poly, -d, 0.0 if h >= w else 90.0)
                        g.contour_segs(segs, 0.0, -d, passes=_passes, note="型腔壁光刀")
                    else:
                        g.pocket_zigzag(flatten(rect_outline(f.length, f.width, 0, f.x, f.y))[0], -d, 0.0)
        elif kind == "shell":
            # 抽壳（开口朝上）：内腔就是一个带原生圆弧的型腔
            t_wall = float(getattr(spec, "shell_t", 0.0) or 0.0)
            rr = o.tool_dia / 2.0
            depth = max(T - t_wall, 0.0)
            if t_wall <= 0 or depth <= 0:
                g.raw("(抽壳参数为 0，跳过)")
                continue
            g.tool_change(t_no, o.tool_dia, op.get("tool", "立铣刀") + "(抽壳内腔)")
            t_no += 1
            tool_segs = inner_outline(spec, t_wall + rr)
            if not tool_segs:
                g.raw("(内腔尺寸小于刀径，跳过)")
                continue
            clear_poly = flatten(tool_segs, 6.0)
            _n = max(1, int(math.ceil(depth / max(o.stepdown, 1e-6))))
            _dz = depth / _n
            for _i in range(_n):
                _z = -_dz * (_i + 1)
                if clear_poly:
                    g.pocket_zigzag(clear_poly[0], _z, 0.0)
                g.contour_segs(tool_segs, _z, _z, passes=1, note="抽壳内腔壁光刀")
        elif kind == "sphere":
            feats = [f for f in spec.build_features() if f.type == "sphere"]
            if not feats:
                continue
            g.tool_change(t_no, o.tool_dia, op.get("tool", "球头铣刀"))
            t_no += 1
            for f in feats:
                R = float(f.diameter) / 2.0
                h = min(float(f.depth or R), R)
                # 球面是三维曲面：步距/层深必须按 mm 给，不能再乘刀具直径
                # （o.stepover 是比例，乘完会变成 3.6mm，球面留下 0.2mm 台阶）。
                _so = float(op.get("stepover_mm") or 0.3)
                _sd = float(op.get("stepdown_mm") or min(0.25, max(0.1, o.stepdown)))
                g.sphere_cut(f.x, f.y, R, h, o.tool_dia / 2.0,
                             stepover=max(0.1, _so), stepdown=max(0.08, _sd))
        elif kind == "sweep":
            from ..calc import (sweep_machinability as _swm,
                                sweep_top_segments as _swtop,
                                xz_offset_segments as _xzoff,
                                xz_clamp_zmin as _xzclamp)
            sws = [x for x in (getattr(spec, "sweeps", None) or []) if _swm(spec, x)["ok"]]
            if not sws:
                g.raw("(扫掠体：没有三轴单面可加工的条目，跳过 —— 见未加工清单)")
                continue
            rr = o.tool_dia / 2.0
            # 侧壁：平底刀沿条带两侧 + 外端直上直下清根（整段都是直线）
            g.tool_change(t_no, o.tool_dia, op.get("tool", "立铣刀(扫掠肋)"))
            _wall_tool = t_no
            t_no += 1
            # 毛坯顶面到基体顶面这一层必须清掉（否则整块板顶面留着余量），
            # 清平时把每条肋的 XY 包络留成岛，肋本体由后面的轮廓+顶面仿形做。
            _islands = []
            for _sw in sws:
                _b = _swm(spec, _sw)["bbox"]
                _islands.append((_b[0], _b[1], _b[2], _b[3]))
            if _stock_top > 0.05:
                g.face_with_island(-(spec.length / 2.0 - rr), spec.length / 2.0 - rr,
                                   -(spec.width / 2.0 - rr), spec.width / 2.0 - rr,
                                   _stock_top, 0.0, _islands, rr,
                                   stepdown=min(max(o.stepdown, 0.5), 1.5),
                                   stepover=o.stepover,
                                   note="顶面清平（避开扫掠肋）")
            for _i, _sw in enumerate(sws, 1):
                _info = _swm(spec, _sw)
                _x0, _x1, _y0, _y1, _zmin, _zmax = _info["bbox"]
                # 世界坐标里零件底面 z=0、基体顶面 z=T；CAM 坐标基体顶面=0。
                _zbot = -(T + 0.5) if _zmin <= 1e-6 else 0.0
                _ztop = max(0.5 + _stock_top, (_zmax - T) + 1.0)
                _ya, _yb = _y0 - rr, _y1 + rr
                # 左侧端面同样要把刀心放到肋轮廓外一个刀半径；原来用 _x0
                # 会让平底刀切进肋端 rr（实测浅弧/凹弧左端过切 1~3.7%）。
                # 平底刀绕矩形岛外侧清根时，刀心轨迹四角必须是 R=刀半径
                # 的四分之一圆弧。若直接走矩形尖角，刀在四个外角会切入
                # 岛内 rr*(sqrt(2)-1)，浅弧两端实测过切 1.4%~3.9%。
                # 这里用原生 G02/G03 圆弧闭合，不把圆弧拆成点链。
                _wall = [
                    ("line", _x0, _y0 - rr, _x1, _y0 - rr),
                    ("arc", _x1, _y0, rr, -90.0, 0.0),
                    ("line", _x1 + rr, _y0, _x1 + rr, _y1),
                    ("arc", _x1, _y1, rr, 0.0, 90.0),
                    ("line", _x1, _y1 + rr, _x0, _y1 + rr),
                    ("arc", _x0, _y1, rr, 90.0, 180.0),
                    ("line", _x0 - rr, _y1, _x0 - rr, _y0),
                    ("arc", _x0, _y0, rr, 180.0, 270.0),
                ]
                g.contour_segs(_wall, _ztop, _zbot, note=f"扫掠肋 {_i} 侧壁清根")
            # 顶面：球头刀沿偏置后的原生直线/圆弧仿形
            _bo = dataclasses.replace(o, feed=min(o.feed, 260.0), spindle=4200.0)
            g.o = _bo
            g.tool_change(t_no, o.tool_dia, "球头铣刀(扫掠肋顶面)")
            t_no += 1
            for _i, _sw in enumerate(sws, 1):
                _info = _swm(spec, _sw)
                _x0, _x1, _y0, _y1, _zmin, _zmax = _info["bbox"]
                # 顶面段是 CAD 世界坐标（基体顶面 z=T）；刀路用 CAM 坐标（基体顶面=0）
                # 凹弧顶面低于基体顶面时，刀尖不能跟着扎到基体里：
                # 把刀心轨迹限制在 z>=rr（刀尖 z>=0），低处用原生 G01 走平。
                _top = _xzclamp(_shift_z(_xzoff(_swtop(_sw), rr), -T), rr)
                if not _top:
                    g.raw(f"(扫掠肋 {_i}：顶面解析段为空，跳过)")
                    continue
                _w = float(_info["w"])
                # 球头刀必须覆盖整个条带宽度。原来从 y0+rr 到 y1-rr 只扫
                # 中间 w-2r 一段，两侧各 r 宽完全没切到，回灌欠切 1~2%。
                # 刀心走到 y0/y1 时刀面正好切到侧壁顶棱；再往外才由平底刀
                # 侧壁清根处理。
                _span = max(_w, 0.0)
                # 曲面顶必须用 mm 级步距：0.45×刀径 = 2.7mm 时球头刀
                # 在圆弧顶面留下 0.32mm  scallop，回灌按 0.05mm 容差判欠切 3.5%。
                _so = max(0.1, float(op.get("stepover_mm") or
                                     (float(op.get("stepover") or 0.45) * o.tool_dia)))
                _n = max(2, int(math.ceil(_span / _so)) + 1)
                _ys = [_y0 + (_span * (k / max(_n - 1, 1))) for k in range(_n)]
                g.sweep_top(_top, _ys, note=f"扫掠肋 {_i} 顶面仿形")
            g.o = o
        elif kind == "fillet":
            # 凸棱圆角：Ø2R 球头刀。球头刀一次沿棱线扫过会挖掉整块四分之一圆，
            # 不是 R 圆角；必须在 XZ 截面沿「球心=圆角圆心 + 2R」的圆弧分层。
            # 每一层的 XY 轮廓都交给 offset_outline 生成原生直线/圆弧，
            # 不把圆弧拆成点链；层间按球面残高自动加密。
            f = float(op.get("radius") or getattr(spec, "fillet", 0.0) or 0.0)
            # 物理上限：刀尖最低 2R，超过 T/2 会扎穿底面；内偏 R 也不能吞掉整个料宽
            _lim = 0.0
            try:
                from ..calc import fillet_limit as _flim
                _lim = _flim(spec)
            except Exception:
                _lim = 0.0
            if _lim > 0 and f > _lim:
                g.raw(f"(圆角 R{f:g} 超过可加工上限 R{_lim:g}，按 R{_lim:g} 加工)")
                f = _lim
            if f <= 0:
                g.raw("(圆角半径为 0，跳过)")
                continue
            want_d = 2.0 * f
            if abs(o.tool_dia - want_d) > 0.02:
                g.raw(f"(警告：圆角 R{f:g} 需要 Ø{want_d:g} 球头刀，当前 Ø{o.tool_dia:g})")
            n_pass = int(op.get("passes") or 0)
            if n_pass < 6:
                # 相邻球心弦高控制在约 0.02mm：N≈(π/2)/(2√(tol/R))
                n_pass = max(8, int(math.ceil((math.pi / 2.0) /
                                              max(2.0 * math.sqrt(0.02 / f), 1e-6))))
            n_pass = min(max(n_pass, 8), 96)
            g.tool_change(t_no, want_d, op.get("tool", "球头铣刀") + "(轮廓圆角)")
            t_no += 1
            g.raw(f"(圆角 R{f:g} 顶面外棱，球头刀 Ø{want_d:g}，{n_pass} 层原生偏置轮廓)")
            any_pass = False
            for i in range(n_pass):
                phi = math.pi - (math.pi / 2.0) * (i / max(n_pass - 1, 1))
                off = -f - 2.0 * f * math.cos(phi)      # offset_outline: 正=外扩，负=内缩
                z_center = -f + 2.0 * f * math.sin(phi)
                z_tip = z_center - f                    # NC 按刀尖编程
                segs = offset_outline(spec, off)
                if not segs:
                    continue
                any_pass = True
                g.contour_segs(segs, z_tip, z_tip, passes=1,
                               note=f"圆角 R{f:g} 第 {i + 1}/{n_pass} 层")
            if not any_pass:
                g.raw("(没有可加工的外轮廓，圆角工序跳过)")
        elif kind == "chamfer":
            # 90° 倒角刀：刀尖走在外形轮廓上、Z = -C，锥面自然切出 C×C 倒角
            c = float(op.get("width") or getattr(spec, "chamfer", 0.0) or 0.0)
            if c <= 0:
                g.raw("(倒角宽度为 0，跳过)")
                continue
            g.tool_change(t_no, o.tool_dia, op.get("tool", "倒角刀 90°"))
            t_no += 1
            g.raw(f"(倒角 C{c:g} 外形轮廓一圈，90°倒角刀)")
            segs0 = offset_outline(spec, 0.0)
            if not segs0:
                continue
            zc = -c
            g.move(z=o.safe_z, rapid=True)
            _p0 = segs0[0]
            if _p0[0] == "line":
                _sx, _sy = _p0[1], _p0[2]
            elif _p0[0] == "arc":
                _sx = _p0[1] + _p0[3] * math.cos(math.radians(_p0[4]))
                _sy = _p0[2] + _p0[3] * math.sin(math.radians(_p0[4]))
            else:
                _sx, _sy = _p0[1] + _p0[3], _p0[2]
            g.move(x=_sx, y=_sy, rapid=True)
            g.move(z=zc, f=o.plunge)
            for _sg in segs0:
                if _sg[0] == "line":
                    g.move(x=_sg[3], y=_sg[4], f=o.feed)
                elif _sg[0] == "arc":
                    g.arc_move(_sg[1], _sg[2], _sg[3], _sg[4], _sg[5],
                               ccw=_sg[5] > _sg[4], f=o.feed)
                elif _sg[0] == "circle":
                    g.arc_move(_sg[1], _sg[2], _sg[3], 0.0, 180.0, ccw=True, f=o.feed)
                    g.arc_move(_sg[1], _sg[2], _sg[3], 180.0, 360.0, ccw=True, f=o.feed)
            g.move(z=o.safe_z, rapid=True)
            g.op_list.append(f"倒角 C{c:g} 外形轮廓")
        elif kind == "drill":
            dia = float(op.get("tool_dia") or 6.0)
            through = bool(op.get("through", True))
            op_depth = float(op.get("depth") or 0.0)
            want_dep = 0.0 if through else round(op_depth, 2)
            pts = holes.get((round(dia, 2), through, want_dep)) or []
            if not pts:
                # 只接受同孔径 + 同通/盲 + 同深度的同一族孔；宁缺勿错
                cands = [(k, v) for k, v in holes.items()
                         if abs(k[0] - dia) <= 0.05 and k[1] == through
                         and (through or abs(k[2] - want_dep) <= 0.05)]
                if cands:
                    (dia, through, want_dep), pts = cands[0]
                else:
                    g.raw(f"(跳过钻孔 目标Ø{dia:g} {'通' if through else '盲'}: 特征表里没有这一组)")
                    continue
            if not pts:
                continue
            # 通孔钻穿留余量；盲孔按图纸深度，绝不多钻
            depth = (T + (2.0 if T * 0.25 < 2 else 3.0)) if through else min(want_dep, T)
            g.drill(dia, pts, depth, t_no if post == "GENERIC" else t_no,
                    blind=not through)
            t_no += 1
            drill_no += len(pts)

    g.raw("")
    g.raw("M09")
    g.raw("M05")
    g.raw("G00 Z50.000")
    g.raw("G91 G28 Z0")
    g.raw("G90")
    g.raw("M30")
    g.raw("%")

    nc = out / f"{spec.file_stem}.nc"
    for _old in out.glob("*.nc"):
        if _old != nc:
            try:
                _old.unlink()
            except Exception:
                pass
    nc.write_text("\n".join(g.lines) + "\n", encoding="utf-8")
    res.nc_path = str(nc)
    res.lines = len(g.lines)
    res.path_length = g.length
    res.minutes = g.length / max(base.feed, 1.0) + 0.6 * len(g.op_list)
    res.operations = g.op_list

    try:
        res.svg_path = _svg(spec, out / f"{spec.file_stem}.svg", base)
    except Exception:
        res.svg_path = ""

    try:
        from .plan import unplanned_ops as _unplanned_ops
        res.unplanned = _unplanned_ops(spec, plan)
    except Exception:
        res.unplanned = []
    problems = validate(spec, base, g)
    res.message = ("NC 程序生成完成（" + POSTS.get(post, post) + "）"
                   if not problems else "NC 程序已生成（告警）：" + "；".join(problems[:3]))
    # 「没生成工序」和「三轴根本做不出来」必须分开：
    # 后者是硬失败（不能为了消 unplanned 就把 ok 判 True），前者才只是告警。
    _hard_keys = ("须侧铣", "三轴无法", "翻面装夹", "第4轴", "悬空",
                  "不可加工", "无法从外部加工", "三轴单面够不到")
    _blocking = [u for u in res.unplanned if any(k in u for k in _hard_keys)]
    if res.unplanned:
        res.message += "；未加工：" + "；".join(res.unplanned[:3])
    if _blocking:
        res.message = ("不可加工（工艺限制）：" + "；".join(_blocking[:2])
                       + "。程序仅为可加工部分的局部刀路，未通过。")

    rep = out / f"{spec.file_stem}_CAM说明.md"
    rep.write_text(_report(spec, base, g, res, plan), encoding="utf-8")
    res.report_path = str(rep)
    res.ok = not _blocking
    return res


def validate(spec: PartSpec, o: CamOptions, g: GCode) -> List[str]:
    """Very small sanity check of the generated program."""
    problems: List[str] = []
    sl, sw, sh = stock_size(spec, o.stock_margin)
    xs, ys, zs = [], [], []
    drilled = set()
    spindle = False
    for ln in g.lines:
        if ln.startswith("S") and "M03" in ln:
            spindle = True
        if ln.startswith("G00") or ln.startswith("G01"):
            for tok, arr in (("X", xs), ("Y", ys), ("Z", zs)):
                i = ln.find(tok)
                if i >= 0:
                    try:
                        arr.append(float(ln[i+1:].split()[0]))
                    except Exception:
                        pass
        if ln.startswith("G83"):
            try:
                x = float(ln.split("X")[1].split()[0])
                y = float(ln.split("Y")[1].split()[0])
                drilled.add((round(x, 2), round(y, 2)))
            except Exception:
                pass
    if not spindle:
        problems.append("未设置主轴转速")
    if xs and (max(xs) > sl / 2 + 1 or min(xs) < -sl / 2 - 1):
        problems.append("X 超出行程范围")
    if ys and (max(ys) > sw / 2 + 1 or min(ys) < -sw / 2 - 1):
        problems.append("Y 超出行程范围")
    if zs and min(zs) < -(spec.thickness + 10):
        problems.append("Z 超深")
    holes = [(round(f.x, 2), round(f.y, 2)) for f in spec.features if f.type == "hole"]
    missing = [h for h in holes if h not in drilled]
    if missing:
        problems.append(f"{len(missing)} 个孔未出现在钻孔循环中")
    return problems


def _svg(spec: PartSpec, path: Path, o: CamOptions) -> str:
    L, W, _ = stock_size(spec, o.stock_margin)
    pad = 20
    scale = min(900 / (L + 2 * pad), 700 / (W + 2 * pad)) if L and W else 1.0

    def tx(x, y):
        return (400 + x * scale, 380 - y * scale)

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="760" '
             f'viewBox="0 0 1000 760"><rect width="1000" height="760" fill="#0d1117"/>']
    sl, sw, _ = stock_size(spec, o.stock_margin)
    x0, y0 = tx(-sl / 2, sw / 2)
    parts.append(f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{sl*scale:.1f}" height="{sw*scale:.1f}" '
                 f'fill="none" stroke="#30363d" stroke-dasharray="6 4"/>')
    for pts in flatten(base_outline(spec)):
        coords = " ".join(f"{tx(x,y)[0]:.1f},{tx(x,y)[1]:.1f}" for x, y in pts)
        parts.append(f'<polygon points="{coords}" fill="#1f6feb22" stroke="#58a6ff" stroke-width="2"/>')
    for f in spec.features:
        if f.type == "hole":
            cx, cy = tx(f.x, f.y)
            parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{f.diameter/2*scale:.1f}" '
                         f'fill="#0d1117" stroke="#f78166" stroke-width="1.6"/>')
        elif f.type == "slot":
            from ..geom import slot_outline as so
            for pts in flatten(so(f.length, f.width, f.x, f.y, f.angle)):
                coords = " ".join(f"{tx(x,y)[0]:.1f},{tx(x,y)[1]:.1f}" for x, y in pts)
                parts.append(f'<polygon points="{coords}" fill="#0d1117" stroke="#d2a8ff" stroke-width="1.6"/>')
        elif f.type in ("pocket", "step"):
            for pts in flatten(rect_outline(f.length, f.width, f.corner_radius, f.x, f.y)):
                coords = " ".join(f"{tx(x,y)[0]:.1f},{tx(x,y)[1]:.1f}" for x, y in pts)
                parts.append(f'<polygon points="{coords}" fill="#0d1117" stroke="#7ee787" stroke-width="1.6"/>')
    txt = f"{spec.part_name}   {spec.length:g}×{spec.width:g}×{spec.thickness:g}mm   {spec.material}"
    parts.append(f'<text x="20" y="30" fill="#c9d1d9" font-size="18" font-family="Microsoft YaHei">{txt}</text>')
    parts.append(f'<text x="20" y="56" fill="#8b949e" font-size="13" font-family="Microsoft YaHei">'
                 f'毛坯 {sl:g}×{sw:g}  ·  刀具 Ø{o.tool_dia:g}/Ø{o.pocket_tool_dia:g}  ·  进给 {o.feed:g}mm/min</text>')
    parts.append("</svg>")
    path.write_text("\n".join(parts), encoding="utf-8")
    return str(path)


def _report(spec: PartSpec, o: CamOptions, g: GCode, res: CamResult, plan: dict | None = None) -> str:
    sl, sw, sh = stock_size(spec, o.stock_margin)
    rows = "\n".join(f"| {i+1} | {op} |" for i, op in enumerate(g.op_list))
    plan_lines = ""
    if plan:
        plan_lines = "\n## 工艺方案\n" + "\n".join(
            f"- {op.get('type')}：Ø{op.get('tool_dia')} {op.get('tool','')} "
            f"{op.get('strategy','')} 进给{op.get('feed')} 转速{op.get('spindle')}"
            for op in plan.get("ops", [])) + f"\n- 来源：{plan.get('source')}（{plan.get('notes','')}）\n"
    unplanned_lines = ""
    if res.unplanned:
        unplanned_lines = ("\n## 未加工项（模型有、程序没有）\n"
                           + "\n".join(f"- {x}" for x in res.unplanned) + "\n")
    return f"""# {spec.part_name} · 加工程序说明

## 零件
- 外形：{spec.length:g} × {spec.width:g} × {spec.thickness:g} mm（{spec.base_kind}）
- 材料：{spec.material}
- 特征数：{len(spec.features)}

## 毛坯与装夹
- 推荐毛坯：{sl:g} × {sw:g} × {sh:g} mm
- 坐标系：G54，X0Y0 为零件中心，Z0 为顶面
- 装夹：平口钳夹持两侧，底面垫平行垫铁

## 工序
| 序号 | 工序 |
|---|---|
{rows}

{plan_lines}
{unplanned_lines}
## 切削参数
- 主轴：{o.spindle:.0f} r/min
- 进给：{o.feed:.0f} mm/min，下刀 {o.plunge:.0f} mm/min
- 每层切深：{o.stepdown:g} mm，步距：{o.stepover*100:.0f}% 刀具直径
- 冷却：{"开" if o.coolant else "关"}

## 程序
- 文件：`{Path(res.nc_path).name}`（{res.lines} 行）
- 轨迹长度：{res.path_length/1000:.2f} m，预计加工时间约 {res.minutes:.0f} 分钟
- 后处理：通用 ISO / FANUC 三轴（可直接导入 NX CAM、SOLIDWORKS CAM、CATIA 加工模块做仿真或转机床格式）
"""
