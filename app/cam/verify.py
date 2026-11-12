"""NC 程序回灌验证（GPU 无关，纯 numpy Z-map 材料去除仿真）。

把生成的 NC 程序重新解析一遍，在毛坯上用 Z-map（高度场）模拟刀具扫掠，
再与 PartSpec 推算出的理论几何逐点比对，用于回答一个硬问题：

    这份程序真的能加工出软件建的那个模型吗？

检查项
------
* 尺寸：仿真后工件的长/宽是否等于图纸尺寸
* 过切：仿真顶面低于理论顶面的面积（会把零件做小）
* 欠切：仿真顶面高于理论顶面的面积（残留材料，没加工到位）
* 孔位：每个孔的坐标是否出现在钻孔循环里
* Z 深度：最深处是否超过零件厚度
* 程序结构：主轴、进给、回零、M30、圆弧/单位/坐标模式

用法
----
    from app.cam.verify import verify_nc
    rep = verify_nc(spec, "out/pdf_1107/箱座.nc")
    print(rep.summary)
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from ..calc import base_outline, feature_depth, stock_size
from ..geom import flatten
from ..models import PartSpec

Pt = Tuple[float, float]

_WORD = re.compile(r"([A-Z])([+-]?(?:\d+\.?\d*|\.\d+))")


@dataclass
class Move:
    x: float
    y: float
    z: float
    rapid: bool
    line_no: int
    tool_dia: float
    feed: Optional[float] = None
    shape: str = "flat"          # flat | ball | cone（90°倒角刀）


@dataclass
class VerifyReport:
    ok: bool = False
    nc_path: str = ""
    lines: int = 0
    moves: int = 0
    stock: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    part_size: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    sim_size: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    sim_volume: float = 0.0
    want_volume: float = 0.0
    overcut_area: float = 0.0
    undercut_area: float = 0.0
    footprint_area: float = 0.0
    overcut_pct: float = 0.0
    undercut_pct: float = 0.0
    blank_left_pct: float = 0.0
    size_error: Tuple[float, float] = (0.0, 0.0)
    wall_size: Tuple[float, ...] = ()
    baseline_gap_pct: float = 0.0      # 两套理论体积基线之差（%）
    baseline_gap_src: str = ""        # 本次比对用的参考来源：exact / grid
    cad_volume: float = 0.0           # CAD 实测体积（多后端取均值）
    cad_n: int = 0
    want_vs_cad_pct: float = 0.0
    expected_vs_cad_pct: float = 0.0
    holes_ok: int = 0
    holes_total: int = 0
    problems: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    tool_summary: List[str] = field(default_factory=list)
    max_depth: float = 0.0
    volume_error_pct: float = 0.0     # 仿真体积相对理论体积的偏差

    @property
    def level(self) -> str:
        return "几何回灌（Z-map 材料去除仿真）"

    @property
    def summary(self) -> str:
        s = (f"【{self.level}】仿真体积 {self.sim_volume:,.0f} mm³ / 理论 {self.want_volume:,.0f} mm³；"
             f"过切 {self.overcut_pct:.2f}%、欠切 {self.undercut_pct:.2f}%；"
             f"仿真外形 {self.sim_size[0]:.2f}×{self.sim_size[1]:.2f} "
             f"(图纸 {self.part_size[0]:g}×{self.part_size[1]:g})")
        return s

    def to_dict(self) -> Dict:
        return {
            "ok": self.ok, "level": self.level, "verified_kind": "geometry",
            "nc_path": self.nc_path, "lines": self.lines, "moves": self.moves,
            "stock": [round(v, 3) for v in self.stock],
            "part_size": [round(v, 3) for v in self.part_size],
            "sim_size": [round(v, 3) for v in self.sim_size],
            "sim_volume": round(self.sim_volume, 1),
            "want_volume": round(self.want_volume, 1),
            "overcut_pct": round(self.overcut_pct, 3),
            "undercut_pct": round(self.undercut_pct, 3),
            "size_error": [round(v, 4) for v in self.size_error],
            "wall_size": [round(v, 3) for v in self.wall_size],
            "baseline_gap_pct": round(self.baseline_gap_pct, 4),
            "baseline_gap_src": self.baseline_gap_src,
            "cad_volume": self.cad_volume, "cad_n": self.cad_n,
            "want_vs_cad_pct": round(self.want_vs_cad_pct, 4),
            "expected_vs_cad_pct": round(self.expected_vs_cad_pct, 4),
            "holes": f"{self.holes_ok}/{self.holes_total}",
            "max_depth": round(self.max_depth, 2),
        "volume_error_pct": round(self.volume_error_pct, 2),
            "problems": self.problems, "warnings": self.warnings,
            "tools": self.tool_summary,
            "summary": self.summary,
        }

    def markdown(self) -> str:
        def yz(b: bool) -> str:
            return "✅" if b else "❌"

        lines = [
            "# NC 程序回灌验证报告", "",
            f"- 程序：`{Path(self.nc_path).name}`（{self.lines} 行 / {self.moves} 个运动段）",
            f"- 毛坯：{self.stock[0]:.2f} × {self.stock[1]:.2f} × {self.stock[2]:.2f} mm",
            f"- 图纸：{self.part_size[0]:g} × {self.part_size[1]:g} × {self.part_size[2]:g} mm",
            f"- 仿真外形：{self.sim_size[0]:.2f} × {self.sim_size[1]:.2f} mm，"
            f"最大剩余高度 {self.sim_size[2]:.2f} mm（不是零件厚度）",
            "", "## 结论", "",
            f"- 整体：{yz(self.ok)} {'通过' if self.ok else '未通过'}",
            f"- 外形尺寸误差：X {self.size_error[0]:+.3f} mm，Y {self.size_error[1]:+.3f} mm",
            f"- 过切面积占比：{self.overcut_pct:.2f}%（切掉不该切的料）",
            f"- 欠切面积占比：{self.undercut_pct:.2f}%（该切的没切干净）",
            f"- 仿真体积：{self.sim_volume:,.0f} mm³ / 理论 {self.want_volume:,.0f} mm³",
            f"- 孔位核对：{self.holes_ok}/{self.holes_total}",
            f"- 最大切深：{self.max_depth:.2f} mm（零件厚 {self.part_size[2]:g} mm；"
    f"通孔钻穿余量 3.5 mm 已计入，超出零件厚度属正常）",
        ]
        if self.tool_summary:
            lines += ["", "## 刀具序列"] + [f"- {t}" for t in self.tool_summary]
        if self.problems:
            lines += ["", "## 问题"] + [f"- ❌ {p}" for p in self.problems]
        if self.warnings:
            lines += ["", "## 提示"] + [f"- ⚠️ {w}" for w in self.warnings]
        if self.ok and not self.problems:
            lines += ["", "> 程序可加工出与模型一致的几何，可进入后置/上机。"]
        return "\n".join(lines) + "\n"


def _num(words: Dict[str, float], key: str, default: float) -> float:
    v = words.get(key)
    return default if v is None else v


def parse_nc(path: str | Path) -> Tuple[List[Move], Dict, List[str]]:
    """解析通用 ISO / FANUC 三轴程序，返回运动段、头信息、警告。"""
    p = Path(path)
    text = p.read_text(encoding="utf-8", errors="replace")
    raw_lines = text.splitlines()
    meta = {"stock": None, "part": None, "post": None, "tools": {}, "tool_shape": {}}
    warnings: List[str] = []
    moves: List[Move] = []

    x = y = z = 0.0
    tool_dia = 0.0
    tool_shape = "flat"
    cur_tool = 0
    feed: Optional[float] = None
    abs_mode = True
    rapid = True
    plane = 17          # 17=G17 XY / 18=G18 XZ / 19=G19 YZ

    for i, ln in enumerate(raw_lines, 1):
        s = ln.strip()
        if not s:
            continue
        if s.startswith("%"):
            continue
        if s.startswith("("):
            m = re.search(r"STOCK:\s*([\d.]+)x([\d.]+)x([\d.]+)", s)
            if m:
                meta["stock"] = tuple(float(g) for g in m.groups())
            m = re.search(r"PART:\s*([^)]+)", s)
            if m:
                meta["part"] = m.group(1).strip()
            m = re.search(r"POST:\s*([^)]+)", s)
            if m:
                meta["post"] = m.group(1).strip()
            m = re.search(r"\(T(\d+)\s+D([\d.]+)", s)
            if m:
                tool_dia = float(m.group(2))
                _t = int(m.group(1))
                meta["tools"][_t] = tool_dia
                # 工具注释里写了“球头”/ “倒角刀”时，回灌必须按实际刀具形状扫掠，
                # 否则球头走圆弧会被当成平底刀“整圈剔平”，倒角会被当成切槽。
                if "球头" in s:
                    meta["tool_shape"][_t] = "ball"
                elif "倒角刀" in s or "V刀" in s:
                    meta["tool_shape"][_t] = "cone"
                else:
                    # 显式写回 flat：否则换回平底刀后仍沿用上一把球头刀的形状
                    meta["tool_shape"][_t] = "flat"
            continue
        if s.startswith(";"):
            continue
        code_part = s.split("(")[0].strip()
        if not code_part:
            continue
        words = {k: float(v) for k, v in _WORD.findall(code_part)}
        letters = set(words)

        if "G" in words:
            g = int(round(words["G"]))
            if g == 90:
                abs_mode = True
            elif g == 91:
                abs_mode = False
            elif g == 20:
                warnings.append(f"第 {i} 行：英制模式 G20")
            elif g in (0, 1):
                rapid = (g == 0)
            elif g in (17, 18, 19):
                plane = g
            elif g == 83:
                # 啄钻：一次下刀到底
                nx_ = words.get("X", x) if abs_mode else x + words.get("X", 0.0)
                ny_ = words.get("Y", y) if abs_mode else y + words.get("Y", 0.0)
                nz_ = words.get("Z", z) if abs_mode else z + words.get("Z", 0.0)
                r_ = words.get("R", 2.0)
                moves.append(Move(x, y, r_, True, i, tool_dia, shape=tool_shape))
                moves.append(Move(nx_, ny_, r_, True, i, tool_dia, shape=tool_shape))
                moves.append(Move(nx_, ny_, nz_, False, i, tool_dia, words.get("F", feed),
                                  shape=tool_shape))
                x, y, z = nx_, ny_, nz_
                continue
            elif g == 80:
                continue
            elif g in (2, 3) and plane == 18:
                # XZ 平面圆弧（G18）：X/Z 终点 + I/K 圆心偏移。
                nx_ = words.get("X", x) if abs_mode else x + words.get("X", 0.0)
                nz_ = words.get("Z", z) if abs_mode else z + words.get("Z", 0.0)
                i_ = words.get("I", 0.0)
                k_ = words.get("K", 0.0)
                if abs(i_) < 1e-9 and abs(k_) < 1e-9:
                    warnings.append(f"第 {i} 行：G18 G0{g} 缺 I/K，按直线近似")
                    moves.append(Move(nx_, y, nz_, False, i, tool_dia, words.get("F", feed),
                                      shape=tool_shape))
                    x, z = nx_, nz_
                    continue
                cx = x + i_
                cz = z + k_
                rho = math.hypot(x - cx, z - cz)
                a0 = math.atan2(z - cz, x - cx)
                a1 = math.atan2(nz_ - cz, nx_ - cx)
                # G18 与 XY 的方向约定相反：FANUC 里 G03 绕 +Y 正向旋转（+Z→+X），
                # 对应 atan2 角度减小；G02 才是角度增大。gcode.arc_move_xz() 同此约定。
                if abs(nx_ - x) < 1e-9 and abs(nz_ - z) < 1e-9:
                    a1 = a0 + (2.0 * math.pi if g == 2 else -2.0 * math.pi)
                elif g == 2:
                    while a1 <= a0:
                        a1 += 2.0 * math.pi
                else:
                    while a1 >= a0:
                        a1 -= 2.0 * math.pi
                _nseg = max(2, int(abs(a1 - a0) * rho / 0.25) + 1)
                for _k in range(1, _nseg + 1):
                    _ak = a0 + (a1 - a0) * _k / _nseg
                    moves.append(Move(cx + rho * math.cos(_ak), y,
                                      cz + rho * math.sin(_ak),
                                      False, i, tool_dia, words.get("F", feed),
                                      shape=tool_shape))
                x, z = nx_, nz_
                continue
            elif g in (2, 3):
                # 圆弧：G02/G03 + I/J。原前这里没处理，圆弧被当直线弦处理，
                # R12 四角的轮廓刀路会切进零件里去（实测假过切 1.86%）。
                nx_ = words.get("X", x) if abs_mode else x + words.get("X", 0.0)
                ny_ = words.get("Y", y) if abs_mode else y + words.get("Y", 0.0)
                i_ = words.get("I", 0.0)
                j_ = words.get("J", 0.0)
                if abs(i_) < 1e-9 and abs(j_) < 1e-9:
                    warnings.append(f"第 {i} 行：G0{g} 缺 I/J，按直线近似")
                    moves.append(Move(nx_, ny_, z, False, i, tool_dia, words.get("F", feed),
                                      shape=tool_shape))
                    x, y = nx_, ny_
                    continue
                cx = x + i_
                cy = y + j_
                rho = math.hypot(x - cx, y - cy)
                a0 = math.atan2(y - cy, x - cx)
                a1 = math.atan2(ny_ - cy, nx_ - cx)
                if abs(nx_ - x) < 1e-9 and abs(ny_ - y) < 1e-9:
                    a1 = a0 + (2.0 * math.pi if g == 3 else -2.0 * math.pi)
                elif g == 3:
                    while a1 <= a0:
                        a1 += 2.0 * math.pi
                else:
                    while a1 >= a0:
                        a1 -= 2.0 * math.pi
                _nseg = max(2, int(abs(a1 - a0) * rho / 0.25) + 1)
                for _k in range(1, _nseg + 1):
                    _ak = a0 + (a1 - a0) * _k / _nseg
                    moves.append(Move(cx + rho * math.cos(_ak), cy + rho * math.sin(_ak), z,
                                      False, i, tool_dia, words.get("F", feed), shape=tool_shape))
                x, y = nx_, ny_
                continue

        if "T" in words:
            cur_tool = int(round(words["T"]))
            if cur_tool in meta["tools"]:
                tool_dia = meta["tools"][cur_tool]
                tool_shape = meta["tool_shape"].get(cur_tool, "flat")
        if "F" in words:
            feed = words["F"]

        has_axis = bool(letters & {"X", "Y", "Z"})
        if not has_axis:
            continue
        nxp = words.get("X", x) if abs_mode else x + words.get("X", 0.0)
        nyp = words.get("Y", y) if abs_mode else y + words.get("Y", 0.0)
        nzp = words.get("Z", z) if abs_mode else z + words.get("Z", 0.0)
        moves.append(Move(nxp, nyp, nzp, rapid, i, tool_dia, feed, shape=tool_shape))
        x, y, z = nxp, nyp, nzp

    if not abs_mode:
        warnings.append("程序未回到 G90 绝对坐标模式")
    return moves, meta, warnings


def _poly_mask(pts: Sequence[Pt], X: np.ndarray, Y: np.ndarray) -> np.ndarray:
    """多段线奇偶判定：点在轮廓内为 True。"""
    n = len(pts)
    inside = np.zeros(X.shape, dtype=bool)
    if n < 3:
        return inside
    j = n - 1
    for i in range(n):
        xi, yi = pts[i]
        xj, yj = pts[j]
        cond = ((yi > Y) != (yj > Y))
        with np.errstate(divide="ignore", invalid="ignore"):
            xint = (xj - xi) * (Y - yi) / np.where(yj - yi == 0, 1e-12, yj - yi) + xi
        inside ^= cond & (X < xint)
        j = i
    return inside


def _footprint_mask(spec: PartSpec, X: np.ndarray, Y: np.ndarray) -> np.ndarray:
    """零件底面轮廓内为 True（多段线奇偶判定，支持圆角矩形/圆）。"""
    mask = np.zeros(X.shape, dtype=bool)
    for pts in flatten(base_outline(spec)):
        mask |= _poly_mask(pts, X, Y)
    return mask


def _expected_top(spec: PartSpec, X: np.ndarray, Y: np.ndarray,
                  inside: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """理论顶面高度（Z0 = 毛坯顶面，向下为负）；thru 标记允许切透的区域。"""
    T = spec.thickness
    top = np.zeros(X.shape, dtype=np.float64)   # 毛坯顶面
    thru = np.zeros(X.shape, dtype=bool)
    top[~inside] = -T                            # 轮廓外整层铣掉
    thru[~inside] = True
    # 抽壳（开口朝上）：内腔沿内缩轮廓挖到 T-shell_t，保留底厚。
    # 不做这一步，回灌侧理论体积会多出整个内腔（实测 fa_shell：36,288 mm³）。
    _st = float(getattr(spec, "shell_t", 0.0) or 0.0)
    if _st > 0 and str(getattr(spec, "shell_open", "top")) == "top" and _st < T:
        try:
            from ..calc import inner_outline
            for _pts in flatten(inner_outline(spec, _st)):
                _sel = _poly_mask(_pts, X, Y)
                top = np.where(_sel, np.minimum(top, -(T - _st)), top)
        except Exception:
            pass
    # Dome is a positive-Z boss above the base top. In CAM coordinates the
    # base top is Z=0 and the stock top is dome_h; the theoretical surface must
    # follow the native elliptical or spherical cap, not a flat face.
    _dh = max(float(getattr(spec, "dome_h", 0.0) or 0.0), 0.0)
    if _dh > 0:
        if spec.base_kind == "round":
            _da = float(spec.diameter) / 2.0
        else:
            _da = min(float(spec.length), float(spec.width)) / 2.0
        if _da > 1e-9:
            _rho2 = X * X + Y * Y
            _dsel = _rho2 <= _da * _da
            if bool(getattr(spec, "dome_elliptic", True)):
                _dsurf = _dh * np.sqrt(np.maximum(1.0 - _rho2 / (_da * _da), 0.0))
            else:
                _dr = (_da * _da + _dh * _dh) / (2.0 * _dh)
                _dsurf = (_dh - _dr) + np.sqrt(np.maximum(_dr * _dr - _rho2, 0.0))
            top = np.where(_dsel, np.maximum(top, _dsurf), top)
    # 扫掠肋（加料体）：只有「顶面肋」这类三轴单面可加工的才在这里表达顶面。
    # 悬空肋的肋底是空气，Z-map 单高度表达不了；那些体在 verify_nc 里单独报工艺
    # 限制并把体积从两套基线同时扣除，绝不假装已加工。
    for _sw in (getattr(spec, "sweeps", None) or []):
        try:
            from ..calc import sweep_machinability as _swm
            from ..calc import sweep_top_z as _swtz
            _si = _swm(spec, _sw)
            _bb = _si.get("bbox")
            if not _si.get("ok") or not _si.get("heightfield") or not _bb:
                continue
            _sel = ((X >= float(_bb[0]) - 1e-9) & (X <= float(_bb[1]) + 1e-9)
                    & (Y >= float(_bb[2]) - 1e-9) & (Y <= float(_bb[3]) + 1e-9))
            if not _sel.any():
                continue
            # sweep 的路径 z 是 CAD 世界坐标（零件底面 z=0、基体顶面 z=T）；
            # verify 的顶面数组以「基体顶面 = 0」为基准，这里统一减 T。
            _zt = _swtz(_sw, X) - float(spec.thickness)
            _sel &= _zt > -1e8
            top = np.where(_sel, np.maximum(top, _zt), top)
        except Exception:
            continue

    for f in spec.features:
        d = feature_depth(spec, f)
        if d <= 0:
            continue
        # 薄壁冲突的孔/槽在建模时被外扩过（spec.grow_of），这里必须同样外扩，
        # 否则"回灌侧理论体积"会比建模侧多出那一圈（实测 +0.51%）。
        try:
            g = spec.grow_of(f)
        except Exception:
            g = 0.0
        if f.type == "hole":
            rr = f.diameter / 2.0 + g
            sel = (X - f.x) ** 2 + (Y - f.y) ** 2 <= rr * rr
        elif f.type == "slot":
            L = max(f.length or f.width, f.width) + 2 * g
            Wd = f.width + 2 * g
            a = math.radians(f.angle)
            ca, sa = math.cos(a), math.sin(a)
            dx, dy = X - f.x, Y - f.y
            u = dx * ca + dy * sa
            v = -dx * sa + dy * ca
            half = (L - Wd) / 2.0
            uu = np.clip(u, -half, half)
            sel = (u - uu) ** 2 + v ** 2 <= (Wd / 2.0) ** 2
        elif f.type == "sphere":
            # 球坑：球心在 z=R-h，表面 z = (R-h) - sqrt(R²-ρ²)；进入这个分支后不能再按平底“-d”切，
            # 否则会把球坑当成直壁圆槽（实测 fa_sphere 差 335 mm³ 且形状不对）。
            RR = max(float(f.diameter) / 2.0, 1e-6)
            hh = min(max(float(f.depth), 0.0), RR)
            if hh > 0:
                rho2 = (X - f.x) ** 2 + (Y - f.y) ** 2
                aa2 = max(RR * RR - (RR - hh) ** 2, 0.0)
                sel = rho2 <= aa2
                surf = (RR - hh) - np.sqrt(np.maximum(RR * RR - rho2, 0.0))
                top = np.where(sel, np.minimum(top, surf), top)
            continue
        else:
            sel = ((np.abs(X - f.x) <= f.length / 2.0 + g)
                   & (np.abs(Y - f.y) <= f.width / 2.0 + g))
        top = np.where(sel, np.minimum(top, -d), top)
        if f.through or f.depth <= 0:
            thru |= sel
    return top, thru


def _boundary_dist(spec: PartSpec, X: np.ndarray, Y: np.ndarray) -> np.ndarray:
    """点集到外轮廓的近似距离（mm，零件内部为正）。

    用于把倒角/圆角那一圈从过切、欠切面积里排除。
    """
    if spec.base_kind == "round":
        return spec.diameter / 2.0 - np.hypot(X, Y)
    L = float(spec.length)
    W = float(spec.width)
    ax = np.abs(X)
    ay = np.abs(Y)
    left = str(getattr(spec, "end_left", "square") or "square").lower()
    right = str(getattr(spec, "end_right", "square") or "square").lower()
    if left == "arc" or right == "arc":
        r = W / 2.0
        xi = max(0.0, L / 2.0 - r)
        d_side = W / 2.0 - ay
        # 左半圆（圆心 -xi）/ 右半圆（圆心 +xi）
        d_arc = np.full(X.shape, np.inf)
        if left == "arc":
            d_arc = np.minimum(d_arc, r - np.hypot(X + xi, Y))
        if right == "arc":
            d_arc = np.minimum(d_arc, r - np.hypot(X - xi, Y))
        d_end = np.minimum(d_arc, L / 2.0 - ax)
        return np.where(ax > xi, d_end, np.minimum(d_side, L / 2.0 - ax))
    cr = min(float(getattr(spec, "corner_radius", 0.0) or 0.0), min(L, W) / 2.0)
    if cr <= 1e-9:
        return np.minimum(L / 2.0 - ax, W / 2.0 - ay)
    dx = np.maximum(ax - (L / 2.0 - cr), 0.0)
    dy = np.maximum(ay - (W / 2.0 - cr), 0.0)
    d_side = np.minimum(L / 2.0 - ax, W / 2.0 - ay)
    return np.where((dx > 0) & (dy > 0), cr - np.hypot(dx, dy), d_side)


def _simulate(moves: Sequence[Move], sl: float, sw: float, T: float,
              cell: float, stock_top: float = 0.0) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Z-map 材料去除仿真；返回 (X 网格, Y 网格, 顶面高度)。"""
    nx = max(2, int(math.ceil(sl / cell)))
    ny = max(2, int(math.ceil(sw / cell)))
    # 单元中心采样：原来采样点正好落在 ±sl/2 的边界线上，边界那一圈会被
    # "在内"判定收进去，footprint 面积比零件大 0.5%（实测 23,120 vs 23,000）。
    xs = np.linspace(-sl / 2.0 + cell / 2.0, sl / 2.0 - cell / 2.0, nx)
    ys = np.linspace(-sw / 2.0 + cell / 2.0, sw / 2.0 - cell / 2.0, ny)
    X, Y = np.meshgrid(xs, ys)
    top = np.full((ny, nx), float(stock_top), dtype=np.float64)

    dxs = xs[1] - xs[0]
    dys = ys[1] - ys[0]
    area = dxs * dys

    prev: Optional[Move] = None
    for mv in moves:
        if prev is None:
            prev = mv
            continue
        cutting = (not mv.rapid) and (mv.z <= stock_top + 0.05 or prev.z <= stock_top + 0.05)
        if cutting:
            r = max(mv.tool_dia, 0.5) / 2.0
            zc = min(mv.z, prev.z)
            y0 = min(prev.y, mv.y) - r
            y1 = max(prev.y, mv.y) + r
            x0 = min(prev.x, mv.x) - r
            x1 = max(prev.x, mv.x) + r
            i0 = max(0, int((y0 + sw / 2.0) / dys) - 1)
            i1 = min(ny - 1, int((y1 + sw / 2.0) / dys) + 1)
            j0 = max(0, int((x0 + sl / 2.0) / dxs) - 1)
            j1 = min(nx - 1, int((x1 + sl / 2.0) / dxs) + 1)
            if i1 >= i0 and j1 >= j0:
                sx = X[i0:i1 + 1, j0:j1 + 1]
                sy = Y[i0:i1 + 1, j0:j1 + 1]
                ax, ay = prev.x, prev.y
                bx, by = mv.x, mv.y
                vx, vy = bx - ax, by - ay
                L2 = vx * vx + vy * vy
                t = np.zeros(sx.shape, dtype=np.float64)
                if L2 < 1e-12:
                    dist = np.hypot(sx - ax, sy - ay)
                    # 纯 Z 向进刀：t 必须取 1（终点），否则 zt 停在起点高度，
                    # 球头刀/倒角刀的垂直下刀完全不切料（实测球坑单刀 sim 少 1,072 mm³）。
                    t = np.ones(sx.shape, dtype=np.float64)
                else:
                    t = np.clip(((sx - ax) * vx + (sy - ay) * vy) / L2, 0.0, 1.0)
                    dist = np.hypot(sx - (ax + t * vx), sy - (ay + t * vy))
                shape = getattr(mv, "shape", "flat")
                hit = dist <= r
                if shape == "ball":
                    # 球头刀：NC 的 Z 是刀尖（与平底刀、倒角刀口径一致）。
                    # 刀面高度 = 刀尖 z + r - sqrt(r²-d²)；轴心处等于刀尖高度。
                    zt = prev.z + t * (mv.z - prev.z)
                    cutz = zt + r - np.sqrt(np.maximum(r * r - dist * dist, 0.0))
                elif shape == "cone":
                    # 90° 倒角刀：刀尖 z_t，半径处高度 z = z_t + d（45°）
                    zt = prev.z + t * (mv.z - prev.z)
                    cutz = zt + dist
                else:
                    cutz = np.full(dist.shape, zc)
                sub = top[i0:i1 + 1, j0:j1 + 1]
                sub[hit] = np.minimum(sub[hit], cutz[hit])
        prev = mv
    return X, Y, top, area


def _profile_extent(moves: Sequence[Move], T: float):
    """从程序里直接量出**精铣**轮廓的位置，反算工件实际外形。

    轮廓铣到底的那几刀会形成若干条闭合回路（粗铣一条、精铣一条）；
    取跨度最大的那一组里**跨度最小的一条**，就是精铣回路。
    刀心轨迹向外偏一个刀半径，所以 工件尺寸 = 刀心跨度 − 2×刀半径。
    轮廓多偏 0.2mm 这里立刻能看出来（Z-map 在轮廓内部看不出 0.2mm 的偏刀）。
    """
    runs: List[List[Move]] = []
    cur: List[Move] = []
    for m in moves:
        deep = (not m.rapid) and m.z <= -(T + 0.2) and m.tool_dia > 0
        if deep:
            cur.append(m)
        else:
            if len(cur) >= 4:
                runs.append(cur)
            cur = []
    if len(cur) >= 4:
        runs.append(cur)
    if not runs:
        return None

    def span(run):
        xs = [m.x for m in run]
        ys = [m.y for m in run]
        return (max(xs) - min(xs)), (max(ys) - min(ys))

    spans = [span(r) for r in runs]
    big = max(spans, key=lambda s: s[0] + s[1])
    limit = 0.8 * (big[0] + big[1])
    cands = [(s, r) for s, r in zip(spans, runs) if (s[0] + s[1]) >= limit]
    s, run = min(cands, key=lambda c: c[0][0] + c[0][1])
    r = max(m.tool_dia for m in run) / 2.0
    return (s[0] - 2 * r, s[1] - 2 * r, r)


def verify_nc(spec: PartSpec, nc_path: str | Path, cell: float = 0.4,
              tol: float = 0.05, margin: float = 6.0,
              model_volume: Optional[float] = None,
              cad_volumes: Optional[Sequence[float]] = None) -> VerifyReport:
    """把 NC 程序回灌成几何，与 PartSpec 对比验证有效性。"""
    rep = VerifyReport(nc_path=str(nc_path))
    p = Path(nc_path)
    if not p.exists():
        rep.problems.append(f"程序不存在：{p}")
        return rep

    moves, meta, warnings = parse_nc(p)
    rep.warnings = warnings
    rep.moves = len(moves)
    rep.lines = len(p.read_text(encoding="utf-8", errors="replace").splitlines())
    T = spec.thickness
    _dh = max(float(getattr(spec, "dome_h", 0.0) or 0.0), 0.0)
    sl, sw, stock_h = stock_size(spec, margin)
    if meta.get("stock"):
        gs = meta["stock"]
        sl, sw = max(sl, gs[0]), max(sw, gs[1])
        if abs(gs[2] - stock_h) > 0.01:
            rep.warnings.append(f"Program stock height {gs[2]:g} differs from geometry stock {stock_h:g}")
    rep.stock = (sl, sw, stock_h)
    rep.part_size = (spec.length, spec.width, T + _dh)

    if not moves:
        rep.problems.append("程序中没有可解析的运动段")
        return rep

    # 刀路越出毛坯 = 撞夹具/台面。原来的判据只看过切/欠切（都在「零件轮廓内」统计），
    # 所以 NC 跑到毛坯外面完全不会被发现：实测箱座识图误给 Φ9@(0,±79)，毛坯只有
    # 230×100（Y 仅 ±50），程序在 Y=±79 下了 6 刀，验证报告却写「整体：通过」。
    ox = max(0.0, max(abs(m.x) for m in moves) - sl / 2.0)
    oy = max(0.0, max(abs(m.y) for m in moves) - sw / 2.0)
    if max(ox, oy) > 0.01:
        xs = [m.x for m in moves]
        ys = [m.y for m in moves]
        bad = [m for m in moves if abs(m.x) > sl / 2.0 + 0.01
               or abs(m.y) > sw / 2.0 + 0.01]
        rep.problems.append(
            f"刀路越出毛坯 {max(ox, oy):.2f} mm（超 0.01 即判废）：程序坐标 X {min(xs):.2f}..{max(xs):.2f}"
            f" / Y {min(ys):.2f}..{max(ys):.2f}，毛坯 {sl:.2f}×{sw:.2f}"
            f"（允许 X ±{sl / 2.0:.2f} / Y ±{sw / 2.0:.2f}）；共 {len(bad)} 段出现，会撞夹具/台面")

    rep.max_depth = -min(m.z for m in moves)
    # 通孔允许钻穿（留 3 mm 让钻尖出头），非通孔超深才算问题
    max_allowed = T + (3.5 if any(f.type == "hole" and (f.through or f.depth <= 0)
                                  for f in spec.features) else 0.5)
    if rep.max_depth > max_allowed:
        rep.problems.append(f"最大切深 {rep.max_depth:.2f} mm 超过允许值 {max_allowed:.1f} mm（扎穿）")

    for t, d in sorted(meta["tools"].items()):
        rep.tool_summary.append(f"T{t} ?{d:g}")
    if not rep.tool_summary:
        rep.tool_summary.append("未解析到刀具表")

    # 毛坯顶面在 CAM 坐标里的偏移：圆顶/扫掠肋谁高取谁（基体顶面 = 0）。
    _sw_top = 0.0
    try:
        from ..calc import sweep_bbox as _swbb2
        _swb = _swbb2(spec)
        if _swb:
            _sw_top = max(0.0, float(_swb[5]) - T)
    except Exception:
        _sw_top = 0.0
    X, Y, top, area = _simulate(moves, sl, sw, T, cell, stock_top=max(_dh, _sw_top))

    inside = _footprint_mask(spec, X, Y)
    # 收缩一圈，避开轮廓边界的网格效应
    er = inside.copy()
    for _ in range(2):
        e = er.copy()
        e[1:, :] &= er[:-1, :]
        e[:-1, :] &= er[1:, :]
        e[:, 1:] &= er[:, :-1]
        e[:, :-1] &= er[:, 1:]
        er = e
    if not er.any():
        er = inside

    want, thru = _expected_top(spec, X, Y, inside)
    cell_area = area
    rep.footprint_area = float(inside.sum() * cell_area)

    # 悬空/形态不支持的扫掠体：三轴单面从上方做不出来。_expected_top 只表达
    # 「顶面肋」，这些体没有进 want；两套理论基线必须同时扣掉它们的解析体积，
    # 否则基线互校会拿"缺了肋的 want"去比"含肋的模型"，报一个假的不一致。
    # 同时留一条明确 problem，绝不因为扣了体积就让门禁通过。
    _sweep_unm_v = 0.0
    try:
        import types as _t
        from ..calc import sweep_machinability as _swm2, sweep_added_volume as _swvol2
        for _i, _sw in enumerate((getattr(spec, "sweeps", None) or []), 1):
            _si = _swm2(spec, _sw)
            if _si.get("ok"):
                continue
            _sv = float(_swvol2(_t.SimpleNamespace(sweeps=[_sw])))
            _sweep_unm_v += max(_sv, 0.0)
            rep.problems.append(
                f"扫掠体 {_i} 未加工：{_si.get('reason') or '形态不支持'}"
                f"（该肋体积 {max(_sv, 0.0):.0f} mm³ 已从两套理论基线同时扣除）")
    except Exception:
        pass

    _has_unm_sweep = _sweep_unm_v > 1e-6
    if _has_unm_sweep:
        rep.warnings.append(
            "存在不可三轴加工的扫掠体：仿真包含未去除的毛坯，报告不做体积/过欠切比对；"
            "该肋须侧铣、翻面或第4轴，当前程序不能判为完成")

    # 毛坯周边残料：轮廓之外仍有未被切到的毛坯（说明没做清边）
    outside_left = (top > -T + tol) & (~inside)
    rep.blank_left_pct = 100.0 * float(outside_left.sum()) / max(float((~inside).sum()), 1.0)

    diff = top - want
    # 通孔/通槽允许切穿到底，不算过切
    over = (diff < -tol) & er & (~thru)
    under = (diff > tol) & er
    if _has_unm_sweep:
        over[:] = False
        under[:] = False
    # 倒角/轮廓圆角是「沿一圈棱去除」：_expected_top 表达不了，体积侧已按
    # edge_feature_volume 单独扣过，这里必须把那一圈从过/欠切面积里排除，
    # 否则 C5 的刀路会被算成 9% 假过切（实测）。
    _edge = max(float(getattr(spec, "chamfer", 0.0) or 0.0),
                float(getattr(spec, "fillet", 0.0) or 0.0))
    if _edge > 0:
        try:
            _bd = _boundary_dist(spec, X, Y)
            _band = _bd < (_edge + 2.0 * cell)
            over = over & (~_band)
            under = under & (~_band)
        except Exception:
            pass
    rep.overcut_area = float(over.sum() * cell_area)
    rep.undercut_area = float(under.sum() * cell_area)
    denom = max(rep.footprint_area, 1e-9)
    rep.overcut_pct = 100.0 * rep.overcut_area / denom
    rep.undercut_pct = 100.0 * rep.undercut_area / denom

    # 仿真工件尺寸 = 轮廓内保留到零件顶面（未被切掉）的区域范围
    keep = inside & (top > -T + tol)
    if keep.any():
        ys_i, xs_i = np.where(keep)
        rep.sim_size = (float(X[ys_i, xs_i].max() - X[ys_i, xs_i].min()),
                        float(Y[ys_i, xs_i].max() - Y[ys_i, xs_i].min()),
                        float(-top[keep].min()))

    # 轮廓壁位置（程序反算）优先，能抓 0.05mm 级的偏刀
    ext = _profile_extent(moves, T)
    rep.wall_size = ()
    if ext is not None:
        wx, wy, rw = ext
        rep.wall_size = (round(wx, 3), round(wy, 3))
        rep.sim_size = (wx, wy, rep.sim_size[2])
        rep.size_error = (wx - spec.length, wy - spec.width)
        if spec.base_kind != "round" and (abs(rep.size_error[0]) > 0.05
                                          or abs(rep.size_error[1]) > 0.05):
            rep.problems.append(
                f"轮廓壁位置偏差：程序反算 {wx:.3f}×{wy:.3f}，图纸 {spec.length:g}×{spec.width:g}"
                f"（单边 {(wx - spec.length) / 2:+.3f} / {(wy - spec.width) / 2:+.3f} mm，"
                f"刀径 Ø{rw * 2:g}）")
    else:
        rep.size_error = (rep.sim_size[0] - spec.length, rep.sim_size[1] - spec.width)
    # 栅格掩膜面积与解析面积有 ±0.5% 的边界偏差（cell=0.4 时一圈格子），
    # 按解析面积等比校正，否则 want_volume 和解析参考永远差 0.5% 量级。
    try:
        from ..calc import base_area as _base_area
        _a_geo = _base_area(spec)
        _a_mask = float(inside.sum() * cell_area)
        _scale = (_a_geo / _a_mask) if _a_mask > 0 else 1.0
    except Exception:
        _scale = 1.0

    # 只统计零件轮廓内的材料，否则毛坯周边残料会把"仿真体积"撑得比理论值大很多
    rep.sim_volume = float(((top + T).clip(min=0) * inside).sum() * cell_area) * _scale
    # 倒角/圆角是"沿一圈棱去除"，_expected_top 只管顶面高度表达不了，单独扣掉。
    # 理论体积另外用细网格算（cell/4）：0.4mm 网格对 28mm 宽的槽会差一格宽（~1.4%），
    # 拿粗网格当"理论值"会让两套基线永远差 0.5% 量级。
    try:
        from ..calc import edge_feature_volume as _efv
        _efv_v = _efv(spec)
    except Exception:
        _efv_v = 0.0
    try:
        _fine = max(0.05, cell / 4.0)
        _nfx = max(2, int(math.ceil(sl / _fine)))
        _nfy = max(2, int(math.ceil(sw / _fine)))
        _fx = np.linspace(-sl / 2.0 + _fine / 2.0, sl / 2.0 - _fine / 2.0, _nfx)
        _fy = np.linspace(-sw / 2.0 + _fine / 2.0, sw / 2.0 - _fine / 2.0, _nfy)
        _FX, _FY = np.meshgrid(_fx, _fy)
        _fin = _footprint_mask(spec, _FX, _FY)
        _fwant, _ = _expected_top(spec, _FX, _FY, _fin)
        _farea = float(_fx[1] - _fx[0]) * float(_fy[1] - _fy[0])
        rep.want_volume = float(((_fwant + T).clip(min=0) * _fin).sum() * _farea) - _efv_v
    except Exception:
        rep.want_volume = (float(((want + T).clip(min=0) * inside).sum() * cell_area) * _scale
                           - _efv_v)

    # CAM 没有倒角/圆角工序时，理论侧不能扣这块体积，否则"理论扣了但没人做"会
    # 伪装成刀路过切（实测 m3：chamfer=5 时差 +1.75%，清掉后 0.03%）。
    #
    # 注意：加回 efv 之后，建模侧 part_volume() 也要加回同一个量，否则两套理论
    # 基线会重新分叉，差值正好等于 efv（实测 m3：285,900 vs 281,070，差 4,830
    # ≈ edge_feature_volume 4,833，基线互校立刻报 1.72%）。所以把加回的量记进
    # _edge_addback，下面做基线互校时对 _model_v 用同一个量对齐。
    _edge_addback = 0.0
    try:
        from ..calc import edge_feature_volume as _efv2
        _edge_v = float(_efv2(spec))
        _txt = Path(nc_path).read_text(encoding="utf-8", errors="ignore")
        _has_edge_op = ("倒角" in _txt) or ("圆角" in _txt)
        if _edge_v > 0 and not _has_edge_op:
            rep.want_volume += _edge_v
            _edge_addback = _edge_v
            _what = []
            if getattr(spec, "chamfer", 0) > 0:
                _what.append("C%g" % spec.chamfer)
            if getattr(spec, "fillet", 0) > 0:
                _what.append("R%g" % spec.fillet)
            rep.warnings.append(
                "加工程序未含倒角/圆角工序（%s）：理论体积已相应加回 %.0f mm³ 再比，"
                "这部分体积不是刀路误差" % ("/".join(_what), _edge_v))
    except Exception:
        pass

    # 两套理论基线互校：建模侧 part_volume() 与回灌侧 want_volume 必须一致，
    # 否则"各自和自己比"都能通过，差异被 ±3% 阈值吞掉（实测差 0.63% 没人发现）。
    try:
        from ..calc import part_volume as _pv, volume_reference as _vref
        # 建模侧已经算过一次（BuildResult.expected），优先复用，别重复算。
        # part_volume() 内部已扣 edge_feature_volume；上面 want 侧在「没有倒角工序」
        # 时把这块加回去了，这里必须同步加回，否则两边差一个 efv（实测 m3 1.72%）。
        _ref_v, _ref_kind = _vref(spec)
        # 抽壳开口朝下/朝侧：内腔要翻面或侧铣才能做，单序回灌的 want 里没有这部分料。
        # 两套基线必然差一个内腔体积，这时比"基线互校"没有意义，直接跳过并说明。
        _so_chk = str(getattr(spec, "shell_open", "top") or "top").lower()
        _shell_off_top = (float(getattr(spec, "shell_t", 0.0) or 0.0) > 0
                          and _so_chk not in ("top", ""))
        if _shell_off_top:
            rep.warnings.append(
                "抽壳开口非朝上（%s）：该内腔不在单序刀路内，已跳过两套理论基线互校" %
                (_so_chk or "none"))
            raise StopIteration
        _model_v = ((float(model_volume) if model_volume else _ref_v)
                    + _edge_addback - _sweep_unm_v)
        rep.baseline_gap_src = _ref_kind
        _ref_kind_is_exact = (_ref_kind == "exact")
        if _model_v > 0:
            _gap = abs(rep.want_volume - _model_v) / _model_v
            rep.baseline_gap_pct = 100.0 * _gap
            # 两套都是栅格实现时（特征交叠、解析式算不了），0.4~0.6% 的差是
            # 域/掩膜实现不同造成的固有差，不是 bug；超过 1% 才当实现不一致。
            # 阈值统一 0.5%：+0.511% 那个系统偏差已定位并修掉（verify 的 want 没算
            # 薄壁外扩 grow），不再需要靠放宽阈值掩盖。来源仍写进 baseline_gap_src。
            _lim = 0.005
            if _gap > _lim:
                rep.problems.append(
                    f"两套理论基线不一致：回灌侧 {rep.want_volume:,.0f} mm³ vs 建模侧 "
                    f"{_model_v:,.0f} mm³（差 {100.0 * _gap:.2f}%，阈值 {100 * _lim:.1f}%）")
    except StopIteration:
        pass
    except Exception:
        pass

    # ---- 用 CAD 实测当裁判：want / expected 各自和 CAD 比，比"两套理论互比"更能定责
    try:
        cads = [float(v) for v in (cad_volumes or []) if v and v > 0]
        if cads:
            cad_mean = sum(cads) / len(cads)
            rep.cad_volume = round(cad_mean, 1)
            rep.cad_n = len(cads)
            _w = abs(rep.want_volume - cad_mean) / cad_mean
            rep.want_vs_cad_pct = 100.0 * _w
            if _w > 0.005:
                rep.problems.append(
                    f"回灌侧理论体积与 CAD 实测差 {100 * _w:.3f}%："
                    f"want {rep.want_volume:,.0f} vs CAD 均值 {cad_mean:,.0f}")
            if model_volume:
                _e = abs(float(model_volume) - cad_mean) / cad_mean
                rep.expected_vs_cad_pct = 100.0 * _e
                if _e > 0.005:
                    rep.problems.append(
                        f"建模侧理论体积与 CAD 实测差 {100 * _e:.3f}%："
                        f"expected {float(model_volume):,.0f} vs CAD 均值 {cad_mean:,.0f}")
    except Exception:
        pass

    # 孔位核对
    drilled = {(round(m.x, 2), round(m.y, 2)) for m in moves if (not m.rapid) and m.z < -0.5}
    for f in spec.build_features():      # 与 CAM/建模同源（重复表达的特征不再要求下刀）
        if f.type != "hole":
            continue
        rep.holes_total += 1
        hit = any(abs(mx - f.x) <= 0.6 and abs(my - f.y) <= 0.6 for mx, my in drilled)
        if hit:
            rep.holes_ok += 1
        else:
            rep.problems.append(f"孔 ?{f.diameter:g} @({f.x:g},{f.y:g}) 未在程序中下刀")

    if rep.overcut_pct > 1.0:
        rep.problems.append(
            f"过切面积 {rep.overcut_pct:.2f}%（{rep.overcut_area:,.0f} mm²）"
            f"，单边约 {max(abs(rep.size_error[0]), abs(rep.size_error[1])) / 2:.3f} mm")
    if rep.undercut_pct > 3.0:
        rep.problems.append(f"欠切面积 {rep.undercut_pct:.2f}%，有区域未加工到位")
    for nm, err in (("长", rep.size_error[0]), ("宽", rep.size_error[1])):
        if abs(err) > 0.3:
            rep.problems.append(
                f"{nm}度偏差 {err:+.3f} mm（超 ±0.3）：工件实际被切到 "
                f"{rep.sim_size[0]:.2f}×{rep.sim_size[1]:.2f}，图纸 {spec.length:g}×{spec.width:g}；"
                f"轮廓刀路未把毛坯余量清完（残料约 {err / 2:.2f} mm/边）")

    # 体积一致性：过切/欠切只统计「轮廓内的逐点高度差」，漏掉「整块该切而未切的
    # 区域」或「阶深整体切浅」这类错误，会出现仿真体积是理论值 2.5 倍却判通过的
    # 假 PASS（实测 21,934 mm³ vs 8,629 mm³）。体积必须单独卡。
    if rep.want_volume > 1e-6 and not _has_unm_sweep:
        rep.volume_error_pct = 100.0 * (rep.sim_volume - rep.want_volume) / rep.want_volume
        if abs(rep.volume_error_pct) > 3.0:
            rep.problems.append(
                f"仿真体积 {rep.sim_volume:,.0f} mm³ 与理论 {rep.want_volume:,.0f} mm³ "
                f"偏差 {rep.volume_error_pct:+.1f}%（超 ±3%）：程序切出的形状与模型不一致")

    rep.ok = not rep.problems
    return rep


def verify_dir(spec: PartSpec, nc_path: str | Path, out_md: Optional[str | Path] = None) -> VerifyReport:
    rep = verify_nc(spec, nc_path)
    if out_md:
        Path(out_md).write_text(rep.markdown(), encoding="utf-8")
    return rep
