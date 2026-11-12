"""Drawing -> PartSpec via a vision LLM (OpenAI-compatible / DashScope)."""
from __future__ import annotations

import base64
import io
import json
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .config import Settings
from .models import PartSpec

SYSTEM_PROMPT = """你是资深机械工艺工程师，擅长读机械零件图与装配图。
任务：从图纸中提取可三维建模的几何参数，输出严格 JSON（不要 markdown 代码块）。
规则：
1) 单位一律 mm，英寸等换算成 mm。
2) 坐标系：零件底面中心为原点，X 向右、Y 向上（俯视图）、Z 向上；x/y 为相对中心坐标。
3) 只取图纸上明确标注或可由视图严格推算的尺寸，不臆造；缺省值在 notes 里注明"需人工确认"。
4) 孔逐个列出；圆周分布时用 bolt_circle 描述（count/diameter/hole_dia/cx/cy）。
5) 多视图要交叉核对总长/总宽/总高，不能自相矛盾。
6) 如果图纸是装配图（有明细表、多个零件序号），drawing_type 填 "assembly"，并在 parts 中列出每个能识别的零件；
   同时把最适合用 2.5D 方式建模的零件（圆盘类/板类，如端盖、透盖、垫片、挡油环）填到 "part" 字段里。
   装配图上属于**装配关系**的线（配合面、剖切线、其它零件的轮廓、装配总长/总宽/中心距）
   一律不能当作本零件的特征；features 只允许出现该零件自身图纸上的孔/槽/台阶。
7) 如果图纸是单个零件，drawing_type 填 "part"，parts 可为空数组。
8) 严禁编造图纸上没有的孔/槽/台阶：features 只能包含图上确实画出并标注的特征。
9) 如果使用了 bolt_circle 描述圆周孔，就不要在 features 里再重复列这些孔。
10) 孔坐标必须由标注尺寸推算（例如标注 4×Ø9、孔距 130×76，则孔在 ±65、±38）。
11) 同心圆（同一中心的多条直径）只保留**真正要切出来的那个孔**：轴承孔一般填一通孔 +
    最多一级沉孔/台阶孔；外圆直径、凸台直径、倒角尺寸都不是孔，不要各列一个孔。
    例：图上标 Ø80/Ø70/Ø68/Ø62H7/Ø47H7 时，通孔填 Ø62（H7），沉孔填 Ø68×2，其余不列。
12) 每个孔的坐标必须写清是从哪个基准量的：datum 填 center / pocket_wall / step_edge /
    bolt_circle / unknown 之一，datum_ref 写清基准对象（例如"台阶边 x=79"、"型腔168×74 的壁"）。
    严禁把"相对某个壁偏移的量"直接当成绝对坐标填写。
13) 特征之间要留可加工间隙：若两个特征边缘只差零点几毫米（相切/薄壁），按图纸意图取一个
    （通常是一个开口），不要同时列出两个相切的孔/槽。
14) 曲线只用 CAD 原生几何：直线给两端点，圆弧给两端点 + 圆心 + direction(+1/-1)；
    严禁用点列/多段折线拟合曲线，也不要把大半径弧降级成 fillet。
15) 回转体（杯/套/锥壳/圆盘法兰等）用 revolve 的闭合母线：r 为半径(>=0)，z 为高度，
    段类型只有 line / arc；不得用 pocket 近似内腔。
16) SR/SΦ 是球面半径/球面直径，不是孔：球坑用 sphere(D=2×SR, depth)，严禁写成 hole。
17) 单端/双端圆弧头（U 形叉、摇臂、连杆）用 base_rect 的 end_left/end_right="arc"；
    手柄/弯臂用 sweep；顶面拱起用 dome；等壁厚内腔用 shell。"""

USER_PROMPT = """请识别这张图纸，输出如下 JSON：
{
  "drawing_type": "part 或 assembly",
  "part": {
    "part_name": "零件名称",
    "part_type": "plate|bracket|flange|block",
    "material": "材料，未知填 45钢",
    "base_kind": "rect 或 round",
    "length": 总长, "width": 总宽, "thickness": 总厚,
    "diameter": 圆盘类外径（非圆盘填 0）,
    "corner_radius": 矩形四角圆角（无则 0）,
    "end_left": "square 或 arc", "end_right": "square 或 arc",
    "shell_t": 抽壳壁厚（无则 0）, "shell_open": "top 或 none",
    "dome_h": 顶面拱高（无则 0）, "dome_elliptic": true,
    "features": [
      {"type":"hole","x":0,"y":0,"diameter":10,"depth":0,"through":true,
       "counterbore_dia":0,"counterbore_depth":0,
       "datum":"center|pocket_wall|step_edge|bolt_circle|unknown",
       "datum_ref":"这个坐标是相对什么量出来的（如 型腔168×74 的壁 / 台阶边 x=79 / 螺栓分布圆Ø130）",
       "note":"Φ10 通孔"},
      {"type":"pocket","x":0,"y":0,"length":40,"width":20,"depth":5,"corner_radius":2},
      {"type":"slot","x":0,"y":0,"length":30,"width":10,"depth":0,"through":true,"angle":0},
      {"type":"step","x":0,"y":0,"length":80,"width":25,"depth":15}
    ],
    "bolt_circle": {"count":4,"diameter":50,"hole_dia":6.6,"cx":0,"cy":0},
    "sweeps": [],
    "revolve_profile": [],
    "notes": "技术要求、公差等",
    "confidence": 0.0
  },
  "parts": [
    {"no":1,"name":"端盖","qty":2,"material":"HT150","note":"可选，仅装配图需要","modelable":true}
  ],
  "steps": [
    {"op":"base_rect","params":{"L":150,"W":75,"H":25,"corner_radius":0},
     "note":"先建毛坯外形","evidence":"主视图 150×75，左视图厚 25","confidence":0.9},
    {"op":"pocket","params":{"x":0,"y":0,"L":80,"W":40,"depth":5},
     "evidence":"俯视图 80×40，剖面 A-A 深 5","confidence":0.85},
    {"op":"hole","params":{"x":0,"y":0,"dia":30,"depth":9,"through":false},
     "evidence":"主视图 Ø30，剖面深 9","confidence":0.8},
    {"op":"chamfer","params":{"size":5},"evidence":"技术要求 C5","confidence":0.9}
  ],
  "notes": "如果信息不足，说明缺什么"
}
注意：
- depth=0 且 through=true 表示通孔/通槽；沉头孔用 counterbore_dia/counterbore_depth。
- 螺纹孔按底孔直径填写并在 note 写 "M8 螺纹孔"。
- 圆盘零件 base_kind 填 round，diameter 填外径，length/width 填直径。
- 孔坐标严格按标注推算，不要用"外形尺寸减去余量"猜。
- steps 是**第一产物**：按"人的建模顺序"给出（新建基体 → 型腔/台阶/槽 → 孔/阵列 → 倒角/圆角），
  每步必须给 evidence（图纸上哪条标注支撑这一步）和 confidence（0~1）。
- 可用的 op（全部已由建模引擎实现）：
  基体：base_rect(L,W,H,corner_radius,end_left,end_right) / base_round(D,H) /
        revolve(profile,D,H) / shaft(segments) / sheet(L,W,t,flange_h,bend_r) /
        gear(module,teeth,face_width,bore,keyway_w,keyway_d)
  切除：pocket(x,y,L,W,depth,corner_radius) / slot(x,y,L,W,depth,through,angle) /
        step_cut(x,y,L,W,depth) / open_slot(L,W,depth,side) / sphere(x,y,D,depth) /
        shell(t,open)
  加料：boss(x,y,L,W,depth) / sweep(profile_w,profile_h,path) / dome(h,elliptic)
  孔：  hole(x,y,dia,depth,through,cb_dia,cb_depth) /
        pattern_rect(cx,cy,nx,ny,dx,dy,dia,through) /
        pattern_circle(cx,cy,count,bcd,dia,through) / mirror(axis)
  边：  chamfer(size) / fillet(radius)
- 曲线段只能写：{"type":"line","p1":[x,y],"p2":[x,y]} 或
  {"type":"arc","p1":[x,y],"p2":[x,y],"center":[x,y],"direction":1}；
  revolve 的坐标是 (r,z)，sweep 的 path 是俯视 (x,y)。
- 单端圆弧头：base_rect 的 end_left="arc" 表示左端半圆（半径 W/2），
  end_right="square" 保持方角；圆弧头是草图里的真圆弧，不要再叠 corner_radius。
- 同一组孔只能用 pattern_* 或逐个 hole，不能两边都写。
- 拿不准的步骤也要列出来，confidence 给低值，并在 note 里写缺什么标注。
- 只输出 JSON。"""

PAGE_PROMPT = """这是多页机械图纸。请逐页判断每一页是什么，只输出 JSON：
{"pages":[{"page":1,"type":"assembly|part|detail|text|other","title":"该页标题/零件名称","reason":"一句判断依据"}]}
判断依据：有明细表(序号/名称/数量)、多个零件序号、装配尺寸 → assembly；
单一零件的三视图 + 尺寸标注 + 标题栏 → part；只有技术要求/说明文字 → text。"""

OCR_PROMPT = """把这张机械图纸上的所有文字、尺寸标注、公差、标题栏、明细表内容完整提取出来，
按"视图 → 标注"分组输出纯文本，不要遗漏任何数字。保留 Φ、±、°、Ra 等符号。"""


@dataclass
class AnalysisResult:
    spec: PartSpec
    drawing_type: str = "part"
    parts: List[dict] = field(default_factory=list)
    notes: str = ""
    raw: str = ""
    page_index: int = 0                 # 实际用于建模的页（0 起）
    page_count: int = 1
    page_type: str = "part"
    page_title: str = ""
    pages: List[dict] = field(default_factory=list)
    uncertain: List[str] = field(default_factory=list)
    # 阻断项：非空则不应直接建模（例如装配图上零件尺寸没依据）。
    # 与 uncertain 的区别：uncertain 是"提示，可继续"，blocking 是"停下，先给人看"。
    blocking: List[str] = field(default_factory=list)
    steps: List = field(default_factory=list)      # 识别后拆出的建模步骤


def _b64_image(path: str, max_px: int = 2200) -> str:
    from PIL import Image, ImageOps
    im = Image.open(path).convert("RGB")
    if max(im.size) < 1400:
        f = 1400.0 / max(im.size)
        im = im.resize((int(im.width * f), int(im.height * f)), Image.LANCZOS)
    if max(im.size) > max_px:
        f = max_px / float(max(im.size))
        im = im.resize((int(im.width * f), int(im.height * f)), Image.LANCZOS)
    im = ImageOps.autocontrast(im, cutoff=1)
    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


def pdf_to_images(path: str, out_dir: str, max_pages: int = 12) -> List[str]:
    import pymupdf as fitz
    doc = fitz.open(path)
    out = []
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    for i in range(min(len(doc), max_pages)):
        pix = doc[i].get_pixmap(dpi=200)
        p = d / f"{Path(path).stem}_p{i+1}.png"
        pix.save(str(p))
        out.append(str(p))
    doc.close()
    return out


def _strip_json(text: str) -> str:
    t = text.strip()
    t = re.sub(r"^```(?:json)?", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        return t[i:j + 1]
    return t


class DrawingAnalyzer:
    def __init__(self, settings: Settings, log: Optional[Callable[[str], None]] = None):
        self.s = settings
        self.log = log or (lambda m: None)

    def _client(self):
        from openai import OpenAI
        return OpenAI(api_key=self.s.api_key, base_url=self.s.base_url,
                      timeout=self.s.timeout, max_retries=1)

    def _chat(self, model: str, content, temperature: float = 0.05) -> str:
        client = self._client()
        r = client.chat.completions.create(model=model, messages=content,
                                           temperature=temperature)
        return (r.choices[0].message.content or "").strip()

    def prepare_images(self, paths: Sequence[str], work_dir: str) -> List[str]:
        imgs: List[str] = []
        for p in paths:
            ext = Path(p).suffix.lower()
            if ext == ".pdf":
                self.log(f"PDF 转图片：{Path(p).name}")
                imgs.extend(pdf_to_images(p, work_dir))
            elif ext in (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"):
                imgs.append(p)
        return imgs

    def ocr(self, image_paths: Sequence[str]) -> str:
        content = [{"type": "text", "text": OCR_PROMPT}]
        for p in image_paths[:4]:
            content.append({"type": "image_url",
                            "image_url": {"url": "data:image/png;base64," + _b64_image(p)}})
        try:
            return self._chat(self.s.ocr_model, [{"role": "user", "content": content}], 0.0)
        except Exception as e:
            self.log(f"OCR 调用失败：{e}")
            return ""

    def _build_content(self, imgs: Sequence[str], hint: str, extra: str = ""):
        text = USER_PROMPT
        if hint.strip():
            text += f"\n\n用户补充（优先级最高）：{hint.strip()}"
        if extra:
            text += "\n\n" + extra
        content = [{"type": "text", "text": text}]
        for p in imgs[:6]:
            content.append({"type": "image_url",
                            "image_url": {"url": "data:image/png;base64," + _b64_image(p)}})
        return content

    def classify_pages(self, imgs: Sequence[str],
                       on_progress: Optional[Callable[[str], None]] = None) -> List[dict]:
        """多页图纸逐页判断类型（装配图 / 零件图 / 文字页）。"""
        prog = on_progress or self.log
        content = [{"type": "text", "text": PAGE_PROMPT + f"\n共 {len(imgs)} 页，按顺序给出。"}]
        for p_ in imgs[:12]:
            content.append({"type": "image_url",
                            "image_url": {"url": "data:image/png;base64," + _b64_image(p_)}})
        try:
            raw = self._chat(self.s.vision_model, [{"role": "user", "content": content}], 0.0)
            data = json.loads(_strip_json(raw))
            pages = list(data.get("pages") or [])
            if pages:
                prog("分页识别：" + "；".join(
                    f"第{p.get('page')}页 {p.get('type')}"
                    + (f"『{p.get('title')}』" if p.get("title") else "") for p in pages))
            return pages
        except Exception as e:
            prog(f"分页识别跳过（{e}）")
            return []

    @staticmethod
    def _wanted_page(hint: str) -> Optional[int]:
        m = re.search(r"第\s*(\d+)\s*页", hint or "")
        if m:
            return int(m.group(1)) - 1
        return None

    def _pick_page(self, pages: List[dict], hint: str,
                   page: Optional[int]) -> Tuple[int, str, str]:
        """返回 (索引, 类型, 标题)。"""
        if not pages:
            return 0, "part", ""
        if page is not None and 0 <= page < len(pages):
            p_ = pages[page]
            return page, str(p_.get("type") or "part"), str(p_.get("title") or "")
        want = self._wanted_page(hint)
        if want is not None and 0 <= want < len(pages):
            p_ = pages[want]
            return want, str(p_.get("type") or "part"), str(p_.get("title") or "")
        for i, p_ in enumerate(pages):
            title = str(p_.get("title") or "")
            if title and title in (hint or ""):
                return i, str(p_.get("type") or "part"), title
        # 优先"只画一个零件"的页（标题里出现多个零件名 / 顿号分隔的跳过）
        def single(p_):
            tt = str(p_.get("title") or "")
            return bool(tt) and "、" not in tt and "," not in tt and "，" not in tt
        for i, p_ in enumerate(pages):
            if str(p_.get("type")) == "part" and single(p_):
                return i, "part", str(p_.get("title") or "")
        for i, p_ in enumerate(pages):
            if str(p_.get("type")) == "part":
                return i, "part", str(p_.get("title") or "")
        p0 = pages[0]
        return 0, str(p0.get("type") or "part"), str(p0.get("title") or "")

    @staticmethod
    def _uncertain_items(ocr_text: str, spec: PartSpec) -> List[str]:
        """拿 OCR 文字里的公差孔径/孔数，检查模型有没有漏。"""
        out: List[str] = []
        if not ocr_text:
            return out
        have = {round(float(f.diameter), 2) for f in spec.features if f.diameter > 0}
        if getattr(spec, "base_kind", "") == "round" and spec.diameter:
            have.add(round(float(spec.diameter), 2))
        for s_ in re.findall(r"[Φφ⌀]\s*(\d+(?:\.\d+)?)\s*[HhKkGgFf][0-9]?", ocr_text):
            d_ = float(s_)
            if not any(abs(d_ - h) <= 0.01 for h in have):
                out.append(f"图纸标注配合孔 Ø{d_:g}(H7/H8 等) 未出现在模型里，需人工确认")
        # 孔数自洽性统一由 _feature_consistency 处理（它会降 confidence 并阻断），
        # 这里不再重复输出同一条提示。
        uniq, seen = [], set()
        for x in out:
            if x not in seen:
                seen.add(x)
                uniq.append(x)
        return uniq

    @staticmethod
    def sanitize_spec(spec: PartSpec) -> List[str]:
        """识图产物的可制造性约束（在进 CAD / CAM 之前就把不可信的坐标拿掉）。

        1) 被当成孔的节圆直径尺寸（如 Ø90 均布 6×Ø10 的 Ø90）→ 剔除
        2) 同轴同心的一串孔径 → 合并成"通孔 + 一级沉孔"
        3) 完全落在毛坯之外的孔/槽（例如 Y=±79 落在 100 宽的箱座上）→ 丢弃
        4) 孔心/轮廓贴在别的特征壁面上（<0.5mm，切完是零厚度尖角）→ 丢弃
        返回给人看的说明，同时写进 trace，报告里能看到"识图改了哪些东西"。
        """
        notes: List[str] = []
        # corner_radius（平面四角圆角）与 fillet（顶面外轮廓圆角）在矩形板上是同一件事，
        # 同时给会被各扣一次体积（实测 +4%）。以 corner_radius 为准。
        if spec.base_kind == "rect" and spec.corner_radius > 0 and spec.fillet > 0:
            notes.append(
                f"图上四角 R{spec.corner_radius:g} 已按基体圆角建模，"
                f"顶面整圈圆角 R{spec.fillet:g} 属重复表达，已忽略")
            spec.fillet = 0.0
        # 圆角半径的物理上限：刀尖最低 2R，R > T/2 会扎穿底面；
        # 内偏轮廓最多内缩 R，R >= min(L,W)/2 会把料宽整个吞掉。
        # 不钳制的话 CAM 会报「刀路越出毛坯 / 最大切深超限」，而 CAD 建的是另一个实体。
        if spec.fillet > 0:
            try:
                from .calc import fillet_limit
                _lim = fillet_limit(spec)
                if _lim > 0 and spec.fillet > _lim + 1e-9:
                    notes.append(
                        f"圆角 R{spec.fillet:g} 超过可加工上限 R{_lim:g}"
                        f"（min(T/2, min(L,W)/2)），已钳制为 R{_lim:g}")
                    spec.fillet = round(_lim, 4)
            except Exception:
                pass
        # through=True 但 depth>0：布尔标志和深度打架。
        # 实测 m1：Ø35 通孔被识图写成 depth=5，而沉孔信息（Ø40 深12）另外给了，
        # 主孔那个 depth=5 没有任何物理含义；但 Feature.is_through() 是「深度优先」，
        # 会把 5 当真，于是建模只挖 5mm、NC 却钻穿 → 体积差 75,684（-7.74%）。
        #
        # 判据要严，否则会误清真正的盲孔：
        #   1) through=True（真盲孔不会同时标通）
        #   2) depth 小于厚度 90%
        #   3) 已有 counterbore_depth（说明深度信息另有出处，主孔这个 depth 是噪声）
        #   4) depth 小于 counterbore_depth 的 2 倍（噪声深度的典型特征：
        #      从沉孔深度误填过来，量级接近）
        # 同时满足四条才清。r3 的 Ø20 depth=28 有沉孔 Ø43 深10，
        # 28 > 10*2，不满足第 4 条 → 保留，不会误清。
        _fixed_depths = []
        for _f in spec.features:
            if getattr(_f, "type", "") != "hole":
                continue
            if not bool(getattr(_f, "through", False)):
                continue
            _d = float(getattr(_f, "depth", 0.0) or 0.0)
            _cbd = float(getattr(_f, "counterbore_depth", 0.0) or 0.0)
            if not (0.0 < _d < float(spec.thickness) * 0.9):
                continue
            if _cbd <= 0.0:
                continue
            if _d >= _cbd * 2.0:
                continue
            _fixed_depths.append((_f, _d))
            _f.depth = 0.0
        if _fixed_depths:
            _names = "、".join(
                f"Ø{float(_f.diameter or 0):g} 深{_d:g}" for _f, _d in _fixed_depths)
            notes.append(
                f"识图给了通孔却带深度（{_names}），已按通孔处理："
                f"该深度无物理含义（沉孔深度应用 counterbore_depth）")
        try:
            for w in spec.drop_pcd_pseudo_holes():
                notes.append(w)
        except Exception as e:
            notes.append(f"节圆尺寸剔除跳过：{e}")
        try:
            for w in spec.merge_concentric_holes():
                notes.append(w)
        except Exception as e:
            notes.append(f"同心孔合并跳过：{e}")

        hl, hw = spec.length / 2.0, spec.width / 2.0

        # 毛坯与孔位一致性：一半以上的孔都越界，说明毛坯尺寸本身可能读错
        # （实测 n3：27×14 读成 14×13，两个 Ø8@±7 全被判越界丢弃）
        _holes = [f for f in spec.features if f.type == "hole"]
        _out = 0
        for f in _holes:
            r = f.diameter / 2.0
            if spec.base_kind == "rect":
                if abs(f.x) + r > hl + 0.5 or abs(f.y) + r > hw + 0.5:
                    _out += 1
            elif abs(f.x) + r > spec.diameter / 2.0 + 0.5:
                _out += 1
        if len(_holes) >= 2 and _out * 2 >= len(_holes):
            notes.append(
                f"毛坯尺寸可能算错：{len(_holes)} 个孔里有 {_out} 个落在毛坯 "
                f"{spec.length:g}×{spec.width:g} 之外（孔坐标最大 ±{max(max(abs(h.x), abs(h.y)) for h in _holes):g}）。"
                f"请核对总长/总宽是否被读错（例如把 27 读成 14），不要只信孔的剔除结果")
            try:
                spec.dropped_features = list(getattr(spec, "dropped_features", None) or []) + [notes[-1]]
            except Exception:
                pass

        keep: List = []
        for f in spec.features:
            if f.type == "hole":
                rx = ry = f.diameter / 2.0
            else:
                rx = max(f.length, f.width or 0.0) / 2.0
                ry = (f.width or f.length) / 2.0
            inside = (abs(f.x) + rx <= hl + 0.5) and (abs(f.y) + ry <= hw + 0.5)
            if spec.base_kind == "round":
                inside = (f.x ** 2 + f.y ** 2) ** 0.5 + max(rx, ry) <= spec.diameter / 2.0 + 0.5
            if not inside:
                notes.append(f"识图给出 {f.type}@({f.x:g},{f.y:g}) 落在毛坯 "
                             f"{spec.length:g}×{spec.width:g} 之外，已丢弃（图纸坐标需人工确认）")
                continue
            keep.append(f)
        spec.features = keep

        # 贴边（零厚度尖角）：把较小那个特征拿掉；去掉一个可能又暴露出下一对，循环到稳定
        try:
            from .conflict import analyze as _a, label as _lb
            for _ in range(6):
                rep = _a(spec)
                drop = sorted(set(rep.get("ignore") or []))
                if not drop:
                    break
                for i in drop:
                    if 0 <= i < len(spec.features):
                        notes.append(f"识图给出 {_lb(spec.features[i])} 贴在其它特征壁面上"
                                     f"（零厚度尖角），已丢弃（图纸坐标需人工确认）")
                spec.features = [f for i, f in enumerate(spec.features) if i not in set(drop)]
        except Exception as e:
            notes.append(f"贴边检测跳过：{e}")
        # 只把"毛坯外"这类由本函数剔除的记进 dropped_features；
        # 贴边特征由 conflict.analyze 自己追加（同一个特征只记一次，别重复）。
        try:
            have = list(getattr(spec, "dropped_features", None) or [])
            for m in notes:
                if "落在毛坯" in m and m not in have:
                    have.append(m)
            spec.dropped_features = have
        except Exception:
            pass
        return notes

    @staticmethod
    def _scale_check(ocr_text: str, spec: PartSpec) -> List[str]:
        """模型给的总体尺寸，图上出现过吗？（防"放大/缩小"这类比例误读）

        实测两个真案例：
          n2：图上 53×30，模型给 106×30 —— 106 只以 "R106" 出现在图上
          n3：图上 27×14，模型给 14×13 —— 14 在图上另有含义
        所以不只看"这个数字有没有出现"，还要看**它是不是带 R/Ø/SR 前缀**。
        """
        if not ocr_text:
            return []
        import re as _re
        plain, prefixed = set(), set()
        for m in _re.finditer(r"([RrФФΦφØ⌀Ss]{0,2})\s*(\d+(?:[.,]\d+)?)", ocr_text):
            try:
                v = round(float(m.group(2).replace(",", ".")), 3)
            except ValueError:
                continue
            if m.group(1):
                prefixed.add(v)
            else:
                plain.add(v)
        if not plain:
            return []

        def _hit(v, pool, tol=0.002):
            return any(abs(v - n) <= max(0.02, abs(v) * tol) for n in pool)

        want = [v for v in (spec.length, spec.width, spec.thickness, spec.diameter) if v and v > 0]
        if not want:
            return []
        hard, soft = [], []
        found_any = False
        for v in want:
            if _hit(v, plain):
                found_any = True
                continue                   # 这个尺寸在图上以裸数字出现 → 可信
            if _hit(v, prefixed) and _hit(v / 2.0, plain):
                hard.append(f"{v:g} 只以 R/Ø{v:g} 出现，图上另有裸数字 {v / 2.0:g}（正好一半）")
            elif _hit(v / 2.0, plain):
                soft.append(f"{v:g} 的一半 {v / 2.0:g} 在图上，疑似把半边/半径当成整尺寸")
            elif _hit(v * 2.0, plain):
                soft.append(f"{v:g} 的 2 倍 {v * 2.0:g} 在图上，疑似把半径当直径")
        if hard:
            return ["总体尺寸疑似比例/半径误读：" + "；".join(hard) + "。请人工核对后再建模"]
        if soft:
            return ["总体尺寸需要核对：" + "；".join(soft) + "。请人工核对后再建模"]
        if found_any:
            return []                      # 至少有一个总体尺寸对得上 → 不报
        biggest = max(plain) if plain else 0
        return [f"模型给的总体尺寸 {', '.join(f'{v:g}' for v in want)} 在图纸文字里一个都没出现"
                f"（图上最大的裸数字是 {biggest:g}）。可能是比例图/单位不同，也可能是识图放大错了，"
                f"请人工核对后再建模"]

    # 图纸上常见的孔径符号/乘号写法（OCR 常把 × 认成 x、把 Ø 认成 Φ/⌀）
    _DIA_CHARS = "Ø⌀Φφϕ∅ø"
    _MUL_CHARS = "×xX*·＊"

    @classmethod
    def _hole_callouts(cls, ocr_text: str) -> Dict[float, int]:
        """从 OCR 文字提取“4×Ø9 / 4-Ø9 / Ø9×4 / 4个Ø9”这类孔标注。

        同一孔径可能被 OCR 重复输出（例如“4×Ø9”和“Ø9 通孔”），
        这里取最大数量而不是求和，避免把同一组孔算两遍。
        """
        if not ocr_text:
            return {}
        dia = "[" + cls._DIA_CHARS + "]"
        mul = "[" + cls._MUL_CHARS + "]"
        pats = [
            rf"(\d+)\s*{mul}\s*{dia}\s*(\d+(?:\.\d+)?)",
            rf"(\d+)\s*[-—–~至]\s*{dia}\s*(\d+(?:\.\d+)?)",
            rf"(\d+)\s*[个处]\s*{dia}\s*(\d+(?:\.\d+)?)",
            rf"{dia}\s*(\d+(?:\.\d+)?)\s*{mul}\s*(\d+)",
            rf"{dia}\s*(\d+(?:\.\d+)?)\s*[^\n]{{0,10}}?(\d+)\s*[个处]",
        ]
        out: Dict[float, int] = {}
        for i, pat in enumerate(pats):
            for m in re.finditer(pat, ocr_text):
                try:
                    if i == 3:
                        cnt, d_ = int(m.group(2)), float(m.group(1))
                    elif i == 4:
                        d_, cnt = float(m.group(1)), int(m.group(2))
                    else:
                        cnt, d_ = int(m.group(1)), float(m.group(2))
                except (TypeError, ValueError):
                    continue
                if not (0 < d_ < 1000 and 1 <= cnt <= 999):
                    continue
                k = round(d_, 3)
                out[k] = max(out.get(k, 0), cnt)
        return out

    @staticmethod
    def _image_hole_count(image_paths: Sequence[str], spec: PartSpec,
                          ocr_text: str = "") -> Optional[int]:
        """粗略数图纸上的小圆孔数量（不依赖 OpenCV）。

        阈值化 → 8 邻域连通域 → 只保留近方形、尺寸合理、圆环角度覆盖充分的
        连通域 → 用板件外轮廓/外圆标定 px/mm，只数直径与模型/标注接近的圆。
        返回 None 表示无法可靠标定；此时宁可跳过图像校验，也不误报。
        """
        try:
            import numpy as np
            from PIL import Image
        except Exception:
            return None
        if not image_paths:
            return None
        try:
            im = Image.open(image_paths[0]).convert("L")
        except Exception:
            return None
        w0, h0 = im.size
        if max(w0, h0) > 1400:
            k = 1400.0 / float(max(w0, h0))
            im = im.resize((max(1, int(round(w0 * k))), max(1, int(round(h0 * k)))),
                           Image.LANCZOS)
        a = np.asarray(im)
        h, w = a.shape
        if h < 40 or w < 40:
            return None
        dark = a < 120
        # 黑底白线的图纸先反相，保证下面检测的是线条而不是背景。
        if float(dark.mean()) > 0.5:
            dark = ~dark

        # ---- 连通域（8 邻域）。只遍历暗像素，图上 10 万级像素可接受。
        visited = np.zeros((h, w), dtype=bool)
        comps = []
        for y0, x0 in np.argwhere(dark):
            y0, x0 = int(y0), int(x0)
            if visited[y0, x0]:
                continue
            stack = [(y0, x0)]
            visited[y0, x0] = True
            pts = []
            while stack:
                y, x = stack.pop()
                pts.append((y, x))
                y1, y2 = max(0, y - 1), min(h, y + 2)
                x1, x2 = max(0, x - 1), min(w, x + 2)
                sub = dark[y1:y2, x1:x2] & (~visited[y1:y2, x1:x2])
                for yy, xx in np.argwhere(sub):
                    ny, nx = y1 + int(yy), x1 + int(xx)
                    visited[ny, nx] = True
                    stack.append((ny, nx))
            comps.append(pts)

        # ---- 找板件外轮廓，标定 px/mm。排除图框（面积过大）和型腔（面积过小）。
        img_area = float(w * h)
        want_ar = (spec.length / spec.width
                   if spec.base_kind != "round" and spec.width > 0 and spec.length > 0
                   else 1.0)
        best = None   # (delta, -area_ratio, scale, bbox)
        for pts in comps:
            if len(pts) < 80:
                continue
            ys = [p[0] for p in pts]
            xs = [p[1] for p in pts]
            minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
            bw, bh = maxx - minx + 1, maxy - miny + 1
            area_ratio = (bw * bh) / img_area
            if area_ratio < 0.05 or area_ratio > 0.62:
                continue
            ar = bw / float(bh)
            delta = abs(math.log(max(ar, 1e-6) / max(want_ar, 1e-6)))
            if delta > 0.45:
                continue
            if spec.base_kind == "round" and spec.diameter > 0:
                sc = bw / float(spec.diameter)
            elif spec.length > 0 and spec.width > 0:
                sc = 0.5 * (bw / float(spec.length) + bh / float(spec.width))
            else:
                continue
            if sc <= 0:
                continue
            key = (round(delta, 3), -area_ratio)
            if best is None or key < best[0]:
                best = (key, sc, (minx, miny, maxx, maxy))
        if best is None:
            return None
        scale, bbox = best[1], best[2]

        dias = {round(float(f.diameter), 3) for f in spec.features
                if f.type == "hole" and f.diameter > 0}
        for d_ in DrawingAnalyzer._hole_callouts(ocr_text):
            dias.add(round(float(d_), 3))
        # OCR 即使没写“4×”，只要写了“Ø9 孔/通孔”，也把 9 当作期望孔径；
        # 这样模型一个孔都没识别出来时，图像证据仍然能兜底。
        _dia = "[" + DrawingAnalyzer._DIA_CHARS + "]"
        for m in re.finditer(
                rf"{_dia}\s*(\d+(?:\.\d+)?)\s*[^\n]{{0,8}}(?:孔|通孔)"
                rf"|(?:孔|通孔)\s*[^\n]{{0,8}}{_dia}\s*(\d+(?:\.\d+)?)",
                ocr_text or ""):
            try:
                d_ = float(m.group(1) or m.group(2))
            except (TypeError, ValueError):
                continue
            if 0 < d_ < 1000:
                dias.add(round(d_, 3))
        exp_radii = [d_ * 0.5 * scale for d_ in dias if d_ > 0]
        if not exp_radii:
            return None

        # ---- 圆形候选：近方形连通域 + 环向覆盖。文字“0”“回”等半径很小，
        #      会被 exp_radii 的尺寸窗口排除；腰形槽/型腔因长宽比或覆盖不足被排掉。
        maxdim = float(max(w, h))
        bx1, by1, bx2, by2 = bbox
        mx, my = 0.04 * (bx2 - bx1), 0.04 * (by2 - by1)
        cand = []
        for pts in comps:
            if len(pts) < 24:
                continue
            ys = [p[0] for p in pts]
            xs = [p[1] for p in pts]
            minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
            bw, bh = maxx - minx + 1, maxy - miny + 1
            mn, mxx = min(bw, bh), max(bw, bh)
            if mn < 9 or mxx > 0.09 * maxdim:
                continue
            if mxx / float(mn) > 1.45:
                continue
            cx = (minx + maxx) * 0.5
            cy = (miny + maxy) * 0.5
            if not (bx1 - mx <= cx <= bx2 + mx and by1 - my <= cy <= by2 + my):
                continue
            arr = np.asarray(pts, dtype=float)
            rad = np.hypot(arr[:, 1] - cx, arr[:, 0] - cy)
            r_lo, r_hi = 0.26 * mn, 0.56 * mn
            sel = (rad >= r_lo) & (rad <= r_hi)
            if int(sel.sum()) < 12:
                continue
            hist, edges = np.histogram(rad[sel],
                                       bins=np.arange(r_lo, r_hi + 1.01, 1.0))
            if hist.size == 0:
                continue
            bi = int(np.argmax(hist))
            best_r = float(edges[bi] + 0.5)
            if hist[bi] < 6:
                continue
            ring = (rad >= best_r - 1.5) & (rad <= best_r + 1.5)
            if int(ring.sum()) < 8:
                continue
            ang = np.degrees(np.arctan2(arr[ring, 0] - cy,
                                        arr[ring, 1] - cx)) % 360.0
            if len(np.unique((ang / 10.0).astype(int))) / 36.0 < 0.55:
                continue
            if not any(0.62 * er <= best_r <= 1.45 * er for er in exp_radii):
                continue
            cand.append((cx, cy, best_r))

        # 同心圆（沉孔/倒角）只算一个孔。
        uniq = []
        for c in sorted(cand, key=lambda t: -t[2]):
            if all(math.hypot(c[0] - u[0], c[1] - u[1])
                   > max(3.0, 0.55 * max(c[2], u[2])) for u in uniq):
                uniq.append(c)
        return len(uniq)

    @classmethod
    def _feature_consistency(cls, ocr_text: str, image_paths: Sequence[str],
                             spec: PartSpec, hint: str = ""
                             ) -> Tuple[List[str], List[str], float]:
        """特征数量自洽性检查 + 结构完整性因子。

        两条独立证据：OCR 标注（N×Ød）和图像检圆。任一显示模型漏孔，
        就把 struct_score 降下来，并给出 uncertain / blocking；最终
        confidence = min(模型自评, struct_score)，不再让模型自评单独说了算。
        """
        uncertain: List[str] = []
        blocking: List[str] = []
        score = 1.0
        holes = [f for f in spec.features if f.type == "hole"]
        confirmed = any(k in (hint or "") for k in
                        ("已确认", "按此建模", "尺寸已核对", "confirmed"))

        # 证据 0：纯结构自洽 —— 完全重合的孔在物理上不可能。
        # 这条不依赖 OCR 也不依赖图像，是硬证据，必须最先查。
        # 实测 sample_plate 两轮各出过 5 个 Ø9 孔、其中两个坐标完全相同，
        # 而旧判据 got >= cnt 直接放过了（4 标注 vs 5 检出）。
        _seen = {}
        _dups = {}
        for f in holes:
            _k = (round(float(f.diameter), 1), round(float(f.x), 1), round(float(f.y), 1))
            _seen[_k] = _seen.get(_k, 0) + 1
        for _k, _n in _seen.items():
            if _n > 1:
                _dups[_k] = _n
        if _dups:
            score = min(score, 0.35)
            _desc = "；".join(f"Ø{k[0]:g}@({k[1]:g},{k[2]:g}) 出现 {n} 次"
                             for k, n in sorted(_dups.items()))
            msg = f"识别结果里有完全重合的孔：{_desc}（同一位置不可能有两个孔，疑似重复计数）"
            uncertain.append(msg)
            if not confirmed:
                blocking.append(
                    "特征自洽性检查未通过：" + msg
                    + "。已停止自动建模；请核对图纸孔数与孔位，"
                      "确认真实结果后写明「已确认」再重跑")

        # 证据 1：OCR 明写的 “4×Ø9” 等孔数标注。
        for d_, cnt in sorted(cls._hole_callouts(ocr_text).items()):
            got = sum(1 for f in holes
                      if abs(float(f.diameter) - d_) <= max(0.1, 0.02 * d_))
            if got > cnt:
                # 多出来不一定是错的（图纸可能另有同类孔），但配合上面的
                # 重复孔判据，这里也要留痕，不能静默放过。
                score = min(score, 0.70)
                uncertain.append(
                    f"图纸标注 {cnt}×Ø{d_:g}，模型识别出 {got} 个 Ø{d_:g} 孔"
                    f"（多 {got - int(cnt)} 个），请核对是否有重复计数")
                continue
            if got >= cnt:
                continue
            miss = int(cnt) - got
            score = min(score, 0.35 + 0.60 * (got / float(cnt)))
            msg = (f"图纸标注 {cnt}×Ø{d_:g}，模型只识别出 {got} 个 Ø{d_:g} 孔"
                   f"（少 {miss} 个）")
            uncertain.append(msg)
            if not confirmed and (got == 0 or miss >= 2 or got * 2 <= cnt):
                blocking.append(
                    "特征数量自洽性检查未通过：" + msg
                    + "。模型很可能漏识孔，已停止自动建模；请核对图纸，"
                      "确认真实孔数后写明「已确认」再重跑")

        # 证据 1.5：孔位有没有图纸出处。
        # 实测同一张图 y 在 ±25 / ±38 之间漂移，两种都是"4 孔对称数量对"，
        # 数量判据全抓不到。但图上标了孔心到边 12（=50−38），y=38 对得上、
        # y=25 对不上（25 在 OCR 里根本不存在）。
        # 只对矩形板做：圆盘类以中心线为主，坐标系不同。
        if (ocr_text and spec.base_kind == "rect"
                and spec.length > 0 and spec.width > 0):
            import re as _re2
            _nums = set()        # 所有数字（可用于"到边距离"）
            _loose = set()       # 独立出现的数字（可用于"直接标坐标"）
            # 形如 30x10 / 4×Ø9 / 80x50 的尺寸对：其中的数字是尺寸信息，
            # 不能当作"孔心坐标被直接标注"的证据。
            # 实测 y=10 会因为槽宽 30x10 里的 10 误判为"有出处"。
            _pairs = set()
            for _m in _re2.finditer(
                    r"(\d+(?:\.\d+)?)\s*[x×*X]\s*[ØøΦφ]?\s*(\d+(?:\.\d+)?)",
                    ocr_text or ""):
                for _g in (1, 2):
                    try:
                        _pairs.add(round(float(_m.group(_g)), 2))
                    except ValueError:
                        pass
            for _m in _re2.finditer(r"(\d+(?:\.\d+)?)", ocr_text or ""):
                try:
                    _v = round(float(_m.group(1)), 2)
                except ValueError:
                    continue
                _nums.add(_v)
                if _v not in _pairs:
                    _loose.add(_v)

            def _cited(v, loose=False):
                """loose=True 时只认独立出现的数字（排除尺寸对里的）。"""
                if v is None:
                    return False
                v = abs(float(v))
                pool = _loose if loose else _nums
                return any(abs(v - n) <= max(0.6, v * 0.02) for n in pool)

            _bad = []
            for f in holes:
                # x、y 必须**分别**有出处。合并成一个 any() 会被 x 短路：
                # 实测 x=65 图上标了、y=25 没标，合并写法会直接放行。
                # 直接标坐标必须用"独立出现的数字"（排除 30x10 这类尺寸对）
                _ok_x = (_cited(f.x, loose=True)
                         or _cited(spec.length / 2.0 - abs(float(f.x))))
                _ok_y = (_cited(f.y, loose=True)
                         or _cited(spec.width / 2.0 - abs(float(f.y))))
                _miss = []
                if not _ok_x:
                    _miss.append("x")
                if not _ok_y:
                    _miss.append("y")
                if _miss:
                    _bad.append(f"Ø{f.diameter:g}@({f.x:g},{f.y:g})[{'/'.join(_miss)} 无出处]")
            if _bad:
                _ratio = len(_bad) / float(len(holes))
                score = min(score, 0.45 + 0.45 * (1.0 - _ratio))
                uncertain.append(
                    "以下孔的坐标在图纸标注里找不到任何出处（直接标坐标、"
                    "或标孔心到边的距离，都对不上）：" + "、".join(_bad)
                    + "。图纸上未标注的位置不能默认对称/居中，请核对这些孔的"
                      "真实位置（图上标了哪些数字就以哪些为准）")

        # 证据 2：图像里的小圆孔数量（OCR 为空或写法没覆盖时兜底）。
        visual = cls._image_hole_count(image_paths, spec, ocr_text)
        if visual is not None and visual >= 2 and len(holes) > visual and not _dups:
            score = min(score, 0.70)
            uncertain.append(
                f"图纸图像中检出约 {visual} 个小圆孔，模型识别出 {len(holes)} 个"
                f"（多 {len(holes) - visual} 个），请核对是否有重复计数")
        if visual is not None and visual >= 2 and len(holes) < visual:
            got, miss = len(holes), visual - len(holes)
            score = min(score, 0.40 + 0.50 * (got / float(visual)))
            msg = (f"图纸图像中检出约 {visual} 个小圆孔，模型只识别出 {got} 个"
                   f"（少 {miss} 个）")
            uncertain.append(msg)
            if not confirmed and not blocking and (got <= 1 or miss >= 2
                                                   or got * 2 <= visual):
                blocking.append(
                    "特征数量自洽性检查未通过：" + msg
                    + "。模型很可能漏识孔，已停止自动建模；请核对图纸，"
                      "确认真实孔数后写明「已确认」再重跑")
        return uncertain, blocking, round(max(0.0, min(1.0, score)), 3)

    def analyze(self, paths: Sequence[str], work_dir: str, hint: str = "",
                on_progress: Optional[Callable[[str], None]] = None,
                page: Optional[int] = None) -> AnalysisResult:
        prog = on_progress or self.log
        if not self.s.api_ready:
            raise RuntimeError("未配置 API Key，请在设置里填写（可沿用 ChatGPT/Codex 的配置）。")
        imgs = self.prepare_images(paths, work_dir)
        if not imgs:
            raise RuntimeError("没有可识别的图纸（支持 PNG/JPG/PDF/DXF）。")

        # ---- 多页图纸：先分页，再挑出该建模的那一页
        pages: List[dict] = []
        page_idx, page_type, page_title = 0, "part", ""
        use_imgs = list(imgs[:6])
        if len(imgs) > 1:
            pages = self.classify_pages(imgs, prog)
            if pages:
                page_idx, page_type, page_title = self._pick_page(pages, hint, page)
                page_idx = min(page_idx, len(imgs) - 1)
                use_imgs = [imgs[page_idx]]
                prog(f"采用第 {page_idx + 1}/{len(imgs)} 页"
                     + (f"『{page_title}』" if page_title else "")
                     + f"（{page_type}）建模；如需其它页，请在提示里写「第N页」或点名零件")
                if page_type != "part":
                    prog("⚠ 这一页不是零件图：识别结果可能混入装配级尺寸，建议指定零件图所在页")
            else:
                prog("未能分页，按全部页面识别（多件图纸请用提示指定「第N页」）")

        content = self._build_content(use_imgs, hint)
        last_err: Optional[Exception] = None
        for attempt in range(1, 4):
            prog(f"AI 读图中（第 {attempt} 次）· {len(imgs)} 张视图")
            try:
                raw = self._chat(self.s.vision_model,
                                 [{"role": "system", "content": SYSTEM_PROMPT},
                                  {"role": "user", "content": content}])
            except Exception as e:
                last_err = e
                prog(f"视觉模型调用失败：{e}")
                time.sleep(2)
                continue
            try:
                data = json.loads(_strip_json(raw))
                pd = data.get("part") or data
                if not pd.get("part_name") and data.get("parts"):
                    pd = {"part_name": data["parts"][0].get("name", "零件")}
                spec = PartSpec.from_dict(pd)
                spec.confidence = float(pd.get("confidence", 0.0) or 0.0)

                # ---- 识图的第一产物是"建模步骤"：有 steps 就用它反推 spec
                steps_raw = list(data.get("steps") or [])
                steps_model = []
                if steps_raw:
                    from .steps import Step as _Step, steps_to_spec as _s2spec
                    steps_model = [_Step.from_dict(s) for s in steps_raw
                                   if isinstance(s, dict) and s.get("op")]
                    try:
                        spec = _s2spec(steps_model, spec)
                        spec.confidence = float(pd.get("confidence", 0.0) or 0.0)
                        prog("按 %d 个建模步骤反推参数" % len(steps_model))
                    except Exception as e:
                        prog(f"步骤反推失败，退回 part 字段：{e}")
                _san = self.sanitize_spec(spec)
                for _m in _san:
                    prog(f"⚠ 识图修正：{_m}")
                spec.raw_analysis = raw
                spec.source = ";".join(Path(p).name for p in imgs)
                dtype = str(data.get("drawing_type") or "part")
                # 置信度纠偏：装配图 / 非零件页要打折，不能报 95% 才让用户踩坑
                conf = float(spec.confidence or 0.0)
                if dtype == "assembly":
                    conf = min(conf, 0.75) * 0.9
                if page_type != "part":
                    conf = min(conf, 0.7)
                if page is None and pages and sum(1 for p_ in pages if p_.get("type") == "part") > 1:
                    conf = min(conf, 0.8)
                    prog("⚠ 这份图纸里有多张零件图，只取了其中一页；其它零件请单独指定页码重跑")
                spec.confidence = round(max(conf, 0.0), 3)

                # ---- 先打印孔位基准（不换算），用来判断是哪个基准被吃掉
                _holes = [f for f in spec.features if f.type == "hole"]
                if _holes:
                    _rows = []
                    for f in _holes:
                        _d = f.datum or "unknown"
                        _r = (f"（{f.datum_ref}）" if f.datum_ref else "")
                        _rows.append(f"Ø{f.diameter:g}@({f.x:g},{f.y:g}) datum={_d}{_r}")
                    prog("孔位基准：" + "；".join(_rows))
                    _cnt: Dict[str, int] = {}
                    for f in _holes:
                        _cnt[f.datum or "unknown"] = _cnt.get(f.datum or "unknown", 0) + 1
                    if _cnt.get("unknown"):
                        prog(f"⚠ 有 {_cnt['unknown']} 个孔没给基准（datum=unknown），坐标可信度低")

                uncertain: List[str] = []
                uncertain += _san
                if dtype == "assembly":
                    uncertain.append("识别的是装配图：装配级尺寸/其它零件轮廓可能被误当成零件特征")
                if page_type != "part":
                    uncertain.append(f"第 {page_idx + 1} 页是「{page_type}」而不是零件图")
                try:
                    ocr_text = self.ocr(use_imgs) if self.s.ocr_model else ""
                except Exception:
                    ocr_text = ""
                uncertain += self._uncertain_items(ocr_text, spec)
                _scale = self._scale_check(ocr_text, spec)
                if _scale:
                    spec.confidence = round(min(spec.confidence, 0.55), 3)
                uncertain += _scale

                # 结构完整性因子：模型自评 0.95 也可能漏孔，必须用图纸证据复核。
                # confidence = min(模型自评, 结构完整性)；漏孔严重时同时进 blocking。
                try:
                    _fc_unc, _fc_block, _struct_score = self._feature_consistency(
                        ocr_text, use_imgs, spec, hint)
                except Exception as _e:
                    prog(f"特征数量自洽性检查跳过（{_e}）")
                    _fc_unc, _fc_block, _struct_score = [], [], 1.0
                if _scale:
                    _struct_score = min(_struct_score, 0.55)
                if _struct_score < 1.0:
                    _prev = spec.confidence
                    spec.confidence = round(min(spec.confidence, _struct_score), 3)
                    prog(f"结构完整性因子 {_struct_score:.2f}：置信度 "
                         f"{_prev:.0%} → {spec.confidence:.0%}")
                uncertain += _fc_unc
                for u in uncertain:
                    prog(f"⚠ 不确定项：{u}")

                # ---- 规范步骤（与最终参数一致）+ 把模型给的依据/置信度贴回去
                canon = []
                try:
                    from .steps import spec_to_steps as _c2s
                    canon = _c2s(spec)
                    pool = {}
                    for m in steps_model:
                        pool.setdefault(m.op, []).append(m)
                    for st in canon:
                        lst = pool.get(st.op) or []
                        if lst:
                            m = lst.pop(0)
                            st.evidence = m.evidence
                            st.confidence = m.confidence or spec.confidence
                except Exception:
                    canon = []
                for m in steps_model:
                    if 0 < m.confidence < 0.6:
                        uncertain.append(
                            "步骤「%s」置信度 %.0f%%%s，请核对图纸"
                            % (m.title(), m.confidence * 100,
                               ("（%s）" % m.evidence) if m.evidence else ""))

                # ---- 阻断判定：装配图上的零件尺寸若在图纸文字里找不到依据，不许直接建模。
                # 实测同一张装配图五次识图给出 T=10/15/20/46，temperature=0.0 也拦不住；
                # 而简单零件图稳定。既然是"图不够"，就不该靠调参硬跑。
                blocking: List[str] = list(_fc_block)
                if dtype == "assembly":
                    # 装配图一律阻断，不看 parts 字段（它本身不稳定：
                    # flow_1110e 里只返回 1 项，导致 v2 判据放行了一张给出
                    # Ø104×12 的图纸——而同一张图此前给过 Ø135×{10,12,15,20,46}）。
                    #
                    # 依据：这是 35 件装配图，端盖自身尺寸图上未直接标注；
                    # 实测五次识别直径 104~136、厚度 10~46，跨度 4.6 倍。
                    # 装配图应先拆零件图，这是工艺常识。
                    _ok_hint = any(k in (hint or "") for k in
                                   ("已确认", "按此建模", "尺寸已核对", "confirmed"))
                    if not _ok_hint:
                        blocking.append(
                            "识别结果来自装配图（drawing_type=assembly）。装配图上零件自身"
                            "尺寸通常没有直接标注，实测同一张图五次识别得到直径 104~136、"
                            "厚度 10~46，不可直接建模。请提供『%s』的零件图；"
                            "若已人工核对无误，请在提示里写明「已确认」再重跑"
                            % spec.part_name)

                res = AnalysisResult(spec=spec,
                                     drawing_type=dtype,
                                     parts=list(data.get("parts") or []),
                                     notes=str(data.get("notes") or ""),
                                     raw=raw,
                                     page_index=page_idx,
                                     page_count=len(imgs),
                                     page_type=page_type,
                                     page_title=page_title,
                                     pages=pages,
                                     uncertain=uncertain,
                                     blocking=blocking,
                                     steps=canon)
                for _b in blocking:
                    prog(f"⛔ 阻断：{_b}")
                prog(f"识别完成：{spec.part_name} · {len(spec.features)} 个特征 · 置信度 {spec.confidence:.0%}"
                     + (f" · {len(uncertain)} 项待确认" if uncertain else "")
                     + (" · ❌ 需人工确认后才能建模" if blocking else ""))
                return res
            except Exception as e:
                last_err = e
                prog(f"返回内容解析失败：{e}，重试…")
                content = self._build_content(imgs, hint, "上一次输出无法解析为 JSON，请严格只输出 JSON。")
        raise RuntimeError(f"图纸识别失败：{last_err}")

    VERIFY_PROMPT = """下面是程序从图纸中提取的建模参数。请对照图纸逐项核对：
- 总体尺寸（长/宽/厚/直径）是否与标注一致；
- 每个孔/槽/型腔的坐标、直径/尺寸、深度是否与图纸标注一致（特别是槽和孔的 Y 坐标）；
- 是否漏掉或多余特征。
只修正明显与图纸不符的项，正确项原样保留。输出与输入完全相同结构的 JSON（不要解释）。"""

    def verify(self, imgs: Sequence[str], spec: PartSpec, hint: str = "",
               on_progress: Optional[Callable[[str], None]] = None) -> PartSpec:
        """二次复核：把已提取参数连同图纸一起交给模型校对。"""
        prog = on_progress or self.log
        import json as _json
        content = [{"type": "text",
                    "text": self.VERIFY_PROMPT + "\n\n当前参数：\n" + _json.dumps(spec.to_dict(), ensure_ascii=False)
                            + (("\n\n用户补充：" + hint) if hint else "")}]
        for p in imgs[:6]:
            content.append({"type": "image_url",
                            "image_url": {"url": "data:image/png;base64," + _b64_image(p)}})
        prog("二次复核中…")
        try:
            raw = self._chat(self.s.vision_model,
                             [{"role": "system", "content": SYSTEM_PROMPT},
                              {"role": "user", "content": content}], 0.0)
            data = _json.loads(_strip_json(raw))
            pd = data.get("part") or data
            fixed = PartSpec.from_dict(pd)
            # 复核模型也会自报 0.95；不能让二次复核把 analyze 阶段基于
            # 结构完整性因子压下来的置信度“洗白”回去。
            fixed.confidence = round(min(
                float(pd.get("confidence", spec.confidence) or spec.confidence),
                float(spec.confidence or 0.0)), 3)
            fixed.source = spec.source
            fixed.raw_analysis = raw
            # 守卫：复核不能减少孔数。首次识别可能已漏孔，复核再删孔必须拒绝。
            h0 = sum(1 for f in spec.features if f.type == "hole")
            h1 = sum(1 for f in fixed.features if f.type == "hole")
            if h1 < h0:
                prog(f"复核被拒绝：孔数 {h0} → {h1}，保留原始识别结果")
                return spec
            # 守卫：复核只能“修正”，不能“删特征”。实测法兰盘图纸 7 个特征被复核
            # 砍成 1 个（6 个环布 Ø10 孔连同节圆 Ø90 被当成一个沉孔），且没有任何告警，
            # 后面 CAD 照着错的 spec 建，把整片材料挖掉。
            n0, n1 = len(spec.features), len(fixed.features)
            if n0 >= 2 and n1 < n0:
                lost = n0 - n1
                if n1 == 0 or lost >= max(2, n0 // 2):
                    prog(f"复核被拒绝：特征数 {n0} → {n1}，删得太多，保留原始识别结果")
                    return spec
                prog(f"复核减少 {lost} 个特征（{n0} → {n1}），保留但标记待确认")
            if n1 or n0:
                prog(f"复核完成：{n0} → {n1} 个特征")
                return fixed
        except Exception as e:
            prog(f"复核跳过（{e}）")
        return spec

    def ask(self, question: str) -> str:
        return self._chat(self.s.text_model, [{"role": "user", "content": question}], 0.2)