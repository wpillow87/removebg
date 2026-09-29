# -*- coding: utf-8 -*-
"""「我的素材库」页面：浏览 gallery 目录里已经处理好的图片。

  · 两种展示模式：卡片（B 站封面那种编排）/ 列表
  · 排序：添加时间 / 名称 / 大小
  · 分组：不分组 / 按标签分组（标签写在文件名里：``名称[标签1][标签2].png``）
  · 只有列表模式右侧带预览面板（约占界面 1/8，位于右上角，下方跟着文件信息）
  · F2 = 「重命名 & 分组」，直接改磁盘上的真实文件名
"""

import os
import time
from typing import Dict, List, Optional, Sequence, Tuple

from PIL import Image
from PyQt5.QtCore import QPoint, QRect, QRectF, QSize, Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFontMetrics, QIcon, QImage, QKeySequence, QPainter
from PyQt5.QtGui import QPainterPath, QPixmap
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QShortcut,
    QSizePolicy,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from .. import paths
from ..core import exporter
from ..core import tags as taglib
from ..core.image_io import (
    FILE_DIALOG_FILTER,
    MAX_SIDE,
    can_rotate_in_place,
    is_supported,
    list_images_in,
    load_image,
    resize_to_max,
    rotate_image,
    save_ico,
    save_image,
    save_png,
    save_webp_lossless,
    trim_to_content,
)
from .rename_dialog import RenameGroupDialog
from .widgets import ImageCanvas

# ---- item 数据角色 ----
ROLE_KIND = Qt.UserRole + 1  # "file" / "header"
ROLE_PATH = Qt.UserRole + 2
ROLE_BASE = Qt.UserRole + 3
ROLE_TAGS = Qt.UserRole + 4
ROLE_EXT = Qt.UserRole + 5

UNTAGGED = "未分组"

# ---- 尺寸常量 ----
# 图片边长只提供这三档，卡片和列表共用同一个值
ICON_SIZE_CHOICES = (40, 100, 160)
ICON_SIZE_DEFAULT = 100
ITEM_SPACING = 0  # 间距固定 0

# 卡片外边距 / 文字区高度（相对图片边长的固定增量）
CARD_PAD_X = 26
CARD_TEXT_H = 64      # 「显示文件名及标签」勾上时给两行文字留的高度
CARD_NO_TEXT_H = 20   # 不显示文字时只留一点点内边距
HEADER_HEIGHT = 30
LIST_ROW_PAD = 10

THUMB_SIZE = 320  # 缩略图生成尺寸，取最大档 160 的 2 倍
MAX_THUMBS = 600  # 超过这个数量就不生成缩略图了，避免一次读爆内存

SORT_MODES: List[Tuple[str, str]] = [
    ("添加时间（新 → 旧）", "ctime_desc"),
    ("添加时间（旧 → 新）", "ctime_asc"),
    ("名称（A → Z）", "name_asc"),
    ("名称（Z → A）", "name_desc"),
    ("大小（大 → 小）", "size_desc"),
    ("大小（小 → 大）", "size_asc"),
]

GROUP_MODES: List[Tuple[str, str]] = [
    ("不分组", "none"),
    ("按标签分组", "tag"),
]


# ==================================================================== 数据


class FileEntry:
    """素材库里的一张图片。"""

    __slots__ = ("path", "base", "tags", "ext", "size", "ctime", "mtime", "mtime_ns")

    def __init__(self, path: str):
        self.path = path
        self.base, self.tags, self.ext = taglib.split_name(os.path.basename(path))
        try:
            stat = os.stat(path)  # 本来就要 stat 一次拿大小/时间，顺手把 mtime_ns 也留下
            self.size = stat.st_size
            self.ctime = stat.st_ctime  # Windows 上是「创建时间」≈ 加入素材库的时间
            self.mtime = stat.st_mtime
            self.mtime_ns = stat.st_mtime_ns
        except OSError:
            self.size = 0
            self.ctime = self.mtime = 0.0
            self.mtime_ns = 0

    @property
    def version(self) -> Tuple[int, int]:
        """缓存版本号：文件一被改写（旋转 / 清空白 / 外部软件改动）就会变。"""
        return (self.mtime_ns, self.size)


def _human_size(num: int) -> str:
    if num < 1024:
        return f"{num} B"
    if num < 1024 * 1024:
        return f"{num / 1024:.1f} KB"
    return f"{num / (1024 * 1024):.2f} MB"


def _fmt_time(ts: float) -> str:
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


# ==================================================================== 缩略图


_placeholder_cache: Dict[int, QPixmap] = {}


def _placeholder_pixmap(side: int) -> QPixmap:
    """没有缩略图时用的浅灰占位图。"""
    if side not in _placeholder_cache:
        pm = QPixmap(side, side)
        pm.fill(QColor(235, 238, 242))
        painter = QPainter(pm)
        painter.setPen(QColor(208, 215, 223))
        painter.drawRect(0, 0, side - 1, side - 1)
        painter.setPen(QColor(168, 176, 186))
        painter.drawText(pm.rect(), Qt.AlignCenter, "…")
        painter.end()
        _placeholder_cache[side] = pm
    return _placeholder_cache[side]


def _rounded_pixmap(image: QImage, radius: int = 14) -> QPixmap:
    """把缩略图裁成圆角，接近视频封面的观感。"""
    src = QPixmap.fromImage(image)
    if src.isNull():
        return src
    out = QPixmap(src.size())
    out.fill(Qt.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(
        QRectF(0, 0, src.width(), src.height()), radius, radius
    )
    painter.setClipPath(path)
    painter.drawPixmap(0, 0, src)
    painter.end()
    return out


class _ThumbnailLoader(QThread):
    """后台串行生成缩略图，避免大图把界面卡死。"""

    loaded = pyqtSignal(str, QImage)

    def __init__(self, paths_: Sequence[str], side: int = THUMB_SIZE, parent=None):
        super().__init__(parent)
        self._paths = list(paths_)
        self._side = side
        self._stop = False

    def cancel(self) -> None:
        self._stop = True

    def run(self) -> None:  # noqa: D102
        for path in self._paths:
            if self._stop:
                return
            try:
                image = load_image(path)
                image.thumbnail((self._side, self._side), Image.LANCZOS)
                qimage = QImage(
                    image.tobytes("raw", "RGBA"),
                    image.width,
                    image.height,
                    QImage.Format_RGBA8888,
                ).copy()
            except Exception:  # noqa: BLE001
                continue
            if self._stop:
                return
            self.loaded.emit(path, qimage)


# ==================================================================== 视图


class _LibraryView(QListWidget):
    """列表控件，尺寸变化时通知外层重算「分组标题」的整行宽度。"""

    resized = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_Hover, True)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self.resized.emit()


class _LibraryDelegate(QStyledItemDelegate):
    """卡片 / 列表两种观感都自己画，分组标题整行铺满。"""

    def __init__(self, owner: "MaterialLibraryWidget", parent=None):
        super().__init__(parent)
        self._owner = owner

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        hint = index.data(Qt.SizeHintRole)
        if isinstance(hint, QSize):
            return hint
        return super().sizeHint(option, index)

    # ---- 绘制 ----

    def paint(self, painter, option, index):  # noqa: N802
        kind = index.data(ROLE_KIND)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        if kind == "header":
            self._paint_header(painter, option, index)
        elif self._owner.view_mode() == "card":
            self._paint_card(painter, option, index)
        else:
            self._paint_row(painter, option, index)
        painter.restore()

    @staticmethod
    def _paint_header(painter, option, index) -> None:
        rect = option.rect
        painter.fillRect(rect, QColor(234, 241, 250))
        painter.setPen(QColor(47, 126, 216))
        font = painter.font()
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(
            rect.adjusted(10, 0, -10, 0),
            Qt.AlignLeft | Qt.AlignVCenter,
            index.data(Qt.DisplayRole) or "",
        )

    @staticmethod
    def _elide(painter, text: str, width: int) -> str:
        return painter.fontMetrics().elidedText(text, Qt.ElideRight, max(0, width))

    @staticmethod
    def _icon_pixmap(index, size: int) -> QPixmap:
        icon = index.data(Qt.DecorationRole)
        if isinstance(icon, QIcon) and not icon.isNull():
            return icon.pixmap(QSize(size, size))
        return QPixmap()

    def _paint_card(self, painter, option, index) -> None:
        rect = option.rect
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)
        if selected:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(214, 232, 253))
            painter.drawRoundedRect(rect.adjusted(3, 3, -3, -3), 10, 10)
        elif hovered:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(244, 247, 251))
            painter.drawRoundedRect(rect.adjusted(3, 3, -3, -3), 10, 10)

        # 封面
        icon_side = self._owner.card_icon()
        cover = QRect(0, 0, icon_side, icon_side)
        cover.moveCenter(QPoint(rect.center().x(), rect.top() + 10 + icon_side // 2))
        pixmap = self._icon_pixmap(index, icon_side)
        if pixmap.isNull():
            painter.drawPixmap(cover, _placeholder_pixmap(icon_side))
        else:
            scaled = pixmap.scaled(
                icon_side, icon_side, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            target = QRect(0, 0, scaled.width(), scaled.height())
            target.moveCenter(cover.center())
            painter.drawPixmap(target, scaled)

        if not self._owner.show_text():
            return

        # 名称
        name_rect = QRect(rect.left() + 8, cover.bottom() + 8, rect.width() - 16, 20)
        painter.setPen(QColor(38, 42, 48))
        font = painter.font()
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(
            name_rect,
            Qt.AlignHCenter | Qt.AlignVCenter,
            self._elide(painter, index.data(ROLE_BASE) or "", name_rect.width()),
        )

        # 分组（标签）
        tags = index.data(ROLE_TAGS) or []
        tag_text = "  ".join(f"#{t}" for t in tags) if tags else UNTAGGED
        tag_rect = name_rect.translated(0, 21)
        painter.setPen(QColor(142, 150, 160))
        font.setBold(False)
        painter.setFont(font)
        painter.drawText(
            tag_rect,
            Qt.AlignHCenter | Qt.AlignVCenter,
            self._elide(painter, tag_text, tag_rect.width()),
        )

    def _paint_row(self, painter, option, index) -> None:
        rect = option.rect
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)
        if selected:
            painter.fillRect(rect.adjusted(2, 1, -2, -1), QColor(214, 232, 253))
        elif hovered:
            painter.fillRect(rect.adjusted(2, 1, -2, -1), QColor(244, 247, 251))

        icon_side = self._owner.list_icon()
        thumb = QRect(
            rect.left() + 10,
            rect.top() + (rect.height() - icon_side) // 2,
            icon_side,
            icon_side,
        )
        pixmap = self._icon_pixmap(index, icon_side)
        if pixmap.isNull():
            painter.drawPixmap(thumb, _placeholder_pixmap(icon_side))
        else:
            scaled = pixmap.scaled(
                icon_side, icon_side, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            target = QRect(0, 0, scaled.width(), scaled.height())
            target.moveCenter(thumb.center())
            painter.drawPixmap(target, scaled)

        if not self._owner.show_text():
            return

        # 右侧标签
        tags = index.data(ROLE_TAGS) or []
        tag_text = "  ".join(f"#{t}" for t in tags) if tags else UNTAGGED
        painter.setPen(QColor(142, 150, 160))
        font = painter.font()
        font.setBold(False)
        painter.setFont(font)
        metrics = QFontMetrics(font)
        tag_width = min(metrics.horizontalAdvance(tag_text) + 8, max(60, rect.width() // 3))
        tag_rect = QRect(rect.right() - tag_width - 12, rect.top(), tag_width, rect.height())

        # 名称
        base_text = (index.data(ROLE_BASE) or "") + (index.data(ROLE_EXT) or "")
        name_rect = QRect(
            thumb.right() + 12,
            rect.top(),
            max(20, tag_rect.left() - thumb.right() - 20),
            rect.height(),
        )
        painter.setPen(QColor(38, 42, 48))
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(
            name_rect,
            Qt.AlignLeft | Qt.AlignVCenter,
            self._elide(painter, base_text, name_rect.width()),
        )

        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QColor(142, 150, 160))
        painter.drawText(
            tag_rect,
            Qt.AlignRight | Qt.AlignVCenter,
            self._elide(painter, tag_text, tag_rect.width()),
        )


# ==================================================================== 页面


class MaterialLibraryWidget(QWidget):
    """「我的素材库」页面。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries: List[FileEntry] = []
        self._entry_by_path: Dict[str, FileEntry] = {}
        self._item_by_path: Dict[str, QListWidgetItem] = {}
        self._header_items: List[QListWidgetItem] = []
        # 缓存都用 path 做主键，值的第一项是「版本号」(mtime_ns, size)：
        # 文件被原地改写（旋转 / 清空白 / 别的软件改过）时版本号就变了，缓存自动失效
        self._thumb_cache: Dict[str, Tuple[Tuple[int, int], QIcon]] = {}
        self._preview_cache: Dict[str, Tuple[Tuple[int, int], Optional[Image.Image]]] = {}
        self._pending_versions: Dict[str, Tuple[int, int]] = {}
        self._loader: Optional[_ThumbnailLoader] = None
        self._loaders: List[_ThumbnailLoader] = []  # 还在跑的线程，退出前要收干净
        self._thumb_token = 0
        self._last_layout_width = -1
        self._sizes_dirty = True
        self._split_initialized = False
        self._updating_selection = False

        self._view_mode = "card"
        self._sort_mode = "ctime_desc"
        self._group_mode = "none"
        self._icon_size = ICON_SIZE_DEFAULT  # 图片边长（三档之一，卡片和列表共用）
        self._show_text = True               # 是否在图片下方显示文件名与标签

        self._last_export_dir = paths.GALLERY_DIR

        self._build_ui()
        self.refresh()

    # ------------------------------------------------------------ 对外状态

    def view_mode(self) -> str:
        return self._view_mode

    # ---- 当前实际生效的尺寸（delegate 与尺寸计算都从这里取）----

    def icon_size(self) -> int:
        return self._icon_size

    def show_text(self) -> bool:
        return self._show_text

    def card_icon(self) -> int:
        return self._icon_size

    def card_w(self) -> int:
        return self._icon_size + CARD_PAD_X

    def card_h(self) -> int:
        extra = CARD_TEXT_H if self._show_text else CARD_NO_TEXT_H
        return self._icon_size + extra

    def list_icon(self) -> int:
        return self._icon_size

    def list_row_h(self) -> int:
        return self._icon_size + LIST_ROW_PAD

    # ============================================================ 界面

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        root.addLayout(self._build_toolbar())
        root.addLayout(self._build_tune_bar())
        root.addWidget(self._build_body(), 1)
        root.addWidget(self._build_export_bar())
        self.status_label = QLabel("就绪")
        self.status_label.setStyleSheet("color:#666;padding:2px;")
        root.addWidget(self.status_label)

        # 快捷键：F2 重命名 / Delete 删除（只在本页面有焦点时生效，不会打扰处理页）
        rename_sc = QShortcut(QKeySequence("F2"), self)
        rename_sc.setContext(Qt.WidgetWithChildrenShortcut)
        rename_sc.activated.connect(self._rename_and_group)
        delete_sc = QShortcut(QKeySequence(Qt.Key_Delete), self)
        delete_sc.setContext(Qt.WidgetWithChildrenShortcut)
        delete_sc.activated.connect(self._delete_selected)

    def _build_toolbar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)

        self.refresh_btn = QPushButton("刷新列表")
        self.refresh_btn.clicked.connect(self.refresh)
        self.add_files_btn = QPushButton("添加图片")
        self.add_files_btn.setToolTip("把外部图片复制进素材库（gallery 目录）")
        self.add_files_btn.clicked.connect(self._add_files_to_gallery)
        self.rename_btn = QPushButton("重命名 & 分组 (F2)")
        self.rename_btn.setToolTip("改文件名 / 给文件名加标签，会直接改磁盘上的文件")
        self.rename_btn.clicked.connect(self._rename_and_group)
        self.trim_btn = QPushButton("清理空白")
        self.trim_btn.setToolTip(
            "把选中素材四周的透明空白裁掉，只保留主体（直接覆盖保存，边距 0）"
        )
        self.trim_btn.clicked.connect(self._trim_selected)
        self.compress_btn = QPushButton("压缩")
        self.compress_btn.setToolTip(
            f"把选中素材压成「长边 ≤ {MAX_SIDE}px + 无损 WebP」，"
            "非 WebP 的会转成 .webp（同名自动加序号）"
        )
        self.compress_btn.clicked.connect(self._compress_selected)
        self.delete_btn = QPushButton("删除选中")
        self.delete_btn.clicked.connect(self._delete_selected)
        self.open_dir_btn = QPushButton("打开目录")
        self.open_dir_btn.clicked.connect(self._open_gallery_dir)

        bar.addWidget(self.refresh_btn)
        bar.addWidget(self.add_files_btn)
        bar.addSpacing(10)
        bar.addWidget(self.rename_btn)
        bar.addWidget(self.trim_btn)
        bar.addWidget(self.compress_btn)
        bar.addWidget(self.delete_btn)
        bar.addStretch(1)

        bar.addWidget(QLabel("展示："))
        self.card_btn = QPushButton("卡片")
        self.list_btn = QPushButton("列表")
        for btn in (self.card_btn, self.list_btn):
            btn.setCheckable(True)
            btn.setMinimumWidth(58)
        self.view_group = QButtonGroup(self)
        self.view_group.setExclusive(True)
        self.view_group.addButton(self.card_btn)
        self.view_group.addButton(self.list_btn)
        self.card_btn.setChecked(True)
        self.card_btn.clicked.connect(lambda: self._set_view_mode("card"))
        self.list_btn.clicked.connect(lambda: self._set_view_mode("list"))
        bar.addWidget(self.card_btn)
        bar.addWidget(self.list_btn)

        bar.addSpacing(10)
        bar.addWidget(QLabel("排序："))
        self.sort_box = QComboBox()
        for label, key in SORT_MODES:
            self.sort_box.addItem(label, key)
        self.sort_box.setCurrentIndex(0)
        self.sort_box.currentIndexChanged.connect(self._on_sort_changed)
        bar.addWidget(self.sort_box)

        bar.addSpacing(10)
        bar.addWidget(QLabel("分组："))
        self.group_box = QComboBox()
        for label, key in GROUP_MODES:
            self.group_box.addItem(label, key)
        self.group_box.setCurrentIndex(0)
        self.group_box.currentIndexChanged.connect(self._on_group_changed)
        bar.addWidget(self.group_box)

        bar.addSpacing(10)
        bar.addWidget(self.open_dir_btn)
        return bar

    def _build_tune_bar(self) -> QHBoxLayout:
        """图片尺寸三档单选 + 「显示文件名及标签」开关。"""
        bar = QHBoxLayout()
        bar.setContentsMargins(2, 0, 2, 0)
        bar.setSpacing(8)

        bar.addWidget(QLabel("图片尺寸："))
        self.size_group = QButtonGroup(self)
        self.size_group.setExclusive(True)
        self.size_buttons: Dict[int, QRadioButton] = {}
        for side in ICON_SIZE_CHOICES:
            radio = QRadioButton(f"{side} px")
            radio.setChecked(side == self._icon_size)
            radio.setToolTip("卡片封面 / 列表缩略图的边长")
            radio.toggled.connect(
                lambda checked, value=side: self._on_size_checked(value, checked)
            )
            self.size_group.addButton(radio)
            self.size_buttons[side] = radio
            bar.addWidget(radio)

        bar.addSpacing(24)
        self.show_text_check = QCheckBox("显示文件名及标签")
        self.show_text_check.setChecked(self._show_text)
        self.show_text_check.setToolTip(
            "取消勾选后只显示图片，卡片和列表行会跟着收紧"
        )
        self.show_text_check.toggled.connect(self._on_show_text_toggled)
        bar.addWidget(self.show_text_check)

        bar.addStretch(1)
        return bar

    def _on_size_checked(self, side: int, checked: bool) -> None:
        if not checked or side == self._icon_size:
            return
        self._icon_size = int(side)
        self._apply_view_mode()
        self.status_label.setText(f"图片尺寸：{self._icon_size} px")

    def _on_show_text_toggled(self, checked: bool) -> None:
        self._show_text = bool(checked)
        self._apply_view_mode()
        self.status_label.setText(
            "显示文件名及标签" if self._show_text else "只显示图片，不显示文件名和标签"
        )

    def _build_body(self) -> QWidget:
        self.view = _LibraryView()
        self.view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.view.setDragDropMode(QAbstractItemView.NoDragDrop)
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.view.setUniformItemSizes(False)
        self.view.setResizeMode(QListView.Adjust)
        self.view.setMovement(QListView.Static)
        self.view.setWrapping(True)
        self.view.setWordWrap(True)
        self.view.setSpacing(4)
        self._delegate = _LibraryDelegate(self, self.view)
        self.view.setItemDelegate(self._delegate)
        self.view.itemSelectionChanged.connect(self._on_selection_changed)
        self.view.itemDoubleClicked.connect(lambda *_: self._rename_and_group())
        self.view.resized.connect(self._update_item_sizes)

        # 右侧预览列（列表模式才显示，约占 1/8）
        self.preview_panel = QWidget()
        preview_layout = QVBoxLayout(self.preview_panel)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(6)
        self.preview_canvas = ImageCanvas("预览")
        self.preview_canvas.setMinimumSize(110, 110)
        self.preview_canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.preview_canvas.set_image(None, "选中素材查看预览")
        preview_layout.addWidget(self.preview_canvas, 1)

        self.info_label = QLabel("选中一张图片查看信息")
        self.info_label.setWordWrap(True)
        self.info_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.info_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.info_label.setStyleSheet("color:#4a525c;font-size:12px;")
        preview_layout.addWidget(self.info_label)

        self.body = QSplitter(Qt.Horizontal)
        self.body.addWidget(self.view)
        self.body.addWidget(self.preview_panel)
        self.body.setStretchFactor(0, 7)
        self.body.setStretchFactor(1, 1)
        self.body.setChildrenCollapsible(False)
        self.body.setSizes([900, 130])
        return self.body

    def _build_export_bar(self) -> QWidget:
        group = QGroupBox("导出所选图片")
        outer = QVBoxLayout(group)

        row1 = QHBoxLayout()
        self.png_check = QCheckBox("导出 PNG（透明底）")
        self.png_check.setChecked(True)
        row1.addWidget(self.png_check)
        self.ico_check = QCheckBox("导出 ICO")
        self.ico_check.toggled.connect(self._sync_ico_enabled)
        row1.addWidget(self.ico_check)
        row1.addSpacing(8)
        self.ico_size_label = QLabel("ICO 尺寸：")
        row1.addWidget(self.ico_size_label)
        self.ico_size_checks: Dict[int, QCheckBox] = {}
        for size in exporter.ICO_SIZE_CHOICES:
            check = QCheckBox(f"{size}")
            check.setChecked(size in exporter.DEFAULT_ICO_SIZES)
            self.ico_size_checks[size] = check
            row1.addWidget(check)
        row1.addStretch(1)
        outer.addLayout(row1)

        row2 = QHBoxLayout()
        self.ico_center_radio = QRadioButton("ICO 居中")
        self.ico_center_radio.setChecked(True)
        self.ico_fill_radio = QRadioButton("ICO 按 PNG 内容铺满")
        row2.addWidget(self.ico_center_radio)
        row2.addWidget(self.ico_fill_radio)
        row2.addSpacing(12)
        row2.addWidget(QLabel("输出到："))
        self.target_dir_edit = QLineEdit(self._last_export_dir)
        self.target_dir_edit.setReadOnly(True)
        self.target_dir_edit.setToolTip(paths.GALLERY_DIR)
        self.target_dir_edit.setMinimumWidth(220)
        row2.addWidget(self.target_dir_edit, 1)
        self.choose_target_btn = QPushButton("选择...")
        self.choose_target_btn.clicked.connect(self._choose_target_dir)
        row2.addWidget(self.choose_target_btn)
        self.export_btn = QPushButton("导出所选")
        self.export_btn.setStyleSheet(
            "QPushButton{background:#2f7ed8;color:white;font-weight:bold;"
            "padding:5px 16px;border:none;border-radius:4px;}"
            "QPushButton:hover{background:#3d8ce6;}"
        )
        self.export_btn.clicked.connect(self._export_selected)
        row2.addWidget(self.export_btn)
        outer.addLayout(row2)

        self._sync_ico_enabled(False)
        return group

    def _sync_ico_enabled(self, enabled: bool) -> None:
        self.ico_size_label.setEnabled(enabled)
        for check in self.ico_size_checks.values():
            check.setEnabled(enabled)
        self.ico_center_radio.setEnabled(enabled)
        self.ico_fill_radio.setEnabled(enabled)

    # ============================================================ 展示模式

    def _set_view_mode(self, mode: str) -> None:
        if mode == self._view_mode:
            return
        self._view_mode = mode
        self.card_btn.setChecked(mode == "card")
        self.list_btn.setChecked(mode == "list")
        self.preview_panel.setVisible(mode == "list")
        self._apply_view_mode()
        for item in self._file_items():
            self._refresh_item_text(item)
        QTimer.singleShot(0, self._apply_split_ratio)

    def _apply_split_ratio(self) -> None:
        """预览面板固定约占 1/8 宽。"""
        total = max(400, self.body.width())
        right = max(110, total // 8)
        self.body.setSizes([total - right, right])

    def _apply_view_mode(self) -> None:
        view = self.view
        if self._view_mode == "card":
            view.setViewMode(QListView.IconMode)
            view.setGridSize(QSize())
            view.setResizeMode(QListView.Adjust)
            view.setWrapping(True)
            view.setWordWrap(True)
        else:
            view.setViewMode(QListView.ListMode)
            view.setGridSize(QSize())
            view.setResizeMode(QListView.Fixed)
            view.setWrapping(False)
            view.setWordWrap(False)
        view.setIconSize(QSize(self._icon_size, self._icon_size))
        view.setSpacing(ITEM_SPACING)
        self._sizes_dirty = True
        self._update_item_sizes()

    def _update_item_sizes(self) -> None:
        """切换模式 / 拖缩放条时重设文件项尺寸；宽度变化时只补分组标题的整行宽。"""
        width = self.view.viewport().width()
        if width <= 0:
            return  # 页面还没显示，等 resized 信号再来一次
        if not self._sizes_dirty and width == self._last_layout_width:
            return
        self._last_layout_width = width

        header_width = max(120, width - 6)
        for item in self._header_items:
            item.setSizeHint(QSize(header_width, HEADER_HEIGHT))

        if not self._sizes_dirty:
            return
        self._sizes_dirty = False
        card = self._view_mode == "card"
        for item in self._file_items():
            item.setSizeHint(
                QSize(self.card_w(), self.card_h())
                if card
                else QSize(160, self.list_row_h())
            )
        self.view.scheduleDelayedItemsLayout()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        if not self._split_initialized:
            self._split_initialized = True
            QTimer.singleShot(0, self._apply_split_ratio)
            QTimer.singleShot(0, self._update_item_sizes)

    def _on_sort_changed(self, _index: int) -> None:
        self._sort_mode = self.sort_box.currentData()
        self._rebuild()

    def _on_group_changed(self, _index: int) -> None:
        self._group_mode = self.group_box.currentData()
        self._rebuild()

    # ============================================================ 数据

    def refresh(self) -> None:
        """重新扫描 gallery 目录并重建列表。"""
        self._rebuild()

    def _load_entries(self) -> List[FileEntry]:
        return [FileEntry(p) for p in list_images_in(paths.GALLERY_DIR)]

    def _sorted_entries(self) -> List[FileEntry]:
        entries = list(self._entries)
        mode = self._sort_mode
        if mode == "ctime_asc":
            entries.sort(key=lambda e: e.ctime)
        elif mode == "name_asc":
            entries.sort(key=lambda e: (e.base.lower(), e.ext.lower()))
        elif mode == "name_desc":
            entries.sort(key=lambda e: (e.base.lower(), e.ext.lower()), reverse=True)
        elif mode == "size_desc":
            entries.sort(key=lambda e: e.size, reverse=True)
        elif mode == "size_asc":
            entries.sort(key=lambda e: e.size)
        else:  # ctime_desc
            entries.sort(key=lambda e: e.ctime, reverse=True)
        return entries

    def _grouped_entries(self) -> List[Tuple[str, List[FileEntry]]]:
        buckets: Dict[str, List[FileEntry]] = {}
        for entry in self._sorted_entries():
            for tag in (entry.tags or [UNTAGGED]):
                buckets.setdefault(tag, []).append(entry)

        def sort_key(pair: Tuple[str, List[FileEntry]]):
            name, items = pair
            return (1 if name == UNTAGGED else 0, -len(items), name)

        return sorted(buckets.items(), key=sort_key)

    def _cached(self, store: Dict[str, Tuple[Tuple[int, int], object]], entry: FileEntry):
        """取缓存；版本号对不上（文件被改写）就当没缓存。"""
        hit = store.get(entry.path)
        if hit is None or hit[0] != entry.version:
            return None
        return hit[1]

    def _forget(self, *paths: str) -> None:
        """让指定路径的缩略图 / 预览缓存立刻失效。"""
        for path in paths:
            self._thumb_cache.pop(path, None)
            self._preview_cache.pop(path, None)
            self._pending_versions.pop(path, None)

    def _rebuild(self) -> None:
        selected = set(self._selected_paths())
        self._entries = self._load_entries()
        self._entry_by_path = {e.path: e for e in self._entries}

        # 已经不在列表里的缩略图 / 预览缓存及时丢掉
        alive = set(self._entry_by_path)
        for path in [p for p in self._thumb_cache if p not in alive]:
            self._thumb_cache.pop(path, None)
        for path in [p for p in self._preview_cache if p not in alive]:
            self._preview_cache.pop(path, None)

        self.view.blockSignals(True)
        self.view.clear()
        self._item_by_path.clear()
        self._header_items.clear()

        if self._group_mode == "tag":
            for title, items in self._grouped_entries():
                self._add_header(f"{title}（{len(items)}）")
                for entry in items:
                    self._add_file_item(entry)
        else:
            for entry in self._sorted_entries():
                self._add_file_item(entry)

        self.view.blockSignals(False)
        self._apply_view_mode()
        self._restore_selection(selected)
        self._update_summary()
        self.preview_panel.setVisible(self._view_mode == "list")
        self._start_thumbnails()

    def _add_header(self, title: str) -> None:
        item = QListWidgetItem(title)
        item.setFlags(Qt.NoItemFlags)
        item.setData(ROLE_KIND, "header")
        self.view.addItem(item)
        self._header_items.append(item)

    def _add_file_item(self, entry: FileEntry) -> None:
        item = QListWidgetItem()
        item.setData(ROLE_KIND, "file")
        item.setData(ROLE_PATH, entry.path)
        item.setData(ROLE_BASE, entry.base)
        item.setData(ROLE_TAGS, list(entry.tags))
        item.setData(ROLE_EXT, entry.ext)
        tip = os.path.basename(entry.path)
        tip += f"\n分组：{'、'.join(entry.tags)}" if entry.tags else f"\n分组：{UNTAGGED}"
        item.setToolTip(tip)
        cached = self._cached(self._thumb_cache, entry)
        if cached is not None:
            item.setIcon(cached)
        else:
            item.setIcon(QIcon(_placeholder_pixmap(THUMB_SIZE)))
        self.view.addItem(item)
        self._item_by_path[entry.path] = item
        self._refresh_item_text(item)

    def _refresh_item_text(self, item: QListWidgetItem) -> None:
        """卡片模式文字也由 delegate 画，这里只把内容写进数据角色。"""
        base = item.data(ROLE_BASE) or ""
        item.setText(base)

    def _file_items(self) -> List[QListWidgetItem]:
        return [
            self.view.item(i)
            for i in range(self.view.count())
            if self.view.item(i).data(ROLE_KIND) == "file"
        ]

    def _selected_paths(self) -> List[str]:
        return [
            item.data(ROLE_PATH)
            for item in self.view.selectedItems()
            if item.data(ROLE_KIND) == "file"
        ]

    def _selected_entries(self) -> List[FileEntry]:
        by_path = {e.path: e for e in self._entries}
        return [by_path[p] for p in self._selected_paths() if p in by_path]

    def _restore_selection(self, paths: set) -> None:
        if not paths:
            return
        self._updating_selection = True
        first = None
        for path in paths:
            item = self._item_by_path.get(path)
            if item is not None:
                item.setSelected(True)
                if first is None:
                    first = item
        self._updating_selection = False
        if first is not None:
            self.view.setCurrentItem(first)

    def _update_summary(self) -> None:
        total = len(self._entries)
        size_total = sum(e.size for e in self._entries)
        tag_count = len({t for e in self._entries for t in e.tags})
        self.status_label.setText(
            f"共 {total} 张素材，占用 {_human_size(size_total)}，"
            f"{tag_count} 个分组标签"
        )

    # ============================================================ 缩略图

    def _retire_loader(self) -> None:
        """把当前这轮线程标记作废：断信号 + cancel，但不阻塞界面。"""
        loader = self._loader
        self._loader = None
        if loader is None:
            return
        try:
            loader.loaded.disconnect()
        except Exception:  # noqa: BLE001
            pass
        loader.cancel()

    def stop_thumbnails(self, wait_ms: int = 3000) -> None:
        """收掉所有后台缩略图线程。

        QThread 还在跑的时候被销毁，Qt 会直接 abort 掉整个进程（Windows 上是
        0xC0000409），所以关窗之前必须 cancel + wait 干净。
        """
        loaders = list(self._loaders)
        self._loaders.clear()
        self._loader = None
        for loader in loaders:
            try:
                loader.loaded.disconnect()
            except Exception:  # noqa: BLE001
                pass
            loader.cancel()
        for loader in loaders:
            if loader.isRunning():
                loader.wait(wait_ms)

    def _on_loader_finished(self) -> None:
        self._loaders = [loader for loader in self._loaders if loader.isRunning()]

    def _start_thumbnails(self) -> None:
        self._retire_loader()

        pending = [e.path for e in self._entries if self._cached(self._thumb_cache, e) is None]
        pending = pending[:MAX_THUMBS]
        if not pending:
            return

        # 记下这批要生成的版本号：万一生成过程中文件被改写，结果也不会写进缓存
        self._pending_versions = {
            e.path: e.version for e in self._entries if e.path in set(pending)
        }
        self._thumb_token += 1
        token = self._thumb_token
        loader = _ThumbnailLoader(pending, THUMB_SIZE, self)
        loader.loaded.connect(lambda path, image, t=token: self._on_thumb(path, image, t))
        loader.finished.connect(self._on_loader_finished)
        self._loader = loader
        self._loaders.append(loader)
        loader.start()

    def _on_thumb(self, path: str, image: QImage, token: int) -> None:
        if token != self._thumb_token:
            return
        entry = self._entry_by_path.get(path)
        if entry is None:
            return
        version = self._pending_versions.get(path, entry.version)
        if version != entry.version:
            return  # 生成期间文件被改过，这次结果作废
        icon = QIcon(_rounded_pixmap(image))
        self._thumb_cache[path] = (version, icon)
        item = self._item_by_path.get(path)
        if item is not None:
            item.setIcon(icon)

    # ============================================================ 选中 / 预览

    def _on_selection_changed(self) -> None:
        if self._updating_selection:
            return
        paths = self._selected_paths()
        if len(paths) == 0:
            self.preview_canvas.set_image(None, "选中素材查看预览")
            self.info_label.setText("选中一张图片查看信息")
            return
        if len(paths) > 1:
            self.preview_canvas.set_image(None, f"已选中 {len(paths)} 张（预览只看一张）")
            self.info_label.setText(
                f"已选中 {len(paths)} 张素材。\n按 F2 可以统一追加分组标签。"
            )
            return
        self._show_preview(paths[0])

    def _show_preview(self, path: str) -> None:
        entry = self._entry_by_path.get(path)
        if entry is None:
            return
        if not os.path.isfile(path):
            self.preview_canvas.set_image(None, "文件已不存在，刷新一下列表")
            self.info_label.setText("文件已不存在")
            return

        hit = self._preview_cache.get(path)
        if hit is not None and hit[0] == entry.version:
            image = hit[1]
        else:
            try:
                image = load_image(path)
            except Exception:  # noqa: BLE001
                image = None
            if len(self._preview_cache) > 8:
                self._preview_cache.clear()
            self._preview_cache[path] = (entry.version, image)

        if image is None:
            self.preview_canvas.set_image(None, f"无法预览：{entry.base}{entry.ext}")
            dims = "-"
        else:
            self.preview_canvas.set_image(image, f"{image.width} × {image.height}")
            dims = f"{image.width} × {image.height}"

        tags_text = " / ".join(entry.tags) if entry.tags else f"（{UNTAGGED}）"
        self.info_label.setText(
            f"<b>{entry.base}</b>{entry.ext}<br>"
            f"分组：{tags_text}<br>"
            f"尺寸：{dims}<br>"
            f"大小：{_human_size(entry.size)}<br>"
            f"添加：{_fmt_time(entry.ctime)}<br>"
            f"修改：{_fmt_time(entry.mtime)}"
        )

    # ============================================================ 重命名 / 分组

    def _rename_and_group(self) -> None:
        entries = self._selected_entries()
        if not entries:
            self.status_label.setText("请先选中要重命名 / 分组的素材（可多选）")
            return

        payload = [(e.path, e.base, list(e.tags), e.ext, e.ctime) for e in entries]
        dialog = RenameGroupDialog(payload, taglib.load_presets(), self)
        if dialog.exec_() != RenameGroupDialog.Accepted:
            return

        if dialog.should_remember():
            taglib.add_presets(dialog.checked_tags())

        changes = dialog.changes()
        if not changes:
            self.status_label.setText("没有需要改动的项")
            return

        # changes() 里的文件名已经过 plan_renames 去重编号，不会再撞名
        done, rotated, errors = 0, 0, []
        for path, new_name, angle in changes:
            target = os.path.join(os.path.dirname(path), new_name)
            same_path = target.lower() == path.lower()
            try:
                if angle:
                    if not can_rotate_in_place(path):
                        errors.append(
                            f"{os.path.basename(path)}：{os.path.splitext(path)[1]} "
                            "不支持旋转后覆盖保存"
                        )
                        continue
                    image = rotate_image(load_image(path), angle)
                    save_image(image, target)
                    if not same_path:
                        os.remove(path)
                    rotated += 1
                elif not same_path:
                    os.rename(path, target)
                done += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{new_name}：{exc}")

        # 原地覆盖改了内容、改名改了路径，两种都要让缓存立刻失效
        self._forget(*[p for p, _n, _a in changes])
        self._forget(*[os.path.join(os.path.dirname(p), n) for p, n, _a in changes])
        self.refresh()
        if errors:
            QMessageBox.warning(
                self,
                "部分文件未能处理",
                f"成功 {done} 个，失败 {len(errors)} 个：\n\n" + "\n".join(errors[:8]),
            )
        parts = [f"已处理 {done} 个文件"]
        if rotated:
            parts.append(f"其中转正 {rotated} 个")
        if errors:
            parts.append(f"失败 {len(errors)} 个")
        self.status_label.setText("，".join(parts))
        if done:
            # 选中项在 refresh 里按新名字恢复不了，这里重新定位一下
            self._reselect_after(changes)

    def _reselect_after(self, changes: List[Tuple[str, str, int]]) -> None:
        new_paths = {
            os.path.join(os.path.dirname(old), new).lower() for old, new, _angle in changes
        }
        self._updating_selection = True
        for path, item in self._item_by_path.items():
            if path.lower() in new_paths:
                item.setSelected(True)
        self._updating_selection = False
        self._on_selection_changed()

    # ============================================================ 清理空白

    def _trim_selected(self) -> None:
        """「按主体裁剪」的独立入口：把选中素材四周的透明空白裁掉。

        和处理流程里那一步裁剪是同一件事，只是判据换成了成品图自己的 alpha。
        旋转时会自动跑一次，这个按钮是给「不旋转、只想收拾边距」用的。
        """
        entries = self._selected_entries()
        if not entries:
            self.status_label.setText("请先选中要清理空白的素材（可多选）")
            return

        targets = [e for e in entries if can_rotate_in_place(e.path)]
        skipped = len(entries) - len(targets)
        if not targets:
            QMessageBox.information(
                self,
                "提示",
                "选中的素材格式不支持原地保存，只有 png / jpg / jpeg / webp 能直接清理空白。",
            )
            return

        if len(targets) > 1:
            reply = QMessageBox.question(
                self,
                "清理空白",
                f"将把选中的 {len(targets)} 张素材四周的透明空白裁掉，"
                "并直接覆盖原文件。\n\n继续吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if reply != QMessageBox.Yes:
                return

        done, unchanged, errors = 0, 0, []
        for entry in targets:
            try:
                image = load_image(entry.path)
                trimmed = trim_to_content(image)
                if trimmed is image:  # 本来就没有空白，不用白重编一次
                    unchanged += 1
                    continue
                save_image(trimmed, entry.path)
                done += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{os.path.basename(entry.path)}：{exc}")

        self._forget(*[e.path for e in targets])
        self.refresh()
        if errors:
            QMessageBox.warning(
                self,
                "部分素材未能清理",
                f"成功 {done} 个，失败 {len(errors)} 个：\n\n" + "\n".join(errors[:8]),
            )
        parts = [f"已清理空白 {done} 张"]
        if unchanged:
            parts.append(f"{unchanged} 张本来就没有空白")
        if skipped:
            parts.append(f"{skipped} 张格式不支持被跳过")
        if errors:
            parts.append(f"失败 {len(errors)} 个")
        self.status_label.setText("，".join(parts))

    # ============================================================ 压缩

    @staticmethod
    def _temp_path_for(target: str) -> str:
        temp = f"{target}.compressing"
        index = 1
        while os.path.exists(temp):
            temp = f"{target}.compressing{index}"
            index += 1
        return temp

    def _compress_selected(self) -> None:
        """把选中素材压成「长边 ≤ MAX_SIDE + 无损 WebP」。

        非 WebP 的会转成 ``.webp``（同名自动加序号，标签仍留在文件名末尾），
        原来的文件删掉；本来就是 WebP 的就地重写。
        """
        entries = self._selected_entries()
        if not entries:
            self.status_label.setText("请先选中要压缩的素材（可多选）")
            return

        targets = [e for e in entries if is_supported(e.path)]
        skipped = len(entries) - len(targets)
        if not targets:
            QMessageBox.information(self, "提示", "选中的素材里没有可压缩的图片。")
            return

        if len(targets) > 1:
            reply = QMessageBox.question(
                self,
                "压缩素材",
                f"将把选中的 {len(targets)} 张素材压成\n"
                f"「长边 ≤ {MAX_SIDE}px + 无损 WebP」，\n"
                "非 WebP 的会转成 .webp 并删掉原文件。\n\n继续吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if reply != QMessageBox.Yes:
                return

        desired = []
        for entry in targets:
            base, tags_, _ext = taglib.split_name(os.path.basename(entry.path))
            desired.append((entry.path, taglib.compose_name(base, tags_, ".webp")))
        final = dict(taglib.plan_renames(desired))

        done, saved_bytes, errors = 0, 0, []
        for entry in targets:
            target = os.path.join(
                os.path.dirname(entry.path),
                final.get(entry.path, os.path.basename(entry.path)),
            )
            try:
                before = os.path.getsize(entry.path)
                small = resize_to_max(load_image(entry.path), MAX_SIDE)
                # 先写临时文件再整体替换，写到一半失败也不会把原图毁了
                temp = self._temp_path_for(target)
                try:
                    save_webp_lossless(small, temp)
                    os.replace(temp, target)
                except Exception:
                    if os.path.exists(temp):
                        os.remove(temp)
                    raise
                if target.lower() != entry.path.lower():
                    os.remove(entry.path)
                done += 1
                saved_bytes += max(0, before - os.path.getsize(target))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{os.path.basename(entry.path)}：{exc}")

        self._forget(*[e.path for e in targets])
        self._forget(*[os.path.join(os.path.dirname(p), n) for p, n in final.items()])
        self.refresh()
        if errors:
            QMessageBox.warning(
                self,
                "部分素材未能压缩",
                f"成功 {done} 个，失败 {len(errors)} 个：\n\n" + "\n".join(errors[:8]),
            )
        parts = [f"已压缩 {done} 张"]
        if saved_bytes:
            parts.append(f"省下 {_human_size(saved_bytes)}")
        if skipped:
            parts.append(f"{skipped} 张不是图片被跳过")
        if errors:
            parts.append(f"失败 {len(errors)} 个")
        self.status_label.setText("，".join(parts))

    # ============================================================ 删除 / 添加

    def _delete_selected(self) -> None:
        selected = self.view.selectedItems()
        paths_to_del = [i.data(ROLE_PATH) for i in selected if i.data(ROLE_KIND) == "file"]
        if not paths_to_del:
            self.status_label.setText("请先选中要删除的素材")
            return
        names = "\n".join(os.path.basename(p) for p in paths_to_del[:10])
        if len(paths_to_del) > 10:
            names += f"\n... 其余 {len(paths_to_del) - 10} 张"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("删除确认")
        box.setText(f"确定删除以下 {len(paths_to_del)} 个文件？\n\n{names}")
        box.setInformativeText("文件会直接从磁盘删除，不可恢复。")
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Cancel)
        if box.exec_() != QMessageBox.Yes:
            return

        deleted, failed = 0, []
        for path in paths_to_del:
            try:
                if os.path.isfile(path):
                    os.remove(path)
                    deleted += 1
            except Exception as exc:  # noqa: BLE001
                failed.append((os.path.basename(path), str(exc)))
        self.refresh()
        self.status_label.setText(
            f"已删除 {deleted} 张；失败 {len(failed)} 张"
            + (f"｜{failed[0][1]}" if failed else "")
        )

    def _add_files_to_gallery(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择要复制进素材库的图片", "", FILE_DIALOG_FILTER
        )
        if not files:
            return
        import shutil

        ok, fail = 0, []
        os.makedirs(paths.GALLERY_DIR, exist_ok=True)
        for src in files:
            try:
                base = os.path.basename(src)
                dst = os.path.join(paths.GALLERY_DIR, base)
                if os.path.exists(dst):
                    stem, ext = os.path.splitext(base)
                    index = 1
                    while os.path.exists(dst):
                        dst = os.path.join(paths.GALLERY_DIR, f"{stem}_{index}{ext}")
                        index += 1
                shutil.copy2(src, dst)
                ok += 1
            except Exception as exc:  # noqa: BLE001
                fail.append((os.path.basename(src), str(exc)))
        self.refresh()
        self.status_label.setText(
            f"已添加 {ok} 张到素材库" + (f"｜{len(fail)} 张失败" if fail else "")
        )

    def _open_gallery_dir(self) -> None:
        try:
            os.startfile(paths.GALLERY_DIR)  # noqa: S606
        except Exception:  # noqa: BLE001
            pass

    def _choose_target_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "选择导出目录", self._last_export_dir)
        if chosen:
            self._last_export_dir = chosen
            self.target_dir_edit.setText(chosen)
            self.target_dir_edit.setToolTip(chosen)

    # ============================================================ 导出

    def _export_selected(self) -> None:
        selected_paths = self._selected_paths()
        if not selected_paths:
            QMessageBox.information(self, "提示", "请先在列表里选中要导出的素材。")
            return
        want_png = self.png_check.isChecked()
        want_ico = self.ico_check.isChecked()
        if not want_png and not want_ico:
            QMessageBox.information(self, "提示", "请至少勾选一种导出格式（PNG 或 ICO）。")
            return

        sizes = [s for s, check in self.ico_size_checks.items() if check.isChecked()]
        target_dir = self.target_dir_edit.text().strip() or paths.GALLERY_DIR
        os.makedirs(target_dir, exist_ok=True)

        png_count = ico_count = fail = 0
        errors: List[str] = []
        fill_mode = self.ico_fill_radio.isChecked()

        taken: set = set()
        for src_path in selected_paths:
            if not is_supported(src_path) or not os.path.isfile(src_path):
                errors.append(f"{os.path.basename(src_path)}：跳过（不是支持的图片）")
                continue
            try:
                image = load_image(src_path)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{os.path.basename(src_path)}：读取失败 {exc}")
                fail += 1
                continue

            base, tags, ext = taglib.split_name(os.path.basename(src_path))
            stem = taglib.compose_name(base, tags, "") or os.path.splitext(
                os.path.basename(src_path)
            )[0]

            if want_png:
                out_path = exporter._unique_path(target_dir, stem, ".png", taken)
                try:
                    save_png(image.convert("RGBA"), out_path)
                    png_count += 1
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{os.path.basename(src_path)}：PNG 导出失败 {exc}")
                    fail += 1

            if want_ico:
                width, height = image.size
                if fill_mode:
                    rgba = image.convert("RGBA")
                    bbox = rgba.getbbox()
                    if bbox:
                        rgba = rgba.crop(bbox)
                        width, height = rgba.size
                    side = max(width, height)
                    square = exporter.extract_square(
                        rgba, (0, 0, width, height)
                    )
                else:
                    side = max(width, height)
                    left = (width - side) // 2
                    top = (height - side) // 2
                    square = exporter.extract_square(
                        image.convert("RGBA"), (left, top, left + side, top + side)
                    )
                out_path = exporter._unique_path(target_dir, stem, ".ico", taken)
                try:
                    save_ico(square, out_path, sizes or exporter.DEFAULT_ICO_SIZES)
                    ico_count += 1
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{os.path.basename(src_path)}：ICO 导出失败 {exc}")
                    fail += 1

        lines = [f"已导出 PNG：{png_count} 个", f"已导出 ICO：{ico_count} 个"]
        if errors:
            lines.append(f"失败 / 跳过：{len(errors)} 个")
            lines.extend(f"· {e}" for e in errors[:8])
            if len(errors) > 8:
                lines.append(f"· ... 其余 {len(errors) - 8} 条")
        lines.append("")
        lines.append(f"输出目录：{target_dir}")

        box = QMessageBox(self)
        box.setWindowTitle("导出完成")
        box.setIcon(QMessageBox.Warning if errors else QMessageBox.Information)
        box.setText("\n".join(lines))
        open_btn = box.addButton("打开输出目录", QMessageBox.ActionRole)
        box.addButton("关闭", QMessageBox.AcceptRole)
        box.exec_()
        if box.clickedButton() is open_btn:
            try:
                os.startfile(target_dir)  # noqa: S606
            except Exception:  # noqa: BLE001
                pass
        self.status_label.setText(
            f"导出完成：PNG {png_count} 个，ICO {ico_count} 个，失败 {fail} 个"
        )
