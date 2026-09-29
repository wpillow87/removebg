# -*- coding: utf-8 -*-
"""ICO 正方形区域框选对话框。"""

from typing import Optional, Tuple

from PIL import Image
from PyQt5.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from .widgets import ImageCanvas


class SquareSelectorDialog(QDialog):
    """在抠图后的图片上手动画出正方形，作为 ICO 的基准区域。"""

    def __init__(
        self,
        image: Image.Image,
        current: Optional[Tuple[int, int, int, int]] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("框选 ICO 图标区域")
        self.resize(780, 640)

        self.canvas = ImageCanvas("按住鼠标左键拖动画出正方形", selectable=True)
        self.canvas.set_image(image, f"图片尺寸：{image.width} × {image.height}")
        if current:
            self.canvas.set_square(current)
        self.canvas.square_selected.connect(lambda _s: self._refresh())

        hint = QLabel(
            "拖拽画出正方形区域，松开鼠标即生效。导出 ICO 时会按所选尺寸等比缩放这个区域，"
            "区域外的部分不会被导出。"
        )
        hint.setWordWrap(True)

        self.size_label = QLabel()
        reset_btn = QPushButton("重置")
        reset_btn.clicked.connect(self._reset)
        ok_btn = QPushButton("确定")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)

        bottom = QHBoxLayout()
        bottom.addWidget(self.size_label)
        bottom.addStretch(1)
        bottom.addWidget(reset_btn)
        bottom.addWidget(cancel_btn)
        bottom.addWidget(ok_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addWidget(self.canvas, 1)
        layout.addLayout(bottom)

        self._refresh()

    def _reset(self) -> None:
        self.canvas.set_square(None)
        self._refresh()

    def _refresh(self) -> None:
        square = self.canvas.square()
        if not square:
            self.size_label.setText("当前框选：未选择")
            return
        x0, y0, x1, y1 = square
        self.size_label.setText(
            f"当前框选：{x1 - x0} × {y1 - y0} 像素（左上角 {x0}, {y0}）"
        )

    def square(self) -> Optional[Tuple[int, int, int, int]]:
        return self.canvas.square()
