# -*- coding: utf-8 -*-
"""本地 OCR：不依赖任何云端识图接口。

只用 RapidOCR（ONNX Runtime，本地 CPU 推理）读图纸文字，再用确定性
规则把文字整理成尺寸标注。这个模块只负责“看见并读出文字”，不猜几何；
几何推断在 local_vision.py 里做。

设计约束
--------
* RapidOCR 延迟初始化：软件启动时不加载 20MB 模型。
* 任何异常都转成 warnings，不让一张坏图把整个流程打崩。
* OCR 常见混淆（Ø→Q、逗号小数、全角字符）在这里统一规范化。
"""
from __future__ import annotations

import math
import re
import threading
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}

_ENGINE = None
_ENGINE_LOCK = threading.Lock()
_ENGINE_ERROR = ""


# ---------------------------------------------------------------- data model
@dataclass
class OcrToken:
    text: str                 # OCR 原始文本
    norm: str                 # 规范化后的文本
    conf: float
    box: Tuple[float, float, float, float]   # x1,y1,x2,y2（图片像素）
    page: int = 0
    rot: int = 0              # 0=原方向；90/270=旋转补读

    @property
    def cx(self) -> float:
        return (self.box[0] + self.box[2]) * 0.5

    @property
    def cy(self) -> float:
        return (self.box[1] + self.box[3]) * 0.5

    @property
    def h(self) -> float:
        return max(1.0, self.box[3] - self.box[1])


@dataclass
class OcrLine:
    text: str
    box: Tuple[float, float, float, float]
    page: int
    tokens: List[OcrToken] = field(default_factory=list)

    @property
    def cx(self) -> float:
        return (self.box[0] + self.box[2]) * 0.5

    @property
    def cy(self) -> float:
        return (self.box[1] + self.box[3]) * 0.5


@dataclass
class OcrPage:
    index: int
    path: str
    tokens: List[OcrToken] = field(default_factory=list)
    text: str = ""
    elapsed: float = 0.0
    ok: bool = True
    error: str = ""


@dataclass
class OcrDoc:
    pages: List[OcrPage] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def tokens(self) -> List[OcrToken]:
        out: List[OcrToken] = []
        for p in self.pages:
            out.extend(p.tokens)
        return out

    @property
    def text(self) -> str:
        return "\n".join(p.text for p in self.pages if p.text)

    @property
    def ok(self) -> bool:
        return any(p.tokens for p in self.pages)


@dataclass
class DimValue:
    value: float
    raw: str
    kind: str                 # diameter | radius | sphere | depth | length | angle | thread | chamfer
    x: float = 0.0
    y: float = 0.0
    page: int = 0
    count: int = 1
    through: bool = False
    label: str = ""


@dataclass
class DimSet:
    diameters: List[DimValue] = field(default_factory=list)
    radii: List[DimValue] = field(default_factory=list)
    spheres: List[DimValue] = field(default_factory=list)
    depths: List[DimValue] = field(default_factory=list)
    lengths: List[DimValue] = field(default_factory=list)
    angles: List[DimValue] = field(default_factory=list)
    threads: List[DimValue] = field(default_factory=list)
    chamfers: List[DimValue] = field(default_factory=list)
    labeled: Dict[str, float] = field(default_factory=dict)

    def numbers(self) -> List[float]:
        return [d.value for d in self.lengths]

    def summary(self) -> str:
        bits = []
        if self.lengths:
            bits.append("数字 " + "/".join(f"{v.value:g}" for v in self.lengths[:12]))
        if self.diameters:
            bits.append("孔径 " + "/".join(f"Ø{v.value:g}" for v in self.diameters[:12]))
        if self.depths:
            bits.append("深度 " + "/".join(f"{v.value:g}" for v in self.depths[:8]))
        return " · ".join(bits)


# ---------------------------------------------------------------- engine
def engine_status() -> Tuple[bool, str]:
    """不加载模型，只回答“本机有没有 RapidOCR”。"""
    try:
        import importlib.util
        ok = importlib.util.find_spec("rapidocr_onnxruntime") is not None
        return ok, "" if ok else "未安装 rapidocr-onnxruntime"
    except Exception as e:      # pragma: no cover
        return False, str(e)


def _engine():
    global _ENGINE, _ENGINE_ERROR
    if _ENGINE is not None:
        return _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is not None:
            return _ENGINE
        try:
            from rapidocr_onnxruntime import RapidOCR
            _ENGINE = RapidOCR()
            _ENGINE_ERROR = ""
        except Exception as e:
            _ENGINE_ERROR = f"{type(e).__name__}: {e}"
            _ENGINE = None
    return _ENGINE


def engine_error() -> str:
    _engine()
    return _ENGINE_ERROR


# ---------------------------------------------------------------- normalize
def normalize_text(text: str) -> str:
    """把 OCR 文本规范成可解析形式。

    注意：Q→Ø 只在“Q 后面紧跟数字”时替换，避免把材料牌号 Q235 误改成 Ø235。
    """
    s = unicodedata.normalize("NFKC", str(text or ""))
    s = s.replace("，", ",").replace("．", ".")
    s = (s.replace("Φ", "Ø").replace("φ", "Ø").replace("∅", "Ø")
          .replace("⌀", "Ø").replace("ø", "Ø").replace("Ø", "Ø"))
    s = s.replace("×", "x").replace("✕", "x").replace("X", "x")
    s = re.sub(r"\s+", " ", s).strip()
    # 技术要求里的条目编号常与首个数粘在一起：1.6×Ø10 -> 6×Ø10。
    # 只在“编号 + 数字×数字”这种明确形态下剥离，避免误伤 1.5 这类尺寸。
    s = re.sub(r"^[1-9]\d?\s*[.、)]\s*(?=\d+\s*[×xX*]\s*[ØΦφ0-9])", "", s)
    # 逗号小数：1,75 / 0,125。机械图不使用千分位逗号。
    s = re.sub(r"(?<=\d),(?=\d)", ".", s)
    # OCR 把 Ø 读成 Q/O，仅在数字前修正；Q235/Q345 等材料牌号先保护起来。
    s = re.sub(r"\b[Qq](195|215|235|275|345|390|420|460)\b", r"MATGRADE\1", s)
    s = re.sub(r"(?<![A-Za-z0-9])[QOo](?=\s*\d)", "Ø", s)
    s = re.sub(r"MATGRADE(\d{3})", r"Q\1", s)
    # 竖排标注常把 Ø17.5 读成 017.5；机械图不用前导零表示尺寸。
    # 前导 0 的直径：OCR 常把 Ø 读成 0（090 / 0120 / 6x010）。
    # 机械图不用前导零表示尺寸，所以“0 + 2~3 位整数”可安全视作直径；
    # 右侧不能再接数字或小数点，避免误伤 0.5 这类小数。
    s = re.sub(r"(?<![\d.])0(?=\d{2,3}(?![\d.]))", "Ø", s)
    # 常见分隔符
    s = re.sub(r"(?<=[ØRr])\s+(?=\d)", "", s)
    return s


def _read_ocr_result(result: Any, page: int) -> List[OcrToken]:
    toks: List[OcrToken] = []
    if not result:
        return toks
    # RapidOCR: [[box, text, score], ...]；也兼容 (result, elapse)
    rows = result
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], (list, tuple)):
        rows = result[0]
    for item in (rows or []):
        try:
            box, text, score = item[0], item[1], item[2]
            if isinstance(box, (list, tuple)) and box and isinstance(box[0], (list, tuple)):
                xs = [float(p[0]) for p in box]
                ys = [float(p[1]) for p in box]
                b = (min(xs), min(ys), max(xs), max(ys))
            else:
                b = tuple(float(v) for v in box)
            t = str(text or "").strip()
            if not t:
                continue
            toks.append(OcrToken(t, normalize_text(t), float(score or 0.0), b, page))
        except Exception:
            continue
    toks.sort(key=lambda t: (round(t.cy / 12.0), t.cx))
    return toks


def _bbox_iou(a, b) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    ua = max(1e-9, (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - inter)
    return inter / ua


def _rot_token(t: OcrToken, ang: int, w: int, h: int, page: int) -> OcrToken:
    """把旋转图上读到的 token 坐标映射回原图。"""
    x1, y1, x2, y2 = t.box
    if ang == 90:       # 旋转图上 x'=y, y'=W-1-x -> 反解 x=W-1-y', y=x'
        b = (w - 1 - y2, x1, w - 1 - y1, x2)
    elif ang == 270:    # 旋转图上 x'=H-1-y, y'=x -> 反解 x=y', y=H-1-x'
        b = (y1, h - 1 - x2, y2, h - 1 - x1)
    else:
        b = t.box
    return OcrToken(t.text, t.norm, t.conf, tuple(float(v) for v in b), page, int(ang))


def run_ocr(paths: Sequence[str], progress=None, multi_rotate: bool = False) -> OcrDoc:
    """对一组图片跑本地 OCR。非图片文件直接跳过并给 warning。

    multi_rotate=True 时额外把图旋转 90°/270° 再读一遍，用于竖排尺寸
    （如 Ø17.5）。结果按文字 + 框重叠去重，取置信度高的。
    """
    log = progress or (lambda *_a, **_k: None)
    doc = OcrDoc()
    eng = _engine()
    if eng is None:
        doc.warnings.append("本地 OCR 引擎不可用：" + (engine_error() or "未知错误"))
        return doc
    import tempfile, time
    from PIL import Image
    for i, path in enumerate(paths):
        p = Path(path)
        if p.suffix.lower() not in IMAGE_EXT:
            doc.warnings.append(f"本地 OCR 只处理图片，跳过 {p.name}")
            continue
        page = OcrPage(index=i, path=str(p))
        log(f"🔍 本地 OCR：{p.name}…")
        t0 = time.time()
        try:
            result, _elapse = eng(str(p))
            page.tokens = _read_ocr_result(result, i)
        except Exception as e:
            page.ok = False
            page.error = f"{type(e).__name__}: {e}"
            doc.warnings.append(f"{p.name} OCR 失败：{page.error}")
            log(f"   ⚠ OCR 失败：{page.error}")
            doc.pages.append(page)
            continue
        if multi_rotate:
            try:
                im = Image.open(str(p)).convert("RGB")
                w, h = im.size
                for ang in (90, 270):
                    tmp = Path(tempfile.gettempdir()) / f"_ocr_rot_{ang}_{p.stem[:20]}.png"
                    im.rotate(ang, expand=True).save(tmp)
                    try:
                        r2, _ = eng(str(tmp))
                        more = [_rot_token(t, ang, w, h, i)
                                for t in _read_ocr_result(r2, i)]
                        for t in more:
                            dup = None
                            best_iou = 0.0
                            for k in page.tokens:
                                _iou = _bbox_iou(k.box, t.box)
                                if _iou > best_iou:
                                    best_iou, dup = _iou, k
                            if dup is None or best_iou <= 0.15:
                                page.tokens.append(t)
                                continue
                            # 同一位置、不同文字的旋转补读与正视读冲突时，正视读优先。
                            # 实测：正视读出的 “Ø90” 被 90° 补读成 “Ø60”，两条都保留
                            # 会让模型以为图上真有一个 Ø60，进而判图纸矛盾。
                            if (t.rot and not dup.rot and best_iou >= 0.45
                                    and dup.conf >= 0.45):
                                continue
                            _same = (dup.norm == t.norm
                                     or (dup.norm and t.norm
                                         and (dup.norm in t.norm or t.norm in dup.norm)))
                            if _same and t.conf > dup.conf:
                                dup.text, dup.norm, dup.conf, dup.box = (
                                    t.text, t.norm, t.conf, t.box)
                    finally:
                        try:
                            tmp.unlink()
                        except Exception:
                            pass
            except Exception as e:
                doc.warnings.append(f"{p.name} 旋转补读失败：{e}")
        page.elapsed = time.time() - t0
        page.tokens.sort(key=lambda t: (round(t.cy / 12.0), t.cx))
        page.text = " ".join(t.text for t in page.tokens)
        page.ok = bool(page.tokens)
        log(f"   → {len(page.tokens)} 个文本块，{page.elapsed:.1f}s")
        doc.pages.append(page)
    return doc


# ---------------------------------------------------------------- lines
def group_lines(tokens: Sequence[OcrToken], y_tol: float = 0.55) -> List[OcrLine]:
    """按 y 中心聚成文本行。OCR 经常把 “Ø” 和 “30” 分成两个块。"""
    by_page: Dict[int, List[OcrToken]] = {}
    for t in tokens:
        by_page.setdefault(t.page, []).append(t)
    lines: List[OcrLine] = []
    for pg, items in by_page.items():
        items = sorted(items, key=lambda t: (t.cy, t.cx))
        used = [False] * len(items)
        for i, t in enumerate(items):
            if used[i]:
                continue
            group = [t]
            used[i] = True
            changed = True
            while changed:
                changed = False
                y0 = sum(x.cy for x in group) / len(group)
                ht = max(x.h for x in group)
                for j, u in enumerate(items):
                    if used[j]:
                        continue
                    if abs(u.cy - y0) <= max(6.0, y_tol * ht):
                        group.append(u)
                        used[j] = True
                        changed = True
            group.sort(key=lambda x: x.cx)
            x1 = min(x.box[0] for x in group)
            y1 = min(x.box[1] for x in group)
            x2 = max(x.box[2] for x in group)
            y2 = max(x.box[3] for x in group)
            text = " ".join(x.norm for x in group)
            lines.append(OcrLine(text, (x1, y1, x2, y2), pg, group))
    lines.sort(key=lambda l: (l.page, round(l.cy / 12.0), l.cx))
    return lines


# ---------------------------------------------------------------- parse
_DIA_RE = re.compile(r"(?:(?P<count>\d{1,2})\s*[x*\-]\s*)?Ø\s*(?P<v>\d+(?:\.\d+)?)", re.I)
_SR_RE = re.compile(r"\bSR\s*(?P<v>\d+(?:\.\d+)?)", re.I)
_R_RE = re.compile(r"(?<![A-Za-z])R\s*(?P<v>\d+(?:\.\d+)?)", re.I)
_DEPTH_RE = re.compile(r"(?:深|深度|孔深)\s*[:：]?\s*(?P<v>\d+(?:\.\d+)?)")
_LABEL_RE = re.compile(r"(?:长|宽|厚|高|外径|内径|直径)\s*[:：]?\s*(?P<v>\d+(?:\.\d+)?)")
_ANGLE_RE = re.compile(r"(?P<v>\d+(?:\.\d+)?)\s*°")
_THREAD_RE = re.compile(r"\bM\s*(?P<v>\d+(?:\.\d+)?)")
_CHAM_RE = re.compile(r"(?<![A-Za-z])C\s*(?P<v>\d+(?:\.\d+)?)(?![\d.])")
_NUM_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)(?![\d.])")


def _spans(text: str, regex: re.Pattern) -> List[Tuple[int, int]]:
    return [(m.start(), m.end()) for m in regex.finditer(text)]


def parse_dimensions(doc: OcrDoc) -> DimSet:
    """从 OCR 文本行提取标注。所有规则都是确定性的。"""
    ds = DimSet()
    lines = group_lines(doc.tokens)
    for ln in lines:
        s = normalize_text(ln.text)
        if not s:
            continue
        through = bool(re.search(r"通孔|钻通|穿通|贯穿|\b通\b", s))
        excl: List[Tuple[int, int]] = []

        for m in _DIA_RE.finditer(s):
            try:
                v = float(m.group("v"))
            except Exception:
                continue
            cnt = int(m.group("count") or 1)
            ds.diameters.append(DimValue(v, m.group(0), "diameter", ln.cx, ln.cy, ln.page,
                                         max(1, cnt), through))
            excl.append((m.start(), m.end()))
        for m in _SR_RE.finditer(s):
            try:
                v = float(m.group("v"))
            except Exception:
                continue
            ds.spheres.append(DimValue(v, m.group(0), "sphere", ln.cx, ln.cy, ln.page, 1, False))
            excl.append((m.start(), m.end()))
        for m in _R_RE.finditer(s):
            if any(a <= m.start() < b for a, b in excl):
                continue
            try:
                v = float(m.group("v"))
            except Exception:
                continue
            ds.radii.append(DimValue(v, m.group(0), "radius", ln.cx, ln.cy, ln.page, 1, False))
            excl.append((m.start(), m.end()))
        for m in _DEPTH_RE.finditer(s):
            try:
                v = float(m.group("v"))
            except Exception:
                continue
            ds.depths.append(DimValue(v, m.group(0), "depth", ln.cx, ln.cy, ln.page, 1, False))
            excl.append((m.start(), m.end()))
        for m in _LABEL_RE.finditer(s):
            try:
                v = float(m.group("v"))
            except Exception:
                continue
            # 只认紧邻数字前的标签；不能把整行/整张图里出现过的
            # “长/宽/厚”套到所有裸数字上（否则最后一个裸数字会覆盖标注）。
            key = s[max(0, m.start() - 6):m.start()]
            label = ""
            for k, lab in (("长", "length"), ("宽", "width"), ("厚", "thickness"),
                           ("高", "height"), ("外径", "diameter"), ("内径", "diameter"),
                           ("直径", "diameter")):
                if k in key:
                    label = lab
                    break
            ds.lengths.append(DimValue(v, m.group(0), "length", ln.cx, ln.cy, ln.page,
                                       1, False, label or "length"))
            if label:
                ds.labeled[label] = v
            excl.append((m.start(), m.end()))
        for m in _ANGLE_RE.finditer(s):
            try:
                ds.angles.append(DimValue(float(m.group("v")), m.group(0), "angle",
                                          ln.cx, ln.cy, ln.page))
            except Exception:
                pass
            excl.append((m.start(), m.end()))
        for m in _THREAD_RE.finditer(s):
            try:
                ds.threads.append(DimValue(float(m.group("v")), m.group(0), "thread",
                                           ln.cx, ln.cy, ln.page))
            except Exception:
                pass
            excl.append((m.start(), m.end()))
        for m in _CHAM_RE.finditer(s):
            if any(a <= m.start() < b for a, b in excl):
                continue
            try:
                ds.chamfers.append(DimValue(float(m.group("v")), m.group(0), "chamfer",
                                            ln.cx, ln.cy, ln.page))
            except Exception:
                pass
            excl.append((m.start(), m.end()))

        # 裸数字：排除已识别的 Ø/R/深/角/螺纹/倒角，以及图号、比例、精度、粗糙度
        for m in _NUM_RE.finditer(s):
            a, b = m.start(), m.end()
            if any(x <= a < y for x, y in excl):
                continue
            tok = m.group(1)
            # 材料牌号/代号里的数字不是尺寸：HT200、Q235、M12…
            if a > 0 and re.match(r"[A-Za-z]", s[a - 1]):
                continue
            before = s[max(0, a - 4):a]
            after = s[b:b + 4]
            if re.search(r"(?:Ra|HRC|IT|GB|JB|图号|比例|第|页|共)\s*$", before, re.I):
                continue
            if re.match(r"\s*(?:[:：]\s*\d|H\d|h\d|g\d|f\d|k\d|m\d|js\d|°|%)", after, re.I):
                continue
            try:
                v = float(tok)
            except Exception:
                continue
            if not (0.1 <= v <= 5000):
                continue
            ds.lengths.append(DimValue(v, tok, "length", ln.cx, ln.cy, ln.page, 1, False))

    # 深度就近挂到孔径上：同一行优先，其次 220px 内。
    for d in ds.diameters:
        if d.through:
            continue
        best = None
        for dep in ds.depths:
            if dep.page != d.page:
                continue
            dist = math.hypot(dep.x - d.x, dep.y - d.y)
            if dist > 260:
                continue
            if best is None or dist < best[0]:
                best = (dist, dep.value)
        if best:
            d.label = f"深{best[1]:g}"

    # 去重裸数字：同一个数出现多次（尺寸线两端、明细栏）只保留一次，
    # 但保留出现次数，供“4×Ø12”这类计数使用。
    return ds


def depth_for(ds: DimSet, dia: float, page: int = -1, tol: float = 0.12) -> Optional[float]:
    best = None
    for d in ds.diameters:
        if page >= 0 and d.page != page:
            continue
        if abs(d.value - dia) <= max(0.25, tol * max(dia, 1.0)):
            if d.label.startswith("深"):
                try:
                    v = float(d.label[1:])
                except Exception:
                    continue
                if best is None or v > best:
                    best = v
    return best
