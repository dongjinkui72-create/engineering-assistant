"""特征之间的二维冲突检测。

图纸（尤其装配图/手绘试卷图）里常有几何上互相矛盾的标注：
  * 一个特征完全落在另一个更深的特征里  → 重复表达，切一次就够
  * 两个特征的轮廓只差零点几毫米        → 薄壁/相切，CAD 内核会造出零厚度面而报错

这里在建模之前把这两类问题找出来，给出可执行的处置：
  包含   → 跳过被包含的那个（体积也只扣一次）
  薄壁   → 把较小的孔外扩 gap+0.1 并入大轮廓（并在报告里标红让用户确认）

特征轮廓统一用多边形近似（圆 48 段、腰形槽按半圆采样），误差 < 0.05mm，
用于"是否包含""最小间隙"这类判断足够。
"""
from __future__ import annotations

import math
from typing import Dict, List, Tuple

Pt = Tuple[float, float]

THIN_WALL = 0.5      # 小于这个间隙就算薄壁（mm）


def label(f) -> str:
    if f.type == "profile_cut":
        _n = len(getattr(f, "profile", None) or [])
        return f"轮廓切除({_n}段)"
    if f.type == "hole":
        return f"孔Φ{f.diameter:g}@({f.x:g},{f.y:g})"
    if f.type == "slot":
        return f"槽{f.length:g}×{f.width:g}"
    if f.type == "pocket":
        return f"型腔{f.length:g}×{f.width:g}"
    return f"{f.type}{f.length:g}×{f.width:g}"


def outline(f, n: int = 180) -> List[Pt]:
    """特征轮廓的闭合多边形（绝对坐标）。"""
    if f.type == "profile_cut":
        try:
            from .geom import flatten, segs_from_profile
            _pts = flatten(segs_from_profile(getattr(f, "profile", None) or []), 2.0)
            if _pts and len(_pts[0]) >= 3:
                return [(float(x), float(y)) for x, y in _pts[0]]
        except Exception:
            pass
        return []
    if f.type in ("hole", "sphere"):
        r = f.diameter / 2.0
        return [(f.x + r * math.cos(2 * math.pi * i / n),
                 f.y + r * math.sin(2 * math.pi * i / n)) for i in range(n)]
    if f.type == "slot":
        L, W = max(f.length, f.width), f.width
        r = W / 2.0
        half = max(0.0, (L - W) / 2.0)
        a = math.radians(f.angle or 0.0)
        ca, sa = math.cos(a), math.sin(a)
        pts: List[Pt] = []
        for i in range(n // 2 + 1):                     # 右端半圆
            t = -math.pi / 2 + math.pi * i / (n // 2)
            pts.append((half + r * math.cos(t), r * math.sin(t)))
        for i in range(n // 2 + 1):                     # 左端半圆
            t = math.pi / 2 + math.pi * i / (n // 2)
            pts.append((-half + r * math.cos(t), r * math.sin(t)))
        return [(f.x + x * ca - y * sa, f.y + x * sa + y * ca) for x, y in pts]
    # 矩形（含圆角，近似为直角矩形即可）
    hl, hw = f.length / 2.0, f.width / 2.0
    return [(f.x - hl, f.y - hw), (f.x + hl, f.y - hw),
            (f.x + hl, f.y + hw), (f.x - hl, f.y + hw)]


def _inside(pt: Pt, poly: List[Pt]) -> bool:
    x, y = pt
    ins = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y):
            xc = (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi
            if x < xc:
                ins = not ins
        j = i
    return ins


def _seg_dist(p: Pt, a: Pt, b: Pt) -> float:
    px, py = p
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 <= 1e-12 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)


def gap_between(pa: List[Pt], pb: List[Pt]) -> float:
    """两个闭合轮廓的最小间隙；相交/包含返回 -1。"""
    if any(_inside(p, pb) for p in pa) or any(_inside(p, pa) for p in pb):
        return -1.0
    best = float("inf")
    for p in pa:
        for i in range(len(pb)):
            best = min(best, _seg_dist(p, pb[i], pb[(i + 1) % len(pb)]))
    for p in pb:
        for i in range(len(pa)):
            best = min(best, _seg_dist(p, pa[i], pa[(i + 1) % len(pa)]))
    return best


def knife_edge(pa: List[Pt], pb: List[Pt], tol: float = THIN_WALL) -> float:
    """两个轮廓「相交」但一侧顶点几乎贴在另一侧边界上 → 零厚度尖角。

    gap_between 对相交轮廓直接返回 -1（当作正常并集），于是「孔心正好落在
    另一特征侧壁平面上」这种退化情形会被静默放过。实测箱座 Φ9@(79,0) 的孔心
    就在 step158×50 的 x=79 壁上，切完后剩余材料在 (79,±4.5) 收成零厚度尖角，
    SolidWorks 抛 -2147352561 '非选择性的参数。' 直接拒绝。这里把这种情形单独
    量出来，返回最小贴边距离（>=tol 或不相交时返回 inf）。
    """
    best = float("inf")
    for pts, poly in ((pa, pb), (pb, pa)):
        for p in pts:
            if not _inside(p, poly):
                continue
            d = min((_seg_dist(p, poly[i], poly[(i + 1) % len(poly)])
                     for i in range(len(poly))), default=1e9)
            if d < best:
                best = d
    return best if best < tol else float("inf")


def contained(inner: List[Pt], outer: List[Pt], tol: float = 0.02) -> bool:
    """inner 是否完全落在 outer 内（允许多边形近似误差 tol）。"""
    if all(_inside(p, outer) for p in inner):
        return True
    # 边界重合也算包含（例如型腔与槽同宽同中心）
    for p in inner:
        if _inside(p, outer):
            continue
        d = min((_seg_dist(p, outer[i], outer[(i + 1) % len(outer)])
                 for i in range(len(outer))), default=1e9)
        if d > tol:
            return False
    return True


def _cut_depth(spec, f) -> float:
    if f.type == "sphere":
        return min(f.depth or f.diameter / 2.0, spec.thickness)
    if f.type == "hole":
        return spec.thickness if (f.through or f.depth <= 0) else min(f.depth, spec.thickness)
    return spec.thickness if (f.through and f.depth <= 0) else min(f.depth or spec.thickness,
                                                                  spec.thickness)


def analyze(spec) -> Dict:
    """返回 {skip:[idx], grow:{idx:mm}, warnings:[str]}

    判定顺序（每个特征对只看一次）：
      1) 一个轮廓完全落在另一个里，且外层切得不浅于内层 → 内层是重复表达，跳过
      2) 轮廓重叠（相交）→ 正常并集切除，不报
      3) 轮廓分离但间隙 < 0.5mm → 薄壁：把较小的那个孔/槽外扩，并入大轮廓
    """
    feats = list(spec.features or [])
    res: Dict = {"skip": [], "grow": {}, "warnings": [], "contained": [], "thin": [],
                 "knife": []}
    if len(feats) < 2:
        return res
    # CAD 导入的 spec 来自真实几何，不是识图猜测：越界/贴壁/薄壁这些
    # 「识图误读」启发式一律不适用，也绝不能写 dropped_features（否则正确
    # 的 CAD 模型会被判未完成）。只保留「完全包含＝重复表达」的几何去重。
    origin_cad = str(getattr(spec, "origin", "vision") or "vision").lower() == "cad"
    polys = [outline(f) for f in feats]
    depths = [_cut_depth(spec, f) for f in feats]
    skip = set()
    # 键槽跟孔一体，不参与越界/薄壁判定（它本身就从孔壁开始）
    idx_keep = [k for k, _f in enumerate(feats)
                if str(getattr(_f, "type", "")).lower() != "keyway"]

    # ---- 0) 越出毛坯：物理上不可能存在，是识图错误的确定性证据，直接判无效
    #       （不像薄壁那样需要人判断）。必须在这里做，因为 build_features() /
    #       part_volume_exact() / geometry 都只认 conflict 的结论，validate() 只在
    #       识图流程末尾跑一次，直调 build() 的路径会绕过它。
    ign_blank = set()
    if getattr(spec, "base_profile", None):
        _bx0, _by0, _bx1, _by1 = spec.profile_bbox()
        _cx, _cy = (_bx0 + _bx1) / 2.0, (_by0 + _by1) / 2.0
        half_l, half_w = (_bx1 - _bx0) / 2.0, (_by1 - _by0) / 2.0
    else:
        _cx, _cy = 0.0, 0.0
        half_l, half_w = spec.length / 2.0, spec.width / 2.0
    for i in (() if origin_cad else idx_keep):
        f = feats[i]
        if f.type == "hole":
            rx = ry = f.diameter / 2.0
        else:
            rx = max(f.length, f.width or 0.0) / 2.0
            ry = (f.width or f.length) / 2.0
        over_x = abs(f.x - _cx) + rx - half_l
        over_y = abs(f.y - _cy) + ry - half_w
        if spec.base_kind == "round":
            over = (f.x ** 2 + f.y ** 2) ** 0.5 + max(rx, ry) - spec.diameter / 2.0
            over_x = over_y = over
        # 与毛坯等大（或更大）的 step/pocket：切掉等于整块去掉，NX 会直接
        # "方法 Update 失败"，是识图把整个外形当成了台阶/型腔。
        if f.type in ("step", "pocket") and spec.base_kind == "rect":
            if (f.length >= spec.length - 0.5) and (f.width >= spec.width - 0.5):
                ign_blank.add(i)
                msg = (f"特征 {label(f)} 与毛坯 {spec.length:g}×{spec.width:g} 等大"
                       f"（等于整块切除），判为识图误读，不参与建模与加工")
                res["warnings"].append("【已忽略】" + msg)
                try:
                    if msg not in spec.dropped_features:
                        spec.dropped_features.append(msg)
                except AttributeError:
                    pass
                continue
        if max(over_x, over_y) > 0.01:
            ign_blank.add(i)
            msg = (f"特征 {label(f)} 超出毛坯 {spec.length:g}×{spec.width:g}"
                   f"（X 超 {max(over_x, 0):.1f} / Y 超 {max(over_y, 0):.1f} mm），"
                   f"判为识图坐标错误（常见于数字颠倒，如 ±37 读成 ±79），"
                   f"不参与建模与加工")
            res["warnings"].append("【已忽略】" + msg)
            try:
                if msg not in spec.dropped_features:
                    spec.dropped_features.append(msg)
            except AttributeError:
                pass
    for i in idx_keep:
        if i in skip:
            continue
        for j in idx_keep:
            if j <= i:
                continue
            if j in skip:
                continue
            pi, pj = polys[i], polys[j]
            # 1) 包含
            if contained(pj, pi, 0.02) or contained(pi, pj, 0.02):
                both = contained(pi, pj, 0.02) and contained(pj, pi, 0.02)
                inner = i if (contained(pi, pj, 0.02) and not both) else j
                outer = j if inner == i else i
                if depths[outer] >= depths[inner] - 1e-6:
                    skip.add(inner)
                    res["contained"].append((outer, inner))
                    res["warnings"].append(
                        f"特征 {label(feats[inner])} 完全落在 {label(feats[outer])} 内"
                        f"且切得更浅，按重复表达处理：只切 {label(feats[outer])}，"
                        f"不重复切除、不重复扣体积")
                continue
            gap = gap_between(pi, pj)
            if gap < 0:            # 轮廓相交 → 正常的并集切除
                # 但「贴边相交」会留下零厚度尖角，CAD 会拒绝，必须单独挑出来
                kn = knife_edge(pi, pj)
                if kn < float("inf"):
                    res["knife"].append((i, j, kn))
                    res["warnings"].append(
                        f"特征 {label(feats[i])} 的轮廓与 {label(feats[j])} 的壁面贴在一起"
                        f"（最近 {kn:.3f}mm），切完会留下零厚度尖角，CAD 会拒绝切除；"
                        f"孔心/轮廓落在另一特征壁面上，多半是识图坐标误读，图纸需确认")
                continue
            # 2) 薄壁
            if gap < THIN_WALL:
                small, big = (i, j) if _span(pi) <= _span(pj) else (j, i)
                res["thin"].append((small, big, gap))
                if feats[small].type in ("hole", "slot") and not origin_cad:
                    grow = round(gap + 0.1, 3)
                    res["grow"][small] = max(res["grow"].get(small, 0.0), grow)
                    res["warnings"].append(
                        f"特征 {label(feats[small])} 与 {label(feats[big])} 之间只剩 "
                        f"{gap:.3f}mm 薄壁，无法加工：已把 {label(feats[small])} 外扩 "
                        f"{grow:g}mm 并入大轮廓（图纸需确认）")
                else:
                    res["warnings"].append(
                        f"特征 {label(feats[small])} 与 {label(feats[big])} 之间只剩 "
                        f"{gap:.3f}mm 薄壁，无法加工，请确认图纸")
    # 3) 贴边（零厚度尖角）：坐标本身就不可信，按误读剔除，不能硬塞进 CAD
    ign = set()
    for i, j, kn in res.get("knife", []):
        small = i if _span(polys[i]) <= _span(polys[j]) else j
        big = j if small == i else i
        if small in ign:
            continue
        # 只有"孔心正好落在别人壁面上"（距离≈0 且小的是孔）才判误读剔除；
        # 像"台阶的侧壁压在已切通的槽里"（0.00mm 但不是孔）、或 0.2mm 薄壁，
        # 都是图纸上真实可能存在、CAD 也可能切得出来的，只告警不剔除——
        # 真切不出来会进 BuildResult.failures，门禁照样红，不会静默。
        if origin_cad or not (feats[small].type == "hole" and kn <= 0.01):
            res["warnings"].append(
                f"特征 {label(feats[small])} 与 {label(feats[big])} 的壁面几乎相接"
                f"（{kn:.2f}mm），可能产生尖角，先按原图切，切不出来会记进失败清单")
            continue
        ign.add(small)
        msg = (f"特征 {label(feats[small])} 贴在 {label(feats[big])} 的壁面上"
               f"（最近 {kn:.2f}mm），切完是零厚度尖角，判为识图坐标误读，不参与建模与加工")
        res["warnings"].append("【已忽略】" + msg)
        # 记到 spec 上，pipeline 会据此判「建模未完成」；否则剔除后理论体积同步变小，
        # 体积偏差恒为 0，反而成了最省事的假通过路径（实测 box3 剔 5 个特征差 6.3%）。
        try:
            if msg not in spec.dropped_features:
                spec.dropped_features.append(msg)
        except AttributeError:
            pass
    res["ignore"] = sorted(set(ign) | ign_blank)
    res["skip"] = sorted(skip)
    return res


def _span(poly: List[Pt]) -> float:
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return max(max(xs) - min(xs), max(ys) - min(ys))
