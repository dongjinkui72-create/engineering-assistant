# -*- coding: utf-8 -*-
"""图纸可用性审计。

目的：在进入 CAD 之前，先回答一个问题——
**这张图纸到底够不够、自相矛盾不矛盾、能不能直接建？**

三条硬规则（来自实际返工教训）：

1. 缺数据不能猜。 `PartSpec.validate()` 会给缺失的长宽厚填 100/60/12，
   给没标深度的型腔填 5mm。这些默认值一旦静默进 CAD，报告会显示"成功"，
   但建出来的不是图纸上的零件。审计必须在 validate() **之前**跑。
2. 矛盾不能平均。 特征超出毛坯、三视图长对正失败、尺寸链互斥，
   这些不是"噪声"，不能靠体积偏差 ±3% 掩盖，直接判不合格。
3. 不支持 ≠ 图纸错。 蜗轮齿形、刻线阵列这类，图纸本身是完整的，
   只是当前软件表达不了；必须单独标 support，不能骂图纸。

审计是纯函数：不修改传入的 spec，所有冲突检测都在 deepcopy 上做。
"""

from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


# ---------------------------------------------------------------- data model
SEV_ERROR = "error"
SEV_WARN = "warn"
SEV_INFO = "info"

CAT_COMPLETE = "completeness"      # 缺尺寸 / 缺视图
CAT_CONTRADICTION = "contradiction"  # 视图互斥 / 尺寸链矛盾
CAT_GEOMETRY = "geometry"          # 特征与毛坯几何冲突
CAT_RECOGNITION = "recognition"    # 识图不确定 / 坐标可疑
CAT_SUPPORT = "support"            # 图纸合格但软件不支持


@dataclass
class AuditIssue:
    code: str
    severity: str
    category: str
    message: str
    evidence: List[str] = field(default_factory=list)
    suggestion: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DrawingAudit:
    # ok | review | invalid | unsupported
    status: str = "ok"
    can_build: bool = True
    completeness: float = 1.0
    summary: str = ""
    issues: List[AuditIssue] = field(default_factory=list)
    checks: List[Dict[str, Any]] = field(default_factory=list)

    # -- helpers ---------------------------------------------------------
    @property
    def errors(self) -> List[AuditIssue]:
        return [i for i in self.issues if i.severity == SEV_ERROR]

    @property
    def warnings(self) -> List[AuditIssue]:
        return [i for i in self.issues if i.severity == SEV_WARN]

    def add(self, code: str, severity: str, category: str, message: str,
            evidence: Optional[Iterable[str]] = None, suggestion: str = "") -> None:
        # 同一句问题只报一次。识图阻断项和三视图阻断项经常用同一句话从两条
        # 路径进来；按 code 去重会留下两行。重复项若级别更高则原地升级。
        rank = {SEV_INFO: 0, SEV_WARN: 1, SEV_ERROR: 2}
        ev = [str(x) for x in (evidence or []) if str(x).strip()]
        for it in self.issues:
            if it.message != message:
                continue
            if rank.get(severity, 0) > rank.get(it.severity, 0):
                it.code = code
                it.severity = severity
                it.category = category
                if suggestion:
                    it.suggestion = suggestion
            for x in ev:
                if x not in it.evidence:
                    it.evidence.append(x)
            return
        self.issues.append(AuditIssue(code=code, severity=severity, category=category,
                                      message=message, evidence=ev,
                                      suggestion=suggestion))

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail})

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "can_build": bool(self.can_build),
            "completeness": round(float(self.completeness), 3),
            "summary": self.summary,
            "issues": [i.to_dict() for i in self.issues],
            "checks": list(self.checks),
        }

    def markdown(self) -> str:
        icon = {"ok": "✅", "review": "⚠️", "invalid": "⛔", "unsupported": "⏸️"}.get(self.status, "❔")
        lines = [f"## {icon} 图纸检查", "", f"**{self.summary}**",
                 "", f"- 完整度：{self.completeness:.0%}",
                 f"- 可直接建模：{'是' if self.can_build else '否'}"]
        if self.issues:
            lines += ["", "| 级别 | 类别 | 问题 | 证据 | 建议 |",
                      "|---|---|---|---|---|"]
            _lv = {SEV_ERROR: "⛔ 阻断", SEV_WARN: "⚠ 警告", SEV_INFO: "ℹ 提示"}
            _cat = {CAT_COMPLETE: "完整性", CAT_CONTRADICTION: "矛盾",
                    CAT_GEOMETRY: "几何", CAT_RECOGNITION: "识图", CAT_SUPPORT: "功能支持"}
            for it in self.issues:
                ev = "<br>".join(it.evidence[:4]).replace("|", "\\|")
                msg = it.message.replace("|", "\\|")
                sug = (it.suggestion or "").replace("|", "\\|")
                lines.append(f"| {_lv.get(it.severity, it.severity)} | "
                             f"{_cat.get(it.category, it.category)} | {msg} | {ev} | {sug} |")
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- utilities
def _finite(*vals: float) -> bool:
    try:
        return all(math.isfinite(float(v)) for v in vals)
    except Exception:
        return False


def _seg_points(seg: Dict[str, Any]) -> Tuple[Optional[Tuple[float, float]], Optional[Tuple[float, float]]]:
    def _pt(v):
        if isinstance(v, (list, tuple)) and len(v) >= 2:
            try:
                return (float(v[0]), float(v[1]))
            except Exception:
                return None
        return None
    if not isinstance(seg, dict):
        return None, None
    return _pt(seg.get("p1")), _pt(seg.get("p2"))


def _profile_problem(segs: Sequence[Dict[str, Any]], label: str,
                     require_closed: bool = True) -> List[str]:
    """返回轮廓的结构问题（空列表 = 没问题）。"""
    out: List[str] = []
    if not segs:
        return [f"{label}为空"]
    if len(segs) < 3:
        out.append(f"{label}只有 {len(segs)} 段，闭合轮廓至少需要 3 段")
    first_p1 = None
    last_p2 = None
    for k, seg in enumerate(segs):
        if not isinstance(seg, dict):
            out.append(f"{label}第 {k+1} 段不是对象")
            continue
        st = str(seg.get("type") or "").lower()
        if st not in ("line", "arc"):
            out.append(f"{label}第 {k+1} 段类型未知：{st or '空'}")
        p1, p2 = _seg_points(seg)
        if p1 is None or p2 is None:
            out.append(f"{label}第 {k+1} 段缺端点 p1/p2")
            continue
        if math.dist(p1, p2) <= 1e-9:
            out.append(f"{label}第 {k+1} 段长度为 0")
        if last_p2 is not None and math.dist(last_p2, p1) > 0.05:
            out.append(f"{label}第 {k} 段与第 {k+1} 段不连续"
                       f"（{last_p2[0]:g},{last_p2[1]:g} → {p1[0]:g},{p1[1]:g}）")
        if k == 0:
            first_p1 = p1
        last_p2 = p2
    if require_closed and first_p1 is not None and last_p2 is not None:
        if math.dist(first_p1, last_p2) > 0.05:
            out.append(f"{label}没有闭合（起点 {first_p1[0]:g},{first_p1[1]:g}，"
                       f"终点 {last_p2[0]:g},{last_p2[1]:g}）")
    return out


def _raw_features(raw: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return []
    feats = raw.get("features")
    return [f for f in feats if isinstance(f, dict)] if isinstance(feats, list) else []


def _has_num(d: Dict[str, Any], key: str) -> bool:
    try:
        return float(d.get(key) or 0.0) > 0
    except Exception:
        return False


def _resolved_import_note(spec, text: str) -> bool:
    """Drop import-time notes that the user has already resolved."""
    t = str(text or "").lower()
    thick = float(getattr(spec, "thickness", 0.0) or 0.0)
    if thick > 0 and (("dxf \u7f3a\u5c11\u539a\u5ea6" in t)
                      or ("thickness \u672a\u6807\u6ce8" in t)
                      or ("\u539a\u5ea6\u672a\u6807\u6ce8" in t)
                      or ("\u539a\u5ea6/\u69fd\u6df1\u672a\u4f53\u73b0" in t)):
        return True
    pending_holes = 0
    for f in getattr(spec, "features", []) or []:
        if getattr(f, "type", "") == "hole" and not getattr(f, "depth_confirmed", True):
            pending_holes += 1
    if pending_holes == 0 and (("\u672a\u7ed9\u51fa\u5b54\u6df1" in t)
                               or ("\u5b54\u6df1\u672a\u786e\u8ba4" in t)
                               or ("\u9010\u5b54\u786e\u8ba4\u901a\u5b54" in t)):
        return True
    return False


# ---------------------------------------------------------------- main audit
def audit_spec(spec, *, raw: Optional[Dict[str, Any]] = None,
               uncertain: Optional[Sequence[str]] = None,
               blocking: Optional[Sequence[str]] = None,
               views: Optional[Sequence[dict]] = None,
               require_views: bool = False,
               drawing_type: str = "part",
               notes: str = "",
               check_steps: bool = True,
               check_views: bool = True) -> DrawingAudit:
    """对一份 PartSpec 做图纸可用性审计。不修改 spec。"""
    rep = DrawingAudit()
    raw = raw if isinstance(raw, dict) else {}
    if raw.get("part") and isinstance(raw.get("part"), dict):
        raw = raw["part"]
    rf = _raw_features(raw)

    # ---- 1) 基体：尺寸是否齐、轮廓是否闭合 -------------------------
    _audit_base(spec, raw, rep)

    # ---- 2) 特征：必要尺寸是否齐 --------------------------------
    _audit_features(spec, rf, rep)
    _audit_bounds(spec, rep)

    # ---- 3) 几何冲突：特征越界 / 等大 / 薄壁 / 尖角 --------------
    _audit_conflicts(spec, rep)

    # ---- 4) 三视图互校 -----------------------------------------
    if check_views and (views is not None or require_views):
        _audit_views(spec, list(views or []), require_views, rep)

    # ---- 5) 步骤往返：中间表示有没有丢东西 ----------------------
    if check_steps:
        _audit_steps(spec, rep)

    # ---- 6) 识图给出的待确认项 ---------------------------------
    for u in (uncertain or []):
        u = str(u).strip()
        if not u or _resolved_import_note(spec, u):
            continue
        # 比例/单位误读是确定性矛盾，不是"提示"
        if any(k in u for k in ("总体尺寸疑似比例", "比例/半径误读", "单位不同",
                                "一个都没出现", "放大", "缩小")):
            rep.add("DIM_SCALE", SEV_ERROR, CAT_CONTRADICTION, u,
                    suggestion="按图上实际标注重填总体尺寸，或换更清晰的图纸")
        else:
            rep.add("UNCERTAIN", SEV_WARN, CAT_RECOGNITION, u,
                    suggestion="请人工核对后再建模")

    # ---- 7) 阻断项 ---------------------------------------------
    for b in (blocking or []):
        b = str(b).strip()
        if not b or _resolved_import_note(spec, b):
            continue
        if any(k in b for k in ("装配图", "assembly")):
            rep.add("ASSEMBLY", SEV_ERROR, CAT_SUPPORT, b,
                    suggestion="请提供该零件的零件图；装配图不能直接建模")
        else:
            rep.add("BLOCKING", SEV_ERROR, CAT_CONTRADICTION, b,
                    suggestion="先把阻断项消除后再建模")

    # ---- 8) 功能支持 -------------------------------------------
    _audit_support(spec, drawing_type, notes or str(getattr(spec, "notes", "") or ""), rep)

    # ---- 9) 汇总 ----------------------------------------------
    _finalize(rep)
    return rep


def _audit_base(spec, raw: Dict[str, Any], rep: DrawingAudit) -> None:
    name = str(getattr(spec, "part_name", "") or "零件")
    gear = float(getattr(spec, "module", 0) or 0) > 0 and int(getattr(spec, "teeth", 0) or 0) > 0
    shaft = bool(getattr(spec, "shaft_segments", None))
    revolve = bool(getattr(spec, "revolve_profile", None))
    sheet = float(getattr(spec, "sheet_t", 0) or 0) > 0 or float(getattr(spec, "sheet_L", 0) or 0) > 0
    profile = bool(getattr(spec, "base_profile", None))
    round_base = str(getattr(spec, "base_kind", "rect") or "rect").lower() == "round"

    if gear:
        ok = True
        if float(spec.module) <= 0:
            rep.add("GEAR_MODULE", SEV_ERROR, CAT_COMPLETE, "齿轮模数未标注（m≤0），无法确定齿形")
            ok = False
        if int(spec.teeth) < 3:
            rep.add("GEAR_TEETH", SEV_ERROR, CAT_COMPLETE, f"齿轮齿数不合理：{spec.teeth}")
            ok = False
        if float(getattr(spec, "face_width", 0) or 0) <= 0:
            rep.add("GEAR_WIDTH", SEV_ERROR, CAT_COMPLETE, "齿轮齿宽未标注")
            ok = False
        rep.check("基体尺寸（齿轮 m/z/齿宽）", ok,
                  f"m={spec.module:g}, z={spec.teeth}, b={spec.face_width:g}")
        return

    if shaft:
        bad = []
        for k, s in enumerate(getattr(spec, "shaft_segments", []) or []):
            try:
                d, L = float(s.get("dia") or 0), float(s.get("length") or 0)
            except Exception:
                d, L = 0, 0
            if d <= 0 or L <= 0:
                bad.append(f"第 {k+1} 段 dia={d:g}, length={L:g}")
        if bad:
            rep.add("SHAFT_SEG", SEV_ERROR, CAT_COMPLETE,
                    "轴段尺寸不全：" + "；".join(bad),
                    suggestion="每段必须同时给出直径与长度")
        rep.check("基体尺寸（轴段）", not bad, "；".join(bad) or f"{len(spec.shaft_segments)} 段")
        return

    if revolve:
        prof = [x for x in (getattr(spec, "revolve_profile", []) or []) if isinstance(x, dict)]
        probs = _profile_problem(prof, "回转母线")
        rr = []
        zz = []
        for seg in prof:
            for k in ("p1", "p2"):
                q = seg.get(k)
                if isinstance(q, (list, tuple)) and len(q) >= 2:
                    try:
                        rr.append(float(q[0])); zz.append(float(q[1]))
                    except Exception:
                        pass
        if rr and min(rr) < -1e-6:
            probs.append(f"回转母线出现负半径 r={min(rr):g}")
        if rr and max(rr) <= 1e-9:
            probs.append("回转母线最大半径 r=0，转出来是空体")
        if zz and (max(zz) - min(zz)) <= 1e-9:
            probs.append("回转母线轴向长度为 0")
        for p in probs:
            rep.add("REVOLVE_PROFILE", SEV_ERROR, CAT_CONTRADICTION if "不连续" in p or "闭合" in p
                    else CAT_COMPLETE, p,
                    suggestion="回转母线必须是 r≥0、首尾闭合的连续直线/圆弧")
        rep.check("基体尺寸（回转母线）", not probs,
                  f"{len(prof)} 段，Rmax={max(rr) if rr else 0:g}，"
                  f"H={max(zz)-min(zz) if zz else 0:g}")
        return

    if sheet:
        miss = [k for k, v in (("板长 sheet_L", spec.sheet_L), ("板宽 sheet_W", spec.sheet_W),
                               ("板厚 sheet_t", spec.sheet_t)) if float(v or 0) <= 0]
        for m in miss:
            rep.add("SHEET_DIM", SEV_ERROR, CAT_COMPLETE, f"钣金件缺 {m}")
        rep.check("基体尺寸（钣金）", not miss,
                  f"{spec.sheet_L:g}×{spec.sheet_W:g}×{spec.sheet_t:g}")
        return

    if profile:
        prof = [x for x in (getattr(spec, "base_profile", []) or []) if isinstance(x, dict)]
        probs = _profile_problem(prof, "基体轮廓")
        for p in probs:
            rep.add("BASE_PROFILE", SEV_ERROR, CAT_CONTRADICTION, p,
                    suggestion="基体轮廓必须是首尾闭合的连续直线/真圆弧")
        try:
            bb = spec.profile_bbox()
            wh = (bb[2] - bb[0], bb[3] - bb[1])
        except Exception:
            wh = (0, 0)
        if wh[0] <= 0 or wh[1] <= 0:
            probs.append(f"基体轮廓包围盒无效：{wh[0]:g}×{wh[1]:g}")
        if float(getattr(spec, "thickness", 0) or 0) <= 0:
            probs.append("厚度未标注")
            rep.add("THICKNESS", SEV_ERROR, CAT_COMPLETE, "零件厚度未标注，无法拉伸成实体",
                    suggestion="在图纸上找到板厚/总高并补填")
        rep.check("基体尺寸（自由轮廓）", not probs,
                  "；".join(probs) or f"{wh[0]:g}×{wh[1]:g}×{spec.thickness:g}")
        return

    # 普通板/圆盘
    if round_base:
        D = float(getattr(spec, "diameter", 0) or 0)
        T = float(getattr(spec, "thickness", 0) or 0)
        if D <= 0:
            # 常见兜底：把 length 当直径
            cand = max(float(getattr(spec, "length", 0) or 0),
                       float(getattr(spec, "width", 0) or 0))
            rep.add("DIAMETER", SEV_ERROR, CAT_COMPLETE,
                    "圆盘/回转体外径未标注（diameter≤0）" +
                    (f"；length/width 里有 {cand:g}，但不能确认是直径" if cand > 0 else ""),
                    suggestion="补填 Ø 直径；如果是矩形件请把基体类型改为矩形")
        if T <= 0:
            rep.add("THICKNESS", SEV_ERROR, CAT_COMPLETE, "零件厚度未标注（thickness≤0）")
        rep.check("基体尺寸（圆盘）", D > 0 and T > 0, f"Ø{D:g}×{T:g}")
        return

    L, W, T = float(spec.length or 0), float(spec.width or 0), float(spec.thickness or 0)
    missing = []
    if L <= 0:
        missing.append("长度 length")
    if W <= 0:
        missing.append("宽度 width")
    if T <= 0:
        missing.append("厚度 thickness")
    if missing:
        # raw 里有这个键但值是 0/空 → 图纸真没读到；raw 里根本没这个键 → 模型没输出
        detail = []
        for key in ("length", "width", "thickness"):
            if key in raw:
                detail.append(f"{key}={raw.get(key)!r}")
        rep.add("BASE_DIM_MISSING", SEV_ERROR, CAT_COMPLETE,
                "零件外形尺寸不全：" + "、".join(missing),
                evidence=detail,
                suggestion="图纸必须给出长/宽/厚（或直径/总高）；不允许用默认值 100×60×12 代建")
    # 数值关系粗检：非长方体时宽>长这类不一定是错，只提示
    if L > 0 and W > 0 and T > 0:
        if T > max(L, W) * 1.5:
            rep.add("THICKNESS_RATIO", SEV_WARN, CAT_CONTRADICTION,
                    f"厚度 {T:g} 明显大于长/宽（{L:g}×{W:g}），可能把总高读成了板厚")
    rep.check("基体尺寸（矩形）", not missing, f"{L:g}×{W:g}×{T:g}")


def _audit_features(spec, raw_feats: List[Dict[str, Any]], rep: DrawingAudit) -> None:
    feats = list(getattr(spec, "features", []) or [])
    L, W = float(spec.length or 0), float(spec.width or 0)
    T = float(spec.thickness or 0)
    for idx, f in enumerate(feats):
        tag = f"#{idx+1} {f.type}@({float(f.x):g},{float(f.y):g})"
        raw_f = raw_feats[idx] if idx < len(raw_feats) and isinstance(raw_feats[idx], dict) else {}

        if not _finite(f.x, f.y):
            rep.add("FEATURE_NAN", SEV_ERROR, CAT_COMPLETE,
                    f"特征 {tag} 的坐标不是有效数字", suggestion="重新识图或手工填坐标")
            continue

        if f.type == "hole":
            if float(f.diameter or 0) <= 0:
                rep.add("HOLE_DIA", SEV_ERROR, CAT_COMPLETE,
                        f"孔 {tag} 没有直径", suggestion="补标 Ø 或删掉这条误读")
            if not bool(getattr(f, "depth_confirmed", True)):
                rep.add("HOLE_DEPTH_UNCONFIRMED", SEV_ERROR, CAT_COMPLETE,
                        f"孔 {tag} 深度未确认（二维 DXF 圆不能直接当作通孔）",
                        suggestion="在建模步骤中勾选“已确认此孔的通孔/盲孔深度”，"
                                   "或填写盲孔深度")
            if T > 0 and float(f.depth or 0) > T + 0.05:
                rep.add("HOLE_TOO_DEEP", SEV_ERROR, CAT_CONTRADICTION,
                        f"孔 {tag} 深 {float(f.depth):g} > 板厚 {T:g}",
                        suggestion="盲孔深度不能超过零件厚度；通孔请把 depth 设为 0")
            if float(getattr(f, "counterbore_dia", 0) or 0) > 0:
                if float(getattr(f, "counterbore_depth", 0) or 0) <= 0:
                    rep.add("CBORE_DEPTH", SEV_ERROR, CAT_COMPLETE,
                            f"沉孔 {tag} 有 Ø{float(f.counterbore_dia):g} 但没有沉深",
                            suggestion="补沉孔深度")
                elif T > 0 and float(f.counterbore_depth) > T + 0.05:
                    rep.add("CBORE_TOO_DEEP", SEV_ERROR, CAT_CONTRADICTION,
                            f"沉孔 {tag} 深 {float(f.counterbore_depth):g} > 板厚 {T:g}")

        elif f.type in ("pocket", "slot", "step"):
            if float(f.length or 0) <= 0 or float(f.width or 0) <= 0:
                rep.add("FEATURE_SIZE", SEV_ERROR, CAT_COMPLETE,
                        f"{f.type} {tag} 尺寸不全（{float(f.length):g}×{float(f.width):g}）",
                        suggestion="长和宽都必须有图上的标注")
            if f.type == "pocket" and float(f.depth or 0) <= 0:
                # validate() 会静默补 5mm —— 这是最危险的"猜"
                raw_has_depth = any(k in raw_f for k in ("depth", "深"))
                raw_says_through = bool(raw_f.get("through"))
                if not raw_says_through:
                    rep.add("POCKET_DEPTH", SEV_ERROR, CAT_COMPLETE,
                            f"型腔 {tag} 没有深度" +
                            ("（识图输出里 depth 缺失或被清成 0）" if not raw_has_depth else
                             "（depth=0 但未明确声明通腔）"),
                            evidence=[str(raw_f)[:200]] if raw_f else [],
                            suggestion="补深度；如果确实是通腔，请显式设 through=true")
            if T > 0 and float(f.depth or 0) > T + 0.05:
                rep.add("FEATURE_TOO_DEEP", SEV_ERROR, CAT_CONTRADICTION,
                        f"{f.type} {tag} 深 {float(f.depth):g} > 板厚 {T:g}")

        elif f.type == "sphere":
            if float(f.diameter or 0) <= 0:
                rep.add("SPHERE_DIA", SEV_ERROR, CAT_COMPLETE, f"球坑 {tag} 没有直径/半径")

        elif f.type == "keyway":
            for k, v in (("长度", f.length), ("宽度", f.width), ("深度", f.depth)):
                if float(v or 0) <= 0:
                    rep.add("KEYWAY_DIM", SEV_ERROR, CAT_COMPLETE, f"键槽 {tag} 缺{k}")
                    break

        elif f.type == "profile_cut":
            prof = [x for x in (f.profile or []) if isinstance(x, dict)]
            probs = _profile_problem(prof, f"profile_cut {tag}")
            for p in probs:
                rep.add("FEATURE_PROFILE", SEV_ERROR, CAT_CONTRADICTION, p,
                        suggestion="轮廓必须是首尾闭合的连续直线/真圆弧")

        # 位置出处：假设出来的坐标 = 图上没标，属于"缺数据"
        if str(getattr(f, "pos_source", "") or "").lower() == "assumed":
            rep.add("POS_ASSUMED", SEV_WARN, CAT_RECOGNITION,
                    f"特征 {tag} 的位置是按居中/对称假设出来的，图上没有标注",
                    evidence=[str(getattr(f, "evidence", "") or "")],
                    suggestion="补标孔心到基准边的距离，或人工确认坐标")

    # 特征总数 0：完整母线/扫掠/抽壳/拱顶本身也描述了几何，不能误报“没有特征”。
    has_body_detail = bool(
        getattr(spec, "revolve_profile", None)
        or getattr(spec, "sweeps", None)
        or getattr(spec, "base_profile", None)
        or getattr(spec, "shaft_segments", None)
        or float(getattr(spec, "shell_t", 0.0) or 0.0) > 0
        or float(getattr(spec, "dome_h", 0.0) or 0.0) > 0
        or float(getattr(spec, "module", 0.0) or 0.0) > 0
        or str(getattr(spec, "part_type", "") or "").lower() == "gear")
    if not feats and not has_body_detail and (L * W * T > 0):
        rep.add("NO_FEATURES", SEV_INFO, CAT_COMPLETE,
                "识别结果里没有任何特征。若图纸确有孔/槽/台阶，说明识图漏读了")


def _audit_bounds(spec, rep: DrawingAudit) -> None:
    """特征是否落在毛坯内（单特征也要查）。

    conflict.analyze() 在特征数 < 2 时会提前返回，实测单个越界孔会漏过去；
    这里独立做一遍，不依赖特征数量。
    """
    feats = list(getattr(spec, "features", []) or [])
    if not feats:
        return
    if getattr(spec, "base_profile", None):
        try:
            bb = spec.profile_bbox()
            cx, cy = (bb[0] + bb[2]) / 2.0, (bb[1] + bb[3]) / 2.0
            half_l, half_w = (bb[2] - bb[0]) / 2.0, (bb[3] - bb[1]) / 2.0
        except Exception:
            return
    else:
        cx = cy = 0.0
        half_l = float(getattr(spec, "length", 0) or 0) / 2.0
        half_w = float(getattr(spec, "width", 0) or 0) / 2.0
    if half_l <= 0 or half_w <= 0:
        return
    round_base = str(getattr(spec, "base_kind", "rect") or "rect").lower() == "round"
    D = float(getattr(spec, "diameter", 0) or 0)
    L = float(getattr(spec, "length", 0) or 0)
    W = float(getattr(spec, "width", 0) or 0)
    for idx, f in enumerate(feats):
        if str(getattr(f, "type", "")).lower() == "keyway":
            continue
        if f.type == "hole" or f.type == "sphere":
            rx = ry = float(f.diameter or 0) / 2.0
        else:
            rx = max(float(f.length or 0), float(f.width or 0)) / 2.0
            ry = (float(f.width or f.length or 0)) / 2.0
        tag = f"#{idx+1} {f.type}@({float(f.x):g},{float(f.y):g})"
        if round_base and D > 0:
            over = math.hypot(float(f.x), float(f.y)) + max(rx, ry) - D / 2.0
            if over > 0.01:
                rep.add("FEATURE_OUT_OF_STOCK", SEV_ERROR, CAT_GEOMETRY,
                        f"特征 {tag} 超出圆盘 Ø{D:g} 的边界 {over:.1f}mm",
                        suggestion="图纸尺寸/位置互斥，或识图坐标读错；必须人工核对")
            continue
        over_x = abs(float(f.x) - cx) + rx - half_l
        over_y = abs(float(f.y) - cy) + ry - half_w
        if max(over_x, over_y) > 0.01:
            rep.add("FEATURE_OUT_OF_STOCK", SEV_ERROR, CAT_GEOMETRY,
                    f"特征 {tag} 超出毛坯 {L:g}×{W:g}"
                    f"（X 超 {max(over_x,0):.1f} / Y 超 {max(over_y,0):.1f} mm）",
                    suggestion="图纸尺寸/位置互斥，或识图坐标读错；必须人工核对")
        # 与毛坯等大的整块切除：不是合法特征
        if f.type in ("step", "pocket") and L > 0 and W > 0:
            if float(f.length or 0) >= L - 0.5 and float(f.width or 0) >= W - 0.5:
                rep.add("FEATURE_AS_BLANK", SEV_ERROR, CAT_GEOMETRY,
                        f"特征 {tag} 与毛坯 {L:g}×{W:g} 等大（等于整块切除），是识图误读",
                        suggestion="把整个外形当成台阶/型腔了；应改为 base 尺寸")


def _audit_conflicts(spec, rep: DrawingAudit) -> None:
    """在副本上跑冲突检测，避免污染真实 spec / dropped_features。"""
    try:
        from .conflict import analyze as _analyze
        sp = copy.deepcopy(spec)
        # 上游清洗已经剔除的特征，证据在这里，不能被清空。
        inherited = [str(x) for x in (getattr(sp, "dropped_features", None) or [])]
        sp.dropped_features = []
        rep_conf = _analyze(sp)
    except Exception as e:
        rep.add("CONFLICT_SKIP", SEV_WARN, CAT_RECOGNITION,
                f"几何冲突检查没能完成：{e}")
        return
    ignored = list(rep_conf.get("ignore") or [])
    dropped = inherited + [str(x) for x in (getattr(sp, "dropped_features", None) or [])]
    dropped = list(dict.fromkeys(dropped))
    if ignored or dropped:
        for msg in dropped:
            rep.add("FEATURE_OUT_OF_STOCK", SEV_ERROR, CAT_GEOMETRY,
                    "特征与毛坯几何矛盾：" + msg,
                    suggestion="图纸尺寸/位置互斥，或识图坐标读错；必须人工核对后重填")
        if ignored:
            rep.add("FEATURE_OUT_OF_STOCK", SEV_ERROR, CAT_GEOMETRY,
                    f"{len(ignored)} 个特征被判为越界/贴壁误读",
                    suggestion="剔除不能代替补全：必须人工核对后重填")
    grow = rep_conf.get("grow") or {}
    if grow:
        rep.add("THIN_WALL", SEV_WARN, CAT_GEOMETRY,
                f"{len(grow)} 个孔/槽与相邻轮廓之间不足 0.5mm，软件只能外扩并入",
                suggestion="确认薄壁是否图纸本意；若不是，孔位/尺寸需要改")
    contained = rep_conf.get("contained") or []
    if contained:
        rep.add("DUPLICATE_CUT", SEV_INFO, CAT_GEOMETRY,
                f"{len(contained)} 处轮廓完全包含且更浅，按重复表达只切外层",
                suggestion="若两个特征在图纸上是独立工序，请核对深度")
    for (i, j, kn) in (rep_conf.get("knife") or []):
        # 已被 ignore 的孔不再重复报
        if i in ignored or j in ignored:
            continue
        rep.add("KNIFE_EDGE", SEV_WARN, CAT_GEOMETRY,
                f"两个特征壁面几乎相接（{kn:.3f}mm），可能切出零厚度尖角",
                suggestion="确认是否应为薄壁/相切结构")


def _audit_views(spec, views: Sequence[dict], require_views: bool, rep: DrawingAudit) -> None:
    try:
        from .threeview import validate_three_views
        vr = validate_three_views(list(views or []), spec, require_views=require_views)
    except Exception as e:
        rep.add("THREEVIEW_SKIP", SEV_WARN, CAT_RECOGNITION, f"三视图核对失败：{e}")
        return
    for b in vr.blocking:
        rep.add("THREEVIEW_BLOCK", SEV_ERROR, CAT_CONTRADICTION, str(b),
                suggestion="三视图对不上：先确认哪个视图/尺寸读错")
    for chk in vr.checks:
        if not chk.get("ok", True) and str(chk.getName() if hasattr(chk, "getName") else chk.get("name", "")).startswith(
                ("长对正", "高平齐", "宽相等", "视图↔参数")):
            rep.add("THREEVIEW_MISMATCH", SEV_ERROR, CAT_CONTRADICTION,
                    str(chk.get("detail") or chk.get("name") or "视图尺寸不一致"))
    _is_gear = (float(getattr(spec, "module", 0.0) or 0.0) > 0
                or str(getattr(spec, "part_type", "") or "").lower() == "gear")
    for u in vr.uncertain:
        if _is_gear and any(k in str(u) for k in
                            ("缺少主视图", "缺少俯视图", "缺少侧视图", "无法完整核对")):
            # 齿轮图纸惯例是"一个剖视 + 右上角参数表"，参数表已确定性还原全部几何；
            # 按三视图缺失告警会给出误导性的完整度扣分。
            continue
        if any(k in str(u) for k in ("缺少主视图", "缺少俯视图", "缺少侧视图", "无法完整核对")):
            rep.add("VIEW_MISSING", SEV_WARN, CAT_COMPLETE, str(u),
                    suggestion="补齐三视图或明确按单视图建模")
        elif "超出俯视图轮廓" in str(u):
            rep.add("VIEW_OUTLINE", SEV_ERROR, CAT_CONTRADICTION, str(u))


def _audit_steps(spec, rep: DrawingAudit) -> None:
    try:
        from .steps import spec_to_steps, steps_to_spec
        from .calc import part_volume_exact
        sp1 = copy.deepcopy(spec)
        sp1.dropped_features = []
        steps = spec_to_steps(sp1)
        sp2 = steps_to_spec(steps, base=copy.deepcopy(spec))
        # 只比几何关键字段
        diffs = []
        for name in ("base_kind", "diameter", "length", "width", "thickness",
                     "corner_radius", "end_left", "end_right", "module",
                     "teeth", "face_width", "sheet_L", "sheet_W", "sheet_t"):
            a, b = getattr(spec, name, None), getattr(sp2, name, None)
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                if abs(float(a) - float(b)) > max(0.02, abs(float(a)) * 0.002):
                    diffs.append(f"{name}: {a:g} → {b:g}")
            elif str(a or "") != str(b or ""):
                diffs.append(f"{name}: {a!r} → {b!r}")
        if len(spec.features) != len(sp2.features):
            diffs.append(f"特征数: {len(spec.features)} → {len(sp2.features)}")
        v1 = v2 = None
        try:
            v1 = part_volume_exact(copy.deepcopy(spec))
            v2 = part_volume_exact(copy.deepcopy(sp2))
        except Exception:
            pass
        if v1 and v2 and abs(v1 - v2) > max(1.0, abs(v1) * 0.005):
            diffs.append(f"体积: {v1:,.1f} → {v2:,.1f}（{abs(v1-v2)/max(v1,1e-9):.2%}）")
        if diffs:
            _upstream = [i for i in rep.issues
                         if i.severity == SEV_ERROR and i.category in
                         (CAT_COMPLETE, CAT_CONTRADICTION, CAT_GEOMETRY)]
            if _upstream:
                rep.add("STEP_ROUNDTRIP_INFO", SEV_INFO, CAT_RECOGNITION,
                        "步骤往返有差异，但上游已有阻断错误，先修上游",
                        evidence=diffs[:8])
            else:
                # 步骤往返属于「软件内部一致性」，不是图纸错。建模真正用的是
                # spec 本身，所以这里降为警告，不阻断；但必须让用户知道
                # 「界面上看到的步骤」和「实际建模参数」有出入。
                rep.add("STEP_ROUNDTRIP", SEV_WARN, CAT_RECOGNITION,
                        "建模步骤与参数往返后不一致（软件内部一致性，不影响本次建模）",
                        evidence=diffs[:8],
                        suggestion="如果准备在界面上手工改步骤，请先反馈这份图纸与步骤")
        rep.check("步骤往返", not diffs, "；".join(diffs) or f"{len(steps)} 步，零误差")
    except Exception as e:
        rep.add("STEP_ROUNDTRIP", SEV_WARN, CAT_RECOGNITION, f"步骤往返检查失败：{e}")


_SUPPORT_HINTS = (
    ("蜗轮", "蜗轮齿形"),
    ("蜗杆", "蜗杆齿形"),
    ("花键", "花键齿形"),
    ("刻度盘", "刻度盘分度"),
    ("刻线", "刻线阵列"),
    ("焊接件", "焊接结构"),
)


def _has_real_thread(text: str) -> bool:
    """排除“是否带螺纹无法确认 / 无螺纹 / 不含螺纹”这类说明性文字。"""
    neg = ("无螺纹", "不含螺纹", "不带螺纹", "未注明螺纹", "没有螺纹",
           "是否带螺纹", "是否螺纹", "螺纹待确认", "螺纹无法确认", "无法确认螺纹")
    for clause in re.split(r"[。；;\n]", str(text or "")):
        if "螺纹" not in clause:
            continue
        if any(k in clause for k in neg):
            continue
        return True
    return False


def _audit_support(spec, drawing_type: str, notes: str, rep: DrawingAudit) -> None:
    # 只看模型明确写出的 notes 与特征说明；raw_analysis 是整段 JSON，
    # 里面的推理词（如「视为」）会把正常图纸误判成近似几何。
    _parts = [str(notes or "")]
    for _f in (getattr(spec, "features", None) or []):
        _parts.append(str(getattr(_f, "note", "") or ""))
        _parts.append(str(getattr(_f, "evidence", "") or ""))
    text = " ".join(_parts)
    if str(drawing_type or "").lower() == "assembly" or (getattr(spec, "parts", None) or []):
        rep.add("UNSUPPORTED_ASSEMBLY", SEV_ERROR, CAT_SUPPORT,
                "这是装配图/多零件结构，当前软件只建单个零件",
                suggestion="从装配图里拆出零件图后再建")
        return
    # 螺纹孔：软件按底孔直径建，不建牙型。这是既有能力，不能据此判整张图不可用；
    # 但必须在报告里说明"交付实体不含螺纹"，否则用户会以为拿到了带牙型的件。
    if _has_real_thread(text):
        rep.add("NO_THREAD_PROFILE", SEV_WARN, CAT_SUPPORT,
                "图纸含螺纹孔：按底孔直径建模，交付实体不含螺纹牙型",
                evidence=["螺纹"],
                suggestion="若需要带牙型的实体，需要另外实现螺纹特征；"
                           "作为加工依据时请在程序中注明攻丝")
    for kw, what in _SUPPORT_HINTS:
        if kw in text:
            rep.add("UNSUPPORTED_GEOMETRY", SEV_ERROR, CAT_SUPPORT,
                    f"图纸含「{kw}」，当前软件不支持{what}的原生建模",
                    evidence=[kw],
                    suggestion="图纸本身可用；需要先扩展该特征的建模能力，不能按近似几何代建")
            break
    # 识图自己承认用了近似/模拟：这是"几何做不到"，不是"图纸错"
    for kw in ("近似", "模拟", "用…代替", "用...代替", "视为", "简化成"):
        if kw in text:
            rep.add("APPROXIMATED", SEV_ERROR, CAT_SUPPORT,
                    f"识图结果里出现「{kw}」：当前软件用近似特征代替了图纸几何",
                    evidence=[notes[:200]],
                    suggestion="图纸本身可能没问题；需要实现对应特征后再建，不能把近似体当成品")
            break


def _finalize(rep: DrawingAudit) -> None:
    n_err = len([i for i in rep.issues if i.severity == SEV_ERROR])
    n_warn = len([i for i in rep.issues if i.severity == SEV_WARN])
    support = [i for i in rep.issues if i.category == CAT_SUPPORT and i.severity == SEV_ERROR]
    hard = [i for i in rep.issues if i.severity == SEV_ERROR and i.category != CAT_SUPPORT]

    rep.completeness = round(max(0.0, min(1.0, 1.0 - 0.28 * len(hard) - 0.06 * n_warn)), 3)

    if hard:
        rep.status = "invalid"
        rep.can_build = False
        rep.summary = (f"图纸不合格：存在 {len(hard)} 项阻断错误"
                       f"{'、' + str(n_warn) + ' 项警告' if n_warn else ''}，不能直接建模。")
    elif support:
        rep.status = "unsupported"
        rep.can_build = False
        rep.summary = (f"图纸数据{('基本完整' if rep.completeness >= 0.8 else '可读')}，"
                       f"但当前软件有 {len(support)} 项功能不支持，暂不能建模。")
    elif n_warn:
        rep.status = "review"
        rep.can_build = True
        rep.summary = f"图纸基本可用，但有 {n_warn} 项需要人工确认。"
    else:
        rep.status = "ok"
        rep.can_build = True
        rep.summary = "图纸检查通过：尺寸完整、视图与几何一致，可以直接建模。"


# ---------------------------------------------------------------- public helpers
def merge_audits(base: DrawingAudit, extra: DrawingAudit) -> DrawingAudit:
    """把 extra 里 base 没有的问题合并进来（用于"清洗前/清洗后"两次审计）。"""
    # 去重键用 (类别, 文本)：同一句话可能由不同代码路径给出不同 code
    # （如「缺少俯视图」既来自视图审计 VIEW_MISSING，也来自识图 UNCERTAIN），
    # 只按 code 去重会在报告里重复两遍。
    seen = {(i.category, i.message) for i in base.issues}
    for it in extra.issues:
        if (it.category, it.message) not in seen:
            base.issues.append(it)
            seen.add((it.category, it.message))
    for c in extra.checks:
        if c not in base.checks:
            base.checks.append(c)
    _finalize(base)
    return base


def merge_dicts(base: Dict[str, Any], extra: Dict[str, Any],
                authoritative: bool = False) -> Dict[str, Any]:
    """?????/????????

    authoritative=True ??extra ??? spec ???????????? spec
    ?????????????????? features ?? 1 ????????
    ?????????????????????????????????
    ???? base ??????????
    """
    if not base:
        return dict(extra or {})
    if not extra:
        return dict(base)
    if authoritative:
        a = DrawingAudit(
            issues=[AuditIssue(**i) for i in (extra.get("issues") or [])],
            checks=list(extra.get("checks") or []))
        seen = {(i.code, i.message) for i in a.issues}
        # ??????? views ??????????????????
        keep_prefixes = ("THREEVIEW_", "VIEW_")
        for raw in (base.get("issues") or []):
            code = str(raw.get("code") or "")
            if not any(code.startswith(p) for p in keep_prefixes):
                continue
            i = AuditIssue(**raw)
            if (i.code, i.message) not in seen:
                a.issues.append(i)
                seen.add((i.code, i.message))
        for c in (base.get("checks") or []):
            name = str(c.get("name") or "")
            if "??" in name and c not in a.checks:
                a.checks.append(c)
        _finalize(a)
        return a.to_dict()
    a = DrawingAudit(
        issues=[AuditIssue(**i) for i in (base.get("issues") or [])],
        checks=list(base.get("checks") or []))
    rank = {SEV_INFO: 0, SEV_WARN: 1, SEV_ERROR: 2}
    by_msg = {i.message: i for i in a.issues}
    for raw in (extra.get("issues") or []):
        i = AuditIssue(**raw)
        old = by_msg.get(i.message)
        if old is None:
            a.issues.append(i)
            by_msg[i.message] = i
        elif rank.get(i.severity, 0) > rank.get(old.severity, 0):
            old.code = i.code
            old.severity = i.severity
            old.category = i.category
            old.suggestion = i.suggestion or old.suggestion
        if old is not None:
            for ev in i.evidence:
                if ev not in old.evidence:
                    old.evidence.append(ev)
    for c in (extra.get("checks") or []):
        if c not in a.checks:
            a.checks.append(c)
    _finalize(a)
    return a.to_dict()


def quick_audit(spec, **kw) -> DrawingAudit:
    return audit_spec(spec, **kw)
