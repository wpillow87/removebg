# -*- coding: utf-8 -*-
"""图片读取 / 保存。

统一把图片转成 RGBA 处理，这样抠图结果（透明底）和原图可以用同一套逻辑。
HEIC/HEIF 依赖 pillow-heif（纯 pip 安装，带 libheif，CPU 可用）。
SVG 是矢量图，用 PyQt5 自带的 QtSvg 光栅化成透明底位图，之后走同一套逻辑。
"""

import os
from typing import Iterable, List, Optional, Tuple

import numpy as np
from PIL import Image, ImageOps

try:  # HEIC 解码
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_AVAILABLE = True
except Exception:  # pragma: no cover - 环境缺失时降级
    HEIF_AVAILABLE = False


SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".heic", ".heif", ".svg"}
SVG_EXTS = {".svg"}
# 能在原文件上直接旋转后覆盖保存的格式。
# SVG 会丢矢量、ICO 是多尺寸包、BMP/HEIC 透明与写入支持不稳，都不在里面。
ROTATABLE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}

# SVG 光栅化的最短 / 最长边：
# 小图标放大到 256 才够清晰，大图最多渲染到 1024，避免吃内存。
SVG_MIN_SIDE = 256
SVG_MAX_SIDE = 1024

# 给 Qt 文件对话框用的过滤器
FILE_DIALOG_FILTER = (
    "图片文件 (*.png *.jpg *.jpeg *.webp *.bmp *.heic *.heif *.svg);;所有文件 (*.*)"
)


def is_supported(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in SUPPORTED_EXTS


def can_rotate_in_place(path: str) -> bool:
    """这个文件能不能旋转 / 清理空白后直接覆盖保存。"""
    return os.path.splitext(path)[1].lower() in ROTATABLE_EXTS


# alpha 大于它才算「主体」；抠图裁剪和这里的清空白共用同一套判据
ALPHA_THRESHOLD = 16

# 素材长边上限：出口统一压到这个尺寸以内（只缩不放，长宽比不变）
MAX_SIDE = 800

# 0~255 → 0/255 的查找表，把 alpha 快速二值化后再找外接矩形（比逐像素判定快得多）
_TRIM_LUT = [255 if _v >= ALPHA_THRESHOLD else 0 for _v in range(256)]


def _qimage_to_pil(image) -> Image.Image:
    """QImage（RGBA8888）→ PIL Image。"""
    from PyQt5.QtGui import QImage

    image = image.convertToFormat(QImage.Format_RGBA8888)
    buffer = image.constBits()
    buffer.setsize(image.byteCount())
    return Image.frombytes("RGBA", (image.width(), image.height()), bytes(buffer))


def load_svg(
    path: str,
    min_side: int = SVG_MIN_SIDE,
    max_side: int = SVG_MAX_SIDE,
) -> Image.Image:
    """把 SVG 矢量图光栅化成透明底 RGBA 位图。

    按矢量图自身的宽高比，把长边放大/缩小到 ``min_side`` ~ ``max_side`` 之间，
    这样 24×24 的小图标导成 ICO 也不会糊。
    """
    from PyQt5.QtCore import QByteArray, QRectF, Qt
    from PyQt5.QtGui import QImage, QPainter
    from PyQt5.QtSvg import QSvgRenderer

    with open(path, "rb") as fh:
        data = fh.read()

    renderer = QSvgRenderer(QByteArray(data))
    if not renderer.isValid():
        raise ValueError("无法解析 SVG 文件（内容可能损坏或不是标准 SVG）")

    default = renderer.defaultSize()
    width = default.width() if default.width() > 0 else min_side
    height = default.height() if default.height() > 0 else min_side
    long_side = max(width, height)

    target = min(max(long_side, min_side), max_side)
    scale = target / long_side
    out_w = max(1, int(round(width * scale)))
    out_h = max(1, int(round(height * scale)))

    qimage = QImage(out_w, out_h, QImage.Format_RGBA8888)
    qimage.fill(Qt.transparent)
    painter = QPainter(qimage)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    # 显式给渲染区域：有些 SVG 没有写死 width/height，只靠 viewBox 撑
    renderer.render(painter, QRectF(0, 0, out_w, out_h))
    painter.end()
    return _qimage_to_pil(qimage)


def load_image(path: str) -> Image.Image:
    """读取图片，纠正 EXIF 方向，统一转 RGBA。SVG 会先光栅化。"""
    if os.path.splitext(path)[1].lower() in SVG_EXTS:
        return load_svg(path)
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode != "RGBA":
            im = im.convert("RGBA")
        return im.copy()


# 递归扫描时直接跳过的目录名（虚拟环境、缓存、依赖等，避免扫出无关图片）
SKIP_DIR_NAMES = {
    ".venv",
    "venv",
    "env",
    "__pycache__",
    "node_modules",
    ".git",
    ".idea",
    ".vscode",
    "site-packages",
}


def collect_images(paths: Iterable[str], recursive: bool = True) -> List[str]:
    """把文件/文件夹混合的输入整理成图片绝对路径列表（去重、保持顺序）。"""
    result: List[str] = []
    seen = set()

    def _add(p: str) -> None:
        ap = os.path.abspath(p)
        if ap not in seen and is_supported(ap):
            seen.add(ap)
            result.append(ap)

    def _walk(root_dir: str) -> None:
        for root, dirs, files in os.walk(root_dir):
            # 原地过滤，os.walk 便不会进入这些目录
            dirs[:] = [
                d
                for d in dirs
                if d not in SKIP_DIR_NAMES and not d.startswith(".")
            ]
            for name in files:
                _add(os.path.join(root, name))

    for raw in paths:
        if not raw:
            continue
        p = os.path.abspath(raw)
        if os.path.isdir(p):
            if recursive:
                _walk(p)
            else:
                for name in os.listdir(p):
                    _add(os.path.join(p, name))
        else:
            _add(p)
    return result


def list_images_in(directory: str) -> List[str]:
    """列出目录下（不递归）所有受支持格式的图片，按文件名排序。

    用于「我的素材库」页面：直接平铺展示，不扫描子目录，避免误列出临时 / 测试产物。
    """
    if not directory or not os.path.isdir(directory):
        return []
    files = []
    for name in os.listdir(directory):
        full = os.path.join(directory, name)
        if os.path.isfile(full) and is_supported(full):
            files.append(full)
    files.sort(key=lambda p: os.path.basename(p).lower())
    return files


try:  # Pillow >= 9.1 把重采样常量挪进了 Image.Resampling
    _BICUBIC = Image.Resampling.BICUBIC
    _LANCZOS = Image.Resampling.LANCZOS
except AttributeError:  # pragma: no cover - 兼容老版本 Pillow
    _BICUBIC = Image.BICUBIC
    _LANCZOS = Image.LANCZOS


def content_bbox(image: Image.Image) -> Optional[Tuple[int, int, int, int]]:
    """主体（非透明像素）的外接矩形；整张全透明时返回 ``None``。"""
    if image.mode != "RGBA":
        image = image.convert("RGBA")
    return image.getchannel("A").point(_TRIM_LUT).getbbox()


def trim_to_content(image: Image.Image, margin: int = 0) -> Image.Image:
    """「按主体裁剪」：把主体四周的透明空白裁掉，只保留主体（外扩 margin 像素）。

    这就是处理流程里那一步裁剪的独立版本 —— 判据从 rembg 的 mask 换成了
    成品图自己的 alpha 通道，而 alpha 本来就是那份 mask 合成出来的。

    没有任何空白可裁时**原样返回同一个对象**，调用方可以用 ``is`` 判断有没有变化。
    """
    if image.mode != "RGBA":
        image = image.convert("RGBA")
    bbox = content_bbox(image)
    if bbox is None:  # 整张全透明，没有主体可留
        return image

    margin = max(0, int(margin))
    box = (
        max(0, bbox[0] - margin),
        max(0, bbox[1] - margin),
        min(image.width, bbox[2] + margin),
        min(image.height, bbox[3] + margin),
    )
    if box == (0, 0, image.width, image.height):
        return image
    return image.crop(box)


def rotate_image(
    image: Image.Image,
    angle: int,
    trim: bool = True,
    margin: int = 0,
) -> Image.Image:
    """按**顺时针** ``angle`` 度旋转，画布自动放大，多出来的地方补透明。

    90 的整数倍走 PIL 的快速路径，不会有重采样损失；其它角度用双三次插值。

    ``expand`` 会把画布撑到旋转后矩形的外接矩形，任意非 90° 角度都会在四角
    多出透明三角，所以默认接着跑一次 :func:`trim_to_content` 把空白清掉
    （``trim=False`` 可以拿到未裁的原始旋转结果）。
    角度为 0 时原样返回（不复制、也不裁）。
    """
    angle = int(angle) % 360
    if angle == 0:
        return image
    if image.mode != "RGBA":
        image = image.convert("RGBA")
    # PIL 的 rotate 正角是逆时针，而界面上约定「正数 = 顺时针」，所以取负
    rotated = image.rotate(
        -angle, resample=_BICUBIC, expand=True, fillcolor=(0, 0, 0, 0)
    )
    return trim_to_content(rotated, margin) if trim else rotated


def _premultiply(rgba: Image.Image):
    """把 RGB 乘上 alpha（预乘），返回 ``(rgb, alpha)``。

    直接对 RGBA 逐通道缩放会把主体边缘和旁边透明区域的 RGB（这里是 0）平均到一起，
    半透明像素的 RGB 被拉黑，缩小后就出现一圈黑边。预乘之后透明区域不参与平均，
    缩完再反预乘回来，边缘颜色就干净了。
    """
    arr = np.asarray(rgba, dtype=np.float32)
    alpha = arr[:, :, 3:4] / 255.0
    rgb = np.clip(arr[:, :, :3] * alpha, 0, 255).astype(np.uint8)
    return (
        Image.fromarray(rgb, "RGB"),
        Image.fromarray(arr[:, :, 3].astype(np.uint8), "L"),
    )


def _unpremultiply(rgb: Image.Image, alpha: Image.Image) -> Image.Image:
    """预乘的 RGB 除以 alpha 还原；alpha=0 的像素按 1 处理（它们本来就不可见）。"""
    rgb_arr = np.asarray(rgb, dtype=np.float32)
    alpha_arr = np.asarray(alpha, dtype=np.float32)[:, :, None]
    restored = np.clip(rgb_arr * 255.0 / np.maximum(alpha_arr, 1.0), 0, 255)
    out = np.concatenate([restored, alpha_arr], axis=2)
    return Image.fromarray(out.astype(np.uint8), "RGBA")


def resize_to_max(image: Image.Image, max_side: int = MAX_SIDE) -> Image.Image:
    """长边超过 ``max_side`` 就等比缩下来（只缩不放，长宽比不变）。

    用预乘 alpha 缩放，透明底素材不会出现黑边 / 彩边。
    尺寸本来就够小、或者 ``max_side <= 0`` 时原样返回同一个对象。
    """
    width, height = image.size
    longest = max(width, height)
    if max_side <= 0 or longest <= max_side:
        return image

    scale = max_side / longest
    new_size = (max(1, round(width * scale)), max(1, round(height * scale)))

    rgba = image if image.mode == "RGBA" else image.convert("RGBA")
    rgb, alpha = _premultiply(rgba)
    rgb = rgb.resize(new_size, _LANCZOS)
    alpha = alpha.resize(new_size, _LANCZOS)
    return _unpremultiply(rgb, alpha)


def save_webp_lossless(image: Image.Image, path: str, method: int = 6) -> str:
    """保存为**无损** WebP（保留 alpha 与软边）。

    ``method`` 0~6 是压缩投入，6 最慢但最小；素材图都在 800px 以内，耗时可忽略。
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    canvas = image if image.mode == "RGBA" else image.convert("RGBA")
    canvas.save(path, format="WEBP", lossless=True, method=method)
    return path


def save_image(image: Image.Image, path: str, quality: int = 95) -> str:
    """按扩展名把图片写回磁盘（旋转后覆盖保存用）。

    PNG / WebP 保留透明；JPEG 没有透明通道，会先垫一层白底。
    """
    ext = os.path.splitext(path)[1].lower()
    os.makedirs(os.path.dirname(path), exist_ok=True)

    if ext == ".png":
        return save_png(image, path)

    if ext in (".jpg", ".jpeg"):
        if image.mode in ("RGBA", "LA", "P"):
            rgba = image.convert("RGBA")
            canvas = Image.new("RGB", rgba.size, (255, 255, 255))
            canvas.paste(rgba, mask=rgba.split()[3])
            canvas.save(path, format="JPEG", quality=quality, subsampling=0)
        else:
            image.convert("RGB").save(
                path, format="JPEG", quality=quality, subsampling=0
            )
        return path

    if ext == ".webp":
        # 素材库统一是无损 WebP，重写时也保持无损，别悄悄降质
        return save_webp_lossless(image, path)

    raise ValueError(f"这个格式不支持旋转后覆盖保存：{ext or path}")


def save_png(image: Image.Image, path: str) -> str:
    """保存为透明底 PNG。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if image.mode != "RGBA":
        image = image.convert("RGBA")
    image.save(path, format="PNG", optimize=True)
    return path


def save_ico(image: Image.Image, path: str, sizes: Iterable[int]) -> str:
    """保存为 ICO。

    Pillow 的 ICO 只接受正方形，内部会按 sizes 逐级缩放出多尺寸图标。
    传入的 image 需要是按最大边长准备好的正方形 RGBA。
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    size_list = sorted({int(s) for s in sizes if 0 < int(s) <= 256}, reverse=True)
    if not size_list:
        size_list = [256]

    canvas = image
    if canvas.mode != "RGBA":
        canvas = canvas.convert("RGBA")

    target = size_list[0]
    if canvas.size != (target, target):
        canvas = canvas.resize((target, target), Image.LANCZOS)

    canvas.save(path, format="ICO", sizes=[(s, s) for s in size_list])
    return path
