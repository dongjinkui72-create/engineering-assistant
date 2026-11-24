"""工程助手 · PySide6 desktop UI."""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QObject, QSize, Qt, QThread, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox,
                               QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QFrame, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QRadioButton, QScrollArea, QSplitter,
                               QSizePolicy, QTableWidget, QTableWidgetItem, QTabWidget, QTextEdit,
                               QVBoxLayout, QWidget)

from ..config import APP_TITLE, APP_VERSION, Settings, project_root
from ..models import PartSpec
from ..steps import Step, spec_to_steps
from ..pipeline import CAD_NAMES, Pipeline, PipelineResult, adapter_status

CSS = """
QMainWindow { background: #0f1115; }
QWidget { color: #d7dce5; font-family: "Microsoft YaHei UI"; font-size: 13px; }
QGroupBox { border: 1px solid #262c36; border-radius: 8px; margin-top: 12px; padding-top: 10px; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; color: #7cc4ff; font-weight: 600; font-size: 12px; }
QPushButton { background: #1c2430; border: 1px solid #2d3644; border-radius: 6px; padding: 8px 14px; }
QPushButton:hover { background: #253041; }
QPushButton:pressed { background: #182030; }
QPushButton:disabled { color: #5b6472; border-color: #232a35; }
QPushButton#primary { background: #1f6feb; border: none; font-weight: 600; }
QPushButton#primary:hover { background: #2a7cf5; }
QPushButton#primary:disabled { background: #24405f; color: #7f8b9b; }
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QTextEdit { background: #141a23; border: 1px solid #2a323f; border-radius: 6px; padding: 5px; }
QTableWidget { background: #141a23; alternate-background-color: #171e28; gridline-color: transparent; border: 1px solid #2a323f; border-radius: 6px; selection-background-color: #1f3a5f; selection-color: #ffffff; }
QTableWidget::item { padding: 3px 8px; }
QHeaderView::section { background: #182030; color: #9fb3c8; border: none; border-right: 1px solid #202836; padding: 7px 8px; font-weight: 600; }
QTabWidget::pane { border: 1px solid #262c36; border-radius: 8px; top: -1px; }
QTabBar { background: #0f1115; }
QTabBar::tab { background: #161d27; padding: 9px 20px; margin-right: 2px; border-top-left-radius: 6px; border-top-right-radius: 6px; }
QTabBar::tab:hover { background: #1c2430; }
QTabBar::tab:selected { background: #1f6feb; color: white; }
QProgressBar { border: 1px solid #2a323f; border-radius: 5px; text-align: center; }
QProgressBar::chunk { background: #1f6feb; border-radius: 5px; }
QFrame#drop { border: 2px dashed #33405a; border-radius: 10px; background: #121822; }
QFrame#drop:hover { border-color: #1f6feb; }
QSplitter::handle { background: #1d2430; }
QSplitter::handle:hover { background: #1f6feb; }
QStatusBar { background: #0d1016; border-top: 1px solid #1d2430; color: #8b98a9; padding: 4px 12px; min-height: 24px; }
QStatusBar::item { border: none; }
QRadioButton { spacing: 8px; }
QRadioButton::indicator { width: 15px; height: 15px; }
QToolBar#mainbar { background: #0f1115; border: none; border-bottom: 1px solid #1d2430; spacing: 2px; padding: 0 14px; }
QLabel#brandName { font-size: 15px; font-weight: 700; color: #e8eef8; }
QLabel#brandTag { font-size: 12px; color: #6d7a8c; }
QToolBar#mainbar QToolButton { padding: 7px 13px; border-radius: 6px; color: #aeb9c8; }
QToolBar#mainbar QToolButton:hover { background: #1c2430; color: #e8eef8; }
QToolBar#mainbar::separator { background: #232a35; width: 1px; margin: 8px 10px; }
QScrollArea { background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
QWidget#leftFooter { background: #0f1115; border-top: 1px solid #1d2430; }
QScrollBar:vertical { background: #0f1115; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #2a3444; border-radius: 5px; min-height: 24px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
"""

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
CAD_EXTS = {".step", ".stp", ".iges", ".igs", ".dxf", ".prt", ".sldprt"}
SUPPORTED_EXTS = IMAGE_EXTS | CAD_EXTS


def _is_supported(path: str) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_EXTS


def _is_cad(path: str) -> bool:
    return Path(path).suffix.lower() in CAD_EXTS


def _installed_models(url: str):
    """读取本地 Ollama 已安装模型；服务未启动时返回空列表。"""
    import json as _json
    import urllib.request as _u
    try:
        with _u.urlopen(url.rstrip("/") + "/api/tags", timeout=4) as r:
            d = _json.loads(r.read().decode("utf-8", "replace"))
        return [str(m.get("name")) for m in (d.get("models") or []) if m.get("name")]
    except Exception:
        return []


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
        self.setMinimumHeight(112)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        self.lbl = QLabel("把零件图纸拖到这里\n"
                          "图片：PNG / JPG / BMP / TIF / WEBP\n"
                          "CAD：STEP / IGES / DXF / PRT / SLDPRT")
        self.lbl.setAlignment(Qt.AlignCenter)
        self.lbl.setWordWrap(True)
        self.lbl.setStyleSheet("color:#7b8798; font-size:13px;")
        lay.addWidget(self.lbl)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            files = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
            if any(_is_supported(f) for f in files):
                e.acceptProposedAction()

    def dropEvent(self, e):
        files = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if files:
            self.dropped.emit(files)
            self.lbl.setText("\n".join(Path(f).name for f in files[:3]))


class ModelPullWorker(QThread):
    """后台下载本地视觉模型，避免设置窗口卡死。"""

    progress = Signal(str)
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, url: str, model: str, parent=None):
        super().__init__(parent)
        self.url = url.rstrip("/")
        self.model = model

    def run(self):
        import json as _json
        import urllib.request as _u
        try:
            body = _json.dumps({"model": self.model, "stream": True}).encode()
            req = _u.Request(self.url + "/api/pull", data=body, method="POST",
                             headers={"Content-Type": "application/json"})
            with _u.urlopen(req, timeout=7200) as r:
                for line in r:
                    try:
                        d = _json.loads(line.decode("utf-8"))
                    except Exception:
                        continue
                    if d.get("error"):
                        self.failed.emit(str(d["error"]))
                        return
                    st = str(d.get("status") or "")
                    tot = d.get("total") or 0
                    done = d.get("completed") or 0
                    if st == "success":
                        self.finished_ok.emit(self.model)
                        return
                    if tot:
                        self.progress.emit("%s  %.1f%%  %.2f/%.2f GB" %
                                           (st, 100.0 * done / tot, done / 1e9, tot / 1e9))
                    else:
                        self.progress.emit(st)
            self.finished_ok.emit(self.model)
        except Exception as e:
            self.failed.emit("%s: %s" % (type(e).__name__, e))


class SettingsDialog(QDialog):
    def __init__(self, s: Settings, parent=None):
        super().__init__(parent)
        self.s = s
        self._pull_worker = None
        self.setWindowTitle("设置")
        self.setMinimumWidth(680)
        f = QFormLayout(self)

        self.lai_enabled = QCheckBox("启用本地视觉模型（完全离线）")
        self.lai_enabled.setChecked(bool(getattr(s, "local_ai_enabled", True)))
        f.addRow("本地识图", self.lai_enabled)

        self.lai_url = QLineEdit(str(getattr(s, "local_ai_url", "http://127.0.0.1:11511")))
        f.addRow("本地模型服务", self.lai_url)

        self.lai_model = QComboBox()
        self.lai_model.setEditable(True)
        self.lai_model.addItems(_installed_models(self.lai_url.text().strip()))
        self.lai_model.setEditText(str(getattr(s, "local_ai_model", "qwen2.5vl:7b")))
        f.addRow("视觉模型", self.lai_model)

        row = QHBoxLayout()
        btn_refresh = QPushButton("刷新已安装")
        btn_refresh.clicked.connect(self._refresh_models)
        btn_down = QPushButton("下载所选模型")
        btn_down.clicked.connect(self._download_model)
        row.addWidget(btn_refresh)
        row.addWidget(btn_down)
        self.lai_state = QLabel("")
        self.lai_state.setWordWrap(True)
        row.addWidget(self.lai_state, 1)
        f.addRow("模型管理", row)

        self.nx = QLineEdit(s.nx_root)
        self.sw = QLineEdit(s.sw_template)
        f.addRow("NX 安装目录", self.nx)
        f.addRow("SolidWorks 模板", self.sw)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def _refresh_models(self):
        url = self.lai_url.text().strip() or "http://127.0.0.1:11511"
        current = self.lai_model.currentText().strip()
        items = _installed_models(url)
        self.lai_model.clear()
        self.lai_model.addItems(items)
        self.lai_model.setEditText(current)
        self.lai_state.setText(("已发现 %d 个模型" % len(items)) if items else "未连接本地模型服务")

    def _download_model(self):
        model = self.lai_model.currentText().strip()
        if not model:
            self.lai_state.setText("请先填写模型名")
            return
        url = self.lai_url.text().strip() or "http://127.0.0.1:11511"
        self.lai_state.setText("正在下载 " + model + "…")
        w = ModelPullWorker(url, model, self)
        w.progress.connect(lambda m: self.lai_state.setText(m))
        w.finished_ok.connect(self._on_model_downloaded)
        w.failed.connect(lambda m: self.lai_state.setText("下载失败：" + m))
        self._pull_worker = w
        w.start()

    def _on_model_downloaded(self, model: str):
        self.lai_state.setText("已安装 " + model)
        self._refresh_models()
        self.lai_model.setEditText(model)

    def apply(self):
        s = self.s
        s.local_ai_enabled = self.lai_enabled.isChecked()
        s.local_ai_url = self.lai_url.text().strip().rstrip("/") or "http://127.0.0.1:11511"
        s.local_ai_model = self.lai_model.currentText().strip() or "qwen2.5vl:7b"
        s.nx_root = self.nx.text().strip()
        s.sw_template = self.sw.text().strip()
        s.save()



class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.s = Settings.load()
        self.files: List[str] = []
        self.spec: Optional[PartSpec] = None
        self.steps: List[Step] = []
        self._analysis = None
        self._analysis_sig = ()
        self._last: Optional[PipelineResult] = None
        self.parts: List[dict] = []
        self.thread: Optional[QThread] = None
        self.worker: Optional[Worker] = None
        self.setWindowTitle(f"{APP_TITLE}  ·  图纸 → 三维模型")
        self.setMinimumSize(1100, 680)
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
        split.setChildrenCollapsible(False)
        split.setHandleWidth(1)

        panel = QWidget()
        lv = QVBoxLayout(panel)
        lv.setContentsMargins(14, 14, 12, 14)
        lv.setSpacing(14)

        g0 = QGroupBox("1 · 导入图纸")
        v0 = QVBoxLayout(g0)
        v0.setContentsMargins(12, 12, 12, 12)
        v0.setSpacing(10)
        self.drop = DropArea()
        self.drop.dropped.connect(self.add_files)
        v0.addWidget(self.drop)

        row = QHBoxLayout()
        row.setSpacing(8)
        b = QPushButton("导入图片 / CAD…")
        b.setMinimumHeight(36)
        b.clicked.connect(self.pick_files)
        row.addWidget(b, 1)
        b2 = QPushButton("清空")
        b2.setMinimumHeight(36)
        b2.setMinimumWidth(76)
        b2.clicked.connect(self.clear_files)
        row.addWidget(b2, 0)
        v0.addLayout(row)

        self.file_list = QLabel("未选择文件")
        self.file_list.setWordWrap(True)
        self.file_list.setMinimumHeight(34)
        self.file_list.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.file_list.setStyleSheet(
            "color:#8b98a9; background:#141a23; border:1px solid #232a35;"
            "border-radius:6px; padding:7px 10px;")
        v0.addWidget(self.file_list)
        lv.addWidget(g0)

        g1 = QGroupBox("2 · 图纸识别与完整性检查")
        v1 = QVBoxLayout(g1)
        v1.setContentsMargins(12, 12, 12, 12)
        v1.setSpacing(10)
        self.btn_analyze = QPushButton("只识别并检查，不建模")
        self.btn_analyze.setMinimumHeight(36)
        self.btn_analyze.clicked.connect(lambda: self.run_task("analyze"))
        v1.addWidget(self.btn_analyze)
        note1 = QLabel("图片由本机视觉模型识读；CAD 文件直接读取几何。\n"
                       "缺尺寸、比例矛盾、特征越界或软件不支持的几何会阻止建模。")
        note1.setWordWrap(True)
        note1.setStyleSheet("color:#8b98a9;")
        v1.addWidget(note1)
        self.part_box = QComboBox()
        self.part_box.setVisible(False)
        self.part_box.currentIndexChanged.connect(self._part_changed)
        v1.addWidget(self.part_box)
        self.lbl_conf = QLabel("尚未识别")
        self.lbl_conf.setWordWrap(True)
        self.lbl_conf.setStyleSheet("color:#8b98a9;")
        v1.addWidget(self.lbl_conf)
        lv.addWidget(g1)

        g3 = QGroupBox("3 · 建模软件（同时只启动一款）")
        v3 = QVBoxLayout(g3)
        v3.setContentsMargins(12, 12, 12, 12)
        v3.setSpacing(10)
        row3 = QHBoxLayout()
        row3.setSpacing(24)
        self.rb_nx = QRadioButton("UG / NX")
        self.rb_sw = QRadioButton("SolidWorks")
        self.rb_sw.setChecked(True)
        row3.addWidget(self.rb_nx)
        row3.addWidget(self.rb_sw)
        row3.addStretch(1)
        v3.addLayout(row3)
        note3 = QLabel("建模时软件会自动关闭另一款 CAD，避免多开占满内存。")
        note3.setWordWrap(True)
        note3.setStyleSheet("color:#8b98a9;")
        v3.addWidget(note3)
        lv.addWidget(g3)
        lv.addStretch(1)

        left = QWidget()
        outer = QVBoxLayout(left)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(panel)
        outer.addWidget(scroll, 1)

        footer = QWidget()
        footer.setObjectName("leftFooter")
        fv = QVBoxLayout(footer)
        fv.setContentsMargins(14, 10, 12, 14)
        fv.setSpacing(8)
        self.btn_build = QPushButton("开始建模（自动识别并检查）")
        self.btn_build.setObjectName("primary")
        self.btn_build.setMinimumHeight(44)
        self.btn_build.clicked.connect(lambda: self.run_task("build"))
        fv.addWidget(self.btn_build)
        b4 = QPushButton("打开输出目录")
        b4.setMinimumHeight(36)
        b4.clicked.connect(self.open_out)
        fv.addWidget(b4)
        outer.addWidget(footer)

        left.setMinimumWidth(400)
        left.setMaximumWidth(540)
        split.addWidget(left)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(10, 14, 14, 14)
        rv.setSpacing(0)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)

        self.view_img = QLabel("导入图纸后在这里预览")
        self.view_img.setAlignment(Qt.AlignCenter)
        self.view_img.setMinimumSize(1, 1)
        self.view_img.setStyleSheet("color:#5b6472; background:#141a23;"
                                    "border:1px solid #232a35; border-radius:8px;")
        pv = QWidget()
        pv_l = QVBoxLayout(pv)
        pv_l.setContentsMargins(12, 12, 12, 12)
        pv_l.addWidget(self.view_img, 1)
        self.tabs.addTab(pv, "图纸")

        self.step_tbl = QTableWidget(0, 6)
        self.step_tbl.setHorizontalHeaderLabels(
            ["#", "类型", "步骤", "图纸依据", "置信度", "状态"])
        hh = self.step_tbl.horizontalHeader()
        for _c, _w in ((0, 40), (1, 96), (4, 72), (5, 64)):
            hh.setSectionResizeMode(_c, QHeaderView.Fixed)
            self.step_tbl.setColumnWidth(_c, _w)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        hh.setMinimumSectionSize(44)
        hh.setFixedHeight(34)
        hh.setHighlightSections(False)
        self.step_tbl.verticalHeader().setVisible(False)
        self.step_tbl.verticalHeader().setDefaultSectionSize(44)
        self.step_tbl.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
        self.step_tbl.setShowGrid(False)
        self.step_tbl.setAlternatingRowColors(True)
        self.step_tbl.setWordWrap(True)
        self.step_tbl.setTextElideMode(Qt.ElideNone)
        self.step_tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.step_tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.step_tbl.setSelectionMode(QAbstractItemView.SingleSelection)

        st_w = QWidget()
        st_v = QVBoxLayout(st_w)
        st_v.setContentsMargins(12, 12, 12, 12)
        st_v.setSpacing(8)
        st_v.addWidget(self.step_tbl, 1)
        hint = QLabel("此处只读展示软件识别出的建模步骤与图纸依据；"
                      "识别有误请更换清晰、完整的图纸。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#8b98a9;")
        st_v.addWidget(hint)
        self.tabs.addTab(st_w, "建模步骤")

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setStyleSheet(
            "font-family: Consolas,'Microsoft YaHei'; font-size: 12px;"
            "background:#0f141b; border:1px solid #232a35; border-radius:8px; padding:8px;")
        log_w = QWidget()
        log_v = QVBoxLayout(log_w)
        log_v.setContentsMargins(12, 12, 12, 12)
        log_v.addWidget(self.log, 1)
        self.tabs.addTab(log_w, "运行日志")

        self.result = QTextEdit()
        self.result.setReadOnly(True)
        self.result.setStyleSheet(
            "background:#0f141b; border:1px solid #232a35; border-radius:8px; padding:8px;")
        res_w = QWidget()
        res_v = QVBoxLayout(res_w)
        res_v.setContentsMargins(12, 12, 12, 12)
        res_v.addWidget(self.result, 1)
        self.tabs.addTab(res_w, "结果")

        rv.addWidget(self.tabs, 1)
        split.addWidget(right)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([430, 790])
        self.setCentralWidget(split)

        bar = self.statusBar()
        bar.setSizeGripEnabled(False)
        self.status_lbl = QLabel("就绪")
        bar.addWidget(self.status_lbl, 1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        self.progress.setFixedWidth(170)
        bar.addPermanentWidget(self.progress)

        tb = self.addToolBar("main")
        tb.setObjectName("mainbar")
        tb.setMovable(False)
        tb.setFloatable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextOnly)
        tb.setFixedHeight(48)
        brand_w = QWidget()
        bh = QHBoxLayout(brand_w)
        bh.setContentsMargins(2, 0, 0, 0)
        bh.setSpacing(10)
        brand = QLabel(APP_TITLE)
        brand.setObjectName("brandName")
        tag = QLabel("图纸 → 三维模型")
        tag.setObjectName("brandTag")
        bh.addWidget(brand)
        bh.addWidget(tag)
        tb.addWidget(brand_w)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        tb.addSeparator()
        act_set = QAction("设置", self)
        act_set.triggered.connect(self.open_settings)
        tb.addAction(act_set)
        act_dir = QAction("输出目录", self)
        act_dir.triggered.connect(self.open_out)
        tb.addAction(act_dir)
        act_compat = QAction("兼容性自检", self)
        act_compat.triggered.connect(self.show_compat)
        tb.addAction(act_compat)

    def _show_conclusion(self, ar) -> None:
        conf = float(getattr(getattr(ar, "spec", None), "confidence", 0.0) or 0.0)
        dtype = str(getattr(ar, "drawing_type", "") or "零件图").strip()
        # 识图内部用英文类型名，界面统一显示中文，避免出现“图纸类型：part”。
        _dmap = {"part": "零件图", "assembly": "装配图", "sheet": "钣金件",
                 "gear": "齿轮", "shaft": "轴类零件", "drawing": "零件图"}
        dtype = _dmap.get(dtype.lower(), dtype)
        audit = getattr(ar, "audit", None) or {}
        summary = str(audit.get("summary") or "").strip()
        n_unc = len(getattr(ar, "uncertain", None) or [])
        lines = [f"图纸类型：{dtype}  ·  置信度 {conf:.0%}"]
        if summary:
            lines.append(summary)
        # summary 里通常已经写了“有 N 项需要人工确认”，不要重复一行。
        if n_unc and "确认" not in summary:
            lines.append(f"{n_unc} 项待人工确认，见“结果”页。")
        self.lbl_conf.setText("\n".join(lines))
        self.lbl_conf.setStyleSheet("color:#8b98a9;")

    def _refresh_status(self):
        st = adapter_status(self.s)
        info = getattr(self.s, "cad_info", {}) or {}
        parts = []
        for k, v in st.items():
            ver = ""
            i = info.get(k)
            if i is not None and getattr(i, "found", False):
                ver = " " + ("12" if k == "NX" and "NX" in i.version else i.version.replace("SOLIDWORKS ", ""))
            parts.append(f"{'🟢' if v else '⚪'} {CAD_NAMES[k]}{ver}")
        try:
            from ..local_ai import status as _ai_status
            ai = _ai_status(self.s)
            model = str(ai.get("model") or "")
            ai_txt = ("🟢 本地识图 " + model if ai.get("ready")
                      else "⚪ 本地识图未就绪 " + model)
        except Exception:
            ai_txt = "⚪ 本地识图不可用"
        self.status_lbl.setText(f"{ai_txt}    {'  '.join(parts)}")

    def show_compat(self):
        from ..compat import report_text
        from ..probe import probe_all, format_report
        from PySide6.QtGui import QGuiApplication
        QGuiApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            results = probe_all(self.s, str(Path(self.s.output_dir) / "_probe"))
            caps = format_report(results)
        except Exception as e:
            caps = f"能力探测失败：{e}"
        finally:
            QGuiApplication.restoreOverrideCursor()
        QMessageBox.information(self, "CAD 兼容性自检",
                                report_text() + "\n\n—— 运行时能力探测 ——\n" + caps)

    # ------------------------------------------------------------------ files
    def add_files(self, files):
        good, bad = [], []
        for f in files:
            (good if _is_supported(f) else bad).append(f)
        if bad:
            QMessageBox.warning(self, APP_TITLE,
                                "只接受图片或 CAD 文件，已忽略：\n" +
                                "\n".join(Path(x).name for x in bad[:8]))
        if not good:
            return
        cand = list(self.files) + good
        kinds = {_is_cad(f) for f in cand}
        if len(kinds) > 1:
            QMessageBox.warning(self, APP_TITLE,
                                "图片与 CAD 不能混在同一任务里，请先清空再导入。")
            return
        changed = False
        for f in good:
            if f not in self.files:
                self.files.append(f)
                changed = True
        if changed:
            self.spec = None
            self.steps = []
            self._analysis = None
            self._analysis_sig = ()
            self.step_tbl.setRowCount(0)
            self.lbl_conf.setText("已导入，等待识别检查")
        self.file_list.setText("\n".join(Path(f).name for f in self.files))
        if not _is_cad(self.files[0]):
            self._preview(self.files[0])
        else:
            self.view_img.setPixmap(QPixmap())
            self.view_img.setText("CAD 文件：" + Path(self.files[0]).name)

    def pick_files(self):
        fs, _ = QFileDialog.getOpenFileNames(
            self, "选择图纸或 CAD 文件", str(Path.home()),
            "图纸/模型 (*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp "
            "*.step *.stp *.iges *.igs *.dxf *.prt *.sldprt);;所有文件 (*.*)")
        if fs:
            self.add_files(fs)

    def clear_files(self):
        self.files = []
        self.spec = None
        self.steps = []
        self._analysis = None
        self._analysis_sig = ()
        self.parts = []
        self.file_list.setText("未选择文件")
        self.lbl_conf.setText("尚未识别")
        self.lbl_conf.setStyleSheet("color:#8b98a9;")
        self.part_box.setVisible(False)
        self.step_tbl.setRowCount(0)
        self.view_img.setPixmap(QPixmap())
        self.view_img.setText("导入图纸后在这里预览")

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
                # 窄窗口下步骤/依据自动换行；悬停仍显示完整文字。
                it.setToolTip(val)
                if c in (2, 3):
                    it.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                else:
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


    def _preview(self, path: str):
        if _is_cad(path):
            self.view_img.setPixmap(QPixmap())
            self.view_img.setText("CAD 文件：" + Path(path).name)
            return
        try:
            px = QPixmap(path)
            if not px.isNull():
                self.view_img.setPixmap(px.scaled(self.view_img.size() * 0.96,
                                                  Qt.KeepAspectRatio, Qt.SmoothTransformation))
        except Exception:
            pass

    def _targets(self):
        return ["NX"] if self.rb_nx.isChecked() else ["SolidWorks"]

    def run_task(self, kind: str):
        if not self.files:
            QMessageBox.information(self, APP_TITLE, "请先导入图片或 CAD 文件。")
            return
        if kind not in ("analyze", "build"):
            return
        self._busy(True)
        self.log.clear()
        if kind == "analyze":
            self._start(self._do_analyze)
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
        self.tabs.setCurrentIndex(2)

    def _log(self, msg: str):
        self.log.appendPlainText(msg)
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    # ------------------------------------------------------------------ actions
    def _do_analyze(self):
        p = Pipeline(self.s, self.worker.log.emit)
        return self._analyze_files(p)

    def _analyze_files(self, p: Pipeline):
        files = list(self.files)
        cad = [f for f in files if _is_cad(f)]
        img = [f for f in files if not _is_cad(f)]
        if cad and img:
            raise RuntimeError("图片与 CAD 不能混在同一个任务里。")
        if cad:
            return p.analyze_cad(files, engine=self._targets()[0], progress=p.log)
        return p.analyze_local(files, progress=p.log, require_three_views=True)

    def _apply_spec(self, spec: PartSpec):
        self.spec = spec
        if not self.steps:
            self.steps = spec_to_steps(spec)
        self.refresh_steps()


    def _do_build(self):
        p = Pipeline(self.s, self.worker.log.emit)
        targets = self._targets()
        sig = tuple(self.files)
        analysis = self._analysis if self._analysis_sig == sig else None
        if analysis is None:
            analysis = self._analyze_files(p)

        audit = dict(getattr(analysis, "audit", {}) or {})
        errs = [str(it.get("message") or "") for it in (audit.get("issues") or [])
                if it.get("severity") == "error"]
        blocked = []
        for b in list(getattr(analysis, "blocking", None) or []) + errs:
            b = str(b).strip()
            if b and b not in blocked:
                blocked.append(b)
        if blocked or not bool(audit.get("can_build", True)) or analysis.spec is None:
            res = PipelineResult(out_dir=str(Path(self.s.output_dir)), analysis=analysis)
            res.steps = list(getattr(analysis, "steps", None) or [])
            res.checks = {"drawing": False, "build": False}
            res.features_failed = ["图纸：" + x for x in blocked]
            res.message = "图纸不完整、数据矛盾或含不支持的几何，已停止建模。"
            try:
                res.report_json, res.report_md = p.write_report(res)
            except Exception:
                pass
            return res

        spec = analysis.spec
        out = str(Path(self.s.output_dir) /
                  f"{spec.part_name}_{len(os.listdir(self.s.output_dir)) if os.path.isdir(self.s.output_dir) else 0}")
        return p.run_spec(spec, targets, out, progress=p.log, analysis=analysis)

    def _on_done(self, res):
        self._busy(False)
        if isinstance(res, PipelineResult):
            if res.analysis:
                self._analysis = res.analysis
                self._analysis_sig = tuple(self.files)
                self.spec = res.analysis.spec
                if getattr(res, "steps", None):
                    self.steps = res.steps
                    self.refresh_steps()
                self._apply_spec(res.analysis.spec)
                self._show_conclusion(res.analysis)
                self.parts = res.analysis.parts
                self._fill_parts(res.analysis.parts)
                if res.checks.get("drawing") is False:
                    QMessageBox.warning(self, APP_TITLE,
                                        "图纸不能直接建模，请查看“结果”页中的具体原因。")
            self._last = res
            self._show_report(res)
            self._preview_result(res)
        else:
            from ..vision import AnalysisResult
            if isinstance(res, AnalysisResult):
                self._analysis = res
                self._analysis_sig = tuple(self.files)
                self.spec = res.spec
                self.steps = list(getattr(res, "steps", None) or spec_to_steps(res.spec))
                self.refresh_steps()
                self._apply_spec(res.spec)
                self._show_conclusion(res)
                self._fill_parts(res.parts)
                self.result.setPlainText(res.raw[:8000])
                self.tabs.setCurrentIndex(3)
                if list(getattr(res, "blocking", None) or []):
                    QMessageBox.warning(self, APP_TITLE,
                                        "图纸检查未通过：\n" +
                                        "\n".join(str(x) for x in res.blocking[:8]))
        self._refresh_status()

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
        return p.analyze_local(self.files, hint=hint, progress=p.log,
                               require_three_views=True)

    def _preview_result(self, res: PipelineResult):
        for b in res.builds:
            if b.image_path and Path(b.image_path).exists():
                px = QPixmap(b.image_path)
                if not px.isNull():
                    self.view_img.setPixmap(px.scaled(self.view_img.size() * 0.96,
                                                      Qt.KeepAspectRatio, Qt.SmoothTransformation))
                    self.tabs.setCurrentIndex(0)
                    return

    def _show_report(self, res: PipelineResult):
        lines = []
        spec = res.analysis.spec if res.analysis else None
        if spec:
            lines.append(f"零件：{spec.part_name}   {spec.length:g}×{spec.width:g}×{spec.thickness:g} mm   {spec.material}")
            lines.append(f"特征：{len(spec.features)} 个\n")
        if res.checks.get("drawing") is False:
            lines.append("❌ 图纸检查未通过，未启动 CAD。")
            for x in res.features_failed:
                lines.append("   ! " + str(x))
            lines.append("")
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
        lines.append(f"\n报告：{res.report_md}")
        self.result.setPlainText("\n".join(lines))
        self.tabs.setCurrentIndex(3)

    def _on_fail(self, err: str):
        self._busy(False)
        self._log(err)
        reason = (err.strip().splitlines() or ["未知错误"])[-1]
        self.lbl_conf.setText("⛔ 这张图纸不能建模\n" + reason[:400])
        self.lbl_conf.setStyleSheet("color:#ff6b6b;")
        self.status_lbl.setText("检查未通过：图纸数据不完整或互相矛盾")
        QMessageBox.critical(self, APP_TITLE, reason[:300])

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
