import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.models import PartSpec, Feature
from app.drawing_audit import audit_spec
from app.threeview import validate_three_views, _strict_round_section_exception
from app.vision import DrawingAnalyzer
from app.calc import revolve_volume
from app.steps import spec_to_steps, steps_to_spec


def seg(r1, z1, r2, z2):
    return {"type": "line", "p1": [r1, z1], "p2": [r2, z2]}


def sleeve(notes):
    prof = [seg(39.25, 0, 39.25, 6), seg(39.25, 6, 24, 6),
            seg(24, 6, 24, 16), seg(24, 16, 19, 16),
            seg(19, 16, 19, 0), seg(19, 0, 39.25, 0)]
    return PartSpec(part_name="间隔套", base_kind="round", diameter=78.5,
                    length=78.5, width=78.5, thickness=16.0,
                    revolve_profile=prof, notes=notes)


def views_main(evidence):
    return [{"name": "main", "label": "主视图（轴向全剖）",
             "outline": {"length": 16.0, "width": 0.0, "height": 78.5},
             "features": [], "evidence": evidence}]


EV = "主视图标注：Ø78.5 外径，Ø48F7，Ø38，OCR 另见 Φ41"

sp_ok = sleeve("OCR中Φ41为误识，图中无此尺寸；母线尺寸齐全。")
v_ok = views_main(EV)
ok, why = _strict_round_section_exception(v_ok, sp_ok)
print("P15 误识排除:", ok, why)
assert ok, why
a = audit_spec(sp_ok, views=v_ok, require_views=True)
print("P15 audit:", a.status, a.can_build, a.summary)
assert a.can_build and a.status in ("ok", "review")

sp_bad41 = sleeve("母线尺寸齐全。")
ok2, why2 = _strict_round_section_exception(views_main(EV), sp_bad41)
print("P15 未声明误识:", ok2, why2)
assert not ok2 and "Ø41" in why2, why2

# 13_防护套：evidence 有 Ø28，但母线没有 r=14
prof13 = [seg(20.5, 0, 17.5, 0), seg(17.5, 0, 17.5, 5.5),
          seg(17.5, 5.5, 11.0, 5.5), seg(11.0, 5.5, 11.0, 24.5),
          seg(11.0, 24.5, 20.5, 24.5), seg(20.5, 24.5, 20.5, 0)]
sp13 = PartSpec(part_name="轴套", base_kind="round", diameter=41.0,
                length=41.0, width=41.0, thickness=24.5,
                revolve_profile=prof13, notes="单轴向剖视，尺寸齐全")
v13 = [{"name": "section", "label": "A-A剖视图",
        "outline": {"length": 41.0, "width": 0.0, "height": 24.5},
        "features": [], "evidence": "标注 Ø41/Ø35/Ø28/Ø22，长度24.5"}]
ok3, why3 = _strict_round_section_exception(v13, sp13)
print("P13 缺 Ø28:", ok3, why3)
assert not ok3 and "Ø28" in why3, why3
a3 = audit_spec(sp13, views=v13, require_views=True)
print("P13 audit:", a3.status, a3.can_build)
assert not a3.can_build

# 开放母线必须拒绝
sp_open = sleeve("")
sp_open.revolve_profile = [seg(39.25, 0, 39.25, 6), seg(39.25, 6, 24, 6),
                           seg(24, 6, 24, 16)]
ok4, why4 = _strict_round_section_exception(v_ok, sp_open)
print("开放母线:", ok4, why4)
assert not ok4

# 15_间隔套：AI 原始母线把法兰外壁走了两遍，且 C1/C2 只在 notes 里。
# repair -> inject -> absorb 必须得到 8 段原生直线母线，体积 28,573.18。
pts_bad = [(39.25, 0), (39.25, 6), (24, 6), (19, 6),
           (19, 16), (39.25, 16), (39.25, 0)]
sp_sleeve = PartSpec(part_name="间隔套", base_kind="round", diameter=78.5,
                     length=78.5, width=78.5, thickness=16.0,
                     revolve_profile=[seg(a[0], a[1], b[0], b[1])
                                      for a, b in zip(pts_bad, pts_bad[1:])],
                     notes="图注 C1、C2", chamfer=2.0,
                     features=[Feature(type="hole", x=0, y=0, diameter=38, through=True),
                               Feature(type="hole", x=0, y=0, diameter=48, through=True)])
ocr_sleeve = "Ø78.5 外径，Ø48F7，Ø38 通孔，C1、C2 倒角，轴向 16，法兰厚 6"
m1 = DrawingAnalyzer._repair_revolve_profile(sp_sleeve, ocr_sleeve)
m2 = DrawingAnalyzer._inject_revolve_chamfers(sp_sleeve, ocr_sleeve)
m3 = DrawingAnalyzer._absorb_revolve_holes(sp_sleeve)
print("P15 sleeve:", m1, "|", m2, "|", m3)
assert m1 and m2 and m3
assert len(sp_sleeve.revolve_profile) == 8, sp_sleeve.revolve_profile
assert abs(float(sp_sleeve.chamfer)) < 1e-9
assert len(sp_sleeve.features) == 0
assert not DrawingAnalyzer._revolve_check(sp_sleeve), DrawingAnalyzer._revolve_check(sp_sleeve)
ev_sleeve = "Ø78.5 外径，Ø48F7 台阶，Ø38 通孔，C1/C2 倒角；轴向 16，法兰厚 6"
ok5, why5 = _strict_round_section_exception(views_main(ev_sleeve), sp_sleeve)
print("P15 sleeve strict:", ok5, why5)
assert ok5, why5
a5 = audit_spec(sp_sleeve, views=views_main(ev_sleeve), require_views=True)
print("P15 sleeve audit:", a5.status, a5.can_build, a5.summary)
assert a5.can_build and a5.status == "ok"
assert abs(float(revolve_volume(sp_sleeve)) - 28573.1779) < 0.01
st5 = spec_to_steps(sp_sleeve)
ops5 = [x.op for x in st5]
print("P15 sleeve steps:", ops5)
assert ops5.count("revolve") == 1 and "chamfer" not in ops5, ops5
sp5b = steps_to_spec(st5, base=sp_sleeve)
assert abs(float(revolve_volume(sp5b)) - 28573.1779) < 0.01

# OCR 噪声 Ø41：确定性重建不能被第四个直径废掉（实测真图 OCR 含 Ø41）
sp_noise = PartSpec(part_name="间隔套", base_kind="round", diameter=78.5,
                    length=78.5, width=78.5, thickness=16.0,
                    revolve_profile=[seg(a[0], a[1], b[0], b[1])
                                     for a, b in zip(pts_bad, pts_bad[1:])],
                    notes="图注 C1、C2")
msg = DrawingAnalyzer.sanitize_spec(
    sp_noise,
    ocr_text="Ø78.5 外径，Ø48F7，Ø38 通孔，另见 Φ41，C1、C2，轴向 16，法兰厚 6",
    evidence_text="Ø78.5、Ø48F7、Ø38、C1、C2、16、6")
print("P15 noise OCR:", msg[-1] if msg else "")
assert msg and any("确定性重建" in m for m in msg), msg
assert len(sp_noise.revolve_profile) == 8, sp_noise.revolve_profile
assert not sp_noise.features
assert abs(float(revolve_volume(sp_noise)) - 28573.1779) < 0.01, revolve_volume(sp_noise)
print("ALL OK")
