"""DashScope (通义万相/千问) 图片生成客户端。

支持阿里云百炼 DashScope 原生 API：
- wan2.7-image / wan2.7-image-pro（万相 2.7）
- wanx-v1 / wanx2.1-t2i-turbo / wanx2.1-t2i-plus（万相旧版）
- qwen-image-3.0（千问图像）

API 文档：
- https://help.aliyun.com/zh/model-studio/wan-image-generation-and-editing-api-reference
- https://www.qianwenai.com/models/qwen-image-3.0
"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import re
from pathlib import Path

import aiohttp
from astrbot.api import logger

from .image_api import ImageAPIError


class DashScopeImageClient:
    """DashScope 图片生成客户端。

    使用同步接口（multimodal-generation/generation），不使用异步任务。
    返回 bytes，和 ImageAPIClient 接口一致。
    """

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        model: str = "wan2.7-image",
        *,
        timeout: int = 300,
    ):
        self.base_url = self._normalize_url(base_url)
        self.api_key = api_key.strip()
        self.model = model.strip() or "wan2.7-image"
        self.timeout = timeout

    @staticmethod
    def _normalize_url(value: str) -> str:
        """规范化 DashScope API 地址。

        用户可填写：
        - 完整端点：https://xxx.cn-beijing.maas.aliyuncs.com/api/v1/services/aigc/...
        - 简化格式：https://dashscope.aliyuncs.com/api/v1
        - 最简格式：https://dashscope.aliyuncs.com
        """
        text = str(value or "").strip().rstrip("/")
        if not text:
            return ""
        if not text.startswith(("http://", "https://")):
            raise ValueError("DashScope 接口地址必须以 http:// 或 https:// 开头")
        # 完整端点，直接使用
        if "/services/aigc/" in text:
            return text
        # 确保以 /api/v1 结尾
        if not text.endswith("/api/v1"):
            if "/api/v1/" not in text:
                text = text + "/api/v1"
        return text + "/services/aigc/multimodal-generation/generation"

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    async def list_models(self) -> list[str]:
        """DashScope 没有公开的模型列表接口，返回已知模型名。"""
        return [
            "wan2.7-image",
            "wan2.7-image-pro",
            "wanx-v1",
            "wanx2.1-t2i-turbo",
            "wanx2.1-t2i-plus",
            "qwen-image-3.0",
        ]

    def safe_error(self, error: object) -> str:
        """脱敏错误信息，隐藏 API Key。"""
        message = str(error)
        if self.api_key:
            message = message.replace(self.api_key, "***")
        return message[:1000]

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def edit(
        self,
        prompt: str,
        images,
        *,
        size: str = "2K",
        quality: str = "high",
        retries: int = 1,
    ) -> bytes:
        """带参考图生图（图像编辑）。

        images: Path 列表（本地文件）或字符串列表（URL/data URI）
        返回图片 bytes。
        """
        if not self.configured:
            raise ImageAPIError(
                "DashScope 未配置，请填写接口地址、密钥和模型名"
            )

        # 构建 content 数组
        content = [{"text": prompt}]
        for img in images:
            if isinstance(img, str) and img.startswith(
                ("http://", "https://", "data:")
            ):
                content.append({"image": img})
            else:
                path = Path(img)
                if not path.exists():
                    raise ImageAPIError(f"参考图不存在：{path}")
                mime = mimetypes.guess_type(path.name)[0] or "image/png"
                data = path.read_bytes()
                b64 = base64.b64encode(data).decode("ascii")
                content.append({"image": f"data:{mime};base64,{b64}"})

        parameters: dict[str, object] = {"n": 1, "watermark": False}
        converted_size = self._convert_size(size)
        if converted_size is not None:
            parameters["size"] = converted_size

        payload = {
            "model": self.model,
            "input": {
                "messages": [
                    {"role": "user", "content": content},
                ]
            },
            "parameters": parameters,
        }

        # wan2.7 有参考图时关闭 thinking_mode（仅纯文生图支持）
        if "wan2.7" in self.model and len(list(images)) > 0:
            payload["parameters"]["thinking_mode"] = False

        last_error = ""
        for attempt in range(retries + 1):
            try:
                image_url = await self._post_for_url(payload)
                return await self._download(image_url)
            except ImageAPIError as exc:
                last_error = str(exc)
                # 配置类错误不重试
                if any(
                    m in last_error
                    for m in ("密钥无效", "InvalidApiKey", "Arrearage")
                ):
                    raise
                if attempt < retries:
                    logger.warning("DashScope 生图失败，重试：%s", last_error)
                    await asyncio.sleep(1.5)

        raise ImageAPIError(last_error or "DashScope 生图失败")

    async def _post_for_url(self, payload: dict) -> str:
        """发送请求，返回图片 URL。"""
        timeout = aiohttp.ClientTimeout(
            total=self.timeout, sock_read=self.timeout
        )
        try:
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.post(
                    self.base_url,
                    headers=self._headers(),
                    json=payload,
                    ssl=True,
                ) as response,
            ):
                raw = await response.text()
                if response.status != 200:
                    raise ImageAPIError(self._explain_http(response.status, raw))
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    raise ImageAPIError("DashScope 返回的不是 JSON")
                if not isinstance(data, dict):
                    raise ImageAPIError("DashScope 返回结构异常")
                return self._extract_image_url(data)
        except ImageAPIError:
            raise
        except TimeoutError:
            raise ImageAPIError(f"DashScope 生图超时（{self.timeout} 秒）")
        except aiohttp.ClientError as exc:
            raise ImageAPIError(f"DashScope 网络请求失败：{exc}")

    async def _download(self, url: str) -> bytes:
        """下载图片 URL 并返回 bytes。"""
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        try:
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.get(url, ssl=True) as response,
            ):
                if response.status != 200:
                    raise ImageAPIError(
                        f"DashScope 图片下载失败（HTTP {response.status}）"
                    )
                return await response.read()
        except ImageAPIError:
            raise
        except aiohttp.ClientError as exc:
            raise ImageAPIError(f"DashScope 图片下载出错：{exc}")

    @staticmethod
    def _explain_http(status: int, body: str) -> str:
        """解析 HTTP 错误。"""
        detail = (body or "").strip()[:200]
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            payload = None
        if isinstance(payload, dict):
            message = payload.get("message") or payload.get("error", {}).get(
                "message"
            )
            if message:
                detail = str(message)[:200]
            code = payload.get("code")
            if code:
                detail = f"[{code}] {detail}"

        if status == 401:
            return f"DashScope 密钥无效（HTTP 401）：{detail}"
        if status == 403:
            return f"DashScope 密钥被拒绝（HTTP 403）：{detail}"
        if status == 404:
            return f"DashScope 接口地址或模型名不对（HTTP 404）：{detail}"
        if status == 429:
            return f"DashScope 限流或额度不足（HTTP 429）：{detail}"
        return f"DashScope 接口报错（HTTP {status}）：{detail}"

    @staticmethod
    def _extract_image_url(data: dict) -> str:
        """从同步响应中提取图片 URL。"""
        output = data.get("output")
        if not isinstance(output, dict):
            raise ImageAPIError("DashScope 响应缺少 output 字段")

        # 同步响应：output.choices[].message.content[].image
        choices = output.get("choices")
        if isinstance(choices, list) and choices:
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                message = choice.get("message")
                if not isinstance(message, dict):
                    continue
                content_list = message.get("content")
                if not isinstance(content_list, list):
                    continue
                for item in content_list:
                    if isinstance(item, dict) and item.get("image"):
                        return item["image"]

        # 异步任务响应
        task_status = output.get("task_status")
        if task_status in ("PENDING", "RUNNING"):
            raise ImageAPIError(
                f"DashScope 任务尚未完成（状态：{task_status}），本插件仅支持同步接口"
            )
        if task_status == "FAILED":
            error_msg = output.get("message") or "未知错误"
            raise ImageAPIError(f"DashScope 任务失败：{error_msg}")

        raise ImageAPIError("DashScope 响应中没有找到图片")

    _SIZE_PATTERN = re.compile(r"^(\d+)\s*[*xX×]\s*(\d+)$")

    def _convert_size(self, size: str) -> str | None:
        """把尺寸转成 DashScope 原生接口要求的「宽*高」。

        返回 None 表示不发送 size，由接口自己决定：
        - "1024x1024"、"2048X1152" → "1024*1024"、"2048*1152"
        - "auto"、空值、无法识别 → None
        - "1K"/"2K"/"4K" 只对万相系列原样保留；qwen-image 系列只接受像素尺寸
        """
        text = str(size or "").strip()
        match = self._SIZE_PATTERN.match(text)
        if match:
            width, height = int(match.group(1)), int(match.group(2))
            if width > 0 and height > 0:
                return f"{width}*{height}"
            return None
        if self._is_qwen_image():
            return None
        if text.upper() in ("1K", "2K", "4K"):
            return text.upper()
        if not text or text.lower() == "auto":
            return "2K"
        return None

    def _is_qwen_image(self) -> bool:
        return "qwen-image" in self.model.lower()
