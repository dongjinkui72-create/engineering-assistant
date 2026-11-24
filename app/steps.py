"""建模步骤（可读、可改、可重建）。

PartSpec <-> Step 列表互转。步骤是人能看懂的建模过程，也是重建模型时的
唯一依据；用户改完步骤后，软件按步骤重新生成 PartSpec 并重新建模。
"""
from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .models import Feature, PartSpec

# op -> (中文名, 参数说明, 是否已由建模引擎实现)
OPS: Dict[str, dict] = {
    "base_rect":    {"name": "新建长方体", "params": ["L", "W", "H", "corner_radius"], "built": True},
    "base_round":   {"name": "新建圆柱体", "params": ["D", "H"], "built": True},
    "shaft":        {"name": "新建回转体", "params": ["segments"], "built": True},
    "revolve":      {"name": "新建回转基体", "params": ["profile", "D", "H"], "built": True},
    "sheet":        {"name": "新建钣金件", "params": ["L", "W", "t", "flange_h", "bend_r"], "built": True},
    "gear":         {"name": "新建齿轮", "params": ["module", "teeth", "face_width", "bore",
                                                    "keyway_w", "keyway_d", "hub_dia",
                                                    "hub_len", "bore_depth", "small_bore"],
                     "built": True},
    "hole":         {"name": "钻孔", "params": ["x", "y", "dia", "depth", "through",
                                                "depth_confirmed", "cb_dia", "cb_depth"], "built": True},
    "keyway":       {"name": "孔内键槽", "params": ["dia", "width", "depth", "length"], "built": True},
    "pocket":       {"name": "型腔铣削", "params": ["x", "y", "L", "W", "depth", "corner_radius", "face"], "built": True},
    "slot":         {"name": "腰形槽", "params": ["x", "y", "L", "W", "depth", "through", "angle", "face"], "built": True},
    "step_cut":     {"name": "台阶切除", "params": ["x", "y", "L", "W", "depth", "face"], "built": True},
    "pattern_circle": {"name": "圆周阵列孔", "params": ["cx", "cy", "count", "bcd", "dia", "through",
                                                       "depth_confirmed"], "built": True},
    "pattern_rect": {"name": "矩形阵列孔", "params": ["cx", "cy", "nx", "ny", "dx", "dy", "dia", "through",
                                                     "depth_confirmed"], "built": True},
    "mirror":       {"name": "镜像", "params": ["axis"], "built": True},
    "sweep":        {"name": "扫掠", "params": ["profile_w", "profile_h", "path"], "built": True},
    "dome":         {"name": "圆顶面", "params": ["h", "elliptic"], "built": True},
    "sphere":       {"name": "球坑", "params": ["x", "y", "D", "depth"], "built": True},
    "boss":         {"name": "凸台", "params": ["x", "y", "L", "W", "depth"], "built": True},
    "shell":        {"name": "抽壳", "params": ["t", "open"], "built": True},
    "open_slot":    {"name": "开口槽", "params": ["L", "W", "depth", "side"], "built": True},
    "chamfer":      {"name": "倒角", "params": ["size"], "built": True},
    "fillet":       {"name": "圆角", "params": ["radius"], "built": True},
}

LABELS = {
    "x": "X 坐标", "y": "Y 坐标", "L": "长", "W": "宽", "H": "总厚",
    "D": "直径", "dia": "孔径", "depth": "深度", "through": "通孔",
    "depth_confirmed": "孔深已确认",
    "corner_radius": "圆角", "angle": "角度", "cb_dia": "沉孔直径",
    "cb_depth": "沉孔深", "count": "数量", "bcd": "分布圆直径",
    "cx": "中心X", "cy": "中心Y", "nx": "X 个数", "ny": "Y 个数",
    "dx": "X 间距", "dy": "Y 间距", "axis": "镜像轴", "size": "倒角", "radius": "半径",
    "h": "拱高", "D": "球径", "elliptic": "椭圆顶", "segments": "分段(直径x长度)", "module": "模数", "teeth": "齿数",
    "face_width": "齿宽", "t": "板厚", "flange_h": "折边高度", "bend_r": "折弯半径", "bore": "轴孔直径", "keyway_w": "键槽宽", "keyway_d": "键槽深",
}


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _xy(v) -> str:
    """坐标显示保留两位小数，避免界面出现 0.128 这类识图噪声。"""
    return f"{round(_f(v), 2):g}"


def parse_segments(text) -> list:
    """把 "20x50,30x30" 或 [[20,50],...] 解析成分段列表。"""
    if isinstance(text, (list, tuple)):
        out = []
        for it in text:
            if isinstance(it, dict):
                out.append({"dia": float(it.get("dia") or 0), "length": float(it.get("length") or 0)})
            elif isinstance(it, (list, tuple)) and len(it) >= 2:
                out.append({"dia": float(it[0]), "length": float(it[1])})
        return out
    segs = []
    for part in str(text or "").replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        for sep in ("x", "X", "*", "×", ":"):
            if sep in part:
                a, b = part.split(sep, 1)
                try:
                    segs.append({"dia": float(a.strip()), "length": float(b.strip())})
                except Exception:
                    pass
                break
    return segs


@dataclass
class Step:
    op: str
    params: dict = field(default_factory=dict)
    note: str = ""
    enabled: bool = True
    evidence: str = ""        # 图纸依据（如"剖面 A-A Ø62H7"）
    confidence: float = 0.0   # 这一步的置信度

    # ---------------------------------------------------------------- text
    @property
    def op_name(self) -> str:
        return OPS.get(self.op, {}).get("name", self.op)

    @property
    def built(self) -> bool:
        return bool(OPS.get(self.op, {}).get("built", False))

    def title(self) -> str:
        p = self.params
        n = self.op_name
        if self.op == "shell":
            _o = "开口朝上" if str(p.get("open") or "top").lower() != "none" else "全封闭"
            return f"抽壳：壁厚 {_f(p.get('t')):g} mm（{_o}）"
        if self.op == "open_slot":
            _side = {"right": "右", "left": "左", "top": "上", "bottom": "下"}.get(
                str(p.get("side") or "right").lower(), str(p.get("side")))
            return ("从%s边切开口槽 %g×%g，深 %g" % (
                _side, _f(p.get("L")), _f(p.get("W")), _f(p.get("depth"))))
        if self.op == "sweep":
            _p = list(p.get("path") or [])
            _narc = sum(1 for x in _p if str(x.get("type")).lower() == "arc")
            _kind = f"直线 {len(_p) - _narc} 段" + (f" + 圆弧 {_narc} 段" if _narc else "")
            return (f"扫掠：截面 {_f(p.get('profile_w')):g}×{_f(p.get('profile_h')):g}，"
                    f"沿路径 {_kind}")
        if self.op == "revolve":
            _pf = list(p.get("profile") or [])
            _na = sum(1 for x in _pf if str(x.get("type")).lower() == "arc")
            return (f"回转：母线 {len(_pf)} 段（直线 {len(_pf) - _na}"
                    + (f" + 真圆弧 {_na}" if _na else "") + "）绕竖轴 360°")
        if self.op == "dome":
            _k = "椭圆顶面" if bool(p.get("elliptic", True)) else "球面帽"
            return f"顶面拱起：{_k}，拱高 {_f(p.get('h')):g} mm"
        if self.op == "sphere":
            return (f"球坑：Ø{_f(p.get('D')):g} 球面凹坑，深 {_f(p.get('depth')):g} mm")
        if self.op == "boss":
            return ("凸台：%g×%g，高 %g" % (_f(p.get("L")), _f(p.get("W")), _f(p.get("depth"))))
        if self.op == "profile_cut":
            _pf = list(p.get("profile") or [])
            _na = sum(1 for x in _pf if str(x.get("type")).lower() == "arc")
            _fc = "底面" if str(p.get("face") or "top").lower() == "bottom" else "顶面"
            return (f"从{_fc}切除任意闭合轮廓：{len(_pf)} 段"
                    + (f"（含真圆弧 {_na} 段）" if _na else "")
                    + f"，深 {_f(p.get('depth')):g} mm")
        if self.op == "base_rect":
            if p.get("profile"):
                _pf = list(p.get("profile") or [])
                _na = sum(1 for x in _pf if str(x.get("type")).lower() == "arc")
                return (f"按图纸闭合轮廓拉伸基体：{len(_pf)} 段"
                        + (f"（含真圆弧 {_na} 段）" if _na else "")
                        + f"，厚 {_f(p['H']):g} mm")
            r = _f(p.get("corner_radius"))
            extra = f"，四角 R{r:g}" if r > 0 else ""
            _ends = []
            if str(p.get("end_left") or "square").lower() == "arc":
                _ends.append("左端圆弧头")
            if str(p.get("end_right") or "square").lower() == "arc":
                _ends.append("右端圆弧头")
            if _ends:
                extra += "，" + "、".join(_ends)
            return f"{_f(p['L']):g}×{_f(p['W']):g}×{_f(p['H']):g} mm{extra}"
        if self.op == "base_round":
            return f"Ø{_f(p['D']):g}×{_f(p['H']):g} mm"
        if self.op == "sheet":
            return (f"新建钣金 U 形件：底板 {_f(p.get('L')):g}×{_f(p.get('W')):g}，"
                    f"板厚 {_f(p.get('t')):g}，折边高 {_f(p.get('flange_h')):g}，折弯 R{_f(p.get('bend_r')):g}")
        if self.op == "gear":
            return (f"新建渐开线直齿轮：模数 {_f(p.get('module')):g}，齿数 {int(_f(p.get('teeth'))):g}，"
                    f"齿宽 {_f(p.get('face_width')):g}，轴孔 Ø{_f(p.get('bore')):g}"
                    + (f"，键槽 {_f(p.get('keyway_w')):g}×{_f(p.get('keyway_d')):g}" if _f(p.get('keyway_w')) > 0 else ""))
        if self.op == "shaft":
            segs = parse_segments(p.get("segments"))
            txt = " → ".join(f"Ø{int(s['dia']):g}×{int(s['length']):g}" for s in segs)
            return f"新建回转体：{txt}" if segs else "新建回转体（未填写分段）"
        if self.op == "hole":
            if not bool(p.get("depth_confirmed", True)):
                t = "深度待确认（请选择通孔或填写盲孔深）"
            else:
                t = "通孔" if p.get("through") else f"盲孔 深{_f(p.get('depth')):g}"
            cb = ""
            if _f(p.get("cb_dia")) > 0:
                cb = f"，沉孔 Ø{_f(p['cb_dia']):g}×{_f(p.get('cb_depth')):g}"
            return f"钻 Ø{_f(p['dia']):g} {t}{cb} @ ({_xy(p['x'])}, {_xy(p['y'])})"
        if self.op == "pocket":
            r = _f(p.get("corner_radius"))
            extra = f"，R{r:g}" if r > 0 else ""
            return (f"型腔 {_f(p['L']):g}×{_f(p['W']):g}{extra}，"
                    f"深 {_f(p['depth']):g} @ ({_xy(p['x'])}, {_xy(p['y'])})")
        if self.op == "slot":
            t = "通槽" if p.get("through") else f"深{_f(p.get('depth')):g}"
            _fc = "（底面）" if str(p.get("face") or "top").lower() == "bottom" else ""
            return (f"直槽 {_f(p['L']):g}×{_f(p['W']):g} {t} "
                    f"@ ({_xy(p['x'])}, {_xy(p['y'])}){_fc}，{_f(p.get('angle')):g}°")
        if self.op == "step_cut":
            return (f"切除台阶：中心 ({_xy(p['x'])}, {_xy(p['y'])}) "
                    f"{_f(p['L']):g}×{_f(p['W']):g}，深 {_f(p['depth']):g}")
        if self.op == "pattern_circle":
            _pk = ("深度待确认（请选择通孔或填写盲孔深）"
                   if not bool(p.get("depth_confirmed", True))
                   else ("通孔" if p.get("through", True)
                         else f"盲孔 深{_f(p.get('depth')):g}"))
            _pcb = (f"，沉孔 Ø{_f(p.get('cb_dia')):g}×{_f(p.get('cb_depth')):g}"
                    if _f(p.get("cb_dia")) > 0 else "")
            _cx, _cy = _f(p.get("cx")), _f(p.get("cy"))
            _ctr = ("" if abs(_cx) < 1e-6 and abs(_cy) < 1e-6
                    else f"以 ({round(_cx, 2):g}, {round(_cy, 2):g}) 为圆心，")
            return (f"{_ctr}Ø{_f(p['bcd']):g} 均布 "
                    f"{int(_f(p['count']))}×Ø{_f(p['dia']):g} {_pk}{_pcb}")
        if self.op == "pattern_rect":
            _pk = ("深度待确认（请选择通孔或填写盲孔深）"
                   if not bool(p.get("depth_confirmed", True))
                   else ("通孔" if p.get("through", True)
                         else f"盲孔 深{_f(p.get('depth')):g}"))
            _pcb = (f"，沉孔 Ø{_f(p.get('cb_dia')):g}×{_f(p.get('cb_depth')):g}"
                    if _f(p.get("cb_dia")) > 0 else "")
            _cx, _cy = _f(p.get("cx")), _f(p.get("cy"))
            _ctr = ("" if abs(_cx) < 1e-6 and abs(_cy) < 1e-6
                    else f"以 ({_cx:g}, {_cy:g}) 为中心，")
            return (f"{_ctr}{int(_f(p['nx']))}×{int(_f(p['ny']))} 阵列 "
                    f"Ø{_f(p['dia']):g} {_pk}{_pcb}，间距 {_f(p['dx']):g}×{_f(p['dy']):g}")
        if self.op == "mirror":
            ax = "X 轴" if str(p.get("axis", "x")).lower() == "x" else "Y 轴"
            return f"将已有孔特征镜像到{ax}"
        if self.op == "chamfer":
            return f"顶面外边缘倒角 C{_f(p.get('size', 1)):g}"
        if self.op == "fillet":
            return f"顶面外边缘圆角 R{_f(p.get('radius', 1)):g}"
        return n

    def to_dict(self) -> dict:
        return {"op": self.op, "params": self.params, "note": self.note,
                "enabled": self.enabled, "evidence": self.evidence,
                "confidence": round(self.confidence, 3)}

    @staticmethod
    def from_dict(d: dict) -> "Step":
        return Step(op=str(d.get("op")), params=dict(d.get("params") or {}),
                    note=str(d.get("note") or ""), enabled=bool(d.get("enabled", True)),
                    evidence=str(d.get("evidence") or ""),
                    confidence=float(d.get("confidence") or 0.0))


# ---------------------------------------------------------------- conversions
def _group_holes(holes: List[Feature], allow_circle: bool = False) -> (List[Step], List[Feature]):
    """把规则分布的孔合并成阵列步骤，单个孔原样返回。"""
    steps: List[Step] = []
    rest = list(holes)

    def _sig(h):
        # 只比几何属性。note 里常写「第 3/6 孔」这类位置说明，
        # 拿它参与分组会把同一圈孔拆成 6 个独立步骤。
        return (round(float(h.depth or 0.0), 4),
                bool(getattr(h, "depth_confirmed", True)),
                round(float(h.counterbore_dia or 0.0), 4),
                round(float(h.counterbore_depth or 0.0), 4),
                str(h.datum or ""), str(h.datum_ref or ""))

    same = bool(rest) and all(_sig(h) == _sig(rest[0]) for h in rest)
    def _clusters(vals: List[float], tol: float = 1.2) -> List[float]:
        """把近似相同的坐标并成一列，避免像素测量出的 ±0.2mm 抖动
        把 2×2 阵列判成「不是矩形阵列」。"""
        out: List[List[float]] = []
        for v in sorted(vals):
            if out and abs(v - sum(out[-1]) / len(out[-1])) <= tol:
                out[-1].append(v)
            else:
                out.append([v])
        return [sum(g) / len(g) for g in out]

    # 1) 矩形阵列
    if len(rest) >= 4:
        xs = _clusters([h.x for h in rest])
        ys = _clusters([h.y for h in rest])
        _grid_ok = (len(xs) >= 2 and len(ys) >= 2 and len(xs) * len(ys) == len(rest))
        if _grid_ok:
            _cells = set()
            for h in rest:
                _xi = min(range(len(xs)), key=lambda i: abs(xs[i] - h.x))
                _yi = min(range(len(ys)), key=lambda i: abs(ys[i] - h.y))
                if abs(xs[_xi] - h.x) > 1.2 or abs(ys[_yi] - h.y) > 1.2:
                    _grid_ok = False
                    break
                _cells.add((_xi, _yi))
            _grid_ok = _grid_ok and len(_cells) == len(rest)
        if _grid_ok and same:
            dia = rest[0].diameter
            if all(abs(h.diameter - dia) < 0.01 for h in rest):
                dx = (xs[-1] - xs[0]) / (len(xs) - 1)
                dy = (ys[-1] - ys[0]) / (len(ys) - 1)
                steps.append(Step("pattern_rect", {
                    "cx": round((xs[0] + xs[-1]) / 2, 2), "cy": round((ys[0] + ys[-1]) / 2, 2),
                    "nx": len(xs), "ny": len(ys), "dx": round(dx, 2), "dy": round(dy, 2),
                    "dia": dia, "through": rest[0].is_through(),
                    "depth": round(float(rest[0].depth or 0.0), 4),
                    "depth_confirmed": bool(getattr(rest[0], "depth_confirmed", True)),
                    "cb_dia": round(float(rest[0].counterbore_dia or 0.0), 4),
                    "cb_depth": round(float(rest[0].counterbore_depth or 0.0), 4),
                    "datum": str(rest[0].datum or ""),
                    "datum_ref": str(rest[0].datum_ref or "")},
                    note=rest[0].note))
                return steps, []
    # 2) 圆周均布（必须有图纸语义依据，不能靠"四个角孔近似对称"推断）
    # 实测安装板 4 个角孔被误判成 Ø150.25 节圆阵列，步骤说明完全错。
    if allow_circle and len(rest) >= 3:
        cx = sum(h.x for h in rest) / len(rest)
        cy = sum(h.y for h in rest) / len(rest)
        rs = [math.hypot(h.x - cx, h.y - cy) for h in rest]
        dia = rest[0].diameter
        if (max(rs) - min(rs) < max(1.0, 0.02 * max(rs)) and 0 < sum(rs) / len(rs)
                and all(abs(h.diameter - dia) < 0.01 for h in rest) and same):
            r = sum(rs) / len(rs)
            steps.append(Step("pattern_circle", {
                "cx": round(cx, 2), "cy": round(cy, 2), "count": len(rest),
                "bcd": round(r * 2, 2), "dia": dia, "through": rest[0].is_through(),
                "depth": round(float(rest[0].depth or 0.0), 4),
                "depth_confirmed": bool(getattr(rest[0], "depth_confirmed", True)),
                "cb_dia": round(float(rest[0].counterbore_dia or 0.0), 4),
                "cb_depth": round(float(rest[0].counterbore_depth or 0.0), 4),
                "datum": str(rest[0].datum or ""),
                "datum_ref": str(rest[0].datum_ref or "")},
                note=rest[0].note))
            return steps, []
    return steps, rest


def spec_to_steps(spec: PartSpec) -> List[Step]:
    # steps 依赖 Feature.is_through() 的"深度优先"语义：未清洗的 spec 会把
    # 「通孔 + 噪声深度」写成盲孔（实测 m1 往返差 8,794 mm³）。
    # 这里先过一遍 sanitize，保证"界面上看到的步骤 = 实际会建的几何"。
    try:
        from .vision import DrawingAnalyzer
        # 清洗会剔除越界特征；必须在副本上做，否则剔除证据丢失，
        # 界面和审计都会误以为"图纸本来就干净"。
        spec = copy.deepcopy(spec)
        DrawingAnalyzer.sanitize_spec(spec)
    except Exception:
        pass
    from .calc import feature_area
    steps: List[Step] = []
    if spec.sheet_t > 0 and spec.sheet_L > 0:
        steps.append(Step("sheet", {"L": spec.sheet_L, "W": spec.sheet_W, "t": spec.sheet_t,
                                    "flange_h": spec.flange_h, "bend_r": spec.bend_r}))
    elif spec.teeth > 0 and spec.module > 0:
        steps.append(Step("gear", {"module": spec.module, "teeth": spec.teeth,
                                   "face_width": spec.face_width, "bore": spec.bore,
                                   "keyway_w": spec.keyway_w, "keyway_d": spec.keyway_d,
                                   "hub_dia": spec.hub_dia, "hub_len": spec.hub_len,
                                   "bore_depth": spec.bore_depth,
                                   "small_bore": spec.small_bore}))
    elif spec.shaft_segments:
        txt = ",".join(f"{int(float(s.get('dia', 0))):g}x{int(float(s.get('length', 0))):g}"
                       for s in spec.shaft_segments)
        steps.append(Step("shaft", {"segments": txt}))
    elif getattr(spec, "revolve_profile", None):
        from .calc import revolve_bbox
        _bb = revolve_bbox(spec) or (0, 0, 0, 0, 0, 0)
        steps.append(Step("revolve",
                          {"profile": [dict(x) for x in spec.revolve_profile],
                           "D": round((_bb[1] - _bb[0]), 4),
                           "H": round((_bb[5] - _bb[4]), 4)},
                          evidence="图纸母线（原生直线/真圆弧绕竖轴回转）"))
    elif spec.base_kind == "round":
        steps.append(Step("base_round", {"D": spec.diameter, "H": spec.thickness},
                          evidence=f"图纸外形：Ø{spec.diameter:g} × 厚 {spec.thickness:g}"))
    else:
        _br = {"L": spec.length, "W": spec.width, "H": spec.thickness,
               "corner_radius": spec.corner_radius,
               "end_left": str(getattr(spec, "end_left", "square") or "square"),
               "end_right": str(getattr(spec, "end_right", "square") or "square")}
        if getattr(spec, "base_profile", None):
            _br["profile"] = [dict(x) for x in spec.base_profile]
        _ev = f"图纸外形：{spec.length:g}×{spec.width:g}×{spec.thickness:g}"
        if spec.corner_radius > 0:
            _ev += f"，四角 R{spec.corner_radius:g}"
        if str(getattr(spec, "end_left", "square") or "square").lower() == "arc":
            _ev += "，左端圆弧头"
        if str(getattr(spec, "end_right", "square") or "square").lower() == "arc":
            _ev += "，右端圆弧头"
        steps.append(Step("base_rect", _br, evidence=_ev))
    for _sw in (getattr(spec, "sweeps", None) or []):
        steps.append(Step("sweep", {"profile_w": _sw.get("profile_w"),
                                    "profile_h": _sw.get("profile_h"),
                                    "path": list(_sw.get("path") or [])},
                           evidence="图纸外形/扫掠体"))
    if float(getattr(spec, "dome_h", 0.0) or 0.0) > 0:
        steps.append(Step("dome", {"h": float(spec.dome_h),
                                   "elliptic": bool(getattr(spec, "dome_elliptic", True))},
                          evidence="图纸顶面圆弧（拱高）"))
    if float(getattr(spec, "shell_t", 0.0) or 0.0) > 0:
        steps.append(Step("shell", {"t": spec.shell_t,
                                    "open": str(getattr(spec, "shell_open", "top") or "top")}))
    holes = [f for f in spec.features if f.type == "hole"]
    # 先按直径分组，再分别判断阵列/圆周分布（避免中心孔干扰）
    by_dia = {}
    for h in holes:
        by_dia.setdefault(round(h.diameter, 2), []).append(h)
    grouped, single = [], []
    _bolt = False
    for _h in holes:
        # 只有图纸明确写了节圆/均布（datum、datum_ref、evidence、note 任一处）才允许
        # 合并成圆周阵列；不能凭"坐标近似在圆上"推断，否则四角孔会被误并。
        _txt = " ".join(str(getattr(_h, k, "") or "") for k in
                        ("datum", "datum_ref", "evidence", "note")).lower()
        if "bolt" in _txt or "pcd" in _txt or "节圆" in _txt or "均布" in _txt:
            _bolt = True
            break
        try:
            if spec.pcd_of(_h) > 0:
                _bolt = True
                break
        except Exception:
            pass
    for dia, hs in by_dia.items():
        g, rest = _group_holes(hs, allow_circle=_bolt)
        single.extend(rest)
        grouped.extend(g)

    # 建模顺序按"人的思路"排：基体 → 型腔/台阶/槽（大切除）→ 孔 → 边特征。
    # 原来把孔排在型腔前面，读起来像先钻孔再铣型腔，与实际加工/建模习惯相反。
    for _kw in [f for f in spec.features if f.type == "keyway"]:
        steps.append(Step("keyway", {"dia": _kw.diameter, "width": _kw.width,
                                     "depth": _kw.depth,
                                     "length": _kw.length or spec.thickness},
                          note=_kw.note, evidence=_kw.evidence))
    cuts = [f for f in spec.features
            if f.type in ("pocket", "step", "slot", "boss", "sphere", "profile_cut")]
    cuts.sort(key=lambda f: -(feature_area(f) if f.type not in ("sphere",) else
                              (f.diameter or 0) ** 2 * 0.785))
    order = cuts + single
    _cut_start = len(steps)
    for f in order:
        if f.type == "hole":
            _note = f.note or ""
            if getattr(f, "datum", "") and "基准=" not in _note:
                _tag = f"基准={f.datum}" + (f"（{f.datum_ref}）" if f.datum_ref else "")
                _note = (_note + "；" if _note else "") + _tag
            _ev = str(getattr(f, "evidence", "") or "")
            if not _ev:
                _ev = f"孔标注 Ø{f.diameter:g}" + (" 通孔" if f.is_through() else f" 深{f.depth:g}")
            steps.append(Step("hole", {"x": f.x, "y": f.y, "dia": f.diameter,
                                       "depth": f.depth, "through": f.through,
                                       "depth_confirmed": bool(getattr(f, "depth_confirmed", True)),
                                       "cb_dia": f.counterbore_dia,
                                       "cb_depth": f.counterbore_depth,
                                       "datum": str(getattr(f, "datum", "") or ""),
                                       "datum_ref": str(getattr(f, "datum_ref", "") or "")},
                              note=_note, evidence=_ev))
        elif f.type == "pocket":
            _pk = {"x": f.x, "y": f.y, "L": f.length, "W": f.width,
                   "depth": f.depth, "corner_radius": f.corner_radius}
            if str(getattr(f, "face", "top") or "top").lower() == "bottom":
                _pk["face"] = "bottom"
            steps.append(Step("pocket", _pk, note=f.note,
                              evidence=str(getattr(f, "evidence", "") or "")
                              or f"图纸标注：型腔 {f.length:g}×{f.width:g} 深 {f.depth:g}"))
        elif f.type == "slot":
            _sk = {"x": f.x, "y": f.y, "L": f.length, "W": f.width,
                   "depth": f.depth, "through": f.through, "angle": f.angle}
            if str(getattr(f, "face", "top") or "top").lower() == "bottom":
                _sk["face"] = "bottom"
            steps.append(Step("slot", _sk, note=f.note,
                              evidence=str(getattr(f, "evidence", "") or "")
                              or f"图纸标注：直槽 {f.length:g}×{f.width:g} 深 {f.depth:g}"))
        elif f.type == "profile_cut":
            _pc = {"profile": [dict(x) for x in (f.profile or [])],
                   "depth": f.depth,
                   "through": bool(f.through)}
            if str(getattr(f, "face", "top") or "top").lower() == "bottom":
                _pc["face"] = "bottom"
            steps.append(Step("profile_cut", _pc, note=f.note,
                              evidence=str(getattr(f, "evidence", "") or "")
                              or f"图纸标注：闭合轮廓切除，深 {f.depth:g}"))
        elif f.type == "sphere":
            steps.append(Step("sphere", {"x": f.x, "y": f.y, "D": f.diameter,
                                         "depth": f.depth}, note=f.note,
                              evidence=str(getattr(f, "evidence", "") or "")
                              or f"图纸标注：球面凹坑 Ø{f.diameter:g} 深 {f.depth:g}"))
        elif f.type == "boss":
            steps.append(Step("boss", {"x": f.x, "y": f.y, "L": f.length,
                                       "W": f.width, "depth": f.depth}, note=f.note,
                              evidence=str(getattr(f, "evidence", "") or "")
                              or f"图纸标注：凸台 {f.length:g}×{f.width:g} 高 {f.depth:g}"))
        elif f.type == "step":
            # 贴边的台阶其实就是"开口槽"，反拆时按 open_slot 输出，语义更清楚
            _hl = spec.length / 2.0
            _hw = spec.width / 2.0
            _side, _hit = None, False
            if spec.base_kind == "rect":
                if abs(abs(f.x) + f.length / 2.0 - _hl) <= 0.05:
                    _side, _hit = ("right" if f.x > 0 else "left"), True
                elif abs(abs(f.y) + f.width / 2.0 - _hw) <= 0.05:
                    _side, _hit = ("top" if f.y > 0 else "bottom"), True
            if _hit:
                steps.append(Step("open_slot", {"L": f.length, "W": f.width,
                                                "depth": f.depth, "side": _side,
                                                "x": f.x, "y": f.y}, note=f.note,
                                  evidence=str(getattr(f, "evidence", "") or "")
                                  or f"图纸标注：开口槽 {f.length:g}×{f.width:g} 深 {f.depth:g}"))
                continue
            steps.append(Step("step_cut", {"x": f.x, "y": f.y, "L": f.length, "W": f.width,
                                           "depth": f.depth}, note=f.note,
                              evidence=str(getattr(f, "evidence", "") or "")
                              or f"图纸标注：台阶 {f.length:g}×{f.width:g} 深 {f.depth:g}"))
    for _g in grouped:
        if not _g.evidence:
            _p = _g.params
            if _g.op == "pattern_circle":
                _g.evidence = (f"{int(_p['count'])}×Ø{_p['dia']:g} "
                               f"沿 Ø{_p['bcd']:g} 均布")
            elif _g.op == "pattern_rect":
                _g.evidence = (f"孔阵列：{int(_p['nx'])}×{int(_p['ny'])} Ø{_p['dia']:g}，"
                               f"间距 {_p['dx']:g}×{_p['dy']:g}")
    steps.extend(grouped)
    if spec.chamfer > 0 and spec.fillet > 0 and spec.chamfer <= spec.fillet:
        steps.append(Step("chamfer", {"size": spec.chamfer},
                          note=f"C{spec.chamfer:g} 落在 R{spec.fillet:g} 圆角去除区内，"
                               f"实际由圆角覆盖，不单独生成几何",
                          evidence=f"图纸技术要求：C{spec.chamfer:g}（被圆角覆盖）"))
    elif spec.chamfer > 0:
        steps.append(Step("chamfer", {"size": spec.chamfer},
                          evidence=f"图纸技术要求：倒角 C{spec.chamfer:g}"))
    if spec.fillet > 0:
        steps.append(Step("fillet", {"radius": spec.fillet},
                          evidence=f"图纸技术要求：圆角 R{spec.fillet:g}"))

    # 依据与置信度兜底：早期只有部分步骤填了 evidence，界面会显示空白/conf=0.0
    for _st, _f in zip(steps[_cut_start:], order):
        if not _st.evidence:
            _st.evidence = str(getattr(_f, "evidence", "") or "")
    _conf = float(getattr(spec, "confidence", 0.0) or 0.0)
    for _i, _st in enumerate(steps):
        if not _st.confidence:
            _st.confidence = _conf
        if not _st.evidence:
            _st.evidence = {"chamfer": "图纸技术要求（倒角）",
                            "fillet": "图纸技术要求（圆角）"}.get(_st.op, "图纸标注")
    return steps


def steps_to_spec(steps: List[Step], base: Optional[PartSpec] = None) -> PartSpec:
    spec = PartSpec(part_name=base.part_name if base else "未命名零件",
                    material=base.material if base else "45钢",
                    notes=base.notes if base else "",
                    confidence=base.confidence if base else 0.0,
                    source=base.source if base else "")
    feats: List[Feature] = []
    for st in steps:
        if not st.enabled:
            continue
        p = st.params
        op = st.op
        if op == "sheet":
            spec.part_type = "sheet"
            spec.sheet_L = _f(p.get("L"), 100.0)
            spec.sheet_W = _f(p.get("W"), 60.0)
            spec.sheet_t = _f(p.get("t"), 1.0)
            spec.flange_h = _f(p.get("flange_h"), 20.0)
            spec.bend_r = _f(p.get("bend_r"), 1.5)
            spec.length = spec.sheet_L
            spec.width = spec.sheet_W
            spec.thickness = spec.sheet_t
        elif op == "gear":
            spec.part_type = "gear"
            spec.module = _f(p.get("module"), 2.0)
            spec.teeth = int(_f(p.get("teeth"), 20))
            spec.face_width = _f(p.get("face_width"), 10.0)
            spec.bore = _f(p.get("bore"))
            spec.keyway_w = _f(p.get("keyway_w"))
            spec.keyway_d = _f(p.get("keyway_d"))
            spec.hub_dia = _f(p.get("hub_dia"))
            spec.hub_len = _f(p.get("hub_len"))
            spec.bore_depth = _f(p.get("bore_depth"))
            spec.small_bore = _f(p.get("small_bore"))
            from .gear import outer_diameter
            spec.base_kind = "round"
            spec.diameter = outer_diameter(spec.module, spec.teeth)
            spec.length = spec.width = spec.diameter
            spec.thickness = spec.face_width + max(spec.hub_len, 0.0)
        elif op == "shaft":
            spec.part_type = "shaft"
            spec.shaft_segments = parse_segments(p.get("segments"))
            total = sum(s["length"] for s in spec.shaft_segments)
            maxd = max((s["dia"] for s in spec.shaft_segments), default=100.0)
            spec.base_kind = "round"
            spec.diameter = maxd
            spec.length = spec.width = maxd
            spec.thickness = total
        elif op == "revolve":
            spec.part_type = "shaft"
            prof = [dict(x) for x in (p.get("profile") or [])]
            spec.revolve_profile = prof
            rmax = 0.0
            zs = []
            for _sg in prof:
                for _k in ("p1", "p2"):
                    _q = _sg.get(_k)
                    if _q:
                        rmax = max(rmax, float(_q[0]))
                        zs.append(float(_q[1]))
            spec.base_kind = "round"
            spec.diameter = rmax * 2.0
            spec.length = spec.width = spec.diameter
            spec.thickness = (max(zs) - min(zs)) if zs else 0.0
        elif op == "base_rect":
            spec.base_kind = "rect"
            spec.length = _f(p.get("L"), 100)
            spec.width = _f(p.get("W"), 60)
            spec.thickness = _f(p.get("H"), 10)
            spec.corner_radius = _f(p.get("corner_radius"))
            spec.end_left = str(p.get("end_left") or "square").lower()
            spec.end_right = str(p.get("end_right") or "square").lower()
            if p.get("profile"):
                from .geom import norm_profile
                spec.base_profile = norm_profile(p.get("profile"))
                if spec.base_profile:
                    _x0, _y0, _x1, _y1 = spec.profile_bbox()
                    spec.length = _x1 - _x0
                    spec.width = _y1 - _y0
        elif op == "base_round":
            spec.base_kind = "round"
            spec.diameter = _f(p.get("D"), 100)
            spec.length = spec.width = spec.diameter
            spec.thickness = _f(p.get("H"), 10)
        elif op == "hole":
            _datum = str(p.get("datum") or "")
            _note = st.note or ""
            if _datum:
                # spec_to_steps 会把基准追加到备注末尾供界面显示；结构字段已另有
                # datum/datum_ref，重建时把这段自动标签剥掉，避免每次往返都变长。
                _note = re.sub(r"[;；]?\s*基准=[^;；]*$", "", _note).strip()
            feats.append(Feature(type="hole", x=_f(p.get("x")), y=_f(p.get("y")),
                                 diameter=_f(p.get("dia"), 6), depth=_f(p.get("depth")),
                                 through=bool(p.get("through", True)),
                                 depth_confirmed=bool(p.get("depth_confirmed", True)),
                                 counterbore_dia=_f(p.get("cb_dia")),
                                 counterbore_depth=_f(p.get("cb_depth")),
                                 datum=_datum,
                                 datum_ref=str(p.get("datum_ref") or ""), note=_note))
        elif op == "keyway":
            feats.append(Feature(type="keyway", x=0.0, y=0.0,
                                 diameter=_f(p.get("dia")),
                                 width=_f(p.get("width")),
                                 depth=_f(p.get("depth")),
                                 length=_f(p.get("length")),
                                 through=True, note=st.note,
                                 evidence=st.evidence))
        elif op == "pocket":
            feats.append(Feature(type="pocket", x=_f(p.get("x")), y=_f(p.get("y")),
                                 length=_f(p.get("L"), 20), width=_f(p.get("W"), 10),
                                 depth=_f(p.get("depth"), 5), through=False,
                                 corner_radius=_f(p.get("corner_radius")),
                                 face=str(p.get("face") or "top").lower(), note=st.note))
        elif op == "slot":
            feats.append(Feature(type="slot", x=_f(p.get("x")), y=_f(p.get("y")),
                                 length=_f(p.get("L"), 20), width=_f(p.get("W"), 8),
                                 depth=_f(p.get("depth")), through=bool(p.get("through", True)),
                                 angle=_f(p.get("angle")),
                                 face=str(p.get("face") or "top").lower(), note=st.note))
        elif op == "step_cut":
            feats.append(Feature(type="step", x=_f(p.get("x")), y=_f(p.get("y")),
                                 length=_f(p.get("L"), 20), width=_f(p.get("W"), 10),
                                 depth=_f(p.get("depth"), 5), through=False,
                                 face=str(p.get("face") or "top").lower(), note=st.note))
        elif op == "pattern_circle":
            n = int(max(1, _f(p.get("count"), 4)))
            bcd = _f(p.get("bcd"))
            dia = _f(p.get("dia"), 6)
            cx, cy = _f(p.get("cx")), _f(p.get("cy"))
            for i in range(n):
                a = 2 * math.pi * i / n
                dep = _f(p.get("depth"))
                feats.append(Feature(type="hole", x=cx + bcd / 2 * math.cos(a),
                                     y=cy + bcd / 2 * math.sin(a), diameter=dia,
                                     depth=dep,
                                     through=bool(p.get("through", dep <= 0.0)),
                                     depth_confirmed=bool(p.get("depth_confirmed", True)),
                                     counterbore_dia=_f(p.get("cb_dia")),
                                     counterbore_depth=_f(p.get("cb_depth")),
                                     datum=str(p.get("datum") or ""),
                                     datum_ref=str(p.get("datum_ref") or ""),
                                     note=st.note))
        elif op == "pattern_rect":
            nx, ny = int(max(1, _f(p.get("nx"), 2))), int(max(1, _f(p.get("ny"), 2)))
            dx, dy = _f(p.get("dx")), _f(p.get("dy"))
            cx, cy = _f(p.get("cx")), _f(p.get("cy"))
            for i in range(nx):
                for j in range(ny):
                    dep = _f(p.get("depth"))
                    feats.append(Feature(type="hole",
                                         x=cx + (i - (nx - 1) / 2.0) * dx,
                                         y=cy + (j - (ny - 1) / 2.0) * dy,
                                         diameter=_f(p.get("dia"), 6),
                                         depth=dep,
                                         through=bool(p.get("through", dep <= 0.0)),
                                         depth_confirmed=bool(p.get("depth_confirmed", True)),
                                         counterbore_dia=_f(p.get("cb_dia")),
                                         counterbore_depth=_f(p.get("cb_depth")),
                                         datum=str(p.get("datum") or ""),
                                         datum_ref=str(p.get("datum_ref") or ""),
                                         note=st.note))
        elif op == "sweep":
            spec.sweeps.append({"profile_w": _f(p.get("profile_w"), 10.0),
                                "profile_h": _f(p.get("profile_h"), 10.0),
                                "path": list(p.get("path") or [])})
        elif op == "dome":
            spec.dome_h = _f(p.get("h"))
            spec.dome_elliptic = bool(p.get("elliptic", True))
        elif op == "profile_cut":
            from .geom import norm_profile
            feats.append(Feature(type="profile_cut", x=0.0, y=0.0,
                                 depth=_f(p.get("depth")),
                                 through=bool(p.get("through", False)),
                                 profile=norm_profile(p.get("profile") or []),
                                 face=str(p.get("face") or "top").lower(),
                                 note=st.note))
        elif op == "sphere":
            feats.append(Feature(type="sphere", x=_f(p.get("x")), y=_f(p.get("y")),
                                 diameter=_f(p.get("D"), 16), depth=_f(p.get("depth")),
                                 through=False, note=st.note))
        elif op == "boss":
            feats.append(Feature(type="boss", x=_f(p.get("x")), y=_f(p.get("y")),
                                 length=_f(p.get("L"), 20), width=_f(p.get("W"), 10),
                                 depth=_f(p.get("depth"), 5), through=False, note=st.note))
        elif op == "shell":
            spec.shell_t = _f(p.get("t"), 2.0)
            spec.shell_open = str(p.get("open") or "top").lower()
        elif op == "open_slot":
            # 开口槽：矩形轮廓从毛坯某条边切进去（不是闭合腰形槽）
            L = _f(p.get("L"), 10.0)
            W = _f(p.get("W"), 6.0)
            dep = _f(p.get("depth"), spec.thickness)
            side = str(p.get("side") or "right").lower()
            x = p.get("x")
            y = p.get("y")
            if x is None and side in ("left", "right") and spec.length > 0:
                half = spec.length / 2.0
                x = (half - L / 2.0) * (1 if side == "right" else -1)
            if y is None and side in ("top", "bottom") and spec.width > 0:
                half = spec.width / 2.0
                y = (half - W / 2.0) * (1 if side == "top" else -1)
            feats.append(Feature(type="step", x=_f(x), y=_f(y), length=L, width=W,
                                 depth=dep, through=(dep >= spec.thickness - 1e-6),
                                 note=st.note or ("开口槽 从%s 边切入" % side)))
        elif op == "chamfer":
            spec.chamfer = _f(p.get("size"))
        elif op == "fillet":
            spec.fillet = _f(p.get("radius"))
        elif op == "mirror":
            axis = str(p.get("axis", "x")).lower()
            mirrored = []
            for f in feats:
                if f.type != "hole":
                    continue
                g = Feature(**{k: v for k, v in f.__dict__.items()})
                if axis == "x":
                    g.y = -f.y
                else:
                    g.x = -f.x
                mirrored.append(g)
            feats.extend(mirrored)
    spec.features = feats
    spec.validate()
    return spec


def steps_json(steps: List[Step]) -> str:
    import json
    return json.dumps([s.to_dict() for s in steps], ensure_ascii=False, indent=2)
