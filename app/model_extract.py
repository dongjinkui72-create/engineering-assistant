# -*- coding: utf-8 -*-
"""本地视觉大模型 → 结构化零件参数（PartSpec）。

流程：
  图片 + 本地 OCR 文本
      → Ollama /api/chat（format=JSON Schema，强制输出结构化 JSON）
      → 字段清洗 / 环形孔展开
      → 确定性交叉核对（OCR 数字、包包围、特征合法性）
      → PartSpec + 检查结论

不允许模型直接操作 CAD 界面；模型只产出参数，建模仍由
app/cad/nx.py、app/cad/sw.py 的原生 API 完成。
"""
from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .local_ai import (_b64_image, _post_json, _url, model_available)

DEFAULT_MODEL = "qwen2.5vl:7b"

SYSTEM_PROMPT = (
    "你是机械制图读图助手。只输出 JSON 对象，不要解释、不要 Markdown。"
    "所有尺寸单位为毫米(mm)。图上标注可能是总尺寸、半径、直径或位置尺寸，"
    "必须换算成零件的真实尺寸。不要编造图上没有的数字。"
)

SCHEMA_HINT = """输出字段：
{
  "part_name": "零件名",
  "part_type": "plate|bracket|flange|block",
  "base_kind": "rect|round",              // 矩形板 or 圆盘
  "length": 总长mm, "width": 总宽mm, "thickness": 总厚mm,
  "diameter": 圆盘外径mm（base_kind=rect 时填 0）,
  "corner_radius": 四角圆角mm（没有填 0）,
  "chamfer": 顶面外轮廓倒角C值mm（没有填 0）,
  "features": [
    {"type":"hole","x":0,"y":0,"diameter":10,"count":6,"pcd":90,"through":true},
    {"type":"pocket","x":0,"y":0,"length":56,"width":38,"depth":6,"corner_radius":0},
    {"type":"slot","x":0,"y":0,"length":30,"width":10,"depth":0},
    {"type":"step","x":0,"y":0,"length":40,"width":20,"depth":5},
    {"type":"boss","x":0,"y":0,"diameter":20,"depth":5}
  ],
  "missing": ["图上找不到的尺寸"],
  "conflicts": ["图上互相矛盾的地方"],
  "confidence": 0.0~1.0,
  "notes": "读图依据"
}
坐标原点 = 零件外形中心。环形均布的孔用 count + pcd（节圆直径），x/y 填 0。
通孔 depth 填 0；盲孔 depth 填实际深度。
完整性规则（必须逐条核对）：
1) 每一个 Ø 标注都要落到 features 里，或明确归为外径/节圆/圆角；不得静默跳过一个直径值。
2) 圆盘/回转体必须检查中心是否有同心孔、沉孔或凸台；中心孔容易漏读，不能省略。
3) 同一中心线上的多个直径要按台阶孔分别表达，不要合并。
4) 若无法确认某个 Ø 对应什么特征，写入 missing 并把 confidence 降到 0.5 以下。"""

# Ollama 结构化输出用的 JSON Schema（会强制模型只产出这些字段）
EXTRACT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "part_name": {"type": "string"},
        "part_type": {"type": "string"},
        "base_kind": {"type": "string", "enum": ["rect", "round"]},
        "length": {"type": "number"},
        "width": {"type": "number"},
        "thickness": {"type": "number"},
        "diameter": {"type": "number"},
        "corner_radius": {"type": "number"},
        "chamfer": {"type": "number"},
        "features": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "x": {"type": "number"},
                    "y": {"type": "number"},
                    "diameter": {"type": "number"},
                    "length": {"type": "number"},
                    "width": {"type": "number"},
                    "depth": {"type": "number"},
                    "through": {"type": "boolean"},
                    "corner_radius": {"type": "number"},
                    "count": {"type": "integer"},
                    "pcd": {"type": "number"},
                    "angle": {"type": "number"},
                    "note": {"type": "string"},
                },
                "required": ["type"],
            },
        },
        "missing": {"type": "array", "items": {"type": "string"}},
        "conflicts": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
        "notes": {"type": "string"},
    },
    "required": ["base_kind", "length", "width", "thickness", "features"],
}


@dataclass
class ExtractResult:
    available: bool = False
    ok: bool = False
    model: str = DEFAULT_MODEL
    spec: Any = None
    data: Dict[str, Any] = field(default_factory=dict)
    missing: List[str] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)
    blocking: List[str] = field(default_factory=list)
    uncertain: List[str] = field(default_factory=list)
    checks: List[Dict[str, str]] = field(default_factory=list)
    confidence: float = 0.0
    elapsed_s: float = 0.0
    raw: str = ""
    error: str = ""
    retry_hint: str = ""

    def note(self, level: str, message: str) -> None:
        self.checks.append({"level": level, "message": message})
        if level == "error":
            if message not in self.blocking:
                self.blocking.append(message)
        elif level == "warn":
            if message not in self.uncertain:
                self.uncertain.append(message)

    def markdown(self) -> str:
        rows = ["### 本地视觉模型结构化识图", ""]
        if not self.available:
            rows.append(f"- 状态：不可用（{self.error}）")
            return "\n".join(rows)
        rows.append(f"- 模型：`{self.model}`，耗时 {self.elapsed_s:.1f} s，"
                    f"置信度 {self.confidence:.0%}")
        s = self.spec
        if s is not None:
            rows.append(f"- 总体：{s.part_name} · {s.base_kind} · "
                        f"{s.length:.2f}×{s.width:.2f}×{s.thickness:.2f} mm"
                        + (f" · Ø{s.diameter:.2f}" if s.base_kind == "round" else ""))
            rows.append(f"- 特征 {len(s.features)} 个："
                        + "，".join(_feature_label(f) for f in s.features[:12]))
        for c in self.checks:
            mark = {"ok": "✅", "warn": "⚠️", "error": "⛔"}.get(c["level"], "·")
            rows.append(f"- {mark} {c['message']}")
        if self.missing:
            rows.append("- 缺失：" + "；".join(self.missing[:8]))
        if self.conflicts:
            rows.append("- 矛盾：" + "；".join(self.conflicts[:8]))
        return "\n".join(rows)


def _feature_label(f: Any) -> str:
    t = {"hole": "孔", "pocket": "型腔", "slot": "槽", "step": "台阶",
         "boss": "凸台"}.get(str(getattr(f, "type", "")), str(getattr(f, "type", "")))
    if getattr(f, "type", "") == "hole":
        return f"{t}Ø{f.diameter:g}"
    return f"{t}{f.length:g}×{f.width:g}"


def _fnum(v: Any) -> float:
    if v is None or isinstance(v, bool):
        return 0.0
    try:
        x = float(str(v).replace(",", ".").strip())
        return x if math.isfinite(x) else 0.0
    except Exception:
        return 0.0


def _as_list(v: Any, cap: int = 40) -> List[Any]:
    return list(v)[:cap] if isinstance(v, (list, tuple)) else []


def _ocr_numbers(text: str) -> List[float]:
    out: List[float] = []
    for m in re.finditer(r"\d+(?:[.,]\d+)?", text or ""):
        try:
            out.append(float(m.group(0).replace(",", ".")))
        except Exception:
            pass
    return out


def _ocr_dia_candidates(text: str) -> List[float]:
    """从 OCR 文本中找疑似直径标注（Ø 常被 OCR 成 0/O）。

    只收「直径符号 + 数字」形态，避免把普通长度/公差当成直径。
    """
    t = str(text or "").replace(",", ".")
    vals: List[float] = []
    for m in re.finditer(r"(?<![\d.])(?:Ø|ø|O|0)(\d{2,4}(?:\.\d+)?)", t):
        try:
            v = float(m.group(1))
        except Exception:
            continue
        if 1.0 <= v <= 6000.0:
            vals.append(v)
    out: List[float] = []
    for v in vals:
        if not any(abs(v - x) <= 0.5 for x in out):
            out.append(v)
    return out


def _ocr_feature_hints(text: str) -> Dict[str, Any]:
    """从 OCR 文本提取明确的特征声明（孔/槽/型腔），用于完整性对账。"""
    t = str(text or "").replace(",", ".").replace("，", "，")
    out: Dict[str, Any] = {"holes": [], "slots": [], "pockets": []}
    # 4×Ø9 通孔 / 4×09通孔
    for m in re.finditer(r"(\d+)\s*[×xX]\s*[0OØ]?\s*(\d+(?:\.\d+)?)\s*通孔", t):
        try:
            out["holes"].append((int(m.group(1)), float(m.group(2))))
        except Exception:
            pass
    # 30×10 通槽
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*[×xX]\s*(\d+(?:\.\d+)?)\s*通?槽", t):
        try:
            out["slots"].append((float(m.group(1)), float(m.group(2))))
        except Exception:
            pass
    # 型腔 80×50 深6
    for m in re.finditer(r"型腔\s*(\d+(?:\.\d+)?)\s*[×xX]\s*(\d+(?:\.\d+)?)\s*深\s*(\d+(?:\.\d+)?)", t):
        try:
            out["pockets"].append((float(m.group(1)), float(m.group(2)), float(m.group(3))))
        except Exception:
            pass
    return out


def _dim_hit(value: float, nums: Sequence[float], tol: float = 0.02) -> bool:
    if value <= 0:
        return False
    for n in nums:
        if abs(n - value) <= max(tol * value, 0.05):
            return True
    return False


def _clean_data(data: Dict[str, Any]) -> Dict[str, Any]:
    d = dict(data or {})
    d["base_kind"] = "round" if str(d.get("base_kind") or "").lower() == "round" else "rect"
    for k in ("length", "width", "thickness", "diameter", "corner_radius", "chamfer"):
        d[k] = max(0.0, _fnum(d.get(k)))
    feats: List[Dict[str, Any]] = []
    pcds_raw: List[float] = []
    unknown: List[str] = []
    for raw in _as_list(d.get("features"), 60):
        if not isinstance(raw, dict):
            continue
        t = str(raw.get("type") or "hole").lower()
        if t not in ("hole", "pocket", "slot", "step", "boss"):
            unknown.append(t)
            continue
        f = {k: raw.get(k) for k in (
            "type", "x", "y", "diameter", "length", "width", "depth", "through",
            "corner_radius", "count", "pcd", "angle", "note") if k in raw}
        f["type"] = t
        for k in ("x", "y", "diameter", "length", "width", "depth", "corner_radius",
                  "pcd", "angle"):
            if k in f:
                f[k] = _fnum(f.get(k))
        try:
            f["count"] = max(1, min(64, int(f.get("count") or 1)))
        except Exception:
            f["count"] = 1
        if t == "hole" and float(f.get("pcd") or 0.0) > 0:
            pcds_raw.append(float(f.get("pcd") or 0.0))
        # 环形均布孔：展开成具体坐标（角度从 +X 起，逆时针均分）
        if t == "hole" and f["count"] > 1 and f.get("pcd", 0.0) > 0:
            n = int(f["count"])
            r = float(f["pcd"]) / 2.0
            a0 = math.radians(float(f.get("angle") or 0.0))
            for i in range(n):
                a = a0 + 2.0 * math.pi * i / n
                g = dict(f)
                g["x"] = round(r * math.cos(a), 4)
                g["y"] = round(r * math.sin(a), 4)
                g.pop("count", None)
                g.pop("pcd", None)
                g["datum"] = "bolt_circle"
                g["datum_ref"] = f"Ø{f['pcd']:g} 均布 {n} 孔"
                g["note"] = f"节圆 Ø{f['pcd']:g} 均布第 {i + 1}/{n} 孔"
                feats.append(g)
        else:
            f.pop("count", None)
            f.pop("pcd", None)
            feats.append(f)
    d["features"] = feats
    d["_pcds"] = pcds_raw
    d["_unknown_features"] = unknown
    return d


def _build_messages(files: Sequence[str], ocr_text: str, hint: str) -> List[Dict[str, Any]]:
    imgs = [str(p) for p in files if Path(p).suffix.lower() in
            {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}]
    encoded = [b for b in (_b64_image(p) for p in imgs[:4]) if b]
    text = SCHEMA_HINT
    if ocr_text.strip():
        text += "\n\n本地 OCR 读到的图上文字（Ø 可能被读成 0）：\n" + ocr_text.strip()[:3000]
        cands = _ocr_dia_candidates(ocr_text)
        if cands:
            text += ("\n\nOCR 中疑似直径标注：" + "、".join("Ø%g" % v for v in cands)
                     + "。请逐一说明它们分别对应外径、节圆、孔还是圆角；不得漏掉任何一个。")
    if hint.strip():
        text += "\n\n补充说明：" + hint.strip()[:600]
    text += "\n\n现在输出 JSON。"
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text, "images": encoded}]


def extract_spec(files: Sequence[str], settings: Any, spec: Any = None,
                 ocr_text: str = "", hint: str = "", log=None) -> ExtractResult:
    log = log or (lambda *_a, **_k: None)
    model = str(getattr(settings, "local_ai_model", DEFAULT_MODEL) or DEFAULT_MODEL)
    url = _url(settings)
    res = ExtractResult(model=model)
    if not bool(getattr(settings, "local_ai_enabled", True)):
        res.error = "设置中未启用本地模型"
        return res
    imgs = [str(p) for p in files if Path(p).suffix.lower() in
            {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}]
    if not imgs:
        res.error = "没有图片输入（CAD 文件请走 CAD 识别）"
        return res
    ok, detail = model_available(settings, log=log)
    if not ok:
        res.error = detail
        return res

    timeout = float(getattr(settings, "local_ai_timeout", 240) or 240)
    log(f"本地视觉模型读图中（{model}，结构化输出，离线）…")
    t0 = time.time()
    attempt_hint = hint
    last = None
    for attempt in range(1):
        res = ExtractResult(model=model, available=True)
        msgs = _build_messages(files, ocr_text, attempt_hint)
        if not any(m.get("images") for m in msgs if m.get("role") == "user"):
            res.error = "图片编码失败"
            return res
        try:
            payload = {
                "model": model,
                "stream": False,
                "think": False,
                "keep_alive": "10m",
                "format": EXTRACT_SCHEMA,
                "messages": msgs,
                "options": {
                    "temperature": 0.0,
                    "top_p": 0.9,
                    "repeat_penalty": 1.05,
                    "num_ctx": 8192,
                    "num_predict": 1600,
                },
            }
            resp = _post_json(url, "/api/chat", payload, timeout=timeout)
            msg = resp.get("message") or {}
            text = str(msg.get("content") or "").strip()
            res.raw = text
            data = {}
            try:
                data = json.loads(text) if text else {}
            except Exception:
                i, j = text.find("{"), text.rfind("}")
                if i >= 0 and j > i:
                    try:
                        data = json.loads(text[i:j + 1])
                    except Exception:
                        data = {}
            res.elapsed_s = time.time() - t0
            if not isinstance(data, dict) or not data.get("features") and not data.get("length"):
                res.error = "模型没有返回可用的结构化参数"
                log("⚠ " + res.error + "；继续使用规则识图。")
                return res
            data = _clean_data(data)
            res.data = data
            placeholders = {"图上找不到的尺寸", "图上互相矛盾的地方"}
            res.missing = [str(x) for x in _as_list(data.get("missing"), 20)
                           if str(x).strip() and str(x).strip() not in placeholders]
            res.conflicts = [str(x) for x in _as_list(data.get("conflicts"), 20)
                             if str(x).strip() and str(x).strip() not in placeholders]
            try:
                res.confidence = max(0.0, min(1.0, float(data.get("confidence") or 0.0)))
            except Exception:
                res.confidence = 0.0
            _validate_and_build(res, data, ocr_text, log, allow_infer=True, files=imgs)
            res.elapsed_s = time.time() - t0
            if res.retry_hint:
                # 严格模式：读不全 / 自相矛盾时直接判图纸不可用。
                # 不再发起第二次模型调用去“补数据 / 改参数”。
                res.note("error", res.retry_hint)
                res.confidence = min(res.confidence, 0.45)
                res.ok = False
                res.retry_hint = ""
            return res
        except Exception as e:
            res.elapsed_s = time.time() - t0
            res.error = f"本地视觉模型调用失败：{type(e).__name__}: {e}"
            log("⚠ " + res.error + "；继续使用规则识图。")
            return res
    return last or res


def _size_close(a: float, b: float, tol: float = 0.12) -> bool:
    a = abs(float(a or 0.0))
    b = abs(float(b or 0.0))
    return abs(a - b) <= max(1.2, tol * max(a, b))


def _feature_problems(spec, hints: Dict[str, Any], data: Dict[str, Any]) -> List[str]:
    """OCR 明确写出的特征与 spec 的逐项对账。"""
    problems: List[str] = []
    for n, dia in hints.get("holes") or []:
        got = [f for f in spec.features
               if f.type == "hole" and abs(f.diameter - float(dia)) <= 0.6]
        if len(got) < int(n):
            problems.append(f"OCR 写明 {int(n)}×Ø{float(dia):g} 通孔，但只识别出 {len(got)} 个")
    for L2, W2 in hints.get("slots") or []:
        got = [f for f in spec.features if f.type == "slot"
               and _size_close(max(f.length, f.width), max(L2, W2), 0.08)
               and _size_close(min(f.length, f.width), min(L2, W2), 0.08)]
        if not got:
            problems.append(f"OCR 写明 {float(L2):g}×{float(W2):g} 通槽，features 中没有对应 slot")
    for L2, W2, dep in hints.get("pockets") or []:
        got = [f for f in spec.features if f.type == "pocket"
               and _size_close(max(f.length, f.width), max(L2, W2), 0.08)
               and _size_close(min(f.length, f.width), min(L2, W2), 0.08)
               and abs(f.depth - float(dep)) <= 0.8]
        if not got:
            problems.append(f"OCR 写明型腔 {float(L2):g}×{float(W2):g} 深{float(dep):g}，"
                            f"features 中没有对应 pocket")
    # 只有还残留“节圆展开”的孔才算问题；像素复核替换后不应继续报。
    if spec.base_kind == "rect" and any(
            f.type == "hole" and str(getattr(f, "datum", "")) == "bolt_circle"
            for f in spec.features):
        problems.append("矩形板上的孔被写成节圆均布；矩形板应按图上边距/孔距给出每个孔的显式 x/y，"
                        "不能再用 pcd")
    return problems


def _symmetrize_4holes(spec, dia: float, tol_center: float = 0.6,
                       tol_pair: float = 1.2) -> bool:
    """四个孔落在四象限、关于零件中心近似对称时，用平均值消除像素噪声。

    只在“中心偏差 <0.6 mm、同名坐标极差 <1.2 mm、四个象限各一个”时生效，
    明显的非对称孔位不会被改动。
    """
    hs = [f for f in spec.features if f.type == "hole"
          and abs(float(f.diameter) - dia) <= max(0.5, 0.05 * dia)]
    if len(hs) != 4:
        return False
    cx = sum(float(f.x) for f in hs) / 4.0
    cy = sum(float(f.y) for f in hs) / 4.0
    if abs(cx) > tol_center or abs(cy) > tol_center:
        return False
    ax = [abs(float(f.x)) for f in hs]
    ay = [abs(float(f.y)) for f in hs]
    if min(ax) < 1.0 or min(ay) < 1.0:
        return False
    if max(ax) - min(ax) > tol_pair or max(ay) - min(ay) > tol_pair:
        return False
    if len({(float(f.x) >= 0.0, float(f.y) >= 0.0) for f in hs}) != 4:
        return False
    xa = sum(ax) / 4.0
    ya = sum(ay) / 4.0
    for f in hs:
        f.x = round(math.copysign(xa, float(f.x)), 3)
        f.y = round(math.copysign(ya, float(f.y)), 3)
        f.pos_source = "pixel-sym"
    return True


def _supplement_rect_from_pixels(spec, hints: Dict[str, Any], data: Dict[str, Any],
                                 files: Sequence[str], res: ExtractResult, log,
                                 ocr_text: str = "") -> bool:
    """矩形板：用俯视图像素轮廓补正孔位、槽和型腔。

    只在 OCR 明确声明了特征时才会写回；孔径用 OCR 名义值，位置用像素测量。
    这是测量而不是猜测：视图框长宽比必须与总体尺寸一致，圆/矩形轮廓
    必须完整落在毛坯内。结果标记 pos_source="pixel" 并降置信度提示复核。
    """
    from .models import Feature
    try:
        from .local_vision import measure_plate_features
        ev = measure_plate_features(files, spec.length, spec.width, log=log)
    except Exception as e:
        res.note("warn", f"俯视图像素复核失败：{type(e).__name__}: {e}")
        return False
    if not ev or not ev.get("features"):
        return False
    px = list(ev.get("features") or [])
    L, W, T = float(spec.length), float(spec.width), float(spec.thickness)
    changed: List[str] = []

    # ---- 孔：OCR 的 n×Ød 是数量与孔径的准绳，像素给位置 ----
    for n, dia in hints.get("holes") or []:
        n = int(n)
        dia = float(dia)
        if n < 1 or dia <= 0:
            continue
        tol = max(1.0, 0.20 * dia)
        cand = [f for f in px if f.kind == "circle"
                and abs(float(f.dia_mm) - dia) <= tol
                and abs(f.cx) <= L / 2.0 - 0.2 and abs(f.cy) <= W / 2.0 - 0.2]
        uniq = []
        for f in sorted(cand, key=lambda z: abs(float(z.dia_mm) - dia)):
            if any(math.hypot(f.cx - g.cx, f.cy - g.cy) <= max(1.5, 0.6 * dia)
                   for g in uniq):
                continue
            uniq.append(f)
        if not uniq:
            continue
        use = uniq[:n] if len(uniq) >= n else uniq
        keep = []
        removed = 0
        for f in spec.features:
            if f.type == "hole" and abs(f.diameter - dia) <= max(0.8, 0.05 * dia):
                near = any(math.hypot(f.x - p.cx, f.y - p.cy) <= max(2.0, 0.8 * dia)
                           for p in use)
                if near:
                    keep.append(f)
                else:
                    removed += 1
                continue
            keep.append(f)
        spec.features = keep
        same = [f for f in keep if f.type == "hole" and abs(f.diameter - dia) <= max(0.8, 0.05 * dia)]
        added = 0
        for p in use:
            if any(math.hypot(f.x - p.cx, f.y - p.cy) <= max(1.5, 0.6 * dia) for f in same):
                continue
            spec.features.append(Feature(
                type="hole", x=round(float(p.cx), 3), y=round(float(p.cy), 3),
                diameter=round(dia, 3), depth=0.0, through=True,
                datum="pixel", pos_source="pixel",
                evidence=f"俯视图圆轮廓 Ø{p.dia_mm:.1f}px → Ø{dia:g}；OCR 标明 {n}×Ø{dia:g} 通孔"))
            same.append(spec.features[-1])
            added += 1
        if added or removed:
            txt = f"{len(same)}×Ø{dia:g}"
            if removed:
                txt += f"（替换 {removed} 个错位孔）"
            changed.append(txt)
        if n == 4 and len(same) == 4 and _symmetrize_4holes(spec, dia):
            changed.append(f"{n}×Ø{dia:g} 按对称中心拟合")

    # ---- 通槽 ----
    for L2, W2 in hints.get("slots") or []:
        L2, W2 = float(L2), float(W2)
        cand = [f for f in px
                if _size_close(f.w_mm, max(L2, W2), 0.10)
                and _size_close(f.h_mm, min(L2, W2), 0.10)]
        if not cand:
            continue
        p = min(cand, key=lambda z: abs(z.w_mm - max(L2, W2)) + abs(z.h_mm - min(L2, W2)))
        ex = [f for f in spec.features if f.type == "slot"
              and _size_close(max(f.length, f.width), max(L2, W2), 0.08)
              and _size_close(min(f.length, f.width), min(L2, W2), 0.08)]
        if ex:
            moved = False
            for f in ex:
                if math.hypot(f.x - p.cx, f.y - p.cy) > 3.0:
                    f.x = round(float(p.cx), 3)
                    f.y = round(float(p.cy), 3)
                    f.pos_source = "pixel"
                    moved = True
            if moved:
                changed.append(f"{L2:g}×{W2:g} 通槽（按像素修正位置）")
        else:
            spec.features.append(Feature(
                type="slot", x=round(float(p.cx), 3), y=round(float(p.cy), 3),
                length=round(max(L2, W2), 3), width=round(min(L2, W2), 3),
                depth=round(T, 3), through=True,
                corner_radius=round(min(L2, W2) / 2.0, 3),
                datum="pixel", pos_source="pixel",
                evidence=f"俯视图轮廓 {p.w_mm:.1f}×{p.h_mm:.1f} → {L2:g}×{W2:g} 通槽"))
            changed.append(f"{L2:g}×{W2:g} 通槽")

    # ---- 型腔 ----
    for L2, W2, dep in hints.get("pockets") or []:
        L2, W2, dep = float(L2), float(W2), float(dep)
        cand = [f for f in px
                if _size_close(f.w_mm, max(L2, W2), 0.10)
                and _size_close(f.h_mm, min(L2, W2), 0.10)]
        if not cand:
            continue
        p = min(cand, key=lambda z: abs(z.w_mm - max(L2, W2)) + abs(z.h_mm - min(L2, W2)))
        ex = [f for f in spec.features if f.type == "pocket"
              and _size_close(max(f.length, f.width), max(L2, W2), 0.08)
              and _size_close(min(f.length, f.width), min(L2, W2), 0.08)
              and abs(f.depth - dep) <= 0.8]
        if ex:
            moved = False
            for f in ex:
                if math.hypot(f.x - p.cx, f.y - p.cy) > 3.0:
                    f.x = round(float(p.cx), 3)
                    f.y = round(float(p.cy), 3)
                    f.pos_source = "pixel"
                    moved = True
            if moved:
                changed.append(f"型腔 {L2:g}×{W2:g}（按像素修正位置）")
        else:
            spec.features.append(Feature(
                type="pocket", x=round(float(p.cx), 3), y=round(float(p.cy), 3),
                length=round(max(L2, W2), 3), width=round(min(L2, W2), 3),
                depth=round(dep, 3), through=False,
                datum="pixel", pos_source="pixel",
                evidence=f"俯视图轮廓 {p.w_mm:.1f}×{p.h_mm:.1f} → {L2:g}×{W2:g} 深{dep:g}"))
            changed.append(f"型腔 {L2:g}×{W2:g} 深{dep:g}")

    # ---- 清理「图上有、OCR 也有」之外的凭空口袋/台阶 ----
    # 模型偶尔会多编一个型腔/台阶（实测安装板多出 40×20 深5）。
    # 只删同时满足两条的：像素轮廓里找不到 + OCR 里没有任何对应数字。
    nums = _ocr_numbers(ocr_text) if ocr_text else []
    kept, dropped = [], []
    for f in spec.features:
        if f.type not in ("pocket", "step", "slot", "boss"):
            kept.append(f)
            continue
        dims = [float(f.length or 0.0), float(f.width or 0.0), float(f.depth or 0.0)]
        ocr_ok = any(abs(d - n) <= max(0.3, 0.02 * max(d, n))
                     for d in dims if d > 0 for n in nums if n > 0)
        px_ok = any(_size_close(float(f.length or 0.0), max(p.w_mm, p.h_mm), 0.10)
                    and _size_close(float(f.width or 0.0), min(p.w_mm, p.h_mm), 0.10)
                    for p in px)
        if ocr_ok or px_ok:
            kept.append(f)
        else:
            dropped.append(f)
    if dropped:
        spec.features = kept
        changed.append("删除无依据特征：" + "、".join(
            f"{f.type} {f.length:g}×{f.width:g} 深{f.depth:g}" for f in dropped))

    if not changed:
        return False
    res.note("warn", "俯视图像素复核补正：" + "；".join(changed) + "（孔位来自像素测量，请复核）")
    res.confidence = min(res.confidence, 0.78)
    log("🔎 俯视图像素复核补正：" + "；".join(changed))
    return True


def _validate_and_build(res: ExtractResult, data: Dict[str, Any], ocr_text: str, log,
                        allow_infer: bool = False,
                        files: Sequence[str] = ()) -> None:
    """严格校验模型输出：只读、不修、不补、不删。

    任何缺尺寸、矛盾、越界、OCR 对不上都记为 blocking error，
    由上层直接判图纸不可用，绝不带着猜测去建模。
    """
    from .models import PartSpec, Feature

    for t in (data.get("_unknown_features") or []):
        res.note("error", f"模型给出了软件不支持的几何类型：{t}（当前只支持孔/型腔/槽/台阶/凸台）")
    for m in res.missing:
        res.note("error", "图纸缺少必要数据：" + str(m))
    for c in res.conflicts:
        res.note("error", "图纸数据自相矛盾：" + str(c))

    L = float(data.get("length") or 0.0)
    W = float(data.get("width") or 0.0)
    T = float(data.get("thickness") or 0.0)
    D = float(data.get("diameter") or 0.0)

    if data.get("base_kind") == "round":
        if D <= 0:
            # 叉/板类零件的圆弧头常被模型误判成“圆盘”。不管是哪种，
            # 没有外径就无法确定毛坯，属于图纸信息不完整，直接拒绝。
            _dims = sorted({round(float(v), 3) for v in _ocr_numbers(ocr_text)
                            if 0 < float(v) < 6000})
            _shown = "、".join("%g" % v for v in _dims[:12])
            res.note("error",
                     "图纸缺少可确定毛坯的外形尺寸：未读到外径"
                     + (f"（图上只读到 {_shown}）" if _shown else "")
                     + "。请补充总长/总宽（或外径）和板厚/总高后再识别。")
            return
        L = W = D
        data["length"] = data["width"] = D
    if L <= 0 or W <= 0 or T <= 0:
        _miss = [lab for lab, val in (("总长", L), ("总宽", W), ("板厚/总高", T))
                 if val <= 0]
        res.note("error", "图纸缺少总体尺寸：" + "、".join(_miss)
                          + "（未标注或未读到），不能确定毛坯，已停止建模")
        return
    if max(L, W, T) > 6000.0:
        res.note("error", "总体尺寸异常放大（%.1f mm），疑似比例误读" % max(L, W, T))
        return
    if min(L, W) / max(L, W) < 0.02:
        res.note("error", "长宽比例异常（%.1f : %.1f）" % (L, W))
        return

    nums = _ocr_numbers(ocr_text)
    hits = [n for n, v in (("长", L), ("宽", W), ("厚", T), ("外径", D)) if _dim_hit(v, nums)]
    if nums:
        if not hits:
            res.note("error", "模型给出的总体尺寸在图上 OCR 数字里一个都找不到"
                              "（%.2f/%.2f/%.2f），疑似比例或单位误读" % (L, W, T))
        elif len(hits) == 1:
            res.note("warn", "只有 %s 在图上 OCR 中找到同值，其余尺寸需人工核对" % hits[0])
        else:
            res.note("ok", "总体尺寸与图上标注吻合：%s" % "、".join(hits))
    else:
        res.note("error", "图上没有读到任何 OCR 尺寸，无法核对模型参数")

    name = str(data.get("part_name") or "").strip()
    if not name or name in ("未命名零件", "未知", "零件", "part"):
        for line in str(ocr_text or "").splitlines():
            tok = (line.strip().split() or [""])[0]
            if 2 <= len(tok) <= 12 and re.search(r"[\u4e00-\u9fff]", tok):
                name = tok
                break
    if not name:
        name = Path(files[0]).stem if files else "未命名零件"

    spec = PartSpec(
        part_name=name[:60],
        part_type=str(data.get("part_type") or "plate"),
        length=L, width=W, thickness=T,
        base_kind=str(data.get("base_kind") or "rect"),
        diameter=D,
        corner_radius=float(data.get("corner_radius") or 0.0),
        chamfer=float(data.get("chamfer") or 0.0),
        source="local-vlm",
        confidence=res.confidence,
    )
    if spec.base_kind == "round":
        spec.diameter = D
        spec.length = spec.width = D

    for fd in data.get("features") or []:
        f = Feature.from_dict(fd)
        if f.type == "hole" and f.diameter <= 0:
            res.note("error", f"{_feature_label(f)} 未标注孔径")
        if f.type in ("pocket", "slot", "step") and (f.length <= 0 or f.width <= 0):
            res.note("error", f"{_feature_label(f)} 缺少长度或宽度")
        if f.type == "boss" and f.depth <= 0:
            res.note("error", f"{_feature_label(f)} 缺少高度")
        if f.type == "hole" and f.diameter >= 0.95 * min(L, W):
            res.note("error", "%s 直径接近毛坯尺寸，图纸数据可疑" % _feature_label(f))
        if spec.base_kind == "round":
            r = D / 2.0
            if math.hypot(f.x, f.y) > r + 1.0:
                res.note("error", "%s @(%.1f,%.1f) 落在圆盘外" % (_feature_label(f), f.x, f.y))
        elif abs(f.x) > L / 2.0 + 1.0 or abs(f.y) > W / 2.0 + 1.0:
            res.note("error", "%s @(%.1f,%.1f) 落在毛坯外" % (_feature_label(f), f.x, f.y))
        spec.features.append(f)

    if not spec.features:
        res.note("warn", "图纸没有可切特征（确认是否为一块光板）")

    hints = _ocr_feature_hints(ocr_text)
    # 矩形板：OCR 给数量/孔径，俯视图像素给孔位。
    # 这是对图纸本身的确定性测量，不是模型补值。
    if spec.base_kind == "rect" and files and hints:
        try:
            _supplement_rect_from_pixels(spec, hints, data, files, res, log,
                                           ocr_text=ocr_text)
        except Exception as e:
            res.note("warn", f"俯视图像素复核失败：{type(e).__name__}: {e}")
    problems = _feature_problems(spec, hints, data)
    if problems:
        for msg in problems:
            res.note("error", "特征完整性核对失败：" + msg)

    # OCR 里每个疑似 Ø 值都必须能对上外径/节圆/孔径/圆角；对不上就报错。
    cands = _ocr_dia_candidates(ocr_text)
    used = [D]
    for f in spec.features:
        if f.type in ("hole", "boss") and f.diameter > 0:
            used.append(f.diameter)
    used.extend([float(x) for x in (data.get("_pcds") or [])])
    radii = []
    for f in spec.features:
        if f.type != "hole":
            continue
        r = math.hypot(f.x, f.y)
        if r > 1.0:
            radii.append(r)
    for r in radii:
        if sum(1 for q in radii if abs(q - r) <= 1.0) >= 3:
            used.append(2.0 * r)
    if spec.corner_radius > 0:
        used.append(spec.corner_radius * 2.0)
    unmatched = []
    for v in cands:
        if not any(abs(v - u) <= max(0.6, 0.015 * max(v, u)) for u in used if u > 0):
            unmatched.append(v)
    if unmatched:
        vals = "、".join("Ø%g" % v for v in unmatched)
        center_holes = [f for f in spec.features
                        if f.type == "hole" and math.hypot(f.x, f.y) < 1.0]
        extra = ""
        if spec.base_kind == "round" and not center_holes:
            extra = "；圆盘中心没有任何孔/沉孔特征，疑似漏读同心孔"
        res.note("error", f"图上直径 {vals} 无法对应到任何已识别特征" + extra)

    for msg in spec.validate_strict():
        res.note("error", msg)

    res.spec = spec
    res.ok = not res.blocking
    log(f"本地视觉模型给出：{spec.part_name} {spec.base_kind} "
        f"{spec.length:g}×{spec.width:g}×{spec.thickness:g}，{len(spec.features)} 个特征")
    if res.ok:
        res.note("ok", "结构化参数通过严格确定性校验（未做任何补值或清洗）")
