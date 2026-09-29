# -*- coding: utf-8 -*-
"""路径与环境初始化。

所有路径都锚定在「程序目录」上：
  - 开发时：项目根目录
  - 打包成 exe 后：exe 所在目录

这样换电脑、拷贝整个文件夹都能完全一致地运行。
"""

import os
import shutil
import sys
import time


def _detect_base_dir() -> str:
    if getattr(sys, "frozen", False):
        # PyInstaller 打包后，sys.executable 是 exe 本身
        return os.path.dirname(os.path.abspath(sys.executable))
    # 本文件位于 <项目根>/app/paths.py，向上两级即程序目录
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


BASE_DIR = _detect_base_dir()
MODELS_DIR = os.path.join(BASE_DIR, "models")
# 成品图片存放处，也就是界面里的「我的素材库」
GALLERY_DIR = os.path.join(BASE_DIR, "gallery")
RESOURCES_DIR = os.path.join(BASE_DIR, "app", "resources")
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_PATH = os.path.join(LOG_DIR, "startup.log")
READY_PATH = os.path.join(LOG_DIR, "ui_ready.txt")

# 旧版本把成品放在 output/，启动时自动搬到 gallery/，避免用户已保存的图「消失」
_LEGACY_GALLERY_DIR = os.path.join(BASE_DIR, "output")

# 程序图标：默认就是程序目录下的 logo.png（换成 logo.ico 小尺寸会更清楚）
DEFAULT_ICON_PATH = os.path.join(BASE_DIR, "logo.png")
# 找图标的顺序：优先 .ico（Windows 标题栏 / 任务栏的小尺寸更清楚），
# 程序目录和 app/resources 两处都找，方便打包 exe 时把图标单独放在资源目录。
ICON_CANDIDATES = (
    os.path.join(BASE_DIR, "logo.ico"),
    DEFAULT_ICON_PATH,
    os.path.join(RESOURCES_DIR, "logo.ico"),
    os.path.join(RESOURCES_DIR, "logo.png"),
)

# Windows 任务栏靠它给程序分组：不设的话，用 pythonw 起的程序会被归到「Python」
# 那一组，任务栏图标显示的是 Python 的图标，而不是我们自己的。
WINDOWS_APP_ID = "removebg.material.library.1"


def icon_path() -> str:
    """返回实际存在的图标文件；一个都没找到就返回默认位置（便于提示用户放哪儿）。"""
    for path in ICON_CANDIDATES:
        if os.path.isfile(path):
            return path
    return DEFAULT_ICON_PATH


_app_id_set = False


def set_windows_app_id() -> None:
    """把本进程标记成一个独立应用，让 Windows 任务栏用自己的图标。

    必须在创建窗口之前调用；非 Windows 平台直接跳过。
    """
    global _app_id_set
    if _app_id_set or not sys.platform.startswith("win"):
        return
    _app_id_set = True
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(WINDOWS_APP_ID)
        log(f"Windows AppUserModelID 已设置：{WINDOWS_APP_ID}")
    except Exception as exc:  # noqa: BLE001 - 设不上只是图标不好看，不影响运行
        log(f"设置 Windows AppUserModelID 失败（不影响使用）：{exc}")


def log(message: str) -> None:
    """把启动过程写进 logs/startup.log，方便排查「双击没反应 / 启动崩溃」。"""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        stamp = time.strftime("%H:%M:%S")
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] {message}\n")
    except Exception:
        pass


def mark_ui_ready() -> None:
    """界面成功显示后写标记文件，启动脚本据此判断程序是否真的起来了。"""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(READY_PATH, "w", encoding="utf-8") as fh:
            fh.write(f"ok\npid={os.getpid()}\npython={sys.executable}\nbase={BASE_DIR}\n")
        log("界面已就绪：ui_ready 标记已写入")
    except Exception:
        pass


def _qt_plugins_dir() -> str | None:
    """返回 PyQt5 自带的 Qt 插件目录（plugins），找不到返回 None。"""
    candidates = []
    if getattr(sys, "frozen", False):
        # PyInstaller 打包后，PyQt5 被解包到 _MEIPASS
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(os.path.join(meipass, "PyQt5", "Qt5", "plugins"))
    try:
        import PyQt5

        candidates.append(
            os.path.join(os.path.dirname(os.path.abspath(PyQt5.__file__)), "Qt5", "plugins")
        )
    except Exception:
        pass
    for path in candidates:
        if os.path.isdir(path):
            return path
    return None


def fix_qt_plugin_path() -> None:
    """修复「no Qt platform plugin could be initialized」启动崩溃。

    原因：当程序所在路径包含中文等非 ASCII 字符时，Qt5 内部把插件目录
    解析成了 ``????``（8 位编码丢信息），于是找不到 qwindows.dll 平台插件，
    直接 qFatal 崩溃（退出码 0xC0000409）。

    做法：在创建 QApplication 之前，用 Python 字符串（PyQt5 会转成 QString，
    全程 UTF-16 不丢字符）把插件目录注册进 Qt，并同步设置环境变量。

    必须在任何 QApplication 之前调用。
    """
    # 必须在 import PyQt5 之前先把 onnxruntime 加载进来。
    # 否则 onnxruntime 1.30.0 的原生 DLL 会在 Qt5 加载之后被 PyQt5 的同名
    # OpenMP / VC++ runtime DLL 覆盖，从而在 import 时段错误（0xC0000005）。
    try:
        import onnxruntime  # noqa: F401  -- 仅为了让原生 DLL 先被加载
        log("onnxruntime 已预加载")
    except Exception as exc:  # pragma: no cover - 极少见
        log(f"预加载 onnxruntime 失败：{exc}")

    plugins = _qt_plugins_dir()
    if not plugins:
        log("警告：找不到 PyQt5 插件目录，Qt 可能无法启动")
        return

    platforms = os.path.join(plugins, "platforms")
    # 只有当外部环境变量确实指向可用的平台插件目录时才尊重它，否则强制覆盖，
    # 避免被其它软件（Anaconda、Qt 安装包等）设置的环境变量带偏。
    current_qpa = os.environ.get("QT_QPA_PLATFORM_PLUGIN_PATH")
    if not (current_qpa and os.path.isfile(os.path.join(current_qpa, "qwindows.dll"))):
        os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = platforms
    current_plugin = os.environ.get("QT_PLUGIN_PATH")
    if not (current_plugin and os.path.isdir(current_plugin)):
        os.environ["QT_PLUGIN_PATH"] = plugins

    try:
        from PyQt5.QtCore import QCoreApplication

        QCoreApplication.addLibraryPath(plugins)
        log(f"Qt 插件目录已注册：{plugins}")
    except Exception as exc:  # pragma: no cover - 环境异常时降级
        log(f"注册 Qt 插件目录失败：{exc}")


def migrate_legacy_gallery() -> None:
    """把旧版本的 output/ 目录迁移成 gallery/，尽量不丢用户已经保存的图片。"""
    if not os.path.isdir(_LEGACY_GALLERY_DIR):
        return
    try:
        if not os.path.isdir(GALLERY_DIR):
            os.rename(_LEGACY_GALLERY_DIR, GALLERY_DIR)
            log(f"已把 output/ 重命名为 gallery/：{GALLERY_DIR}")
            return
        moved = 0
        for name in os.listdir(_LEGACY_GALLERY_DIR):
            src = os.path.join(_LEGACY_GALLERY_DIR, name)
            dst = os.path.join(GALLERY_DIR, name)
            if os.path.isfile(src) and not os.path.exists(dst):
                shutil.move(src, dst)
                moved += 1
        if moved:
            log(f"已从 output/ 迁移 {moved} 个文件到 gallery/")
        if not os.listdir(_LEGACY_GALLERY_DIR):
            os.rmdir(_LEGACY_GALLERY_DIR)
    except Exception as exc:  # noqa: BLE001 - 迁移失败不影响程序启动
        log(f"迁移 output/ 失败（可手动改名）：{exc}")


def setup_environment() -> None:
    """创建目录、固定 rembg 模型位置、修复 Qt 插件路径。"""
    log(f"程序目录：{BASE_DIR}")
    log(f"Python：{sys.executable}")

    os.makedirs(MODELS_DIR, exist_ok=True)
    migrate_legacy_gallery()
    os.makedirs(GALLERY_DIR, exist_ok=True)

    # rembg 默认把模型下载到 用户目录/.u2net，这里强制指向项目内的 models 目录，
    # 保证模型随项目走、换电脑零差异、且不会偷偷联网下载。
    os.environ["U2NET_HOME"] = MODELS_DIR

    # 让 Windows 任务栏把本程序当独立应用，用自己的图标（要在建窗口之前设）
    set_windows_app_id()

    # CPU 推理线程数：默认用一半逻辑核心，避免笔记本满载降频、风扇狂转
    if "OMP_NUM_THREADS" not in os.environ:
        os.environ["OMP_NUM_THREADS"] = str(max(1, (os.cpu_count() or 4) // 2))

    # 路径含中文时必须先修复 Qt 插件查找，否则 QApplication 会直接崩溃
    fix_qt_plugin_path()


def model_path(model_name: str) -> str:
    return os.path.join(MODELS_DIR, f"{model_name}.onnx")
