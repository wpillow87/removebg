# -*- coding: utf-8 -*-
"""「重命名 & 分组」对话框。

规则：
  - 标签写在文件名里：``主名[标签1][标签2].扩展名``，扩展名永远不动；
  - **单选**：可以改主名，标签可增可删（勾选状态 = 最终标签）；
    右侧还带预览 + 旋转，角度不为 0 时会把文件转正后覆盖保存；
  - **多选**：主名不可改，下面勾选的标签会**追加**到每一个选中的文件上，
    此时不提供旋转（避免一次转错一整批）。
"""

import os
from typing import List, Optional, Sequence, Tuple

from PIL import Image
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..core import tags as taglib
from ..core.image_io import can_rotate_in_place, load_image, rotate_image
from .widgets import FlowLayout, ImageCanvas, RotateBar

CHIP_STYLE = (
    "QPushButton{border:1px solid #c3ccd6;border-radius:13px;padding:4px 12px;"
    "background:#f6f8fb;color:#3a4149;}"
    "QPushButton:hover{border:1px solid #2f7ed8;color:#2f7ed8;}"
    "QPushButton:checked{background:#2f7ed8;border:1px solid #2f7ed8;"
    "color:white;font-weight:bold;}"
)

# 预览用的最长边：太大每次微调都要重采样，卡手
PREVIEW_MAX = 720

try:  # Pillow >= 9.1
    _LANCZOS = Image.Resampling.LANCZOS
except AttributeError:  # pragma: no cover - 兼容老版本 Pillow
    _LANCZOS = Image.LANCZOS

# entries 里每一项： (path, base, tags, ext, ctime)
Entry = Tuple[str, str, List[str], str, float]


class RenameGroupDialog(QDialog):
    """改主文件名 + 勾选 / 新增标签（单选还能转正），确定后由调用方落盘。"""

    def __init__(self, entries: Sequence[Entry], presets: Sequence[str], parent=None):
        super().__init__(parent)
        self._entries: List[Entry] = list(entries)
        self._multi = len(self._entries) > 1
        self._ext = self._entries[0][3] if self._entries else ""
        self._chip_buttons: List[QPushButton] = []
        self._preview_image: Optional[Image.Image] = None

        self.setWindowTitle("重命名 & 分组")
        self.setMinimumWidth(560 if self._multi else 980)
        self.setMinimumHeight(420 if not self._multi else 0)

        self._build_ui(presets)
        self._refresh_preview()
        self._refresh_rotation_preview()

    # ============================================================ 构建界面

    def _build_ui(self, presets: Sequence[str]) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(10)

        if self._multi:
            head = (
                f"已选中 {len(self._entries)} 个文件：文件名留空就各自保持原名，"
                "填了就统一改成这个名字（重名自动加 _1）。"
            )
        else:
            head = f"正在修改：{os.path.basename(self._entries[0][0])}"
        head_label = QLabel(head)
        head_label.setStyleSheet("font-weight:bold;color:#2b3138;")
        root.addWidget(head_label)

        body = QHBoxLayout()
        body.setSpacing(14)
        body.addWidget(self._build_left_column(presets), 3)
        right = self._build_right_column()
        body.addWidget(right, 2)
        if self._multi:
            right.setVisible(False)
        root.addLayout(body, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("确定")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        # 界面构建完成后才允许刷新预览（前面 setText 会触发 textChanged）
        self._ready = True

    def _build_left_column(self, presets: Sequence[str]) -> QWidget:
        column = QWidget()
        root = QVBoxLayout(column)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        # ---- 文件名行 ----
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("文件名："))
        self.name_edit = QLineEdit()
        self.name_edit.textChanged.connect(self._refresh_preview)
        name_row.addWidget(self.name_edit, 1)
        self.ext_label = QLabel(self._ext or "（无后缀）")
        self.ext_label.setStyleSheet("color:#7a828c;")
        name_row.addWidget(self.ext_label)
        root.addLayout(name_row)

        if self._multi:
            self.name_edit.setPlaceholderText(
                f"留空 = 各自保持原名；填了 = {len(self._entries)} 个文件统一用这个名字"
            )
        else:
            self.name_edit.setPlaceholderText("主文件名（不含标签和后缀）")
            self.name_edit.setText(self._entries[0][1])
            # 默认聚焦并全选主名，直接敲键盘就能改
            self.name_edit.setFocus()
            self.name_edit.selectAll()

        # ---- 标签区 ----
        tag_title = QLabel("分组标签（点一下切换选中，可多选）")
        tag_title.setStyleSheet("color:#4a525c;font-weight:bold;margin-top:4px;")
        root.addWidget(tag_title)

        self._chips_host = QWidget()
        self._chips_layout = FlowLayout(self._chips_host, margin=2, h_spacing=8, v_spacing=8)
        self._chips_scroll = QScrollArea()
        self._chips_scroll.setWidgetResizable(True)
        self._chips_scroll.setWidget(self._chips_host)
        self._chips_scroll.setMinimumHeight(96)
        self._chips_scroll.setMaximumHeight(190)
        self._chips_scroll.setStyleSheet(
            "QScrollArea{border:1px solid #e2e7ee;border-radius:6px;}"
        )
        root.addWidget(self._chips_scroll)

        initial = self._initial_chips(presets)
        checked = set() if self._multi else set(self._entries[0][2])
        for tag in initial:
            self._add_chip(tag, tag in checked)
        self._sync_chips_height()

        # ---- 自定义标签 ----
        custom_row = QHBoxLayout()
        custom_row.addWidget(QLabel("新标签："))
        self.custom_edit = QLineEdit()
        self.custom_edit.setPlaceholderText("临时新标签，回车或点「添加」")
        self.custom_edit.returnPressed.connect(self._add_custom_tag)
        custom_row.addWidget(self.custom_edit, 1)
        self.add_tag_btn = QPushButton("添加")
        self.add_tag_btn.clicked.connect(self._add_custom_tag)
        custom_row.addWidget(self.add_tag_btn)
        root.addLayout(custom_row)

        self.remember_check = QCheckBox("把上面勾选的新标签存为常用标签")
        self.remember_check.setChecked(True)
        root.addWidget(self.remember_check)

        # ---- 最终文件名预览 ----
        self.preview_label = QLabel()
        self.preview_label.setWordWrap(True)
        self.preview_label.setStyleSheet(
            "background:#f3f6fa;border:1px solid #e2e7ee;border-radius:6px;"
            "padding:6px 8px;color:#2b3138;"
        )
        root.addWidget(self.preview_label)
        root.addStretch(1)
        return column

    def _build_right_column(self) -> QWidget:
        column = QWidget()
        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        title = QLabel("预览与旋转")
        title.setStyleSheet("color:#4a525c;font-weight:bold;")
        layout.addWidget(title)

        self.preview_canvas = ImageCanvas("预览")
        self.preview_canvas.setMinimumSize(300, 260)
        self.preview_canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.preview_canvas.set_image(None, "选中素材后在这里预览")
        layout.addWidget(self.preview_canvas, 1)

        self.rotate_bar = RotateBar()
        self.rotate_bar.angle_changed.connect(self._on_angle_changed)
        layout.addWidget(self.rotate_bar)

        self.rotate_hint = QLabel(
            "旋转后会顺带把主体四周的空白清掉，点「确定」时直接覆盖保存（扩展名不变）。"
        )
        self.rotate_hint.setWordWrap(True)
        self.rotate_hint.setStyleSheet("color:#7a828c;font-size:12px;")
        layout.addWidget(self.rotate_hint)

        self._prepare_preview_image()
        return column

    def _prepare_preview_image(self) -> None:
        """读一张缩小版原图，旋转预览都在它上面做，保证微调不卡。"""
        path = self._entries[0][0] if self._entries else ""
        try:
            image = load_image(path)
        except Exception:  # noqa: BLE001
            self.preview_canvas.set_image(None, "无法预览该图片")
            self.rotate_bar.set_enabled(False)
            self.rotate_hint.setText("这张图读不出来，旋转不可用。")
            return

        if max(image.size) > PREVIEW_MAX:
            image.thumbnail((PREVIEW_MAX, PREVIEW_MAX), _LANCZOS)
        self._preview_image = image

        if not can_rotate_in_place(path):
            self.rotate_bar.set_enabled(False)
            self.rotate_hint.setText(
                f"{self._ext} 不能旋转后覆盖保存（会损坏原文件），"
                "需要转方向请先导出成 PNG。"
            )
        else:
            self.rotate_bar.set_enabled(True)

    # ============================================================ 标签按钮

    def _initial_chips(self, presets: Sequence[str]) -> List[str]:
        """初始展示的标签：常用标签 + 选中文件已有的标签。"""
        existing: List[str] = []
        for _path, _base, tags, _ext, _ctime in self._entries:
            existing.extend(tags)
        return taglib.normalize_tags(list(presets) + existing)

    def _add_chip(self, tag: str, checked: bool) -> None:
        tag = (tag or "").strip()
        if not tag:
            return
        for btn in self._chip_buttons:  # 已有同名标签就只更新选中状态
            if btn.text() == tag:
                if checked:
                    btn.setChecked(True)
                return
        btn = QPushButton(tag)
        btn.setCheckable(True)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setStyleSheet(CHIP_STYLE)
        btn.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        btn.setChecked(checked)
        btn.toggled.connect(lambda *_: self._refresh_preview())
        self._chip_buttons.append(btn)
        self._chips_layout.addWidget(btn)
        self._sync_chips_height()

    def _sync_chips_height(self) -> None:
        """让标签容器按实际行数撑高，避免按钮被裁掉。"""
        width = self._chips_scroll.viewport().width() if hasattr(self, "_chips_scroll") else 0
        if width <= 60:
            width = max(260, self.width() - 60)
        self._chips_host.setMinimumHeight(self._chips_layout.heightForWidth(width))

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._sync_chips_height()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        # 打开对话框时直接聚焦并全选主名，用户可以立刻敲键盘改名
        if not self._multi:
            self.name_edit.setFocus()
            self.name_edit.selectAll()

    # ============================================================ 交互

    def _add_custom_tag(self) -> None:
        tag = self.custom_edit.text().strip()
        ok, reason = taglib.is_valid_tag(tag)
        if not ok:
            QMessageBox.warning(self, "标签不可用", reason)
            return
        self._add_chip(tag, True)
        self.custom_edit.clear()
        self._refresh_preview()

    def _on_accept(self) -> None:
        text = self.name_edit.text().strip()
        if text:  # 留空表示「沿用各自原来的主名」，不用校验
            ok, reason = taglib.is_valid_base(text)
            if not ok:
                QMessageBox.warning(self, "文件名不可用", reason)
                self.name_edit.setFocus()
                return
        self.accept()

    def _on_angle_changed(self, _angle: int) -> None:
        self._refresh_rotation_preview()
        self._refresh_preview()

    # ============================================================ 结果

    def is_multi(self) -> bool:
        return self._multi

    def checked_tags(self) -> List[str]:
        return taglib.normalize_tags(b.text() for b in self._chip_buttons if b.isChecked())

    def should_remember(self) -> bool:
        return self.remember_check.isChecked()

    def rotation(self) -> int:
        """要应用的旋转角（多选时恒为 0）。"""
        return 0 if self._multi else self.rotate_bar.angle()

    def new_base_for(self, original_base: str) -> str:
        """某个文件最终使用的主文件名；输入框留空就沿用各自原来的名字。"""
        return self.name_edit.text().strip() or original_base

    def final_tags_for(self, current_tags: Sequence[str]) -> List[str]:
        """某个文件最终的标签列表。

        单选 = 勾选结果；多选 = 原标签 + 勾选结果（只增不减）。
        """
        checked = self.checked_tags()
        if self._multi:
            return taglib.normalize_tags(list(current_tags) + checked)
        return checked

    def desired_names(self) -> List[Tuple[str, str]]:
        """``[(路径, 想要的文件名), ...]``，还没做去重编号。"""
        return [
            (
                path,
                taglib.compose_name(self.new_base_for(base), self.final_tags_for(tags), ext),
            )
            for path, base, tags, ext, _ctime in self._entries
        ]

    def changes(self) -> List[Tuple[str, str, int]]:
        """返回 ``[(老路径, 最终文件名, 旋转角), ...]``，只包含真正有变化的项。

        重名由 :func:`tags.plan_renames` 统一处理成 ``_1`` / ``_2`` 序号。
        """
        angle = self.rotation()
        final = dict(taglib.plan_renames(self.desired_names()))
        plan: List[Tuple[str, str, int]] = []
        for path, wanted in self.desired_names():
            new_name = final.get(path, wanted)
            if new_name != os.path.basename(path) or angle:
                plan.append((path, new_name, angle))
        return plan

    # ============================================================ 预览

    def _refresh_rotation_preview(self) -> None:
        if self._preview_image is None:
            return
        angle = self.rotation()
        image = rotate_image(self._preview_image, angle) if angle else self._preview_image
        self.preview_canvas.set_image(image, f"{image.width} × {image.height}")

    def _refresh_preview(self) -> None:
        if not getattr(self, "_ready", False):
            return
        desired = self.desired_names()
        final = dict(taglib.plan_renames(desired))

        lines: List[str] = []
        for path, wanted in desired[:6]:
            name = final.get(path, wanted)
            old = os.path.basename(path)
            lines.append(f"· {old}（不变）" if name == old else f"· {old}  →  {name}")
        if len(desired) > 6:
            lines.append(f"· ... 其余 {len(desired) - 6} 个")

        angle = self.rotation()
        if angle:
            lines.append(f"· 并顺时针旋转 {angle}° + 清理空白（覆盖保存）")
        numbered = sum(1 for path, wanted in desired if final.get(path, wanted) != wanted)
        if numbered:
            lines.append(f"· {numbered} 个重名，已自动加 _1 / _2 序号")

        title = "最终文件名：" if not self._multi else "即将重命名："
        if not self.changes():
            lines.append("（没有变化）")
        self.preview_label.setText(title + "\n" + "\n".join(lines))
