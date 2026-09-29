# -*- coding: utf-8 -*-
"""主窗口。

布局：
  顶层：QTabWidget（标签栏位于窗口最顶部）
    - 「处理」：整个处理页面
        · 动作栏（添加 / 移除 / 清空 / 开始 / 取消 /
          以 webp 格式保存到 gallery / 导出 PNG 到桌面）
        · 处理参数栏（模型 / 裁剪边距 / 主体筛选 / 模型状态）
        · 左：待处理图片列表；右：原图（含 hint）+ 处理后
        · 底部：进度条 / 状态文本 / 素材库目录
    - 「我的素材库」：MaterialLibraryWidget（卡片/列表浏览 gallery 目录）
  处理页有两个出口：「以 webp 格式保存到 gallery」入库（长边 ≤ 800 + 无损 WebP，
  存完清空列表）；「导出 PNG 到桌面」只往桌面丢原尺寸透明底 PNG，不进素材库、不动列表。
  ICO 等其它格式在素材库里按需导出。

模型文件（168MB~977MB）不进仓库：选中一个还没下载的模型时会自动从 GitHub release
下载到 <程序目录>/models（进度显示在模型状态文字上），下完离线复用。
"""

import os
from dataclasses import replace
from typing import Dict, List, Optional, Tuple

from PIL import Image
from PyQt5.QtCore import QStandardPaths, QTimer, Qt, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
    QMainWindow,
)

from .. import paths
from ..core import exporter, remover
from ..core.image_io import (
    FILE_DIALOG_FILTER,
    MAX_SIDE,
    collect_images,
    load_image,
    rotate_image,
)
from ..core.processor import Result, Settings
from .gallery import MaterialLibraryWidget
from .widgets import ImageCanvas, RotateBar
from .worker import ProcessWorker

OK_COLOR = QColor(28, 138, 74)
FAIL_COLOR = QColor(198, 56, 56)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("图片去背景工具")
        self.resize(1280, 820)
        self.setAcceptDrops(True)

        self._paths: List[str] = []
        self._results: Dict[str, Result] = {}
        # 每张图独立的 hint（开始处理时不会被清空）；都是 list，可叠加多个矩形
        # 分别记录画在「原图」和画在「处理后结果图」上的框
        self._hints: Dict[str, Tuple[List[Tuple[int, int, int, int]],
                                     List[Tuple[int, int, int, int]]]] = {}
        self._after_hints: Dict[str, Tuple[List[Tuple[int, int, int, int]],
                                          List[Tuple[int, int, int, int]]]] = {}
        # 每张图独立的撤销栈：[(source, mode, box), ...]
        self._hint_history: Dict[str, List[Tuple[str, str, Tuple[int, int, int, int]]]] = {}
        self._editing_hints: bool = False  # 是否在「修改」模式
        # 每张图各自的旋转角（顺时针为正，0 = 不转）；保存时用旋转后的结果
        self._rotations: Dict[str, int] = {}
        self._preview_cache: Dict[str, Image.Image] = {}
        self._worker: Optional[ProcessWorker] = None

        # 状态栏提示相关状态
        self._last_log_message: str = "就绪"
        self._canvas_hint_active: bool = False

        self._build_ui()
        self._refresh_model_status()

        # 旋转预览防抖：大图转一次要 200ms 上下，长按 −/+ 时先把角度记下来，
        # 停下来再重绘右上角那张图，手感才顺
        self._rotate_preview_timer = QTimer(self)
        self._rotate_preview_timer.setSingleShot(True)
        self._rotate_preview_timer.setInterval(120)
        self._rotate_preview_timer.timeout.connect(self._show_current)

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(0)

        # 顶层标签页：「我的素材库」与整个「处理」页面同级，标签栏位于窗口最顶部
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_process_page(), "处理")
        self.library = MaterialLibraryWidget(self)
        self.tabs.addTab(self.library, "我的素材库")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        root.addWidget(self.tabs, 1)

    def _build_action_bar(self) -> QWidget:
        bar = QWidget()
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)

        self.btn_add_files = QPushButton("添加图片")
        self.btn_add_folder = QPushButton("添加文件夹")
        self.btn_remove = QPushButton("移除选中")
        self.btn_clear = QPushButton("清空列表")
        self.btn_start = QPushButton("开始处理")
        self.btn_cancel = QPushButton("取消")
        self.btn_export = QPushButton("以 webp 格式保存到 gallery")
        self.btn_export.setToolTip(
            "处理结果直接入库到「我的素材库」：长边 ≤ 800px + 无损 WebP，存完清空列表"
        )
        self.btn_export_png = QPushButton("导出 PNG 到桌面")
        self.btn_export_png.setToolTip(
            "把处理结果按原尺寸导成透明底 PNG 放到桌面（不进素材库，也不清空列表）"
        )

        self.btn_start.setStyleSheet(
            "QPushButton{background:#2f7ed8;color:white;font-weight:bold;padding:6px 18px;"
            "border:none;border-radius:4px;}"
            "QPushButton:hover{background:#3d8ce6;}"
            "QPushButton:disabled{background:#b9c3cd;}"
        )

        self.btn_add_files.clicked.connect(self._add_files)
        self.btn_add_folder.clicked.connect(self._add_folder)
        self.btn_remove.clicked.connect(self._remove_selected)
        self.btn_clear.clicked.connect(self._clear_list)
        self.btn_start.clicked.connect(self._start_processing)
        self.btn_cancel.clicked.connect(self._cancel_processing)
        self.btn_export.clicked.connect(self._export)
        self.btn_export_png.clicked.connect(self._export_png_to_desktop)

        for widget in (self.btn_add_files, self.btn_add_folder, self.btn_remove, self.btn_clear):
            layout.addWidget(widget)
        layout.addSpacing(16)
        layout.addWidget(self.btn_start)
        layout.addWidget(self.btn_cancel)
        layout.addSpacing(16)
        layout.addWidget(self.btn_export)
        layout.addWidget(self.btn_export_png)
        layout.addStretch(1)
        self.btn_cancel.setEnabled(False)
        return bar

    def _build_settings_bar(self) -> QWidget:
        group = QGroupBox("处理参数")
        layout = QHBoxLayout(group)

        layout.addWidget(QLabel("模型："))
        self.model_box = QComboBox()
        for m in remover.MODELS:
            self.model_box.addItem(m.label, m.model_id)
        self.model_box.setMinimumWidth(260)
        self.model_box.currentIndexChanged.connect(self._on_model_changed)
        layout.addWidget(self.model_box)

        # 模型文件不进仓库，缺失时选中模型会自动下；这个按钮是手动重试 / 提前下载用
        self.btn_download_model = QPushButton("下载模型")
        self.btn_download_model.setToolTip(
            "模型文件不进 Git 仓库，缺失时选中该模型会自动从 GitHub release 下载"
        )
        self.btn_download_model.clicked.connect(self._download_current_model)
        self.btn_download_model.setVisible(False)
        layout.addWidget(self.btn_download_model)

        layout.addSpacing(12)
        layout.addWidget(QLabel("裁剪边距："))
        self.margin_spin = QSpinBox()
        self.margin_spin.setRange(0, 20)
        self.margin_spin.setValue(2)
        self.margin_spin.setSuffix(" px")
        self.margin_spin.setToolTip("主体裁剪框外额外保留的透明边距，防止边缘被切掉")
        layout.addWidget(self.margin_spin)

        self.largest_check = QCheckBox("只保留最大主体")
        self.largest_check.setChecked(True)
        self.largest_check.setToolTip("一张图里有多个物体时，只保留面积最大的那个")
        layout.addWidget(self.largest_check)

        self.decontaminate_check = QCheckBox("边缘去色晕（较慢）")
        layout.addWidget(self.decontaminate_check)

        self.post_process_check = QCheckBox("清理蒙版噪点（默认开启）")
        self.post_process_check.setChecked(True)
        self.post_process_check.setToolTip(
            "rembg 自带的 mask 后处理：去掉蒙版里的小噪点 / 小孔，让主体边缘更干净。"
        )
        layout.addWidget(self.post_process_check)

        layout.addStretch(1)
        self.model_status = QLabel()
        layout.addWidget(self.model_status)
        return group

    # ------------------------------------------------------------ 处理 tab

    def _build_process_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 8, 4, 4)
        layout.setSpacing(8)

        layout.addWidget(self._build_action_bar())
        layout.addWidget(self._build_settings_bar())

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_right_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([360, 900])
        layout.addWidget(splitter, 1)

        layout.addLayout(self._build_status_bar())
        return page

    def _build_left_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        group = QGroupBox("图片列表（可直接把文件或文件夹拖进来）")
        box = QVBoxLayout(group)
        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list_widget.currentItemChanged.connect(lambda *_: self._show_current())
        box.addWidget(self.list_widget)
        layout.addWidget(group, 1)
        return panel

    def _build_right_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        compare_group = QGroupBox("处理前后对比（左：原图｜右：去背景结果）")
        compare_layout = QHBoxLayout(compare_group)
        self.before_canvas = ImageCanvas(
            "处理前（原图）", selectable=False
        )
        self.before_canvas.set_image(None, "添加图片后在这里查看原图")
        self.after_canvas = ImageCanvas("处理后")
        self.after_canvas.set_image(None, "开始处理后显示去背景结果")
        # 两个画布都支持画 hint / 缩放 / 平移；框选操作通过主窗口状态分发
        self.before_canvas.rect_drawn.connect(
            lambda mode, box: self._on_hint_drawn("before", mode, box)
        )
        self.after_canvas.rect_drawn.connect(
            lambda mode, box: self._on_hint_drawn("after", mode, box)
        )
        compare_layout.addWidget(self.before_canvas, 1)
        compare_layout.addWidget(self.after_canvas, 1)
        layout.addWidget(compare_group, 1)

        # 旋转：只作用于「处理后」的结果，保存到素材库时用的就是旋转后的图
        self.rotate_bar = RotateBar()
        self.rotate_bar.angle_changed.connect(self._on_rotation_changed)
        layout.addWidget(self.rotate_bar)

        # 缩放通过滚轮 / 双击完成；鼠标悬停在图片区域时状态栏会提示操作方式。
        # 手动引导（hint）工具条：默认「修改」入口，编辑态切换红/绿框
        hint_group = self._build_hint_bar()
        layout.addWidget(hint_group)

        # 图片区域悬停时，在状态栏提示操作方式
        self.before_canvas.mouse_entered.connect(self._on_canvas_enter)
        self.before_canvas.mouse_left.connect(self._on_canvas_leave)
        self.after_canvas.mouse_entered.connect(self._on_canvas_enter)
        self.after_canvas.mouse_left.connect(self._on_canvas_leave)

        return panel

    def _build_hint_bar(self) -> QWidget:
        """手动引导工具条：默认态 = 一个「修改」按钮 + 清除；编辑态 = 红/绿/撤销/完成。

        两个态共享同一个布局，只是切换可见按钮。
        """
        group = QGroupBox("手动引导（可选）")
        layout = QHBoxLayout(group)

        # ---- 默认态 ----
        self.btn_hint_edit = QPushButton("修改")
        self.btn_hint_edit.setToolTip(
            "进入手动引导模式：在画布上直接画「要的（绿）」「不要的（红）」矩形"
        )
        self.btn_hint_edit.setStyleSheet(
            "QPushButton{background:#2f7ed8;color:white;font-weight:bold;padding:4px 18px;"
            "border:none;border-radius:4px;}"
            "QPushButton:hover{background:#3d8ce6;}"
        )
        self.btn_hint_edit.clicked.connect(self._enter_hint_edit)

        self.btn_hint_clear = QPushButton("清除本图 hint")
        self.btn_hint_clear.setToolTip("把当前图片的「主体框」和「背景框」都清掉")
        self.btn_hint_clear.clicked.connect(self._clear_hints)

        self.hint_summary = QLabel("（未框选）")
        self.hint_summary.setStyleSheet("color:#666;")

        # ---- 编辑态（默认隐藏） ----
        self.btn_hint_keep = QPushButton("画绿色框")
        self.btn_hint_keep.setToolTip(
            "在主体上画矩形，告诉程序「这块是主体」。\n"
            "  · 框外像素全部清成透明；\n"
            "  · 框内仍按模型软值保留边缘。\n"
            "可画多个、自动取并集。"
        )
        self.btn_hint_keep.setStyleSheet(
            "QPushButton{background:#28a840;color:white;font-weight:bold;"
            "padding:4px 14px;border:none;border-radius:4px;}"
            "QPushButton:checked{background:#1c8a4a;border:2px solid #fff;}"
        )
        self.btn_hint_keep.setCheckable(True)
        self.btn_hint_keep.clicked.connect(lambda: self._select_hint_mode("keep"))

        self.btn_hint_drop = QPushButton("画红色框")
        self.btn_hint_drop.setToolTip(
            "在要剔除的区域画矩形，告诉程序「这块不要」。\n"
            "  · 框内像素全部清成透明；\n"
            "可画多个。"
        )
        self.btn_hint_drop.setStyleSheet(
            "QPushButton{background:#dc3c3c;color:white;font-weight:bold;"
            "padding:4px 14px;border:none;border-radius:4px;}"
            "QPushButton:checked{background:#a02020;border:2px solid #fff;}"
        )
        self.btn_hint_drop.setCheckable(True)
        self.btn_hint_drop.clicked.connect(lambda: self._select_hint_mode("drop"))

        self.btn_hint_undo = QPushButton("撤销")
        self.btn_hint_undo.setToolTip("撤回最近一次画的矩形（也可用 Ctrl+Z）")
        self.btn_hint_undo.clicked.connect(self._undo_last_hint)

        self.btn_hint_finish = QPushButton("完成")
        self.btn_hint_finish.setToolTip("退出手动引导模式")
        self.btn_hint_finish.clicked.connect(self._exit_hint_edit)

        # Ctrl+Z 全局快捷键（在两个画布聚焦时也生效）
        from PyQt5.QtGui import QKeySequence as _QKS
        from PyQt5.QtWidgets import QShortcut as _QSC
        self._undo_shortcut = _QSC(_QKS.Undo, self)
        self._undo_shortcut.setContext(Qt.ApplicationShortcut)
        self._undo_shortcut.activated.connect(self._undo_last_hint)

        # ---- 共享 hint_summary 显示位置（默认态与编辑态各一个 label 不可行，用同一个） ----
        # 默认态先入 layout
        self._hint_bar_layout = layout
        self._hint_buttons_idle = [
            self.btn_hint_edit,
            self.btn_hint_clear,
            self.hint_summary,
        ]
        self._hint_buttons_edit = [
            self.btn_hint_keep,
            self.btn_hint_drop,
            self.btn_hint_undo,
            self.btn_hint_finish,
        ]

        for w in self._hint_buttons_idle:
            layout.addWidget(w)
        layout.addStretch(1)

        for w in self._hint_buttons_edit:
            layout.addWidget(w)
            w.hide()

        self._editing_hints = False
        self._refresh_hint_buttons_enabled()
        return group

    def _refresh_hint_bar_state(self) -> None:
        """根据 ``self._editing_hints`` 切换工具条上的可见按钮。"""
        idle_widgets = self._hint_buttons_idle
        edit_widgets = self._hint_buttons_edit
        if self._editing_hints:
            for w in idle_widgets:
                w.hide()
            for w in edit_widgets:
                w.show()
        else:
            for w in edit_widgets:
                w.hide()
            for w in idle_widgets:
                w.show()

    def _build_status_bar(self) -> QHBoxLayout:
        layout = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("%p%")
        self.progress.setFixedWidth(260)
        self.status_label = QLabel("就绪")
        layout.addWidget(self.progress)
        layout.addWidget(self.status_label, 1)
        gallery_label = QLabel(f"素材库：{paths.GALLERY_DIR}")
        gallery_label.setToolTip(paths.GALLERY_DIR)
        layout.addWidget(gallery_label)
        return layout

    # -------------------------------------------------------------- 状态

    def _refresh_model_status(self) -> None:
        """当前选中的模型状态 + 缺失时的下载按钮。"""
        model_id = self.model_box.currentData()
        if not model_id:
            self.model_status.setText("")
            self.btn_download_model.setVisible(False)
            return
        info = remover.model_info(model_id)
        short = info.label.split("（")[0].strip()

        if self._is_downloading(model_id):
            # 下载中：状态文字由进度回调接管，按钮先禁掉当进度指示
            self.model_status.setStyleSheet("color:#2f7ed8;")
            self.btn_download_model.setVisible(True)
            self.btn_download_model.setEnabled(False)
            self.btn_download_model.setText("正在下载...")
            return

        if remover.is_available(model_id):
            size_mb = remover.model_size(model_id) / (1024 * 1024)
            self.model_status.setText(f"{short}：{size_mb:.0f} MB ✓")
            self.model_status.setStyleSheet("color:#1c8a4a;")
            self.model_status.setToolTip(
                f"{info.notes}\n\n文件：{remover.model_file(model_id)}"
            )
            self.btn_download_model.setVisible(False)
            return

        self.model_status.setStyleSheet("color:#c63838;")
        if info.can_auto_download:
            self.model_status.setText(f"{short}：未下载")
            self.model_status.setToolTip(
                f"{info.notes}\n\n"
                f"选中这个模型时会自动从 GitHub release 下载（约 {info.size_mb}MB），"
                "下完即可用；也可以点右边按钮现在下。"
            )
            self.btn_download_model.setText(f"下载（约 {info.size_mb}MB）")
            self.btn_download_model.setVisible(True)
            self.btn_download_model.setEnabled(True)
        else:
            self.model_status.setText(f"⚠ 模型缺失：{os.path.basename(info.path)}")
            self.model_status.setToolTip(f"请把 onnx 文件放到：{info.path}")
            self.btn_download_model.setVisible(False)

    def _on_model_changed(self, _idx: int) -> None:
        self._refresh_model_status()
        # 模型文件不进仓库：选中一个还没下载的模型，就自动开始下
        self._auto_download_if_missing()

    # ------------------------------------------------------------ 模型下载

    def _is_downloading(self, model_id: str) -> bool:
        thread = getattr(self, "_download_thread", None)
        return (
            thread is not None
            and thread.isRunning()
            and getattr(thread, "model_id", None) == model_id
        )

    def _auto_download_if_missing(self) -> None:
        """选中的模型缺失时自动开始下载（不用额外点确认，界面上有进度）。"""
        model_id = self.model_box.currentData()
        if not model_id:
            return
        if remover.is_available(model_id) or self._is_downloading(model_id):
            return
        info = remover.model_info(model_id)
        if info.can_auto_download:
            self._start_model_download(model_id, announce=False)

    def _download_current_model(self) -> None:
        """手动下载当前选中的模型（自动下载失败后重试也走这里）。"""
        model_id = self.model_box.currentData()
        if not model_id or self._is_downloading(model_id):
            return
        info = remover.model_info(model_id)
        if not info.can_auto_download:
            return
        self._start_model_download(model_id, announce=True)

    def _start_model_download(self, model_id: str, announce: bool) -> None:
        from PyQt5.QtCore import QThread

        info = remover.model_info(model_id)
        if announce:
            reply = QMessageBox.question(
                self,
                "下载模型",
                f"{info.label}\n\n"
                f"模型文件约 {info.size_mb}MB，需要联网从 GitHub release 下载。\n\n"
                "下载位置：\n"
                f"{info.path}\n\n"
                "继续下载吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if reply != QMessageBox.Yes:
                return

        self.btn_download_model.setEnabled(False)
        self.btn_download_model.setText("正在下载...")
        self.model_status.setStyleSheet("color:#2f7ed8;")
        self.model_status.setText(f"正在连接 GitHub 下载 {info.label} ...")
        self._log(f"开始下载模型 {info.label}（约 {info.size_mb}MB）")

        # 下载放线程里跑，UI 不卡死；进度通过信号回到状态栏
        class _DownloadThread(QThread):
            done = pyqtSignal(bool, str)
            tick = pyqtSignal(int, int, str)

            def __init__(self, mid):
                super().__init__()
                self.model_id = mid

            def run(self):
                try:
                    remover.download_model(
                        self.model_id,
                        progress=lambda d, t, s: self.tick.emit(d, t, s),
                    )
                    self.done.emit(True, "")
                except Exception as exc:  # noqa: BLE001
                    self.done.emit(False, str(exc))

        thread = _DownloadThread(model_id)
        self._download_thread = thread
        thread.tick.connect(self._on_download_tick)
        thread.done.connect(
            lambda ok, msg, i=info, a=announce: self._on_download_done(i, ok, msg, a)
        )
        thread.start()

    def _on_download_tick(self, _done: int, _total: int, text: str) -> None:
        """下载进度：只更新状态文字，别顺手写日志（一次下载上百条进度）。"""
        thread = getattr(self, "_download_thread", None)
        if thread is not None and self.model_box.currentData() == thread.model_id:
            self.model_status.setText(text)
            self.model_status.setToolTip(text)

    def _on_download_done(
        self, info: remover.ModelInfo, ok: bool, msg: str, announce: bool
    ) -> None:
        self.btn_download_model.setEnabled(True)
        if ok:
            self._log(f"模型已就绪：{info.label}")
            if announce:
                QMessageBox.information(
                    self,
                    "下载完成",
                    f"{info.label} 已就绪。\n\n以后直接离线使用，不会重复下载。",
                )
            else:
                self._log("（选中模型后自动下载完成）")
        else:
            self._log(f"模型下载失败：{msg.splitlines()[0] if msg else '未知原因'}")
            if announce:
                QMessageBox.critical(
                    self,
                    "下载失败",
                    f"{info.label} 下载失败：\n{msg}\n\n"
                    "可能原因：\n"
                    "  · 网络不通（国内访问 GitHub 较慢）\n"
                    "  · 防火墙 / 代理拦截\n"
                    f"  · 磁盘空间不足（需 ~{info.size_mb}MB）\n\n"
                    f"也可以手动下载后放进 models 目录：\n{info.source_url}",
                )
        self._refresh_model_status()

    def _set_busy(self, busy: bool) -> None:
        self.btn_start.setEnabled(not busy)
        self.btn_cancel.setEnabled(busy)
        self.btn_add_files.setEnabled(not busy)
        self.btn_add_folder.setEnabled(not busy)
        self.btn_remove.setEnabled(not busy)
        self.btn_clear.setEnabled(not busy)
        self.btn_export.setEnabled(not busy)
        self.btn_export_png.setEnabled(not busy)
        # 处理期间别让用户手动触发模型下载（worker 里缺模型会自己下，会撞车）
        if busy:
            self.btn_download_model.setEnabled(False)
        else:
            self._refresh_model_status()

    def _settings(self) -> Settings:
        """构造一次通用 Settings（hint 由 worker 每张图单独装）。"""
        return Settings(
            model=self.model_box.currentData(),
            margin=self.margin_spin.value(),
            largest_only=self.largest_check.isChecked(),
            decontaminate=self.decontaminate_check.isChecked(),
            post_process=self.post_process_check.isChecked(),
        )

    # ------------------------------------------------------------ 列表操作

    def _add_paths(self, raw_paths) -> None:
        new_paths = collect_images(raw_paths, recursive=True)
        added = 0
        for path in new_paths:
            if path in self._paths:
                continue
            self._paths.append(path)
            item = QListWidgetItem(os.path.basename(path))
            item.setData(Qt.UserRole, path)
            item.setToolTip(path)
            self.list_widget.addItem(item)
            added += 1

        if self.list_widget.currentRow() < 0 and self.list_widget.count() > 0:
            self.list_widget.setCurrentRow(0)

        skipped = len(new_paths) - added
        message = f"新增 {added} 张图片"
        if skipped:
            message += f"，跳过 {skipped} 张重复图片"
        self._log(message)

    def _add_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择图片", "", FILE_DIALOG_FILTER
        )
        if files:
            self._add_paths(files)

    def _add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择文件夹")
        if folder:
            self._add_paths([folder])

    def _remove_selected(self) -> None:
        for item in self.list_widget.selectedItems():
            path = item.data(Qt.UserRole)
            self._paths = [p for p in self._paths if p != path]
            self._results.pop(path, None)
            self._hints.pop(path, None)
            self._after_hints.pop(path, None)
            self._hint_history.pop(path, None)
            self._rotations.pop(path, None)
            self._preview_cache.pop(path, None)
            self.list_widget.takeItem(self.list_widget.row(item))
        self._show_current()

    def _clear_list(self, announce: bool = True) -> None:
        self.list_widget.clear()
        self._paths.clear()
        self._results.clear()
        self._hints.clear()
        self._after_hints.clear()
        self._hint_history.clear()
        self._rotations.clear()
        self._preview_cache.clear()
        self._show_current()
        self.progress.setValue(0)
        if announce:
            self._log("已清空列表")

    def _item_for_path(self, path: str) -> Optional[QListWidgetItem]:
        for row in range(self.list_widget.count()):
            item = self.list_widget.item(row)
            if item.data(Qt.UserRole) == path:
                return item
        return None

    def _set_item_state(self, item: QListWidgetItem, text: str, color: Optional[QColor]) -> None:
        item.setText(text)
        if color is not None:
            item.setForeground(color)

    # ------------------------------------------------------------ 拖拽支持

    def dragEnterEvent(self, event):  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):  # noqa: N802
        paths_dropped = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths_dropped:
            self._add_paths(paths_dropped)
            event.acceptProposedAction()

    # ------------------------------------------------------------ 处理

    def _start_processing(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        if not self._paths:
            QMessageBox.information(self, "提示", "请先添加图片。")
            return
        # 模型没下载也不拦着：worker 里会先自动下（进度显示在状态栏），下完接着处理
        model_id = self.model_box.currentData()
        info = remover.model_info(model_id)
        if not remover.is_available(model_id):
            if not info.can_auto_download:
                QMessageBox.warning(
                    self,
                    "模型缺失",
                    f"找不到模型文件：\n{info.path}\n\n"
                    "请把 .onnx 文件放进项目 models 目录后重试。",
                )
                return
            if not self._is_downloading(model_id):
                self._log(
                    f"模型 {info.label} 尚未下载（约 {info.size_mb}MB），"
                    "先自动下载再开始处理..."
                )

        # 保留上次结果，以便二次迭代时以上次的 result.after 作为输入
        prev_results = dict(self._results)
        self._results.clear()
        for row in range(self.list_widget.count()):
            item = self.list_widget.item(row)
            item.setForeground(QColor(90, 96, 104))
            item.setText(os.path.basename(item.data(Qt.UserRole)))
        self._show_current()

        self.progress.setRange(0, len(self._paths))
        self.progress.setValue(0)
        self._set_busy(True)
        self._log(f"开始处理 {len(self._paths)} 张图片（CPU 推理，请稍候）")

        self._worker = ProcessWorker(
            self._paths,
            self._settings(),
            self,
            hint_for=self._hint_for_path,
            prev_results=prev_results,
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.item_done.connect(self._on_item_done)
        self._worker.all_done.connect(self._on_all_done)
        self._worker.start()

    def _cancel_processing(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._log("正在取消，当前图片处理完即停止...")

    def _on_progress(self, done: int, total: int, name: str) -> None:
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(done)
        self._log(f"[{done}/{total}] {name}")

    def _on_item_done(self, result: Result) -> None:
        self._results[result.src] = result
        item = self._item_for_path(result.src)
        name = os.path.basename(result.src)
        if item is not None:
            if result.ok:
                self._set_item_state(item, f"{name}   ✓ {result.size_text}", OK_COLOR)
            else:
                self._set_item_state(item, f"{name}   ✗ {result.message}", FAIL_COLOR)

        # 若本次是以 result.after 作为输入完成的迭代，消费掉 after hint，只保留最终结果
        if result.ok and result.used_after_input:
            self._after_hints.pop(result.src, None)
            hist = self._hint_history.get(result.src, [])
            self._hint_history[result.src] = [
                (s, m, b) for s, m, b in hist if s != "after"
            ]

        current = self.list_widget.currentItem()
        if current is not None and current.data(Qt.UserRole) == result.src:
            self._show_current()

    def _on_all_done(self) -> None:
        self._set_busy(False)
        done = sum(1 for r in self._results.values() if r.ok)
        failed = sum(1 for r in self._results.values() if not r.ok)
        self.progress.setValue(self.progress.maximum())

        if self._worker is not None and self._worker.cancelled:
            self._log(f"已取消：成功 {done} 张，失败 {failed} 张")
        else:
            self._log(
                f"处理完成：成功 {done} 张，失败 {failed} 张。"
                "可以「以 webp 格式保存到 gallery」或「导出 PNG 到桌面」。"
            )
        self._refresh_model_status()

    # ------------------------------------------------------------ hint

    def _current_path(self) -> Optional[str]:
        item = self.list_widget.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _any_hints_for(self, path: Optional[str]) -> bool:
        if path is None:
            return False
        before = self._hints.get(path, ([], []))
        after = self._after_hints.get(path, ([], []))
        return bool(before[0] or before[1] or after[0] or after[1])

    def _refresh_hint_buttons_enabled(self) -> None:
        has = self._current_path() is not None
        path = self._current_path()
        # 默认态：编辑、清除；编辑态：红/绿/撤销/完成
        self.btn_hint_edit.setEnabled(has)
        self.btn_hint_clear.setEnabled(
            has and not self._editing_hints and self._any_hints_for(path)
        )
        if self._editing_hints:
            self.btn_hint_keep.setEnabled(True)
            self.btn_hint_drop.setEnabled(True)
            history = self._hint_history.get(path or "", [])
            self.btn_hint_undo.setEnabled(bool(history))

    def _refresh_hint_summary(self) -> None:
        path = self._current_path()
        if path is None:
            self.hint_summary.setText("（未框选）")
            return
        before_keep, before_drop = self._hints.get(path, ([], []))
        after_keep, after_drop = self._after_hints.get(path, ([], []))
        keep = list(before_keep) + list(after_keep)
        drop = list(before_drop) + list(after_drop)
        if not keep and not drop:
            self.hint_summary.setText("（未框选）")
        else:
            parts = []
            if keep:
                parts.append(f"主体框 {len(keep)} 个")
            if drop:
                parts.append(f"背景框 {len(drop)} 个")
            self.hint_summary.setText(" ｜ ".join(parts))

    def _hint_for_path(self, path: str) -> Tuple[List[Tuple[int, int, int, int]],
                                                  List[Tuple[int, int, int, int]],
                                                  bool]:
        """返回 (keep_boxes, drop_boxes, use_after_input)。

        如果用户在「处理后」画布上画了 hint，则以上一次的 result.after 作为输入图；
        否则使用原图 + 处理前 hint。
        """
        after_keep, after_drop = self._after_hints.get(path, ([], []))
        if after_keep or after_drop:
            return list(after_keep), list(after_drop), True
        before_keep, before_drop = self._hints.get(path, ([], []))
        return list(before_keep), list(before_drop), False

    def _set_hints(self, path: str,
                   keep_boxes: List[Tuple[int, int, int, int]],
                   drop_boxes: List[Tuple[int, int, int, int]]) -> None:
        if not keep_boxes and not drop_boxes:
            self._hints.pop(path, None)
            self._hint_history.pop(path, None)
        else:
            self._hints[path] = (list(keep_boxes), list(drop_boxes))

    # ---- 编辑模式 ----

    def _enter_hint_edit(self) -> None:
        path = self._current_path()
        if path is None:
            return
        # 必须先有原图才能画
        if self._load_preview(path) is None:
            QMessageBox.warning(self, "无法加载原图", "该图片无法用于手动引导。")
            return
        self._editing_hints = True
        # 默认笔刷 = 绿色（要的）；两个画布都进入可绘制状态
        self.btn_hint_keep.setChecked(True)
        self.btn_hint_drop.setChecked(False)
        self._sync_canvas_hint_modes()
        self._refresh_hint_bar_state()
        self._refresh_hint_buttons_enabled()
        # 进入编辑态时也把焦点交给画布，方便滚轮缩放
        self.before_canvas.setFocus()

    def _exit_hint_edit(self) -> None:
        self._editing_hints = False
        self.before_canvas.set_hint_mode(None)
        self.after_canvas.set_hint_mode(None)
        self.btn_hint_keep.setChecked(False)
        self.btn_hint_drop.setChecked(False)
        self._refresh_hint_bar_state()
        self._refresh_hint_buttons_enabled()
        self._refresh_list_item_hint_tag(self._current_path())

    def _select_hint_mode(self, mode: str) -> None:
        if not self._editing_hints:
            return
        if mode == "keep":
            self.btn_hint_keep.setChecked(True)
            self.btn_hint_drop.setChecked(False)
        else:
            self.btn_hint_keep.setChecked(False)
            self.btn_hint_drop.setChecked(True)
        self._sync_canvas_hint_modes()

    def _sync_canvas_hint_modes(self) -> None:
        """按当前状态给两块画布装画笔。

        结果图一旦被旋转过，它上面的坐标就跟 ``result.after`` 对不上了，
        所以旋转过的结果图不开放画框（想画就先「归零」）。
        """
        mode: Optional[str] = None
        if self._editing_hints:
            mode = "drop" if self.btn_hint_drop.isChecked() else "keep"
        path = self._current_path()
        rotated = bool(self._rotations.get(path or "", 0))
        self.before_canvas.set_hint_mode(mode)
        self.after_canvas.set_hint_mode(None if rotated else mode)
        if rotated and self._editing_hints:
            self.status_label.setText(
                "结果图已旋转，暂不能在结果图上画框；要画请先「归零」，或在左图（原图）上画"
            )

    def _undo_last_hint(self) -> None:
        path = self._current_path()
        if path is None:
            return
        history = self._hint_history.get(path)
        if not history:
            return
        source, mode, _box = history.pop()
        if source == "before":
            keep, drop = self._hints.get(path, ([], []))
        else:
            keep, drop = self._after_hints.get(path, ([], []))
        if mode == "keep" and keep:
            keep.pop()
        elif mode == "drop" and drop:
            drop.pop()
        if source == "before":
            if keep or drop:
                self._hints[path] = (keep, drop)
            else:
                self._hints.pop(path, None)
            self.before_canvas.set_hints(*self._hints.get(path, ([], [])))
        else:
            if keep or drop:
                self._after_hints[path] = (keep, drop)
            else:
                self._after_hints.pop(path, None)
            self.after_canvas.set_hints(*self._after_hints.get(path, ([], [])))
        self._refresh_hint_summary()
        self._refresh_hint_buttons_enabled()
        self._refresh_list_item_hint_tag(path)

    def _on_hint_drawn(self, source: str, mode: str, box: Tuple[int, int, int, int]) -> None:
        path = self._current_path()
        if path is None or not self._editing_hints:
            return
        if source == "before":
            keep, drop = self._hints.get(path, ([], []))
        else:
            keep, drop = self._after_hints.get(path, ([], []))
        if mode == "keep":
            keep = list(keep) + [box]
        else:
            drop = list(drop) + [box]
        if source == "before":
            self._hints[path] = (keep, drop)
            self.before_canvas.add_hint(mode, box)
        else:
            self._after_hints[path] = (keep, drop)
            self.after_canvas.add_hint(mode, box)
        self._hint_history.setdefault(path, []).append((source, mode, box))
        self._refresh_hint_summary()
        self._refresh_hint_buttons_enabled()
        self._refresh_list_item_hint_tag(path)

    def _refresh_list_item_hint_tag(self, path: Optional[str]) -> None:
        if path is None:
            return
        item = self._item_for_path(path)
        if item is None or item.foreground().color() == OK_COLOR:
            return
        before_keep, before_drop = self._hints.get(path, ([], []))
        after_keep, after_drop = self._after_hints.get(path, ([], []))
        keep = list(before_keep) + list(after_keep)
        drop = list(before_drop) + list(after_drop)
        name = os.path.basename(path)
        if not keep and not drop:
            item.setText(name)
            item.setForeground(QColor(90, 96, 104))
        else:
            parts = []
            if keep:
                parts.append(f"主体×{len(keep)}")
            if drop:
                parts.append(f"背景×{len(drop)}")
            item.setText(f"{name}   · {'/'.join(parts)}")
            item.setForeground(QColor(180, 132, 0))

    def _clear_hints(self) -> None:
        path = self._current_path()
        if path is None:
            return
        self._hints.pop(path, None)
        self._after_hints.pop(path, None)
        self._hint_history.pop(path, None)
        self.before_canvas.set_hints([], [])
        self.after_canvas.set_hints([], [])
        self._refresh_hint_summary()
        self._refresh_hint_buttons_enabled()
        self._refresh_list_item_hint_tag(path)

    # ------------------------------------------------------------ 预览

    def _load_preview(self, path: str) -> Optional[Image.Image]:
        if path in self._preview_cache:
            return self._preview_cache[path]
        try:
            image = load_image(path)
        except Exception:  # noqa: BLE001
            return None
        if len(self._preview_cache) > 4:
            self._preview_cache.clear()
        self._preview_cache[path] = image
        return image

    def _show_current(self) -> None:
        self._refresh_hint_buttons_enabled()
        self._refresh_hint_summary()

        item = self.list_widget.currentItem()
        if item is None:
            self.before_canvas.set_image(None, "添加图片后在这里查看原图")
            self.after_canvas.set_image(None, "开始处理后显示去背景结果")
            self.rotate_bar.set_angle(0, notify=False)
            self.rotate_bar.set_enabled(False)
            return

        path = item.data(Qt.UserRole)
        result = self._results.get(path)
        keep_hint, drop_hint = self._hints.get(path, ([], []))
        after_keep, after_drop = self._after_hints.get(path, ([], []))

        # 左侧（原图）永远显示原图（含处理前 hint）；右侧（处理后）按 result 显示
        image = self._load_preview(path)
        if image is None:
            self.before_canvas.set_image(None, "无法预览该图片")
            self.before_canvas.set_hints(keep_hint, drop_hint)
        else:
            sub_hint = ""
            if keep_hint or drop_hint:
                k = len(keep_hint)
                d = len(drop_hint)
                sub_hint = (
                    f" ｜ hint: 主体×{k} 背景×{d}"
                    if (k or d) else ""
                )
            self.before_canvas.set_image(
                image,
                f"原图 {os.path.basename(path)} {image.width} × {image.height}{sub_hint}",
            )
            self.before_canvas.set_hints(keep_hint, drop_hint)

        angle = self._rotations.get(path, 0)
        has_after = result is not None and result.ok and result.after is not None
        if has_after:
            # 旋转默认接着按主体裁一遍，把转出来的透明角落清掉
            after_image = rotate_image(result.after, angle) if angle else result.after
            subtitle = f"处理后（透明底）{after_image.width} × {after_image.height}"
            if angle:
                subtitle += f" ｜ 旋转 {angle}° + 已清理空白"
            self.after_canvas.set_image(after_image, subtitle)
            self.after_canvas.set_hints(after_keep, after_drop)
        else:
            self.after_canvas.set_square(None)
            self.after_canvas.set_hints(after_keep, after_drop)
            if result is not None and not result.ok:
                self.after_canvas.set_image(None, f"失败原因：{result.message}")
            else:
                self.after_canvas.set_image(None, "尚未处理")

        self.rotate_bar.set_angle(angle, notify=False)
        self.rotate_bar.set_enabled(has_after)

    # ---------------------------------------------------------------- 旋转

    def _on_rotation_changed(self, angle: int) -> None:
        path = self._current_path()
        if path is None:
            return
        if angle:
            self._rotations[path] = angle
        else:
            self._rotations.pop(path, None)
        self._sync_canvas_hint_modes()
        self._rotate_preview_timer.start()  # 防抖，见 __init__ 里的说明
        if angle:
            self._log(
                f"{os.path.basename(path)} 旋转 {angle}°（已顺带清理空白，"
                "保存到素材库时用这个方向）"
            )
        else:
            self._log(f"{os.path.basename(path)} 旋转已归零")

    # ---------------------------------------------------------------- 保存 / 导出

    def _rotated_results(self) -> List[Result]:
        """两个出口共用的准备步骤：按每张图各自的旋转角转正（转完自动清掉空白）。"""
        out: List[Result] = []
        for path in self._paths:
            result = self._results.get(path)
            if result is None:
                continue
            angle = self._rotations.get(path, 0)
            if angle and result.ok and result.after is not None:
                result = replace(result, after=rotate_image(result.after, angle))
            out.append(result)
        return out

    def _export(self) -> None:
        """把去背景结果写进 gallery 目录（我的素材库）：长边 ≤ 800 + 无损 WebP。"""
        to_export = self._rotated_results()
        if not to_export:
            QMessageBox.information(self, "提示", "还没有处理结果，请先开始处理。")
            return

        try:
            # 入库统一是「长边 ≤ 800 + 无损 WebP」，其它格式（ICO 等）到素材库里再导
            report = exporter.export_webp(to_export, paths.GALLERY_DIR)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "保存失败", str(exc))
            return

        saved_count = len(report["webp"])  # type: ignore[arg-type]
        errors = report["errors"]  # type: ignore[assignment]

        lines = [
            f"已保存 {saved_count} 张到素材库（长边 ≤ {MAX_SIDE}px，无损 WebP）。"
        ]
        if errors:
            lines.append(f"失败：{len(errors)} 个")
            lines.append("")
            lines.extend(f"· {name}：{reason}" for name, reason in errors[:8])
            if len(errors) > 8:
                lines.append(f"· ... 其余 {len(errors) - 8} 个")
        lines.append("")
        lines.append(f"素材库目录：{paths.GALLERY_DIR}")
        if saved_count:
            lines.append("")
            lines.append("保存后处理列表会自动清空，方便直接开始下一批。")

        self.library.refresh()

        box = QMessageBox(self)
        box.setWindowTitle("已保存到素材库")
        box.setIcon(QMessageBox.Warning if errors else QMessageBox.Information)
        box.setText("\n".join(lines))
        open_btn = box.addButton("打开目录", QMessageBox.ActionRole)
        switch_btn = box.addButton("去「我的素材库」看看", QMessageBox.ActionRole)
        box.addButton("关闭", QMessageBox.AcceptRole)
        box.exec_()
        clicked = box.clickedButton()
        if clicked is open_btn:
            try:
                os.startfile(paths.GALLERY_DIR)  # noqa: S606
            except Exception:  # noqa: BLE001
                pass
        elif clicked is switch_btn:
            self.tabs.setCurrentIndex(1)

        # 存下来了就把处理列表清空，接着处理下一批
        if saved_count:
            self._clear_list(announce=False)
            self._log(f"已保存 {saved_count} 张到素材库，列表已清空")
        else:
            self._log("没有图片保存成功，列表保留以便重试")

    @staticmethod
    def _desktop_dir() -> str:
        """桌面路径：优先用系统给的位置（兼容 OneDrive 重定向过去的桌面）。"""
        desktop = QStandardPaths.writableLocation(QStandardPaths.DesktopLocation)
        if not desktop or not os.path.isdir(desktop):
            desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        os.makedirs(desktop, exist_ok=True)
        return desktop

    def _export_png_to_desktop(self) -> None:
        """把结果导成原尺寸透明底 PNG 到桌面。

        和入库那条路完全独立：**不进素材库**、**不清空列表**，方便同一批图既丢桌面一份、
        又留在这里继续改（比如再调旋转角度后入库）。
        """
        to_export = self._rotated_results()
        if not to_export:
            QMessageBox.information(self, "提示", "还没有处理结果，请先开始处理。")
            return

        desktop = self._desktop_dir()
        try:
            # 桌面这份要拿去别处用，保留原尺寸（PNG 无损，不缩），也不生成 ICO
            report = exporter.export_results(to_export, desktop, True, False, [])
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "导出失败", str(exc))
            return

        count = len(report["png"])  # type: ignore[arg-type]
        errors = report["errors"]  # type: ignore[assignment]

        lines = [f"已导出 {count} 张透明底 PNG 到桌面（原尺寸，未放进素材库）。"]
        if errors:
            lines.append(f"失败：{len(errors)} 个")
            lines.append("")
            lines.extend(f"· {name}：{reason}" for name, reason in errors[:8])
            if len(errors) > 8:
                lines.append(f"· ... 其余 {len(errors) - 8} 个")
        lines.append("")
        lines.append(f"桌面目录：{desktop}")
        lines.append("")
        lines.append("处理列表保持不变，可以接着入库或处理下一批。")

        box = QMessageBox(self)
        box.setWindowTitle("已导出 PNG 到桌面")
        box.setIcon(QMessageBox.Warning if errors else QMessageBox.Information)
        box.setText("\n".join(lines))
        open_btn = box.addButton("打开桌面", QMessageBox.ActionRole)
        box.addButton("关闭", QMessageBox.AcceptRole)
        box.exec_()
        if box.clickedButton() is open_btn:
            try:
                os.startfile(desktop)  # noqa: S606
            except Exception:  # noqa: BLE001
                pass

        self._log(f"已导出 {count} 张 PNG 到桌面：{desktop}")

    # ---------------------------------------------------------------- 杂项

    def _on_tab_changed(self, index: int) -> None:
        """切到素材库时重新扫一遍目录，保证刚保存的图片能看到。"""
        if self.tabs.widget(index) is self.library:
            self.library.refresh()


    def _log(self, message: str) -> None:
        self._last_log_message = message
        self.status_label.setText(message)

    def _on_canvas_enter(self) -> None:
        self._canvas_hint_active = True
        self.status_label.setText("拖拽：空格 / Alt + 左键｜中键｜滚轮缩放")

    def _on_canvas_leave(self) -> None:
        self._canvas_hint_active = False
        self.status_label.setText(self._last_log_message or "就绪")

    def closeEvent(self, event):  # noqa: N802
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(5000)
        # 素材库的缩略图线程也要收干净，否则进程退出时 Qt 会直接 abort
        self.library.stop_thumbnails()
        event.accept()
