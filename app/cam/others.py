"""Gear process card + sheet-metal laser/bend program."""
from __future__ import annotations

import math
from pathlib import Path


def generate_gear_card(spec, out_dir: str, post: str = "GENERIC"):
    """Gear blank turning + hobbing process card."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    from .turning import TurnResult
    d_out = spec.module * (spec.teeth + 2)
    width = spec.face_width
    d_pitch = spec.module * spec.teeth
    res = TurnResult()
    L = ["%", f"O3001 (GEAR {spec.part_name})",
         f"(BLANK D{d_out + 2:g} x {width + 2:g}  {spec.material})",
         "G21 G40 G99 G90", "G54", "",
         "(T0101 粗车)", "T0101", "S900 M03", "M08",
         f"G00 X{d_out + 4:.1f} Z2.0",
         "G01 Z0 F0.2", "G01 X-1.0 F0.15", "G00 X" + f"{d_out + 4:.1f} Z2.0",
         f"G71 U1.5 R0.5", f"G71 P10 Q20 U0.3 W0.05 F0.2",
         "N10 G00 X0.0", "G01 Z0 F0.15",
         f"G01 X{d_out:.3f} Z0", f"G01 Z{-width:.3f}",
         f"G01 X{d_out:.3f}", "N20 G01 X0.0",
         "T0202", "S1200 M03", "G70 P10 Q20 F0.1", "",
         "(镗孔)", "T0303", "S800 M03",
         f"G00 X{spec.bore - 1:.1f} Z2.0",
         f"G01 Z0 F0.15",
         f"G01 X{spec.bore:.3f} F0.1",
         f"G01 Z{-width - 1:.3f} F0.12",
         "G00 Z2.0", "",
         "G00 X150.0 Z150.0", "M09", "M05", "M30", "%"]
    nc = out / f"{spec.file_stem}_齿坯车削.nc"
    nc.write_text("\n".join(L) + "\n", encoding="utf-8")
    res.ok = True
    res.nc_path = str(nc)
    res.lines = len(L)
    res.ops = ["端面", "外圆粗车", "外圆精车", "镗孔", "滚齿(工艺卡)"]
    res.minutes = 12.0
    res.message = "齿轮程序与工艺卡生成完成"

    keyway_txt = ("，键槽 %g×%g" % (spec.keyway_w, spec.keyway_d)) if spec.keyway_w else ""
    tooth_height = 2.25 * spec.module
    card = out / f"{spec.file_stem}_滚齿工艺卡.md"
    card.write_text(f"""# {spec.part_name} · 齿轮加工工艺卡

## 齿轮参数
- 模数 m = {spec.module:g}
- 齿数 z = {spec.teeth}
- 压力角 20°
- 分度圆 Ø{d_pitch:g}，齿顶圆 Ø{d_out:g}
- 齿宽 {width:g} mm
- 轴孔 Ø{spec.bore:g} mm{keyway_txt}

## 工序
| 序号 | 工序 | 设备 | 参数 |
|---|---|---|---|
| 1 | 下料 | 锯床 | Ø{d_out + 4:g} × {width + 5:g} |
| 2 | 车端面/外圆/镗孔 | 数控车 | 见 `{nc.name}` |
| 3 | 滚齿 | 滚齿机 | 滚刀 m{spec.module:g} 单头，主轴 180 r/min，进给 1.2 mm/r，切深 {tooth_height:g} mm |
| 4 | 去毛刺 | 钳工 | — |
| 5 | 检验 | — | 公法线长度 / 齿圈径向跳动 |

## 滚齿参数
- 滚刀：标准渐开线滚刀，m{spec.module:g}，外径 Ø70，头数 1
- 主轴转速：180 r/min；轴向进给：1.2 mm/r
- 切削深度：全齿高 {tooth_height:g} mm，一次走刀
- 冷却：切削油

> 齿形由本软件生成的渐开线模型给出，可直接用于线切割/铣齿对刀比对。
""", encoding="utf-8")
    res.report_path = str(card)
    return res


def generate_sheet_program(spec, out_dir: str, post: str = "GENERIC"):
    """Laser/plasma cut the flat blank + bend sequence."""
    from .turning import TurnResult
    from ..sheet import unfold_size, bend_allowance
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    bl, bw = unfold_size(spec.sheet_L, spec.sheet_W, spec.sheet_t, spec.flange_h, spec.bend_r)
    ba = bend_allowance(90.0, spec.bend_r, spec.sheet_t)
    LEAD = 5.0                     # 引入/引出长度（沿底边延长线切入切出）
    L = ["%", f"O4001 (SHEET {spec.part_name})",
         f"(BLANK {bl:.2f} x {bw:.2f} x {spec.sheet_t:g}  {spec.material})",
         "(POST: 激光/等离子切割 2D，含引入/引出；割缝补偿请按板厚在机床上设定)",
         f"(KERF: 未在程序内补偿 —— 1mm 板割缝约 0.10~0.20mm，请用机床刀补或 CAM 补偿)",
         "G21 G90 G40",
         "M08 (辅助气开)",
         f"G00 X{-LEAD:.3f} Y0.0",
         "M03 (激光/等离子 开)", "G04 P0.3",
         f"G01 X0.0 Y0.0 F3000      (引入：沿底边延长线切入)",
         f"G01 X{bl:.3f} Y0.0",
         f"G01 X{bl:.3f} Y{bw:.3f}",
         f"G01 X0.0 Y{bw:.3f}",
         f"G01 X0.0 Y0.0             (闭合)",
         f"G01 X{-LEAD:.3f} Y0.0     (引出：沿原路退出，起终点自然重叠)",
         "M05 (关)", "M09", "G00 X0.0 Y0.0", "M30", "%"]
    nc = out / f"{spec.file_stem}_激光下料.nc"
    nc.write_text("\n".join(L) + "\n", encoding="utf-8")
    res = TurnResult()
    res.ok = True
    res.nc_path = str(nc)
    res.lines = len(L)
    res.ops = ["激光切割展开轮廓", "折弯 ×2"]
    res.minutes = 3.0
    res.message = "钣金下料与折弯程序生成完成"

    rep = out / f"{spec.file_stem}_钣金工艺.md"
    rep.write_text(f"""# {spec.part_name} · 钣金工艺

## 展开
- 下料尺寸：**{bl:.2f} × {bw:.2f} × {spec.sheet_t:g} mm**
- 折弯半径 R{spec.bend_r:g}，K 因子 0.44，单次折弯补偿 BA = {ba:.2f} mm

## 工序
| 序号 | 工序 | 说明 |
|---|---|---|
| 1 | 激光下料 | `{nc.name}`（{bl:.1f}×{bw:.1f}，含 5mm 引入/引出） |
| 2 | 折弯 1 | 距边 {spec.flange_h - spec.sheet_t - spec.bend_r:.2f} mm 折 90°，上模 R{spec.bend_r:g} |
| 3 | 折弯 2 | 对称边同上 |
| 4 | 去毛刺 | — |

## 切割注意事项
- 程序已含 **5mm 引入/引出**（沿底边延长线切进切出），起终点重叠，避免转角烧缺。
- **割缝补偿未写入程序**：1mm 板割缝约 0.10~0.20mm，上机前请按板厚在机床（G41/G42）
  或 CAM 里设定，否则外形会小一个割缝宽度。
- 该程序按"展开轮廓下料"生成，折弯后尺寸由折弯机保证。

## 折弯线位置（以下料左边为 0）
- 折弯线 1：X = {spec.flange_h - spec.sheet_t - spec.bend_r + ba / 2:.2f} mm
- 折弯线 2：X = {bw - (spec.flange_h - spec.sheet_t - spec.bend_r + ba / 2):.2f} mm
""", encoding="utf-8")
    res.report_path = str(rep)
    return res