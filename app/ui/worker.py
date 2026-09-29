# -*- coding: utf-8 -*-
"""后台处理线程：串行处理图片，避免界面卡死，也不会把笔记本 CPU 打满。"""

import os
from typing import Callable, Dict, List, Optional, Tuple

from PyQt5.QtCore import QThread, pyqtSignal

from ..core import remover
from ..core.processor import Result, Settings, process_image


class ProcessWorker(QThread):
    progress = pyqtSignal(int, int, str)  # 已完成数, 总数, 当前文件名/提示
    item_done = pyqtSignal(object)  # Result
    all_done = pyqtSignal()

    def __init__(
        self,
        paths,
        settings: Settings,
        parent=None,
        hint_for: Optional[Callable[[str], Tuple[List[Tuple[int, int, int, int]],
                                                List[Tuple[int, int, int, int]],
                                                bool]]] = None,
        prev_results: Optional[Dict[str, Result]] = None,
    ):
        """``hint_for(path) -> (keep_boxes, drop_boxes, use_after_input)``：

        每张图处理前被调用，返回该图专属的 keep / drop hint 列表，以及是否应以上一次
        处理后的 ``result.after`` 作为输入图（二次迭代）。
        没传就按 settings.keep_boxes / settings.drop_boxes 用（适用于「所有图用同一份 hint」的情况）。
        """
        super().__init__(parent)
        self._paths = list(paths)
        self._base_settings = settings
        self._hint_for = hint_for
        self._prev_results = prev_results or {}
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def run(self) -> None:
        total = len(self._paths)
        session = None

        for index, path in enumerate(self._paths):
            if self._cancelled:
                break

            if session is None:
                self.progress.emit(index, total, f"正在加载模型（{self._base_settings.model}）...")
                try:
                    # 模型没下载过时 get_session 会自动下（进度转发到界面进度条/状态栏）
                    session = remover.get_session(
                        self._base_settings.model,
                        progress=lambda _d, _t, text: self.progress.emit(index, total, text),
                    )
                except Exception as exc:  # noqa: BLE001
                    for rest in self._paths[index:]:
                        self.item_done.emit(Result(rest, False, str(exc)))
                    break

            self.progress.emit(index, total, os.path.basename(path))
            settings = self._settings_for(path)

            # 二次迭代：以上一次处理后的 result.after 作为输入图
            prev_after = None
            if settings.use_after_input and path in self._prev_results:
                prev = self._prev_results[path]
                if prev.ok and prev.after is not None:
                    prev_after = prev.after

            result = process_image(path, settings, session=session, image=prev_after)
            # 把当前图的 hint 带回 Result，方便界面里再次进入弹窗时知道已有值
            result.keep_boxes = list(settings.keep_boxes)
            result.drop_boxes = list(settings.drop_boxes)
            result.used_after_input = prev_after is not None
            self.item_done.emit(result)

        self.all_done.emit()

    def _settings_for(self, path: str) -> Settings:
        base = self._base_settings
        if self._hint_for is None:
            return base
        try:
            keep, drop, use_after = self._hint_for(path)
        except Exception:  # noqa: BLE001
            keep, drop, use_after = [], [], False
        from dataclasses import replace

        return replace(
            base,
            keep_boxes=list(keep),
            drop_boxes=list(drop),
            use_after_input=bool(use_after),
        )