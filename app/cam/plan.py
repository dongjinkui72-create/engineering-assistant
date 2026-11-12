"""CAM 工艺规划：内置规则优先，处理不了时可调用外部 AI 生成。"""
from __future__ import annotations

import json
import math
from typing import Callable, Dict, List, Optional

from ..calc import feature_area, feature_depth, stock_size
from ..models import PartSpec

POSTS = {
    "NX": "UG/NX CAM",
    "SolidWorks": "SOLIDWORKS CAM",
    "CATIA": "CATIA V5 加工",
    "GENERIC": "通用 ISO",
}


def _tool_for(width: float, minimum: float = 2.0) -> float:
    for d in (3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0, 16.0, 20.0, 25.0):
        if d <= width + 1e-6:
            return max(minimum, d)
    return max(minimum, round(width * 0.8, 1))


def builtin_plan(spec: PartSpec, post: str = "GENERIC") -> Dict:
    """规则化的工艺方案：外形轮廓 → 型腔/槽 → 钻孔。"""
    ops: List[dict] = []
    small = min(spec.length, spec.width, spec.thickness)
    profile_dia = _tool_for(min(spec.length, spec.width) * 0.12, 6.0)
    ops.append({"type": "profile", "tool_dia": profile_dia, "tool": "立铣刀",
                "stepdown": min(2.0, max(0.5, spec.thickness / 6)),
                "stepover": 0.45, "feed": 600, "spindle": 3000,
                "note": "外形轮廓分层铣削，留 0.2mm 精加工余量"})

    # 圆顶面：基体顶面之上的凸起。毛坯加高 dome_h 后，用平底刀把
    # 圆顶轮廓以外的料面铣到基体顶面，再用球头刀按曲面法向偏置走等高圆。
    _dh = float(getattr(spec, "dome_h", 0.0) or 0.0)
    if _dh > 0:
        _a = min(float(spec.length), float(spec.width)) / 2.0
        _ball = 2.0 if _a <= 20.0 else 4.0
        _rough = _tool_for(max(6.0, min(spec.length, spec.width) * 0.15), 4.0)
        ops.append({"type": "dome", "tool_dia": _ball, "tool": "球头铣刀",
                    "rough_dia": _rough, "strategy": "offset-surface",
                    "stepover_mm": 0.3, "stepdown_mm": min(1.0, max(0.3, _dh / 4.0)),
                    "feed": 320, "spindle": 4200,
                    "note": f"圆顶面 高{_dh:g}，平底刀粗清 + 球头刀原生圆弧等高精铣"})

    # 扫掠体（肋/把手）：只有「肋根下方有料托住」的才可能在一次装夹里做出来。
    # 悬空肋（例如杯子把手：肋根在 z=11.5，下方全是空气）三轴单面够不到，
    # 既不生成假工序，也不静默丢弃 —— 交给 unplanned_ops 报真实原因。
    _sws = list(getattr(spec, "sweeps", None) or [])
    if _sws:
        from ..calc import sweep_machinability as _swm
        _doable = [x for x in _sws if _swm(spec, x)["ok"]]
        if _doable:
            _wmin = min(float(x.get("profile_w") or 0.0) for x in _doable)
            d = _tool_for(_wmin, 3.0)
            ops.append({"type": "sweep", "tool_dia": d, "tool": "立铣刀(扫掠肋)",
                        "strategy": "band-wall+top-follow", "stepover": 0.45,
                        "stepover_mm": 0.35,
                        "stepdown": min(1.5, max(0.4, spec.thickness / 10)),
                        "feed": 420, "spindle": 3600,
                        "note": f"扫掠肋 {len(_doable)} 处：条带侧壁清根 + 顶面沿路径仿形"})

    _sh = float(getattr(spec, "shell_t", 0.0) or 0.0)
    # 只有朝上的开口能在一次装夹里从顶面挖出来。朝下/朝侧面要翻面或侧铣，
    # 不能拿顶面刀路硬切（实测会把内腔切到零件上表面，回灌报 84% 假过切）。
    if _sh > 0 and str(getattr(spec, "shell_open", "top") or "top").lower() == "top":
        d = _tool_for(max(3.0, min(spec.length, spec.width) * 0.10), 3.0)
        ops.append({"type": "shell", "tool_dia": d, "tool": "立铣刀", "strategy": "contour+zigzag",
                    "stepover": 0.45, "stepdown": min(2.0, max(0.5, spec.thickness / 8)),
                    "feed": 500, "spindle": 3200,
                    "note": f"抽壳内腔 壁厚{_sh:g}，深{max(spec.thickness - _sh, 0):g}"})

    pockets = [f for f in spec.features if f.type in ("pocket", "step")]
    if pockets:
        w = min(min(f.width, f.length) for f in pockets)
        d = _tool_for(w * 0.7, 3.0)
        ops.append({"type": "pocket", "tool_dia": d, "tool": "立铣刀", "strategy": "zigzag",
                    "stepover": 0.45, "stepdown": min(1.5, spec.thickness / 8),
                    "feed": 450, "spindle": 3500,
                    "note": f"型腔/台阶清角，共 {len(pockets)} 处"})

    slots = [f for f in spec.features if f.type == "slot"]
    if slots:
        d = _tool_for(min(f.width for f in slots), 3.0)
        ops.append({"type": "slot", "tool_dia": d, "tool": "立铣刀", "strategy": "中心线+轮廓",
                    "stepover": 0.4, "stepdown": min(1.5, spec.thickness / 6),
                    "feed": 420, "spindle": 3500, "note": f"腰形槽 {len(slots)} 处"})

    spheres = [f for f in spec.build_features() if f.type == "sphere"]
    if spheres:
        # 球头刀选最大不超过球径的标准刀：刀球半径越接近球坑半径，
        # 刀心球面半径 Rc=R-r_t 越小，等高刀路越少且越贴近理论球面。
        _sd = min(f.diameter for f in spheres)
        d = 0.0
        for _t in (20.0, 16.0, 12.0, 10.0, 8.0, 6.0, 5.0, 4.0, 3.0):
            if _t <= _sd + 1e-6:
                d = _t
                break
        if d <= 0:
            d = max(3.0, round(_sd * 0.8, 1))
        ops.append({"type": "sphere", "tool_dia": d, "tool": "球头铣刀",
                    "strategy": "waterline",
                    "stepover": 0.3, "stepdown": 0.2,
                    "stepover_mm": 0.3, "stepdown_mm": 0.2,
                    "feed": 300, "spindle": 4200,
                    "note": f"球坑 {len(spheres)} 处，球头刀等高环绕"})

    # 按 (直径, 通/盲, 深度) 分组：通孔和盲孔绝不能混在一个 op 里，
    # 否则盲孔会被按通孔深度钻穿（实测 Ø30 深 9 被钻到 Z-38，过切 22mm）
    holes: Dict[tuple, int] = {}
    for f in spec.build_features():
        if f.type == "hole":
            d_eff = f.diameter + 2.0 * spec.grow_of(f)
            through = f.is_through()
            dep = 0.0 if through else round(min(float(f.depth), spec.thickness), 2)
            key = (round(d_eff, 2), through, dep)
            holes[key] = holes.get(key, 0) + 1
    for (dia, through, dep), n in sorted(holes.items()):
        if through:
            drill_depth = round(spec.thickness + (2.0 if spec.thickness * 0.25 < 2 else 3.0), 2)
            note = f"Ø{dia:g} 通孔 ×{n}（钻穿）"
        else:
            drill_depth = dep
            note = f"Ø{dia:g} 盲孔 深{dep:g} ×{n}"
        ops.append({"type": "drill", "tool_dia": dia, "tool": "钻头", "cycle": "G83",
                    "peck": max(1.0, spec.thickness / 3), "feed": 150,
                    "through": through, "depth": drill_depth,
                    "spindle": 1200 if dia > 8 else 1800, "note": note})

    # ????????????????????????? R?
    # ??????????offset=0???? Z=-R????????? Z=0?
    # 顶面外轮廓圆角（凸棱倒圆）：球头刀半径必须等于图纸 R。
    # 刀心沿零件真实棱线（offset=0）、刀尖 Z=-R，球心正好落在顶面 Z=0，
    # 球面扫过四分之一的圆角去除区。不能拿小刀多道逼近，否则 R 不对。
    _fr = float(getattr(spec, "fillet", 0.0) or 0.0)
    _fl = _fr
    if _fr > 0:
        try:
            from .. import calc as _calc
            _fl = _calc.fillet_effective(spec)
        except Exception:
            _fl = _fr
        _note = f"顶面外轮廓圆角 R{_fl:g}，Ø{2.0 * _fl:g} 球头刀沿原生偏置轮廓分层成形"
        if abs(_fl - _fr) > 1e-9:
            _note += f"（图纸 R{_fr:g} 超过可加工上限 T/2={spec.thickness / 2.0:g}，已钳制）"
        ops.append({"type": "fillet", "tool_dia": round(2.0 * _fl, 3),
                    "radius": _fl, "tool": "球头铣刀",
                    "strategy": "edge-roundover-waterline",
                    "stepdown": _fl / 8.0, "stepover": 0.0, "passes": 0,
                    "feed": 300, "spindle": 4200, "note": _note})


    _ch = float(getattr(spec, "chamfer", 0.0) or 0.0)
    if _ch > 0:
        d = _tool_for(max(6.0, min(spec.length, spec.width) * 0.10), 6.0)
        ops.append({"type": "chamfer", "tool_dia": d, "tool": "倒角刀 90°", "angle": 90.0,
                    "width": _ch, "feed": 300, "spindle": 4000,
                    "note": f"外形上轮廓倒角 C{_ch:g}"})

    sl, sw, sh = stock_size(spec, 6.0)
    return {"post": post, "stock": {"L": sl, "W": sw, "H": sh},
            "origin": "X0Y0 零件中心，Z0 顶面", "ops": ops,
            "source": "builtin", "notes": "内置规则生成"}


def plan_problems(spec: PartSpec, plan: Dict) -> List[str]:
    probs: List[str] = []
    ops = plan.get("ops") or []
    if not ops:
        probs.append("方案为空")
    for op in ops:
        if op.get("type") not in ("profile", "pocket", "slot", "drill", "face",
                                  "shell", "chamfer", "fillet", "sphere", "dome", "sweep"):
            probs.append(f"未知工序 {op.get('type')}")
        d = float(op.get("tool_dia") or 0)
        if d <= 0 or d > 40:
            probs.append(f"刀具直径异常：{d}")
    covered = {op.get("type") for op in ops}
    if spec.features and "drill" not in covered and any(f.type == "hole" for f in spec.features):
        probs.append("缺少钻孔工序")
    return probs


def unplanned_ops(spec: PartSpec, plan: Dict) -> List[str]:
    """模型有、但工艺方案里没有对应工序的特征。

    目的：把「图纸有、程序没加工」单独报出来，
    不让这块体积混进 ±3% 的刀路误差（实测 m3 C5：1.75% 假失败）。
    """
    out: List[str] = []
    kinds = {op.get("type") for op in (plan.get("ops") or [])}
    for f in spec.build_features():
        if f.type == "sphere" and "sphere" not in kinds:
            out.append(f"球坑 Ø{f.diameter:g} 深{f.depth:g}：无球头刀曲面刀路")
        if f.type == "boss" and "boss" not in kinds:
            out.append(f"凸台 {f.length:g}×{f.width:g}：无凸台加工工序")
    if float(getattr(spec, "dome_h", 0.0) or 0.0) > 0 and "dome" not in kinds:
        out.append(f"圆顶面 高{float(spec.dome_h):g}：无曲面刀路")
    _sws = list(getattr(spec, "sweeps", None) or [])
    if _sws:
        from ..calc import sweep_machinability as _swm
        for _i, _sw in enumerate(_sws, 1):
            _info = _swm(spec, _sw)
            if not _info["ok"]:
                out.append(f"扫掠体 {_i}：{_info['reason']}")
            elif "sweep" not in kinds:
                out.append(f"扫掠体 {_i}：无扫掠加工工序")
    if float(getattr(spec, "shell_t", 0.0) or 0.0) > 0:
        _so = str(getattr(spec, "shell_open", "top") or "top").lower()
        if _so == "none":
            out.append(f"封闭抽壳 壁厚{spec.shell_t:g}：三轴无法从外部加工内腔")
        elif _so == "bottom":
            out.append(f"抽壳开口朝下 壁厚{spec.shell_t:g}：需翻面装夹（第 2 序），当前程序未包含")
        elif _so not in ("top", ""):
            out.append(f"抽壳开口朝{_so} 壁厚{spec.shell_t:g}：需侧铣/第 4 轴")
        elif "shell" not in kinds:
            out.append(f"抽壳内腔 壁厚{spec.shell_t:g}：无内腔加工工序")
    if float(getattr(spec, "chamfer", 0.0) or 0.0) > 0 and "chamfer" not in kinds:
        out.append(f"倒角 C{spec.chamfer:g}：无倒角工序")
    if float(getattr(spec, "fillet", 0.0) or 0.0) > 0 and "fillet" not in kinds:
        out.append(f"轮廓圆角 R{spec.fillet:g}：无圆角刀工序")
    return out


AI_PROMPT = """你是数控加工工艺工程师。根据零件参数，给出 2.5D 三轴铣削加工方案（JSON）。
要求：
- 只输出 JSON，结构如下：
{"stock":{"L":长,"W":宽,"H":高},
 "ops":[{"type":"profile|pocket|slot|drill|shell|chamfer|sphere|dome|fillet|sweep","tool_dia":刀具直径,"tool":"刀具名",
         "strategy":"zigzag|contour|centerline","stepover":0.45,"stepdown":2,"feed":600,
         "spindle":3000,"cycle":"G83","peck":2,"note":"说明"}]}
- 刀具直径必须小于对应特征尺寸（槽宽、型腔短边、孔径）。
- 工序顺序：外形 → 圆顶面 → 型腔/台阶 → 槽 → 球坑 → 钻孔 → 倒角。
- 圆顶面 dome 用球头铣刀，必须给 stepover_mm/stepdown_mm（mm，不是比例）。
- 铝合金可用高转速，钢件保守一些。"""


def ai_plan(spec: PartSpec, settings, log: Optional[Callable[[str], None]] = None,
            post: str = "GENERIC") -> Dict:
    """外部 AI 生成方案；失败或不合格时回退到内置规则。"""
    from openai import OpenAI
    log = log or (lambda m: None)
    base = builtin_plan(spec, post)
    try:
        client = OpenAI(api_key=settings.api_key, base_url=settings.base_url,
                        timeout=120, max_retries=0)
        payload = json.dumps(spec.to_dict(), ensure_ascii=False)
        r = client.chat.completions.create(
            model=settings.text_model, temperature=0.2,
            messages=[{"role": "system", "content": AI_PROMPT},
                      {"role": "user", "content": "零件参数：\n" + payload}])
        text = (r.choices[0].message.content or "").strip()
        i, j = text.find("{"), text.rfind("}")
        data = json.loads(text[i:j + 1])
        probs = plan_problems(spec, data)
        if probs:
            log("AI 方案不合格：" + "；".join(probs[:3]) + "，改用内置方案")
            return base
        data["post"] = post
        data["source"] = "ai"
        data["notes"] = "外部 AI 生成（已通过尺寸校验）"
        log("已采用 AI 生成的加工方案")
        return data
    except Exception as e:
        log(f"AI 工艺规划失败：{e}，改用内置方案")
        return base