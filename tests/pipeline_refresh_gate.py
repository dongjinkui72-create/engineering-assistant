# -*- coding: utf-8 -*-
"""????????????????????????

?? 15_????????? 1 ????? + ?????blocking ?
??????features ?? 1 ????????????? 0???????
???????????????????????????
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app.pipeline as pipeline
from app.config import Settings
from app.drawing_audit import audit_spec, merge_dicts
from app.models import PartSpec, Feature
from app.vision import AnalysisResult, DrawingAnalyzer


def seg(r1, z1, r2, z2):
    return {"type": "line", "p1": [r1, z1], "p2": [r2, z2]}


PROF = [
    seg(39.25, 1, 38.25, 0), seg(38.25, 0, 19, 0),
    seg(19, 0, 19, 16), seg(19, 16, 24, 16),
    seg(24, 16, 24, 8), seg(24, 8, 22, 6),
    seg(22, 6, 39.25, 6), seg(39.25, 6, 39.25, 1),
]


def make_spec(with_extra=True):
    return PartSpec(
        part_name="???", base_kind="round", diameter=78.5,
        length=78.5, width=78.5, thickness=16.0,
        revolve_profile=[dict(x) for x in PROF],
        features=([Feature(type="hole", x=0, y=0, diameter=48,
                           through=True)] if with_extra else []),
        notes="?? C1?C2", confidence=0.9,
    )


# ---- 1) merge_dicts??????????????????????? ----
base = {
    "issues": [
        {"code": "BLOCKING", "severity": "error", "category": "contradiction",
         "message": "? spec ??????? features", "evidence": [], "suggestion": ""},
        {"code": "THREEVIEW_MISMATCH", "severity": "error", "category": "contradiction",
         "message": "??????", "evidence": ["??? 27 / ??? 14"],
         "suggestion": ""},
    ],
    "checks": [{"name": "?????", "ok": False, "detail": "1 ?"}],
}
extra = {"issues": [], "checks": []}
merged = merge_dicts(base, extra, authoritative=True)
codes = [i["code"] for i in merged["issues"]]
assert "BLOCKING" not in codes, codes
assert "THREEVIEW_MISMATCH" in codes, codes
assert any(c.get("name") == "?????" for c in merged["checks"])

# ---- 2) Pipeline.analyze???? blocking -> ????? -> can_build ----
class FakeAnalyzer:
    def __init__(self, settings, log=None):
        self.s = settings
        self.log = log

    def analyze(self, files, work, hint="", on_progress=None, page=None,
                require_three_views=False):
        sp = make_spec(True)
        errs = DrawingAnalyzer._revolve_check(sp)
        assert errs, "????????????????? features"
        dup = "??????????????48@(0,0)?2"
        au = audit_spec(sp, uncertain=[dup], blocking=errs,
                        drawing_type="part", notes=sp.notes).to_dict()
        return AnalysisResult(spec=sp, drawing_type="part", notes=sp.notes,
                              uncertain=[dup], blocking=list(errs), audit=au)

    def prepare_images(self, files, work):
        return []

    def verify(self, imgs, spec, hint="", on_progress=None):
        fixed = PartSpec.from_dict(spec.to_dict())
        fixed.features = []
        if on_progress:
            on_progress("?????1 ? 0 ???")
        return fixed

    _feature_consistency = staticmethod(lambda *a, **k: ([], [], 1.0))
    _revolve_check = staticmethod(DrawingAnalyzer._revolve_check)


real = pipeline.DrawingAnalyzer
pipeline.DrawingAnalyzer = FakeAnalyzer
try:
    with tempfile.TemporaryDirectory() as td:
        s = Settings(output_dir=td)
        res = pipeline.Pipeline(s, lambda m: None).analyze(
            [str(Path(td) / "dummy.png")], verify=True)
        assert res.blocking == [], res.blocking
        assert not DrawingAnalyzer._revolve_check(res.spec)
        assert len(res.spec.features) == 0
        assert res.audit.get("can_build") is True, res.audit
        assert res.spec.confidence > 0.3, res.spec.confidence
        assert res.steps and res.steps[0].op == "revolve", [x.op for x in res.steps]
finally:
    pipeline.DrawingAnalyzer = real

print("ALL OK")
