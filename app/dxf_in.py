"""Minimal DXF → PartSpec extraction (exact 2D geometry, depth unknown)."""
from __future__ import annotations

from pathlib import Path
from typing import List

from .models import Feature, PartSpec


def analyze_dxf(path: str) -> PartSpec:
    import ezdxf
    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    circles, lines, lw = [], [], []
    for e in msp:
        t = e.dxftype()
        if t == "CIRCLE":
            circles.append((e.dxf.center.x, e.dxf.center.y, e.dxf.radius))
        elif t == "LINE":
            lines.append((e.dxf.start.x, e.dxf.start.y, e.dxf.end.x, e.dxf.end.y))
        elif t == "LWPOLYLINE":
            pts = [(p[0], p[1]) for p in e.get_points("xy")]
            lw.append((pts, e.closed))
    xs = [c[0] - c[2] for c in circles] + [l[0] for l in lines] + [l[2] for l in lines]
    ys = [c[1] - c[2] for c in circles] + [l[1] for l in lines] + [l[3] for l in lines]
    for pts, _ in lw:
        xs += [p[0] for p in pts]
        ys += [p[1] for p in pts]
    if not xs:
        raise RuntimeError("DXF 中未找到可识别的几何实体")
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    cx, cy = (minx + maxx) / 2.0, (miny + maxy) / 2.0
    L, W = maxx - minx, maxy - miny
    feats: List[Feature] = []
    outer_r = max(L, W) / 2.0
    for (x, y, r) in circles:
        if r < outer_r * 0.985:
            feats.append(Feature(type="hole", x=x - cx, y=y - cy,
                                 diameter=2 * r, through=True, depth=0.0,
                                 note="DXF 圆"))
    spec = PartSpec(part_name=Path(path).stem, base_kind="rect",
                    length=round(L, 2), width=round(W, 2), thickness=10.0,
                    features=feats, confidence=0.55, source=Path(path).name,
                    notes="来自 DXF 的二维轮廓；厚度需人工确认（默认 10mm），型腔/槽深未体现。")
    if abs(L - W) < 0.05 and len(circles) <= 1:
        spec.base_kind = "round"
        spec.diameter = round(L, 2)
        spec.part_type = "flange"
    return spec
