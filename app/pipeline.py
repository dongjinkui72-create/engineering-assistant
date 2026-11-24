"""End-to-end pipeline: drawing -> PartSpec -> CAD model(s)."""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from .cad.base import BuildResult
from .calc import part_volume
from .config import Settings
from .models import PartSpec
from .steps import Step, spec_to_steps
from .vision import AnalysisResult, DrawingAnalyzer

Log = Callable[[str], None]


def _noop(msg: str) -> None:
    pass


CAD_NAMES = {"NX": "UG/NX", "SolidWorks": "SolidWorks"}


def make_adapter(name: str, s: Settings):
    if name == "NX":
        from .cad.nx import NxAdapter
        return NxAdapter(s.nx_root)
    if name == "SolidWorks":
        from .cad.sw import SwAdapter
        return SwAdapter(s.sw_template)
    raise ValueError(f"未知的 CAD：{name}")


def adapter_status(s: Settings) -> Dict[str, bool]:
    out = {}
    for n in CAD_NAMES:
        try:
            out[n] = bool(make_adapter(n, s).available())
        except Exception:
            out[n] = False
    return out


_DRIFT_THRESHOLD = 0.30      # 特征数相对上次波动超过 30% 就告警


def _record_vision_drift(settings, files, res) -> str:
    """同一份图纸这次识别的特征数 vs 上次，跳变就返回告警（并写进 result.json）。"""
    import json as _json
    from pathlib import Path as _P
    spec = getattr(res, "spec", None)
    if not files or spec is None:
        return ""
    key = "|".join(sorted(_P(f).name for f in files))
    if getattr(res, "page_index", 0):
        key += "#p%d" % (res.page_index + 1)
    state_dir = _P(getattr(settings, "output_dir", ".")) / "_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    fp = state_dir / "vision_history.json"
    try:
        hist = _json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        hist = {}
    n = len(spec.features)
    fp = sorted("%s@%.1f,%.1f/D%.1f" % (f.type, f.x, f.y, f.diameter)
                for f in spec.features)
    rec = hist.get(key) or {}
    prev = rec.get("features")
    prev_fp = rec.get("fingerprint") or []
    msg = ""
    if isinstance(prev, int) and prev > 0:
        d = abs(n - prev) / float(prev)
        if d > _DRIFT_THRESHOLD:
            msg = ("识图不稳定：同一份图纸这次识别 %d 个特征，上次 %d 个（波动 %.0f%%）。"
                   "坐标/数量请人工核对后再建模。" % (n, prev, 100 * d))
            res.uncertain = list(getattr(res, "uncertain", None) or []) + [msg]
    # 数量没变但位置变了也要报：按"类型+直径"配对后比坐标
    if prev_fp and not msg:
        def _key(s):
            return s.split("@")[0]
        old_by = {}
        for s in prev_fp:
            old_by.setdefault(_key(s), []).append(s)
        moved = []
        for s in fp:
            grp = old_by.get(_key(s)) or []
            if not grp:
                moved.append(s + "（新增）")
                continue
            grp.remove(grp[0])
            o = grp and None
        try:
            import re as _re
            def _xy(s):
                m = _re.search(r"@(-?[\d.]+),(-?[\d.]+)", s)
                return (float(m.group(1)), float(m.group(2))) if m else None
            olds = {}
            for s in prev_fp:
                olds.setdefault(_key(s), []).append(_xy(s))
            shift = []
            for s in fp:
                xy = _xy(s)
                cand = olds.get(_key(s)) or []
                if not cand:
                    shift.append(s + "（新增）")
                    continue
                best = min(cand, key=lambda c: abs(c[0] - xy[0]) + abs(c[1] - xy[1]))
                dxy = max(abs(best[0] - xy[0]), abs(best[1] - xy[1]))
                cand.remove(best)
                if dxy > 1.0:
                    shift.append("%s 由 (%.1f,%.1f) 漂到 (%.1f,%.1f)"
                                 % (_key(s), best[0], best[1], xy[0], xy[1]))
            if shift:
                m2 = ("同一图纸孔位/特征位置漂移：" + "；".join(shift[:4])
                      + "（>1mm，请人工核对）")
                res.uncertain = list(getattr(res, "uncertain", None) or []) + [m2]
                msg = msg or m2
        except Exception:
            pass
    hist[key] = {"features": n, "prev": prev, "fingerprint": fp,
                 "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        fp.write_text(_json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return msg


@dataclass

class PipelineResult:
    ok: bool = False
    analysis: Optional[AnalysisResult] = None
    builds: List[BuildResult] = field(default_factory=list)
    checks: dict = field(default_factory=dict)
    # 「CAD：特征名」形式的未切除清单，给报告和 UI 用，不能只是打日志。
    features_failed: list = field(default_factory=list)
    steps: list = field(default_factory=list)
    out_dir: str = ""
    report_json: str = ""
    report_md: str = ""
    message: str = ""
    seconds: float = 0.0


class _DrawingBlocked(RuntimeError):
    """图纸不合格 / 功能不支持：不进 CAD，直接终止。"""


class Pipeline:
    def __init__(self, settings: Settings, log: Log = _noop):
        self.s = settings
        self.log = log

    # ------------------------------------------------------------------ step 1
    def analyze(self, files: Sequence[str], hint: str = "", verify: bool = True,
                progress: Optional[Log] = None, page: Optional[int] = None,
                require_three_views: bool = False) -> AnalysisResult:
        p = progress or self.log
        work = Path(self.s.output_dir) / "_vision"
        analyzer = DrawingAnalyzer(self.s, self.log)
        res = analyzer.analyze(files, str(work), hint=hint, on_progress=p,
                                page=page, require_three_views=require_three_views)
        # 记住识图阶段的置信度。verify 重新出 spec 时不能用被临时压低的值当上限。
        _conf_analyzed = float(res.spec.confidence or 0.0)
        for u in getattr(res, "uncertain", []) or []:
            p(f"⚠ 待确认：{u}")
        # 阻断项（例如装配图上零件尺寸没依据）：明确喊出来，并压低置信度，
        # 让上层/用户知道"这个 spec 不可直接信任"。
        _blk = list(getattr(res, "blocking", None) or [])
        if _blk:
            for b in _blk:
                p(f"⛔ 不允许直接建模：{b}")
            res.spec.confidence = min(float(res.spec.confidence or 0.0), 0.3)
        if verify:
            imgs = analyzer.prepare_images(files, str(work))
            # 复核针对的是识图结果本身，不继承上面的临时降分。
            res.spec.confidence = _conf_analyzed
            res.spec = analyzer.verify(imgs, res.spec, hint=hint, on_progress=p)
            # verify ????? spec??????? spec ??????????
            # verify 会整体替换 spec：复核前基于旧 spec 的诊断不能继续保留，
            # 否则已修好的错误会把“可以建模”永久锁死。
            _STATE_DIAG_PREFIXES = (
                "\u56de\u8f6c\u6bcd\u7ebf",
                "\u8bc6\u522b\u7ed3\u679c\u91cc\u6709\u5b8c\u5168\u91cd\u5408\u7684\u5b54",
                "\u56fe\u7eb8\u6807\u6ce8 ",
                "\u56fe\u7eb8\u56fe\u50cf\u4e2d\u68c0\u51fa\u7ea6",
                "\u4ee5\u4e0b\u7279\u5f81\u7684\u4f4d\u7f6e\u56fe\u4e0a\u6ca1\u6709\u6807\u6ce8",
                "\u7279\u5f81\u81ea\u6d3d\u6027\u68c0\u67e5\u672a\u901a\u8fc7",
                "\u7279\u5f81\u6570\u91cf\u81ea\u6d3d\u6027\u68c0\u67e5\u672a\u901a\u8fc7",
                "\u4f4d\u7f6e\u51fa\u5904\u68c0\u67e5\u672a\u901a\u8fc7",
            )

            def _drop_stale_state_diag(msgs):
                out = []
                for _m in (msgs or []):
                    _t = str(_m).strip()
                    if any(_t.startswith(_x) for _x in _STATE_DIAG_PREFIXES):
                        continue
                    if _t not in out:
                        out.append(_t)
                return out

            res.uncertain = _drop_stale_state_diag(res.uncertain)
            res.blocking = _drop_stale_state_diag(res.blocking)
            # 齿轮参数表重建后，旧 spec 的「轴向总长/比例」诊断已失效。
            # 齿轮补丁在 verify 内重新注入 spec，并打上 _gear_rebuilt 标记。
            if getattr(res.spec, "_gear_rebuilt", False):
                # 齿轮 spec 是"参数表确定性重建"，旧 spec 的轮廓/键槽/三视图
                # 诊断已失效：features 被清空后 keyway 会留下 0×0 的幽灵告警。
                _GEAR_STALE = ("比例", "轴向总长", "找不到可对应的轮廓",
                               "keyway", "缺少主视图", "孔内键槽", "回转母线")
                res.uncertain = [x for x in res.uncertain
                                 if not any(k in str(x) for k in _GEAR_STALE)]
                res.blocking = [x for x in res.blocking
                                if not any(k in str(x) for k in _GEAR_STALE)]
                res.spec.confidence = max(float(res.spec.confidence or 0.0), 0.75)
            # 二次复核是"重新出一份 spec"：会把 pos_source / evidence 冲掉，
            # 也可能把位置改回居中。复核后再做一次位置出处检查，保证
            # 最终进 CAD 的那份 spec 里，坐标是有出处的（dim 或像素实测）。
            try:
                _u2, _b2, _s2 = DrawingAnalyzer._feature_consistency(
                    "", imgs, res.spec, hint)
                for _m in _u2:
                    if _m not in res.uncertain:
                        res.uncertain.append(_m)
                        p(f"⚠ 待确认：{_m}")
                for _m in _b2:
                    if _m not in res.blocking:
                        res.blocking.append(_m)
                        p(f"⛔ 不允许直接建模：{_m}")
                if _b2:
                    res.spec.confidence = min(float(res.spec.confidence or 0.0), 0.3)
            except Exception as _e:
                p(f"复核后位置出处检查跳过（{_e}）")
        # 二次复核可能改写回转母线：这里再跑一次确定性自检。
        # （verify 是"重新出一份 spec"，analyze 里的检查不能假设它还成立。）
        try:
            _rev = DrawingAnalyzer._revolve_check(res.spec)
            for _m in _rev:
                if _m not in res.blocking:
                    res.blocking.append(_m)
                    p(f"⛔ 不允许直接建模：{_m}")
            if _rev:
                res.spec.confidence = min(float(res.spec.confidence or 0.0), 0.5)
        except Exception as _e:
            p(f"回转母线自检跳过（{_e}）")

        # ---- 图纸可用性审计（复核后再审一次，与识图时那次合并）
        # 顺序要紧：必须在 spec.validate() 之前。validate() 会把缺失的长/宽/厚
        # 填成 100/60/12、把缺深度的型腔填 5mm、把越界特征直接删掉，
        # 先 validate 就等于把图纸缺陷洗白成默认值。
        try:
            from .drawing_audit import audit_spec as _audit_spec, merge_dicts as _merge_audits
            _post = _audit_spec(
                res.spec, uncertain=res.uncertain, blocking=res.blocking,
                drawing_type=str(getattr(res, "drawing_type", "part") or "part"),
                notes=str(getattr(res, "notes", "") or "")).to_dict()
            _pre = getattr(res, "audit", None) or {}
            # 复核后审计是最终 spec 的权威结果；复核前只保留三视图证据。
            res.audit = _merge_audits(_pre, _post, authoritative=True)
            _ad = res.audit
            p("📋 " + str(_ad.get("summary") or ""))
            if not _ad.get("can_build", True):
                for _it in (_ad.get("issues") or []):
                    if _it.get("severity") == "error":
                        p(f"   ⛔ {_it.get('message')}")
        except Exception as _e:
            p(f"图纸可用性审计跳过（{_e}）")

        # 最终诊断以复核后的 spec 为准，避免旧阻断造成假降分。
        try:
            _final_ad = getattr(res, "audit", None) or {}
            if res.blocking or not bool(_final_ad.get("can_build", True)):
                _cap = 0.3 if res.blocking else 0.5
                res.spec.confidence = round(min(float(res.spec.confidence or 0.0), _cap), 3)
        except Exception:
            pass
        warnings = res.spec.validate()
        for w in warnings:
            p(f"⚠ {w}")
        # 先拆成建模步骤给人看（识图 → 步骤 → 用户确认 → 建模）
        try:
            from .steps import spec_to_steps as _s2s
            res.steps = _s2s(res.spec)
            p(f"识别完成，拆成 {len(res.steps)} 个建模步骤：")
            for _i, _st in enumerate(res.steps, 1):
                p(f"  {_i}. 【{_st.op_name}】{_st.title()}"
                  + (f"   · {_st.note}" if _st.note else ""))
        except Exception as _e:
            p(f"步骤拆分失败：{_e}")
        try:
            _drift = _record_vision_drift(self.s, files, res)
            if _drift:
                p(f"⚠ {_drift}")
        except Exception:
            pass
        return res


    def analyze_three_views(self, files: Sequence[str], hint: str = "",
                            progress: Optional[Log] = None,
                            page: Optional[int] = None,
                            verify: bool = False) -> AnalysisResult:
        """工程图识别（强制三视图核对：长对正/高平齐/宽相等）。

        与 analyze() 共用同一条视觉链路，只是打开 require_three_views。
        """
        return self.analyze(files, hint=hint, verify=verify, progress=progress,
                            page=page, require_three_views=True)

    def analyze_local(self, files: Sequence[str], hint: str = "",
                      progress: Optional[Log] = None,
                      require_three_views: bool = True) -> AnalysisResult:
        """本地识图 + 图纸检验，不调用任何外部 AI/API。

        图片先走本地 OCR + 视图分割 + 尺寸反解。规则识图失败时，用内置
        本地视觉大模型产出结构化 PartSpec（仍然完全离线），再由确定性
        校验（OCR 数字、包围盒、特征越界）决定能不能建模。
        """
        p = progress or self.log
        from .local_vision import local_check
        p("本地识图（不调用外部 AI）…")
        chk = local_check(files, hint=hint, log=p,
                          require_three_views=require_three_views)
        for it in chk.issues:
            if it.severity == "error":
                p("⛔ " + it.message)
            else:
                p("⚠ " + it.message)

        spec = chk.spec
        used_ai = False
        uncertain = list(chk.uncertain)
        blocking = list(chk.blocking)
        audit = dict(chk.audit or {})
        ocr_text = "\n".join(t.text for t in chk.ocr.tokens)
        ai_md = ""

        if bool(getattr(self.s, "local_ai_enabled", True)):
            try:
                from .model_extract import extract_spec
                ex = extract_spec(files, self.s, ocr_text=ocr_text, hint=hint, log=p)
                ai_md = ex.markdown()
                if ex.available:
                    if spec is None:
                        if ex.ok and ex.spec is not None:
                            spec = ex.spec
                            used_ai = True
                            uncertain.append("总体参数由本地视觉模型识读（规则识图未通过）；"
                                             "请核对图上尺寸是否一致")
                            p("本地视觉模型补出了可建模参数，且通过尺寸交叉核对。")
                        else:
                            for b in ex.blocking:
                                if b not in blocking:
                                    blocking.append("本地视觉模型：" + b)
                    elif ex.spec is not None:
                        _mism = self._compare_spec_ai(spec, ex.spec)
                        for m in _mism:
                            uncertain.append("本地视觉模型复核：" + m)
                            issues = audit.setdefault("issues", [])
                            if isinstance(issues, list):
                                issues.append({"severity": "warn", "message": m,
                                               "code": "LOCAL_AI_DIFF"})
                        if not _mism and ex.ok:
                            p("本地视觉模型与规则识图一致 ✔")
                        # 严格模式下不做“审计失败后再用模型抢救”。
                        # 但规则 OCR 本身很低置信时，可以在识图阶段选择本地视觉模型作为读图源，
                        # 仍然只走一次确定性审计；审计不过就直接报错。
                        _rule_conf = float(getattr(spec, "confidence", 0.0) or 0.0)
                        _rule_weak = _rule_conf < 0.6 or bool(blocking)
                        if ex.ok and _rule_weak:
                            p("规则识图置信度低或证据不足，采用本地视觉模型的读图结果（仍走同一套审计）。")
                            spec = ex.spec
                            used_ai = True
                            uncertain = [u for u in uncertain
                                         if not str(u).startswith("本地视觉模型复核：")]
                            if isinstance(audit.get("issues"), list):
                                audit["issues"] = [i for i in audit["issues"]
                                                   if not (isinstance(i, dict)
                                                           and i.get("code") == "LOCAL_AI_DIFF")]
            except Exception as e:
                p(f"⚠ 本地视觉模型不可用：{e}；继续使用规则识图。")

        if used_ai:
            # 规则识图没解出总体尺寸时留下的阻断项，已被
            # “本地视觉模型 + 俯视图像素实测”这条独立证据链取代：
            # 降级为提示，不再阻断；几何仍由后面的确定性审计把关。
            _rule_only = ("三视图像素比例与 OCR 数字无法闭合",
                          "个视图外框；矩形零件至少需要")
            _kept = []
            for _b in blocking:
                if any(_k in str(_b) for _k in _rule_only):
                    uncertain.append("规则识图未能自动解出总体尺寸，"
                                     "已由本地视觉模型+像素实测完成：" + str(_b))
                else:
                    _kept.append(_b)
            blocking = _kept

        # 严格模式：本地模型输出就是最终读图结果。
        # 这里不做任何参数清洗、补值、删特征或二次模型复核，
        # 只用确定性审计判断“能不能建”；审计不过直接阻止建模。
        if used_ai:
            _views = list(chk.views or [])
            if not _views:
                try:
                    from .local_vision import measure_views_for_spec
                    _views = measure_views_for_spec(files, spec, log=p)
                    if _views:
                        p("📐 视图证据：本地像素实测 "
                          + "、".join(f"{v['name']} {v['outline']}" for v in _views))
                except Exception as _ve:
                    p(f"⚠ 视图证据测量失败：{_ve}")
            chk.views = _views
            try:
                from .drawing_audit import audit_spec as _ai_audit
                _rep = _ai_audit(
                    spec, views=_views,
                    require_views=bool(require_three_views and len(_views) >= 2),
                    uncertain=uncertain, blocking=blocking,
                    drawing_type="part", notes="本地视觉模型识图")
                audit = _rep.to_dict()
                if not bool(audit.get("can_build", True)):
                    for _it in (audit.get("issues") or []):
                        if _it.get("severity") == "error":
                            _m = "本地视觉模型：" + str(_it.get("message") or "")
                            if _m.strip() and _m not in blocking:
                                blocking.append(_m)
                p("📋 本地视觉模型结果审计：" + str(audit.get("summary") or ""))
            except Exception as e:
                _m = f"本地视觉模型结果无法审计：{e}"
                if _m not in blocking:
                    blocking.append(_m)
                p("⛔ " + _m)

        if spec is None:
            why = "；".join(blocking) or "没有读到可用于建模的总体尺寸"
            raise RuntimeError("本地识图未能得到可信参数：" + why)

        if audit and not bool(audit.get("can_build", True)):
            for it in (audit.get("issues") or []):
                if it.get("severity") == "error":
                    msg = str(it.get("message") or "")
                    if msg and msg not in blocking:
                        blocking.append(msg)
        if not used_ai and not chk.can_build and not blocking:
            blocking.append(str(audit.get("summary") or "本地识图结果证据不足，需人工确认"))
        steps = []
        try:
            steps = spec_to_steps(spec)
            p(f"本地识图完成，拆成 {len(steps)} 个建模步骤：")
            for _i, _st in enumerate(steps, 1):
                p(f"  {_i}. 【{_st.op_name}】{_st.title()}"
                  + (f"   · {_st.note}" if getattr(_st, "note", "") else ""))
        except Exception as e:
            p(f"步骤拆分失败：{e}")
        raw = chk.report_md or json.dumps(
            {"issues": [i.__dict__ for i in chk.issues],
             "view_count": chk.view_count}, ensure_ascii=False, indent=2)
        if ai_md:
            raw = (raw or "") + "\n\n" + ai_md
        ar = AnalysisResult(spec=spec, drawing_type="part", notes=chk.notes,
                            raw=raw, uncertain=uncertain, blocking=blocking,
                            steps=steps, audit=audit)
        ar.views = list(chk.views or [])
        ar.ocr_text = ocr_text
        ar.views_count = int(chk.view_count)
        p("📋 " + str(audit.get("summary") or "本地检查完成"))
        for b in blocking:
            p("⛔ 不允许直接建模：" + b)
        return ar

    @staticmethod
    def _compare_spec_ai(rule, ai) -> List[str]:
        """规则 spec 与模型 spec 的总体尺寸/特征数差异（人可读）。"""
        out = []
        for name, a, b in (("总长", rule.length, ai.length),
                           ("总宽", rule.width, ai.width),
                           ("总厚", rule.thickness, ai.thickness)):
            if a and b and abs(a - b) > max(0.01 * max(a, b), 0.3):
                out.append(f"{name}不一致：规则 {a:.2f} / 模型 {b:.2f} mm")
        if rule.base_kind != ai.base_kind:
            out.append(f"基体形式不一致：规则 {rule.base_kind} / 模型 {ai.base_kind}")
        n1, n2 = len(rule.build_features()), len(ai.build_features())
        if n1 != n2:
            out.append(f"可切特征数不一致：规则 {n1} / 模型 {n2}")
        return out

    def analyze_cad(self, files: Sequence[str], engine: str = "NX",
                    progress: Optional[Log] = None) -> AnalysisResult:
        """读 CAD 文件（STEP/IGES/DXF/PRT/SLDPRT）→ PartSpec → 建模步骤。

        与识图完全分开：几何来自文件本身，不调用视觉模型。
        """
        p = progress or self.log
        from .cad_input import import_cad, is_cad_file, kind_of
        cand = [f for f in files if is_cad_file(f)]
        if not cand:
            raise RuntimeError("没有可读取的 CAD 文件（支持 STEP/STP、IGES/IGS、DXF、PRT、SLDPRT）")
        path = cand[0]
        p(f"读取 CAD 文件：{Path(path).name}（{kind_of(path)}）· 引擎 {engine}")
        out_dir = Path(self.s.output_dir) / "_cad_import"
        res = import_cad(path, engine=engine, settings=self.s,
                         out_dir=str(out_dir), log=p)
        spec = res.spec
        try:
            # CAD 导入也必须遵守“缺数据不猜”：这里不能用默认 100×60×12
            # 或默认厚度把文件缺失的字段补齐，否则审计会被洗白。
            _warn = spec.validate(repair_dims=False)
            for _w in _warn:
                p(f"⚠ {_w}")
        except Exception as _e:
            p(f"参数规范化跳过（{_e}）")
        # 图纸审计同样适用于 CAD 文件导入。关键尺寸缺失（尤其 DXF 的厚度）、
        # 特征越界、功能不支持等必须保持阻断；只有纯提示项才允许降级为 review。
        _cad_audit: dict = {}
        try:
            from .drawing_audit import audit_spec as _audit_spec
            _cad_audit = _audit_spec(
                spec, uncertain=res.uncertain, blocking=res.blocking,
                drawing_type="cad", notes=f"CAD 文件：{Path(path).name}",
                check_steps=False).to_dict()
            _hard = [it for it in (_cad_audit.get("issues") or [])
                     if it.get("severity") == "error"
                     and it.get("category") != "support"]
            _critical_codes = {"THICKNESS", "BASE_DIM_MISSING", "DIAMETER",
                               "FEATURE_OUT_OF_STOCK", "FEATURE_AS_BLANK",
                               "UNSUPPORTED_GEOMETRY", "APPROXIMATED"}
            _critical = bool(res.blocking) or any(
                str(it.get("code") or "") in _critical_codes for it in _hard)
            if str(_cad_audit.get("status")) == "invalid" and not _hard and not _critical:
                _cad_audit["status"] = "review"
                _cad_audit["can_build"] = True
                _cad_audit["summary"] = ("CAD 文件已成功解析；下列提示项请核对后再建模。"
                                         + str(_cad_audit.get("summary") or ""))
            elif str(_cad_audit.get("status")) == "invalid":
                _cad_audit["can_build"] = False
            p("📋 " + str(_cad_audit.get("summary") or ""))
        except Exception as _e:
            p(f"CAD 图纸审计跳过（{_e}）")
        steps = list(res.steps or [])
        if not steps:
            try:
                steps = spec_to_steps(spec)
            except Exception as _e:
                p(f"步骤拆分失败：{_e}")
        p(f"CAD 读取完成，拆成 {len(steps)} 个建模步骤：")
        for _i, _st in enumerate(steps, 1):
            p(f"  {_i}. 【{_st.op_name}】{_st.title()}"
              + (f"   · {_st.note}" if getattr(_st, "note", "") else ""))
        uncertain = list(res.uncertain or [])
        blocking = list(res.blocking or [])
        for _b in blocking:
            p(f"⛔ 不允许直接建模：{_b}")
        ar = AnalysisResult(spec=spec,
                            drawing_type="cad",
                            notes=f"CAD 文件：{Path(path).name}（{res.source_kind}）",
                            raw=json.dumps(res.to_dict(), ensure_ascii=False, indent=2),
                            uncertain=uncertain,
                            blocking=blocking,
                            steps=steps,
                            audit=_cad_audit)
        ar.cad_source = {
            "path": str(path),
            "kind": res.source_kind,
            "engine": engine,
            "bbox": list(res.bbox or []),
            "bbox_min": list(res.bbox_min or []),
            "bbox_max": list(res.bbox_max or []),
            "cylinders": int(len(res.cylinders or [])),
            "volume": float(res.volume or 0.0),
            "converted_step": res.converted_step or "",
        }
        return ar

    # ------------------------------------------------------------------ step 2
    def build(self, spec: PartSpec, targets: Sequence[str], out_dir: str,
              progress: Optional[Log] = None) -> List[BuildResult]:
        p = progress or self.log
        try:
            spec.validate()          # 规范化（cr/fillet 互斥、越界剔除…）
        except Exception:
            pass
        _apply_ignored(spec, p)      # 直接调用 build 的路径（工具/脚本）也要一致
        results: List[BuildResult] = []
        for t in targets:
            adapter = make_adapter(t, self.s)
            name = CAD_NAMES.get(t, t)
            p(f"—— {name} 开始建模 ——")
            sub = Path(out_dir) / t
            r = adapter.build(spec, str(sub), p)
            try:
                from .stepcheck import check as step_check, summary_text
                if r.step_path and os.path.exists(r.step_path) and (r.step_path.lower().endswith((".step", ".stp"))):
                    chk = step_check(spec, r.step_path)
                    r.details["dim_check"] = chk
                    p("尺寸核对：" + summary_text(chk).replace("\n", " | "))
                    if not chk["ok"]:
                        p("⚠ 尺寸核对未通过")
            except Exception as _e:
                p(f"尺寸核对跳过：{_e}")
            try:
                from .calc import centroid as _cent
                if r.details.get("centroid") and r.ok:
                    ex, ey, ez = _cent(spec)
                    cx, cy, cz = r.details["centroid"]
                    d = max(abs(cx - ex), abs(cy - ey), abs(cz - ez))
                    r.details["centroid_check"] = {"expected": [round(ex,2), round(ey,2), round(ez,2)],
                                                   "cad": [round(cx,2), round(cy,2), round(cz,2)],
                                                   "diff": round(d,3), "ok": d < 0.3}
                    p(f"质心核对：偏差 {d:.3f} mm {'✔' if d < 0.3 else '✘'}")
            except Exception:
                pass
            try:
                from .calc import volume_reference as _vr
                _ref_v, _ref_kind = _vr(spec)
                r.details["ref_kind"] = _ref_kind
                r.details["ref_volume"] = round(_ref_v, 1)
                # 参考值和 CAD 是否"同源"：同源时 error_pct≈0 是恒等式，不是验证结论
                _same_src = bool(getattr(spec, "shaft_segments", None)
                                 or (spec.teeth > 0 and spec.module > 0)
                                 or (spec.sheet_t > 0 and spec.sheet_L > 0))
                r.details["reference_source"] = ("same_definition" if _same_src
                                                 else "independent")
            except Exception:
                _ref_kind = "?"
            _vsrc = str((getattr(r, "details", None) or {}).get("volume_source") or "")
            if (r.ok and r.expected and _vsrc != "measured"
                    and abs(r.volume - r.expected) <= 1e-6 * r.expected):
                p(f"⚠ {name}：体积是理论值回填，没有实测（请核对 CAD 侧 MassProperties）")
            if r.ok and r.expected:
                p(f"{name} 完成：CAD {r.volume:,.0f} / 参考 {r.expected:,.0f} mm³"
                  f"（{_ref_kind}），偏差 {r.error_pct:.3f}%")
            else:
                p(f"{name} 失败：{r.message}")
            results.append(r)
        return results

    # -------------------------------------------------------------- CAD verify
    def _judge_build(self, res: PipelineResult, spec: PartSpec):
        """统一判定：任一「非 unsupported」后端必须成功，外形核对必须通过，剔除特征算失败。"""
        _supported = [b for b in res.builds if not getattr(b, "unsupported", False)]
        build_ok = bool(_supported) and all(b.ok for b in _supported)
        feats_failed = []
        for b in res.builds:
            for name in (getattr(b, "failures", None) or []):
                feats_failed.append(f"{b.cad}：{name}")
            # 外形量错（dim_check 失败）比体积偏差更严重：体积能被平均掩盖，外形不会
            _chk = (b.details or {}).get("dim_check")
            if _chk and not _chk.get("ok", True):
                build_ok = False
                _bad = "；".join(_chk.get("issues") or [])[:160]
                feats_failed.append(f"{b.cad}：外形/尺寸核对未通过（{_bad}）")
        dropped = list(getattr(spec, "dropped_features", None) or [])
        if dropped:
            build_ok = False
            for dmsg in dropped:
                feats_failed.append(f"识图：{dmsg}")
        return build_ok, feats_failed

    def run_spec(self, spec: PartSpec, targets: Sequence[str], out_dir: str,
                 progress: Optional[Log] = None,
                 analysis: Optional[AnalysisResult] = None) -> PipelineResult:
        """用给定参数跑建模链路（门禁 → 建模 → 落盘）。

        analysis 不为空时复用整份图纸扫描阶段的识别/审计/steps，避免同一张图
        在“识别”和“建模”之间二次识别产生不同结果。此时 spec 以 analysis.spec 为准。
        """
        t0 = time.time()
        p = progress or self.log
        res = PipelineResult(out_dir=out_dir)
        try:
            if analysis is not None:
                if getattr(analysis, "spec", None) is None:
                    raise ValueError("复用识别结果时 analysis.spec 为空")
                spec = analysis.spec
                _ad = dict(getattr(analysis, "audit", {}) or {})
                _summary = str(_ad.get("summary") or "图纸审计（复用批量识别结果）")
                p("📋 " + _summary)
                if not _ad.get("can_build", True):
                    res.checks = {"drawing": False, "build": False}
                    res.features_failed = ["图纸：" + str(_it.get("message"))
                                           for _it in (_ad.get("issues") or [])
                                           if _it.get("severity") == "error"]
                    res.message = _summary
                    res.analysis = analysis
                    raise _DrawingBlocked(res.message)
                spec.validate()
                _apply_ignored(spec, p)
                res.analysis = analysis
                res.steps = list(getattr(analysis, "steps", None) or [])
                if not res.steps:
                    res.steps = spec_to_steps(spec)
            else:
                # 直接给参数也不能绕过图纸门禁：缺尺寸 / 自相矛盾的 spec 一样停下。
                from .drawing_audit import audit_spec as _audit_spec
                _au = _audit_spec(spec, drawing_type="part", notes="直接给定参数（无图纸）",
                                  check_views=False)
                _ad = _au.to_dict()
                p("📋 " + _au.summary)
                if not _ad.get("can_build", True):
                    res.checks = {"drawing": False, "build": False}
                    res.features_failed = ["图纸：" + str(_it.get("message"))
                                           for _it in (_ad.get("issues") or [])
                                           if _it.get("severity") == "error"]
                    res.message = _au.summary
                    # 报告要能写出来：解析流程里 spec 可能是半成品，先挂上
                    res.analysis = AnalysisResult(spec=spec, drawing_type="part", parts=[],
                                                  notes="直接给定参数（无图纸）", audit=_ad)
                    raise _DrawingBlocked(res.message)
                spec.validate()
                _apply_ignored(spec, p)
                res.analysis = AnalysisResult(spec=spec, drawing_type="part", parts=[],
                                              notes="直接给定参数（无图纸）",
                                              audit=_ad)
                res.steps = spec_to_steps(spec)
            p(f"零件：{spec.part_name}（{spec.size_label}，"
              f"{len(spec.features)} 个特征）")
            res.builds = self.build(spec, targets, out_dir, progress=p)
            build_ok, feats_failed = self._judge_build(res, spec)
            res.checks = {"build": build_ok}
            res.features_failed = feats_failed
            res.ok = build_ok
            res.message = ("建模完成（外形/体积已核对）" if res.ok else "未完成：建模失败")
        except _DrawingBlocked as e:
            res.ok = False
            res.message = str(e)
            p("⛔ " + res.message)
        except Exception as e:
            res.message = f"流程中断：{e}"
            p(res.message)
        res.seconds = time.time() - t0
        try:
            res.report_json, res.report_md = self.write_report(res)
        except Exception as e:
            p(f"报告写入失败：{e}")
        return res

    def run(self, files: Sequence[str], targets: Sequence[str], out_dir: str,
            hint: str = "", progress: Optional[Log] = None,
            page: Optional[int] = None) -> PipelineResult:
        t0 = time.time()
        p = progress or self.log
        res = PipelineResult(out_dir=out_dir)
        try:
            analysis = self.analyze(files, hint=hint, progress=p, page=page)
            res.analysis = analysis
            spec = analysis.spec
            # ---- 图纸门禁：不合格 / 功能不支持的图纸，绝不送进 CAD。
            _ad = dict(getattr(analysis, "audit", None) or {})
            if _ad and not _ad.get("can_build", True):
                _st = str(_ad.get("status") or "")
                _is_support = _st == "unsupported"
                _errs = [str(_it.get("message") or "") for _it in (_ad.get("issues") or [])
                         if _it.get("severity") == "error"]
                if not _is_support:
                    _errs = [m for m in _errs
                             if "不支持" not in m] or _errs
                res.checks = {"drawing": False, "build": False}
                res.features_failed = [(("图纸（功能不支持）：" if _is_support else "图纸：") + m)
                                       for m in _errs if m]
                res.message = str(_ad.get("summary") or "图纸不合格，不能直接建模")
                for _f in res.features_failed[:8]:
                    p("  [图纸问题] " + _f)
                raise _DrawingBlocked(res.message)
            # 贴边误读的特征只在 CAD 侧被 build_features() 排除，必须在进 CAD 之前
            # 对 spec.features 本身落实剔除，避免报告与实体分裂。
            _apply_ignored(spec, p)
            res.steps = list(getattr(analysis, "steps", None) or []) or spec_to_steps(spec)
            p(f"零件：{spec.part_name}（{spec.size_label}，"
              f"{len(spec.features)} 个特征）")
            res.builds = self.build(spec, targets, out_dir, progress=p)
            # 发布门禁：所有「非 unsupported」的后端都必须建成，且外形核对通过。
            build_ok, feats_failed = self._judge_build(res, spec)
            res.checks = {"build": build_ok}
            res.features_failed = feats_failed
            res.ok = build_ok
            if res.ok:
                res.message = "建模完成（外形/体积已核对）"
            else:
                res.message = "未完成：建模失败"
                for nm in feats_failed[:5]:
                    p(f"  [特征未切除] {nm}")
        except _DrawingBlocked as e:
            res.ok = False
            res.message = str(e)
            p("⛔ " + res.message)
        except Exception as e:
            res.message = f"流程中断：{e}"
            p(res.message)
        res.seconds = time.time() - t0
        try:
            res.report_json, res.report_md = self.write_report(res)
        except Exception as e:
            p(f"报告写入失败：{e}")
        return res

    # ------------------------------------------------------------------ report
    def write_report(self, res: PipelineResult) -> tuple[str, str]:
        out = Path(res.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        spec = res.analysis.spec if res.analysis else None
        data = {
            "part": spec.to_dict() if spec else None,
            "drawing_type": res.analysis.drawing_type if res.analysis else "",
            "page": ({"index": getattr(res.analysis, "page_index", 0) + 1,
                      "count": getattr(res.analysis, "page_count", 1),
                      "type": getattr(res.analysis, "page_type", ""),
                      "title": getattr(res.analysis, "page_title", "")}
                     if res.analysis else None),
            "uncertain": getattr(res.analysis, "uncertain", []) if res.analysis else [],
            "drawing_audit": (getattr(res.analysis, "audit", None) or {}) if res.analysis else {},
            "parts": res.analysis.parts if res.analysis else [],
            "builds": [b.to_dict() for b in res.builds],
            # 明确不可用/不支持的后端单独列出，避免报告把它当成红色失败或 0 体积。
            "unsupported": [
                {"cad": b.cad, "reason": b.unsupported_reason}
                for b in res.builds if getattr(b, "unsupported", False)
            ],
            "checks": getattr(res, "checks", None),
            # 未切除 / 被识图剔除的清单必须落到 json，否则只看 report.md 的对勾
            # 会以为「全部完成」；框外核对时这两项是最先要看的。
            "features_failed": list(getattr(res, "features_failed", None) or []),
            "dropped_features": list(getattr(spec, "dropped_features", None) or []),
            "steps": [s.to_dict() for s in res.steps],
            "conflicts": (spec.conflicts().get("warnings", []) if spec is not None else []),
            "features_total": (len(spec.features) if spec is not None else 0),
            "features_built": (len(spec.build_features()) if spec is not None else 0),
            "features_skipped_duplicate": (len(spec.conflicts().get("skip", [])) if spec is not None else 0),
            "features_ignored": (len(spec.conflicts().get("ignore", [])) if spec is not None else 0),
            "seconds": round(res.seconds, 1),
            "message": res.message,
        }
        jp = out / "result.json"
        jp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        dim_rows = []
        for b in res.builds:
            chk = (b.details or {}).get("dim_check")
            if chk:
                for name, d in chk.get("dims", {}).items():
                    dim_rows.append(f"| {CAD_NAMES.get(b.cad, b.cad)} | {name} | {d['want']:.2f} | {d['got']:.2f} | "
                                    f"{'✅' if d['ok'] else '❌'} |")
                holes = [h for h in chk.get("holes", [])]
                if holes:
                    okn = sum(1 for h in holes if h.get("ok"))
                    dim_rows.append(f"| {CAD_NAMES.get(b.cad, b.cad)} | 孔位 | {len(holes)} | {okn} | "
                                    f"{'✅' if okn == len(holes) else '❌'} |")
        rows = []
        _unsup = [b for b in res.builds if getattr(b, "unsupported", False)]
        unsup_txt = ""
        if _unsup:
            unsup_txt = ("> ⏸️ 以下后端明确不可用/不支持，未参与判定：" +
                         "；".join(f"{CAD_NAMES.get(b.cad, b.cad)}：{b.unsupported_reason or '原因未记录'}"
                                  for b in _unsup) + "\n")
        for b in res.builds:
            _status = ("⏸️ 不支持" if getattr(b, "unsupported", False)
                       else ("✅" if b.ok else "❌"))
            if getattr(b, "unsupported", False):
                rows.append(f"| {CAD_NAMES.get(b.cad, b.cad)} | {_status} | - | - | - | {b.elapsed:.1f}s |")
            else:
                rows.append(f"| {CAD_NAMES.get(b.cad, b.cad)} | {_status} | "
                            f"{b.volume:,.0f} | {b.expected:,.0f} | {b.error_pct:.2f}% | {b.elapsed:.1f}s |")
        feats = "\n".join(f"- {f.type} @({f.x:g},{f.y:g}) "
                          f"{('Ø%g' % f.diameter) if f.type == 'hole' else ('%g×%g×%g' % (f.length, f.width, f.depth))}"
                          f"{' 通' if f.through else ''}" for f in (spec.features if spec else []))
        dim_txt = "\n".join(dim_rows) or "| - | - | - | - | - |"
        sheet_txt = ""
        if spec is not None and getattr(spec, "sheet_t", 0) > 0:
            from .sheet import unfold_size
            bl, bw = unfold_size(spec.sheet_L, spec.sheet_W, spec.sheet_t,
                                 spec.flange_h, spec.bend_r)
            sheet_txt = (f"## 钣金展开\n- 下料尺寸：{bl:.2f} × {bw:.2f} mm（K=0.44）\n"
                         f"- 板厚 {spec.sheet_t:g} / 折弯 R{spec.bend_r:g} / 折边高 {spec.flange_h:g}\n")
        step_txt = "\n".join(f"{i+1}. {s.title()}" for i, s in enumerate(res.steps)) or "- 无"
        _ff = getattr(res, "features_failed", None) or []
        _drop = list(getattr(spec, "dropped_features", None) or [])
        # 两类失败分开写：切不出来 vs 识图判定无效被剔除。混在一起会让人以为
        # 「删掉特征」也算成功——实测剔除后理论体积同步变小，自比恒为 0%。
        cut_bad = [x for x in _ff if not str(x).startswith("识图：")]
        seen = []
        for x in _drop:
            if x not in seen:
                seen.append(x)
        _blocks = []
        if cut_bad:
            _blocks.append("**CAD 里切不出来：**\n"
                           + "\n".join("- \u274c " + str(x) for x in cut_bad))
        if seen:
            _blocks.append("**识图判定无效，已剔除（零件不完整，必须人工确认图纸）：**\n"
                           + "\n".join("- \u26d4 " + str(x) for x in seen))
        if _blocks:
            feat_fail_txt = "\n\n".join(_blocks) + "\n\n"
            feat_fail_txt += ("> 模型不完整，不能作为加工依据。"
                              "切不出来的多见于相切 / 零厚度薄壁 / 重复轮廓；"
                              "被剔除的多见于孔心落在别的特征壁面上、或落在毛坯之外。")
        else:
            feat_fail_txt = "- 无（所有特征均已切除成功）"
        _conf_warn = (spec.conflicts().get("warnings", []) if spec is not None else [])
        if _conf_warn:
            conf_txt = ("\n\n".join(f"- ⚠ {x}" for x in _conf_warn) +
                        "\n\n> 以上是图纸层面的几何冲突（薄壁 / 重复轮廓）。软件已按括号里的方式处理，"
                        "**请先确认图纸**；若要按原图重做，请修改对应特征尺寸后重新建模。")
        else:
            conf_txt = "- 无"
        audit_txt = ""
        _aud = (getattr(res.analysis, "audit", None) or {}) if res.analysis else {}
        if _aud:
            try:
                from .drawing_audit import DrawingAudit as _DA, AuditIssue as _AI
                _o = _DA(status=str(_aud.get("status") or "ok"),
                         can_build=bool(_aud.get("can_build", True)),
                         completeness=float(_aud.get("completeness", 1.0) or 1.0),
                         summary=str(_aud.get("summary") or ""),
                         issues=[_AI(**i) for i in (_aud.get("issues") or [])],
                         checks=list(_aud.get("checks") or []))
                audit_txt = _o.markdown() + "\n"
            except Exception:
                audit_txt = ""
        md = f"""# 工程助手 · 建模报告

- 零件：**{spec.part_name if spec else '-'}**
- 外形：{spec.size_label} mm（{'圆盘' if spec and spec.base_kind == 'round' else '板件'}）
- 材料：{spec.material if spec else '-'}
- 图纸类型：{res.analysis.drawing_type if res.analysis else '-'}

{audit_txt}
## ⚠ 图纸冲突（请先看这里）
{conf_txt}

## 建模步骤
{step_txt}

## 特征
{feats or '- 无'}

## 建模结果
| CAD | 状态 | CAD 实测 mm³ | 参考体积 mm³ | CAD vs 参考 | 耗时 |
|---|---|---|---|---|---|
{chr(10).join(rows)}

{unsup_txt}

{sheet_txt}

## 尺寸核对（从 CAD 导出的 STEP 读取实际几何）
| CAD | 项目 | 图纸 | CAD 实测 | 结果 |
|---|---|---|---|---|
{dim_txt}

## 未切除的特征
{feat_fail_txt}

> 由「工程助手」自动生成 · {time.strftime('%Y-%m-%d %H:%M')}
"""
        mp = out / "report.md"
        mp.write_text(md, encoding="utf-8")
        res.report_json, res.report_md = str(jp), str(mp)
        return str(jp), str(mp)

def _apply_ignored(spec, p=None) -> int:
    """把 conflict 判定为「贴边误读」的特征真正从 spec.features 里摘掉，返回摘除数。
    conflict.analyze returns ignore indices; CAD build_features() skips them.
    Volume and geometry checks must use the same feature set, otherwise the
    report can claim a feature was dropped while still accounting for it.
    """
    # CAD 导入的 spec（origin="cad"）里特征是从真实几何读出来的，"越界/贴壁"
    # 只说明毛坯尺寸估错，不能当识图误读删掉——与 build_features() 的口径保持一致。
    if str(getattr(spec, "origin", "vision")).lower() == "cad":
        return 0
    try:
        bad = set(spec.conflicts().get("ignore") or [])
    except Exception:
        return 0
    if not bad:
        return 0
    kept = [f for i, f in enumerate(spec.features) if i not in bad]
    n = len(spec.features) - len(kept)
    if n <= 0:
        return 0
    spec.features = kept
    if p:
        p(f"已摘除 {n} 个贴边误读特征：建模与加工都不再使用")
    return n
