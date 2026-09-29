# -*- coding: utf-8 -*-
"""抠图模型管理。

模型文件（168MB ~ 977MB）**不进 Git 仓库**，统一放在 <程序目录>/models 下：
首次选中某个模型、或直接开始处理时如果文件不在，程序会自动从 rembg 官方
GitHub release 下载一次（界面上有真实进度），之后永远离线用。

内置支持：
  - isnet-general-use（通用，电子元件/产品/杂物）· 约 168MB
  - u2net_human_seg（人像/半身/全身）· 约 168MB
  - bria-rmbg（RMBG-2.0 · BRIA AI）· 约 977MB

怎么下载的：
  直接调 rembg 自己的 ``session.download_models()``，URL 与校验值（md5/sha256）
  都取自 rembg 的 session 定义，本项目不再另外维护一份下载地址；
  唯一做手脚的地方是把它的进度条对象换成我们的适配器（见 :func:`_pooch_progress_bar`），
  这样 pooch 下载时能把字节数转发到界面上。

关于目录结构：
  rembg 新版会下到 ``<U2NET_HOME>/models/<模型名>/<模型名>.onnx``（每个模型一个子目录），
  而本项目统一用扁平布局 ``models/<模型名>.onnx``。所以下载完（以及每次启动）
  都会跑一次 :func:`normalize_model_layout` 把文件搬平；读取时两条路径都认
  （见 :func:`existing_model_path`），万一文件被占用搬不动也不会误报「模型缺失」。
"""

import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from PIL import Image

from .. import paths

# 进度回调：progress(downloaded_bytes, total_bytes, status_text)
DownloadProgress = Callable[[int, int, str], None]


@dataclass(frozen=True)
class ModelInfo:
    """单个模型的元信息。"""

    model_id: str               # rembg 用的 alias，同时也是 models/ 下的文件名
    label: str                  # 下拉框显示名
    path: str                   # 规范位置：models/<model_id>.onnx
    size_mb: int = 0            # 下载体积（MB），用于提示文案
    notes: str = ""             # 补充说明（适用场景 / 效果）
    source_url: str = ""        # 下载来源，仅用于失败时给用户手动下载的指引
    can_auto_download: bool = True  # 缺失时能不能自动从远端拉


# rembg 官方 release 的统一前缀（手动下载兜底时提示用）
_RELEASE_URL = "https://github.com/danielgatis/rembg/releases/download/v0.0.0"


def _flat_path(model_id: str) -> str:
    """本项目的规范位置：三个模型平铺在 ``models/`` 下。"""
    return os.path.join(paths.MODELS_DIR, f"{model_id}.onnx")


def _nested_path(model_id: str) -> str:
    """rembg 新版的规范位置：``models/models/<模型名>/<模型名>.onnx``。

    rembg 只往这里写、不往扁平位置写，所以下载后要搬一次
    （见 :func:`normalize_model_layout`）。读取时两边都认。
    """
    return os.path.join(paths.MODELS_DIR, "models", model_id, f"{model_id}.onnx")


def _candidate_paths(model_id: str) -> List[str]:
    """能放这个模型的位置，按优先级排列。"""
    return [_flat_path(model_id), _nested_path(model_id)]


def _usable(path: str) -> bool:
    """文件存在且够大（排除下到一半 / 损坏的残留）。"""
    try:
        return os.path.getsize(path) >= MIN_MODEL_BYTES
    except OSError:
        return False


def existing_model_path(model_id: str) -> Optional[str]:
    """模型文件实际躺在哪儿；没有则返回 ``None``。"""
    for path in _candidate_paths(model_id):
        if _usable(path):
            return path
    return None


def _loose_candidates() -> List[str]:
    """历史遗留的散落文件。

    两类：
      1. ``models/models/<模型名>/<模型名>.onnx``（rembg 新版下进去的）
      2. ``models/`` 根目录下名字带版本号的，比如 ``bria-rmbg-2.0.onnx``
         （老版本直接照 release 的原始文件名下的）
    """
    found: List[str] = []
    roots = [paths.MODELS_DIR, os.path.join(paths.MODELS_DIR, "models")]
    for root in roots:
        if not os.path.isdir(root):
            continue
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            full = os.path.join(root, name)
            if not name.lower().endswith(".onnx"):
                if os.path.isdir(full):  # models/models/<模型名>/ 这一层
                    try:
                        for sub in os.listdir(full):
                            if sub.lower().endswith(".onnx"):
                                found.append(os.path.join(full, sub))
                    except OSError:
                        continue
                continue
            if os.path.isfile(full):
                found.append(full)
    return found


def _prune_empty_dirs() -> None:
    """清掉 rembg 留下的空 ``models/models/<模型名>/`` 目录。"""
    root = os.path.join(paths.MODELS_DIR, "models")
    if not os.path.isdir(root):
        return
    try:
        for name in os.listdir(root):
            sub = os.path.join(root, name)
            try:
                if os.path.isdir(sub) and not os.listdir(sub):
                    os.rmdir(sub)
            except OSError:
                continue
        if not os.listdir(root):
            os.rmdir(root)
    except OSError:
        pass


def normalize_model_layout() -> None:
    """把散落、名字不对的模型文件搬到扁平规范位置 ``models/<模型名>.onnx``。

    每次启动和每次下载完成后各跑一次，这样老版本留下的文件也能被认出来，
    不会莫名其妙重新下 977MB。

    搬不动（文件正被 onnxruntime 占用）也无所谓：:func:`existing_model_path`
    两条路径都查，只是位置不够整齐。
    """
    for info in MODELS:
        flat = _flat_path(info.model_id)
        if _usable(flat):
            continue
        for src in _loose_candidates():
            if not _usable(src):
                continue
            if info.model_id not in os.path.basename(src).lower():
                continue
            try:
                os.replace(src, flat)  # 同一分区，rename 是瞬时的
                paths.log(f"模型已归位：{src} → {flat}")
            except OSError as exc:  # 被占用就保持原样，不影响使用
                paths.log(f"模型归位失败（不影响使用）：{exc}")
            break
    _prune_empty_dirs()


MODELS: List[ModelInfo] = [
    ModelInfo(
        "isnet-general-use",
        "通用模型（电子元件 / 产品 / 杂物）",
        _flat_path("isnet-general-use"),
        size_mb=168,
        notes="ISNet 通用模型 · 电子元件、产品、杂物的默认选择",
        source_url=f"{_RELEASE_URL}/isnet-general-use.onnx",
    ),
    ModelInfo(
        "u2net_human_seg",
        "人像模型（人物 / 半身 / 全身）",
        _flat_path("u2net_human_seg"),
        size_mb=168,
        notes="U2Net 人像模型 · 人物、半身、全身",
        source_url=f"{_RELEASE_URL}/u2net_human_seg.onnx",
    ),
    ModelInfo(
        "bria-rmbg",
        "RMBG-2.0（BRIA AI · 效果最好）",
        _flat_path("bria-rmbg"),
        size_mb=977,
        notes="BRIA RMBG-2.0 · 效果最好，但体积大、速度慢一些",
        # 注意：bria 的 release 文件名带版本号，下到本地后会改名成 bria-rmbg.onnx
        source_url=f"{_RELEASE_URL}/bria-rmbg-2.0.onnx",
    ),
]

# alias → ModelInfo 字典
MODEL_BY_ID: Dict[str, ModelInfo] = {m.model_id: m for m in MODELS}

DEFAULT_MODEL = "isnet-general-use"

# 小于这个大小视为下载未完成 / 文件损坏
MIN_MODEL_BYTES = 1024 * 1024

_sessions: Dict[str, object] = {}
# 会话缓存。用 RLock 而不是 Lock 是防御性的：持锁期间会走下载流程，
# 万一以后下载逻辑里再取一次锁，普通 Lock 会直接死锁。
_session_lock = threading.RLock()
# 下载锁：界面上的「选中模型就自动下」和处理线程里的「缺了就下」可能同时发生，
# 靠它保证同一个模型只被下一份，不会重复占用带宽。
_download_lock = threading.RLock()


class ModelNotReady(RuntimeError):
    """模型文件缺失或损坏。"""


def model_info(model_id: str) -> ModelInfo:
    return MODEL_BY_ID[model_id]


def model_file(model_id: str) -> str:
    """模型文件的实际路径；还没下载时返回规范位置（方便提示用户放哪儿）。"""
    return existing_model_path(model_id) or _flat_path(model_id)


def model_size(model_id: str) -> int:
    path = model_file(model_id)
    return os.path.getsize(path) if os.path.exists(path) else 0


def is_available(model_id: str) -> bool:
    return existing_model_path(model_id) is not None


def missing_models() -> List[str]:
    return [m.model_id for m in MODELS if not is_available(m.model_id)]


class _PoochProgress:
    """pooch 的自定义进度条。

    pooch 约定：``total`` 由它赋值，之后每收一块就 ``update(chunk)``，结束时 ``close()``。
    传 ``progressbar=True`` 只会往 stderr 画 tqdm 进度条（GUI 里完全看不到），
    所以这里换成把字节数转发给界面。

    回调按「百分比变化」节流：168MB 按 1KB 一块是十几万次 ``update``，
    直接转发会把 UI 信号队列打爆。
    """

    def __init__(self, model_id: str, progress: Optional[DownloadProgress]):
        self._model_id = model_id
        self._progress = progress
        self._last_percent = -1
        self._last_emit = 0.0
        self.n = 0
        self.total = 0

    def update(self, size: int) -> None:
        self.n += max(0, int(size))
        if self._progress is None:
            return
        if self.total > 0:
            percent = min(100, self.n * 100 // self.total)
            if percent == self._last_percent:
                return
            self._last_percent = percent
        else:
            # 服务器没给 content-length，就按时间节流
            now = time.monotonic()
            if now - self._last_emit < 1.0:
                return
            self._last_emit = now
        self._emit()

    def close(self) -> None:
        """pooch 结束时会调用，补一次收尾（确保能显示到 100%）。"""
        self._emit()

    def _emit(self) -> None:
        if self._progress is None:
            return
        done_mb = self.n / (1024 * 1024)
        if self.total > 0:
            total_mb = self.total / (1024 * 1024)
            text = (
                f"正在下载 {self._model_id}："
                f"{min(100, self.n * 100 // self.total)}%（{done_mb:.0f}/{total_mb:.0f} MB）"
            )
        else:
            text = f"正在下载 {self._model_id}：已收到 {done_mb:.0f} MB"
        self._progress(self.n, self.total, text)


@contextmanager
def _pooch_progress_bar(
    model_id: str, progress: Optional[DownloadProgress]
) -> Iterator[None]:
    """临时把 ``pooch.retrieve`` 的进度条换成我们的适配器（其它参数原样透传）。

    只改 ``progressbar`` 这一个参数：URL、校验值、落盘目录都还是 rembg 自己的，
    这样既不用维护第二份下载地址，又能拿到真实进度。
    """
    import pooch

    original = pooch.retrieve

    def patched(url, known_hash=None, fname=None, path=None, progressbar=False, **kwargs):
        return original(
            url,
            known_hash,
            fname=fname,
            path=path,
            progressbar=_PoochProgress(model_id, progress),
            **kwargs,
        )

    pooch.retrieve = patched
    try:
        yield
    finally:
        pooch.retrieve = original


def _download_via_rembg(model_id: str, progress: Optional[DownloadProgress] = None) -> None:
    """用 rembg 自己的下载器把模型拉下来（含校验），进度转发到界面。

    这里直接调 session 类的 ``download_models()``，而不是 ``new_session()``：
    前者只下文件、不建推理会话，既不会白吃一份内存，也不会让 onnxruntime
    占着文件导致后面「搬到扁平目录」失败。
    """
    from rembg.sessions import sessions as rembg_sessions

    info = model_info(model_id)
    short = info.label.split("（")[0].strip()
    if progress:
        progress(0, 0, f"正在连接 GitHub 下载 {short}（约 {info.size_mb}MB）...")

    try:
        session_cls = rembg_sessions[model_id]
    except KeyError as exc:  # rembg 版本对不上时给个明确说法
        raise ModelNotReady(
            f"当前 rembg 不认识模型 {model_id}，请升级 rembg 后重试。"
        ) from exc

    with _pooch_progress_bar(model_id, progress):
        session_cls.download_models()


def download_model(model_id: str, progress: Optional[DownloadProgress] = None) -> str:
    """确保模型文件就位，缺了就下；返回文件路径（扁平规范位置）。

    多个线程同时要同一个模型时只会下一份（靠 ``_download_lock`` 串起来）。
    """
    with _download_lock:
        existing = existing_model_path(model_id)
        if existing is not None:
            return existing

        info = model_info(model_id)
        if not info.can_auto_download:
            raise ModelNotReady(
                f"模型文件缺失：{info.path}\n"
                "请把对应的 .onnx 文件放进 models 目录后重试。"
            )

        paths.setup_environment()  # 保证 U2NET_HOME 指向项目 models 目录
        _download_via_rembg(model_id, progress)
        normalize_model_layout()  # rembg 下在嵌套目录，搬到扁平位置

        found = existing_model_path(model_id)
        if found is None:
            raise ModelNotReady(
                f"{info.label} 下载后仍找不到模型文件。\n"
                f"预期位置：{_flat_path(model_id)}\n"
                "可能网络中断、防火墙拦截或磁盘空间不足；"
                f"也可以手动下载后改名放到 models 目录：\n{info.source_url}"
            )
        if progress:
            size_mb = os.path.getsize(found) / (1024 * 1024)
            progress(
                os.path.getsize(found),
                os.path.getsize(found),
                f"{info.label.split('（')[0].strip()} 已就绪（{size_mb:.0f} MB）",
            )
        return found


def get_session(model_id: str, progress: Optional[DownloadProgress] = None):
    """获取（并缓存）一个 CPU 推理会话。

    模型不在就自动下载（带 ``progress`` 进度回调），已经下过就直接复用本地文件。
    下载期间会持锁，但调用方都在 worker 线程里，不会卡 UI。
    """
    with _session_lock:
        cached = _sessions.get(model_id)
        if cached is not None:
            return cached

        download_model(model_id, progress)

        paths.setup_environment()  # 确保 U2NET_HOME 已指向项目 models 目录
        from rembg import new_session

        session = new_session(model_id, providers=["CPUExecutionProvider"])
        _sessions[model_id] = session
        return session


def predict_mask(
    image: Image.Image,
    model_id: str,
    session=None,
    post_process: bool = False,
    progress: Optional[DownloadProgress] = None,
) -> Image.Image:
    """返回与原图同尺寸的前景蒙版（L 模式，255 = 主体）。"""
    if session is None:
        session = get_session(model_id, progress=progress)

    from rembg import remove

    return remove(
        image,
        session=session,
        only_mask=True,
        post_process_mask=post_process,
    )


def decontaminate(rgb: Image.Image, alpha: Image.Image) -> Image.Image:
    """去除边缘残留的背景色晕（会额外耗时）。"""
    from rembg.bg import decontaminate_cutout

    return decontaminate_cutout(rgb, alpha)