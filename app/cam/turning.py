"""Turning (lathe) program generator for stepped shafts - FANUC style G71/G70."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import List


@dataclass
class TurnOptions:
    rough_depth: float = 1.5      # U (radial depth per pass)
    rough_feed: float = 0.25      # mm/rev
    finish_feed: float = 0.12
    spindle: int = 900
    finish_allow_x: float = 0.4   # U in G71 (diameter allowance)
    finish_allow_z: float = 0.1   # W
    program_no: int = 2001


@dataclass
class TurnResult:
    ok: bool = False
    nc_path: str = ""
    report_path: str = ""
    lines: int = 0
    ops: List[str] = field(default_factory=list)
    message: str = ""
    minutes: float = 0.0

    def to_dict(self):
        return {"ok": self.ok, "nc_path": self.nc_path, "report_path": self.report_path,
                "lines": self.lines, "ops": self.ops, "operations": self.ops,
                "message": self.message, "minutes": round(self.minutes, 1)}


def generate_turning(spec, out_dir: str, opts: TurnOptions | None = None,
                     post: str = "GENERIC") -> TurnResult:
    o = opts or TurnOptions()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    res = TurnResult()
    segs = [(float(s.get("dia") or 0), float(s.get("length") or 0)) for s in spec.shaft_segments]
    if not segs:
        res.message = "不是回转体零件"
        return res

    total = sum(l for _, l in segs)
    dmax = max(d for d, _ in segs)
    blank = dmax + 4.0
    L: List[str] = []
    L.append("%")
    L.append(f"O{o.program_no:04d} (SHAFT {spec.part_name})")
    L.append(f"(BLANK D{blank:g} x {total + 4:g}   MATERIAL {spec.material})")
    L.append(f"(POST: {post} / FANUC G71+G70)")
    L.append("G21 G40 G99 G90")
    L.append("G54")
    L.append("")
    L.append("(T0101  粗车刀 R0.8)")
    L.append("T0101")
    L.append(f"S{o.spindle} M03")
    L.append("M08")
    L.append(f"G00 X{blank + 2:.1f} Z3.0")
    L.append("(--- 端面 ---)")
    L.append("G01 Z0 F0.2")
    L.append("G01 X-1.0 F0.15")
    L.append("G00 X" + f"{blank + 2:.1f} Z2.0")
    L.append("")
    L.append("(--- G71 粗车循环 ---)")
    L.append(f"G71 U{o.rough_depth:g} R0.5")
    L.append(f"G71 P10 Q20 U{o.finish_allow_x:g} W{o.finish_allow_z:g} F{o.rough_feed:g}")
    L.append("N10 G00 X0.0")
    L.append("G01 Z0.0 F0.15")
    z = 0.0
    ops = ["端面车削", "G71 粗车", "G70 精车"]
    for i, (d, l) in enumerate(segs):
        L.append(f"G01 X{d:.3f} Z{-z:.3f}")
        z += l
        L.append(f"G01 Z{-z:.3f}")
    L.append(f"G01 X{segs[-1][0]:.3f}")
    L.append("N20 G01 X0.0")
    L.append("")
    L.append("(--- G70 精车 ---)")
    L.append("T0202")
    L.append(f"S{o.spindle + 300} M03")
    L.append(f"G70 P10 Q20 F{o.finish_feed:g}")
    L.append("")
    L.append("G00 X" + f"{blank + 20:.1f} Z{Z_SAFE:.1f}" if False else "G00 X150.0 Z150.0")
    L.append("M09")
    L.append("M05")
    L.append("M30")
    L.append("%")

    nc = out / f"{spec.file_stem}_车削.nc"
    nc.write_text("\n".join(L) + "\n", encoding="utf-8")
    res.nc_path = str(nc)
    res.lines = len(L)
    res.ops = ops
    res.minutes = (total + blank) / 120.0 * 3
    res.ok = True
    res.message = "车削程序生成完成"

    rep = out / f"{spec.file_stem}_车削说明.md"
    rep.write_text(f"""# {spec.part_name} · 车削程序说明

## 毛坯
- 棒料 Ø{blank:g} × {total + 4:g} mm

## 工序
1. 三爪卡盘夹持，伸出 {total + 10:g} mm，找正
2. 车端面
3. G71 粗车外圆（每刀切深 {o.rough_depth:g} mm，进给 {o.rough_feed:g} mm/r）
4. G70 精车（进给 {o.finish_feed:g} mm/r）
5. 调头夹持，车另一端面至总长

## 分段
{chr(10).join(f"- Ø{d:g} × {l:g}" for d, l in segs)}

## 参数
- 主轴 {o.spindle} r/min（粗）/ {o.spindle + 300} r/min（精）
- 单边留量 U{o.finish_allow_x:g}，轴向留量 W{o.finish_allow_z:g}
- 冷却：开（乳化液）

程序文件：`{nc.name}`（{len(L)} 行）
""", encoding="utf-8")
    res.report_path = str(rep)
    return res