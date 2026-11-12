"""End-to-end pipeline: drawing -> PartSpec -> CAD model(s) -> NC program."""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from .cad.base import BuildResult
from .cam.gcode import CamOptions, CamResult, generate as generate_cam
from .cam.plan import ai_plan, builtin_plan
from .calc import part_volume
from .config import Settings
from .models import PartSpec
from .steps import Step, spec_to_steps
from .vision import AnalysisResult, DrawingAnalyzer

Log = Callable[[str], None]


def _noop(msg: str) -> None:
    pass


CAD_NAMES = {"NX": "UG/NX", "SolidWorks": "SolidWorks", "CATIA": "CATIA"}


def make_adapter(name: str, s: Settings):
    if name == "NX":
        from .cad.nx import NxAdapter
        return NxAdapter(s.nx_root)
    if name == "SolidWorks":
        from .cad.sw import SwAdapter
        return SwAdapter(s.sw_template)
    if name == "CATIA":
        from .cad.catia import CatiaAdapter
        return CatiaAdapter(s.catia_exe, s.catia_env_dir, s.catia_env_name)
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
    cam: Optional[CamResult] = None
    verify: object = None
    checks: dict = field(default_factory=dict)
    # 「CAD：特征名」形式的未切除清单，给报告和 UI 用，不能只是打日志。
    features_failed: list = field(default_factory=list)
    steps: list = field(default_factory=list)
    out_dir: str = ""
    report_json: str = ""
    report_md: str = ""
    message: str = ""
    seconds: float = 0.0


class Pipeline:
    def __init__(self, settings: Settings, log: Log = _noop):
        self.s = settings
        self.log = log

    # ------------------------------------------------------------------ step 1
    def analyze(self, files: Sequence[str], hint: str = "", verify: bool = True,
                progress: Optional[Log] = None, page: Optional[int] = None) -> AnalysisResult:
        p = progress or self.log
        work = Path(self.s.output_dir) / "_vision"
        analyzer = DrawingAnalyzer(self.s, self.log)
        res = analyzer.analyze(files, str(work), hint=hint, on_progress=p, page=page)
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
            res.spec = analyzer.verify(imgs, res.spec, hint=hint, on_progress=p)
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

    # -------------------------------------------------------------- NC verify
    @staticmethod
    def verify_program(spec: PartSpec, nc_path: str, model_volume: float | None = None,
                       cad_volumes=None):
        """按零件类型选回灌验证器：三轴铣用 Z-map，车削/齿轮/钣金用各自工艺比对。"""
        if getattr(spec, "revolve_profile", None):
            from .cam.verify2 import verify_revolve_turning
            return verify_revolve_turning(spec, nc_path)
        if getattr(spec, "shaft_segments", None):
            from .cam.verify2 import verify_turning
            return verify_turning(spec, nc_path)
        if getattr(spec, "teeth", 0) > 0 and getattr(spec, "module", 0) > 0:
            from .cam.verify2 import verify_gear
            return verify_gear(spec, nc_path)
        if getattr(spec, "sheet_t", 0) > 0 and getattr(spec, "sheet_L", 0) > 0:
            from .cam.verify2 import verify_sheet
            return verify_sheet(spec, nc_path)
        from .cam.verify import verify_nc
        return verify_nc(spec, nc_path, model_volume=model_volume, cad_volumes=cad_volumes)

    # ------------------------------------------------------------------ step 3
    def cam(self, spec: PartSpec, out_dir: str,
            progress: Optional[Log] = None, post: str = "GENERIC",
            use_ai: bool = False) -> CamResult:
        p = progress or self.log
        _apply_ignored(spec, p)      # 加工也只看"真正要切的特征"，避免报告与 NC 打架
        if spec.sheet_t > 0 and spec.sheet_L > 0:
            from .cam.others import generate_sheet_program
            p("生成钣金下料/折弯程序…")
            r = generate_sheet_program(spec, out_dir, post)
            p(f"{r.message}：{Path(r.nc_path).name}")
            return r
        if spec.teeth > 0 and spec.module > 0:
            from .cam.others import generate_gear_card
            p("生成齿轮程序与工艺卡…")
            r = generate_gear_card(spec, out_dir, post)
            p(f"{r.message}：{Path(r.nc_path).name}")
            return r
        if spec.shaft_segments:
            from .cam.turning import generate_turning
            p("生成车削程序…")
            r = generate_turning(spec, out_dir, post=post)
            p(f"{r.message}：{Path(r.nc_path).name}")
            return r
        if getattr(spec, "revolve_profile", None):
            from .cam.revolve_turn import generate_revolve_turning
            p("生成回转母线车削程序…")
            r = generate_revolve_turning(spec, out_dir, post=post)
            p(f"{r.message}：{Path(r.nc_path).name}")
            return r
        
        p(f"生成加工程序（{post}）…")
        plan = ai_plan(spec, self.s, p, post) if use_ai else builtin_plan(spec, post)
        r = generate_cam(spec, out_dir, plan=plan, post=post)
        if r.ok:
            p(f"NC 程序完成：{Path(r.nc_path).name}（{r.lines} 行，约 {r.minutes:.0f} 分钟）")
        else:
            p(f"NC 程序失败：{r.message}")
        return r

    # ------------------------------------------------------------------ all
    def run_spec(self, spec: PartSpec, targets: Sequence[str], out_dir: str,
                 do_cam: bool = True, cam_post: str = "GENERIC",
                 progress: Optional[Log] = None) -> PipelineResult:
        """不经识图，直接用给定参数跑完整链路（建模 → 程序 → 回灌 → 门禁 → 落盘）。

        回归/自检走这条：结果会写 result.json / report.md，别人能照着复验。
        """
        t0 = time.time()
        p = progress or self.log
        res = PipelineResult(out_dir=out_dir)
        try:
            spec.validate()
            _apply_ignored(spec, p)
            res.analysis = AnalysisResult(spec=spec, drawing_type="part", parts=[],
                                          notes="直接给定参数（无图纸）")
            res.steps = spec_to_steps(spec)
            p(f"零件：{spec.part_name}（{spec.length:g}×{spec.width:g}×{spec.thickness:g}，"
              f"{len(spec.features)} 个特征）")
            res.builds = self.build(spec, targets, out_dir, progress=p)
            if do_cam:
                res.cam = self.cam(spec, out_dir, progress=p, post=cam_post)
                if res.cam and res.cam.nc_path:
                    try:
                        _mv = res.builds[0].expected if res.builds else None
                        _cads = [b.volume for b in res.builds if b.ok and b.volume > 0]
                        try:
                            vrep = self.verify_program(spec, res.cam.nc_path,
                                                       model_volume=_mv, cad_volumes=_cads)
                        except TypeError:
                            _cads = [b.volume for b in res.builds if b.ok and b.volume > 0]
                        _mv = res.builds[0].expected if res.builds else None
                        vrep = self.verify_program(spec, res.cam.nc_path,
                                                   model_volume=_mv, cad_volumes=_cads)
                        (Path(out_dir) / "NC验证.md").write_text(vrep.markdown(), encoding="utf-8")
                        res.verify = vrep
                        p("")
                        p(f"NC 验证：{'通过' if vrep.ok else '未通过'} — {vrep.summary}")
                        for pr in vrep.problems:
                            p(f"  [问题] {pr}")
                    except Exception as e:
                        p(f"NC 验证失败：{e}")
            # 明确"该后端不支持"的不参与判定（否则一个后端做不了会把整个零件判死），
            # 但至少要有一个后端建成，且没有任何"失败"的后端。
            _supported = [b for b in res.builds if not getattr(b, "unsupported", False)]
            build_ok = bool(_supported) and all(b.ok for b in _supported)
            # 生成了 NC 不等于能加工：硬限制（翻面/侧铣/扎穿）时 res.cam.ok=False，
            # 只看 nc_path 会把「明确不可加工」判成完成（实测抽壳开口朝下）。
            cam_ok = bool(res.cam and getattr(res.cam, "ok", False)
                          and getattr(res.cam, "nc_path", ""))
            verify_ok = bool(res.verify is not None and getattr(res.verify, "ok", False))
            feats_failed = []
            for _u in (getattr(res.cam, "unplanned", None) or []):
                feats_failed.append(f"CAM：{_u}")
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
            res.checks = {"build": build_ok, "cam": cam_ok, "verify": verify_ok}
            res.features_failed = feats_failed
            res.ok = build_ok and cam_ok and verify_ok
            _cam_label = ("不可加工（工艺限制）" if (res.cam and getattr(res.cam, "nc_path", "")
                                                   and not getattr(res.cam, "ok", True))
                          else "没有生成程序")
            res.message = ("全部完成（建模 + 程序 + 验证通过）" if res.ok
                           else "未完成：" + "、".join(
                               [x for x, ok in (("建模失败", build_ok), (_cam_label, cam_ok),
                                                ("验证未通过", verify_ok)) if not ok]))
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
            hint: str = "", do_cam: bool = True, cam_post: str = "GENERIC",
            use_ai_cam: bool = False, progress: Optional[Log] = None,
            page: Optional[int] = None) -> PipelineResult:
        t0 = time.time()
        p = progress or self.log
        res = PipelineResult(out_dir=out_dir)
        try:
            analysis = self.analyze(files, hint=hint, progress=p, page=page)
            res.analysis = analysis
            spec = analysis.spec
            # 贴边误读的特征只在 CAD 侧被 build_features() 排除，CAM（gcode/plan/
            # steps）和 verify 都是直接遍历 spec.features，于是出现「报告写已剔除、
            # NC 里照样钻孔」的分裂（实测 box6：4 个 Ø9 判剔除，.nc 仍钻 Ø9×6）。
            # 必须在进 CAD/CAM 之前对 spec.features 本身落实剔除。
            _apply_ignored(spec, p)
            res.steps = list(getattr(analysis, "steps", None) or []) or spec_to_steps(spec)
            p(f"零件：{spec.part_name}（{spec.length:g}×{spec.width:g}×{spec.thickness:g}，"
              f"{len(spec.features)} 个特征）")
            res.builds = self.build(spec, targets, out_dir, progress=p)
            if do_cam:
                res.cam = self.cam(spec, out_dir, progress=p, post=cam_post, use_ai=use_ai_cam)
                if res.cam and res.cam.nc_path:
                    try:
                        vp = Path(out_dir) / "NC验证.md"
                        _mv = res.builds[0].expected if res.builds else None
                        _cads = [b.volume for b in res.builds if b.ok and b.volume > 0]
                        try:
                            vrep = self.verify_program(spec, res.cam.nc_path,
                                                       model_volume=_mv, cad_volumes=_cads)
                        except TypeError:
                            _cads = [b.volume for b in res.builds if b.ok and b.volume > 0]
                        _mv = res.builds[0].expected if res.builds else None
                        vrep = self.verify_program(spec, res.cam.nc_path,
                                                   model_volume=_mv, cad_volumes=_cads)
                        vp.write_text(vrep.markdown(), encoding="utf-8")
                        res.verify = vrep
                        p("")
                        p(f"NC 回灌验证：{'通过' if vrep.ok else '未通过'} — {vrep.summary}")
                        for pr in vrep.problems:
                            p(f"  [问题] {pr}")
                    except Exception as e:
                        p(f"NC 回灌验证失败：{e}")
            # 发布门禁：建模成功 **且** 出了程序 **且** 回灌验证通过，三者缺一不可。
            # 否则报告写「全部完成」就是骗人的。
            # 「任一成功」会让 NX 通过、SolidWorks 特征切失败却报「全部完成」，
            # 与用户的放行标准（每一条都要绿）冲突，所以要求全部 CAD 都成功。
            # 明确"该后端不支持"的不参与判定（否则一个后端做不了会把整个零件判死），
            # 但至少要有一个后端建成，且没有任何"失败"的后端。
            _supported = [b for b in res.builds if not getattr(b, "unsupported", False)]
            build_ok = bool(_supported) and all(b.ok for b in _supported)
            cam_ok = bool(res.cam and getattr(res.cam, "nc_path", ""))
            verify_ok = bool(res.verify is not None and getattr(res.verify, "ok", False))
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
            # 识图误读被剔除的特征也算「没做出来」。道理：剔除后理论体积同步变小，
            # 体积偏差恒为 0，删特征反而成了最容易的通过路径（实测 box3 剔 5 个特征，
            # 与未剔除时理论体积差 63,000 mm3 / 6.3%，但自比是 0%）。
            _spec = res.analysis.spec if getattr(res, "analysis", None) else None
            dropped = list(getattr(_spec, "dropped_features", None) or [])
            if dropped:
                build_ok = False
                for dmsg in dropped:
                    feats_failed.append(f"识图：{dmsg}")
            res.checks = {"build": build_ok, "cam": cam_ok, "verify": verify_ok}
            res.features_failed = feats_failed
            res.ok = build_ok and cam_ok and verify_ok
            if res.ok:
                res.message = "全部完成（建模 + 程序 + 回灌验证通过）"
            else:
                bad = []
                if not build_ok:
                    bad.append("建模失败")
                    for nm in feats_failed[:5]:
                        p(f"  [特征未切除] {nm}")
                if not cam_ok:
                    bad.append("没有生成加工程序")
                if not verify_ok:
                    bad.append("回灌验证未通过")
                res.message = "未完成：" + "、".join(bad)
                for pr in (getattr(res.verify, "problems", []) or [])[:5]:
                    p(f"  [验证问题] {pr}")
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
            "parts": res.analysis.parts if res.analysis else [],
            "builds": [b.to_dict() for b in res.builds],
            # 明确不可用/不支持的后端单独列出，避免报告把它当成红色失败或 0 体积。
            "unsupported": [
                {"cad": b.cad, "reason": b.unsupported_reason}
                for b in res.builds if getattr(b, "unsupported", False)
            ],
            "cam": res.cam.to_dict() if res.cam else None,
            "verify": res.verify.to_dict() if getattr(res, "verify", None) else None,
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
        md = f"""# 工程助手 · 建模报告

- 零件：**{spec.part_name if spec else '-'}**
- 外形：{spec.length:g} × {spec.width:g} × {spec.thickness:g} mm（{'圆盘' if spec and spec.base_kind == 'round' else '板件'}）
- 材料：{spec.material if spec else '-'}
- 图纸类型：{res.analysis.drawing_type if res.analysis else '-'}

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

## 加工程序
{(f"- 文件：{Path(res.cam.nc_path).name}（{res.cam.lines} 行）" + chr(10) + f"- 工序：{' / '.join(getattr(res.cam, 'operations', None) or getattr(res.cam, 'ops', []) or [])}" + chr(10) + f"- 预计加工时间：约 {res.cam.minutes:.0f} 分钟") if res.cam and res.cam.ok else "- 未生成"}

> 由「工程助手」自动生成 · {time.strftime('%Y-%m-%d %H:%M')}
"""
        mp = out / "report.md"
        mp.write_text(md, encoding="utf-8")
        res.report_json, res.report_md = str(jp), str(mp)
        return str(jp), str(mp)

def _apply_ignored(spec, p=None) -> int:
    """把 conflict 判定为「贴边误读」的特征真正从 spec.features 里摘掉，返回摘除数。

    conflict.analyze 只给出索引（res["ignore"]），CAD 侧 build_features() 会跳过，
    但 CAM（gcode/plan/steps）与 verify 都直接遍历 spec.features。两边不一致会产出
    自相矛盾的交付物：报告写「已剔除，不参与建模与加工」，NC 里却按原坐标钻了孔
    （实测 box6：4 个 Ø9 被剔除，箱座.nc 仍按 Ø9×6 钻）。
    dropped_features 的文案已由 conflict 写全，这里不重复追加。
    """
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
