"""生图接口客户端。OpenAI Images 格式，gpt-image-2 与 gpt-image-2.5 通用。

配置项：
    image_api_base_url   形如 https://host/v1，末尾缺 /v1 时自动补
    image_api_key        密钥
    image_api_model      模型名

请求接口：{base}/images/edits（multipart）。
名片卡均带参考图，不使用纯文生图接口。

生图请求不主动使用 HTTP 代理。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import mimetypes
from collections.abc import Iterable
from pathlib import Path

import aiohttp
from aiohttp import ClientTimeout, FormData
from astrbot.api import logger

from .keypool import KeyPool

DEFAULT_MODEL = "gpt-image-2.5"

DEFAULT_TIMEOUT = 300


class ImageAPIError(RuntimeError):
    """生图失败。message 为可直接返回给用户的文案。"""


def normalize_api_base(value: object) -> str:
    """规范生图接口地址：去尾斜杠，缺 /v1 时补上。空值返回空串。"""
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    if not text.startswith(("http://", "https://")):
        raise ValueError("生图接口地址必须以 http:// 或 https:// 开头。")
    if not text.endswith("/v1") and "/v1/" not in text:
        text += "/v1"
    return text


class ImageAPIClient:
    """每次调用创建独立会话，调用结束即关闭。"""

    def __init__(
        self,
        base_url: str = "",
        api_key: str | KeyPool | Iterable[str] = "",
        model: str = DEFAULT_MODEL,
        *,
        ssl_verify: bool = True,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self.base_url = normalize_api_base(base_url)
        self.keys = api_key if isinstance(api_key, KeyPool) else KeyPool(api_key)
        self.model = str(model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        self.ssl_verify = bool(ssl_verify)
        self.timeout = int(timeout)

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.keys and self.model)

    def endpoint(self, kind: str) -> str:
        """kind: generations / edits"""
        return f"{self.base_url}/images/{kind}"

    def _headers(self, key: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {key}",
            "User-Agent": "astrbot-jx3-mingpian",
            "Accept": "application/json",
        }

    def safe_error(self, error: object) -> str:
        """诊断结果不回传可能被上游错误信息回显的密钥。"""
        message = str(error)
        for key in sorted(self.keys.keys, key=len, reverse=True):
            message = message.replace(key, "***")
        return message

    async def list_models(self) -> list[str]:
        """查询服务公布的模型，不推断图片能力，不推进生图轮询游标。"""
        if not self.base_url or not self.keys:
            raise ImageAPIError("请先填写生图接口地址和密钥")
        order = self.keys.order()
        for index, key in enumerate(order):
            try:
                return await self._get_models(key)
            except ImageAPIError as exc:
                message = self.safe_error(exc)
                if index + 1 < len(order) and KeyPool.is_credential_failure(str(exc)):
                    continue
                raise ImageAPIError(message) from None
        raise ImageAPIError("没有可用密钥")

    async def _get_models(self, key: str) -> list[str]:
        timeout = ClientTimeout(total=min(self.timeout, 15))
        try:
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.get(
                    f"{self.base_url}/models",
                    headers=self._headers(key),
                    ssl=self.ssl_verify,
                ) as response,
            ):
                raw = await response.text()
                if response.status != 200:
                    if response.status == 404:
                        raise ImageAPIError(
                            "服务未提供 /models 接口，请手动填写模型名并使用实际生图测试"
                        )
                    raise ImageAPIError(self._explain_http(response.status, raw))
            payload = json.loads(raw)
            if not isinstance(payload, dict) or not isinstance(
                payload.get("data"), list
            ):
                raise ImageAPIError("模型列表结构异常，预期 data 数组")
            models = sorted(
                {
                    item["id"].strip()
                    for item in payload["data"]
                    if isinstance(item, dict)
                    and isinstance(item.get("id"), str)
                    and item["id"].strip()
                }
            )
            if payload["data"] and not models:
                raise ImageAPIError("模型列表中没有有效的模型 ID")
            return models
        except json.JSONDecodeError:
            raise ImageAPIError("模型列表接口返回的不是 JSON") from None
        except TimeoutError:
            raise ImageAPIError("获取模型列表超时，请检查服务地址或稍后再试") from None
        except aiohttp.ClientError as exc:
            raise ImageAPIError(f"模型列表网络请求失败：{exc}") from None

    async def _post(self, url: str, key: str, *, form: FormData) -> dict:
        timeout = ClientTimeout(total=self.timeout, sock_read=self.timeout)
        try:
            # 不传 proxy，生图请求直连
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.post(
                    url,
                    headers=self._headers(key),
                    data=form,
                    ssl=self.ssl_verify,
                ) as response,
            ):
                raw = await response.text()
                if response.status != 200:
                    raise ImageAPIError(self._explain_http(response.status, raw))
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    raise ImageAPIError("生图接口返回的不是 JSON")
                if not isinstance(payload, dict):
                    raise ImageAPIError("生图接口返回结构异常")
                return payload
        except ImageAPIError:
            raise
        except TimeoutError:
            raise ImageAPIError(f"生图超时（{self.timeout} 秒），请稍后再试")
        except aiohttp.ClientError as exc:
            raise ImageAPIError(f"生图网络请求失败：{exc}")

    @staticmethod
    def _explain_http(status: int, body: str) -> str:
        detail = (body or "").strip()[:200]
        # 响应体可能不是 JSON，解析失败时沿用原始文本
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            payload = None
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict) and error.get("message"):
                detail = str(error["message"])[:200]
            elif payload.get("message"):
                detail = str(payload["message"])[:200]
        if "Images API is not supported for this platform" in detail:
            return (
                "当前服务不支持 OpenAI Images API；请在「生图设置」切换为“本机 Codex CLI”，"
                "或改用提供 /images/edits 的 OpenAI 兼容服务。"
            )
        if status in (401, 403):
            return f"生图密钥被拒绝（HTTP {status}）：{detail}"
        if status == 404:
            return f"生图接口地址或模型名不对（HTTP 404）：{detail}"
        if status == 429:
            return f"生图接口限流或额度不足（HTTP 429）：{detail}"
        return f"生图接口报错（HTTP {status}）：{detail}"

    async def _extract_image(self, payload: dict) -> bytes:
        """从响应中取出图片。

        兼容三种返回形式：b64_json（含 data URI）、url、image_url。
        服务端默认返回 url，因此请求时一并要求 b64_json。
        """
        items = payload.get("data")
        if not isinstance(items, list) or not items:
            raise ImageAPIError("生图接口没有返回图片")
        for item in items:
            if not isinstance(item, dict):
                continue
            for field in ("b64_json", "b64", "image_base64"):
                value = item.get(field)
                if isinstance(value, str) and value:
                    data = self._decode_b64(value)
                    if data is not None:
                        return data
            for field in ("url", "image_url"):
                value = item.get(field)
                if isinstance(value, str) and value:
                    data = await self._fetch_image(value)
                    if data is not None:
                        return data
        raise ImageAPIError("生图接口没有返回可用的图片数据")

    @staticmethod
    def _decode_b64(value: str) -> bytes | None:
        """解码 base64，兼容 data:image/png;base64,xxx 前缀。"""
        text = (
            value.split(",", 1)[1]
            if value.startswith("data:") and "," in value
            else value
        )
        # 去掉空白再解码：服务端可能按列折行，validate=True 会因此拒绝合法数据
        text = "".join(text.split())
        try:
            return base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError):
            logger.warning("生图返回的 base64 数据无法解码，尝试下一条")
            return None

    async def _fetch_image(self, url: str) -> bytes | None:
        """下载图片地址。data URI 直接解码。"""
        if url.startswith("data:"):
            return self._decode_b64(url)
        if not url.startswith(("http://", "https://")):
            return None
        timeout = ClientTimeout(total=self.timeout)
        try:
            # 下载生图服务返回的图片地址
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.get(url, ssl=self.ssl_verify) as response,
            ):
                if response.status != 200:
                    logger.warning("图片下载失败 %s: %s", response.status, url)
                    return None
                return await response.read()
        except aiohttp.ClientError as exc:
            logger.warning("图片下载出错 %s: %s", url, exc)
            return None

    @staticmethod
    def _is_stable_failure(message: str) -> bool:
        """判断是否为配置类错误。此类错误重试无效，直接放弃。"""
        for marker in ("密钥被拒绝", "地址或模型名不对", "不是 JSON"):
            if marker in message:
                return True
        return False

    async def edit(
        self,
        prompt: str,
        images: Iterable[Path],
        *,
        size: str,
        quality: str = "high",
        retries: int = 1,
    ) -> bytes:
        """带参考图生图。尺寸由调用方指定，失败时按 retries 重试。"""
        if not self.configured:
            raise ImageAPIError(
                "还没配置生图接口，请填写「生图接口地址 / 密钥 / 模型名」"
            )
        paths = [Path(p) for p in images]
        if not paths:
            raise ImageAPIError("edit 至少需要一张参考图")

        order = self.keys.take_order()
        last_error = ""
        for key_index, key in enumerate(order):
            # 每条密钥先按 retries 重试，仍失败则换下一条
            for attempt in range(retries + 1):
                form = FormData()
                form.add_field("model", self.model)
                form.add_field("prompt", prompt)
                # 尺寸原样透传：各家服务接受的尺寸集合不同，不做本地白名单过滤
                form.add_field("size", size or "auto")
                form.add_field("quality", quality)
                form.add_field("n", "1")
                # 默认返回的是图片地址，这里直接要 base64
                form.add_field("response_format", "b64_json")
                for path in paths:
                    form.add_field(
                        "image[]",
                        path.read_bytes(),
                        filename=path.name,
                        # 按文件名推断，与文件真实格式一致（调用方按字节定后缀）
                        content_type=mimetypes.guess_type(path.name)[0] or "image/png",
                    )
                logger.info(
                    "生图请求 %s model=%s size=%s 参考图 %d 张（密钥 %d/%d，第 %d 次）",
                    self.endpoint("edits"),
                    self.model,
                    size,
                    len(paths),
                    key_index + 1,
                    len(order),
                    attempt + 1,
                )
                try:
                    payload = await self._post(self.endpoint("edits"), key, form=form)
                    return await self._extract_image(payload)
                except ImageAPIError as exc:
                    last_error = str(exc)
                    if self._is_stable_failure(last_error):
                        break
                    if attempt >= retries:
                        break
                    logger.warning("生图失败，重试一次：%s", last_error)
                    await asyncio.sleep(1.5)
            if key_index + 1 < len(order) and KeyPool.is_credential_failure(last_error):
                logger.warning("生图密钥不可用，改用下一条：%s", last_error)
                continue
            break
        raise ImageAPIError(last_error or "生图失败")
