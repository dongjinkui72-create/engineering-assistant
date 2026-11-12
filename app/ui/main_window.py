"""工程助手 · PySide6 desktop UI."""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox,
                               QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QFrame, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QScrollArea, QSpinBox, QSplitter,
                               QTableWidget, QTableWidgetItem, QTabWidget, QTextEdit,
                               QVBoxLayout, QWidget)

from ..config import APP_TITLE, APP_VERSION, Settings, project_root
from ..models import Feature, PartSpec
from ..steps import LABELS as LABEL_OF, OPS, Step, spec_to_steps, steps_to_spec
from ..pipeline import CAD_NAMES, Pipeline, PipelineResult, adapter_status

CSS = """
QMainWindow { background: #0f1115; }
QWidget { color: #d7dce5; font-family: "Microsoft YaHei UI"; font-size: 13px; }
QGroupBox { border: 1px solid #262c36; border-radius: 8px; margin-top: 14px; padding-top: 10px; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 4px; color: #7cc4ff; }
QPushButton { background: #1c2430; border: 1px solid #2d3644; border-radius: 6px; padding: 7px 12px; }
QPushButton:hover { background: #253041; }
QPushButton:disabled { color: #5b6472; border-color: #232a35; }
QPushButton#primary { background: #1f6feb; border: none; font-weight: 600; }
QPushButton#primary:hover { background: #2a7cf5; }
QPushButton#primary:disabled { background: #24405f; color: #7f8b9b; }
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QTextEdit { background: #141a23; border: 1px solid #2a323f; border-radius: 6px; padding: 5px; }
QTableWidget { background: #141a23; gridline-color: #232a35; border: 1px solid #2a323f; border-radius: 6px; }
QHeaderView::section { background: #182030; color: #9fb3c8; border: none; padding: 5px; }
QTabWidget::pane { border: 1px solid #262c36; border-radius: 8px; }
QTabBar::tab { background: #161d27; padding: 8px 16px; border-top-left-radius: 6px; border-top-right-radius: 6px; }
QTabBar::tab:selected { background: #1f6feb; color: white; }
QProgressBar { border: 1px solid #2a323f; border-radius: 5px; text-align: center; }
QProgressBar::chunk { background: #1f6feb; border-radius: 5px; }
QFrame#drop { border: 2px dashed #33405a; border-radius: 10px; background: #121822; }
"""


class Worker(QObject):
    log = Signal(str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn, self.args, self.kwargs = fn, args, kwargs

    def run(self):
        try:
            self.done.emit(self.fn(*self.args, **self.kwargs))
        except Exception:
            self.failed.emit(traceback.format_exc())


class DropArea(QFrame):
    dropped = Signal(list)

    def __init__(self):
        super().__init__()
        self.setObjectName("drop")
        self.setAcceptDrops(True)
        self.setMinimumHeight(110)
        lay = QVBoxLayout(self)
        self.lbl = QLabel("把零件图纸拖到这里\n支持 PNG / JPG / PDF / DXF")
        self.lbl.setAlignment(Qt.AlignCenter)
        self.lbl.setStyleSheet("color:#7b8798; font-size:14px;")
        lay.addWidget(self.lbl)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        files = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if files:
            self.dropped.emit(files)
            self.lbl.setText("\n".join(Path(f).name for f in files[:3]))


class SettingsDialog(QDialog):
    def __init__(self, s: Settings, parent=None):
        super().__init__(parent)
        self.s = s
        self.setWindowTitle("设置")
        self.setMinimumWidth(620)
        f = QFormLayout(self)
        self.base = QLineEdit(s.base_url)
        self.key = QLineEdit(s.api_key)
        self.key.setEchoMode(QLineEdit.Password)
        self.vmodel = QLineEdit(s.vision_model)
        self.nx = QLineEdit(s.nx_root)
        self.sw = QLineEdit(s.sw_template)
        self.catia = QLineEdit(s.catia_exe)
        f.addRow("API 地址", self.base)
        f.addRow("API Key", self.key)
        f.addRow("视觉模型", self.vmodel)
        f.addRow("NX 安装目录", self.nx)
        f.addRow("SolidWorks 模板", self.sw)
        f.addRow("CATIA 启动器", self.catia)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def apply(self):
        s = self.s
        s.base_url = self.base.text().strip()
        s.api_key = self.key.text().strip()
        s.vision_model = self.vmodel.text().strip()
        s.nx_root = self.nx.text().strip()
        s.sw_template = self.sw.text().strip()
        s.catia_exe = self.catia.text().strip()
        s.save()



class StepDialog(QDialog):
    """编辑单个建模步骤的参数。"""

    def __init__(self, step: Step, parent=None):
        super().__init__(parent)
        self.step = step
        self.widgets = {}
        self.setWindowTitle(f"编辑步骤 · {step.op_name}")
        self.setMinimumWidth(420)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(step.title()))
        form = QFormLayout()
        for key in OPS.get(step.op, {}).get("params", []):
            val = step.params.get(key)
            if key == "through":
                w = QCheckBox("是通孔/通槽")
                w.setChecked(bool(val if val is not None else True))
            elif key == "axis":
                w = QComboBox()
                w.addItems(["x - 沿 X 轴镜像", "y - 沿 Y 轴镜像"])
                w.setCurrentIndex(1 if str(val).lower() == "y" else 0)
            else:
                w = QDoubleSpinBox()
                w.setRange(-5000, 5000)
                w.setDecimals(2)
                w.setSuffix(" mm")
                w.setSingleStep(0.5)
                w.setValue(float(val) if val not in (None, "") else 0.0)
            self.widgets[key] = w
            form.addRow(LABEL_OF.get(key, key), w)
        self.note = QLineEdit(step.note)
        form.addRow("备注", self.note)
        lay.addLayout(form)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def result_step(self) -> Step:
        params = dict(self.step.params)
        for key, w in self.widgets.items():
            if isinstance(w, QCheckBox):
                params[key] = w.isChecked()
            elif isinstance(w, QComboBox):
                params[key] = "y" if w.currentIndex() == 1 else "x"
            else:
                params[key] = float(w.value())
        return Step(self.step.op, params, self.note.text().strip(), self.step.enabled)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.s = Settings.load()
        self.files: List[str] = []
        self.spec: Optional[PartSpec] = None
        self.steps: List[Step] = []
        self._last: Optional[PipelineResult] = None
        self.parts: List[dict] = []
        self.thread: Optional[QThread] = None
        self.worker: Optional[Worker] = None
        self.setWindowTitle(f"{APP_TITLE}  ·  图纸 → 三维模型 → 加工程序")
        self.resize(1280, 800)
        try:
            from PySide6.QtGui import QGuiApplication
            g = QGuiApplication.primaryScreen().availableGeometry()
            w = min(1200, g.width() - 60)
            h = min(780, g.height() - 60)
            self.resize(w, h)
            self.move(g.x() + (g.width() - w) // 2, g.y() + 10)
        except Exception:
            pass
        self.setStyleSheet(CSS)
        icon = project_root() / "assets" / "icon.png"
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))
        self._build_ui()
        self._refresh_status()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        split = QSplitter(Qt.Horizontal)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(10, 10, 10, 10)
        lv.setSpacing(8)

        self.drop = DropArea()
        self.drop.dropped.connect(self.add_files)
        lv.addWidget(self.drop)

        row = QHBoxLayout()
        b = QPushButton("选择图纸…")
        b.clicked.connect(self.pick_files)
        row.addWidget(b)
        b2 = QPushButton("清空")
        b2.clicked.connect(self.clear_files)
        row.addWidget(b2)
        lv.addLayout(row)

        self.file_list = QLabel("未选择图纸")
        self.file_list.setWordWrap(True)
        self.file_list.setStyleSheet("color:#8b98a9;")
        lv.addWidget(self.file_list)

        g1 = QGroupBox("1 · 读图")
        v1 = QVBoxLayout(g1)
        self.btn_analyze = QPushButton("识别图纸（AI 读数）")
        self.btn_analyze.clicked.connect(lambda: self.run_task("analyze"))
        v1.addWidget(self.btn_analyze)
        self.cb_verify = QCheckBox("识别后二次复核（更准，约 +15 秒）")
        self.cb_verify.setChecked(True)
        v1.addWidget(self.cb_verify)
        row_pg = QHBoxLayout()
        row_pg.addWidget(QLabel("图纸页"))
        self.sp_page = QSpinBox()
        self.sp_page.setRange(0, 40)
        self.sp_page.setValue(0)
        self.sp_page.setSpecialValueText("自动")
        self.sp_page.setToolTip("多页 PDF（如装配图+各零件图）时，指定用第几页建模；0=自动挑零件图")
        row_pg.addWidget(self.sp_page)
        row_pg.addStretch(1)
        v1.addLayout(row_pg)
        self.part_box = QComboBox()
        self.part_box.setVisible(False)
        self.part_box.currentIndexChanged.connect(self._part_changed)
        v1.addWidget(self.part_box)
        self.lbl_conf = QLabel("—")
        self.lbl_conf.setStyleSheet("color:#8b98a9;")
        v1.addWidget(self.lbl_conf)
        lv.addWidget(g1)

        g2 = QGroupBox("2 · 参数（可修改）")
        v2 = QVBoxLayout(g2)
        row_a = QHBoxLayout()
        self.ed_name = QLineEdit()
        self.ed_mat = QLineEdit()
        self.ed_mat.setMaximumWidth(120)
        row_a.addWidget(QLabel("名称")); row_a.addWidget(self.ed_name, 1)
        row_a.addWidget(QLabel("材料")); row_a.addWidget(self.ed_mat)
        v2.addLayout(row_a)

        row_b = QHBoxLayout()
        self.sp_l = QSpinBox(); self.sp_w = QSpinBox(); self.sp_t = QSpinBox()
        for lbl, sp in (("长/Ø", self.sp_l), ("宽", self.sp_w), ("厚", self.sp_t)):
            sp.setRange(1, 5000); sp.setSuffix(" mm"); sp.setMaximumWidth(110)
            row_b.addWidget(QLabel(lbl)); row_b.addWidget(sp)
        self.cb_base = QComboBox(); self.cb_base.addItems(["矩形板", "圆盘"])
        self.cb_base.setMaximumWidth(96)
        row_b.addWidget(self.cb_base)
        row_b.addStretch(1)
        v2.addLayout(row_b)

        self.tbl = QTableWidget(0, 5)
        self.tbl.setHorizontalHeaderLabels(["类型", "X", "Y", "尺寸", "深"])
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tbl.setMinimumHeight(104)
        v2.addWidget(self.tbl)
        lv.addWidget(g2, 1)

        g3 = QGroupBox("3 · 建模与加工")
        v3 = QVBoxLayout(g3)
        self.cb_nx = QCheckBox("UG / NX")
        self.cb_sw = QCheckBox("SolidWorks")
        self.cb_catia = QCheckBox("CATIA")
        self.cb_nx.setChecked(True)
        row3 = QHBoxLayout()
        for c in (self.cb_nx, self.cb_sw, self.cb_catia):
            row3.addWidget(c)
        v3.addLayout(row3)
        row3b = QHBoxLayout()
        row3b.addWidget(QLabel("加工程序由"))
        self.cb_post = QComboBox()
        self.cb_post.addItem("UG / NX CAM", "NX")
        self.cb_post.addItem("SolidWorks CAM", "SolidWorks")
        self.cb_post.addItem("CATIA 加工", "CATIA")
        self.cb_post.addItem("内置通用后处理", "GENERIC")
        row3b.addWidget(self.cb_post, 1)
        v3.addLayout(row3b)
        self.cb_ai_cam = QCheckBox("AI 辅助生成刀路（内置规则不满足时）")
        v3.addWidget(self.cb_ai_cam)
        self.btn_build = QPushButton("开始建模 + 生成加工程序")
        self.btn_build.setObjectName("primary")
        self.btn_build.clicked.connect(lambda: self.run_task("all"))
        v3.addWidget(self.btn_build)
        row4 = QHBoxLayout()
        b3 = QPushButton("仅生成程序")
        b3.clicked.connect(lambda: self.run_task("cam"))
        row4.addWidget(b3)
        b4 = QPushButton("打开输出目录")
        b4.clicked.connect(self.open_out)
        row4.addWidget(b4)
        v3.addLayout(row4)
        lv.addWidget(g3)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(left)
        scroll.setMinimumWidth(420)
        scroll.setMaximumWidth(560)
        split.addWidget(scroll)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(6, 10, 10, 10)
        self.tabs = QTabWidget()
        self.view_img = QLabel("识别后在这里预览")
        self.view_img.setAlignment(Qt.AlignCenter)
        self.view_img.setStyleSheet("color:#5b6472;")
        self.tabs.addTab(self.view_img, "图纸")
        self.step_tbl = QTableWidget(0, 6)
        self.step_tbl.setHorizontalHeaderLabels(["#", "类型", "步骤", "图纸依据", "置信度", "状态"])
        self.step_tbl.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.step_tbl.setColumnWidth(0, 36)
        self.step_tbl.setColumnWidth(1, 100)
        self.step_tbl.setColumnWidth(2, 320)
        self.step_tbl.setColumnWidth(4, 64)
        self.step_tbl.setColumnWidth(5, 56)
        self.step_tbl.verticalHeader().setVisible(False)
        self.step_tbl.doubleClicked.connect(lambda idx: self.edit_step(idx.row()))
        st_w = QWidget()
        st_v = QVBoxLayout(st_w)
        st_v.setContentsMargins(8, 8, 8, 8)
        bar = QHBoxLayout()
        for text, fn in (("编辑参数", lambda: self.edit_step(self.step_tbl.currentRow())),
                         ("上移", lambda: self.move_step(-1)),
                         ("下移", lambda: self.move_step(1)),
                         ("删除", self.del_step),
                         ("启用/禁用", self.toggle_step),
                         ("添加步骤", self.add_step)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            bar.addWidget(b)
        bar.addStretch(1)
        b_re = QPushButton("按步骤重新建模")
        b_re.setObjectName("primary")
        b_re.clicked.connect(self.rebuild_from_steps)
        bar.addWidget(b_re)
        st_v.addLayout(bar)
        st_v.addWidget(self.step_tbl, 1)
        hint = QLabel("提示：双击某一步可修改尺寸；改完点「按步骤重新建模」，软件会严格按新参数重建。")
        hint.setStyleSheet("color:#8b98a9;")
        st_v.addWidget(hint)
        self.tabs.addTab(st_w, "建模步骤")

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setStyleSheet("font-family: Consolas,'Microsoft YaHei'; font-size: 12px;")
        self.tabs.addTab(self.log, "运行日志")
        self.result = QTextEdit()
        self.result.setReadOnly(True)
        self.tabs.addTab(self.result, "结果")
        rv.addWidget(self.tabs, 1)
        split.addWidget(right)
        split.setSizes([440, 740])
        self.setCentralWidget(split)

        bar = self.statusBar()
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        self.progress.setMaximumWidth(180)
        bar.addPermanentWidget(self.progress)
        self.status_lbl = QLabel()
        bar.addWidget(self.status_lbl)

        tb = self.addToolBar("main")
        tb.setMovable(False)
        act_set = QAction("设置", self)
        act_set.triggered.connect(self.open_settings)
        tb.addAction(act_set)
        act_dir = QAction("输出目录", self)
        act_dir.triggered.connect(self.open_out)
        tb.addAction(act_dir)
        act_compat = QAction("兼容性自检", self)
        act_compat.triggered.connect(self.show_compat)
        tb.addAction(act_compat)

    def _refresh_status(self):
        st = adapter_status(self.s)
        info = getattr(self.s, "cad_info", {}) or {}
        parts = []
        for k, v in st.items():
            ver = ""
            i = info.get(k)
            if i is not None and getattr(i, "found", False):
                ver = " " + ("12" if k == "NX" and "NX" in i.version else i.version.replace("SOLIDWORKS ", "").replace("CATIA V5 ", ""))
            parts.append(f"{'🟢' if v else '⚪'} {CAD_NAMES[k]}{ver}")
        api = "🟢 API 已配置" if self.s.api_ready else "🔴 API 未配置"
        self.status_lbl.setText(f"{api}    {'  '.join(parts)}")

    def show_compat(self):
        from ..compat import report_text
        from ..probe import probe_all, format_report
        from PySide6.QtGui import QGuiApplication
        QGuiApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            results = probe_all(self.s, str(Path(self.s.output_dir) / "_probe"), with_catia=False)
            caps = format_report(results)
        except Exception as e:
            caps = f"能力探测失败：{e}"
        finally:
            QGuiApplication.restoreOverrideCursor()
        QMessageBox.information(self, "CAD 兼容性自检",
                                report_text() + "\n\n—— 运行时能力探测 ——\n" + caps)

    # ------------------------------------------------------------------ files
    def add_files(self, files):
        for f in files:
            if f not in self.files:
                self.files.append(f)
        self.file_list.setText("\n".join(Path(f).name for f in self.files))
        self._preview(self.files[0])

    def pick_files(self):
        fs, _ = QFileDialog.getOpenFileNames(
            self, "选择零件图纸", str(Path.home()),
            "图纸 (*.png *.jpg *.jpeg *.bmp *.pdf *.dxf);;所有文件 (*.*)")
        if fs:
            self.add_files(fs)

    def clear_files(self):
        self.files = []
        self.file_list.setText("未选择图纸")

    # ------------------------------------------------------------------ steps
    def refresh_steps(self):
        self.step_tbl.setRowCount(0)
        for i, st in enumerate(self.steps):
            r = self.step_tbl.rowCount()
            self.step_tbl.insertRow(r)
            _cf = (f"{st.confidence:.0%}" if st.confidence > 0 else "")
            _ev = st.evidence or ("图纸外形尺寸" if st.op.startswith("base") else "（未标注依据）")
            for c, val in enumerate([str(i + 1), st.op_name, st.title(),
                                     _ev, _cf,
                                     "启用" if st.enabled else "停用"]):
                it = QTableWidgetItem(val)
                if c != 2:
                    it.setTextAlignment(Qt.AlignCenter)
                if not st.enabled:
                    it.setForeground(Qt.gray)
                elif c == 4 and st.confidence > 0:
                    # 三档：<60% 红、60~80% 黄、>=80% 正常
                    if st.confidence < 0.6:
                        it.setForeground(QColor("#e05252"))
                    elif st.confidence < 0.8:
                        it.setForeground(QColor("#d08a2a"))
                self.step_tbl.setItem(r, c, it)

    def edit_step(self, row: int):
        if row < 0 or row >= len(self.steps):
            return
        st = self.steps[row]
        dlg = StepDialog(st, self)
        if dlg.exec():
            self.steps[row] = dlg.result_step()
            self.refresh_steps()

    def move_step(self, delta: int):
        row = self.step_tbl.currentRow()
        j = row + delta
        if row < 0 or j < 0 or j >= len(self.steps):
            return
        self.steps[row], self.steps[j] = self.steps[j], self.steps[row]
        self.refresh_steps()
        self.step_tbl.setCurrentCell(j, 0)

    def add_step(self):
        from PySide6.QtWidgets import QInputDialog
        names = [f"{k} · {v['name']}" for k, v in OPS.items() if k not in ("base_rect", "base_round")]
        keys = [k for k in OPS if k not in ("base_rect", "base_round")]
        name, ok = QInputDialog.getItem(self, APP_TITLE, "选择要添加的工序", names, 0, False)
        if not ok:
            return
        op = keys[names.index(name)]
        params = {k: 0.0 for k in OPS[op]["params"]}
        if "through" in params:
            params["through"] = True
        if op == "mirror":
            params["axis"] = "y"
        st = Step(op, params)
        self.steps.append(st)
        self.refresh_steps()
        self.edit_step(len(self.steps) - 1)

    def del_step(self):
        row = self.step_tbl.currentRow()
        if 0 <= row < len(self.steps):
            del self.steps[row]
            self.refresh_steps()

    def toggle_step(self):
        row = self.step_tbl.currentRow()
        if 0 <= row < len(self.steps):
            self.steps[row].enabled = not self.steps[row].enabled
            self.refresh_steps()

    def rebuild_from_steps(self):
        if not self.steps:
            QMessageBox.information(self, APP_TITLE, "还没有建模步骤，请先识别图纸。")
            return
        preview = "\n".join(f"{i+1}. {st.title()}" for i, st in enumerate(self.steps)
                             if st.enabled)[:1500]
        ok = QMessageBox.question(self, "确认建模步骤",
                                  "将严格按以下步骤重新建模：\n\n" + preview +
                                  "\n\n确认开始？")
        if ok != QMessageBox.Yes:
            return
        spec = steps_to_spec(self.steps, self.spec)
        self.spec = spec
        self._apply_spec(spec)
        if not self.spec.features and self.spec.base_kind == "rect" and self.spec.length <= 1:
            QMessageBox.warning(self, APP_TITLE, "步骤里没有有效的基体尺寸。")
            return
        self._busy(True)
        self.log.clear()
        self._start(self._do_build)

    def _preview(self, path: str):
        try:
            if path.lower().endswith(".pdf"):
                import pymupdf as fitz
                doc = fitz.open(path)
                pix = doc[0].get_pixmap(dpi=150)
                px = QPixmap()
                px.loadFromData(pix.tobytes("png"))
            else:
                px = QPixmap(path)
            if not px.isNull():
                self.view_img.setPixmap(px.scaled(self.view_img.size() * 0.96,
                                                  Qt.KeepAspectRatio, Qt.SmoothTransformation))
        except Exception:
            pass

    # ------------------------------------------------------------------ tasks
    def _targets(self):
        t = []
        if self.cb_nx.isChecked():
            t.append("NX")
        if self.cb_sw.isChecked():
            t.append("SolidWorks")
        if self.cb_catia.isChecked():
            t.append("CATIA")
        return t

    def run_task(self, kind: str):
        if not self.files:
            QMessageBox.information(self, APP_TITLE, "请先选择一张图纸。")
            return
        if kind in ("build", "all", "cam") and not self.spec:
            QMessageBox.information(self, APP_TITLE, "请先识别图纸。")
            return
        self._busy(True)
        self.log.clear()
        if kind == "analyze":
            self._start(self._do_analyze)
        elif kind == "cam":
            self._start(self._do_cam)
        else:
            self._start(self._do_build)

    def _start(self, fn):
        self.thread = QThread()
        self.worker = Worker(fn)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self._log)
        self.worker.done.connect(self._on_done)
        self.worker.failed.connect(self._on_fail)
        self.worker.done.connect(self.thread.quit)
        self.worker.failed.connect(self.thread.quit)
        self.thread.start()

    def _busy(self, on: bool):
        for b in (self.btn_analyze, self.btn_build):
            b.setEnabled(not on)
        self.progress.setVisible(on)
        self.tabs.setCurrentIndex(1)

    def _log(self, msg: str):
        self.log.appendPlainText(msg)
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    # ------------------------------------------------------------------ actions
    def _do_analyze(self):
        p = Pipeline(self.s, self.worker.log.emit)
        pg = self.sp_page.value()
        res = p.analyze(self.files, verify=self.cb_verify.isChecked(),
                        progress=self.worker.log.emit,
                        page=(pg - 1) if pg > 0 else None)
        return res

    def _apply_spec(self, spec: PartSpec):
        self.spec = spec
        self.ed_name.setText(spec.part_name)
        self.ed_mat.setText(spec.material)
        self.sp_l.setValue(int(round(spec.length)))
        self.sp_w.setValue(int(round(spec.width)))
        self.sp_t.setValue(int(round(spec.thickness)))
        self.cb_base.setCurrentIndex(1 if spec.base_kind == "round" else 0)
        self.tbl.setRowCount(0)
        for f in spec.features:
            self._add_row(f)
        if not self.steps:
            self.steps = spec_to_steps(spec)
        self.refresh_steps()

    def _add_row(self, f: Feature):
        r = self.tbl.rowCount()
        self.tbl.insertRow(r)
        size = f"Ø{f.diameter:g}" if f.type == "hole" else f"{f.length:g}×{f.width:g}"
        vals = [f.type, f"{f.x:g}", f"{f.y:g}", size, f"{f.depth:g}"]
        for c, v in enumerate(vals):
            it = QTableWidgetItem(v)
            it.setTextAlignment(Qt.AlignCenter)
            self.tbl.setItem(r, c, it)

    def _read_spec(self) -> PartSpec:
        spec = self.spec or PartSpec()
        spec.part_name = self.ed_name.text().strip() or "未命名零件"
        spec.material = self.ed_mat.text().strip() or "45钢"
        spec.length = float(self.sp_l.value())
        spec.width = float(self.sp_w.value())
        spec.thickness = float(self.sp_t.value())
        spec.base_kind = "round" if self.cb_base.currentIndex() == 1 else "rect"
        if spec.base_kind == "round":
            spec.diameter = spec.length
        return spec

    def _do_build(self):
        spec = self._read_spec()
        targets = self._targets()
        if not targets:
            raise RuntimeError("请至少勾选一款 CAD 软件")
        out = str(Path(self.s.output_dir) / f"{spec.part_name}_{len(os.listdir(self.s.output_dir)) if os.path.isdir(self.s.output_dir) else 0}")
        p = Pipeline(self.s, self.worker.log.emit)
        res = p.run(self.files, targets, out, do_cam=True,
                    cam_post=self.cb_post.currentData() or "GENERIC",
                    use_ai_cam=self.cb_ai_cam.isChecked(),
                    progress=self.worker.log.emit)
        return res

    def _do_cam(self):
        from ..cam.gcode import generate
        spec = self._read_spec()
        out = str(Path(self.s.output_dir) / f"{spec.part_name}_nc")
        return generate(spec, out)

    def _on_done(self, res):
        self._busy(False)
        if isinstance(res, PipelineResult):
            if res.analysis:
                self.spec = res.analysis.spec
                if getattr(res, "steps", None):
                    self.steps = res.steps
                    self.refresh_steps()
                self._apply_spec(res.spec if hasattr(res, "spec") else res.analysis.spec)
                self.lbl_conf.setText(f"图纸类型：{res.analysis.drawing_type} · 置信度 {res.analysis.spec.confidence:.0%}")
                self.parts = res.analysis.parts
                self._fill_parts(res.analysis.parts)
            self._show_report(res)
            self._preview_result(res)
        elif hasattr(res, "nc_path"):
            self.result.setPlainText(
                f"NC 程序：{res.nc_path}\n行数：{res.lines}\n轨迹长度：{res.path_length:.0f} mm\n"
                f"预计时间：{res.minutes:.0f} 分钟\n\n工序：\n" + "\n".join(res.operations))
            self.tabs.setCurrentIndex(2)
        else:
            from ..vision import AnalysisResult
            if isinstance(res, AnalysisResult):
                self.steps = spec_to_steps(res.spec)
                self._apply_spec(res.spec)
                self.lbl_conf.setText(f"图纸类型：{res.drawing_type} · 置信度 {res.spec.confidence:.0%}")
                self._fill_parts(res.parts)
                self.result.setPlainText(res.raw[:4000])
                self.tabs.setCurrentIndex(2)

    def _fill_parts(self, parts):
        if not parts:
            self.part_box.setVisible(False)
            return
        self.part_box.blockSignals(True)
        self.part_box.clear()
        self.part_box.addItem("（当前识别的零件）", -1)
        for i, p in enumerate(parts):
            name = p.get("name") or f"零件{i+1}"
            qty = p.get("qty", 1)
            self.part_box.addItem(f"{p.get('no', i+1)}. {name} ×{qty}", i)
        self.part_box.blockSignals(False)
        self.part_box.setVisible(True)

    def _part_changed(self, idx):
        if idx <= 0 or not self.parts:
            return
        p = self.parts[self.part_box.itemData(idx)]
        hint = (f"这是装配图。请只提取第{p.get('no')}号零件『{p.get('name')}』的建模参数"
                f"（{p.get('note','')}），base_kind 按零件实际形状选择。")
        self.log.appendPlainText(f"切换零件：{p.get('name')}，重新识别…")
        self.spec = None
        self.files = list(self.files)
        self._busy(True)
        self._start(lambda: self._do_focus(hint))

    def _do_focus(self, hint):
        p = Pipeline(self.s, self.worker.log.emit)
        return p.analyze(self.files, hint=hint, verify=self.cb_verify.isChecked(),
                         progress=self.worker.log.emit)

    def _preview_result(self, res: PipelineResult):
        for b in res.builds:
            if b.image_path and Path(b.image_path).exists():
                px = QPixmap(b.image_path)
                if not px.isNull():
                    self.view_img.setPixmap(px.scaled(self.view_img.size() * 0.96,
                                                      Qt.KeepAspectRatio, Qt.SmoothTransformation))
                    self.tabs.setCurrentIndex(0)
                    return
        if res.cam and res.cam.svg_path:
            pass

    def _show_report(self, res: PipelineResult):
        lines = []
        spec = res.analysis.spec if res.analysis else None
        if spec:
            lines.append(f"零件：{spec.part_name}   {spec.length:g}×{spec.width:g}×{spec.thickness:g} mm   {spec.material}")
            lines.append(f"特征：{len(spec.features)} 个\n")
        for b in res.builds:
            mark = "✅" if b.ok else "❌"
            lines.append(f"{mark} {CAD_NAMES.get(b.cad, b.cad)}：体积 {b.volume:,.0f} / 理论 {b.expected:,.0f} mm³"
                         f"（偏差 {b.error_pct:.2f}%）  {b.elapsed:.1f}s")
            if b.part_path:
                lines.append(f"    → {b.part_path}")
            if b.step_path:
                lines.append(f"    → {b.step_path}")
            if not b.ok:
                lines.append(f"    ! {b.message}")
        if res.cam and res.cam.ok:
            lines.append(f"\nNC 程序：{res.cam.nc_path}（{res.cam.lines} 行，约 {res.cam.minutes:.0f} 分钟）")
        lines.append(f"\n报告：{res.report_md}")
        self.result.setPlainText("\n".join(lines))
        self.tabs.setCurrentIndex(2)

    def _on_fail(self, err: str):
        self._busy(False)
        self._log(err)
        QMessageBox.critical(self, APP_TITLE, err.splitlines()[-1][:300])

    # ------------------------------------------------------------------ misc
    def open_settings(self):
        d = SettingsDialog(self.s, self)
        if d.exec():
            d.apply()
            self._refresh_status()
            QMessageBox.information(self, APP_TITLE, "设置已保存。")

    def open_out(self):
        Path(self.s.output_dir).mkdir(parents=True, exist_ok=True)
        os.startfile(self.s.output_dir)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())