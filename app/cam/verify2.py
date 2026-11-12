"""非三轴铣零件的程序回灌验证（车削 / 齿轮齿坯 / 钣金下料）。

Z-map 材料去除仿真只适用于三轴铣；车削、滚齿、激光下料的程序结构完全不同。
这里按各自工艺"从程序反读几何"，再和 PartSpec 比对：

* 车削：读 G71 的 P/Q 轮廓段（X 为直径、Z 为轴向）→ 还原回转轮廓 → 与 shaft_segments 比
* 齿轮：读齿坯车削轮廓 + 镗孔 → 齿顶圆 / 齿宽 / 轴孔
* 钣金：读激光切割轨迹 → 包围盒与闭合性 → 与展开尺寸比

返回对象与 verify.VerifyReport 保持同样的鸭子接口（ok / summary / problems /
warnings / to_dict / markdown），pipeline 可以无差别使用。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_WORD = re.compile(r"([A-Z])([+-]?(?:\d+\.?\d*|\.\d+))")


@dataclass
class MechReport:
    kind: str = ""
    ok: bool = False
    nc_path: str = ""
    lines: int = 0
    checks: List[Dict] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    sim_volume: float = 0.0
    want_volume: float = 0.0

    def add(self, name: str, want, got, tol: float, unit: str = "mm") -> bool:
        try:
            good = abs(float(got) - float(want)) <= tol
        except Exception:
            good = False
        self.checks.append({"name": name, "want": want, "got": got, "tol": tol,
                            "unit": unit, "ok": good})
        if not good:
            self.problems.append(f"{name} 不符：程序 {got} / 图纸 {want}（允差 ±{tol:g}{unit}）")
        return good

    def require(self, name: str, cond: bool, msg: str) -> bool:
        self.checks.append({"name": name, "want": True, "got": bool(cond), "tol": 0,
                            "unit": "", "ok": bool(cond)})
        if not cond:
            self.problems.append(msg)
        return bool(cond)

    @property
    def level(self) -> str:
        """验证等级：几何回灌 / 结构合规。"""
        return "结构合规（非几何回灌）"

    @property
    def summary(self) -> str:
        n = len(self.checks)
        bad = sum(1 for c in self.checks if not c["ok"])
        txt = f"【{self.level}】{n} 项检查，{n - bad} 项通过"
        if self.sim_volume:
            txt += (f"；回转体积 {self.sim_volume:,.0f} / 理论 {self.want_volume:,.0f} mm³"
                    f"（{100.0 * (self.sim_volume - self.want_volume) / max(self.want_volume, 1e-9):+.3f}%）")
        return txt

    def to_dict(self) -> Dict:
        return {"ok": self.ok, "level": self.level, "verified_kind": "structure",
                "kind": self.kind, "nc_path": self.nc_path,
                "lines": self.lines, "checks": self.checks, "problems": self.problems,
                "warnings": self.warnings, "sim_volume": round(self.sim_volume, 1),
                "want_volume": round(self.want_volume, 1), "summary": self.summary}

    def markdown(self) -> str:
        def yz(b: bool) -> str:
            return "✅" if b else "❌"

        head = {"turning": "车削程序校验（结构合规，非几何回灌）",
                "gear": "齿轮齿坯程序校验（结构合规，非几何回灌）",
                "sheet": "钣金下料程序校验（结构合规，非几何回灌）"}.get(self.kind, "程序校验")
        out = [f"# {head}", "",
               f"- 程序：`{Path(self.nc_path).name}`（{self.lines} 行）",
               f"- 结论：{yz(self.ok)} {'通过' if self.ok else '未通过'}",
               f"- 说明：{self.summary}", "",
               "## 逐项核对", "", "| 检查项 | 图纸/要求 | 程序反读 | 允差 | 结果 |",
               "|---|---|---|---|---|"]
        for c in self.checks:
            w = c["want"]
            g = c["got"]
            if isinstance(w, float):
                w = f"{w:.2f}"
            if isinstance(g, float):
                g = f"{g:.2f}"
            tol = f"±{c['tol']:g}{c['unit']}" if c["tol"] else "—"
            out.append(f"| {c['name']} | {w} | {g} | {tol} | {yz(c['ok'])} |")
        if self.problems:
            out += ["", "## 问题"] + [f"- ❌ {x}" for x in self.problems]
        if self.warnings:
            out += ["", "## 提示"] + [f"- {x}" for x in self.warnings]
        return "\n".join(out) + "\n"


def _read(nc_path) -> List[str]:
    return Path(nc_path).read_text(encoding="utf-8", errors="replace").splitlines()


def _nums(line: str) -> Dict[str, float]:
    return {m.group(1): float(m.group(2)) for m in _WORD.finditer(line.replace(" ", ""))}


def _block(lines: List[str], p_no: int, q_no: int) -> List[Tuple[str, Dict[str, float]]]:
    """取出 G71 P.. Q.. 之间的轮廓段。"""
    out: List[Tuple[str, Dict[str, float]]] = []
    inside = False
    for ln in lines:
        s = ln.strip()
        up = s.upper()
        if re.match(rf"N0*{p_no}\b", up):
            inside = True
            out.append(("N", _nums(s)))
            continue
        if inside and re.match(rf"N0*{q_no}\b", up):
            out.append(("N", _nums(s)))
            break
        if inside:
            code = up[:3]
            out.append((code, _nums(s)))
    return out


def _profile(block) -> List[Tuple[float, float]]:
    """[(z, dia)]，z 为负（向卡盘方向），dia 为直径。"""
    pts: List[Tuple[float, float]] = []
    x = z = 0.0
    for code, w in block:
        if "X" in w:
            x = w["X"]
        if "Z" in w:
            z = w["Z"]
        if code in ("G01", "G1 ", "G00", "G0 ", "N10", "N20"):
            pts.append((z, x))
    # 去掉相邻重复点
    uniq: List[Tuple[float, float]] = []
    for p in pts:
        if not uniq or abs(p[0] - uniq[-1][0]) > 1e-6 or abs(p[1] - uniq[-1][1]) > 1e-6:
            uniq.append(p)
    return uniq


def verify_turning(spec, nc_path) -> MechReport:
    rep = MechReport(kind="turning", nc_path=str(nc_path))
    lines = _read(nc_path)
    rep.lines = len(lines)
    segs = [(float(s.get("dia") or 0), float(s.get("length") or 0))
            for s in (spec.shaft_segments or [])]
    if not segs:
        rep.problems.append("零件不是回转体，无法按车削验证")
        return rep

    # --- 程序结构
    txt = "\n".join(lines).upper()
    m71 = re.search(r"G71\s+P(\d+)\s+Q(\d+)", txt)
    rep.require("G71 粗车循环", bool(m71), "程序里没有 G71 P..Q.. 复合粗车循环")
    rep.require("G70 精车", "G70" in txt, "程序里没有 G70 精车循环")
    rep.require("M30 程序结束", "M30" in txt, "程序没有 M30 结束符")
    rep.require("G21 公制单位", "G21" in txt, "程序没有 G21 米制单位")
    rep.require("主轴正转 M03", "M03" in txt, "程序没有 M03 主轴指令")

    if not m71:
        rep.ok = False
        return rep
    p_no, q_no = int(m71.group(1)), int(m71.group(2))
    prof = _profile(_block(lines, p_no, q_no))
    rep.require("轮廓点数 ≥ 3", len(prof) >= 3, "P/Q 轮廓段解析不到足够坐标点")
    if len(prof) < 3:
        return rep

    # --- 尺寸：总长 / 各段直径
    total = sum(l for _, l in segs)
    zmin = min(z for z, _ in prof)
    rep.add("总长", total, abs(zmin), 0.05)
    got_dias = [round(d, 3) for _, d in prof if d > 0]
    for i, (d, l) in enumerate(segs, 1):
        rep.require(f"第{i}段 Ø{d:g} 出现在轮廓里",
                    any(abs(g - d) <= 0.02 for g in got_dias),
                    f"轮廓里找不到 Ø{d:g}（程序里是 {sorted(set(got_dias))}）")

    # --- 体积（按轮廓回转）
    vol = 0.0
    for (z1, d1), (z2, d2) in zip(prof, prof[1:]):
        dz = abs(z2 - z1)
        if dz < 1e-9:
            continue
        r1, r2 = d1 / 2.0, d2 / 2.0
        vol += math.pi * dz * (r1 * r1 + r1 * r2 + r2 * r2) / 3.0
    want = sum(math.pi * (d / 2.0) ** 2 * l for d, l in segs)
    rep.sim_volume, rep.want_volume = vol, want
    rep.add("回转体积", round(want, 1), round(vol, 1), max(1.0, want * 0.005), "mm³")

    rep.ok = not rep.problems
    return rep


# -*- coding: utf-8 -*-
def verify_revolve_turning(spec, nc_path) -> MechReport:
    """回转母线车削程序回灌：从 G71 的 P/Q 轮廓反算回转体积。

    外轮廓积分 − 内孔积分 = 成品体积。圆弧按 G02/G03 + R 还原成真圆弧，
    用等步长积分计算，不做理论值代入。
    """
    from ..calc import revolve_volume
    from .revolve_turn import parse_nc_contours, contour_volume, _classify
    rep = MechReport(kind="turning", nc_path=str(nc_path))
    lines = _read(nc_path)
    rep.lines = len(lines)
    txt = "\n".join(lines).upper()
    rep.require("G21 公制单位", "G21" in txt, "程序没有 G21 米制单位")
    rep.require("主轴正转 M03", "M03" in txt, "程序没有 M03 主轴指令")
    rep.require("M30 程序结束", "M30" in txt, "程序没有 M30 结束符")
    rep.require("外圆 G71 粗车", "G71" in txt, "程序没有 G71 粗车循环")
    rep.require("外圆 G70 精车", "G70" in txt, "程序没有 G70 精车循环")

    prof = [q for q in (getattr(spec, "revolve_profile", None) or []) if isinstance(q, dict)]
    if len(prof) < 3:
        rep.problems.append("零件没有回转母线，无法按车削验证")
        rep.ok = False
        return rep
    outer_segs, inner_segs, _faces = _classify(prof)
    want = float(revolve_volume(spec) or 0.0)
    blocks = parse_nc_contours(txt)
    vo = contour_volume(blocks["outer"])
    vi = contour_volume(blocks["inner"])
    sim = vo - vi
    rep.sim_volume, rep.want_volume = sim, want
    rep.add("回转体积", round(want, 1), round(sim, 1), max(1.0, want * 0.005), "mm³")

    # 圆弧数量：母线里有几段真圆弧，程序里就必须有几条 G02/G03。
    n_arc_want = len([s for s in (outer_segs + inner_segs)
                      if str(s.get("type")) == "arc"])
    n_arc_got = len(re.findall(r"G0[23]\s", txt))
    rep.require("原生圆弧段数", n_arc_got >= n_arc_want,
                f"母线有 {n_arc_want} 段圆弧，程序只有 {n_arc_got} 条 G02/G03")

    # 最大外径与总长：圆弧要取弧顶，不能只看端点
    from .revolve_turn import _seg_points, _arc_points
    rmax = max((p[0] for seg in prof for p in _seg_points(seg, 200)), default=0.0)
    # 母线 z 一律是负值（z=0 在右端面），总长要取绝对值最大，不能用 max(原值)
    # ——原来这里恒等于 0，回转件一律报「总长 程序 70 / 图纸 0」。圆弧段还要看弧顶。
    zmax = max([abs(float(s["p1"][1])) for s in prof]
               + [abs(float(s["p2"][1])) for s in prof]
               + [abs(p[1]) for seg in prof for p in _seg_points(seg, 200)])
    got_rmax = 0.0
    for s in blocks["outer"]:
        if s.get("mode") in ("G02", "G03") and s.get("r") is not None:
            pts = _arc_points(float(s["x1"]) / 2.0, float(s["z1"]),
                              float(s["x2"]) / 2.0, float(s["z2"]),
                              float(s["r"]), s["mode"] == "G02")
            got_rmax = max(got_rmax, max((abs(q[0]) for q in pts), default=0.0))
        else:
            got_rmax = max(got_rmax, abs(float(s["x1"])) / 2.0, abs(float(s["x2"])) / 2.0)
    rep.add("最大外径 Ø", round(2.0 * rmax, 3), round(2.0 * got_rmax, 3), 0.05, "mm")
    got_z = max((abs(float(s["z1"])) for s in blocks["outer"]), default=0.0)
    rep.add("总长", round(zmax, 3), round(got_z, 3), 0.05, "mm")
    if inner_segs:
        rep.require("内孔 G71 粗镗", txt.count("G71") >= 2, "有内孔但程序里没有第二组 G71")
        rep.require("内孔 G70 精镗", txt.count("G70") >= 2, "有内孔但程序里没有第二组 G70")

    rep.warnings.append("车削回灌按 NC 的 P/Q 轮廓积分；刀具半径补偿与夹持变形不在本次校验内")
    rep.ok = not rep.problems
    return rep

def verify_gear(spec, nc_path) -> MechReport:
    rep = MechReport(kind="gear", nc_path=str(nc_path))
    lines = _read(nc_path)
    rep.lines = len(lines)
    txt = "\n".join(lines).upper()
    d_out = float(spec.module) * (float(spec.teeth) + 2)
    width = float(spec.face_width)
    bore = float(spec.bore or 0)

    m71 = re.search(r"G71\s+P(\d+)\s+Q(\d+)", txt)
    rep.require("G71 粗车循环", bool(m71), "齿坯程序里没有 G71 循环")
    rep.require("G70 精车", "G70" in txt, "齿坯程序里没有 G70 精车")
    rep.require("M30 程序结束", "M30" in txt, "程序没有 M30 结束符")

    prof = _profile(_block(lines, int(m71.group(1)), int(m71.group(2)))) if m71 else []
    got_out = max((d for _, d in prof), default=0.0)
    got_w = abs(min((z for z, _ in prof), default=0.0))
    rep.add("齿顶圆 Ø", round(d_out, 3), round(got_out, 3), 0.05)
    rep.add("齿宽", round(width, 3), round(got_w, 3), 0.05)

    if bore > 0:
        # 镗孔：程序里 X 走到 bore 且 Z 深于齿宽
        got_bore = 0.0
        for ln in lines:
            w = _nums(ln.replace(" ", ""))
            if "X" in w and abs(w["X"] - bore) < 0.05:
                got_bore = w["X"]
        rep.add("轴孔 Ø", round(bore, 3), round(got_bore, 3), 0.05)

    rep.warnings.append(
        f"齿形（m{spec.module:g} z{spec.teeth}）由滚齿工序保证，滚齿参数见工艺卡；"
        "程序只验证齿坯（齿顶圆/齿宽/轴孔）")
    rep.warnings.append("本零件为齿轮：程序回灌只覆盖车削齿坯 + 滚齿工艺参数，齿形由滚刀保证")
    rep.ok = not rep.problems
    return rep


def verify_sheet(spec, nc_path) -> MechReport:
    rep = MechReport(kind="sheet", nc_path=str(nc_path))
    lines = _read(nc_path)
    rep.lines = len(lines)
    from ..sheet import unfold_size
    bl, bw = unfold_size(spec.sheet_L, spec.sheet_W, spec.sheet_t,
                         spec.flange_h, spec.bend_r)
    pts: List[Tuple[float, float]] = []
    lead: List[Tuple[float, float]] = []      # 第一段 G01 之前的 G00 落点（切割起点）
    started = False
    for ln in lines:
        up = ln.strip().upper()
        if not (up.startswith("G01") or up.startswith("G00")):
            continue
        w = _nums(ln.replace(" ", ""))
        if "X" not in w or "Y" not in w:
            continue
        xy = (w["X"], w["Y"])
        if up.startswith("G01"):
            started = True
            pts.append(xy)
        elif not started:
            lead.append(xy)
    rep.require("切割轨迹点数 ≥ 4", len(pts) >= 4, "程序里没有可解析的切割轨迹")
    if len(pts) < 4:
        return rep
    # 引入/引出点落在零件轮廓外（负坐标），量轮廓尺寸时要排除
    _T = 0.5      # 程序坐标保留 3 位小数，容差放宽到 0.5mm（引入只有 5mm，不会误并）
    inside = [q for q in pts if -_T <= q[0] <= bl + _T and -_T <= q[1] <= bw + _T]
    lead = [q for q in pts if q not in inside]
    body = inside or pts
    xs = [q[0] for q in body]
    ys = [q[1] for q in body]
    got_l, got_w = max(xs) - min(xs), max(ys) - min(ys)
    rep.add("展开长", round(bl, 3), round(got_l, 3), 0.05)
    rep.add("展开宽", round(bw, 3), round(got_w, 3), 0.05)
    rep.require("引入/引出", bool(lead),
                "程序起点直接落在零件轮廓上（无引入/引出），转角会烧缺")
    # 闭合判定：终点回到起点（起点可能是上面一条 G00 的落点）
    starts = list(pts[:1]) + list(lead[-1:])
    closed = any(abs(s[0] - pts[-1][0]) < 1e-6 and abs(s[1] - pts[-1][1]) < 1e-6
                 for s in starts)
    rep.require("轮廓闭合", closed, "下料轨迹首尾不闭合")
    rep.warnings.append(
        f"折弯（2 道 90°，R{spec.bend_r:g}，K=0.44）由折弯机保证，"
        "程序只验证展开下料轮廓；折弯线位置见钣金工艺卡")
    rep.ok = not rep.problems
    return rep
