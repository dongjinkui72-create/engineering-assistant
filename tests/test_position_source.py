"""位置出处（dim / pixel / assumed）回归测试 —— 全离线，不调视觉模型。

用 `samples/sample_plate.png` 的真实图纸做对照，基准值来自 CODEX_NOTES.md
（像素实测 5.000 px/mm，型腔中心 y=-10、槽中心 y=+32、孔 (±65,±38) Ø9）。

跑法：
    python tools/test_position_source.py
输出 PASS/FAIL，任一失败退出码 1（可直接接进 CI）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import measure                      # noqa: E402
from app.models import Feature, PartSpec     # noqa: E402
from app.vision import DrawingAnalyzer       # noqa: E402

IMG = str(ROOT / "samples" / "sample_plate.png")
BASELINE = ROOT / "samples" / "sample_plate.expected.json"
TOL = 1.5          # mm，图纸像素精度 ±0.2mm，这里留足余量

_results = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(("[PASS] " if ok else "[FAIL] ") + name + (("  " + detail) if detail else ""))


def base_spec(**kw) -> PartSpec:
    spec = PartSpec(part_name="安装板", length=160.0, width=100.0, thickness=12.0,
                    base_kind="rect", corner_radius=2.0, chamfer=2.0)
    for k, v in kw.items():
        setattr(spec, k, v)
    return spec


def wrong_spec() -> PartSpec:
    """复刻识图的错误结果：型腔 y=0、槽 y=+25（都是居中假设）。"""
    s = base_spec()
    s.features = [
        Feature(type="pocket", x=0.0, y=0.0, length=80.0, width=50.0, depth=6.0),
        Feature(type="slot", x=0.0, y=25.0, length=30.0, width=10.0, through=True),
        Feature(type="hole", diameter=9.0, x=-65.0, y=38.0, through=True),
        Feature(type="hole", diameter=9.0, x=65.0, y=38.0, through=True),
        Feature(type="hole", diameter=9.0, x=-65.0, y=-38.0, through=True),
        Feature(type="hole", diameter=9.0, x=65.0, y=-38.0, through=True),
    ]
    return s


def true_spec() -> PartSpec:
    """直接读 samples/sample_plate.expected.json 当对照基准（§4.3 的做法）。

    这样基准只有一份，改图纸时只改 json，不用同步改测试。
    """
    import json
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    data.pop("_source", None)
    data.pop("_note", None)
    return PartSpec.from_dict(data)


# ---------------------------------------------------------------- 一、标定与实测

cal, items = measure.measure_image(IMG, base_spec())
check("标定成功", cal is not None and cal.px_per_mm > 0,
      f"px/mm={cal.px_per_mm:.3f}" if cal else "")
if cal:
    part_w = (cal.bbox[2] - cal.bbox[0]) / cal.px_per_mm
    part_h = (cal.bbox[3] - cal.bbox[1]) / cal.px_per_mm
    check("外形按 160×100 标定自洽", abs(part_w - 160.0) < 3 and abs(part_h - 100.0) < 3,
          f"meas {part_w:.1f}×{part_h:.1f} mm")

kinds = {it.kind for it in items}
check("检出 型腔/槽/孔 三类轮廓", {"rect", "slot", "circle"} <= kinds,
      f"detected={len(items)}: " + "、".join(it.text() for it in items))

pocket = next((it for it in items if it.kind == "rect"), None)
slot = next((it for it in items if it.kind == "slot"), None)
circles = [it for it in items if it.kind == "circle"]

check("型腔尺寸 80×50", bool(pocket) and abs(max(pocket.w_mm, pocket.h_mm) - 80) < 2
      and abs(min(pocket.w_mm, pocket.h_mm) - 50) < 2,
      pocket.size_text() if pocket else "未检出")
check("型腔中心 y=-10（不是居中 0）", bool(pocket) and abs(pocket.cy + 10.0) < TOL,
      f"y={pocket.cy:+.1f}" if pocket else "未检出")
check("槽尺寸 30×10", bool(slot) and abs(max(slot.w_mm, slot.h_mm) - 30) < 2
      and abs(min(slot.w_mm, slot.h_mm) - 10) < 2,
      slot.size_text() if slot else "未检出")
check("槽中心 y=+32（不是 25）", bool(slot) and abs(slot.cy - 32.0) < TOL,
      f"y={slot.cy:+.1f}" if slot else "未检出")
check("孔数 4", len(circles) == 4, f"got {len(circles)}")

_near = lambda it, x, y: abs(it.cx - x) < TOL and abs(it.cy - y) < TOL  # noqa: E731
check("孔位 (±65,±38) 四象限", len(circles) == 4 and all([
    any(_near(c, -65, 38) for c in circles),
    any(_near(c, 65, 38) for c in circles),
    any(_near(c, -65, -38) for c in circles),
    any(_near(c, 65, -38) for c in circles)]))

# ---------------------------------------------------------------- 二、错误 spec 必须被拦

OCR = ("安装板 材料45钢 160 100 12 4×Ø9 通孔 30×10 通槽 "
       "型腔 80×50 深6 15 65 80 50 12 R2")

unc, blk, score = DrawingAnalyzer._feature_consistency(OCR, [IMG], wrong_spec())
check("错误 spec（型腔 y=0、槽 y=25）被阻断", bool(blk), f"score={score} blocking={len(blk)}")
check("错误 spec 置信度因子 <=0.45", score <= 0.45, f"score={score}")
check("阻断说明里给出实测值", any("实测" in b for b in blk),
      (blk[0][:80] + "…") if blk else "")

# 位置出处分三分：槽/型腔是 assumed，孔有标注出处是 dim
sp = wrong_spec()
DrawingAnalyzer._feature_consistency(OCR, [IMG], sp)
src = {f.type + str(i): f.pos_source for i, f in enumerate(sp.features)}
check("孔的出处是 dim", all(f.pos_source == "dim" for f in sp.features if f.type == "hole"),
      str([f.pos_source for f in sp.features if f.type == "hole"]))
check("型腔/槽的出处是 assumed（被实测推翻）",
      all(f.pos_source in ("assumed", "pixel") for f in sp.features if f.type != "hole")
      and any(f.pos_source == "assumed" for f in sp.features if f.type != "hole"),
      str([f.pos_source for f in sp.features if f.type != "hole"]))

# ---------------------------------------------------------------- 三、正确 spec 放行

sp2 = true_spec()
unc2, blk2, score2 = DrawingAnalyzer._feature_consistency(OCR, [IMG], sp2)
check("正确 spec 不阻断", not blk2, f"score={score2} blocking={blk2}")
check("正确 spec 位置出处为 dim/pixel",
      all(f.pos_source in ("dim", "pixel") for f in sp2.features),
      str([(f.type, f.pos_source) for f in sp2.features]))

# ---------------------------------------------------------------- 四、提示词开关：按像素实测自动修正

sp3 = wrong_spec()
unc3, blk3, score3 = DrawingAnalyzer._feature_consistency(
    "", [IMG], sp3, hint="按像素实测重跑")
p3 = next(f for f in sp3.features if f.type == "pocket")
s3 = next(f for f in sp3.features if f.type == "slot")
check("「按像素实测」自动把型腔改到 y≈-10", abs(p3.y + 10.0) < TOL, f"y={p3.y:g}")
check("「按像素实测」自动把槽改到 y≈+32", abs(s3.y - 32.0) < TOL, f"y={s3.y:g}")
check("自动修正后不再阻断（且留痕）", not blk3 and any("已按像素实测修正" in u for u in unc3),
      f"score={score3}")

# ---------------------------------------------------------------- 五、无实测证据时只降分不阻断

unc4, blk4, score4 = DrawingAnalyzer._feature_consistency(OCR, [], true_spec())
check("没有图纸图片时退化为仅 OCR 判据（不误阻断）", not blk4, f"score={score4}")

# ---------------------------------------------------------------- 六、法兰盘（圆形基体，极坐标孔）

FIMG = str(ROOT / "samples" / "sample_flange.png")


def flange_spec(radius: float = 45.0) -> PartSpec:
    s = PartSpec(part_name="法兰盘", base_kind="round", diameter=120.0, thickness=15.0)
    s.features = [Feature(type="hole", diameter=30.0, x=0, y=0, through=True)]
    for ang in (0.0, 60.0, 120.0, 180.0, 240.0, 300.0):
        import math
        s.features.append(Feature(type="hole", diameter=10.0,
                                  x=round(radius * math.cos(math.radians(ang)), 2),
                                  y=round(radius * math.sin(math.radians(ang)), 2),
                                  through=True))
    return s


cal2, items2 = measure.measure_image(FIMG, flange_spec())
check("法兰标定成功", cal2 is not None, f"px/mm={cal2.px_per_mm:.3f}" if cal2 else "")
if cal2:
    dia = (cal2.bbox[2] - cal2.bbox[0]) / cal2.px_per_mm
    check("法兰外径按 Ø120 标定自洽", abs(dia - 120.0) < 3, f"meas Ø{dia:.1f}")
_c30 = [it for it in items2 if it.kind == "circle" and abs(it.dia_mm - 30) < 2]
check("Ø30 内孔检出且在中心", any(abs(c.cx) < TOL and abs(c.cy) < TOL for c in _c30),
      "、".join(c.text() for c in _c30) or "未检出")
_h10 = [it for it in items2 if it.kind == "circle" and abs(it.dia_mm - 10) < 1.6]
check("Ø10 孔检出 ≥5 个", len(_h10) >= 5, f"got {len(_h10)}")
check("Ø10 孔都在 Ø90 节圆附近（R≈45）", all(
    abs((c.cx ** 2 + c.cy ** 2) ** 0.5 - 45.0) < 2.0 for c in _h10),
    "、".join(f"R={(c.cx ** 2 + c.cy ** 2) ** 0.5:.1f}" for c in _h10))

# 同一尺寸的 6 个孔必须一一对应；否则 5 个会被误判成"位置不符"
_unc5, _blk5, _sc5 = DrawingAnalyzer._feature_consistency("", [FIMG], flange_spec())
check("法兰 6 孔全对时不被阻断", not _blk5, f"score={_sc5} blocking={_blk5[:1]}")
# 大中心孔（Ø30）不能算进"小孔数"，否则会稳定误报"多 1 个"（原实现实测踩过）
check("Ø30 内孔不参与小孔计数、置信度不被误压", _sc5 >= 0.9, f"score={_sc5}")

_bad_fl = flange_spec(radius=40.0)
_unc6, _blk6, _sc6 = DrawingAnalyzer._feature_consistency("", [FIMG], _bad_fl)
check("法兰孔全错位（R40）被阻断", bool(_blk6), f"score={_sc6}")

failed = [n for n, ok, _ in _results if not ok]
print("\n结果：%d/%d 通过" % (len(_results) - len(failed), len(_results)))
if failed:
    print("失败项：" + "、".join(failed))
sys.exit(1 if failed else 0)
