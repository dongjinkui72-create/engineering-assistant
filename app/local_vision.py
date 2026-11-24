# -*- coding: utf-8 -*-
"""本地识图 + 本地图纸检验（完全离线）。

流程
----
1. RapidOCR 读图纸文字（local_ocr.py）。
2. 连通域找视图外框，按「长对正 / 高平齐 / 宽相等」的像素比例配出
   主视图 / 俯视图 / 侧视图。
3. 用 OCR 数字和三个视图的像素比例反解总体尺寸（要求三视图的
   px/mm 一致，不一致就判图纸矛盾）。
4. 在俯视图里量圆孔 / 槽 / 型腔的像素轮廓，换算成 mm 和零件坐标。
5. 用 drawing_audit.py 的确定性规则做最终门禁。

这不是“猜图”：任何一步证据不足都会写进 blocking，界面会禁止建模。
外部云端识图仍可作为可选项（Settings.vision_mode = "cloud"）。
"""
from __future__ import annotations

import itertools
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .local_ocr import (DimSet, OcrDoc, OcrToken, group_lines, normalize_text,
                        parse_dimensions, run_ocr, engine_status)
from .models import Feature, PartSpec


# ---------------------------------------------------------------- data model
@dataclass
class LocalIssue:
    severity: str            # error | warn | info
    code: str
    message: str
    suggestion: str = ""


@dataclass
class ViewBox:
    page: int
    path: str
    bbox: Tuple[int, int, int, int]
    w: int
    h: int
    contained: int = 0
    dark: Any = None
    comps: Any = None

    @property
    def area(self) -> int:
        return self.w * self.h


@dataclass
class LocalCheck:
    spec: Optional[PartSpec] = None
    ocr: OcrDoc = field(default_factory=OcrDoc)
    dims: DimSet = field(default_factory=DimSet)
    views: List[dict] = field(default_factory=list)
    issues: List[LocalIssue] = field(default_factory=list)
    notes: str = ""
    confidence: float = 0.0
    can_build: bool = False
    audit: dict = field(default_factory=dict)
    report_md: str = ""
    shape: str = "rect"
    view_count: int = 0
    matching_error: float = 0.0
    round_inner: List[float] = field(default_factory=list)
    round_holes: List[Tuple[float, float, float]] = field(default_factory=list)
    round_chamfer: float = 0.0

    @property
    def blocking(self) -> List[str]:
        return [i.message for i in self.issues if i.severity == "error"]

    @property
    def uncertain(self) -> List[str]:
        return [i.message for i in self.issues if i.severity == "warn"]


# ---------------------------------------------------------------- view boxes
def _bbox_iou(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    ua = max(1, (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter)
    return inter / float(ua)


def _aspect_ok(ar: float) -> bool:
    return 0.16 <= ar <= 6.5


def find_view_boxes(paths: Sequence[str], log=None) -> List[ViewBox]:
    """在每一页里找“零件视图外框”。

    判据（全部确定性）：
    * 面积占页面 2.5%~55%，长宽比 0.16~6.5；
    * 不贴页边（排除图框/标题栏）；
    * 笔画占比 < 32%（排除实心块、密集文字区）；
    * 优先包含其它闭合轮廓（孔/槽）的大轮廓。
    """
    from . import measure

    log = log or (lambda *_a, **_k: None)
    out: List[ViewBox] = []
    for pg, path in enumerate(paths):
        if Path(path).suffix.lower() not in {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}:
            continue
        gray = measure._load_gray(str(path))
        if gray is None:
            continue
        a, _k = gray
        h, w = a.shape
        dark = a < measure._DARK
        if float(dark.mean()) > 0.5:
            dark = ~dark
        try:
            comps = measure._components(dark)
        except Exception as e:
            log(f"⚠ {Path(path).name} 视图分割失败：{e}")
            continue
        cand: List[ViewBox] = []
        for pts in comps:
            if len(pts) < 120:
                continue
            x1, y1, x2, y2 = measure._bbox(pts)
            bw, bh = x2 - x1 + 1, y2 - y1 + 1
            if bw < 55 or bh < 40:
                continue
            ar = bw / float(bh)
            if not _aspect_ok(ar):
                continue
            area_ratio = (bw * bh) / float(w * h)
            if not (0.025 <= area_ratio <= 0.55):
                continue
            # 贴页边：图框或标题栏
            if x1 < 0.012 * w or y1 < 0.012 * h or x2 > 0.988 * w or y2 > 0.988 * h:
                continue
            fill = len(pts) / float(bw * bh)
            if fill > 0.32:
                continue
            cand.append(ViewBox(pg, str(path), (x1, y1, x2, y2), bw, bh, dark=dark, comps=comps))
        # 去重（同一外框可能被双线画成两个连通域）
        kept: List[ViewBox] = []
        for b in sorted(cand, key=lambda z: -z.area):
            if any(_bbox_iou(b.bbox, k.bbox) > 0.72 for k in kept):
                continue
            kept.append(b)
        # 包含的闭合轮廓越多，越像视图外框
        for b in kept:
            n = 0
            for c in comps:
                if len(c) < 18:
                    continue
                cb = measure._bbox(c)
                if (b.bbox[0] + 2 < cb[0] and cb[2] < b.bbox[2] - 2
                        and b.bbox[1] + 2 < cb[1] and cb[3] < b.bbox[3] - 2):
                    n += 1
            b.contained = n
        kept.sort(key=lambda z: (-(z.contained >= 1), -z.area))
        out.extend(kept[:5])
        log(f"🔎 {Path(path).name}: 找到 {len(kept[:5])} 个候选视图框")
    return out


# ---------------------------------------------------------------- view matching
@dataclass
class _DimItem:
    box: int
    side: str          # w | h
    value: float


def _matchings(items: List[_DimItem]) -> Iterable[List[Tuple[_DimItem, _DimItem]]]:
    if not items:
        yield []
        return
    a = items[0]
    for k in range(1, len(items)):
        b = items[k]
        if b.box == a.box:
            continue
        rest = items[1:k] + items[k + 1:]
        for tail in _matchings(rest):
            yield [(a, b)] + tail


def _triple_candidates(boxes: Sequence[ViewBox]) -> List[Tuple[float, Tuple[int, int, int], List[Tuple[_DimItem, _DimItem]]]]:
    out = []
    for idx in itertools.combinations(range(len(boxes)), 3):
        items: List[_DimItem] = []
        for i in idx:
            b = boxes[i]
            items.append(_DimItem(i, "w", float(b.w)))
            items.append(_DimItem(i, "h", float(b.h)))
        best = None
        for mt in _matchings(items):
            errs = []
            for a, b in mt:
                r = max(a.value, b.value) / max(1e-9, min(a.value, b.value))
                errs.append(abs(math.log(r)))
            err = sum(errs) / len(errs)
            if best is None or err < best[0]:
                best = (err, mt)
        if best and best[0] < 0.11:
            out.append((best[0], idx, best[1]))
    out.sort(key=lambda z: z[0])
    return out


def _unique_numbers(ds: DimSet, lo: float = 0.5, hi: float = 3000.0) -> List[float]:
    trivial = {d.value for d in ds.diameters} | {d.value for d in ds.radii} | \
              {d.value for d in ds.spheres} | {d.value for d in ds.depths} | \
              {d.value for d in ds.angles} | {d.value for d in ds.chamfers}
    vals: List[float] = []
    for d in ds.lengths:
        v = float(d.value)
        if not (lo <= v <= hi):
            continue
        if any(abs(v - t) < 1e-6 for t in trivial):
            continue
        if all(abs(v - x) > 1e-6 for x in vals):
            vals.append(v)
    # 大尺寸更可能是总体尺寸，排前面；但保留全部供比例校验
    vals.sort(key=lambda v: -v)
    return vals[:20]


def _solve_dims(matching: List[Tuple[_DimItem, _DimItem]], ds: DimSet,
                view_count: int) -> Tuple[Optional[List[float]], float, List[str]]:
    """用三视图像素比例 + OCR 数字反解三条总体尺寸。

    返回 (pair_values, 比例一致性误差, 提示)。pair_values 与 matching 一一对应，
    单位是 mm；哪一条是 L/W/T 由 _assign_axes 按视图布局决定。
    """
    warns: List[str] = []
    px = []
    for a, b in matching:
        px.append((a.value / b.value + b.value / a.value) * 0.5)

    vals = _unique_numbers(ds)
    if len(vals) < 2:
        return None, 9.9, ["图上可用的总体数字少于 2 个"]

    labeled = dict(ds.labeled or {})
    best = None
    cand_vals = vals[:14]
    triples = list(itertools.combinations_with_replacement(cand_vals, 3))
    for t in triples:
        if len(set(round(x, 6) for x in t)) == 1:
            continue
        for perm in set(itertools.permutations(t)):
            scales = [perm[k] / px[k] for k in range(3)]
            mean = sum(scales) / 3.0
            if mean <= 0:
                continue
            err = max(abs(sc - mean) / mean for sc in scales)
            # 明确标注的长/宽/厚是最强证据：哪个排列能让标注值落在对应位置，加分。
            pen = 0.0
            if labeled:
                # 这里还不知道 pair 的轴，先把标注值集合与 perm 集合做最近匹配，
                # 只排除“完全对不上”的排列。
                lv = [float(v) for v in labeled.values() if float(v) > 0]
                if lv:
                    dmin = min(abs(sorted(perm)[i] - sorted(lv)[i])
                               for i in range(min(len(perm), len(lv))))
                    pen += dmin / max(1.0, max(lv))
            score = err + 0.15 * pen
            if best is None or score < best[0]:
                best = (score, err, list(perm))
    if best is not None and best[0] <= 0.075:
        return best[2], float(best[1]), warns

    # 只有两个数时，用比例推第三维（必须两维比例高度一致）
    if len(vals) >= 2:
        best2 = None
        for vpair in itertools.permutations(vals[:12], 2):
            for lab_pair in ((0, 1), (0, 2), (1, 2)):
                s0 = vpair[0] / px[lab_pair[0]]
                s1 = vpair[1] / px[lab_pair[1]]
                mean = (s0 + s1) / 2.0
                err = abs(s0 - s1) / max(mean, 1e-9)
                if best2 is None or err < best2[0]:
                    best2 = (err, vpair, lab_pair, mean)
        if best2 is not None and best2[0] <= 0.035:
            err, vpair, lab_pair, mean = best2
            vals3 = [0.0, 0.0, 0.0]
            vals3[lab_pair[0]] = vpair[0]
            vals3[lab_pair[1]] = vpair[1]
            third = ({0, 1, 2} - set(lab_pair)).pop()
            vals3[third] = mean * px[third]
            if 0.5 <= vals3[third] <= 3000:
                warns.append(f"第 {third + 1} 条总体尺寸未直接标注，由三视图比例推算为 "
                             f"{vals3[third]:.2f} mm（请人工核对）")
                return vals3, float(err), warns

    return None, 9.9, ["OCR 数字与三视图比例对不上，不能确定 L/W/T"]


def _assign_axes(boxes: Sequence[ViewBox], chosen: Sequence[int],
                 matching: List[Tuple[_DimItem, _DimItem]],
                 pair_vals: Sequence[float], ds: DimSet) -> Optional[Tuple[Dict[str, float], int, int, int]]:
    """把三条像素尺寸落到 L/W/T，并找出主视图/俯视图/侧视图。

    标准三视图布局：
      * 主视图和俯视图上下对齐（共享 L）；
      * 主视图和侧视图左右对齐（共享 T）；
      * 俯视图和侧视图共享 W。
    第一角/第三角只影响俯视图在上还是在下，不改变共享关系。
    """
    if len(chosen) != 3 or len(matching) != 3 or len(pair_vals) != 3:
        return None
    pm: Dict[Tuple[int, int], float] = {}
    for (a, b), v in zip(matching, pair_vals):
        pm[tuple(sorted((a.box, b.box)))] = float(v)

    def cx(i: int) -> float:
        b = boxes[i]
        return (b.bbox[0] + b.bbox[2]) * 0.5

    def cy(i: int) -> float:
        b = boxes[i]
        return (b.bbox[1] + b.bbox[3]) * 0.5

    # 上下对齐的一对=主视图/俯视图；另一个=侧视图
    best = None
    for i, j in itertools.combinations(chosen, 2):
        k = [x for x in chosen if x not in (i, j)][0]
        dx = abs(cx(i) - cx(j))
        dy = abs(cy(i) - cy(j))
        # 上下对齐：x 接近，y 拉开
        score = dx / max(1.0, dy + dx)
        if best is None or score < best[0]:
            best = (score, i, j, k)
    if best is None:
        return None
    _s, i, j, k = best
    # i/j 中靠上的那个先当“主视图”，靠下的当“俯视图”；若上下关系反过来，
    # 只是 W/T 互换，下面用厚度标注或较小值纠正。
    up, down = (i, j) if cy(i) <= cy(j) else (j, i)
    L = pm[tuple(sorted((up, down)))]
    main_extra = pm[tuple(sorted((up, k)))]
    top_extra = pm[tuple(sorted((down, k)))]

    # 厚度优先用 OCR 的“厚/高”标注；否则薄板取较小值。
    want_t = float(ds.labeled.get("thickness") or ds.labeled.get("height") or 0.0)
    if want_t > 0:
        if abs(main_extra - want_t) <= abs(top_extra - want_t):
            T, W = main_extra, top_extra
        else:
            T, W = top_extra, main_extra
    else:
        T, W = (main_extra, top_extra) if main_extra <= top_extra else (top_extra, main_extra)
    return {"L": L, "W": W, "T": T}, up, down, k

def _near_view_numbers(ds: DimSet, boxes: Sequence[ViewBox],
                       pad: float = 0.65, min_x: float = 40.0,
                       min_y: float = 60.0) -> List[Any]:
    """只保留落在视图外框附近的裸数字，排除标题栏/技术要求里的数字。"""
    out = []
    for d in ds.lengths:
        for b in boxes:
            x1, y1, x2, y2 = b.bbox
            # 侧视图常是细长条：按高取 pad 会只剩很小范围，
            # 标注数字被挡在外面。两个方向都给绝对下限。
            px = max(pad * max(1, x2 - x1), min_x)
            py = max(pad * max(1, y2 - y1), min_y)
            if x1 - px <= d.x <= x2 + px and y1 - py <= d.y <= y2 + py:
                out.append(d)
                break
    return out


@dataclass
class _RoundGuess:
    """单视图回转体的确定性读数结果。"""
    dims: Dict[str, float]
    bores: List[float]
    holes: List[Tuple[float, float, float]]
    note: str
    chamfer: float = 0.0


def _round_simple(ds: DimSet, boxes: Sequence[ViewBox], doc: OcrDoc = None):
    """简单回转体（盘/套/环/法兰）的单视图识别。

    只做“读数归类”，不猜几何：
      * 外径 = OCR 里最大的直径标注；
      * 节圆 = 图上写有“均布/节圆/PCD”且存在 n×Ød 时，取比外径小的最大非孔直径；
      * 内孔 = 其余未计数的直径（< 92% 外径）；
      * n×Ød + 节圆 -> 直接展开成 n 个孔位（角度均分）；
      * 轴向厚度取视图附近的裸数字，排除已认定为直径/节圆的值。
    证据不足（无外径、厚度候选冲突、槽/球面/扫掠）一律返回 None。
    """
    text = "\n".join(t.text for t in doc.tokens) if doc is not None else ""
    if "槽" in text or getattr(ds, "spheres", None):
        return None
    if re.search(r"抽壳|扫掠|球坑|螺旋|螺纹", text):
        return None
    if len(getattr(ds, "radii", []) or []) > 2:
        return None

    counts: Dict[float, int] = {}
    for d in ds.diameters:
        try:
            v = float(d.value)
        except Exception:
            continue
        if v < 3.0:
            continue
        counts[v] = max(counts.get(v, 1), int(d.count or 1))
    if not counts:
        return None
    D = max(counts)

    # 节圆 + 螺栓孔
    pcd = None
    bolt = {v: c for v, c in counts.items() if c >= 2 and v < 0.5 * D}
    if bolt and re.search(r"节圆|分布圆|PCD|均布", text, re.I):
        single = [v for v, c in counts.items() if c <= 1 and v < 0.98 * D]
        if single:
            pcd = max(single)
    holes: List[Tuple[float, float, float]] = []
    n_hole = 0
    if pcd is not None:
        for dv, n in sorted(bolt.items()):
            r = pcd / 2.0
            if r + dv / 2.0 > D / 2.0 + 1e-6 or r - dv / 2.0 < 0:
                return None                     # 孔超出外圆或穿过圆心：图纸矛盾
            n_hole += int(n)
            for i in range(int(n)):
                a = 2.0 * math.pi * i / float(n)
                holes.append((round(r * math.cos(a), 3), round(r * math.sin(a), 3), dv))

    bores = sorted([v for v, c in counts.items()
                    if c <= 1 and v < 0.92 * D and v != pcd], reverse=True)

    # 轴向厚度：优先“厚/高”标注，其次视图附近的裸数字（排除直径与节圆）
    known = {round(k, 3) for k in counts}
    if pcd is not None:
        known.add(round(pcd, 3))
    t_lab = float(ds.labeled.get("thickness") or ds.labeled.get("height") or 0.0)
    cands: List[float] = []
    if t_lab > 0:
        cands = [t_lab]
    else:
        for d in _near_view_numbers(ds, boxes):
            v = float(d.value)
            if round(v, 3) in known:
                continue
            if 0.06 * D <= v <= 0.70 * D:
                cands.append(v)
        for d in getattr(ds, "depths", []) or []:
            v = float(d.value)
            if round(v, 3) in known:
                continue
            if 0.06 * D <= v <= 0.70 * D:
                cands.append(v)
    cands = sorted({round(v, 3) for v in cands if v > 0})
    if not cands:
        # 单视图回转体：厚度常标在侧视图外侧，可能落在视图框之外。
        # 严格兜底：排除已认定的直径/节圆，且全图只能剩一个候选。
        for d in ds.lengths:
            v = float(d.value)
            if round(v, 3) in known:
                continue
            if 0.06 * D <= v <= 0.70 * D:
                cands.append(v)
        cands = sorted({round(v, 3) for v in cands if v > 0})
        if len(cands) != 1:
            return None
    T = float(cands[0])
    if len(cands) >= 2 and abs(cands[1] - cands[0]) / max(cands[0], 1e-9) <= 0.25:
        if t_lab > 0:
            T = t_lab
        else:
            return None          # 两个差不多的候选，分不清轴向后不猜
    if not (0.04 * D <= T <= 1.2 * D):
        return None

    cham = 0.0
    vals = sorted({float(c.value) for c in (getattr(ds, "chamfers", []) or [])
                   if float(c.value or 0) > 0})
    if len(vals) == 1:
        cham = vals[0]

    bits = [f"外径 Ø{D:g}", f"厚度 {T:g}"]
    if pcd is not None and holes:
        bits.append(f"节圆 Ø{pcd:g} 上 {n_hole}×Ø{holes[0][2]:g} 均布")
    if bores:
        bits.append("内孔 " + "、".join(f"Ø{v:g}" for v in bores))
    if cham:
        bits.append(f"未注倒角 C{cham:g}")
    note = "单视图回转体（OCR 确定性读数）：" + "，".join(bits) + "；未配出完整三视图"
    return _RoundGuess(dims={"L": D, "W": D, "T": T}, bores=bores,
                       holes=holes, note=note, chamfer=cham)


def _round_dims(ds: DimSet, boxes: Sequence[ViewBox]) -> Optional[Dict[str, float]]:
    """圆盘/回转体：优先 厚/高 标签，其次 直径 + 视图比例。"""
    if not ds.diameters:
        return None
    d = max((x.value for x in ds.diameters), default=0.0)
    if d <= 0:
        return None
    t = float(ds.labeled.get("thickness") or ds.labeled.get("height") or 0.0)
    if t <= 0 and ds.depths:
        t = float(ds.depths[0].value)
    if t > 0:
        return {"L": d, "W": d, "T": t}
    # 用接近圆形的视图与其它视图的比例推厚度
    circ = [b for b in boxes if abs(b.w / float(b.h) - 1.0) < 0.08]
    if circ and len(boxes) >= 2:
        c = circ[0]
        sc = c.w / d
        for b in boxes:
            if b is c:
                continue
            for v in (b.h, b.w):
                tv = v / sc
                if 0.5 <= tv <= 1000:
                    return {"L": d, "W": d, "T": round(tv, 2)}
    return None


# ---------------------------------------------------------------- features
def _measure_view_features(view: ViewBox, part_w: float, part_h: float,
                           scale: float) -> List[Any]:
    from . import measure
    if view.dark is None or view.comps is None or scale <= 0:
        return []
    a = view.dark
    bx1, by1, bx2, by2 = view.bbox
    mx = max(2.0, 0.02 * (bx2 - bx1))
    my = max(2.0, 0.02 * (by2 - by1))
    min_side_mm = 1.2
    max_side_mm = 0.95 * min(part_w, part_h) if min(part_w, part_h) > 0 else 1e9
    items = []
    for pts in view.comps:
        if len(pts) < 20:
            continue
        x1, y1, x2, y2 = measure._bbox(pts)
        bw, bh = x2 - x1 + 1, y2 - y1 + 1
        if bw < 7 or bh < 7:
            continue
        if bw / float(bh) > 12 or bh / float(bw) > 12:
            continue
        if x1 < bx1 + mx or y1 < by1 + my or x2 > bx2 - mx or y2 > by2 - my:
            continue
        w_mm, h_mm = bw / scale, bh / scale
        if min(w_mm, h_mm) < min_side_mm or max(w_mm, h_mm) > max_side_mm:
            continue
        aspect = max(w_mm, h_mm) / max(min(w_mm, h_mm), 1e-6)
        ccx, ccy = (x1 + x2) * 0.5, (y1 + y2) * 0.5
        r, cover = measure._ring_radius(pts, ccx, ccy, bw, bh)
        dia_mm = 2.0 * r / scale if r > 0 else 0.0
        squared = measure._squared_corners(a, x1, y1, x2, y2)
        if (aspect <= 1.3 and cover >= 0.5 and dia_mm > 0
                and 0.75 * min(w_mm, h_mm) <= dia_mm <= 1.25 * max(w_mm, h_mm)):
            kind = "circle"
        elif aspect >= 1.3 and not squared:
            kind = "slot"
        else:
            kind = "rect"
        items.append(measure.PixelFeature(
            kind=kind, cx=(ccx - (bx1 + bx2) * 0.5) / scale,
            cy=-(ccy - (by1 + by2) * 0.5) / scale,
            w_mm=w_mm, h_mm=h_mm,
            dia_mm=dia_mm if kind == "circle" else 0.0,
            px_bbox=(x1, y1, x2, y2), squared=squared))
    kept = []
    for it in sorted(items, key=lambda f: -(f.w_mm * f.h_mm)):
        if any(measure._iou(it.px_bbox, k.px_bbox) > 0.60 for k in kept):
            continue
        kept.append(it)
    return kept[:24]


def measure_plate_features(paths: Sequence[str], length: float, width: float,
                           log=None) -> Optional[Dict[str, Any]]:
    """为矩形板定位俯视图，并用像素量出孔 / 槽 / 型腔轮廓。

    只做确定性测量：视图框长宽比必须与 L/W 一致（±12%），
    优先选择包含其它闭合轮廓的外框（避免把型腔本身当成视图）。
    返回 {box, scale, features}；找不到可信俯视图时返回 None。
    """
    length = float(length or 0.0)
    width = float(width or 0.0)
    if length <= 0 or width <= 0:
        return None
    boxes = find_view_boxes(paths, log=log)
    if not boxes:
        return None
    cands = []
    for swap in (False, True):
        a, b = (width, length) if swap else (length, width)
        for box in boxes:
            s1 = box.w / a
            s2 = box.h / b
            if s1 <= 0 or s2 <= 0:
                continue
            r = s1 / s2
            if not (0.88 <= r <= 1.14):
                continue
            cands.append((box, 0.5 * (s1 + s2)))
        if cands:
            break
    if not cands:
        return None
    # 视图外框会包含孔/槽/型腔；型腔自身没有内嵌轮廓。
    cands.sort(key=lambda t: (t[0].contained >= 2, t[0].area), reverse=True)
    box, sc = cands[0]
    feats = _measure_view_features(box, length, width, sc)
    return {"box": box, "scale": sc, "features": feats}


def measure_views_for_spec(paths: Sequence[str], spec, log=None) -> List[dict]:
    """为一份已确定的 PartSpec 生成「像素实测视图证据」，供三视图互校使用。

    只做确定性测量：视图外框必须是真实检出的闭合轮廓框，长宽比与 spec 一致；
    比例尺只用俯视图的长边定标，短边与主/侧视图的尺寸因此是独立核对，
    不是把 spec 抄进 views（否则审计会变成自证）。

    返回 normalize_views 可接受的结构；找不到可信视图时返回 []。
    """
    images = [str(f) for f in paths if Path(f).suffix.lower() in
              {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}]
    if not images:
        return []
    L = float(getattr(spec, "length", 0.0) or 0.0)
    W = float(getattr(spec, "width", 0.0) or 0.0)
    T = float(getattr(spec, "thickness", 0.0) or 0.0)
    if str(getattr(spec, "base_kind", "")) != "rect" or min(L, W, T) <= 0:
        return []
    try:
        boxes = find_view_boxes(images, log=log)
    except Exception:
        return []
    if not boxes:
        return []

    def _pick(a_mm: float, b_mm: float, exclude=None):
        cands = []
        for swap in (False, True):
            a, b = (b_mm, a_mm) if swap else (a_mm, b_mm)
            for box in boxes:
                if exclude is not None and box is exclude:
                    continue
                s1, s2 = box.w / a, box.h / b
                if s1 <= 0 or s2 <= 0:
                    continue
                r = s1 / s2
                if not (0.85 <= r <= 1.18):
                    continue
                cands.append((box, swap, 0.5 * (s1 + s2)))
            if cands:
                break
        if not cands:
            return None
        cands.sort(key=lambda t: (t[0].contained >= 2, t[0].area), reverse=True)
        return cands[0]

    top_pick = _pick(L, W)
    if top_pick is None:
        return []
    box, swap, _sc = top_pick
    # 只用长边定标：短边就成了对照 spec 的独立测量
    sc = (box.h / L) if swap else (box.w / L)
    if sc <= 0:
        return []
    top_len = (box.h if swap else box.w) / sc
    top_wid = (box.w if swap else box.h) / sc
    feats = []
    for f in spec.features:
        if f.type not in ("hole", "pocket", "slot", "step", "boss", "sphere"):
            continue
        feats.append({"type": f.type, "x": round(float(f.x), 3), "y": round(float(f.y), 3),
                      "diameter": round(float(f.diameter or 0.0), 3)})
    views = [{"name": "top",
              "outline": {"length": round(top_len, 2), "width": round(top_wid, 2)},
              "features": feats,
              "evidence": "本地像素外框实测（比例尺取自俯视图长边）"}]
    main_pick = _pick(L, T, exclude=box)
    if main_pick is not None:
        mb = main_pick[0]
        views.append({"name": "main",
                      "outline": {"length": round(mb.w / sc, 2),
                                  "height": round(mb.h / sc, 2)},
                      "features": [], "evidence": "本地像素外框实测"})
    side_pick = _pick(W, T, exclude=box)
    if side_pick is not None and (main_pick is None or side_pick[0] is not main_pick[0]):
        sb = side_pick[0]
        views.append({"name": "side",
                      "outline": {"width": round(sb.w / sc, 2),
                                  "height": round(sb.h / sc, 2)},
                      "features": [], "evidence": "本地像素外框实测"})
    return views


def _best_dia(ds: DimSet, measured: float, page: int) -> Tuple[Optional[float], float]:
    best = None
    for d in ds.diameters:
        if d.page != page and d.page >= 0:
            continue
        if d.value <= 0:
            continue
        rel = abs(d.value - measured) / max(measured, 1e-6)
        if rel <= 0.16 and (best is None or rel < best[0]):
            best = (rel, d.value)
    if best:
        return best[1], best[0]
    return None, 9.9


def _nearest_depth(ds: DimSet, x: float, y: float, page: int) -> Optional[float]:
    cands = []
    for d in ds.depths:
        if d.page != page:
            continue
        dist = math.hypot(d.x - x, d.y - y)
        cands.append((dist, d.value))
    if not cands:
        return None
    cands.sort()
    if len(cands) >= 2 and cands[1][0] < cands[0][0] * 1.35:
        return None          # 两个深度差不多近，不能确定归属
    return cands[0][1] if cands[0][0] <= 600 else None


def _depth_of_dia(ds: DimSet, dia: float, page: int) -> Tuple[Optional[float], bool]:
    # 同一值可能出现在多个标注里，取最近的深度
    best = None
    through = False
    for d in ds.diameters:
        if abs(d.value - dia) > max(0.25, 0.12 * max(dia, 1.0)):
            continue
        if d.through:
            through = True
        if d.label.startswith("深"):
            try:
                v = float(d.label[1:])
            except Exception:
                continue
            dist = math.hypot(d.x, d.y)
            if best is None or dist < best[0]:
                best = (dist, v)
    return (best[1] if best else None), through


# ---------------------------------------------------------------- main
def local_check(files: Sequence[str], hint: str = "", log=None,
                require_three_views: bool = True) -> LocalCheck:
    """识图 + 检验，完全本地。不修改任何文件。"""
    log = log or (lambda *_a, **_k: None)
    out = LocalCheck()
    ok_engine, msg = engine_status()
    if not ok_engine:
        out.issues.append(LocalIssue("error", "LOCAL_OCR_MISSING",
                                     f"本地 OCR 引擎不可用：{msg}",
                                     "重新安装工程助手完整包"))
        out.report_md = "## 本地识图\n\n❌ 本地 OCR 引擎不可用：" + msg
        return out

    images = [str(f) for f in files if Path(f).suffix.lower() in
              {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}]
    if not images:
        out.issues.append(LocalIssue("error", "LOCAL_NO_IMAGE",
                                     "本地识图只接受图片；DXF/DWG 请用“识别 CAD”通道",
                                     "导入 png/jpg/bmp 图纸，或改用 CAD 文件"))
        out.report_md = "## 本地识图\n\n❌ 没有可识别的图片。"
        return out

    # 1) OCR
    out.ocr = run_ocr(images, progress=log, multi_rotate=True)
    out.dims = parse_dimensions(out.ocr)
    if not out.ocr.ok:
        out.issues.append(LocalIssue("error", "LOCAL_OCR_EMPTY",
                                     "图上没有读到任何文字，无法识图",
                                     "换清晰度更高的图纸，或确认图片没有反相/过曝"))
    _all_text = "\n".join(t.text for t in out.ocr.tokens)
    # 多零件判定：只认标题栏/明细栏的实际结构，不能只数“比例”“序号”，
    # 否则题目说明页（也有“比例/序号”字样）会被误判成一页多图。
    def _tb_ids(key):
        vals = []
        for _t in out.ocr.tokens:
            if key in _t.text:
                _m = re.search(key + r"\s*(\d+)", _t.norm)
                vals.append(_m.group(1) if _m else (_t.norm or ""))
        seen = []
        for _v in vals:
            if _v not in seen:
                seen.append(_v)
        return seen
    _jh_ids = _tb_ids("件号")
    _th_ids = _tb_ids("图号")
    _n_xh = _all_text.count("序号")
    _n_bl = _all_text.count("比例")
    _n_bom = sum(_all_text.count(k) for k in ("明细", "代号", "借用件"))
    if len(_jh_ids) >= 2 or len(_th_ids) >= 2 or (_n_xh >= 2 and _n_bl >= 2 and _n_bom >= 1):
        _ids = "、".join((_jh_ids or _th_ids)[:4])
        out.issues.append(LocalIssue(
            "error", "LOCAL_MULTI_PART",
            f"图中出现 {max(len(_jh_ids), len(_th_ids))} 个零件标题栏"
            + (f"（件号 {_ids}）" if _ids else "")
            + "，这是一页多图；本地识图无法判断要建哪一个零件",
            "把目标零件单独裁成一张图再导入，或改用该零件的 DXF/DWG"))
    # 不是零件图：题目要求/说明页会有大量长句文字，且没有零件标题栏。
    _prose = [t.text for t in out.ocr.tokens if len(t.text) >= 26]
    _has_title = any(k in _all_text for k in ("件号", "图号", "零件名称"))
    if len(_prose) >= 6 and not _has_title:
        out.issues.append(LocalIssue(
            "error", "LOCAL_NOT_PART",
            "这一页没有零件标题栏，文字多为题目要求/说明，不是零件图，不能用来建模",
            "导入包含该零件三视图的那一页，或该零件的 DXF/DWG"))
    if len(out.dims.lengths) + len(out.dims.diameters) < 3:
        out.issues.append(LocalIssue("error", "LOCAL_DIM_FEW",
                                     "图上读到的尺寸标注少于 3 个，不能还原几何",
                                     "补齐总体尺寸与特征尺寸"))

    # 2) 视图分割
    boxes = find_view_boxes(images, log=log)
    out.view_count = len(boxes)
    _few_views = len(boxes) < 2
    if len(boxes) > 6:
        boxes = boxes[:6]
        out.issues.append(LocalIssue("warn", "LOCAL_VIEW_MANY",
                                     "候选视图框较多，已取面积最大的 6 个；请人工核对视图选择"))

    # 3) 视图配对 + 尺寸反解
    triples = _triple_candidates(boxes) if len(boxes) >= 3 else []
    dims = None
    chosen = None
    matching = None
    err = 9.9
    main_i = top_i = side_i = None
    if triples:
        best = None
        for cand in triples[:6]:
            mt = cand[2]
            d, e, w = _solve_dims(mt, out.dims, 3)
            if d is None:
                continue
            axes = _assign_axes(boxes, cand[1], mt, d, out.dims)
            if axes is None:
                continue
            dd, mi, ti, si = axes
            score = e + cand[0] * 2.0
            if best is None or score < best[0]:
                best = (score, dd, e, w, mt, cand[1], mi, ti, si)
        if best:
            _s, dims, err, warns, matching, chosen, main_i, top_i, side_i = best
            out.issues.extend(LocalIssue("warn", "LOCAL_DIM_DERIVED", w) for w in warns)
            out.matching_error = float(err)
    rnd = _round_simple(out.dims, boxes, out.ocr) if boxes else None
    prefer_round = False
    if rnd is not None:
        if dims is None:
            prefer_round = True
        else:
            try:
                _mx = max(float(dims.get(k, 0) or 0) for k in ("L", "W", "T"))
                _dmax = max((float(d.value) for d in out.dims.diameters), default=0.0)
                # 三视图反解出的总体尺寸比图上最大直径还小很多 -> 明显配错视图
                if _dmax >= 2.6 * max(_mx, 1e-9):
                    prefer_round = True
            except Exception:
                pass
    if prefer_round and rnd is not None:
        dims = dict(rnd.dims)
        out.shape = "round"
        out.round_inner = [float(v) for v in rnd.bores]
        out.round_holes = list(rnd.holes)
        out.round_chamfer = float(rnd.chamfer)
        out.matching_error = 0.0
        out.issues.append(LocalIssue("warn", "LOCAL_ROUND_DERIVED", rnd.note))
    if _few_views and not (prefer_round and rnd is not None):
        out.issues.append(LocalIssue("error", "LOCAL_VIEW_FEW",
                                     f"只找到 {len(boxes)} 个视图外框；矩形零件至少需要主视图+俯视图",
                                     "检查图纸是否缺视图，或提高图片分辨率"))
    elif _few_views:
        out.issues.append(LocalIssue(
            "warn", "LOCAL_VIEW_SINGLE_ROUND",
            f"只有 {len(boxes)} 个视图，但该件是简单回转体（外径、轴向长度、内径齐全），"
            "单剖视图足以表达；已按单视图建模",
            "若还有槽、球坑、偏心孔等单视图表达不了的特征，请改用 DXF 或补视图"))
    if dims is None:
        out.issues.append(LocalIssue("error", "LOCAL_DIM_UNSOLVED",
                                     "三视图像素比例与 OCR 数字无法闭合，不能确定长/宽/厚",
                                     "核对图纸比例、单位与标注；或手工填写总体尺寸"))
        out.report_md = _report(out, None)
        _finalize_check(out)
        return out

    L, W, T = float(dims["L"]), float(dims["W"]), float(dims["T"])
    if min(L, W, T) <= 0:
        out.issues.append(LocalIssue("error", "LOCAL_DIM_NONPOS",
                                     "反解出的总体尺寸存在非正值", "核对图纸标注"))
        _finalize_check(out)
        out.report_md = _report(out, None)
        return out

    # 3b) 建初始 spec（几何来源：本地像素 + OCR）
    spec = PartSpec(
        part_name=(Path(images[0]).stem if images else "未命名零件"),
        part_type="plate",
        source="local-vision",
        origin="vision",
        length=round(L, 3),
        width=round(W, 3),
        thickness=round(T, 3),
        base_kind=("round" if out.shape == "round" else "rect"),
        diameter=(round(max(L, W), 3) if out.shape == "round" else 0.0),
        chamfer=(round(out.round_chamfer, 3) if out.shape == "round" else 0.0),
    )

    # 4) 特征
    if out.shape == "round":
        for dv in out.round_inner:
            spec.features.append(Feature(
                type="hole", x=0.0, y=0.0, diameter=round(float(dv), 3),
                depth=0.0, through=True, pos_source="local-ocr",
                evidence=f"单视图 OCR 内径 Ø{dv:g}"))
            out.issues.append(LocalIssue(
                "warn", "LOCAL_ROUND_BORE",
                f"内径 Ø{dv:g} 按通孔处理（单视图无法区分盲孔/通孔）",
                "确认是通孔后可直接建模；若是盲孔请在参数表补深度"))
        for (hx, hy, hd) in out.round_holes:
            spec.features.append(Feature(
                type="hole", x=round(float(hx), 3), y=round(float(hy), 3),
                diameter=round(float(hd), 3), depth=0.0, through=True,
                pos_source="local-ocr", evidence=f"OCR 节圆均布 Ø{hd:g}"))
        out.views = []
        top_box = None
        front_box = None
    else:
        top_box = boxes[top_i] if top_i is not None and 0 <= top_i < len(boxes) else None
        front_box = boxes[main_i] if main_i is not None and 0 <= main_i < len(boxes) else None
        if top_box is None and boxes:
            # 兜底：长宽比最接近 L/W 的框
            want = L / max(W, 1e-9)
            top_box = min(boxes, key=lambda b: min(
                abs(math.log(max(b.w / float(b.h), 1e-9) / want)),
                abs(math.log(max(b.h / float(b.w), 1e-9) / want))))
        if top_box is not None:
            sc = 0.5 * (top_box.w / max(L, 1e-9) + top_box.h / max(W, 1e-9))
            feats = _measure_view_features(top_box, L, W, sc)
            flip_y = False
            if front_box is not None:
                flip_y = top_box.bbox[1] > front_box.bbox[1]   # 第一角投影：俯视图在主视图下方
            _build_features(spec, feats, out.dims, top_box, sc, flip_y, out.issues, log)
            out.views = _make_views(spec, boxes, chosen or [], top_box, front_box, sc, L, W, T)
        else:
            out.issues.append(LocalIssue("warn", "LOCAL_TOP_VIEW",
                                         "没有定位到俯视图，未提取孔/槽特征"))

    # 5) 本地审计（纯规则）
    spec.confidence = 0.72 if not any(i.severity == "error" for i in out.issues) else 0.35
    try:
        from .drawing_audit import audit_spec
        rep = audit_spec(
            spec, views=out.views, require_views=bool(
                require_three_views and out.view_count >= 2
                and not (out.shape == "round" and not out.views)),
            uncertain=[i.message for i in out.issues if i.severity == "warn"],
            blocking=[i.message for i in out.issues if i.severity == "error"],
            drawing_type="part", notes=out.notes or "本地 OCR 识图")
        out.audit = rep.to_dict()
        if not bool(out.audit.get("can_build", True)):
            spec.confidence = min(spec.confidence, 0.3)
    except Exception as e:
        out.issues.append(LocalIssue("warn", "LOCAL_AUDIT_FAIL", f"本地审计跳过：{e}"))
        out.audit = {}
    out.spec = spec
    out.notes = _notes(out)
    _finalize_check(out)
    out.report_md = _report(out, spec)
    log("📋 " + str((out.audit or {}).get("summary") or ""))
    return out


def _build_features(spec: PartSpec, feats: List[Any], ds: DimSet, top_box: ViewBox,
                    scale: float, flip_y: bool, issues: List[LocalIssue], log) -> None:
    key = normalize_text(Path(top_box.path).stem)
    # 孔
    measured_by_dia: Dict[float, int] = {}
    for f in feats:
        if f.kind != "circle":
            continue
        nominal, rel = _best_dia(ds, f.dia_mm, top_box.page)
        dia = nominal if nominal is not None else f.dia_mm
        if nominal is None:
            issues.append(LocalIssue("warn", "LOCAL_DIA_UNLABELED",
                                     f"视图里量到 Ø{f.dia_mm:.1f}，但 OCR 没有对应孔径标注；"
                                     f"按实测值使用，请人工核对"))
        depth, through = _depth_of_dia(ds, dia, top_box.page)
        if depth is None and not through:
            through = True
            issues.append(LocalIssue(
                "error", "LOCAL_HOLE_DEPTH",
                f"孔 Ø{dia:g} 没有标注深度，也没有写“通孔”；已暂按通孔显示，禁止直接建模",
                "在参数表里填写深度后重新检查；若确为通孔，请在图纸上确认"))
        y = f.cy if flip_y else -f.cy
        spec.features.append(Feature(
            type="hole", x=round(f.cx, 3), y=round(y, 3),
            diameter=round(float(dia), 3),
            depth=round(float(depth or 0.0), 3),
            through=bool(through or depth is None),
            pos_source="local-pixel", evidence=f"俯视图轮廓 {f.size_text()}"))
        measured_by_dia[float(dia)] = measured_by_dia.get(float(dia), 0) + 1
    # OCR 的 n×Ø 与实测圆数量对账
    for d in ds.diameters:
        if d.count > 1:
            got = measured_by_dia.get(float(d.value), 0)
            if got < d.count:
                sev = "error" if got == 0 else "warn"
                issues.append(LocalIssue(sev, "LOCAL_HOLE_COUNT",
                                         f"图纸标注 {d.count}×Ø{d.value:g}，俯视图只量到 {got} 个",
                                         "确认孔是否画在其它视图，或孔位是否被线条遮挡"))
    # 型腔 / 槽
    for f in feats:
        if f.kind == "circle":
            continue
        depth = _nearest_depth(ds, (top_box.bbox[0] + top_box.bbox[2]) * 0.5 + f.cx * scale,
                               (top_box.bbox[1] + top_box.bbox[3]) * 0.5 - f.cy * scale,
                               top_box.page)
        if depth is None:
            issues.append(LocalIssue(
                "error", "LOCAL_POCKET_DEPTH",
                f"视图里量到 {f.size_text()} 的槽/型腔，但找不到对应深度标注",
                "在参数表里补充深度后重新检查"))
            continue
        typ = "slot" if f.kind == "slot" else "pocket"
        y = f.cy if flip_y else -f.cy
        spec.features.append(Feature(
            type=typ, x=round(f.cx, 3), y=round(y, 3),
            length=round(f.w_mm, 3), width=round(f.h_mm, 3),
            depth=round(float(depth), 3), through=False,
            corner_radius=round(min(f.w_mm, f.h_mm) * 0.5, 3) if f.kind == "slot" else 0.0,
            pos_source="local-pixel", evidence=f"俯视图轮廓 {f.size_text()}"))


def _make_views(spec: PartSpec, boxes: Sequence[ViewBox], chosen: Sequence[int],
                top: Optional[ViewBox], front: Optional[ViewBox], scale: float,
                L: float, W: float, T: float) -> List[dict]:
    out: List[dict] = []
    if front is not None:
        out.append({"name": "main",
                    "outline": {"length": round(front.w / scale, 2),
                                "height": round(front.h / scale, 2)},
                    "features": [], "evidence": "本地像素实测"})
    if top is not None:
        fs = [{"type": f.type, "x": f.x, "y": f.y,
               "diameter": f.diameter if f.type == "hole" else 0.0}
              for f in spec.features]
        out.append({"name": "top",
                    "outline": {"length": round(top.w / scale, 2),
                                "width": round(top.h / scale, 2)},
                    "features": fs, "evidence": "本地像素实测"})
    # 侧视图
    side = None
    for i in chosen:
        b = boxes[i]
        if b is top or b is front:
            continue
        side = b
        break
    if side is not None:
        out.append({"name": "side",
                    "outline": {"width": round(side.w / scale, 2),
                                "height": round(side.h / scale, 2)},
                    "features": [], "evidence": "本地像素实测"})
    return out


def _notes(c: LocalCheck) -> str:
    bits = ["本地离线识图（RapidOCR + 视图比例 + 轮廓实测）"]
    if c.spec:
        bits.append(f"总体 {c.spec.length:g}×{c.spec.width:g}×{c.spec.thickness:g}")
    bits.append(f"{len(c.ocr.tokens)} 个文本块")
    if c.ocr.pages:
        bits.append(f"{len(c.ocr.pages)} 页")
    return "；".join(bits)


def _finalize_check(c: LocalCheck) -> None:
    if c.spec is not None:
        c.spec.notes = _notes(c)
        c.spec.confidence = round(float(c.spec.confidence or 0.0), 3)
    audit_ok = bool(c.audit.get("can_build", True)) if c.audit else True
    has_err = any(i.severity == "error" for i in c.issues)
    c.can_build = bool(c.spec and not has_err and audit_ok and c.spec.confidence >= 0.45)
    if c.can_build and c.spec is not None:
        c.spec.confidence = max(c.spec.confidence, 0.62)


def _report(c: LocalCheck, spec: Optional[PartSpec]) -> str:
    L = ["## 本地识图 / 图纸检验（离线，无云端 AI）", ""]
    L.append(f"- OCR：{len(c.ocr.tokens)} 个文本块 · {len(c.ocr.pages)} 页")
    L.append(f"- 视图：找到 {c.view_count} 个候选视图框")
    if spec is not None:
        L.append(f"- 总体尺寸：{spec.length:g} × {spec.width:g} × {spec.thickness:g} mm"
                 + ("（三视图比例一致性 "
                    f"{c.matching_error * 100:.2f}%）" if c.matching_error < 1 else ""))
        L.append(f"- 特征：{len(spec.features)} 个")
        for f in spec.features:
            if f.type == "hole":
                ds = "通孔" if f.is_through() else f"深{f.depth:g}"
                L.append(f"  - 孔 Ø{f.diameter:g} @({f.x:+.2f}, {f.y:+.2f}) {ds}")
            else:
                L.append(f"  - {f.type} {f.length:g}×{f.width:g} 深{f.depth:g} "
                         f"@({f.x:+.2f}, {f.y:+.2f})")
    L.append("")
    L.append(f"**结论：{'可以建模' if c.can_build else '不能直接建模'}**")
    L.append("")
    if c.issues:
        L.append("### 检查项")
        for i in c.issues:
            mark = {"error": "⛔", "warn": "⚠", "info": "ℹ"}.get(i.severity, "·")
            L.append(f"- {mark} {i.message}")
            if i.suggestion:
                L.append(f"  - 处理：{i.suggestion}")
        L.append("")
    if c.ocr.warnings:
        L.append("### OCR 提示")
        for w in c.ocr.warnings:
            L.append(f"- {w}")
        L.append("")
    if c.dims.summary():
        L.append("### 读到的尺寸")
        L.append("`" + c.dims.summary().replace("Ø", "D") + "`")
        L.append("")
    L.append("### OCR 原文")
    L.append("```")
    L.append(c.ocr.text[:6000])
    L.append("```")
    return "\n".join(L)
