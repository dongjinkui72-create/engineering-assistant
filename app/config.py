"""Runtime configuration for 工程助手.

API credentials default to the ones your ChatGPT/Codex client already uses
(%USERPROFILE%\.codex\config.toml); a local settings.json overrides them.
"""
from __future__ import annotations

import json
import os
import re
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
    # CAD
    nx_root: str = r"D:\ug\NX 12.0"
    sw_template: str = r"C:\ProgramData\SolidWorks\SOLIDWORKS 2024\templates\零件.prtdot"
    catia_exe: str = r"E:\catia\anzhuang\intel_a\code\bin\CATSTART.exe"
    catia_env_dir: str = r"C:\ProgramData\DassaultSystemes\CATEnv"
    catia_env_name: str = "CATIA_P3.V5R21.B21"
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
        cfg = _read_codex_toml()
        if not s.api_key:
            s.api_key = cfg.get("api_key", "") or os.environ.get("DASHSCOPE_API_KEY", "")
        if cfg.get("base_url") and not (p.exists() and json.loads(p.read_text(encoding="utf-8")).get("base_url")):
            s.base_url = cfg["base_url"]
        if not s.output_dir:
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