"""CAD 文件读取：DXF / STEP / IGES / PRT / SLDPRT -> PartSpec + 建模步骤。

设计原则：
  * 只做**几何读取**，不调用视觉模型；所有尺寸来自文件本身。
  * STEP 直接用 app.stepcheck 的解析器；DXF 用 app.dxf_in。
  * PRT 走 NX 的 STEP214 转换器；SLDPRT 走 SolidWorks COM 另存 STEP。
  * IGES 先走内置保守解析（线/圆弧/点），解析不到再交给 CAD 转换。
  * 任何"没读出来"的东西都写进 uncertain，不静默编造。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .models import Feature, PartSpec
from .stepcheck import parse_step

Log = Callable[[str], None]

STEP_EXT = {".step", ".stp"}
IGES_EXT = {".igs", ".iges"}
DXF_EXT = {".dxf"}
NX_EXT = {".prt"}
SW_EXT = {".sldprt", ".sldasm"}
ALL_EXT = STEP_EXT | IGES_EXT | DXF_EXT | NX_EXT | SW_EXT


@dataclass
class CadImportResult:
    spec: PartSpec
    source_kind: str = ""
    path: str = ""
    bbox: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    bbox_min: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    bbox_max: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    cylinders: List[dict] = field(default_factory=list)
    volume: float = 0.0
    converted_step: str = ""
    uncertain: List[str] = field(default_factory=list)
    blocking: List[str] = field(default_factory=list)
    steps: List[Any] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        L, W, T = self.bbox
        holes = sum(1 for f in self.spec.features if f.type == "hole")
        slots = sum(1 for f in self.spec.features if f.type == "slot")
        parts = []
        if holes:
            parts.append(f"{holes} 个孔")
        if slots:
            parts.append(f"{slots} 个槽")
        others = len(self.spec.features) - holes - slots
        if others > 0:
            parts.append(f"{others} 个其他特征")
        desc = "、".join(parts) if parts else "未识别到孔槽特征"
        return f"{self.source_kind}：{L:.2f}×{W:.2f}×{T:.2f} mm，{desc}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_kind": self.source_kind,
            "path": self.path,
            "bbox": list(self.bbox),
            "bbox_min": list(self.bbox_min),
            "bbox_max": list(self.bbox_max),
            "volume": self.volume,
            "converted_step": self.converted_step,
            "uncertain": self.uncertain,
            "blocking": self.blocking,
            "spec": self.spec.to_dict(),
        }


def _noop(msg: str) -> None:
    pass


def kind_of(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext in STEP_EXT:
        return "STEP"
    if ext in IGES_EXT:
        return "IGES"
    if ext in DXF_EXT:
        return "DXF"
    if ext in NX_EXT:
        return "NX_PRT"
    if ext in SW_EXT:
        return "SW_PART"
    return ""


def is_cad_file(path: str) -> bool:
    return Path(path).suffix.lower() in ALL_EXT


def _axis_unit(ax: Any) -> Optional[Tuple[float, float, float]]:
    if not ax or len(ax) < 3:
        return None
    x, y, z = float(ax[0]), float(ax[1]), float(ax[2])
    n = math.sqrt(x * x + y * y + z * z)
    if n <= 1e-9:
        return None
    return (x / n, y / n, z / n)


def _cyl_sweep_deg(c: dict, circles: List[dict]) -> Optional[float]:
    """??????????????????????????

    SolidWorks ??????????????????????? 4 ?
    ?18.892 ?????? 93.39?+73.66?+180?=347.05?????????
    ??????????????????????????????
    ?????????????????? 330? ??????
    """
    r = float(c.get("r") or 0.0)
    p = c.get("p")
    if not p or r <= 0:
        return None
    ax = _axis_unit(c.get("ax")) or (0.0, 0.0, 1.0)
    planes: Dict[float, List[float]] = {}
    for cr in circles:
        rr = float(cr.get("r") or 0.0)
        if abs(rr - r) > max(1e-3, 1e-3 * r):
            continue
        q = cr.get("c") or (0.0, 0.0, 0.0)
        dv = [float(q[i]) - float(p[i]) for i in range(3)]
        t = sum(dv[i] * ax[i] for i in range(3))
        # ?????????????????????????
        # ???? placement ?? z=-1?????? z=0/80?
        d = math.sqrt(sum((dv[i] - ax[i] * t) ** 2 for i in range(3)))
        if d > max(0.25, 0.01 * r):
            continue
        sw = cr.get("sweep")
        if sw is None:
            continue
        planes.setdefault(round(t, 1), []).append(float(sw))
    if not planes:
        return None
    # ????????????????? 360???????????
    return min(360.0, max(sum(v) for v in planes.values()))


def _swap_yz(data: Dict) -> Dict:
    """把「Y 轴向上」的文件转成统一的「Z 轴向上」。

    SolidWorks 默认 Y 向上：它导出的 STEP / IGES 里，零件厚度沿 Y、
    宽度沿 Z。本软件内部一律 Z 向上（与 NX 一致），所以这里做一次
    (x, y, z) -> (x, z, y) 的换轴，包围盒与圆柱轴线一起换。
    """
    out = dict(data)
    bmin = tuple(float(v) for v in (data.get("bbox_min") or (0, 0, 0)))
    bmax = tuple(float(v) for v in (data.get("bbox_max") or (0, 0, 0)))
    if len(bmin) == 3 and len(bmax) == 3:
        nmin = (bmin[0], bmin[2], bmin[1])
        nmax = (bmax[0], bmax[2], bmax[1])
        out["bbox_min"] = tuple(min(a, b) for a, b in zip(nmin, nmax))
        out["bbox_max"] = tuple(max(a, b) for a, b in zip(nmin, nmax))
        out["bbox"] = tuple(b - a for a, b in zip(out["bbox_min"], out["bbox_max"]))
    cyls = []
    for c in (data.get("cylinders") or []):
        d = dict(c)
        pp = c.get("p")
        if pp and len(pp) == 3:
            d["p"] = (float(pp[0]), float(pp[2]), float(pp[1]))
        ax = c.get("ax")
        if ax and len(ax) == 3:
            d["ax"] = (float(ax[0]), float(ax[2]), float(ax[1]))
        cyls.append(d)
    circs = []
    for c in (data.get("circles") or []):
        d = dict(c)
        cc = c.get("c")
        if cc and len(cc) == 3:
            d["c"] = (float(cc[0]), float(cc[2]), float(cc[1]))
        ax = c.get("ax")
        if ax and len(ax) == 3:
            d["ax"] = (float(ax[0]), float(ax[2]), float(ax[1]))
        circs.append(d)
    if circs:
        out["circles"] = circs
    out["cylinders"] = cyls
    return out


def _swap_xz(data: Dict) -> Dict:
    """(x, y, z) -> (z, y, x)：把「X 轴向上」的文件转成 Z 向上。"""
    out = dict(data)
    bmin = tuple(float(v) for v in (data.get("bbox_min") or (0, 0, 0)))
    bmax = tuple(float(v) for v in (data.get("bbox_max") or (0, 0, 0)))
    if len(bmin) == 3 and len(bmax) == 3:
        nmin = (bmin[2], bmin[1], bmin[0])
        nmax = (bmax[2], bmax[1], bmax[0])
        out["bbox_min"] = tuple(min(a, b) for a, b in zip(nmin, nmax))
        out["bbox_max"] = tuple(max(a, b) for a, b in zip(nmin, nmax))
        out["bbox"] = tuple(b - a for a, b in zip(out["bbox_min"], out["bbox_max"]))
    cyls = []
    for c in (data.get("cylinders") or []):
        d = dict(c)
        pp = c.get("p")
        if pp and len(pp) == 3:
            d["p"] = (float(pp[2]), float(pp[1]), float(pp[0]))
        ax = c.get("ax")
        if ax and len(ax) == 3:
            d["ax"] = (float(ax[2]), float(ax[1]), float(ax[0]))
        cyls.append(d)
    circs = []
    for c in (data.get("circles") or []):
        d = dict(c)
        cc = c.get("c")
        if cc and len(cc) == 3:
            d["c"] = (float(cc[2]), float(cc[1]), float(cc[0]))
        ax = c.get("ax")
        if ax and len(ax) == 3:
            d["ax"] = (float(ax[2]), float(ax[1]), float(ax[0]))
        circs.append(d)
    out["cylinders"] = cyls
    if circs:
        out["circles"] = circs
    return out


def _axis_hole_counts(data: Dict) -> List[int]:
    """统计 X/Y/Z 三个方向各有多少个『整圆』圆柱面（≥330°）。"""
    circs = list(data.get("circles") or [])
    counts = [0, 0, 0]
    seen = set()
    for c in (data.get("cylinders") or []):
        ax = _axis_unit(c.get("ax"))
        p = c.get("p")
        r = float(c.get("r") or 0.0)
        if not ax or not p or r <= 0:
            continue
        k = max(range(3), key=lambda i: abs(ax[i]))
        if abs(abs(ax[k]) - 1.0) > 1e-3:
            continue
        sw = _cyl_sweep_deg(c, circs)
        if sw is not None and sw >= 330.0:
            key = (k, round(float(p[0]), 1), round(float(p[1]), 1), round(float(p[2]), 1), round(r, 3))
            if key in seen:
                continue
            seen.add(key)
            counts[k] += 1
    return counts


def _auto_axis(data: Dict) -> Dict:
    """按整圆孔最多的方向判定「厚度轴」，统一转成 Z 向上。

    SolidWorks 不同模板导出的 STEP 朝向不一致（实测旧模板 Y 向上、
    新模板 Z 向上），所以不能再按厂商硬编码换轴，要看几何本身。
    """
    n = _axis_hole_counts(data)
    if n[1] > n[2] and n[1] >= n[0]:
        return _swap_yz(data)
    if n[0] > n[2] and n[0] > n[1]:
        return _swap_xz(data)
    return data


def _step_spec(path: str, log: Log = _noop, up_axis: str = 'z') -> CadImportResult:
    data = parse_step(path)
    ua = str(up_axis).lower()
    if ua == "auto":
        data = _auto_axis(data)
    elif ua == "y":
        data = _swap_yz(data)
    spans = tuple(float(v) for v in (data.get("bbox") or (0, 0, 0)))
    bmin = tuple(float(v) for v in (data.get("bbox_min") or (0, 0, 0)))
    bmax = tuple(float(v) for v in (data.get("bbox_max") or (0, 0, 0)))
    L, W, T = spans
    if L <= 0 or W <= 0 or T <= 0:
        raise RuntimeError("STEP 中没有可识别的三维几何（包围盒为空）")
    cx = (bmin[0] + bmax[0]) / 2.0
    cy = (bmin[1] + bmax[1]) / 2.0
    cz = (bmin[2] + bmax[2]) / 2.0

    cyls = list(data.get("cylinders") or [])
    uncertain: List[str] = []
    blocking: List[str] = []

    # ---- 判定圆盘类：包围盒 X≈Y，且存在与外径同半径的竖圆柱面
    round_base = False
    if abs(L - W) <= max(0.6, 0.02 * max(L, W)):
        for c in cyls:
            ax = _axis_unit(c.get("ax"))
            r = float(c.get("r") or 0)
            if ax and abs(ax[2]) > 0.9 and abs(r - L / 2.0) <= max(0.5, 0.02 * L):
                round_base = True
                break

    # ---- 圆柱面 -> 孔。只认竖轴（Z），同轴多级按沉孔合并
    # 部分圆弧圆柱面（槽端 180°、圆角 90°）不是孔，先按真实扫掠角剔除。
    circle_recs = list(data.get("circles") or [])

    def _cyl_sweep(c) -> Optional[float]:
        # ????? STEP ???????????????????
        # ????????????SW ?????? 93?+74?+180???
        return _cyl_sweep_deg(c, circle_recs)

    groups: Dict[Tuple[float, float], List[float]] = {}
    side_axes = 0
    partial_arcs = 0
    partial_cyls: List[dict] = []
    for c in cyls:
        r = float(c.get("r") or 0)
        p = c.get("p")
        ax = _axis_unit(c.get("ax"))
        if r <= 0.2 or not p or not ax:
            continue
        if abs(ax[2]) < 0.85:
            side_axes += 1
            continue
        if round_base and abs(r - L / 2.0) <= max(0.5, 0.02 * L):
            continue
        if r >= 0.48 * min(L, W):
            continue
        _sw = _cyl_sweep(c)
        if _sw is not None and _sw < 330.0:
            partial_arcs += 1
            partial_cyls.append(c)
            continue
        x = float(p[0]) - cx
        y = float(p[1]) - cy
        key = (round(x, 1), round(y, 1))
        groups.setdefault(key, []).append(r)

    feats: List[Feature] = []
    for (x, y), radii in sorted(groups.items()):
        radii = sorted(set(round(r, 3) for r in radii))
        if not radii:
            continue
        d = round(2 * radii[0], 3)
        cb = round(2 * radii[-1], 3) if len(radii) > 1 and radii[-1] > radii[0] * 1.05 else 0.0
        f = Feature(type="hole", x=x, y=y, diameter=d, depth=0.0, through=True,
                    counterbore_dia=cb, counterbore_depth=0.0,
                    datum="center", datum_ref="CAD 文件坐标",
                    pos_source="dim", evidence="STEP 圆柱面轴线",
                    note=("Ø%g 通孔" % d) + ((" + 沉孔 Ø%g（深待定）" % cb) if cb else ""))
        feats.append(f)

    # ---- 成对的半圆柱面 -> 腰形槽（两槽端圆弧 + 直线侧壁）
    # 判定保守：半径相同、两圆心连线不贴毛坯边界、圆心距 > 1.5r。
    slot_feats: List[Feature] = []
    used_partial = 0
    uniq: Dict[Tuple[float, float, float], List[dict]] = {}
    for c0 in partial_cyls:
        r0 = float(c0.get("r") or 0.0)
        p0 = c0.get("p")
        ax0 = _axis_unit(c0.get("ax"))
        if not p0 or abs(ax0[2]) < 0.85:
            continue
        key0 = (round(float(p0[0]) - cx, 2), round(float(p0[1]) - cy, 2), round(r0, 3))
        uniq.setdefault(key0, []).append(c0)
    keys = sorted(uniq)
    used_keys = set()
    for i in range(len(keys)):
        if i in used_keys:
            continue
        x1, y1, r1 = keys[i]
        for j in range(i + 1, len(keys)):
            if j in used_keys:
                continue
            x2, y2, r2 = keys[j]
            if abs(r1 - r2) > 0.02:
                continue
            r0 = r1
            dist = math.hypot(x2 - x1, y2 - y1)
            if dist < 1.5 * r0:
                continue
            if max(abs(x1), abs(x2)) + r0 > L / 2.0 - 0.5:
                continue
            if max(abs(y1), abs(y2)) + r0 > W / 2.0 - 0.5:
                continue
            zs = []
            for kk in (keys[i], keys[j]):
                for cc in uniq[kk]:
                    pp = cc.get("p")
                    for cr in circle_recs:
                        rr = float(cr.get("r") or 0.0)
                        if abs(rr - r0) > max(1e-3, 1e-3 * r0):
                            continue
                        q = cr.get("c") or (0.0, 0.0, 0.0)
                        if math.hypot(float(pp[0]) - float(q[0]), float(pp[1]) - float(q[1])) <= max(0.25, 0.01 * r0):
                            zs.append(float(q[2]))
            zlo = min(zs) if zs else bmin[2]
            zhi = max(zs) if zs else bmax[2]
            thru = abs(zlo - bmin[2]) <= 0.6 and abs(zhi - bmax[2]) <= 0.6
            depth0 = 0.0 if thru else max(0.0, zhi - zlo)
            length0 = dist + 2.0 * r0
            width0 = 2.0 * r0
            ang = math.degrees(math.atan2(y2 - y1, x2 - x1))
            if ang < 0:
                ang += 180.0
            if ang > 180.0:
                ang -= 180.0
            slot_feats.append(Feature(
                type="slot", x=round((x1 + x2) / 2.0, 3), y=round((y1 + y2) / 2.0, 3),
                length=round(length0, 3), width=round(width0, 3), angle=round(ang, 3),
                depth=round(depth0, 3), through=bool(thru),
                datum="center", datum_ref="CAD 文件坐标", pos_source="dim",
                evidence="STEP 槽端半圆柱面 %d 处" % (len(uniq[keys[i]]) + len(uniq[keys[j]])),
                note=("腰形槽 %.3f×%.3f" % (length0, width0)) + ("（通）" if thru else "（深按 STEP 圆弧高度推断）")))
            used_keys.add(i)
            used_keys.add(j)
            used_partial += 1
            break
    if slot_feats:
        feats.extend(slot_feats)

    if feats:
        uncertain.append("STEP 导入的孔按『通孔』处理；盲孔深度/沉孔深度需人工确认")
    if side_axes:
        uncertain.append(f"检测到 {side_axes} 个水平轴圆柱面（侧向孔），当前 2.5D 建模不支持，未列入特征")
    left_partial = max(0, len(uniq) - 2 * used_partial)
    if left_partial:
        uncertain.append(f"检测到 {left_partial} 处未配对的圆弧圆柱面（圆角/异形端），未按孔处理")
    if not feats:
        uncertain.append("STEP 中未识别到竖轴圆柱孔；型腔/槽/曲面特征需人工补充")
    else:
        extra = "；已自动识别 %d 个腰形槽" % len(slot_feats) if slot_feats else ""
        uncertain.append("型腔 / 台阶 / 曲面暂不自动识别，需人工按图纸补充" + extra)

    spec = PartSpec(part_name=Path(path).stem, base_kind="round" if round_base else "rect",
                    length=round(L, 2), width=round(W, 2), thickness=round(T, 2),
                    diameter=round(max(L, W), 2) if round_base else 0.0,
                    part_type="flange" if round_base else "plate",
                    features=feats, confidence=0.62 if feats else 0.5,
                    source=Path(path).name, origin="cad",
                    notes=(f"来自 CAD 文件 {Path(path).name}；包围盒 "
                           f"{L:.2f}×{W:.2f}×{T:.2f}，圆柱面 {len(cyls)} 个。"))
    spec.raw_analysis = f"STEP bbox={spans} points={data.get('points')} cylinders={len(cyls)}"
    return CadImportResult(spec=spec, source_kind="STEP", path=str(path),
                           bbox=(L, W, T), bbox_min=bmin, bbox_max=bmax,
                           cylinders=cyls, uncertain=uncertain, blocking=blocking,
                           raw={"points": data.get("points"), "bbox": spans})


# ---------------------------------------------------------------------------
# IGES：只解析 110(线) / 100(圆弧) / 116(点) / 124(变换矩阵)
# ---------------------------------------------------------------------------

def _iges_sections(text: str) -> Tuple[str, List[str], List[str]]:
    d_lines: List[str] = []
    p_lines: List[str] = []
    g_text = ""
    for raw in text.splitlines():
        line = raw.rstrip("\r\n")
        if len(line) < 1:
            continue
        sec = line[72:73].upper() if len(line) >= 73 else ""
        if sec == "G":
            g_text += line[:72]
        elif sec == "D":
            d_lines.append(line[:72].ljust(72))
        elif sec == "P":
            p_lines.append(line[:64])
    return g_text, d_lines, p_lines


def _iges_params(p_lines: List[str]) -> Dict[int, str]:
    out: Dict[int, List[str]] = {}
    for line in p_lines:
        try:
            de = int(line[0:8])
        except Exception:
            continue
        out.setdefault(de, []).append(line[8:64].strip())
    return {k: "".join(v) for k, v in out.items()}


def _mat_apply(p: Tuple[float, float, float], m: Optional[List[float]]) -> Tuple[float, float, float]:
    if not m or len(m) < 12:
        return p
    x, y, z = p
    return (m[0] * x + m[1] * y + m[2] * z + m[3],
            m[4] * x + m[5] * y + m[6] * z + m[7],
            m[8] * x + m[9] * y + m[10] * z + m[11])


def _circle_from_3(p1, p2, p3):
    ax, ay = p1[0], p1[1]
    bx, by = p2[0], p2[1]
    cx, cy = p3[0], p3[1]
    d = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-9:
        return None
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) +
          (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) +
          (cx * cx + cy * cy) * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)
    return ux, uy, r


def _iges_spec(path: str, log: Log = _noop) -> CadImportResult:
    text = Path(path).read_text(encoding="utf-8", errors="ignore")
    _, d_lines, p_lines = _iges_sections(text)
    if not d_lines:
        raise RuntimeError("IGES 文件缺少 Directory 段")
    params = _iges_params(p_lines)
    dents: Dict[int, Tuple[int, int, int]] = {}
    for i in range(0, len(d_lines) - 1, 2):
        a = d_lines[i]
        try:
            de = i // 2 + 1
            etype = int(a[0:8])
            pptr = int(a[8:16])
            trptr = int(a[48:56])
        except Exception:
            continue
        dents[de] = (etype, pptr, trptr)

    mats: Dict[int, List[float]] = {}
    for de, (etype, pptr, _tr) in dents.items():
        if etype != 124:
            continue
        raw = params.get(de, "")
        nums = [float(x) for x in re.findall(r"[-+]?\d*\.?\d+(?:[EeDd][-+]?\d+)?", raw)[:12]]
        if len(nums) >= 12:
            mats[de] = nums

    points: List[Tuple[float, float, float]] = []
    circles: List[Tuple[float, float, float, float, bool]] = []  # cx,cy,r,z,closed
    for de, (etype, pptr, trptr) in dents.items():
        raw = params.get(de, "")
        nums = [float(x.replace("D", "E")) for x in
                re.findall(r"[-+]?\d*\.?\d+(?:[EeDd][-+]?\d+)?", raw)]
        m = mats.get(trptr)
        if etype == 110 and len(nums) >= 6:
            q1 = _mat_apply((nums[0], nums[1], nums[2]), m)
            q2 = _mat_apply((nums[3], nums[4], nums[5]), m)
            points += [q1, q2]
        elif etype == 116 and len(nums) >= 3:
            points.append(_mat_apply((nums[0], nums[1], nums[2]), m))
        elif etype == 100 and len(nums) >= 7:
            zt = nums[0]
            q1 = _mat_apply((nums[1], nums[2], zt), m)
            q2 = _mat_apply((nums[3], nums[4], zt), m)
            q3 = _mat_apply((nums[5], nums[6], zt), m)
            c = _circle_from_3(q1, q2, q3)
            if c:
                ux, uy, r = c
                closed = (abs(q1[0] - q3[0]) < 1e-6 and abs(q1[1] - q3[1]) < 1e-6)
                circles.append((ux, uy, r, (q1[2] + q2[2] + q3[2]) / 3.0, closed))
                for k in range(8):
                    a = 2 * math.pi * k / 8.0
                    points.append((ux + r * math.cos(a), uy + r * math.sin(a), (q1[2] + q2[2] + q3[2]) / 3.0))
            else:
                points += [q1, q2, q3]
    if not points:
        raise RuntimeError("IGES 中未解析到线/圆弧/点几何")
    xs = [p[0] for p in points]; ys = [p[1] for p in points]; zs = [p[2] for p in points]
    L, W, T = max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)
    if T <= 0:
        T = 1.0
    cx = (max(xs) + min(xs)) / 2.0
    cy = (max(ys) + min(ys)) / 2.0

    feats: List[Feature] = []
    seen = set()
    for (ux, uy, r, z, closed) in circles:
        if not closed:
            continue
        if r <= 0.3 or r >= 0.48 * min(L, W):
            continue
        key = (round(ux - cx, 1), round(uy - cy, 1), round(2 * r, 1))
        if key in seen:
            continue
        seen.add(key)
        feats.append(Feature(type="hole", x=key[0], y=key[1], diameter=key[2],
                             depth=0.0, through=True, datum="center",
                             datum_ref="CAD 文件坐标", pos_source="dim",
                             evidence="IGES 整圆", note="Ø%g 通孔" % key[2]))
    uncertain = ["IGES 按毫米解析；单位/盲孔深度需人工确认"]
    if not feats:
        uncertain.append("IGES 未识别到整圆孔；型腔/槽/曲面需人工补充")
    tip = "IGES 内置保守解析" if not converted else "CAD 转换"
    spec = PartSpec(part_name=Path(path).stem, base_kind="rect",
                    length=round(L, 2), width=round(W, 2), thickness=round(T, 2),
                    features=feats, confidence=0.55 if feats else 0.45,
                    source=Path(path).name, origin="cad",
                    notes=f"来自 IGES（{tip}）；包围盒 {L:.2f}×{W:.2f}×{T:.2f}。")
    return CadImportResult(spec=spec, source_kind="IGES", path=str(path),
                           bbox=(L, W, T),
                           bbox_min=(min(xs), min(ys), min(zs)),
                           bbox_max=(max(xs), max(ys), max(zs)),
                           uncertain=uncertain,
                           raw={"points": len(points), "circles": len(circles)})


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------

def _dxf_result(path: str, log: Log) -> CadImportResult:
    from .dxf_in import analyze_dxf
    spec = analyze_dxf(path)
    try:
        spec.origin = "cad"
    except Exception:
        pass
    L, W, T = spec.stock
    uncertain: List[str] = []
    blocking: List[str] = []
    # DXF 是二维图：厚度、槽深、盲孔深度通常不在轮廓文件里。
    # 这里禁止 validate() 用 10mm/12mm 之类的默认值把缺失数据“补”成可建模参数。
    try:
        for w in spec.validate(repair_dims=False):
            if str(w).strip() and str(w) not in uncertain:
                uncertain.append(str(w))
    except Exception as e:
        uncertain.append(f"DXF 参数检查未完成：{e}")
    if spec.notes:
        uncertain.append(spec.notes)
    if not spec.features:
        uncertain.append("DXF 中未识别到孔/槽；如需加工请人工补特征")
    if float(spec.thickness or 0.0) <= 0:
        msg = "DXF 缺少厚度，不能直接拉伸成实体；请人工补填厚度后再建模"
        blocking.append(msg)
        if msg not in uncertain:
            uncertain.append(msg)
    unconfirmed = [f for f in spec.features
                   if f.type == "hole" and not bool(getattr(f, "depth_confirmed", True))]
    if unconfirmed:
        msg = (f"DXF 中有 {len(unconfirmed)} 个圆未给出孔深："
               "请逐孔确认通孔，或填写盲孔深度")
        blocking.append(msg)
        if msg not in uncertain:
            uncertain.append(msg)
    return CadImportResult(spec=spec, source_kind="DXF", path=str(path),
                           bbox=(L, W, T), bbox_min=spec.bbox()[:3],
                           bbox_max=spec.bbox()[3:], uncertain=uncertain,
                           blocking=blocking)

def _export_with_engine(path: str, engine: str, settings, out_dir: str,
                        log: Log) -> str:
    base = Path(out_dir or (Path(path).parent / "_cad_import"))
    base.mkdir(parents=True, exist_ok=True)
    out = base / (Path(path).stem + ".step")
    ext = Path(path).suffix.lower()
    # NX ? step214ug.exe ??? PRT???? IGES/STEP?
    if ext in NX_EXT:
        if engine and not engine.upper().startswith("NX"):
            raise RuntimeError("读取 .prt 需要 NX 引擎（当前 %s）" % engine)
        from .cad.nx import NxAdapter
        nx = NxAdapter(settings.nx_root)
        if not nx.available():
            raise RuntimeError("未找到 NX 启动器，无法读取 .prt")
        if not nx.export_step(Path(path), out, log):
            raise RuntimeError("NX 未能把 %s 导出为 STEP" % Path(path).name)
        return str(out)
    if ext in SW_EXT:
        if engine and not engine.lower().startswith("solid"):
            raise RuntimeError("读取 .sldprt 需要 SolidWorks 引擎（当前 %s）" % engine)
        eng = "SolidWorks"
    else:
        eng = engine or "SolidWorks"
        if ext in (".igs", ".iges") and eng.upper().startswith("NX"):
            raise RuntimeError(
                "IGES 内置解析失败，当前 NX 引擎不支持把 IGES 转 STEP；"
                "请改用 SolidWorks 引擎，或先人工转成 STEP")
        if ext in (".step", ".stp"):
            return str(path)
    if eng.lower().startswith("solid"):
        from .cad.sw import SwAdapter
        sw = SwAdapter(settings.sw_template)
        if not sw.available():
            raise RuntimeError("SolidWorks 不可用，无法转换 %s" % Path(path).name)
        if not sw.export_step(Path(path), out, log):
            raise RuntimeError("SolidWorks 未能把 %s 导出为 STEP" % Path(path).name)
        return str(out)
    raise RuntimeError("不支持的 CAD 引擎：%s" % eng)


def import_cad(path: str, engine: str = "", settings=None, out_dir: str = "",
               log: Log = _noop) -> CadImportResult:
    """读取一个 CAD 文件并返回统一的 PartSpec + 建模步骤。"""
    p = Path(path)
    if not p.exists():
        raise RuntimeError("文件不存在：%s" % path)
    kind = kind_of(path)
    if not kind:
        raise RuntimeError("不支持的 CAD 文件类型：%s（支持 %s）" %
                           (p.suffix, "、".join(sorted(ALL_EXT))))
    log(f"读取 CAD 文件：{p.name}（{kind}）")

    if kind == "DXF":
        res = _dxf_result(str(p), log)
    elif kind == "STEP":
        # ????? STEP ?????? Y ????????????????
        res = _step_spec(str(p), log, up_axis="auto")
    elif kind == "IGES":
        # 先内置解析；失败且给了引擎再转 STEP
        try:
            res = _iges_spec(str(p), log)
        except Exception as e:
            if not settings or not engine:
                raise
            log(f"IGES 内置解析失败（{e}），改用 {engine} 转 STEP…")
            stp = _export_with_engine(str(p), engine, settings, out_dir, log)
            res = _step_spec(stp, log, up_axis="auto")
            res.source_kind = "IGES→STEP"
            res.converted_step = stp
    elif kind in ("NX_PRT", "SW_PART"):
        if not settings:
            raise RuntimeError("读取 %s 需要 CAD 环境（未提供设置）" % p.suffix)
        eng = engine or ("NX" if kind == "NX_PRT" else "SolidWorks")
        stp = _export_with_engine(str(p), eng, settings, out_dir, log)
        # SolidWorks 默认 Y 轴向上，导出的 STEP 需换成统一的 Z 向上
        res = _step_spec(stp, log, up_axis="auto")
        res.source_kind = kind if res.source_kind == "STEP" else f"{kind}→STEP"
        res.converted_step = stp
    else:
        raise RuntimeError("未实现：%s" % kind)

    res.path = str(p)
    try:
        res.spec.origin = "cad"
    except Exception:
        pass
    try:
        from .steps import spec_to_steps
        res.steps = spec_to_steps(res.spec)
    except Exception as e:
        res.uncertain.append("建模步骤拆分失败：%s" % e)
    for u in res.uncertain:
        log(f"⚠ {u}")
    log(f"CAD 读取完成：{res.summary()}")
    return res


def import_cad_many(paths: Sequence[str], engine: str = "", settings=None,
                    out_dir: str = "", log: Log = _noop) -> List[CadImportResult]:
    out = []
    for p in paths:
        if is_cad_file(p):
            out.append(import_cad(p, engine=engine, settings=settings,
                                  out_dir=out_dir, log=log))
    return out
