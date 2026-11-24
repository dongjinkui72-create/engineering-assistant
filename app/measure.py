"""图纸像素实测 —— 把"图上没标注的位置"量出来。

为什么需要这个模块（来自 CODEX_NOTES.md 的实测结论）：

* `sample_plate.png` 上，型腔 80×50 与槽 30×10 的位置**完全没有标注**，只标了尺寸。
  识图模型对没标注的位置一律按"居中"填，于是给出型腔 y=0、槽 y=+25；
  图纸实际是 y=-10、y=+32（型腔偏下、槽偏上）。
* 体积校验抓不到这类错：型腔和槽整体平移，体积只差 0.6%
  （识图 spec 160,832.4 vs 实测 spec 159,856.1）。
  所以"体积 PASS"完全可能是几何错的。

这里的做法：
  1. 先在视图里找零件外轮廓，用**图上标注了的外形尺寸**（长×宽 / 直径）标定 px/mm；
  2. 再找闭合轮廓（孔 / 槽 / 型腔），量出中心与尺寸；
  3. 把像素坐标换算成零件坐标（原点在零件中心，y 向上为正）。

精度（sample_plate.png 1800×1200，实测）：5.000 px/mm，±1px ≈ ±0.2mm。

本模块只依赖 numpy + PIL（与 `vision.py` 里已有的数孔逻辑同源），不引入 OpenCV。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

_MAX_DIM = 1400          # 超过就缩放：连通域遍历是按像素走的，原图 1800×1200 太慢
_DARK = 120              # 灰度阈值：与 vision._image_hole_count 保持一致


@dataclass
class Calibration:
    """一张视图的标定结果。"""
    px_per_mm: float
    bbox: Tuple[int, int, int, int]        # 外轮廓 (x1, y1, x2, y2)，缩放后像素
    center: Tuple[float, float]            # 外轮廓中心（像素）
    image_size: Tuple[int, int]            # 缩放后的 (w, h)
    method: str = "outline"                # outline | round


@dataclass
class PixelFeature:
    """图上一个闭合轮廓的像素实测结果（尺寸已换算成 mm）。"""
    kind: str                    # circle | slot | rect
    cx: float                    # 零件坐标（mm，原点在零件中心）
    cy: float
    w_mm: float                  # 外接矩形宽（mm）
    h_mm: float                  # 外接矩形高（mm）
    dia_mm: float = 0.0          # circle 用
    px_bbox: Tuple[int, int, int, int] = (0, 0, 0, 0)
    squared: bool = True         # 四角是否为直角（区分型腔 / 腰形槽）

    def size_text(self) -> str:
        if self.kind == "circle":
            return f"Ø{self.dia_mm:.1f}"
        return f"{self.w_mm:.1f}×{self.h_mm:.1f}"

    def text(self) -> str:
        return f"{self.kind} {self.size_text()} @({self.cx:+.1f},{self.cy:+.1f})"


_CACHE: Dict[Tuple, Tuple[Optional[Calibration], List[PixelFeature]]] = {}
_CACHE_MAX = 8


# ---------------------------------------------------------------- 图像 → 轮廓

def _load_gray(path: str) -> Optional[Tuple["object", float]]:
    from PIL import Image
    import numpy as np
    try:
        im = Image.open(path).convert("L")
    except Exception:
        return None
    w0, h0 = im.size
    k = 1.0
    if max(w0, h0) > _MAX_DIM:
        k = _MAX_DIM / float(max(w0, h0))
        im = im.resize((max(1, int(round(w0 * k))), max(1, int(round(h0 * k))),
                        ), Image.LANCZOS)
    return np.asarray(im), k


def _components(dark) -> List[List[Tuple[int, int]]]:
    """8 邻域连通域。只遍历暗像素；1400px 图上是 10 万级，可接受。"""
    import numpy as np
    h, w = dark.shape
    visited = np.zeros((h, w), dtype=bool)
    comps: List[List[Tuple[int, int]]] = []
    for y0, x0 in np.argwhere(dark):
        y0, x0 = int(y0), int(x0)
        if visited[y0, x0]:
            continue
        stack = [(y0, x0)]
        visited[y0, x0] = True
        pts: List[Tuple[int, int]] = []
        while stack:
            y, x = stack.pop()
            pts.append((y, x))
            y1, y2 = max(0, y - 1), min(h, y + 2)
            x1, x2 = max(0, x - 1), min(w, x + 2)
            sub = dark[y1:y2, x1:x2] & (~visited[y1:y2, x1:x2])
            for yy, xx in np.argwhere(sub):
                ny, nx = y1 + int(yy), x1 + int(xx)
                visited[ny, nx] = True
                stack.append((ny, nx))
        comps.append(pts)
    return comps


def _bbox(pts: Sequence[Tuple[int, int]]) -> Tuple[int, int, int, int]:
    ys = [p[0] for p in pts]
    xs = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _dark_frac(a, x1, y1, x2, y2, dark_val=_DARK) -> float:
    sub = a[max(0, y1):max(0, y2), max(0, x1):max(0, x2)]
    if sub.size == 0:
        return 0.0
    return float((sub < dark_val).mean())


# ---------------------------------------------------------------- 标定

def _find_outline(comps, w: int, h: int, spec) -> Optional[Tuple[float, Tuple[int, int, int, int]]]:
    """在连通域里找零件外轮廓，并算出 px/mm。

    判据与 `vision._image_hole_count` 一致：面积占比 5%~62%（排除整页图框）、
    长宽比靠近 spec 的 L/W；同样接近时取面积大的（主视图比标题栏/局部视图大）。
    """
    img_area = float(w * h)
    if getattr(spec, "base_kind", "") == "round" and float(getattr(spec, "diameter", 0) or 0) > 0:
        want_ar = 1.0
    elif float(getattr(spec, "length", 0) or 0) > 0 and float(getattr(spec, "width", 0) or 0) > 0:
        want_ar = float(spec.length) / float(spec.width)
    else:
        return None
    best = None                                   # (delta, -area_ratio, scale, bbox)
    for pts in comps:
        if len(pts) < 80:
            continue
        x1, y1, x2, y2 = _bbox(pts)
        bw, bh = x2 - x1 + 1, y2 - y1 + 1
        area_ratio = (bw * bh) / img_area
        if area_ratio < 0.05 or area_ratio > 0.62:
            continue
        ar = bw / float(bh)
        delta = abs(math.log(max(ar, 1e-6) / max(want_ar, 1e-6)))
        if delta > 0.45:
            continue
        if getattr(spec, "base_kind", "") == "round":
            sc = bw / float(spec.diameter)
        else:
            sc = 0.5 * (bw / float(spec.length) + bh / float(spec.width))
        if sc <= 0:
            continue
        key = (round(delta, 3), -area_ratio)
        if best is None or key < best[0]:
            best = (key, sc, (x1, y1, x2, y2))
    if best is None:
        return None
    return best[1], best[2]


def calibrate(image_path: str, spec) -> Optional[Calibration]:
    """只用外轮廓标定（找外轮廓 → 用标注的外形尺寸算 px/mm）。"""
    gray = _load_gray(image_path)
    if gray is None:
        return None
    a, _k = gray
    h, w = a.shape
    if h < 40 or w < 40:
        return None
    dark = a < _DARK
    if float(dark.mean()) > 0.5:        # 黑底白线图纸先反相
        dark = ~dark
    comps = _components(dark)
    found = _find_outline(comps, w, h, spec)
    if not found:
        return None
    sc, (x1, y1, x2, y2) = found
    return Calibration(px_per_mm=float(sc), bbox=(x1, y1, x2, y2),
                       center=((x1 + x2) * 0.5, (y1 + y2) * 0.5),
                       image_size=(w, h))


# ---------------------------------------------------------------- 轮廓 → 特征

def _ring_radius(pts, cx: float, cy: float, wx: int, wy: int) -> Tuple[float, float]:
    """圆环半径与角向覆盖：返回 (半径, 覆盖率)。

    孔上常画十字中心线，bbox 中心会被拉长，但十字是对称的，
    所以用 bbox 中心当圆心仍然可靠；半径靠环向直方图的峰值找。
    """
    import numpy as np
    arr = np.asarray(pts, dtype=float)
    rad = np.hypot(arr[:, 1] - cx, arr[:, 0] - cy)
    r_max = 0.5 * math.hypot(wx, wy) + 3.0
    if r_max < 4:
        return 0.0, 0.0
    hist, edges = np.histogram(rad, bins=np.arange(2.0, r_max + 1.0, 1.0))
    if hist.size == 0:
        return 0.0, 0.0
    bi = int(np.argmax(hist))
    r = float(edges[bi] + 0.5)
    band = (rad >= r - 1.5) & (rad <= r + 1.5)
    if int(band.sum()) < 8:
        return r, 0.0
    ang = np.degrees(np.arctan2(arr[band, 0] - cy, arr[band, 1] - cx)) % 360.0
    cover = len(np.unique((ang / 10.0).astype(int))) / 36.0
    return r, float(cover)


def _squared_corners(a, x1: int, y1: int, x2: int, y2: int) -> bool:
    """四角是不是直角：直角矩形的角上有线，腰形槽/圆角的角上是白的。

    补丁取 bbox 短边的 8%，最多 6px，避免把长边也吃进来。
    """
    pad = max(3, min(6, int(0.08 * min(x2 - x1, y2 - y1))))
    fracs = [
        _dark_frac(a, x1, y1, x1 + pad, y1 + pad),
        _dark_frac(a, x2 - pad, y1, x2, y1 + pad),
        _dark_frac(a, x1, y2 - pad, x1 + pad, y2),
        _dark_frac(a, x2 - pad, y2 - pad, x2, y2),
    ]
    return sum(1 for f in fracs if f > 0.04) >= 3


def _iou(b1: Tuple[int, int, int, int], b2: Tuple[int, int, int, int]) -> float:
    ax1, ay1, ax2, ay2 = b1
    bx1, by1, bx2, by2 = b2
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    a1 = (ax2 - ax1) * (ay2 - ay1)
    a2 = (bx2 - bx1) * (by2 - by1)
    return inter / float(a1 + a2 - inter) if (a1 + a2 - inter) > 0 else 0.0


def measure_image(image_path: str, spec) -> Tuple[Optional[Calibration], List[PixelFeature]]:
    """量一张视图：返回 (标定, 轮廓列表)。同一张图按 (路径, mtime, 外形) 缓存。"""
    try:
        mt = Path(image_path).stat().st_mtime
    except Exception:
        mt = 0.0
    key = (str(Path(image_path).resolve()), round(mt, 3),
           round(float(getattr(spec, "length", 0) or 0), 2),
           round(float(getattr(spec, "width", 0) or 0), 2),
           round(float(getattr(spec, "diameter", 0) or 0), 2),
           str(getattr(spec, "base_kind", "")))
    if key in _CACHE:
        return _CACHE[key]

    gray = _load_gray(image_path)
    if gray is None:
        _CACHE[key] = (None, [])
        return _CACHE[key]
    a, _k = gray
    h, w = a.shape
    dark = a < _DARK
    if float(dark.mean()) > 0.5:
        dark = ~dark
    comps = _components(dark)
    found = _find_outline(comps, w, h, spec)
    if not found:
        _CACHE[key] = (None, [])
        return _CACHE[key]
    sc, (bx1, by1, bx2, by2) = found
    cal = Calibration(px_per_mm=float(sc), bbox=(bx1, by1, bx2, by2),
                      center=((bx1 + bx2) * 0.5, (by1 + by2) * 0.5),
                      image_size=(w, h),
                      method=("round" if getattr(spec, "base_kind", "") == "round" else "outline"))
    cx_px, cy_px = cal.center

    # 只有在视图内部、且尺寸像特征（1mm~45% 外形）的闭合轮廓才算候选。
    # 边界留 2% 余量：外轮廓本身是双线时会有一圈紧贴的线，别把它当成特征。
    mx = max(2.0, 0.02 * (bx2 - bx1))
    my = max(2.0, 0.02 * (by2 - by1))
    part_w = (bx2 - bx1) / sc if sc > 0 else 0.0
    part_h = (by2 - by1) / sc if sc > 0 else 0.0
    min_side_mm = 1.0
    # 上限放到 95% 外形：型腔常常占了板宽的一大半（实测 80×50 在 160×100 的板上，
    # 长边 80 已是板宽的 80%）。外轮廓本身因"视图内部余量"已被排除，不会误收。
    max_side_mm = 0.95 * min(part_w, part_h) if min(part_w, part_h) > 0 else 1e9

    items: List[PixelFeature] = []
    for pts in comps:
        if len(pts) < 20:
            continue
        x1, y1, x2, y2 = _bbox(pts)
        bw, bh = x2 - x1 + 1, y2 - y1 + 1
        if bw < 7 or bh < 7:
            continue
        if bw / float(bh) > 12 or bh / float(bw) > 12:      # 尺寸线、中心线、剖面线
            continue
        if x1 < bx1 + mx or y1 < by1 + my or x2 > bx2 - mx or y2 > by2 - my:
            continue                                        # 不在视图内部
        w_mm, h_mm = bw / sc, bh / sc
        if min(w_mm, h_mm) < min_side_mm or max(w_mm, h_mm) > max_side_mm:
            continue
        aspect = max(w_mm, h_mm) / max(min(w_mm, h_mm), 1e-6)
        ccx, ccy = (x1 + x2) * 0.5, (y1 + y2) * 0.5
        r, cover = _ring_radius(pts, ccx, ccy, bw, bh)
        dia_mm = 2.0 * r / sc if r > 0 else 0.0
        squared = _squared_corners(a, x1, y1, x2, y2)
        if (aspect <= 1.3 and cover >= 0.5 and dia_mm > 0
                and 0.75 * min(w_mm, h_mm) <= dia_mm <= 1.25 * max(w_mm, h_mm)):
            kind = "circle"
        elif aspect >= 1.3 and not squared:
            kind = "slot"
        else:
            kind = "rect"
        items.append(PixelFeature(
            kind=kind,
            cx=(ccx - cx_px) / sc,
            cy=-(ccy - cy_px) / sc,          # 图像 y 向下，零件 y 向上
            w_mm=w_mm, h_mm=h_mm,
            dia_mm=dia_mm if kind == "circle" else 0.0,
            px_bbox=(x1, y1, x2, y2), squared=squared))

    # 双层轮廓（型腔常画内外两条线）：IoU 高的留外层大的那个。
    kept: List[PixelFeature] = []
    for it in sorted(items, key=lambda f: -(f.w_mm * f.h_mm)):
        if any(_iou(it.px_bbox, k.px_bbox) > 0.60 for k in kept):
            continue
        kept.append(it)
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[key] = (cal, kept)
    return _CACHE[key]


# ---------------------------------------------------------------- 与 spec 对上

_FEATURE_KIND = {"hole": "circle", "pocket": "rect", "slot": "slot", "step": "rect", "boss": "rect"}


def _size_of(f) -> Tuple[float, float]:
    if f.type == "hole":
        d = float(f.diameter or 0)
        return d, d
    return float(f.length or 0), float(f.width or 0)


def _err_of(f, it: PixelFeature) -> Optional[float]:
    """特征与轮廓的尺寸相对误差；类型不符或差太多返回 None。"""
    kind = _FEATURE_KIND.get(f.type)
    if not kind or it.kind != kind:
        return None
    L, W = _size_of(f)
    short, long_ = min(L, W), max(L, W)
    if short <= 0:
        return None
    if kind == "circle":
        err = abs(it.dia_mm - short) / short
        # 孔径容差比外形宽：孔上的十字中心线会把 bbox 撑大（实测 Ø9 量成 8.5）
        return err if err <= 0.20 else None
    a, b = sorted((it.w_mm, it.h_mm))
    err = abs(a - short) / short + abs(b - long_) / max(long_, 1e-6)
    return err if err <= 0.30 else None


def assign(features, items: Sequence[PixelFeature]) -> Dict[int, PixelFeature]:
    """特征 → 轮廓 的**一一对应**（匈牙利太贵，用全局贪心）。

    为什么必须一一对应：法兰盘上 6 个 Ø10 孔尺寸完全一样，按"每个特征各自找
    最像的轮廓"会让 6 个特征全对上同一个孔，然后 5 个被误判成"位置不符"。
    尺寸一样时的排序用"离得近的先配"（只是同一尺寸候选之间的次序，
    不会把错的坐标洗成对的——真错了距离仍然很大）。
    """
    pairs = []
    for i, f in enumerate(features):
        for j, it in enumerate(items):
            e = _err_of(f, it)
            if e is not None:
                d = math.hypot(it.cx - float(f.x), it.cy - float(f.y))
                pairs.append((round(e, 3), round(d, 2), i, j))
    pairs.sort(key=lambda t: (t[0], t[1], t[2], t[3]))
    got_f: Dict[int, PixelFeature] = {}
    got_i = set()
    for _e, _d, i, j in pairs:
        if i in got_f or j in got_i:
            continue
        got_f[i] = items[j]
        got_i.add(j)
    return got_f


def match(f, items: Sequence[PixelFeature]) -> Optional[PixelFeature]:
    """单个特征找图上对应的轮廓（**不用位置**，因为要检验的就是位置）。"""
    best, best_err = None, 1e9
    for it in items:
        e = _err_of(f, it)
        if e is not None and e < best_err:
            best, best_err = it, e
    return best


def describe(cal: Calibration) -> str:
    return (f"外形 {cal.bbox[2] - cal.bbox[0]}×{cal.bbox[3] - cal.bbox[1]} px"
            f" 标定 {cal.px_per_mm:.3f} px/mm（±{1.0 / cal.px_per_mm:.2f} mm/px）")


# ---------------------------------------------------------------- 就地复核

_MASK_CACHE: Dict[Tuple, Tuple[object, Optional[Calibration]]] = {}


def _mask(path: str, spec):
    """缓存暗像素掩码：一张图会被"找轮廓"和"就地复核"各扫一次，别重复读盘。"""
    try:
        mt = round(Path(path).stat().st_mtime, 3)
    except Exception:
        mt = 0.0
    key = (str(Path(path).resolve()), mt, round(float(getattr(spec, "length", 0) or 0), 2),
           round(float(getattr(spec, "width", 0) or 0), 2),
           round(float(getattr(spec, "diameter", 0) or 0), 2))
    if key in _MASK_CACHE:
        return _MASK_CACHE[key]
    gray = _load_gray(path)
    if gray is None:
        _MASK_CACHE[key] = (None, None)
        return _MASK_CACHE[key]
    a, _ = gray
    dark = a < _DARK
    if float(dark.mean()) > 0.5:
        dark = ~dark
    if len(_MASK_CACHE) >= 4:
        _MASK_CACHE.pop(next(iter(_MASK_CACHE)))
    _MASK_CACHE[key] = (dark, calibrate(path, spec))
    return _MASK_CACHE[key]


def _samples(f, sc: float) -> Optional[List[Tuple[float, float]]]:
    """按特征形状生成"应该有线"的采样点（零件坐标，mm）。"""
    if f.type == "hole" and float(f.diameter or 0) > 0:
        r = float(f.diameter) / 2.0
        n = 72
        return [(f.x + r * math.cos(2 * math.pi * k / n),
                 f.y + r * math.sin(2 * math.pi * k / n)) for k in range(n)]
    L, W = float(f.length or 0), float(f.width or 0)
    if L <= 0 or W <= 0:
        return None
    ang = math.radians(float(getattr(f, "angle", 0) or 0))
    ca, sa = math.cos(ang), math.sin(ang)

    def rot(dx: float, dy: float) -> Tuple[float, float]:
        return (f.x + dx * ca - dy * sa, f.y + dx * sa + dy * ca)

    if f.type == "slot" and L > W:
        pts: List[Tuple[float, float]] = []
        half = (L - W) / 2.0
        for k in range(17):                       # 两条直边
            dx = -half + 2 * half * k / 16.0
            pts += [rot(dx, W / 2.0), rot(dx, -W / 2.0)]
        for sign in (1.0, -1.0):                  # 两端半圆（th ∈ [-90°, +90°]）
            for k in range(13):
                th = -math.pi / 2.0 + math.pi * k / 12.0
                pts.append(rot(sign * (half + (W / 2.0) * math.cos(th)),
                               (W / 2.0) * math.sin(th)))
        return pts
    pts = []
    nx = max(8, int(round(L / 2.0))), max(8, int(round(W / 2.0)))
    for k in range(nx[0]):                        # 上下两条边
        dx = -L / 2.0 + L * k / (nx[0] - 1)
        pts += [rot(dx, W / 2.0), rot(dx, -W / 2.0)]
    for k in range(nx[1]):                        # 左右两条边
        dy = -W / 2.0 + W * k / (nx[1] - 1)
        pts += [rot(L / 2.0, dy), rot(-L / 2.0, dy)]
    return pts


def shape_coverage(path: str, spec, f, tol_mm: float = 0.7) -> Optional[Tuple[float, int]]:
    """**就地复核**：不去找轮廓，而是在"模型给的坐标"上查该处有没有对应的线。

    为什么需要它：有些孔的轮廓和引出线/图框粘成一个大连通域（实测法兰盘 6 个
    Ø10 孔有 1 个并进了整页图框），按"找轮廓再配对"就永远缺一个。这时改为
    在给定位置直接查"该处是不是真有一圈 Ø10 的线"：
      - 覆盖率 ≥55% → 位置是对的（出处记 pixel）
      - 覆盖率 ≤15% → 该处根本没有这个特征，模型是在用居中/对称糊弄（阻断）
    """
    mask, cal = _mask(path, spec)
    if mask is None or cal is None:
        return None
    pts = _samples(f, cal.px_per_mm)
    if not pts:
        return None
    tol = max(1, int(round(tol_mm * cal.px_per_mm)))
    h, w = mask.shape
    hits = 0
    for xm, ym in pts:
        px = int(round(cal.center[0] + xm * cal.px_per_mm))
        py = int(round(cal.center[1] - ym * cal.px_per_mm))
        y1, y2 = max(0, py - tol), min(h, py + tol + 1)
        x1, x2 = max(0, px - tol), min(w, px + tol + 1)
        if y2 > y1 and x2 > x1 and bool(mask[y1:y2, x1:x2].any()):
            hits += 1
    return (hits / float(len(pts)), len(pts))
