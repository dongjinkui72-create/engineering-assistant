# -*- coding: utf-8 -*-
"""UI 冒烟：建模软件单选、步骤表填充、已删除的 CAM 入口不再启动线程。"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication, QMessageBox
from app.models import PartSpec, Feature
from app.ui.main_window import MainWindow

app = QApplication([])
w = MainWindow()

# 默认 SolidWorks；两者互斥，同时只能选中一个。
assert w._targets() == ["SolidWorks"], w._targets()
w.rb_nx.setChecked(True)
assert w._targets() == ["NX"], w._targets()
assert not w.rb_sw.isChecked()
w.rb_sw.setChecked(True)
assert w._targets() == ["SolidWorks"], w._targets()
assert not w.rb_nx.isChecked()
print("cad target exclusive: ok")

# 步骤表能按 spec 自动生成，并显示图纸依据。
spec = PartSpec(length=120, width=80, thickness=15, base_kind="round",
                diameter=120, features=[
                    Feature(type="hole", x=0, y=0, diameter=30, through=True),
                    Feature(type="chamfer", depth=1.0),
                ])
w.steps = []
w._apply_spec(spec)
assert w.step_tbl.rowCount() >= 2, w.step_tbl.rowCount()
assert w.step_tbl.item(0, 3).text().strip(), "base step evidence empty"
print("steps table:", w.step_tbl.rowCount(), "rows")

# CAM 已删除：调用旧入口不应产生后台线程（屏蔽提示框，避免离屏测试阻塞）。
QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.Ok)
w.files = []
w.run_task("cam")
assert getattr(w, "thread", None) is None
print("removed cam entry: no thread")
w.close()
print("UI smoke: PASS")