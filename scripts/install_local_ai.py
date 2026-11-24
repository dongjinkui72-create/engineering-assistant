"""安装适合本机内存的本地识图模型（离线运行，不联网识图）。

用法：
    python scripts/install_local_ai.py             # 自动检测内存并安装推荐档
    python scripts/install_local_ai.py --check     # 只检查，不下载
    python scripts/install_local_ai.py --list      # 打印各内存档位
    python scripts/install_local_ai.py --model qwen2.5vl:7b

模型权重不放在 GitHub 仓库里（单文件远超 100 MB），本脚本通过 Ollama
官方源下载。下载完成后模型保存在 local_ai/models，软件运行时完全离线。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import ai_tiers  # noqa: E402


def _ollama_exe() -> Path | None:
    env = os.environ.get("ENGINEERING_ASSISTANT_OLLAMA", "").strip()
    candidates = []
    if env:
        candidates.append(Path(env))
    candidates += [
        ROOT / "local_ai" / "runtime" / "ollama.exe",
        ROOT / "local_ai" / "ollama.exe",
        ROOT / "ollama.exe",
        Path(r"E:\Ollama\ollama.exe"),
        Path.home() / "AppData" / "Local" / "Programs" / "Ollama" / "ollama.exe",
    ]
    for p in candidates:
        if p.exists():
            return p
    which = shutil.which("ollama")
    return Path(which) if which else None


def _installed_models(url: str) -> list[str]:
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(url + "/api/tags", timeout=5) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return []
    return [str(m.get("name") or m.get("model") or "") for m in data.get("models", [])]


def main() -> int:
    ap = argparse.ArgumentParser(description="安装本地识图 AI 模型")
    ap.add_argument("--model", help="手动指定模型，例如 qwen2.5vl:7b")
    ap.add_argument("--check", action="store_true", help="只检查模型是否已安装")
    ap.add_argument("--list", action="store_true", help="列出各内存档位")
    args = ap.parse_args()

    info = ai_tiers.describe()
    ram = info["ram_gb"]
    print("=" * 62)
    print("工程助手 · 本地识图 AI 安装器")
    print("=" * 62)
    print(f"本机物理内存：{ram if ram is not None else '未检测到'} GB")
    print(f"推荐模型：    {info['model']}（约 {info['download_gb']} GB 下载）")
    print(f"适用档位：    {info['label']}")
    print(f"说明：        {info['note']}")
    if info["low_memory"]:
        print("警告：内存低于 8 GB，叠加 NX / SolidWorks 可能卡顿。")
    print()
    print("全部档位：")
    print(ai_tiers.format_table())
    print()

    if args.list:
        return 0

    model = (args.model or info["model"]).strip()
    exe = _ollama_exe()
    if exe is None:
        print("未找到 ollama.exe。")
        print("请先安装 Ollama：https://ollama.com/download/windows")
        print("或把 ollama.exe 放到 local_ai/runtime/ 下。")
        return 2

    # 让内置运行器起来，才能用它的模型目录执行 pull / 查询。
    try:
        from app.config import Settings
        from app.local_ai import ensure_server

        settings = Settings.load()
        ready, detail = ensure_server(settings)
        print(detail)
        if not ready:
            return 3
        url = str(getattr(settings, "local_ai_url", "http://127.0.0.1:11511")).rstrip("/")
    except Exception as e:
        url = "http://127.0.0.1:11511"
        print(f"提示：内置运行器启动检查失败（{e}），仍尝试直接下载。")

    names = _installed_models(url)
    installed = any(n == model or n.startswith(model + ":") for n in names)
    if args.check:
        print("已安装模型：" + (", ".join(names) if names else "无"))
        print(f"{model}：{'已安装' if installed else '未安装'}")
        return 0 if installed else 1
    if installed:
        print(f"{model} 已安装，无需重复下载。")
        return 0

    env = os.environ.copy()
    env["OLLAMA_HOST"] = url.split("://", 1)[-1].strip("/")
    env["OLLAMA_NO_CLOUD"] = "1"
    print(f"开始下载 {model} …（约 {info['download_gb']} GB，可断点续传）")
    try:
        rc = subprocess.call([str(exe), "pull", model], env=env)
    except KeyboardInterrupt:
        print("\n已取消。")
        return 130
    if rc != 0:
        print(f"下载失败，退出码 {rc}。可重试同一命令，Ollama 会续传。")
        return rc
    names = _installed_models(url)
    if any(n == model or n.startswith(model + ":") for n in names):
        print(f"完成：{model} 已安装。")
        print("把 settings.json 的 local_ai_model 设为该模型即可使用。")
        return 0
    print("下载命令结束，但模型列表里未看到该模型，请检查磁盘空间。")
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
