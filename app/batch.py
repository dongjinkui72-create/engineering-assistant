"""Batch drawing workflow used by the desktop UI.

One import -> discover every part -> audit every part -> one CAD choice ->
build every usable part sequentially.  This is deliberately a product-level
workflow, not a collection of background helper scripts.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from .config import Settings
from .pipeline import Pipeline, PipelineResult
from .pdf_parts import DiscoveredPart, _safe_name, discover_parts
from .cad_input import is_cad_file


@dataclass
class BatchPart:
    part_no: str
    name: str
    page: int
    image: str
    rect: list = field(default_factory=list)
    note: str = ""
    status: str = "pending"      # pending/analyzing/ready/blocked/unsupported/built/failed
    reason: str = ""
    confidence: float = 0.0
    analysis: object = None
    build: object = None

    @classmethod
    def from_discovered(cls, p: DiscoveredPart) -> "BatchPart":
        return cls(part_no=p.part_no, name=p.name, page=p.page, image=p.image,
                   rect=list(p.rect or []), note=p.note, status=p.status,
                   reason=p.reason)

    def summary(self) -> dict:
        spec = getattr(self.analysis, "spec", None) if self.analysis is not None else None
        b = self.build
        builds = []
        if isinstance(b, PipelineResult):
            for r in (b.builds or []):
                builds.append({
                    "cad": r.cad,
                    "ok": bool(r.ok),
                    "volume": round(float(r.volume or 0), 3),
                    "expected": round(float(r.expected or 0), 3),
                    "error_pct": round(float(getattr(r, "error_pct", 0) or 0), 4),
                    "part_path": r.part_path,
                    "step_path": r.step_path,
                    "message": r.message,
                    "dim_ok": (r.details or {}).get("dim_check", {}).get("ok"),
                })
        return {
            "part_no": self.part_no,
            "name": self.name,
            "page": self.page,
            "image": self.image,
            "rect": list(self.rect or []),
            "status": self.status,
            "reason": self.reason,
            "confidence": round(float(self.confidence or 0), 3),
            "spec": spec.to_dict() if spec is not None else None,
            "audit": dict(getattr(self.analysis, "audit", {}) or {}) if self.analysis is not None else {},
            "build_ok": bool(getattr(b, "ok", False)) if b is not None else False,
            "build_message": str(getattr(b, "message", "") or ""),
            "builds": builds,
        }


@dataclass
class BatchResult:
    stage: str
    parts: List[BatchPart]
    message: str = ""
    report_json: str = ""
    report_md: str = ""


class BatchRunner:
    def __init__(self, settings: Settings,
                 progress: Optional[Callable[[str], None]] = None):
        self.s = settings
        self.p = progress or (lambda _m: None)

    def _work_dir(self) -> Path:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        root = Path(self.s.output_dir) / "_batch" / stamp
        root.mkdir(parents=True, exist_ok=True)
        return root

    def scan(self, files: Sequence[str], verify: bool = True) -> BatchResult:
        root = self._work_dir()
        self.p(f"开始处理整份图纸，共 {len(files)} 个文件")
        discovered = discover_parts(files, str(root / "discovery"), progress=self.p)
        parts = [BatchPart.from_discovered(x) for x in discovered]
        n = len(parts)
        for i, part in enumerate(parts, 1):
            part.status = "analyzing"
            self.p("")
            self.p(f"===== 识别 {i}/{n}：件 {part.part_no} {part.name}（第 {part.page} 页）=====")
            try:
                pipe = Pipeline(self.s, self.p)
                src = Path(part.image)
                if src.suffix.lower() == ".dwg":
                    raise RuntimeError("DWG 暂不支持直接解析，请在 CAD 中另存为 DXF 后导入")
                if is_cad_file(str(src)):
                    # CAD 图纸（DXF/STEP/IGES）走几何解析，不送视觉模型。
                    res = pipe.analyze_cad([str(src)], engine="NX", progress=self.p)
                else:
                    # 整份图纸扫描默认走本地识图，不依赖任何外部视觉 API。
                    res = pipe.analyze_local([part.image], progress=self.p,
                                             require_three_views=True)
                part.analysis = res
                part.confidence = float(getattr(res.spec, "confidence", 0) or 0)
                if getattr(res.spec, "part_name", ""):
                    name = str(res.spec.part_name).strip()
                    if name and name not in ("零件", "未命名零件"):
                        part.name = name
                audit = dict(getattr(res, "audit", {}) or {})
                can_build = bool(audit.get("can_build", True))
                blocking = list(getattr(res, "blocking", []) or [])
                if not can_build:
                    status = str(audit.get("status") or "")
                    part.status = "unsupported" if status == "unsupported" else "blocked"
                    errs = [str(x.get("message") or "") for x in (audit.get("issues") or [])
                            if x.get("severity") == "error"]
                    part.reason = "；".join((errs or blocking or [audit.get("summary") or "图纸不可用"]))[:800]
                elif blocking:
                    part.status = "blocked"
                    part.reason = "；".join(str(x) for x in blocking)[:800]
                else:
                    part.status = "ready"
                    part.reason = str(audit.get("summary") or "图纸完整，可以建模")
                self.p(f"状态：{_STATUS_CN.get(part.status, part.status)} · {part.reason}")
            except Exception as e:
                msg = str(e)
                if msg.startswith("本地识图未能得到可信参数"):
                    # 这不是程序崩溃，而是图纸本身不可直接建模；必须归入
                    # blocked，让批量门禁和用户都能清楚区分。
                    part.status = "blocked"
                    part.reason = msg.split("：", 1)[-1][:800]
                    self.p("⛔ 图纸不可用：" + part.reason)
                else:
                    part.status = "failed"
                    part.reason = f"识别中断：{msg}"
                    self.p("❌ " + part.reason)
        report_json, report_md = self._write_report(root, parts, stage="scan")
        ready = sum(1 for x in parts if x.status == "ready")
        bad = n - ready
        msg = (f"整份图纸识别完成：共 {n} 个零件，可建模 {ready} 个，"
               f"需修正或不支持 {bad} 个。\n汇总报告：{report_md}")
        self.p(msg)
        return BatchResult("scan", parts, msg, report_json, report_md)

    def build_all(self, parts: Sequence[BatchPart], target: str,
                  verify: bool = True) -> BatchResult:
        root = self._work_dir()
        ready = [x for x in parts if x.status == "ready" and x.analysis is not None]
        self.p(f"开始一键建模：{len(ready)} 个可用零件；CAD = {target}；同一时间只启动这一款 CAD")
        for i, part in enumerate(ready, 1):
            self.p("")
            self.p(f"===== 建模 {i}/{len(ready)}：件 {part.part_no} {part.name} =====")
            try:
                out = root / "models" / f"part{_safe_name(part.part_no)}_{_safe_name(part.name)}"
                pipe = Pipeline(self.s, self.p)
                res = pipe.run_spec(part.analysis.spec, [target], str(out), progress=self.p,
                                    analysis=part.analysis)
                part.build = res
                part.status = "built" if res.ok else "failed"
                part.reason = res.message
            except Exception as e:
                part.status = "failed"
                part.reason = f"建模中断：{e}"
                self.p("❌ " + part.reason)
        report_json, report_md = self._write_report(root, parts, stage="build")
        built = sum(1 for x in parts if x.status == "built")
        msg = (f"一键建模完成：成功 {built} 个，失败/未建 "
               f"{len(parts)-built} 个。\n输出目录：{root}\n汇总报告：{report_md}")
        self.p(msg)
        return BatchResult("build", list(parts), msg, report_json, report_md)

    def _write_report(self, root: Path, parts: Sequence[BatchPart], stage: str):
        data = [x.summary() for x in parts]
        js = root / "batch_manifest.json"
        md = root / "batch_report.md"
        js.write_text(json.dumps({"stage": stage, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                                  "parts": data}, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = ["# 整份图纸自动处理报告", "", f"- 阶段：{stage}", f"- 零件数：{len(parts)}", ""]
        lines += ["| 件号 | 名称 | 页 | 状态 | 置信度 | 说明 |", "|---|---|---:|---|---:|---|"]
        for p in parts:
            lines.append(f"| {p.part_no} | {p.name} | {p.page} | "
                         f"{_STATUS_CN.get(p.status, p.status)} | {p.confidence:.0%} | {p.reason[:180]} |")
        lines += ["", "## 输出文件", ""]
        for p in parts:
            b = p.build
            if isinstance(b, PipelineResult):
                for r in (b.builds or []):
                    if r.part_path:
                        lines.append(f"- 件 {p.part_no} {p.name}：`{r.part_path}`")
                    if r.step_path:
                        lines.append(f"- 件 {p.part_no} {p.name}：`{r.step_path}`")
        md.write_text("\n".join(lines), encoding="utf-8")
        return str(js), str(md)


_STATUS_CN = {
    "pending": "待识别", "analyzing": "识别中", "ready": "可建模",
    "blocked": "图纸不合格", "unsupported": "暂不支持", "built": "已建模",
    "failed": "失败",
}
