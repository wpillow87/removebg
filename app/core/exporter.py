# -*- coding: utf-8 -*-
"""导出：PNG（透明底）与 ICO（正方形图标）。"""

import os
from typing import Dict, Iterable, List, Sequence, Tuple

from PIL import Image

from .image_io import (
    MAX_SIDE,
    resize_to_max,
    save_ico,
    save_png,
    save_webp_lossless,
)
from .processor import Result

# ICO 常用的正方形尺寸
ICO_SIZE_CHOICES: List[int] = [16, 32, 48, 64, 128, 256]
DEFAULT_ICO_SIZES: List[int] = [256]


def _unique_path(directory: str, stem: str, ext: str, taken: set) -> str:
    """文件名沿用原文件名；已存在时自动加序号，绝不覆盖。"""
    candidate = os.path.join(directory, f"{stem}{ext}")
    index = 1
    while os.path.exists(candidate) or candidate.lower() in taken:
        candidate = os.path.join(directory, f"{stem}_{index}{ext}")
        index += 1
    taken.add(candidate.lower())
    return candidate


def square_box_for(result: Result, image: Image.Image) -> Tuple[int, int, int, int]:
    """计算 ICO 用的正方形区域（image 坐标系）。"""
    width, height = image.size

    if result.ico_mode == "manual" and result.square:
        x0, y0, x1, y1 = (int(v) for v in result.square)
        x0 = max(0, min(x0, width - 1))
        y0 = max(0, min(y0, height - 1))
        x1 = max(x0 + 1, min(x1, width))
        y1 = max(y0 + 1, min(y1, height))
        return (x0, y0, x1, y1)

    # 主体居中：取能包住整张（已裁剪到主体）图的最小正方形，居中放置
    side = max(width, height)
    left = (width - side) // 2
    top = (height - side) // 2
    return (left, top, left + side, top + side)


def extract_square(image: Image.Image, box: Tuple[int, int, int, int]) -> Image.Image:
    """按给定区域截正方形，越界部分用透明补足（保持长宽比不变形）。"""
    x0, y0, x1, y1 = box
    width, height = x1 - x0, y1 - y0
    side = max(width, height, 1)

    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    offset = ((side - width) // 2 - x0, (side - height) // 2 - y0)
    canvas.paste(image.convert("RGBA"), offset)
    return canvas


def export_webp(
    results: Iterable[Result],
    out_dir: str,
    max_side: int = MAX_SIDE,
) -> Dict[str, object]:
    """把处理结果写进素材库：长边压到 ``max_side`` 以内 + 无损 WebP。

    返回 {"webp": [...], "errors": [(文件名, 原因)]}
    """
    os.makedirs(out_dir, exist_ok=True)

    report: Dict[str, object] = {"webp": [], "errors": []}
    taken: set = set()

    for result in results:
        if not result.ok or result.after is None:
            report["errors"].append(  # type: ignore[union-attr]
                (os.path.basename(result.src), result.message or "处理失败")
            )
            continue

        stem = os.path.splitext(os.path.basename(result.src))[0]
        try:
            small = resize_to_max(result.after, max_side)
            path = _unique_path(out_dir, stem, ".webp", taken)
            save_webp_lossless(small, path)
            report["webp"].append(path)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            report["errors"].append((f"{stem}.webp", str(exc)))  # type: ignore[union-attr]

    return report


def export_results(
    results: Iterable[Result],
    out_dir: str,
    want_png: bool,
    want_ico: bool,
    ico_sizes: Sequence[int],
) -> Dict[str, object]:
    """把处理结果写入输出目录。

    返回 {"png": [...], "ico": [...], "errors": [(文件名, 原因)]}
    """
    os.makedirs(out_dir, exist_ok=True)

    report: Dict[str, object] = {"png": [], "ico": [], "errors": []}
    taken: set = set()

    for result in results:
        if not result.ok or result.after is None:
            report["errors"].append(  # type: ignore[union-attr]
                (os.path.basename(result.src), result.message or "处理失败")
            )
            continue

        stem = os.path.splitext(os.path.basename(result.src))[0]

        if want_png:
            try:
                path = _unique_path(out_dir, stem, ".png", taken)
                save_png(result.after, path)
                report["png"].append(path)  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001
                report["errors"].append((f"{stem}.png", str(exc)))  # type: ignore[union-attr]

        if want_ico:
            try:
                box = square_box_for(result, result.after)
                square = extract_square(result.after, box)
                path = _unique_path(out_dir, stem, ".ico", taken)
                save_ico(square, path, ico_sizes or DEFAULT_ICO_SIZES)
                report["ico"].append(path)  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001
                report["errors"].append((f"{stem}.ico", str(exc)))  # type: ignore[union-attr]

    return report
