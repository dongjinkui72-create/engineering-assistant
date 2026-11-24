"""三视图结构化核对。

识图模型现在不仅要给出 PartSpec，还要给出 `views`：
主视图 / 俯视图 / 侧视图各自的外形、特征投影和标注依据。
本模块只做**确定性校验**，不调用模型：
  * 三视图之间的同名尺寸必须一致（长对正、高平齐、宽相等）
  * 视图尺寸必须与 PartSpec 的 L/W/T 一致
  * 孔位必须落在俯视图轮廓内
  * 每个特征的投影至少在一个视图里有依据

校验结果分两级：
  uncertain —— 提示，可继续
  blocking  —— 视图互相矛盾，先别建模
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .models import PartSpec

# 视图规范化：模型可能写 main/top/side，也可能写中文
VIEW_ALIASES = {
    "main": "main", "front": "main", "主视图": "main", "正视图": "main", "前视图": "main",
    "top": "top", "plan": "top", "俯视图": "top", "上视图": "top",
    "side": "side", "left": "side", "right": "side", "侧视图": "side",
    "左视图": "side", "右视图": "side",
    "section": "section", "剖视图": "section", "剖面": "section", "section_a": "section",
    "detail": "detail", "局部放大": "detail", "放大图": "detail",
}

VIEW_LABELS = {
    "main": "主视图", "top": "俯视图", "side": "侧视图",
    "section": "剖视图", "detail": "局部放大", "other": "其它视图",
}

_TOL_ABS = 0.45          # mm，小于这个差值不算矛盾
_TOL_REL = 0.010         # 1%


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _first(d: Dict[str, Any], keys: Sequence[str], default: Any = None) -> Any:
    for k in keys:
        if k in d and d.get(k) not in (None, ""):
            return d.get(k)
    return default


def normalize_views(raw: Any) -> List[dict]:
    """把模型输出规范成统一结构，缺字段不抛异常。"""
    if not isinstance(raw, list):
        return []
    out: List[dict] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        name_raw = str(_first(item, ("name", "view", "type", "视图", "名称"), "other")).strip()
        name = VIEW_ALIASES.get(name_raw.lower(), VIEW_ALIASES.get(name_raw, "other"))
        outline_raw = item.get("outline") or item.get("size") or {}
        if not isinstance(outline_raw, dict):
            outline_raw = {}
        length = _f(_first(outline_raw, ("length", "L", "长", "width_x"), 0.0))
        width = _f(_first(outline_raw, ("width", "W", "宽", "depth", "深"), 0.0))
        height = _f(_first(outline_raw, ("height", "H", "高"), 0.0))
        # 有些模型把尺寸直接写在视图对象上
        if not length:
            length = _f(_first(item, ("length", "L", "长"), 0.0))
        if not width:
            width = _f(_first(item, ("width", "W", "宽", "depth"), 0.0))
        if not height:
            height = _f(_first(item, ("height", "H", "高"), 0.0))
        feats = []
        for f in (item.get("features") or []):
            if isinstance(f, dict):
                feats.append({
                    "type": str(f.get("type") or "hole"),
                    "x": _f(f.get("x")),
                    "y": _f(f.get("y")),
                    "diameter": _f(_first(f, ("diameter", "dia", "D"), 0.0)),
                    "note": str(f.get("note") or ""),
                })
        out.append({
            "name": name,
            "label": str(_first(item, ("label", "title", "标题"), VIEW_LABELS.get(name, name_raw))),
            "scale": str(_first(item, ("scale", "比例"), "") or ""),
            "outline": {"length": length, "width": width, "height": height},
            "features": feats,
            "evidence": str(_first(item, ("evidence", "依据", "note"), "") or ""),
            "index": i,
        })
    return out


def _view_dim(v: dict, axis: str) -> float:
    """axis: L/W/H。主视图给 L,H；俯视图给 L,W；侧视图给 W,H。"""
    o = v.get("outline") or {}
    n = v.get("name")
    if axis == "L":
        if n in ("main", "top"):
            return _f(o.get("length"))
        return _f(o.get("length"))
    if axis == "W":
        if n == "side":
            return _f(o.get("width"))
        return _f(o.get("width"))
    return _f(o.get("height"))


def _tol(a: float, b: float) -> float:
    return max(_TOL_ABS, _TOL_REL * max(abs(a), abs(b)))


def _check(checks: List[dict], name: str, ok: bool, detail: str,
           severity: str = "blocking") -> None:
    checks.append({"name": name, "ok": bool(ok), "detail": detail,
                   "severity": "" if ok else severity})


@dataclass
class ThreeViewReport:
    views: List[dict] = field(default_factory=list)
    checks: List[dict] = field(default_factory=list)
    uncertain: List[str] = field(default_factory=list)
    blocking: List[str] = field(default_factory=list)
    exception: str = ""          # 严格放行依据（当前用于完整回转体的单轴向剖视）

    @property
    def ok(self) -> bool:
        return not self.blocking

    def summary(self) -> str:
        if not self.views:
            if self.exception:
                return "回转体单轴向剖视（图纸未返回三视图）：" + self.exception
            return "未提供三视图结构"
        bad = [c for c in self.checks if not c["ok"]]
        if self.blocking:
            return "三视图核对未完成：" + "；".join(self.blocking[:3])
        if bad:
            return f"三视图核对 {len(self.checks) - len(bad)}/{len(self.checks)} 通过：" + \
                   "；".join(c["detail"] for c in bad[:4])
        if not self.checks:
            tail = ("；另有 " + "；".join(self.uncertain[:2])) if self.uncertain else ""
            return f"三视图结构已读取，但没有可执行的交叉核对{tail}"
        if self.uncertain:
            return (f"三视图 {len(self.checks)} 项检查通过，另有 {len(self.uncertain)} 项待确认："
                    + "；".join(self.uncertain[:2]))
        extra = f"；{self.exception}" if self.exception else ""
        return f"三视图核对通过（{len(self.views)} 个视图，{len(self.checks)} 项检查{extra}）"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "views": self.views,
            "checks": self.checks,
            "uncertain": self.uncertain,
            "blocking": self.blocking,
            "exception": self.exception,
            "summary": self.summary(),
        }


_DIA_RE = re.compile(r"[ØΦφ⌀]\s*(\d+(?:[.,]\d+)?)")

# GD&T 公差框里的 Ø（位置度/同轴度/圆跳动等）表示公差带直径，不是零件几何直径。
_GD_TOL_MARKS = ("◎", "⌖", "⌭", "⌯", "⌓", "↗", "⌰")


def _dia_labels(ev: str) -> List[float]:
    """从 `evidence` 文本里取「零件几何口径」的 Ø 标注，跳过公差带 Ø。"""
    out: List[float] = []
    text = str(ev or "").replace("，", ",")
    for m in _DIA_RE.finditer(text):
        pre = text[max(0, m.start() - 6):m.start()]
        if any(ch in pre for ch in _GD_TOL_MARKS):
            continue
        try:
            out.append(float(m.group(1).replace(",", ".")))
        except Exception:
            continue
    return out


def _profile_point(seg: dict, key: str):
    q = seg.get(key)
    if not isinstance(q, (list, tuple)) or len(q) < 2:
        return None
    try:
        r, z = float(q[0]), float(q[1])
    except Exception:
        return None
    if not (math.isfinite(r) and math.isfinite(z)):
        return None
    return r, z


def _close_pt(a, b, tol: float = 0.08) -> bool:
    return bool(a and b and abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol)


_MISREAD_HINT = ("误识", "误读", "错读", "识别错", "图中无", "图上无",
                 "不存在此", "没有此", "OCR")


def _misread_dia_labels(spec: PartSpec):
    """从 notes 里找出被明确判为 OCR 误识的 Ø 标注（返回 set[float]）。

    只认同一分句里既有 Ø 数字、又有“误识/图中无/OCR”等词的情况。
    实测 15_间隔套：notes 写“OCR中Φ41为误识，图中无此尺寸”，而剖视
    evidence 里仍带着 Φ41；若不排除，完整母线也会被这条 OCR 噪声挡住。
    """
    note = str(getattr(spec, "notes", "") or "")
    out = set()
    for clause in re.split(r"[；;。\n]", note):
        if not any(k in clause for k in _MISREAD_HINT):
            continue
        for raw in _DIA_RE.findall(clause.replace("，", ",")):
            try:
                v = float(raw.replace(",", "."))
            except Exception:
                continue
            if v > 0:
                out.add(round(v, 4))
    return out


def _strict_round_section_exception(views: List[dict], spec: PartSpec) -> Tuple[bool, str]:
    """单轴向剖视图放行：只接受可由闭合母线完整证明的回转体。

    这不是“少一个视图也凑合”的宽松规则。母线结构、外径、轴向长度、剖视
    外形，以及剖视依据里出现的每个 Ø 标注都必须互相覆盖，缺一项就不放行。
    """
    if spec.base_kind != "round" or float(spec.diameter or 0) <= 0:
        return False, "基体不是有明确直径的回转体"
    prof = [s for s in (getattr(spec, "revolve_profile", None) or []) if isinstance(s, dict)]
    if len(prof) < 3:
        return False, "闭合回转母线少于 3 段"

    radii: List[float] = []
    zs: List[float] = []
    for i, seg in enumerate(prof):
        typ = str(seg.get("type") or "").lower()
        if typ not in ("line", "arc"):
            return False, f"母线第 {i+1} 段不是直线/真圆弧（{typ or '未知'}）"
        p1, p2 = _profile_point(seg, "p1"), _profile_point(seg, "p2")
        if p1 is None or p2 is None:
            return False, f"母线第 {i+1} 段端点无效"
        if p1[0] < -1e-9 or p2[0] < -1e-9:
            return False, f"母线第 {i+1} 段出现负半径"
        if abs(p1[0] - p2[0]) <= 1e-6 and abs(p1[1] - p2[1]) <= 1e-6:
            return False, f"母线第 {i+1} 段长度为零"
        if typ == "arc":
            c = _profile_point(seg, "center")
            if c is None:
                return False, f"母线第 {i+1} 段圆弧缺少有效圆心"
            r1 = math.hypot(p1[0] - c[0], p1[1] - c[1])
            r2 = math.hypot(p2[0] - c[0], p2[1] - c[1])
            if abs(r1 - r2) > 0.12 or r1 <= 1e-6:
                return False, f"母线第 {i+1} 段圆弧两端半径不一致"
        radii.extend((p1[0], p2[0]))
        zs.extend((p1[1], p2[1]))

    for i in range(len(prof) - 1):
        if not _close_pt(_profile_point(prof[i], "p2"), _profile_point(prof[i + 1], "p1")):
            return False, f"母线第 {i+1}/{i+2} 段不连续"
    if not _close_pt(_profile_point(prof[-1], "p2"), _profile_point(prof[0], "p1")):
        return False, "回转母线不闭合"

    rmax = max(radii)
    zmin, zmax = min(zs), max(zs)
    dia_profile = 2.0 * rmax
    axial = zmax - zmin
    if abs(dia_profile - float(spec.diameter)) > max(0.25, 0.005 * dia_profile):
        return False, f"母线最大直径 {dia_profile:g} 与参数 Ø{float(spec.diameter):g} 不一致"
    if abs(axial - float(spec.thickness)) > max(0.25, 0.005 * axial):
        return False, f"母线轴向跨度 {axial:g} 与厚度 {float(spec.thickness):g} 不一致"

    by = {v.get("name"): v for v in views}
    sec = by.get("section")
    if not sec and len(views) == 1:
        # 回转体的主视图本身就是轴向全剖视（套/法兰/轴类图纸通常不给“剖视图”
        # 这个名字）。这里只在“整张图只有一个视图”时借用主视图，且下面仍要
        # 逐项核对 D×T 外形与全部 Ø 标注，几何门槛不降低。
        sec = by.get("main")
    if not sec:
        return False, "没有轴向剖视图"
    out = sec.get("outline") or {}
    dims = [float(out.get("length") or 0), float(out.get("height") or 0)]
    if min(dims) <= 0:
        return False, "剖视图缺少两个有效外形尺寸"
    D, T = float(spec.diameter), float(spec.thickness)
    pair_ok = ((abs(dims[0] - D) <= max(0.25, 0.005 * D) and
                abs(dims[1] - T) <= max(0.25, 0.005 * T)) or
               (abs(dims[1] - D) <= max(0.25, 0.005 * D) and
                abs(dims[0] - T) <= max(0.25, 0.005 * T)))
    if not pair_ok:
        return False, (f"剖视外形 {dims[0]:g}×{dims[1]:g} 与 Ø{D:g}×{T:g} 不匹配")

    labels: List[float] = []
    for v in views:
        labels.extend(_dia_labels(str(v.get("evidence") or "")))
    labels = sorted(set(round(x, 4) for x in labels if x > 0))
    # Ø0.02 这类「公差带直径」不是零件尺寸：比零件还小两个数量级的 Ø 一律不当几何标注核对，
    # 否则形位公差框会把合法的单剖视回转体判成不合格（实测间隔套 Ø78.5 被 Ø0.02 卡死）。
    _floor = max(0.5, 0.004 * dia_profile)
    tiny = [d for d in labels if d < _floor]
    labels = [d for d in labels if d >= _floor]
    if not labels:
        return False, "剖视依据里没有可核对的 Ø 标注"
    misread = _misread_dia_labels(spec)
    missing = []
    ignored = []
    covered = 0
    for d in labels:
        rr = d / 2.0
        if any(abs(r - rr) <= max(0.25, 0.005 * d) for r in radii):
            covered += 1
            continue
        if round(d, 4) in misread:
            ignored.append(d)
            continue
        missing.append(f"Ø{d:g}(r={rr:g})")
    if missing:
        return False, "母线与剖视标注未覆盖：" + "、".join(missing)
    extra = ""
    if ignored:
        extra = ("；已排除 notes 判定的 OCR 误识 "
                 + "/".join(f"Ø{d:g}" for d in ignored))
    if tiny:
        extra += ("；已忽略公差带 Ø" + "/".join(f"{d:g}" for d in tiny)
                  + "（形位公差，不是几何直径）")
    return True, (f"完整回转体单轴向剖视严格例外：母线闭合、Ø{dia_profile:g}×{axial:g} "
                  f"与剖视一致，{covered} 个 Ø 标注均被母线覆盖{extra}")


def _self_proving_revolve(spec: PartSpec) -> Tuple[bool, str]:
    """没有 views 时的回转体兜底：母线本身必须闭合、尺寸链自洽。

    只对「整张图就是一个回转体」生效：母线首尾闭合、最大半径=参数外径、
    轴向跨度=参数厚度，且 spec 里的同心孔直径都能在母线上找到。普通零件
    没有母线，直接拒绝，绝不把缺三视图的图纸放行。
    """
    if spec.base_kind != "round" or float(spec.diameter or 0) <= 0:
        return False, "基体不是有明确直径的回转体"
    prof = [x for x in (getattr(spec, "revolve_profile", None) or [])
            if isinstance(x, dict)]
    if len(prof) < 3:
        return False, "闭合回转母线少于 3 段"

    radii: List[float] = []
    zs: List[float] = []
    for i, seg in enumerate(prof):
        typ = str(seg.get("type") or "").lower()
        if typ not in ("line", "arc"):
            return False, f"母线第 {i+1} 段不是直线/真圆弧"
        p1, p2 = _profile_point(seg, "p1"), _profile_point(seg, "p2")
        if p1 is None or p2 is None:
            return False, f"母线第 {i+1} 段端点无效"
        if p1[0] < -1e-9 or p2[0] < -1e-9:
            return False, f"母线第 {i+1} 段出现负半径"
        if abs(p1[0] - p2[0]) <= 1e-6 and abs(p1[1] - p2[1]) <= 1e-6:
            return False, f"母线第 {i+1} 段长度为零"
        radii.extend((p1[0], p2[0]))
        zs.extend((p1[1], p2[1]))

    for i in range(len(prof) - 1):
        if not _close_pt(_profile_point(prof[i], "p2"),
                         _profile_point(prof[i + 1], "p1")):
            return False, f"母线第 {i+1}/{i+2} 段不连续"
    if not _close_pt(_profile_point(prof[-1], "p2"),
                     _profile_point(prof[0], "p1")):
        return False, "回转母线不闭合"

    rmax = max(radii)
    axial = max(zs) - min(zs)
    D, T = float(spec.diameter), float(spec.thickness)
    if abs(2.0 * rmax - D) > max(0.25, 0.005 * D):
        return False, f"母线最大直径 {2*rmax:g} 与参数 Ø{D:g} 不一致"
    if abs(axial - T) > max(0.25, 0.005 * T):
        return False, f"母线轴向跨度 {axial:g} 与厚度 {T:g} 不一致"

    # 同心孔（x=y=0）必须由母线表达，否则建模会重复扣料。
    covered = 0
    for f in (getattr(spec, "features", None) or []):
        try:
            concentric = abs(float(f.x or 0)) <= 0.6 and abs(float(f.y or 0)) <= 0.6
            dia = float(f.diameter or 0)
        except Exception:
            continue
        if str(getattr(f, "type", "")).lower() != "hole" or not concentric or dia <= 0:
            continue
        if any(abs(r - dia / 2.0) <= max(0.2, 0.005 * dia) for r in radii):
            covered += 1
        else:
            return False, f"同心孔 Ø{dia:g} 未被母线表达（会重复切除）"
    bore = float(getattr(spec, "bore", 0.0) or 0.0)
    if bore > 0 and not any(abs(r - bore / 2.0) <= max(0.2, 0.005 * bore) for r in radii):
        return False, f"内孔 Ø{bore:g} 未被母线表达"

    return True, (f"母线闭合、Ø{D:g}×{T:g}、轴向 {axial:g}，"
                  f"{len(prof)} 段母线，{covered} 个同心孔全部落在母线上")


def validate_three_views(raw_views: Any, spec: PartSpec,
                         require_views: bool = False) -> ThreeViewReport:
    """三视图一致性核对。require_views=False 时，单视图图纸只提示不阻断。"""
    views = normalize_views(raw_views)
    rep = ThreeViewReport(views=views)
    if not views:
        # 回转体（轴/套/法兰/盘）常只给一个轴向全剖视，识图也可能完全不返回
        # views 结构。此时只要母线闭合、尺寸链自洽、同心孔全部落在母线上，
        # 就按「单剖视回转体」放行；普通零件仍然阻断。
        self_ok, self_detail = _self_proving_revolve(spec)
        if self_ok:
            rep.exception = self_detail
            _check(rep.checks, "回转体单剖视自证", True, self_detail)
            rep.uncertain.append(
                "图纸未返回三视图结构；回转体按闭合母线 + 尺寸链自证，建议人工核对母线")
            return rep
        msg = "识图没有返回三视图结构（views），无法做长对正/高平齐/宽相等的交叉核对"
        msg += "；回转体单剖视自证也不成立：" + self_detail
        if require_views:
            rep.blocking.append(msg)
        else:
            rep.uncertain.append(msg)
        return rep

    by: Dict[str, dict] = {}
    for v in views:
        by.setdefault(v["name"], v)
    # 同名视图去重时保留第一个，并提示
    if len(by) < len(views):
        rep.uncertain.append("同一视图出现多次，仅采用第一份做尺寸核对")

    # ---- 1) 视图之间：长对正 / 高平齐 / 宽相等
    pairs: List[Tuple[str, str, str, str]] = [
        ("main", "top", "L", "长对正"),
        ("main", "side", "H", "高平齐"),
        ("top", "side", "W", "宽相等"),
    ]
    for a, b, axis, label in pairs:
        va, vb = by.get(a), by.get(b)
        if not va or not vb:
            continue
        da, db = _view_dim(va, axis), _view_dim(vb, axis)
        if da <= 0 or db <= 0:
            rep.uncertain.append(f"{VIEW_LABELS.get(a)}/{VIEW_LABELS.get(b)} 的{axis}尺寸缺失，"
                                 f"无法核对「{label}」")
            continue
        ok = abs(da - db) <= _tol(da, db)
        _detail = (f"{label}一致：{VIEW_LABELS.get(a, a)} {da:g} mm = "
                   f"{VIEW_LABELS.get(b, b)} {db:g} mm") if ok else (
                   f"{label}失败：{VIEW_LABELS.get(a, a)} {da:g} mm，"
                   f"{VIEW_LABELS.get(b, b)} {db:g} mm")
        _check(rep.checks, f"{label}({a}.{axis}↔{b}.{axis})", ok, _detail)

    # ---- 2) 视图与 spec：L/W/T
    exp = {"L": float(spec.length), "W": float(spec.width), "H": float(spec.thickness)}
    _ptype = str(getattr(spec, "part_type", "") or "").lower()
    _revolve_axis = (spec.base_kind == "round" and spec.diameter > 0
                     and (bool(getattr(spec, "revolve_profile", None))
                          or _ptype in ("shaft", "sleeve", "ring", "bushing", "disc")))
    if _revolve_axis:
        # 回转体按轴向剖视出图：主视图水平方向是轴向长度(thickness)，
        # 竖直方向是最大外径；不能再拿直径去比主视图水平方向。
        exp = {"L": float(spec.thickness), "W": float(spec.diameter),
               "H": float(spec.diameter)}
        # 模型常把回转体的轴向剖视画成“宽=直径、高=总长”（横纵互换），
        # 例如实测 76.5x31 被写成 spec 的 31x76.5；这里只在两对数完全
        # 互补时交换，并留痕，不能掩盖真正不一致的视图。
        _m = by.get("main")
        if _m:
            _ml, _mh = _view_dim(_m, "L"), _view_dim(_m, "H")
            _D, _T = float(spec.diameter), float(spec.thickness)
            if (_ml > 0 and _mh > 0 and _T > 0 and _D > 0
                    and abs(_ml - _D) <= _tol(_ml, _D)
                    and abs(_mh - _T) <= _tol(_mh, _T)
                    and abs(_ml - _T) > _tol(_ml, _T)):
                _m["outline"]["length"], _m["outline"]["height"] = _mh, _ml
                rep.uncertain.append(
                    f"主视图外形按轴向剖视读图：宽=Ø{_D:g}、高={_T:g}（横纵互换后核对）")
    elif spec.base_kind == "round" and spec.diameter > 0:
        exp["L"] = exp["W"] = float(spec.diameter)
    for view_name, axes in (("main", ("L", "H")), ("top", ("L", "W")), ("side", ("W", "H"))):
        v = by.get(view_name)
        if not v:
            continue
        for axis in axes:
            got = _view_dim(v, axis)
            if got <= 0 or exp[axis] <= 0:
                continue
            ok = abs(got - exp[axis]) <= _tol(got, exp[axis])
            _detail = (f"{VIEW_LABELS.get(view_name, view_name)} {axis}={got:g} mm 与参数 "
                       f"{exp[axis]:g} mm 一致") if ok else (
                       f"{VIEW_LABELS.get(view_name, view_name)} {axis}={got:g} mm 与参数 "
                       f"{exp[axis]:g} mm 不一致")
            _check(rep.checks, f"视图↔参数({view_name}.{axis})", ok, _detail)
    # ---- 3) 特征投影：孔位必须落在俯视图轮廓内
    top = by.get("top")
    if top and top["outline"].get("length", 0) > 0 and top["outline"].get("width", 0) > 0:
        halfL = top["outline"]["length"] / 2.0
        halfW = top["outline"]["width"] / 2.0
        for f in spec.features:
            if f.type not in ("hole", "pocket", "slot", "step", "boss", "sphere"):
                continue
            if abs(f.x) > halfL + _tol(halfL, f.x) or abs(f.y) > halfW + _tol(halfW, f.y):
                msg = (f"特征 {f.type} Ø{f.diameter:g}/L{f.length:g} @({f.x:g},{f.y:g}) "
                       f"超出俯视图轮廓 {2*halfL:g}×{2*halfW:g}")
                rep.blocking.append(msg)
                _check(rep.checks, "特征在轮廓内", False, msg)

    # ---- 4) 孔位投影依据：如果视图里列了孔，至少要有一处与 spec 对上
    view_holes: List[dict] = []
    for v in views:
        for f in v.get("features") or []:
            if f.get("type") in ("hole", "孔", "圆孔"):
                view_holes.append({**f, "view": v["name"]})
    spec_holes = [f for f in spec.features if f.type == "hole"]
    if spec_holes and view_holes:
        unmatched = []
        for h in spec_holes:
            hit = False
            for vh in view_holes:
                same_d = (vh.get("diameter") or 0) <= 0 or abs((vh.get("diameter") or 0) - h.diameter) <= 0.6
                # 主视图只对 x，侧视图只对 y，俯视图 x/y 都对
                vn = vh.get("view")
                if vn == "main":
                    same_pos = abs(vh.get("x", 0) - h.x) <= 0.8
                elif vn == "side":
                    same_pos = abs(vh.get("y", 0) - h.y) <= 0.8
                else:
                    same_pos = (abs(vh.get("x", 0) - h.x) <= 0.8 and
                                abs(vh.get("y", 0) - h.y) <= 0.8)
                if same_d and same_pos:
                    hit = True
                    break
            if not hit:
                unmatched.append(f"Ø{h.diameter:g}@({h.x:g},{h.y:g})")
        if unmatched:
            msg = "以下孔在 views 里找不到对应投影（可能漏标或坐标错）：" + "、".join(unmatched[:6])
            rep.uncertain.append(msg)
            _check(rep.checks, "孔位投影依据", False, msg, severity="uncertain")

    # ---- 5) 必需视图缺失
    missing = [k for k in ("main", "top", "side") if k not in by]
    if missing:
        strict_ok, strict_detail = _strict_round_section_exception(views, spec)
        if strict_ok:
            rep.exception = strict_detail
            _check(rep.checks, "单剖视回转体严格核对", True, strict_detail)
        else:
            msg = "缺少" + "、".join(VIEW_LABELS[k] for k in missing) + "，无法完整核对三视图"
            # 不论视图叫 section 还是 main，都要把例外不成立的具体原因带出来，
            # 否则用户只看到“缺视图”，无法知道是母线不闭合还是 Ø 标注没覆盖。
            msg += "；单剖视回转体严格例外不成立：" + strict_detail
            if require_views:
                rep.blocking.append(msg)
            else:
                rep.uncertain.append(msg)

    return rep
