import json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.models import PartSpec, Feature
from app.drawing_audit import audit_spec

def show(tag, sp, **kw):
    a = audit_spec(sp, **kw)
    print(f"{tag:28s} status={a.status:11s} can_build={a.can_build} comp={a.completeness}")
    for i in a.issues:
        print("      ", i.severity, i.category, i.message[:100])
    return a

sp = PartSpec(part_name="板", length=120, width=80, thickness=35)
show("普通矩形板", sp)

sp2 = PartSpec(part_name="缺厚", length=120, width=80, thickness=0)
show("缺厚度", sp2)

sp3 = PartSpec(part_name="孔越界", length=120, width=80, thickness=10,
               features=[Feature(type="hole", x=200, y=0, diameter=12)])
show("单孔越界", sp3)

sp4 = PartSpec(part_name="腔缺深", length=120, width=80, thickness=20,
               features=[Feature(type="pocket", x=0, y=0, length=40, width=30, depth=0)])
show("型腔缺深度", sp4)
