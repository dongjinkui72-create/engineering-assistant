"""Runtime configuration for 工程助手.

API credentials default to the ones your ChatGPT/Codex client already uses
(%USERPROFILE%\.codex\config.toml); a local settings.json overrides them.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

APP_NAME = "EngineeringAssistant"
APP_TITLE = "工程助手"
APP_VERSION = "1.0.0"


def app_data_dir() -> Path:
    d = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _read_codex_toml() -> dict:
    p = Path.home() / ".codex" / "config.toml"
    if not p.exists():
        return {}
    t = p.read_text(encoding="utf-8", errors="ignore")
    base = re.search(r'base_url\s*=\s*"([^"]+)"', t)
    key = re.search(r'experimental_bearer_token\s*=\s*"([^"]+)"', t)
    model = re.search(r'^\s*model\s*=\s*"([^"]+)"', t, re.M)
    out = {}
    if base:
        out["base_url"] = base.group(1)
    if key:
        out["api_key"] = key.group(1)
    if model:
        out["text_model"] = model.group(1)
    return out


@dataclass
class Settings:
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key: str = ""
    vision_model: str = "qwen3-vl-plus"
    text_model: str = "qwen3.8-max"
    ocr_model: str = "qwen3.5-ocr"
    timeout: int = 180
    # Offline local vision reader (Qwen2.5-VL 7B via bundled Ollama)
    local_ai_enabled: bool = True
    local_ai_model: str = "qwen2.5vl:7b"
    local_ai_url: str = "http://127.0.0.1:11511"
    local_ai_timeout: int = 180
    # CAD
    nx_root: str = r"D:\ug\NX 12.0"
    sw_template: str = r"C:\ProgramData\SolidWorks\SOLIDWORKS 2024\templates\零件.prtdot"
    # misc
    comfyui_dir: str = r"E:\ComfyUI\ComfyUI-aki-v3.2"
    comfyui_url: str = "http://127.0.0.1:8188"
    output_dir: str = ""
    extra: dict = field(default_factory=dict)
    cad_info: dict = field(default_factory=dict)

    @staticmethod
    def load() -> "Settings":
        s = Settings()
        p = app_data_dir() / "settings.json"
        if p.exists():
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                for k, v in d.items():
                    if hasattr(s, k) and v not in (None, ""):
                        setattr(s, k, v)
            except Exception:
                pass
        # 旧版本把本地模型指向 11434（系统 Ollama 的默认端口）。
        # 现在随包运行时固定用 11511，避免与用户自己的 Ollama 抢端口。
        if str(getattr(s, "local_ai_url", "")).rstrip("/") == "http://127.0.0.1:11434":
            s.local_ai_url = "http://127.0.0.1:11511"
        # 旧默认模型 qwen3-vl:2b 结构化读图不合格，已换成 7B。
        if str(getattr(s, "local_ai_model", "") or "") in ("", "qwen3-vl:2b"):
            s.local_ai_model = "qwen2.5vl:7b"
        cfg = _read_codex_toml()
        if not s.api_key:
            s.api_key = cfg.get("api_key", "") or os.environ.get("DASHSCOPE_API_KEY", "")
        if cfg.get("base_url") and not (p.exists() and json.loads(p.read_text(encoding="utf-8")).get("base_url")):
            s.base_url = cfg["base_url"]
        if getattr(sys, "frozen", False):
            # 打包版绝不能把结果写进程序自己的 _internal 目录：
            # 那样覆盖安装/卸载时结果会丢失，而且用户根本找不到。
            od = str(s.output_dir or "").strip()
            bad = not od
            if od:
                try:
                    base = Path(getattr(sys, "_MEIPASS", project_root())).resolve()
                    bad = Path(od).resolve().is_relative_to(base)
                except Exception:
                    bad = "_internal" in od
            if bad:
                s.output_dir = str(Path.home() / "Documents" / "工程助手" / "输出")
        elif not s.output_dir:
            s.output_dir = str(project_root() / "out")
        try:
            from .compat import apply_to_settings
            s.cad_info = apply_to_settings(s)   # 自动适配本机已安装的 CAD 版本
        except Exception:
            s.cad_info = {}
        return s

    def save(self) -> None:
        p = app_data_dir() / "settings.json"
        p.write_text(json.dumps(self.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")

    @property
    def api_ready(self) -> bool:
        return bool(self.api_key and self.base_url)

    def masked_key(self) -> str:
        k = self.api_key or ""
        return (k[:6] + "..." + k[-4:]) if len(k) > 12 else ("未配置" if not k else "***")