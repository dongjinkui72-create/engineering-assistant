from __future__ import annotations

"""Optional fully-offline vision reviewer.

The deterministic OCR/rule engine remains the source of truth.  This module
only asks a local Qwen3-VL model for a second opinion and turns obvious
contradictions into audit warnings.  It never executes CAD commands and it
never invents missing dimensions.
"""

import base64
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

DEFAULT_MODEL = "qwen2.5vl:7b"
DEFAULT_URL = "http://127.0.0.1:11511"
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}

SYSTEM_PROMPT = r"""只回答四行，不要解释。"""

JSON_SCHEMA = r"""视图：图上真实存在的视图（主视图/俯视图/左视图/右视图/剖视图/局部放大/轴测图），逗号分隔；没有写“无”
尺寸：有/无
可建模：是/否
矛盾：有/无"""



@dataclass
class LocalAIResult:
    available: bool = False
    model: str = DEFAULT_MODEL
    url: str = DEFAULT_URL
    elapsed_s: float = 0.0
    drawing_type: str = "unknown"
    views: List[dict] = field(default_factory=list)
    has_titleblock: Optional[bool] = None
    has_dimensions: Optional[bool] = None
    has_section: Optional[bool] = None
    has_hidden_lines: Optional[bool] = None
    has_centerlines: Optional[bool] = None
    overall: Dict[str, Any] = field(default_factory=dict)
    features: List[dict] = field(default_factory=list)
    missing_data: List[str] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)
    complete: Optional[bool] = None
    confidence: float = 0.0
    summary: str = ""
    advisories: List[dict] = field(default_factory=list)
    raw: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.available and not self.error)

    def markdown(self) -> str:
        icon = "✅" if self.ok else "⚠️"
        lines = [f"## {icon} 本地视觉模型复核（离线）", ""]
        if not self.ok:
            lines.append(f"- 状态：未启用或不可用（{self.error or '模型未加载'}）")
            lines.append("- 规则识图不受影响，仍按本地 OCR/尺寸审计继续。")
            return "\n".join(lines) + "\n"
        lines += [
            f"- 模型：`{self.model}`",
            f"- 用时：{self.elapsed_s:.1f} 秒",
            f"- 图纸类型：{self.drawing_type or 'unknown'}",
            f"- 模型置信度：{self.confidence:.0%}",
        ]

        def _mark(v):
            return "✅" if v is True else ("❌" if v is False else "❔")
        checks = [
            ("标题栏", self.has_titleblock),
            ("尺寸标注", self.has_dimensions),
            ("剖视图", self.has_section),
            ("虚线", self.has_hidden_lines),
        ]
        lines.append("- 图纸要素：" + "  ".join(f"{n}{_mark(v)}" for n, v in checks))
        zh = {"front": "主视图", "top": "俯视图", "left": "左视图", "right": "右视图",
              "section": "剖视图", "detail": "局部放大", "isometric": "轴测图"}
        names = []
        for v in self.views or []:
            if isinstance(v, dict):
                n = str(v.get("name") or "").strip()
            else:
                n = str(v or "").strip()
            if n:
                names.append(zh.get(n, n))
        if names:
            lines.append("- 模型认为的视图：" + "、".join(names[:8]))
        if self.complete is not None:
            lines.append("- 模型判断可直接建模：" + ("是" if self.complete else "否"))
        O = self.overall or {}
        dims = [O.get(k) for k in ("length", "width", "thickness")]
        if any(v is not None for v in dims):
            lines.append("- 模型读出外形：" + " × ".join(
                ("?" if v is None else f"{float(v):g}") for v in dims) + " mm")
        if self.summary:
            lines.append(f"- 结论：{self.summary}")
        if self.missing_data:
            lines.append("- 模型提示缺数据：" + "；".join(self.missing_data[:8]))
        if self.conflicts:
            lines.append("- 模型提示矛盾：" + "；".join(self.conflicts[:8]))
        if self.advisories:
            lines += ["", "| 级别 | 问题 | 证据 |", "|---|---|---|"]
            for a in self.advisories[:12]:
                lv = "⚠ 警告" if a.get("severity") == "warn" else "ℹ 提示"
                msg = str(a.get("message", "")).replace("|", r"\|")
                ev = str(a.get("evidence", "")).replace("|", r"\|")
                lines.append(f"| {lv} | {msg} | {ev} |")
        lines.append("\n> AI 只做复核；最终尺寸、门禁和建模仍由规则引擎与 CAD 实测决定。")
        return "\n".join(lines) + "\n"


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _app_roots() -> Iterable[Path]:
    """Candidate roots in both source and frozen (PyInstaller) layouts."""
    if getattr(sys, "frozen", False):
        yield Path(sys.executable).resolve().parent
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            yield Path(meipass)
    root = _project_root()
    yield root
    yield root.parent


def _runtime_candidates() -> Iterable[Path]:
    env = os.environ.get("ENGINEERING_ASSISTANT_OLLAMA", "").strip()
    if env:
        yield Path(env)
    for root in _app_roots():
        yield root / "local_ai" / "runtime" / "ollama.exe"
        yield root / "local_ai" / "ollama.exe"
        yield root / "ollama.exe"
    yield Path(r"E:\Ollama\ollama.exe")
    yield Path.home() / "AppData" / "Local" / "Programs" / "Ollama" / "ollama.exe"


def _model_dir() -> Optional[Path]:
    env = os.environ.get("ENGINEERING_ASSISTANT_MODELS", "").strip()
    if env and Path(env).exists():
        return Path(env)
    for root in _app_roots():
        p = root / "local_ai" / "models"
        if p.exists():
            return p
    return None


def _url(settings: Any) -> str:
    return str(getattr(settings, "local_ai_url", DEFAULT_URL) or DEFAULT_URL).rstrip("/")


def _get_json(url: str, path: str, timeout: float = 5.0) -> Any:
    req = urllib.request.Request(url + path, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _post_json(url: str, path: str, payload: dict, timeout: float) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url + path, data=data, method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def server_alive(url: str, timeout: float = 2.0) -> bool:
    try:
        _get_json(url, "/api/tags", timeout=timeout)
        return True
    except Exception:
        return False


def ensure_server(settings: Any, log=None) -> tuple[bool, str]:
    """Return (ready, detail).  Starts the bundled portable runtime if needed."""
    log = log or (lambda *_a, **_k: None)
    url = _url(settings)
    if server_alive(url):
        return True, "已连接本地模型服务"
    exe = next((p for p in _runtime_candidates() if p.exists()), None)
    if exe is None:
        return False, "未找到本地模型运行器 ollama.exe"
    models = _model_dir()
    env = os.environ.copy()
    hostport = url.split("://", 1)[-1].strip("/") or "127.0.0.1:11511"
    env["OLLAMA_HOST"] = hostport
    env["OLLAMA_VULKAN"] = "1"
    env["OLLAMA_NO_CLOUD"] = "1"
    env["OLLAMA_KEEP_ALIVE"] = "10m"
    if models is not None:
        env["OLLAMA_MODELS"] = str(models)
    cwd = exe.parent
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen([str(exe), "serve"], cwd=str(cwd), env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, creationflags=flags)
        log("正在启动内置本地视觉模型（首次约需数秒）…")
    except Exception as e:
        return False, f"本地模型服务启动失败：{e}"
    deadline = time.time() + 45
    while time.time() < deadline:
        if server_alive(url, timeout=2.0):
            return True, "本地模型服务已启动"
        time.sleep(1.0)
    return False, "本地模型服务启动超时"


def _tag_names(payload: Any) -> List[str]:
    out = []
    for m in (payload or {}).get("models", []) or []:
        n = str(m.get("name") or m.get("model") or "")
        if n:
            out.append(n)
    return out


def model_available(settings: Any, log=None) -> tuple[bool, str]:
    ok, detail = ensure_server(settings, log=log)
    if not ok:
        return False, detail
    url = _url(settings)
    model = str(getattr(settings, "local_ai_model", DEFAULT_MODEL) or DEFAULT_MODEL)
    try:
        names = _tag_names(_get_json(url, "/api/tags", timeout=5))
    except Exception as e:
        return False, f"读取本地模型列表失败：{e}"
    if not any(n == model or n.startswith(model + ":") for n in names):
        return False, f"本地模型 {model} 未安装；清单：{', '.join(names[:6]) or '空'}"
    return True, f"本地模型 {model} 可用"


def _b64_image(path: str, max_side: int = 1400) -> Optional[str]:
    try:
        from PIL import Image, ImageOps
        im = Image.open(path)
        im = ImageOps.exif_transpose(im)
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        w, h = im.size
        if max(w, h) > max_side:
            s = max_side / float(max(w, h))
            im = im.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85, optimize=True)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        try:
            return base64.b64encode(Path(path).read_bytes()).decode("ascii")
        except Exception:
            return None


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else {}
    except Exception:
        pass
    i, j = text.find("{"), text.rfind("}")
    if i >= 0 and j > i:
        try:
            v = json.loads(text[i:j + 1])
            return v if isinstance(v, dict) else {}
        except Exception:
            pass
    return {}


def _num(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(str(v).replace(",", ".").strip())
        return x if x > 0 else None
    except Exception:
        return None


def _tri(v: Any) -> Optional[bool]:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        t = v.strip().lower()
        if t in ("true", "yes", "1", "是", "有"):
            return True
        if t in ("false", "no", "0", "否", "无"):
            return False
    return None


def _as_list(v: Any, limit: int = 12) -> List[Any]:
    if not isinstance(v, list):
        return []
    return v[:limit]


def _ell(v: Any, limit: int = 140) -> str:
    s = str(v or "").strip()
    return s if len(s) <= limit else s[:limit - 1] + "…"


_VIEW_WORDS = [
    ("主视", "front"), ("俯视", "top"), ("左视", "left"), ("右视", "right"),
    ("剖视", "section"), ("局部放大", "detail"), ("轴测", "isometric"),
]


def _yes_no(v: Any):
    t = str(v or "").strip().lower()
    if not t:
        return None
    if any(k in t for k in ("无", "否", "不能", "不可以", "no", "false")):
        return False
    if any(k in t for k in ("有", "是", "可以", "能", "yes", "true")):
        return True
    return None


def _parse_review(text: str):
    """Parse the four-line answer (or a JSON object if the model produced one)."""
    t = str(text or "").strip()
    if not t:
        return None
    if t.startswith("{"):
        try:
            obj = json.loads(t)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    t = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", t).strip()
    lines = [re.sub(r"^[\s\-*•\d\.、]+", "", ln).strip() for ln in t.splitlines() if ln.strip()]
    if not lines:
        return None
    vals = {}
    seq = []
    for ln in lines:
        m = re.match(r"^(视图|尺寸|可建模|矛盾)\s*[:：]\s*(.*)$", ln)
        if m:
            vals[m.group(1)] = m.group(2).strip()
        else:
            seq.append(ln)
    if not vals and len(seq) >= 3:
        for k, v in zip(("视图", "尺寸", "可建模", "矛盾"), seq):
            vals[k] = v
    if not vals:
        return None
    views = []
    raw_views = vals.get("视图", "")
    if raw_views and raw_views not in ("无", "没有"):
        for word, name in _VIEW_WORDS:
            if word in raw_views and name not in views:
                views.append(name)
    return {
        "drawing_type": "part",
        "views": views,
        "has_dimensions": _yes_no(vals.get("尺寸")),
        "complete": _yes_no(vals.get("可建模")),
        "conflicts": ([] if _yes_no(vals.get("矛盾")) is not True else ["模型认为图上存在矛盾，需人工核对"]),
        "confidence": 0.6 if len(vals) >= 3 else 0.4,
        "summary": "视图：" + (raw_views or "未说明"),
    }


def _compare(spec: Any, data: dict, res: LocalAIResult) -> None:
    O = data.get("overall") if isinstance(data.get("overall"), dict) else {}
    local = {
        "length": getattr(spec, "length", None),
        "width": getattr(spec, "width", None),
        "thickness": getattr(spec, "thickness", None),
    }
    labels = {"length": "长", "width": "宽", "thickness": "厚"}
    mismatches = []
    for k in ("length", "width", "thickness"):
        a = _num(local.get(k))
        b = _num(O.get(k))
        if a and b:
            tol = max(0.5, abs(a) * 0.015)
            if abs(a - b) > tol:
                mismatches.append(f"{labels[k]}：规则 {a:g} / 模型 {b:g} mm")
    if mismatches:
        res.advisories.append({
            "severity": "warn", "code": "LOCAL_AI_DIM_MISMATCH",
            "message": "本地视觉模型与规则识图的总体尺寸不一致，需人工确认",
            "evidence": "；".join(mismatches),
            "suggestion": "以图纸原始标注和人工复核为准；不要直接按模型数值覆盖 spec。",
        })

    dt = str(data.get("drawing_type") or "unknown").lower()
    if dt == "assembly":
        res.advisories.append({
            "severity": "warn", "code": "LOCAL_AI_ASSEMBLY",
            "message": "本地视觉模型判断这可能是一张装配图，不能直接当单件建模",
            "evidence": _ell(data.get("summary")),
            "suggestion": "先确认零件图/装配图；若为装配图，请拆出单件图纸。",
        })

    for s in _as_list(data.get("missing_data"), 8):
        if str(s).strip():
            res.advisories.append({
                "severity": "warn", "code": "LOCAL_AI_MISSING",
                "message": "本地视觉模型提示图纸可能缺少必要数据",
                "evidence": _ell(s),
                "suggestion": "核对原图；补齐前不要绕过门禁。",
            })
    for s in _as_list(data.get("conflicts"), 8):
        if str(s).strip():
            res.advisories.append({
                "severity": "warn", "code": "LOCAL_AI_CONFLICT",
                "message": "本地视觉模型提示图纸可能存在尺寸或视图矛盾",
                "evidence": _ell(s),
                "suggestion": "回到图纸核对；必要时人工修正 spec。",
            })

    if data.get("has_dimensions") is False:
        res.advisories.append({
            "severity": "warn", "code": "LOCAL_AI_NO_DIMENSIONS",
            "message": "本地视觉模型没有在图上看到尺寸标注",
            "evidence": _ell(data.get("summary")),
            "suggestion": "确认导入的是带尺寸的零件图，而不是照片或示意图。",
        })
    if data.get("complete") is False:
        res.advisories.append({
            "severity": "warn", "code": "LOCAL_AI_INCOMPLETE",
            "message": "本地视觉模型认为这张图纸不足以直接建模",
            "evidence": "；".join(str(x) for x in _as_list(data.get("missing_data"), 4)) or _ell(data.get("summary")),
            "suggestion": "核对缺失的视图或尺寸；补齐前不要绕过门禁。",
        })
    if data.get("has_titleblock") is False:
        res.advisories.append({
            "severity": "info", "code": "LOCAL_AI_NO_TITLEBLOCK",
            "message": "本地视觉模型没有识别到标题栏",
            "evidence": _ell(data.get("summary")),
            "suggestion": "检查图框是否被裁掉；标题栏缺失不影响已确认的几何。",
        })


def analyze_drawing(files: Sequence[str], settings: Any, spec: Any = None,
                    ocr_text: str = "", hint: str = "", log=None) -> LocalAIResult:
    log = log or (lambda *_a, **_k: None)
    model = str(getattr(settings, "local_ai_model", DEFAULT_MODEL) or DEFAULT_MODEL)
    url = _url(settings)
    res = LocalAIResult(model=model, url=url)
    if not bool(getattr(settings, "local_ai_enabled", True)):
        res.error = "设置中未启用"
        return res
    ok, detail = model_available(settings, log=log)
    if not ok:
        res.error = detail
        log("⚠ " + detail + "；继续使用纯规则识图。")
        return res

    imgs = [str(p) for p in files if Path(p).suffix.lower() in IMAGE_EXT]
    if not imgs:
        res.error = "没有图片输入"
        return res
    encoded = [b for b in (_b64_image(p) for p in imgs[:4]) if b]
    if not encoded:
        res.error = "图片编码失败"
        return res

    # 2B 模型的 thinking 很啰嗦。请求越短、答案越短，越不容易卡在截断上。
    user = JSON_SCHEMA + ("\n补充提示：" + hint if hint else "")
    t0 = time.time()
    log(f"本地视觉模型复核中（{model}，完全离线）…")
    try:
        timeout = float(getattr(settings, "local_ai_timeout", 180) or 180)
        data = None
        text = ""
        last_text = ""
        # 2B 的 thinking 偶发打转。先用极短 system 试一次，失败再换成无 system 重试。
        for attempt in range(2):
            msgs = []
            if attempt == 0:
                msgs.append({"role": "system", "content": SYSTEM_PROMPT})
            msgs.append({"role": "user", "content": user, "images": encoded})
            payload = {
                "model": model,
                "stream": False,
                "think": False,
                "keep_alive": "10m",
                "messages": msgs,
                "options": {
                    "temperature": 0.0 if attempt == 0 else 0.35,
                    "repeat_penalty": 1.12,
                    "num_ctx": 8192,
                    "num_predict": 2500,
                },
            }
            resp = _post_json(url, "/api/chat", payload, timeout=timeout)
            _msg = resp.get("message") or {}
            last_text = str(_msg.get("thinking") or "")
            text = str(_msg.get("content") or "")
            if not text.strip():
                text = last_text
            data = _parse_review(text)
            if data:
                break
            log("本地视觉模型第一次没给出可解析结论，正在重试…")
        res.elapsed_s = time.time() - t0
        res.raw = text or last_text
        if not data:
            raise ValueError("模型没有返回可解析的审图结论")
        res.available = True
        res.drawing_type = str(data.get("drawing_type") or "unknown").lower()
        views = []
        for v in _as_list(data.get("views"), 12):
            if isinstance(v, dict):
                name = str(v.get("name") or "").strip()
                if name:
                    views.append({"name": name, "present": bool(v.get("present", True))})
            elif str(v).strip():
                views.append({"name": str(v).strip(), "present": True})
        res.views = views
        res.overall = data.get("overall") if isinstance(data.get("overall"), dict) else {}
        res.features = _as_list(data.get("features"), 40)
        res.has_titleblock = _tri(data.get("has_titleblock"))
        res.has_dimensions = _tri(data.get("has_dimensions"))
        res.has_section = _tri(data.get("has_section"))
        res.has_hidden_lines = _tri(data.get("has_hidden_lines"))
        res.has_centerlines = _tri(data.get("has_centerlines"))
        res.missing_data = [str(x) for x in _as_list(data.get("missing_data"), 16) if str(x).strip()]
        res.conflicts = [str(x) for x in _as_list(data.get("conflicts"), 16) if str(x).strip()]
        res.complete = data.get("complete") if isinstance(data.get("complete"), bool) else None
        try:
            res.confidence = max(0.0, min(1.0, float(data.get("confidence") or 0.0)))
        except Exception:
            res.confidence = 0.0
        res.summary = _ell(data.get("summary"), 240)
        _compare(spec, data, res)
        log(f"本地视觉模型完成：{res.drawing_type}，置信度 {res.confidence:.0%}，"
            f"{res.elapsed_s:.1f} 秒")
        if res.advisories:
            for a in res.advisories[:4]:
                log("⚠ AI复核：" + str(a.get("message") or ""))
        return res
    except Exception as e:
        res.elapsed_s = time.time() - t0
        res.error = f"本地视觉模型调用失败：{e}"
        log("⚠ " + res.error + "；继续使用纯规则识图。")
        return res


def status(settings: Any) -> dict:
    model = str(getattr(settings, "local_ai_model", DEFAULT_MODEL) or DEFAULT_MODEL)
    out = {"enabled": bool(getattr(settings, "local_ai_enabled", True)),
           "model": model, "url": _url(settings), "ready": False, "detail": ""}
    if not out["enabled"]:
        out["detail"] = "已关闭"
        return out
    ok, detail = model_available(settings)
    out["ready"], out["detail"] = ok, detail
    return out
