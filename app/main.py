# -*- coding: utf-8 -*-
"""程序入口。"""

import os
import sys


def _app_icon(path: str):
    """把 logo 装成多尺寸 QIcon（找不到图标就返回空 QIcon）。

    Qt 直接拿一张大 PNG 去缩 16px 会糊，所以这里按标题栏 / 任务栏 / Alt+Tab 常用的
    几档尺寸各缩一份，谁用哪档就取哪档。

    两个调用前提：
      · PyQt5 的导入放在函数里 —— 本项目要求先 import onnxruntime 再加载 Qt
        （见 paths.fix_qt_plugin_path 的说明）；
      · 必须在 QApplication 创建**之后**调用，QPixmap 离开 QGuiApplication 会直接 abort。
    """
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QIcon, QImage, QPixmap

    if not os.path.isfile(path):
        return QIcon()

    source = QImage(path)
    if source.isNull():
        return QIcon()

    icon = QIcon()
    for size in (16, 20, 24, 32, 40, 48, 64, 128, 256):
        icon.addPixmap(
            QPixmap.fromImage(
                source.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )
        )
    return icon


def main() -> int:
    # 保证以脚本方式运行时能找到 app 包
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)

    from . import paths

    paths.setup_environment()

    # 模型文件不进仓库，克隆下来第一次跑时是空的；
    # 这里顺手把散落在 rembg 嵌套目录（models/models/<名>/）或沿用 release
    # 原始文件名的模型搬成 models/<名>.onnx，老用户不会因为改名被重新下一遍。
    from .core import remover

    remover.normalize_model_layout()

    paths.log("开始导入 PyQt5")
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QFont
    from PyQt5.QtWidgets import QApplication

    paths.log("PyQt5 导入完成，正在创建 QApplication")

    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    paths.log(f"QApplication 创建成功，platform={app.platformName()}")
    app.setApplicationName("图片去背景工具")
    app.setStyle("Fusion")

    # 程序图标：程序目录下的 logo.png（app.setWindowIcon 会被所有窗口/对话框继承）
    icon_file = paths.icon_path()
    icon = _app_icon(icon_file)
    if icon.isNull():
        paths.log(f"没找到程序图标，可放一张 logo.png 到：{icon_file}")
    else:
        app.setWindowIcon(icon)
        paths.log(f"程序图标已加载：{icon_file}")

    font = QFont("Microsoft YaHei UI", 9)
    app.setFont(font)
    app.setStyleSheet(
        """
        QGroupBox {
            border: 1px solid #d7dbe0;
            border-radius: 6px;
            margin-top: 10px;
            padding-top: 8px;
            font-weight: bold;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 4px;
        }
        QPushButton {
            padding: 5px 12px;
            border: 1px solid #c6ccd4;
            border-radius: 4px;
            background: #f7f8fa;
        }
        QPushButton:hover { background: #eef1f5; }
        QPushButton:disabled { color: #a8b0ba; background: #f2f3f5; }
        QListWidget { border: none; }
        """
    )

    from .ui.main_window import MainWindow

    window = MainWindow()
    window.show()
    paths.mark_ui_ready()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
