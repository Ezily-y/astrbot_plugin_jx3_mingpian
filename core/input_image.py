"""从附图或引用消息读取一张参考图，使用 AstrBot 的媒体解析接口。"""

import asyncio
import base64
from io import BytesIO

from astrbot.api import message_components as Comp
from PIL import Image

from .card import _art_suffix

MAX_IMAGE_BYTES = 20 * 1024 * 1024


async def read_image(event) -> bytes:
    chain = event.get_messages()
    images = [part for part in chain if isinstance(part, Comp.Image)]
    if not images:
        images = [
            part
            for reply in chain
            if isinstance(reply, Comp.Reply)
            for part in (reply.chain or [])
            if isinstance(part, Comp.Image)
        ]
    if not images:
        raise ValueError(
            "请在同条消息附一张图片，或引用含图片的消息；若平台未提供引用图片，请重新附图发送。"
        )
    if len(images) != 1:
        raise ValueError("一次只能使用一张参考图片，请单独发送或引用所需图片。")
    try:
        # 该接口自动清理媒体解析器下载/解码的临时文件，保留本地源图。
        encoded = await asyncio.wait_for(images[0].convert_to_base64(), timeout=30)
        if len(encoded) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
            raise ValueError("参考图片过大，请压缩到 20 MB 以内。")
        blob = base64.b64decode(encoded, validate=True)
        if len(blob) > MAX_IMAGE_BYTES:
            raise ValueError("参考图片过大，请压缩到 20 MB 以内。")
        if not _art_suffix(blob):
            raise ValueError("请提供 PNG、JPEG、WebP 或 GIF 格式的图片。")
        with Image.open(BytesIO(blob)) as image:
            image.verify()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("图片读取失败或图片已损坏，请重新附图发送。") from exc
    return blob
