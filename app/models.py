"""Core data model for SmartModel AI.

A PartSpec is the neutral intermediate representation used by every CAD
adapter (NX / SolidWorks / CATIA) and by the CAM engine.  All dimensions are
millimetres.  The part origin sits at the centre of the base footprint on the
bottom face; +Z points up.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


def _f(v, default=0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


@dataclass
class Feature:
    type: str = "hole"          # hole | pocket | slot | step | boss
    x: float = 0.0              # centre X (mm, relative to part centre)
    y: float = 0.0              # centre Y
    diameter: float = 0.0       # holes
    length: float = 0.0         # pockets/slots/steps (X size)
    width: float = 0.0          # pockets/slots (Y size)
    depth: float = 0.0          # 0 => through
    through: bool = True
    counterbore_dia: float = 0.0
    counterbore_depth: float = 0.0
    corner_radius: float = 0.0
    end_left: str = "square"     # square | arc（圆弧头，半径 W/2）
    end_right: str = "square"
    angle: float = 0.0          # slot orientation, degrees CCW
    datum: str = ""             # 孔位基准：center|pocket_wall|step_edge|bolt_circle|unknown
    datum_ref: str = ""         # 基准对象描述（如 "型腔168×74" / "台阶边x=79"）
    note: str = ""

    def is_through(self) -> bool:
        """通/盲的唯一判据：有正深度就是盲孔，depth<=0 才是通孔。

        实测坑：识图给了 depth=9 但 through=True，CAM 按通孔钻到 Z-38（多钻 29mm）。
        深度比布尔标志更可靠，所以这里以深度为准。
        """
        return float(self.depth or 0.0) <= 0.0

    def key(self) -> tuple:
        return (self.type, round(self.x, 3), round(self.y, 3),
                round(self.diameter, 3), round(self.length, 3),
                round(self.width, 3), round(self.depth, 3))

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["through"] = self.is_through()      # 深度优先，序列化时统一口径
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in d.items()}

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Feature":
        t = str(d.get("type", "hole")).lower().strip()
        alias = {"孔": "hole", "圆孔": "hole", "盲孔": "hole", "通孔": "hole",
                 "凹槽": "pocket", "型腔": "pocket", "方槽": "pocket",
                 "槽": "slot", "腰形槽": "slot", "长圆槽": "slot",
                 "台阶": "step", "阶梯": "step", "凸台": "boss",
                 "球坑": "sphere", "球面": "sphere", "球": "sphere"}
        t = alias.get(t, t)
        if t not in ("hole", "pocket", "slot", "step", "boss", "sphere"):
            t = "hole"
        depth = _f(d.get("depth"))
        through = bool(d.get("through", depth <= 0))
        return Feature(
            type=t, x=_f(d.get("x")), y=_f(d.get("y")),
            diameter=_f(d.get("diameter")), length=_f(d.get("length")),
            width=_f(d.get("width")), depth=depth, through=through,
            counterbore_dia=_f(d.get("counterbore_dia")),
            counterbore_depth=_f(d.get("counterbore_depth")),
            corner_radius=_f(d.get("corner_radius")),
            angle=_f(d.get("angle")), datum=str(d.get("datum", "") or ""),
            datum_ref=str(d.get("datum_ref", "") or ""),
            note=str(d.get("note", "")))


@dataclass
class PartSpec:
    part_name: str = "未命名零件"
    part_type: str = "plate"     # plate | bracket | flange | block
    material: str = "45钢"
    units: str = "mm"
    length: float = 100.0
    width: float = 60.0
    thickness: float = 12.0
    base_kind: str = "rect"      # rect | round
    diameter: float = 0.0        # for round base
    corner_radius: float = 0.0
    end_left: str = "square"     # square | arc（圆弧头，半径 W/2）
    end_right: str = "square"
    shell_t: float = 0.0         # 抽壳壁厚（0 = 不抽壳）
    shell_open: str = "top"      # top | none（closed）
    features: List[Feature] = field(default_factory=list)
    # 原生扫掠（加料）：[{profile_w, profile_h, path:[{type,line|arc,p1,p2,center}]}]
    # 矩形截面沿平面路径（直线/真圆弧）扫掠，不做点列拟合。
    sweeps: List[dict] = field(default_factory=list)
    # 通用回转基体：闭合母线轮廓（r,z）绕竖轴 Z 转 360°。
    # [{"type":"line","p1":[r,z],"p2":[r,z]},
    #  {"type":"arc","p1":[r,z],"p2":[r,z],"center":[r,z],"direction":1}]
    # direction: +1 = (r,z) 平面逆时针，-1 = 顺时针。r >= 0，轴为 r=0。
    # 杯/锥壳/法兰/圆顶一类零件直接由这条母线定义，CAD 侧用原生直线+真圆弧，不点列拟合。
    revolve_profile: List[dict] = field(default_factory=list)
    # validate()/conflict.analyze() 判为识图误读而剔除的特征说明（人可读）。
    # 必须记下来：剔除之后「理论体积」也一起变小，体积偏差恒为 0，
    # 否则删特征反而变成最容易的「通过」路径（实测 box3 剔 5 个特征 = 理论体积差 6.3%）。
    dropped_features: List[str] = field(default_factory=list)
    shaft_segments: List[dict] = field(default_factory=list)
    sheet_L: float = 0.0         # sheet metal blank length
    sheet_W: float = 0.0
    sheet_t: float = 0.0
    flange_h: float = 0.0
    bend_r: float = 1.5
    module: float = 0.0          # gear module (0 = not a gear)
    teeth: int = 0
    face_width: float = 0.0
    bore: float = 0.0
    keyway_w: float = 0.0
    keyway_d: float = 0.0   # 回转体分段 [{dia,length}]
    chamfer: float = 0.0          # 顶面外边缘倒角 C（mm）
    edge_fillet: float = 0.0      # 顶面外边缘圆角 R（mm）
    parts: list = field(default_factory=list)   # 装配体：子零件（高级）
    shaft_segments: List[dict] = field(default_factory=list)
    sheet_L: float = 0.0         # sheet metal blank length
    sheet_W: float = 0.0
    sheet_t: float = 0.0
    flange_h: float = 0.0
    bend_r: float = 1.5
    module: float = 0.0          # gear module (0 = not a gear)
    teeth: int = 0
    face_width: float = 0.0
    bore: float = 0.0
    keyway_w: float = 0.0
    keyway_d: float = 0.0   # 回转体分段 [{dia,length}]
    chamfer: float = 0.0        # 顶面外轮廓倒角 C 值（0=无）
    fillet: float = 0.0         # 顶面外轮廓圆角 R 值（0=无）
    dome_h: float = 0.0         # 圆顶面拱高（mm，0=无）
    dome_elliptic: bool = True  # True=椭圆顶面（SW InsertDome 原生），False=球面帽
    notes: str = ""
    confidence: float = 0.0
    source: str = ""
    raw_analysis: str = ""

    # ---------- derived values ----------
    @property
    def stock(self):
        if self.base_kind == "round" or (self.diameter > 0 and self.length <= 0):
            d = self.diameter or self.length
            return (d, d, self.thickness)
        return (self.length, self.width, self.thickness)

    def bbox(self):
        L, W, T = self.stock
        return (-L / 2.0, -W / 2.0, 0.0, L / 2.0, W / 2.0, T)

    def validate(self) -> List[str]:
        warns: List[str] = []
        # 齿轮：厚度/外径必须由模数齿数决定，否则会退回默认的 100×60×12，
        # 导致 verify 的 Z-map 按 12 厚去比（实测 dim_check 报 30 vs 10）
        if self.teeth > 0 and self.module > 0:
            from .gear import outer_diameter
            self.base_kind = "round"
            self.diameter = outer_diameter(self.module, self.teeth)
            self.length = self.width = self.diameter
            self.thickness = float(self.face_width or self.thickness)
            return warns
        if self.base_kind == "round":
            if self.diameter <= 0:
                self.diameter = max(self.length, self.width)
            if self.thickness <= 0:
                warns.append("厚度无效，已按 10mm 处理")
                self.thickness = 10.0
            if self.diameter <= 0:
                warns.append("直径无效，已按 100mm 处理")
                self.diameter = 100.0
            self.length = self.width = self.diameter
        else:
            for name in ("length", "width", "thickness"):
                if getattr(self, name) <= 0:
                    warns.append(f"{name} 无效，已按默认值处理")
                    setattr(self, name, {"length": 100.0, "width": 60.0, "thickness": 12.0}[name])
        for w in self.drop_pcd_pseudo_holes():
            warns.append(w)
        # 矩形板的四角圆角（corner_radius）和顶面整圈圆角（fillet）是同一件事，
        # 同时给会被各扣一次体积（实测 m3 +4%）并把 SW 的边选圆角做残。
        # 这条必须放在 validate（所有路径都会调），不能只放识图 sanitize。
        if self.base_kind == "rect" and self.corner_radius > 0 and self.fillet > 0:
            warns.append(
                f"四角 R{self.corner_radius:g} 已按基体圆角建模，"
                f"顶面整圈圆角 R{self.fillet:g} 属重复表达，已忽略")
            self.fillet = 0.0
        # 矩形板的四角圆角（corner_radius）与顶面整圈圆角（fillet）是同一件事，
        # 同时给会被各扣一次体积（实测 +4%）并把 SW 的边选圆角做残（只做 2 条）。
        # 放在 validate() 里，所有路径（识图 / batch 直调 / run_spec）都会经过。
        if self.base_kind == "rect" and self.corner_radius > 0 and self.fillet > 0:
            warns.append(
                f"四角 R{self.corner_radius:g} 已按基体圆角建模，"
                f"顶面整圈圆角 R{self.fillet:g} 属重复表达，已忽略")
            self.fillet = 0.0
        for w in self.merge_concentric_holes():
            warns.append(w)
        half_l = self.length / 2.0
        half_w = self.width / 2.0
        bad: List[Feature] = []
        for f in self.features:
            if f.type == "hole" and f.diameter <= 0:
                f.diameter = 6.0
                warns.append("发现未标注直径的孔，已按 Φ6 处理")
            if f.type in ("pocket", "slot", "step"):
                if f.length <= 0 and f.type != "slot":
                    f.length = 20.0
                if f.width <= 0:
                    f.width = 10.0
            if f.type == "pocket" and f.depth <= 0:
                f.depth = min(self.thickness, 5.0)
                f.through = False
            # 通/盲统一按深度判（depth>0 = 盲孔），否则 CAD 侧读 f.through 会切穿盲孔
            try:
                f.through = f.is_through()
            except Exception:
                pass
            if f.type == "hole":
                rx = ry = f.diameter / 2.0
            else:
                rx = max(f.length, f.width or 0.0) / 2.0
                ry = (f.width or f.length) / 2.0
            out_of_blank = False
            if abs(f.x) + rx > half_l + 0.01 and self.base_kind == "rect":
                warns.append(f"特征 {f.type}@({f.x:.1f},{f.y:.1f}) 超出毛坯 X 边界")
                out_of_blank = True
            if abs(f.y) + ry > half_w + 0.01 and self.base_kind == "rect":
                warns.append(f"特征 {f.type}@({f.x:.1f},{f.y:.1f}) 超出毛坯 Y 边界")
                out_of_blank = True
            r = max(rx, ry)
            if abs(f.x) + r > self.diameter / 2.0 and self.base_kind == "round":
                warns.append(f"特征 {f.type}@({f.x:.1f},{f.y:.1f}) 超出圆盘边界")
                out_of_blank = True
            # 落在毛坯之外的特征是识图误读（例如把装配位置 / 尺寸标注读成零件特征）。
            # 绝不能放进 CAD 和 CAM：实测箱座 Φ9@(0,±79) 让 NC 在 Y=±79 下刀，
            # 而毛坯只有 230×100，等于往夹具和台面上钻孔。这里直接剔除并告警。
            if out_of_blank:
                bad.append(f)
        for f in bad:
            if f in self.features:
                self.features.remove(f)
                msg = (f"特征 {f.type}@({f.x:.1f},{f.y:.1f}) 完全落在毛坯之外，"
                       f"判为识图误读，不参与建模与加工")
                warns.append("【已忽略】" + msg)
                if msg not in self.dropped_features:
                    self.dropped_features.append(msg)

        # ---- 特征间冲突：薄壁 / 包含 / 相切 ----
        # CAD 遇到零厚度几何会直接拒绝切除，而体积偏差只有几个百分点，
        # 很容易被误判为「通过了」。这里统一由 conflict.analyze 判定，
        # 它同时给出处置方案（跳过重复轮廓 / 外扩并入），建模与算体积都用同一份结果。
        try:
            from .conflict import analyze as _analyze_conflicts
            rep = _analyze_conflicts(self)
            self._conflict_cache = rep
            for w in rep["warnings"]:
                if w not in warns:
                    warns.append(w)
        except Exception as e:          # 冲突检测不能把识别流程搞挂
            warns.append(f"特征冲突检测跳过：{e}")
        return warns

    def counterbore_is_void(self, f) -> bool:
        """沉孔位置在顶面已经被别的特征切掉了？（例如沉孔落在已切通的型腔/槽里）

        这种沉孔没有材料可切，CAD 会直接拒绝（"非选择性的参数"），必须跳过。
        """
        if f.type != "hole" or f.counterbore_depth <= 0 or f.counterbore_dia <= 0:
            return False
        try:
            from .conflict import outline as _ol, _inside
            rep = self.conflicts()
            bad = set(rep.get("skip") or []) | set(rep.get("ignore") or [])
            cbp = _ol(type("X", (), {"type": "hole", "x": f.x, "y": f.y,
                                     "diameter": f.counterbore_dia / 2.0 * 2,
                                     "length": 0, "width": 0, "angle": 0})(), 24)
            for i, g in enumerate(self.features):
                if g is f or i in bad or g.type == "hole":
                    continue
                depth = (self.thickness if (g.through and g.depth <= 0)
                         else min(g.depth or self.thickness, self.thickness))
                if depth < f.counterbore_depth:
                    continue
                gp = _ol(g, 48)
                if all(_inside(p_, gp) for p_ in cbp):
                    return True
        except Exception:
            return False
        return False

    def merge_concentric_holes(self) -> List[str]:
        """同一中心的一串同心孔 → 合成「通孔 + 一级沉孔」。

        识图常把 Ø80/Ø70/Ø68/Ø62/Ø47 这种同轴的一堆直径各列一个通孔，切出来是
        互相覆盖的孔，几何与实际完全不符。这里按"最小直径通孔 + 次大直径沉孔"合并，
        更大的直径判为外形/凸台尺寸丢弃。
        """
        warns: List[str] = []
        holes = [f for f in self.features if f.type == "hole"]
        if len(holes) < 2:
            return warns
        groups: List[List[Feature]] = []
        for f in holes:
            for g in groups:
                h0 = g[0]
                if abs(h0.x - f.x) <= 0.5 and abs(h0.y - f.y) <= 0.5:
                    g.append(f)
                    break
            else:
                groups.append([f])
        for g in groups:
            if len(g) < 2:
                continue
            g.sort(key=lambda h: h.diameter)
            keep = g[0]
            bigger = [h for h in g[1:] if h.diameter > keep.diameter + 0.3]
            if not bigger:
                continue
            cb = bigger[0]                      # 只保留一级沉孔（次大直径）
            depth = cb.depth if (cb.depth and cb.depth > 0) else min(2.0, self.thickness * 0.2)
            keep.counterbore_dia = round(cb.diameter, 3)
            keep.counterbore_depth = round(depth, 2)
            keep.through = True
            drop = [h for h in g if h is not keep]
            for h in drop:
                if h in self.features:
                    self.features.remove(h)
            warns.append(
                f"同轴同心孔合并：Ø{keep.diameter:g} 通孔 + Ø{keep.counterbore_dia:g} 沉孔深"
                f"{keep.counterbore_depth:g}（图上有 {len(g)} 个同心直径 "
                f"{'/'.join('Ø%g' % h.diameter for h in g)}，其余判为外形/凸台尺寸）")
        return warns

    _BAD_NAME = re.compile(r'[\\/:*?"<>|\r\n\t]+')

    @property
    def file_stem(self) -> str:
        """落盘用的文件名（净化过）：零件名里常有 "/"（如 泵体/壳体），
        直接当路径会建出一个不存在的子目录 —— NX 的 SaveAs 不报错但什么也不写。"""
        s = self._BAD_NAME.sub("_", str(self.part_name or ""))
        s = s.strip(" .")
        return s or "part"

    def conflicts(self) -> Dict[str, Any]:
        """薄壁/重复轮廓的判定结果（见 app/conflict.py）。"""
        try:
            from .conflict import analyze as _a
            return _a(self)
        except Exception:
            return {"skip": [], "grow": {}, "warnings": []}

    def pcd_of(self, f: "Feature", tol: float = 1.5) -> float:
        """如果 f 是“环布孔”之一，返回它们的节圆直径（PCD）；否则返回 0。

        图纸常见标注「6×Ø10 均布 Ø90」：Ø90 是 6 个孔的中心距圆直径，不是沉孔。
        识图或复核很容易把它当成 Ø90 沉孔，切下去会把整片材料挖掉
        （实测法兰盘：CAD 74,220.1，正确值 151,974.5，少了一整个 Ø90×15 圆环）。

        判据：另有 ≥3 个同直径孔，半径都与到中心的距离相等，且
        该直径≈ 2×半径 —— 这种情况下 diameter 是节圆尺寸，不是孔径。
        """
        if f.type != "hole" or f.diameter <= 0 or (not f.through and f.depth > 0):
            return 0.0
        peers = [g for g in self.features
                 if g is not f and g.type == "hole" and g.diameter > 0
                 and abs(g.diameter - f.diameter) <= 0.6]
        rad = math.hypot(f.x, f.y)
        # 情形 A：f 本身就是环布孔之一，diameter 被写成了节圆尺寸
        if rad > 1e-6 and len(peers) >= 2:
            same = [g for g in peers if abs(math.hypot(g.x, g.y) - rad) <= tol]
            if len(same) >= 2 and abs(f.diameter - 2.0 * rad) <= tol:
                return 2.0 * rad
            return 0.0
        # 情形 B：f 标在中心，而四周有一圈更小的等半径孔，
        # 且 f 的直径≈那个半径的两倍 → 这是“Ø90 均布 6×Ø10”里的节圆尺寸
        if rad <= tol:
            ring: List[Feature] = []
            for g in self.features:
                if g is f or g.type != "hole" or g.diameter <= 0:
                    continue
                if g.diameter >= f.diameter - 0.6:
                    continue        # 只把小孔当环布孔
                gr = math.hypot(g.x, g.y)
                if gr > tol:
                    ring.append(g)
            if len(ring) < 3:
                return 0.0
            radii = [math.hypot(g.x, g.y) for g in ring]
            r0 = sorted(radii)[len(radii) // 2]
            on_ring = [r for r in radii if abs(r - r0) <= tol]
            if len(on_ring) < 3:
                return 0.0
            if abs(f.diameter - 2.0 * r0) <= tol:
                return 2.0 * r0
        return 0.0

    def drop_pcd_pseudo_holes(self) -> List[str]:
        """删掉被当成孔的节圆直径尺寸（例如 Ø90 均布 6×Ø10 里的 Ø90）。"""
        warns: List[str] = []
        drop: List[Feature] = []
        for f in self.features:
            pcd = self.pcd_of(f)
            if pcd <= 0:
                continue
            rad = math.hypot(f.x, f.y)
            if rad <= 1.5:
                # 情形 B：中心标的节圆尺寸，四周一圈小孔
                r0 = pcd / 2.0
                cnt = sum(1 for g in self.features
                          if g is not f and g.type == "hole" and g.diameter > 0
                          and g.diameter < f.diameter - 0.6
                          and abs(math.hypot(g.x, g.y) - r0) <= 1.5)
                warns.append(
                    f"剔除节圆尺寸：Ø{f.diameter:g} 标在中心，实际是 {cnt} 个均布孔"
                    f"（分布半径 {r0:.2f}）的节圆直径，不是沉孔"
                    f"（当孔切会挖掉 Ø{f.diameter:g} 整片材料）")
            else:
                warns.append(
                    f"剔除节圆尺寸：Ø{f.diameter:g}@{f.x:g},{f.y:g} 是环布孔的节圆直径"
                    f"（半径 {rad:.2f}），不是孔径")
            drop.append(f)
        for f in drop:
            if f in self.features:
                self.features.remove(f)
        return warns

    def build_features(self) -> List["Feature"]:
        """真正要切的特征：去掉重复表达的 + 贴边误读的，保留原始顺序。"""
        rep = self.conflicts()
        bad = set(rep.get("skip") or []) | set(rep.get("ignore") or [])
        return [f for i, f in enumerate(self.features) if i not in bad]

    def grow_of(self, f: "Feature") -> float:
        """某个孔/槽因薄壁需要外扩的量（mm，0=不需要）。"""
        rep = self.conflicts()
        for idx, g in (rep.get("grow") or {}).items():
            if 0 <= idx < len(self.features) and self.features[idx] is f:
                return float(g)
        return 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "part_name": self.part_name, "part_type": self.part_type,
            "material": self.material, "units": "mm",
            "length": round(self.length, 4), "width": round(self.width, 4),
            "thickness": round(self.thickness, 4), "base_kind": self.base_kind,
            "diameter": round(self.diameter, 4), "corner_radius": round(self.corner_radius, 4),
            "shell_t": round(float(getattr(self, "shell_t", 0.0) or 0.0), 3),
            "shell_open": str(getattr(self, "shell_open", "top") or "top"),
            "end_left": str(getattr(self, "end_left", "square") or "square"),
            "end_right": str(getattr(self, "end_right", "square") or "square"),
            "features": [f.to_dict() for f in self.features],
            "sweeps": list(getattr(self, "sweeps", []) or []),
            "revolve_profile": list(getattr(self, "revolve_profile", []) or []),
            "chamfer": round(self.chamfer, 3), "fillet": round(self.fillet, 3),
            "dome_h": round(float(getattr(self, "dome_h", 0.0) or 0.0), 3),
            "dome_elliptic": bool(getattr(self, "dome_elliptic", True)),
            "shaft_segments": self.shaft_segments,
            "sheet_L": self.sheet_L, "sheet_W": self.sheet_W, "sheet_t": self.sheet_t,
            "flange_h": self.flange_h, "bend_r": self.bend_r,
            "module": self.module, "teeth": self.teeth, "face_width": self.face_width,
            "bore": self.bore, "keyway_w": self.keyway_w, "keyway_d": self.keyway_d,
            "notes": self.notes, "confidence": round(self.confidence, 3),
            "source": self.source,
        }

    def to_json(self) -> str:
        """给 NX 日志用：只下发真正要切的特征，并带上薄壁外扩量 grow。"""
        d = self.to_dict()
        rep = self.conflicts()
        grow = rep.get("grow") or {}
        feats = []
        for i, f in enumerate(self.features):
            if i in set(rep.get("skip") or []):
                continue
            fd = f.to_dict()
            fd["grow"] = round(float(grow.get(i, 0.0)), 4)
            feats.append(fd)
        d["features"] = feats
        d["conflicts"] = rep.get("warnings", [])
        return json.dumps(d, ensure_ascii=False, indent=2)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "PartSpec":
        base = str(d.get("base_kind", d.get("base", "rect")) or "rect").lower()
        if base in ("圆", "round", "circular", "flange"):
            base = "round"
        else:
            base = "rect"
        p = PartSpec(
            part_name=str(d.get("part_name") or "未命名零件"),
            part_type=str(d.get("part_type") or ("flange" if base == "round" else "plate")),
            material=str(d.get("material") or "45钢"),
            length=_f(d.get("length")), width=_f(d.get("width")),
            thickness=_f(d.get("thickness")), base_kind=base,
            diameter=_f(d.get("diameter")), corner_radius=_f(d.get("corner_radius")),
            shell_t=_f(d.get("shell_t")),
            shell_open=str(d.get("shell_open") or "top").lower(),
            end_left=str(d.get("end_left") or "square").lower(),
            end_right=str(d.get("end_right") or "square").lower(),
            chamfer=_f(d.get("chamfer")), fillet=_f(d.get("fillet")),
            dome_h=_f(d.get("dome_h")),
            dome_elliptic=bool(d.get("dome_elliptic", True)),
            shaft_segments=list(d.get("shaft_segments") or []),
            sweeps=list(d.get("sweeps") or []),
            revolve_profile=list(d.get("revolve_profile") or []),
            sheet_L=_f(d.get("sheet_L")), sheet_W=_f(d.get("sheet_W")),
            sheet_t=_f(d.get("sheet_t")), flange_h=_f(d.get("flange_h")),
            bend_r=_f(d.get("bend_r"), 1.5),
            module=_f(d.get("module")), teeth=int(_f(d.get("teeth"))),
            face_width=_f(d.get("face_width")), bore=_f(d.get("bore")),
            keyway_w=_f(d.get("keyway_w")), keyway_d=_f(d.get("keyway_d")),
            notes=str(d.get("notes") or ""),
            confidence=_f(d.get("confidence")),
            source=str(d.get("source") or ""),
        )
        feats = []
        for fd in (d.get("features") or []):
            if isinstance(fd, dict):
                feats.append(Feature.from_dict(fd))
        p.features = feats
        # expand bolt circle helper if the model returned one
        bc = d.get("bolt_circle")
        if isinstance(bc, dict) and _f(bc.get("count")) >= 2 and _f(bc.get("hole_dia")) > 0:
            n = int(_f(bc.get("count")))
            bcd = _f(bc.get("diameter"))
            hd = _f(bc.get("hole_dia"))
            cx, cy = _f(bc.get("cx")), _f(bc.get("cy"))
            have = {(round(f.x, 1), round(f.y, 1)) for f in p.features if f.type == "hole"}
            for i in range(n):
                a = 2 * math.pi * i / n
                hx = round(cx + bcd / 2.0 * math.cos(a), 1)
                hy = round(cy + bcd / 2.0 * math.sin(a), 1)
                if any(abs(hx - x) < 1.5 and abs(hy - y) < 1.5 for x, y in have):
                    continue
                p.features.append(Feature(type="hole", x=hx, y=hy, diameter=hd, through=True))
        # de-duplicate identical features (vision models sometimes list a hole twice)
        seen = set()
        uniq = []
        for f in p.features:
            k = (f.type, round(f.x, 1), round(f.y, 1), round(f.diameter, 1),
                 round(f.length, 1), round(f.width, 1))
            if k in seen:
                continue
            seen.add(k)
            uniq.append(f)
        p.features = uniq
        return p

    @staticmethod
    def load(path: str) -> "PartSpec":
        with open(path, "r", encoding="utf-8") as f:
            return PartSpec.from_dict(json.load(f))

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.to_json())
