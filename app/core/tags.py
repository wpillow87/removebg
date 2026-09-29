# -*- coding: utf-8 -*-
"""文件名标签：``名称[标签1][标签2].扩展名``

标签直接写在文件名里，好处是换电脑 / 拷贝整个文件夹都不会丢，
不需要额外的数据库文件。

约定：
  - 标签一律写在主文件名的**末尾**，一个标签一对中括号；
  - 解析时同时兼容半角 ``[ ]`` 与全角 ``【 】``，写入时统一用半角；
  - ``led灯[电子设备][我的物品].png`` → 主名 ``led灯``，标签 ``电子设备``、``我的物品``。

展示时标签不会混在文件名里显示，而是单独作为「分组」展示。
"""

import json
import os
import re
from typing import Iterable, List, Sequence, Tuple

from .. import paths

# 结尾处的一对标签括号（半角 / 全角都认）
_TRAILING_TAG = re.compile(r"\s*[\[【]([^\[\]【】]*)[\]】]\s*$")
# 标签里不允许出现的字符（括号本身 + Windows 文件名非法字符）
_FORBIDDEN_IN_TAG = set('[]【】/\\:*?"<>|')

# 没有预设文件时用的默认常用标签
DEFAULT_PRESET_TAGS: List[str] = [
    "我的物品",
    "电子设备",
    "家具",
    "人物",
    "风景",
    "食物",
    "图标",
    "待整理",
]

PRESET_PATH = os.path.join(paths.BASE_DIR, "tags.json")


# ------------------------------------------------------------------ 解析 / 拼装


def split_name(filename: str) -> Tuple[str, List[str], str]:
    """把文件名拆成 ``(主名, [标签...], 扩展名)``。

    ``os.path.basename`` 之后的纯文件名即可，扩展名原样保留（含点）。
    """
    stem, ext = os.path.splitext(filename)
    tags: List[str] = []
    while True:
        match = _TRAILING_TAG.search(stem)
        if not match:
            break
        tag = match.group(1).strip()
        if tag:
            tags.insert(0, tag)  # 从后往前剥，插到最前面保持原顺序
        stem = stem[: match.start()]
    return stem.strip(), tags, ext


def compose_name(base: str, tags: Sequence[str], ext: str = "") -> str:
    """``(主名, 标签, 扩展名)`` → 完整文件名。"""
    out = (base or "").strip()
    for tag in normalize_tags(tags):
        out += f"[{tag}]"
    return out + (ext or "")


def normalize_tags(tags: Iterable[str]) -> List[str]:
    """去空白、去重、保持先后顺序。"""
    result: List[str] = []
    seen = set()
    for raw in tags:
        tag = (raw or "").strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        result.append(tag)
    return result


def is_valid_tag(tag: str) -> Tuple[bool, str]:
    """检查标签是否可用，返回 ``(是否合法, 原因)``。"""
    tag = (tag or "").strip()
    if not tag:
        return False, "标签不能为空。"
    bad = sorted(ch for ch in tag if ch in _FORBIDDEN_IN_TAG)
    if bad:
        return False, f"标签里不能出现这些字符：{' '.join(bad)}"
    if len(tag) > 24:
        return False, "标签太长了（最多 24 个字）。"
    return True, ""


def _unique_name(filename: str, directory: str, taken: set) -> str:
    """名字被占用时自动加序号。

    序号加在**主名**后面、标签前面，这样 ``led灯[电子设备].png`` 撞名会变成
    ``led灯_1[电子设备].png`` —— 标签始终待在文件名末尾，解析规则不会被破坏。
    """
    if filename.lower() not in taken and not os.path.exists(
        os.path.join(directory, filename)
    ):
        return filename

    base, tags, ext = split_name(filename)
    index = 1
    while True:
        candidate = compose_name(f"{base}_{index}", tags, ext)
        if candidate.lower() not in taken and not os.path.exists(
            os.path.join(directory, candidate)
        ):
            return candidate
        index += 1


def plan_renames(desired: Sequence[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """把「想要的文件名」整理成一组互不冲突的最终名字。

    ``desired`` 是 ``[(老路径, 想要的文件名), ...]``，返回 ``[(老路径, 最终文件名), ...]``，
    只包含真的需要改名的项。重名会自动加 ``_1`` / ``_2`` 序号：
      · 磁盘上已经有同名文件 → 加序号；
      · 本批里两个文件想改成同一个名字 → 后一个加序号。

    这样调用方拿到的名字保证互不冲突、也不与磁盘上现有的文件冲突。
    """
    moving = [(path, name) for path, name in desired if name != os.path.basename(path)]
    if not moving:
        return []

    moving_paths = {path.lower() for path, _name in moving}
    taken: set = set()
    for directory in {os.path.dirname(path) for path, _name in moving}:
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            # 不会挪窝的名字先占住，免得被别人抢了
            if os.path.join(directory, name).lower() not in moving_paths:
                taken.add(name.lower())

    result: List[Tuple[str, str]] = []
    for path, wanted in moving:
        final = _unique_name(wanted, os.path.dirname(path), taken)
        taken.add(final.lower())
        result.append((path, final))
    return result


def is_valid_base(base: str) -> Tuple[bool, str]:
    """检查主文件名（不含扩展名）是否可用。"""
    base = (base or "").strip()
    if not base:
        return False, "文件名不能为空。"
    if any(ch in _FORBIDDEN_IN_TAG - {"[", "]", "【", "】"} for ch in base):
        return False, '文件名里不能出现 \\ / : * ? " < > | 这些字符。'
    if "[" in base or "]" in base or "【" in base or "】" in base:
        return False, "文件名里不要直接写中括号，标签请在下面点击选择。"
    return True, ""


# ------------------------------------------------------------------ 常用标签


def load_presets() -> List[str]:
    """读取常用标签；没有配置文件时返回默认值。"""
    try:
        with open(PRESET_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            tags = normalize_tags(str(x) for x in data)
            if tags:
                return tags
    except Exception:  # noqa: BLE001 - 配置文件损坏时静默降级
        pass
    return list(DEFAULT_PRESET_TAGS)


def save_presets(tags: Iterable[str]) -> None:
    try:
        with open(PRESET_PATH, "w", encoding="utf-8") as fh:
            json.dump(normalize_tags(tags), fh, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass


def add_presets(tags: Iterable[str]) -> List[str]:
    """把若干标签并入常用标签并落盘，返回最新的完整列表。"""
    merged = normalize_tags(list(load_presets()) + list(tags))
    save_presets(merged)
    return merged


def remove_preset(tag: str) -> List[str]:
    rest = [t for t in load_presets() if t != tag]
    save_presets(rest)
    return rest
