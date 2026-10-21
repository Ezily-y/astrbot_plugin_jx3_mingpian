"""人物图鉴：立绘提示词、图鉴字段清单、LLM 提示词与返回解析。

图鉴的角色名由用户选填，未填时留空；其余字段由多模态 LLM 依据立绘生成。
立绘以用户发送或引用的图片为参考图生成，不查询游戏角色信息。
"""

from __future__ import annotations

import base64
import json
import re

from .sizes import RATIO_SIZES


class TujianError(RuntimeError):
    """图鉴生成失败。message 为可直接返回给用户的文案。"""


# 立绘尺寸。图鉴主视觉是横向的半身像，与模板顶部区域同比例
PORTRAIT_SIZE = RATIO_SIZES["4:3"]

# 立绘提示词：以名片形象图为参考图，重绘一张图鉴用的立绘
PORTRAIT_PROMPT = """Asset type: 剑网3（JX3）风格的角色立绘，用作人物图鉴的顶部主视觉
Primary request: 以 Image 1 中的角色为准，画这位角色的半身或四分之三身立绘，面向镜头，姿态自然
Input image: Image 1 为该角色的名片形象图，是身份与外观的唯一依据
人物约束（最重要，违反则整张作废）:
- 容貌、脸型、五官、发型、发色、发饰、耳饰、妆容、肤色必须与 Image 1 中的角色完全一致，是同一个角色
- 服装款式与颜色必须与 Image 1 完全一致，不要换装、不要改衣色、不要摘换配饰、不要增减身上的装饰
- 原图人物身上或手中的物件保持原样
Style/medium: 国风仙侠高质感 3D 游戏 CG，柔和电影光与克制的轮廓光，皮肤细腻、发丝分明、织物与金属珠宝材质清晰
Composition/framing: 角色居中，头部完整入画且头顶留出空间，腰部以下可裁出画外
Background: 与该角色气质呼应的虚化场景，冷调柔光，可加光斑、花瓣、雪尘、星点等氛围粒子，背景整体压暗以突出人物
Constraints:
- 画面中绝对不能出现任何文字、汉字、数字、书法、印章、logo、水印、条码
- 不要画边框、面板、条码或任何 UI 元素
- 只出现一个人物，不要重复人物或拼贴多张脸
"""

# 图鉴字段：(键名, 中文标签)。顺序即模板中的展示顺序
FIELDS: tuple[tuple[str, str], ...] = (
    ("alias", "别称"),
    ("race", "种族"),
    ("age", "年龄"),
    ("height", "身高"),
    ("identity", "身份"),
    ("camp", "阵营"),
    ("temperament", "气质"),
    ("likes", "喜好"),
    ("weapon", "武器"),
)

# 列表型字段：(键名, 中文标签, 条数)
LIST_FIELDS: tuple[tuple[str, str, int], ...] = (
    ("pros", "优点", 3),
    ("cons", "不足", 3),
    ("points", "要点总结", 4),
)

SYSTEM_PROMPT = (
    "你是武侠角色设定集的撰稿人。你会收到一张角色立绘和可选的角色名，"
    "据此写出该角色的设定文案。只输出一个 JSON 对象，不要任何解释、不要代码块围栏。"
)


def build_prompt(name: str) -> str:
    """组装给多模态 LLM 的提示词：角色名 + 字段清单 + 输出格式。"""
    identity_hint = (
        "角色名是唯一给定的真实信息，其余全部由你依据立绘创作。"
        if name
        else "角色名未提供，姓名保持留空，不要自行起名；其余设定由你依据立绘创作。"
    )
    fields = "\n".join(f'  "{key}": "{label}，不超过 12 字"' for key, label in FIELDS)
    lists = "\n".join(
        f'  "{key}": ["{label}，{count} 条，每条不超过 20 字"]'
        for key, label, count in LIST_FIELDS
    )
    return (
        f"角色名：{name}\n\n"
        "请观察这张立绘，判断角色的门派气质、发色瞳色、服饰材质与配饰细节，"
        f"据此写出这个角色的设定。{identity_hint}\n\n"
        "只输出如下 JSON，不要任何其他文字：\n"
        "{\n"
        '  "quote": "一句引语，不超过 30 字",\n'
        f"{fields},\n"
        f"{lists}\n"
        "}"
    )


def _extract_json(raw: str) -> dict | None:
    """从模型返回里取出 JSON 对象，容忍代码块围栏与前后废话。"""
    text = str(raw or "").strip()
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _text(value: object) -> str:
    """标量字段一律转成单行字符串，空值返回空串。"""
    if value is None or isinstance(value, (dict, list)):
        return ""
    return " ".join(str(value).split())


def _items(value: object, count: int) -> list[str]:
    """列表字段：只取非空项，最多 count 条。"""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    items = [text for item in value if (text := _text(item))]
    return items[:count]


def parse_profile(raw: str) -> dict:
    """把 LLM 返回规整为模板要的形状。

    字段缺失或格式不合时填空值，由模板跳过空模块，不因此中断出图。
    """
    data = _extract_json(raw) or {}
    profile: dict = {"quote": _text(data.get("quote"))}
    for key, _label in FIELDS:
        profile[key] = _text(data.get(key))
    for key, _label, count in LIST_FIELDS:
        profile[key] = _items(data.get(key), count)
    return profile


def is_empty(profile: dict) -> bool:
    """是否一个字段都没解析出来，用于判断模型是否根本没按格式返回。"""
    return not any(profile.get(key) for key, _ in FIELDS) and not profile.get("quote")


def payload(name: str, profile: dict, portrait: bytes) -> dict:
    """组装模板变量。字段名与 templates/standalone/tujian.html 一致。"""
    from .card import brush_font_uri, card_font_uri

    return {
        "cardFont": card_font_uri(),
        "brushFont": brush_font_uri(),
        "name": name,
        "portrait": "data:image/png;base64,"
        + base64.b64encode(portrait).decode("ascii"),
        "quote": profile.get("quote", ""),
        # 只把有值的字段交给模板，模板便不必逐字段判断
        "fields": [
            {"label": label, "value": profile[key]}
            for key, label in FIELDS
            if profile.get(key)
        ],
        "pros": profile.get("pros", []),
        "cons": profile.get("cons", []),
        "points": profile.get("points", []),
    }
