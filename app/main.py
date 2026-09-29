# -*- coding: utf-8 -*-
"""程序入口。"""

import os
import sys


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
