# -*- coding: utf-8 -*-
"""单张图片的完整处理流程：hint（可选）→ 抠图 → 应用 hint → 只留最大主体 → 按主体裁剪 → 保留透明底。

hint 语义：
  - keep_box（一个或多个）：把 box 取并集当作「主体限定框」。
    并集外的像素 alpha **全部清零**（这部分我不要）。
    并集内的像素按 mask 软值保留（不要涂白，模型给的边缘软值继续生效）。
  - drop_box（一个或多个）：每个 box 内的像素 alpha **全部清零**（明确剔除）。
"""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np
from PIL import Image

from . import remover
from .image_io import ALPHA_THRESHOLD, load_image


@dataclass
class Settings:
    """处理参数（界面上可调）。"""

    model: str = remover.DEFAULT_MODEL
    margin: int = 2  # 裁剪时额外保留的透明边距（像素）
    largest_only: bool = True  # 一张图有多个主体时只保留最大的
    decontaminate: bool = False  # 边缘去色晕（更慢）
    post_process: bool = True  # rembg 自带 mask 后处理（去小噪点 / 小孔），默认开
    alpha_threshold: int = ALPHA_THRESHOLD

    # 多个 hint 框（输入图坐标系）。空列表表示未框选。
    keep_boxes: List[Tuple[int, int, int, int]] = field(default_factory=list)
    drop_boxes: List[Tuple[int, int, int, int]] = field(default_factory=list)
    # 是否以上一次处理后的结果图作为输入（二次迭代）
    use_after_input: bool = False


@dataclass
class Result:
    """单张图片的处理结果。"""

    src: str
    ok: bool
    message: str = ""
    before: Optional[Image.Image] = None  # 输入图裁剪区（处理前）
    after: Optional[Image.Image] = None  # 去背景后（与 before 同一裁剪区）
    crop_box: Tuple[int, int, int, int] = (0, 0, 0, 0)
    # 手动框选的 ICO 正方形区域（after 图坐标系）
    square: Optional[Tuple[int, int, int, int]] = None
    ico_mode: str = "center"  # center / manual
    # 本次处理实际使用的 hint（输入图坐标系）
    keep_boxes: List[Tuple[int, int, int, int]] = field(default_factory=list)
    drop_boxes: List[Tuple[int, int, int, int]] = field(default_factory=list)
    # 本次是否以上一次处理后的结果图作为输入
    used_after_input: bool = False

    @property
    def size_text(self) -> str:
        if not self.ok or self.after is None:
            return ""
        return f"{self.after.width} × {self.after.height}"


def _keep_largest(binary: np.ndarray) -> np.ndarray:
    """只保留面积最大的连通区域。"""
    from scipy import ndimage

    labels, count = ndimage.label(binary)
    if count <= 1:
        return binary

    sizes = np.bincount(labels.ravel())
    sizes[0] = 0  # 背景标签不计
    return labels == int(sizes.argmax())


def _clip_box(box: Tuple[int, int, int, int], w: int, h: int) -> Optional[Tuple[int, int, int, int]]:
    """把 box 裁剪到图片范围；若退化到无效就返回 None。"""
    x0, y0, x1, y1 = box
    x0 = max(0, min(int(x0), w))
    y0 = max(0, min(int(y0), h))
    x1 = max(0, min(int(x1), w))
    y1 = max(0, min(int(y1), h))
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1, y1)


def _apply_hints(
    mask_np: np.ndarray,
    keep_boxes: List[Tuple[int, int, int, int]],
    drop_boxes: List[Tuple[int, int, int, int]],
) -> np.ndarray:
    """按 hint 修正软蒙版。

    drop_box：box 内 alpha 清零（明确剔除）。
    keep_box：取并集作为「主体限定框」，并集**外**alpha 清零；并集内**不动**，
             这样模型给的边缘软值仍能保留透明过渡，不会有硬边。
    顺序：先 drop 再 keep（drop 的优先级更高，避免被 keep 的并集覆盖）。
    """
    out = mask_np.copy()
    h, w = out.shape

    # 1. drop：box 内清零
    for box in drop_boxes or []:
        clipped = _clip_box(box, w, h)
        if clipped is None:
            continue
        x0, y0, x1, y1 = clipped
        out[y0:y1, x0:x1] = 0

    # 2. keep：并集外清零
    if keep_boxes:
        inside = np.zeros((h, w), dtype=bool)
        any_valid = False
        for box in keep_boxes:
            clipped = _clip_box(box, w, h)
            if clipped is None:
                continue
            x0, y0, x1, y1 = clipped
            inside[y0:y1, x0:x1] = True
            any_valid = True
        if any_valid:
            out[~inside] = 0
        else:
            # 所有 keep 框都不在图片内 —— 视为「整张图我都要」
            # 这种情况完全没意义（拿不到任何主体），所以这里等同 drop：全清零
            out[:, :] = 0

    return out


def process_image(
    src: str,
    settings: Settings,
    session=None,
    image: Optional[Image.Image] = None,
) -> Result:
    """处理单张图片。任何异常都转成 ok=False 的结果，不中断批量任务。

    传入 ``image`` 时直接以其为输入图（用于二次迭代：把上次结果再处理）；
    未传入时按 ``src`` 路径读取原图。
    """
    if image is None:
        try:
            image = load_image(src)
        except Exception as exc:  # noqa: BLE001
            return Result(src, False, f"读取失败：{exc}")

    try:
        mask = remover.predict_mask(
            image,
            settings.model,
            session=session,
            post_process=settings.post_process,
        )
    except remover.ModelNotReady as exc:
        return Result(src, False, str(exc))
    except Exception as exc:  # noqa: BLE001
        return Result(src, False, f"抠图失败：{exc}")

    mask_np = np.asarray(mask.convert("L"), dtype=np.uint8)

    # 应用用户给的 hint（在裁剪之前、原图坐标系）
    mask_np = _apply_hints(mask_np, settings.keep_boxes, settings.drop_boxes)

    binary = mask_np >= max(1, int(settings.alpha_threshold))

    if settings.largest_only:
        binary = _keep_largest(binary)

    if not binary.any():
        return Result(
            src,
            False,
            "未检测到主体（处理后整张图都被判定为背景）"
            + (
                "——可能 keep 框把主体都框掉了，或者 drop 框覆盖了全部主体"
                if (settings.keep_boxes or settings.drop_boxes) else ""
            ),
        )

    ys, xs = np.where(binary)
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    y0, y1 = int(ys.min()), int(ys.max()) + 1

    margin = max(0, int(settings.margin))
    x0 = max(0, x0 - margin)
    y0 = max(0, y0 - margin)
    x1 = min(image.width, x1 + margin)
    y1 = min(image.height, y1 + margin)
    box = (x0, y0, x1, y1)

    # 用（可能已筛掉其他主体的）二值结果去限制软蒙版
    alpha_full = Image.fromarray(mask_np, "L")
    if settings.largest_only:
        keep = Image.fromarray((binary * 255).astype("uint8"), "L")
        alpha_full = Image.composite(alpha_full, Image.new("L", mask.size, 0), keep)

    before = image.crop(box).convert("RGBA")
    alpha_crop = alpha_full.crop(box)

    try:
        if settings.decontaminate:
            after = remover.decontaminate(before.convert("RGB"), alpha_crop)
        else:
            after = Image.composite(
                before, Image.new("RGBA", alpha_crop.size, (0, 0, 0, 0)), alpha_crop
            )
    except Exception as exc:  # noqa: BLE001
        return Result(src, False, f"合成透明底失败：{exc}", before=before, crop_box=box)

    return Result(src, True, "", before=before, after=after, crop_box=box)