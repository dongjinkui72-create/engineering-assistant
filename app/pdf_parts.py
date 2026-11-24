"""Part discovery for drawing imports.

图纸导入只接受两类文件：

* 图片：PNG / JPG / JPEG / BMP / WEBP / TIF / TIFF
* CAD 图纸：DXF / DWG

PDF 不再作为图纸输入格式（不解析、不拆分、不内置任何测试图纸）。
每个输入文件视为一个待识别零件；图片交给视觉识别，DXF 交给几何解析。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}
CAD_EXT = {".dxf", ".dwg"}
SUPPORTED_EXT = IMAGE_EXT | CAD_EXT


@dataclass
class DiscoveredPart:
    part_no: str
    name: str
    page: int
    rect: list
    image: str
    note: str = ""
    status: str = "pending"
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "part_no": self.part_no,
            "name": self.name,
            "page": self.page,
            "rect": list(self.rect),
            "image": self.image,
            "note": self.note,
            "status": self.status,
            "reason": self.reason,
        }


def _safe_name(text: str) -> str:
    text = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(text or "")).strip(" .")
    return text or "未命名零件"


def discover_parts(files: Sequence[str], work_dir: str,
                   progress: Optional[Callable[[str], None]] = None) -> List[DiscoveredPart]:
    """把每个输入文件登记为一个候选零件（图片或 CAD 图纸）。"""
    p = progress or (lambda _m: None)
    out = Path(work_dir) / "parts"
    out.mkdir(parents=True, exist_ok=True)
    result: List[DiscoveredPart] = []

    for f in files:
        path = Path(f)
        ext = path.suffix.lower()
        if ext == ".pdf":
            raise RuntimeError(
                "PDF 不是支持的图纸格式：请导入图片（PNG/JPG/BMP）或 CAD 图纸（DXF）")
        if ext not in SUPPORTED_EXT:
            raise RuntimeError(
                f"不支持的图纸格式 {ext or '(无扩展名)'}：只支持图片 PNG/JPG/BMP/TIF 与 CAD 图纸 DXF/DWG")
        result.append(DiscoveredPart(
            part_no=f"F{len(result) + 1}", name=path.stem, page=1,
            rect=[], image=str(path), note="单张输入文件"))

    # Deduplicate identical paths, keep reading order.
    seen = set()
    clean: List[DiscoveredPart] = []
    for item in result:
        key = str(Path(item.image).resolve()) if Path(item.image).exists() else item.image
        if key in seen:
            continue
        seen.add(key)
        clean.append(item)
    p(f"图纸登记完成：共 {len(clean)} 个候选零件")
    return clean
