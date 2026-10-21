"""提示词清单：读取配置中的名片特写提示词，提供序号、清单文本与等待提示。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .sizes import UNSPECIFIED

DEFAULT_TASK_HINT = "正在努力生成图片中，请耐心等待几分钟..."
DEFAULT_PROMPT_NAME = "提示词"
MENU_TITLE = "名片特写"


@lru_cache(maxsize=1)
def default_prompts() -> list[dict]:
    """复用配置页的内置模板，避免维护两套提示词。"""
    schema = Path(__file__).resolve().parents[1] / "_conf_schema.json"
    return json.loads(schema.read_text(encoding="utf-8"))["mingpian_prompts"]["default"]


@dataclass(frozen=True)
class Prompt:
    """一条提示词。index 从 1 起，与清单展示的序号一致。

    ratio 为该提示词适用的宽高比，取「不指定」时出图尺寸交给接口自己决定。
    """

    index: int
    name: str
    text: str
    ratio: str = UNSPECIFIED


def load_prompts(raw: object) -> list[Prompt]:
    """把配置中的提示词列表规整为带序号的清单。

    跳过内容为空的条目；序号按当前顺序连续生成，不留空号。
    """
    items = raw if isinstance(raw, list) else []
    prompts: list[Prompt] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("prompt") or "").strip()
        if not text:
            continue
        name = (
            str(item.get("name") or "").strip()
            or f"{DEFAULT_PROMPT_NAME}{len(prompts) + 1}"
        )
        ratio = str(item.get("ratio") or "").strip() or UNSPECIFIED
        prompts.append(
            Prompt(index=len(prompts) + 1, name=name, text=text, ratio=ratio)
        )
    return prompts


def empty_prompt_names(raw: object) -> list[str]:
    """列出内容为空的条目名称，帮助里单独显示，避免“加了却看不到”。"""
    items = raw if isinstance(raw, list) else []
    names: list[str] = []
    for position, item in enumerate(items, 1):
        if not isinstance(item, dict):
            continue
        if str(item.get("prompt") or "").strip():
            continue
        name = str(item.get("name") or "").strip()
        names.append(name or f"未命名条目（第 {position} 条）")
    return names


def menu_text(
    prompts: list[Prompt],
    *,
    title: str = MENU_TITLE,
    skipped: list[str] | None = None,
) -> str:
    """展示当前配置的提示词；不创建等待会话，不自动生成。"""
    lines = "\n".join(f"{prompt.index}. {prompt.name}" for prompt in prompts)
    text = f"{title}可选提示词：\n{lines or '未配置可用提示词'}"
    if skipped:
        text += "\n\n内容为空、未启用的条目：\n" + "\n".join(
            f"- {name}" for name in skipped
        )
        text += "\n填写提示词内容并保存后即可选择。"
    return (
        text + "\n\n使用 /生图 或 @机器人 生图 特写 名称或编号，需附图或引用图片。"
    )


def select_prompt(prompts: list[Prompt], selector: str) -> Prompt:
    selector = selector.strip()
    if selector.isdecimal():
        index = int(selector)
        if 1 <= index <= len(prompts):
            return prompts[index - 1]
    else:
        matches = [prompt for prompt in prompts if prompt.name == selector]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError("提示词名称重复，请使用编号选择。\n" + menu_text(prompts))
    raise ValueError("请指定有效的提示词名称或编号。\n" + menu_text(prompts))


def task_hint(value: object) -> str:
    """生图等待提示。配置为空时使用默认文案。"""
    text = str(value or "").strip()
    return text or DEFAULT_TASK_HINT
