"""Common result type for CAD adapters."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class BuildResult:
    ok: bool = False
    cad: str = ""
    part_path: str = ""
    step_path: str = ""
    image_path: str = ""
    volume: float = 0.0          # mm^3 measured by the CAD system
    expected: float = 0.0        # mm^3 from the analytic model
    elapsed: float = 0.0
    message: str = ""
    details: dict = field(default_factory=dict)
    failures: List[str] = field(default_factory=list)   # 特征切除失败清单
    unsupported: bool = False      # 明确"这个后端做不了这种几何"，不算失败
    unsupported_reason: str = ""

    @property
    def error_pct(self) -> float:
        if not self.expected:
            return 0.0
        return abs(self.volume - self.expected) / self.expected * 100.0

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["error_pct"] = round(self.error_pct, 4)
        return d
