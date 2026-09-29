# -*- coding: utf-8 -*-
"""自绘图片控件：棋盘格透明底 + 缩放/平移 + 叠加矩形。

通用功能（两张画布都具备）：
  - 等比缩放居中显示，按 set_image 时刻的图片大小自动 ``fit_to_window``
  - 鼠标滚轮 = 以光标为中心的缩放
  - 中键 / Alt+左键 / 空格+左键 = 平移
  - 双击 = 100% 原图查看
  - 工具栏方法 ``fit_to_window / actual_size / zoom_in / zoom_out / reset_view``

可选叠加：
  - ``set_square`` 单个虚线正方形（ICO 用）
  - ``set_hints`` 多组 hint 矩形（处理前手动引导）

画框模式（处理前手动引导，**取代旧版的 HintSelectorDialog 弹窗**）：
  - ``set_hint_mode("keep" | "drop" | None)`` 切换绘制笔
  - 左键拖拽：在图片内画矩形 → 发 ``rect_drawn`` 信号
  - 矩形数据由 main_window 持有，撤销也在 main_window 的栈里
"""

from typing import List, Optional, Tuple

from PIL import Image
from PyQt5.QtCore import QPoint, QRect, QSize, Qt, pyqtSignal
from PyQt5.QtGui import QBrush, QColor, QFont, QImage, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import (
    QAbstractSpinBox,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QWidget,
)

PADDING = 8
CHECKER_TILE = 12
TITLE_HEIGHT = 22
SUBTITLE_HEIGHT = 22
ZOOM_MIN = 0.05
ZOOM_MAX = 20.0
ZOOM_STEP = 1.25  # 滚轮一格缩放倍数

# 处理前 hint 用色：绿 = 框选要的，红 = 框选不要的
KEEP_COLOR = QColor(40, 168, 64)
DROP_COLOR = QColor(220, 60, 60)
SQUARE_COLOR = QColor(255, 176, 32)

_HINT_CURSOR = {
    "keep": Qt.CrossCursor,
    "drop": Qt.CrossCursor,
    None: Qt.ArrowCursor,
}


def pil_to_qimage(image: Image.Image) -> QImage:
    if image.mode != "RGBA":
        image = image.convert("RGBA")
    data = image.tobytes("raw", "RGBA")
    qimage = QImage(data, image.width, image.height, QImage.Format_RGBA8888)
    return qimage.copy()  # 复制一份，避免 data 被回收后悬空


class ImageCanvas(QWidget):
    """缩放/平移图片控件，可叠加 ICO 方框与 hint 矩形，支持在画布上直接画 hint。"""

    square_selected = pyqtSignal(tuple)  # (x0, y0, x1, y1) 图片坐标
    rect_drawn = pyqtSignal(str, tuple)  # (mode, box) mode="keep"/"drop"
    zoom_changed = pyqtSignal(float)    # 缩放比例变了，方便外层同步显示
    mouse_entered = pyqtSignal()        # 鼠标进入画布区域
    mouse_left = pyqtSignal()           # 鼠标离开画布区域

    def __init__(
        self,
        title: str = "",
        selectable: bool = False,        # 画正方形（ICO 用，外部 SquareSelectorDialog 触发）
        rect_selectable: bool = False,   # 已弃用；画 hint 由 hint_mode 控制
        parent=None,
    ):
        super().__init__(parent)
        self._title = title
        self._subtitle = ""
        self._pixmap: Optional[QPixmap] = None
        self._image_size = (0, 0)
        self._base_scale = 1.0  # 由 fit_to_window 计算的图片→视口的初始比例
        self._zoom = 1.0       # 用户当前缩放倍率
        self._origin = QPoint(0, 0)  # 图片左上角在 widget 里的绘制位置
        self._image_rect = QRect()   # 当前图片绘制区域（含 padding 调整后）

        # ICO 方框画模式（外部 SquareSelectorDialog 不复用，仅保留兼容）
        self._square_mode = selectable
        # hint 画模式："keep" / "drop" / None
        self._hint_mode: Optional[str] = None

        self._square: Optional[Tuple[int, int, int, int]] = None
        self._keep_boxes: List[Tuple[int, int, int, int]] = []
        self._drop_boxes: List[Tuple[int, int, int, int]] = []

        # 拖拽 / 平移
        self._drag_start: Optional[QPoint] = None
        self._draw_rect: Optional[QRect] = None
        self._panning = False
        self._space_down = False

        self.setMinimumSize(200, 200)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_Hover, True)
        self._update_cursor()

        self._checker = self._build_checker()

    # ============================================================== 对外

    def title(self) -> str:
        return self._title

    def set_title(self, text: str) -> None:
        self._title = text
        self.update()

    def set_image(self, image: Optional[Image.Image], subtitle: str = "") -> None:
        """设置图片；新图进来自动 fit_to_window。"""
        if image is None:
            self._pixmap = None
            self._image_size = (0, 0)
            self._zoom = 1.0
        else:
            self._pixmap = QPixmap.fromImage(pil_to_qimage(image))
            self._image_size = (image.width, image.height)
        self._subtitle = subtitle
        self._zoom = 1.0
        self._origin = QPoint(0, 0)
        self._recompute_layout()
        self.zoom_changed.emit(self._zoom)
        self.update()

    # ---- 缩放控制（外部按钮触发）----

    def zoom(self) -> float:
        """当前 zoom 倍率（1.0 = 初始 fit_to_window；>1 = 放大，<1 = 缩小）。"""
        return self._zoom

    def fit_to_window(self) -> None:
        self._zoom = 1.0
        self._origin = QPoint(0, 0)
        self._recompute_layout()
        self.zoom_changed.emit(self._zoom)
        self.update()

    def actual_size(self) -> None:
        """100% 显示原图（一个像素 = 一个屏幕点）。"""
        if self._image_size[0] <= 0:
            return
        self._zoom = self._base_scale
        self._center_image()
        self.zoom_changed.emit(self._zoom)
        self.update()

    def zoom_in(self) -> None:
        self._set_zoom(self._zoom * ZOOM_STEP, anchor=None)

    def zoom_out(self) -> None:
        self._set_zoom(self._zoom / ZOOM_STEP, anchor=None)

    def reset_view(self) -> None:
        """一键回到 fit_to_window 并把视图居中（看图乱飞时常用）。"""
        self.fit_to_window()

    # ---- 方框 / hint 叠加 ----

    def set_square(self, square: Optional[Tuple[int, int, int, int]]) -> None:
        self._square = square
        self.update()

    def square(self) -> Optional[Tuple[int, int, int, int]]:
        return self._square

    def set_hints(
        self,
        keep_boxes: Optional[List[Tuple[int, int, int, int]]] = None,
        drop_boxes: Optional[List[Tuple[int, int, int, int]]] = None,
    ) -> None:
        """整体替换要叠加显示的 keep / drop 矩形列表。"""
        self._keep_boxes = list(keep_boxes or [])
        self._drop_boxes = list(drop_boxes or [])
        self.update()

    def keep_boxes(self) -> List[Tuple[int, int, int, int]]:
        return list(self._keep_boxes)

    def drop_boxes(self) -> List[Tuple[int, int, int, int]]:
        return list(self._drop_boxes)

    def add_hint(self, mode: str, box: Tuple[int, int, int, int]) -> None:
        if mode == "keep":
            self._keep_boxes.append(box)
        elif mode == "drop":
            self._drop_boxes.append(box)
        else:
            raise ValueError(f"mode 必须是 keep 或 drop，收到 {mode!r}")
        self.update()

    def remove_last_hint(self, mode: str) -> bool:
        target = self._keep_boxes if mode == "keep" else self._drop_boxes
        if not target:
            return False
        target.pop()
        self.update()
        return True

    def clear_hints(self, mode: Optional[str] = None) -> None:
        if mode in (None, "keep"):
            self._keep_boxes.clear()
        if mode in (None, "drop"):
            self._drop_boxes.clear()
        self.update()

    # ---- 画 hint 模式 ----

    def hint_mode(self) -> Optional[str]:
        return self._hint_mode

    def set_hint_mode(self, mode: Optional[str]) -> None:
        """``"keep"`` / ``"drop"`` / ``None``。

        非 None 时左键拖拽会在画布上画矩形，画完发 ``rect_drawn(mode, box)``。
        矩形**不**自动加入内部 hints 列表，由 main_window 收到信号后追加，
        这样撤销栈和数据源保持一致。
        """
        if mode not in ("keep", "drop", None):
            raise ValueError(f"mode 必须是 keep / drop / None，收到 {mode!r}")
        self._hint_mode = mode
        self._update_cursor()

    def _update_cursor(self) -> None:
        if self._hint_mode or self._square_mode:
            self.setCursor(Qt.CrossCursor)
        elif self._panning or self._space_down:
            self.setCursor(Qt.OpenHandCursor)
        else:
            self.setCursor(Qt.ArrowCursor)

    # ============================================================= 绘制

    @staticmethod
    def _build_checker() -> QPixmap:
        pixmap = QPixmap(CHECKER_TILE * 2, CHECKER_TILE * 2)
        pixmap.fill(QColor(255, 255, 255))
        painter = QPainter(pixmap)
        painter.fillRect(0, 0, CHECKER_TILE, CHECKER_TILE, QColor(226, 229, 233))
        painter.fillRect(
            CHECKER_TILE, CHECKER_TILE, CHECKER_TILE, CHECKER_TILE, QColor(226, 229, 233)
        )
        painter.end()
        return pixmap

    def _recompute_layout(self) -> None:
        """根据 widget 尺寸、image 尺寸、zoom、pan 重算 origin 与 base_scale。"""
        if self._pixmap is None or self._pixmap.isNull():
            self._image_rect = QRect()
            return
        avail_w = max(1, self.width() - PADDING * 2)
        avail_h = max(1, self.height() - PADDING * 2 - TITLE_HEIGHT - SUBTITLE_HEIGHT)
        img_w, img_h = self._image_size
        self._base_scale = min(avail_w / img_w, avail_h / img_h)
        self._apply_origin()

    def _apply_origin(self) -> None:
        scale = self._base_scale * self._zoom
        img_w, img_h = self._image_size
        draw_w = int(img_w * scale)
        draw_h = int(img_h * scale)
        # 视口中心（去掉标题 / 副标题占位）
        cx = self.width() // 2 + self._origin.x() - 0  # _origin.x() 此时存的是 pan
        cy = TITLE_HEIGHT + (self.height() - TITLE_HEIGHT - SUBTITLE_HEIGHT) // 2 + self._origin.y()
        # origin 是图片左上角的绘制坐标
        self._image_rect = QRect(int(cx - draw_w / 2), int(cy - draw_h / 2), draw_w, draw_h)

    def _image_rect_for(self, box: Tuple[int, int, int, int]) -> QRect:
        x0, y0, x1, y1 = box
        scale = self._base_scale * self._zoom
        ox, oy = self._image_rect.left(), self._image_rect.top()
        return QRect(
            ox + int(x0 * scale),
            oy + int(y0 * scale),
            int((x1 - x0) * scale),
            int((y1 - y0) * scale),
        )

    def _to_image_point(self, pos: QPoint) -> Tuple[int, int]:
        scale = self._base_scale * self._zoom
        if scale <= 0:
            return (0, 0)
        x = int((pos.x() - self._image_rect.left()) / scale)
        y = int((pos.y() - self._image_rect.top()) / scale)
        x = max(0, min(x, self._image_size[0]))
        y = max(0, min(y, self._image_size[1]))
        return (x, y)

    def _in_image(self, pos: QPoint) -> bool:
        return self._image_rect.contains(pos)

    def _center_image(self) -> None:
        """zoom 变化时把图片摆回中心（origin 由 _apply_origin 用 widget 中心重新算）。"""
        self._origin = QPoint(0, 0)
        self._apply_origin()

    def _set_zoom(self, new_zoom: float, anchor: Optional[QPoint]) -> None:
        new_zoom = max(ZOOM_MIN, min(ZOOM_MAX, new_zoom))
        if abs(new_zoom - self._zoom) < 1e-6:
            return
        old_zoom = self._zoom
        self._zoom = new_zoom
        if anchor is not None and self._image_size[0] > 0:
            # 让 anchor 在 widget 上的位置保持不变
            old_scale = self._base_scale * old_zoom
            new_scale = self._base_scale * new_zoom
            if old_scale > 0 and new_scale > 0:
                ox_old, oy_old = self._image_rect.left(), self._image_rect.top()
                ix = (anchor.x() - ox_old) / old_scale
                iy = (anchor.y() - oy_old) / old_scale
                # 用 ix/iy 反推新 origin
                self._image_rect.setLeft(int(anchor.x() - ix * new_scale))
                self._image_rect.setTop(int(anchor.y() - iy * new_scale))
                # 把 _origin 存成相对 widget 中心的偏移（_apply_origin 用它居中）
                widget_cx = self.width() // 2
                widget_cy = TITLE_HEIGHT + (self.height() - TITLE_HEIGHT - SUBTITLE_HEIGHT) // 2
                desired_draw_w = int(self._image_size[0] * new_scale)
                desired_draw_h = int(self._image_size[1] * new_scale)
                self._origin = QPoint(
                    self._image_rect.left() + desired_draw_w // 2 - widget_cx,
                    self._image_rect.top() + desired_draw_h // 2 - widget_cy,
                )
        self._apply_origin()
        self.zoom_changed.emit(self._zoom)
        self.update()

    # ============================================================= 事件

    def resizeEvent(self, event):  # noqa: N802
        self._recompute_layout()
        super().resizeEvent(event)

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(38, 41, 46))

        title_font = QFont()
        title_font.setPointSize(9)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QColor(232, 234, 237))
        painter.drawText(QRect(PADDING, 2, self.width() - PADDING * 2, TITLE_HEIGHT),
                         Qt.AlignLeft | Qt.AlignVCenter, self._title)

        if self._pixmap is None or self._pixmap.isNull():
            painter.setPen(QColor(140, 146, 154))
            painter.drawText(self.rect(), Qt.AlignCenter, self._subtitle or "暂无图片")
            return

        if self._image_rect.isValid():
            painter.setBrush(QBrush(self._checker))
            painter.drawRect(self._image_rect)
            painter.drawPixmap(self._image_rect, self._pixmap)

        # keep / drop hint
        for boxes, color, label in (
            (self._keep_boxes, KEEP_COLOR, "要"),
            (self._drop_boxes, DROP_COLOR, "✗"),
        ):
            for idx, box in enumerate(boxes):
                r = self._image_rect_for(box)
                painter.setBrush(QColor(color.red(), color.green(), color.blue(), 50))
                painter.setPen(QPen(color, 2, Qt.DashLine))
                painter.drawRect(r)
                painter.setPen(color)
                painter.setFont(QFont("", 8, QFont.Bold))
                painter.drawText(QRect(r.left() + 4, r.top() + 2, 200, 16),
                                 Qt.AlignLeft | Qt.AlignVCenter,
                                 f"{label} #{idx + 1}")

        # ICO 正方形
        if self._square:
            sq = self._image_rect_for(self._square)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(SQUARE_COLOR, 2, Qt.DashLine))
            painter.drawRect(sq)

        # 当前拖拽矩形
        if self._draw_rect:
            color = KEEP_COLOR if self._hint_mode == "keep" else (
                DROP_COLOR if self._hint_mode == "drop" else SQUARE_COLOR
            )
            painter.setBrush(QColor(color.red(), color.green(), color.blue(), 50))
            painter.setPen(QPen(color, 2))
            painter.drawRect(self._draw_rect)

        if self._subtitle:
            painter.setFont(title_font)
            painter.setPen(QColor(160, 166, 174))
            painter.drawText(
                QRect(PADDING, self.height() - SUBTITLE_HEIGHT,
                      self.width() - PADDING * 2, SUBTITLE_HEIGHT),
                Qt.AlignLeft | Qt.AlignVCenter,
                self._subtitle,
            )

        # zoom 比例在右上角小字提示
        if self._pixmap is not None:
            scale_pct = int(self._base_scale * self._zoom * 100)
            painter.setFont(QFont("", 8))
            painter.setPen(QColor(170, 176, 184))
            painter.drawText(
                QRect(self.width() - 60, 2, 56, TITLE_HEIGHT),
                Qt.AlignRight | Qt.AlignVCenter,
                f"{scale_pct}%",
            )

    # ---------- 鼠标 / 滚轮 ----------

    def wheelEvent(self, event):  # noqa: N802
        if self._pixmap is None:
            return
        delta = event.angleDelta().y()
        if delta == 0:
            return
        factor = ZOOM_STEP if delta > 0 else 1 / ZOOM_STEP
        self._set_zoom(self._zoom * factor, anchor=event.pos())

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        if event.button() == Qt.LeftButton and self._pixmap is not None:
            self.actual_size()
            return
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.MiddleButton:
            self._panning = True
            self._drag_start = event.pos()
            self._update_cursor()
            return
        if event.button() == Qt.LeftButton:
            if (self._space_down or event.modifiers() & Qt.AltModifier):
                # 空格 / Alt + 左键 = 平移
                self._panning = True
                self._drag_start = event.pos()
                self._update_cursor()
                return
            if self._hint_mode and self._in_image(event.pos()):
                self._drag_start = event.pos()
                self._draw_rect = QRect(event.pos(), event.pos())
                self.update()
                return
            if self._square_mode and self._pixmap is not None:
                self._drag_start = event.pos()
                self._draw_rect = QRect(event.pos(), event.pos())
                self.update()
                return

    def mouseMoveEvent(self, event):  # noqa: N802
        # 平移
        if self._panning and self._drag_start is not None:
            dx = event.pos().x() - self._drag_start.x()
            dy = event.pos().y() - self._drag_start.y()
            self._image_rect.translate(dx, dy)
            widget_cx = self.width() // 2
            widget_cy = TITLE_HEIGHT + (self.height() - TITLE_HEIGHT - SUBTITLE_HEIGHT) // 2
            scale = self._base_scale * self._zoom
            desired_draw_w = int(self._image_size[0] * scale)
            desired_draw_h = int(self._image_size[1] * scale)
            self._origin = QPoint(
                self._image_rect.left() + desired_draw_w // 2 - widget_cx,
                self._image_rect.top() + desired_draw_h // 2 - widget_cy,
            )
            self._drag_start = event.pos()
            self.update()
            return

        if self._drag_start is None or self._draw_rect is None:
            return

        x0, y0 = self._drag_start.x(), self._drag_start.y()
        x1, y1 = event.pos().x(), event.pos().y()
        if self._square_mode:
            side = max(abs(x1 - x0), abs(y1 - y0))
            left = x0 if x1 >= x0 else x0 - side
            top = y0 if y1 >= y0 else y0 - side
            self._draw_rect = QRect(left, top, side, side)
        else:
            left = min(x0, x1)
            top = min(y0, y1)
            self._draw_rect = QRect(left, top, abs(x1 - x0), abs(y1 - y0))
        self.update()

    def mouseReleaseEvent(self, event):  # noqa: N802
        if self._panning:
            self._panning = False
            self._drag_start = None
            self._update_cursor()
            return

        if self._drag_start is None or self._draw_rect is None:
            return
        rect = self._draw_rect
        self._drag_start = None
        self._draw_rect = None

        if rect.width() < 4 or rect.height() < 4:
            self.update()
            return

        x0, y0 = self._to_image_point(rect.topLeft())
        x1, y1 = self._to_image_point(rect.bottomRight())
        ix0, ix1 = sorted((x0, x1))
        iy0, iy1 = sorted((y0, y1))
        if ix1 - ix0 < 4 or iy1 - iy0 < 4:
            self.update()
            return

        box = (ix0, iy0, ix1, iy1)
        if self._hint_mode:
            self.rect_drawn.emit(self._hint_mode, box)
        elif self._square_mode:
            side = min(ix1 - ix0, iy1 - iy0)
            square = (ix0, iy0, ix0 + side, iy0 + side)
            self.set_square(square)
            self.square_selected.emit(square)

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() == Qt.Key_Space and not event.isAutoRepeat():
            self._space_down = True
            self._update_cursor()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):  # noqa: N802
        if event.key() == Qt.Key_Space and not event.isAutoRepeat():
            self._space_down = False
            self._update_cursor()
            return
        super().keyReleaseEvent(event)

    def enterEvent(self, event):  # noqa: N802
        super().enterEvent(event)
        self.mouse_entered.emit()

    def leaveEvent(self, event):  # noqa: N802
        super().leaveEvent(event)
        self.mouse_left.emit()


_ROTATE_QSS = """
QWidget#rotateBar {
    background: #f5f8fc;
    border: 1px solid #e1e7ef;
    border-radius: 8px;
}
QWidget#rotateBar QLabel { color: #4a525c; background: transparent; }
QWidget#rotateBar QLabel#rotateTitle { color: #2b3138; font-weight: bold; }
QWidget#rotateBar QPushButton {
    background: #ffffff;
    border: 1px solid #d3dbe4;
    border-radius: 6px;
    padding: 3px 10px;
    color: #333a42;
    font-weight: bold;
}
QWidget#rotateBar QPushButton:hover { border-color: #2f7ed8; color: #2f7ed8; }
QWidget#rotateBar QPushButton:pressed { background: #e6f0fb; }
QWidget#rotateBar QPushButton:disabled {
    color: #b9c0c8; border-color: #e8ecf1; background: #fafbfd;
}
QWidget#rotateBar QSpinBox {
    background: #ffffff;
    border: 1px solid #d3dbe4;
    border-radius: 6px;
    padding: 2px 4px;
    min-width: 58px;
    color: #2f7ed8;
    font-weight: bold;
}
QWidget#rotateBar QSlider::groove:horizontal {
    height: 4px; background: #dde4ec; border-radius: 2px;
}
QWidget#rotateBar QSlider::sub-page:horizontal {
    background: #2f7ed8; border-radius: 2px;
}
QWidget#rotateBar QSlider::handle:horizontal {
    background: #ffffff; border: 2px solid #2f7ed8;
    width: 11px; height: 11px; margin: -6px 0; border-radius: 8px;
}
QWidget#rotateBar QSlider::handle:horizontal:hover { background: #eaf2fd; }
QWidget#rotateBar QSlider:disabled::handle:horizontal { border-color: #c8cfd8; }
QWidget#rotateBar QSlider:disabled::sub-page:horizontal { background: #e4e9ef; }
"""


def normalize_angle(angle: int) -> int:
    """把任意角度归一化到 (-180, 180]。"""
    value = ((int(angle) + 180) % 360) - 180
    return 180 if value == -180 else value


class RotateBar(QWidget):
    """旋转控件：``↺/↻ 90°`` 大调，``−/+ 1°`` 微调（可长按），滑杆拖动找正。

    角度约定：**正数 = 顺时针**，0 表示原图方向。
    """

    angle_changed = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("rotateBar")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(_ROTATE_QSS)

        self._angle = 0
        self._updating = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 7, 10, 7)
        layout.setSpacing(6)

        title = QLabel("旋转")
        title.setObjectName("rotateTitle")
        layout.addWidget(title)

        self.btn_ccw = self._button("↺ 90°", "逆时针转 90°")
        self.btn_cw = self._button("↻ 90°", "顺时针转 90°")
        layout.addWidget(self.btn_ccw)
        layout.addWidget(self.btn_cw)
        layout.addSpacing(6)

        self.btn_dec = self._button("− 1°", "逆时针 1°（长按可连续微调）", repeat=True)
        layout.addWidget(self.btn_dec)

        self.angle_box = QSpinBox()
        self.angle_box.setRange(-180, 180)
        self.angle_box.setSingleStep(1)
        self.angle_box.setSuffix("°")
        self.angle_box.setAlignment(Qt.AlignCenter)
        self.angle_box.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.angle_box.setToolTip("当前旋转角度（正数 = 顺时针），也可以直接输入")
        layout.addWidget(self.angle_box)

        self.btn_inc = self._button("+ 1°", "顺时针 1°（长按可连续微调）", repeat=True)
        layout.addWidget(self.btn_inc)
        layout.addSpacing(6)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(-180, 180)
        self.slider.setSingleStep(1)
        self.slider.setPageStep(5)
        self.slider.setTickInterval(45)
        self.slider.setTickPosition(QSlider.TicksBelow)
        self.slider.setMinimumWidth(150)
        self.slider.setToolTip("左右拖动微调角度")
        layout.addWidget(self.slider, 1)

        self.btn_reset = self._button("归零", "恢复成原始方向")
        layout.addWidget(self.btn_reset)

        self.btn_ccw.clicked.connect(lambda: self.step(-90))
        self.btn_cw.clicked.connect(lambda: self.step(90))
        self.btn_dec.clicked.connect(lambda: self.step(-1))
        self.btn_inc.clicked.connect(lambda: self.step(1))
        self.btn_reset.clicked.connect(lambda: self.set_angle(0))
        self.angle_box.valueChanged.connect(self._on_widget_changed)
        self.slider.valueChanged.connect(self._on_widget_changed)

        self.set_enabled(False)

    @staticmethod
    def _button(text: str, tip: str, repeat: bool = False) -> QPushButton:
        btn = QPushButton(text)
        btn.setToolTip(tip)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFocusPolicy(Qt.NoFocus)
        if repeat:
            btn.setAutoRepeat(True)
            btn.setAutoRepeatDelay(420)
            btn.setAutoRepeatInterval(70)
        return btn

    # ---- 对外 ----

    def angle(self) -> int:
        return self._angle

    def set_angle(self, angle: int, notify: bool = True) -> None:
        angle = normalize_angle(angle)
        if angle == self._angle and notify:
            return
        self._angle = angle
        self._updating = True
        self.angle_box.setValue(angle)
        self.slider.setValue(angle)
        self._updating = False
        if notify:
            self.angle_changed.emit(angle)

    def step(self, delta: int) -> None:
        self.set_angle(self._angle + delta)

    def set_enabled(self, enabled: bool) -> None:
        for widget in (
            self.btn_ccw,
            self.btn_cw,
            self.btn_dec,
            self.btn_inc,
            self.angle_box,
            self.slider,
            self.btn_reset,
        ):
            widget.setEnabled(enabled)

    def _on_widget_changed(self, value: int) -> None:
        if self._updating:
            return
        self.set_angle(value)


class FlowLayout(QLayout):
    """自动换行的水平布局，用来摆「标签按钮」这种宽窄不一的小控件。"""

    def __init__(self, parent=None, margin: int = 0, h_spacing: int = 6, v_spacing: int = 6):
        super().__init__(parent)
        self._items = []
        self._h_spacing = h_spacing
        self._v_spacing = v_spacing
        self.setContentsMargins(margin, margin, margin, margin)

    # ---- QLayout 必需接口 ----

    def addItem(self, item):  # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index):  # noqa: N802
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):  # noqa: N802
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientations(Qt.Orientation(0))

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        return size

    # ---- 实际排布 ----

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        margins = self.contentsMargins()
        effective = rect.adjusted(
            margins.left(), margins.top(), -margins.right(), -margins.bottom()
        )
        x, y = effective.x(), effective.y()
        line_height = 0
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + self._h_spacing
            if next_x - self._h_spacing > effective.right() and line_height > 0:
                x = effective.x()
                y = y + line_height + self._v_spacing
                next_x = x + hint.width() + self._h_spacing
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + margins.bottom()