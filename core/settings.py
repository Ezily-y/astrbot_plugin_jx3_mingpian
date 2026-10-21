"""生图设置页面服务：接口方案、模型检测和实际生图测试的唯一入口。

页面只返回密钥数量，不把已保存密钥回传浏览器；留空的密钥表示保留原值。
"""

from __future__ import annotations

import asyncio
import base64
import tempfile
from contextlib import suppress
from io import BytesIO
from pathlib import Path
from time import perf_counter

from astrbot.api import logger
from PIL import Image, ImageDraw

from .backend import API_BACKEND, make_client_for_profile
from .image_api import (
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT,
    ImageAPIError,
    normalize_api_base,
)
from .profiles import ImageProfile, ProfileManager, new_profile_id

_UNSET = object()


def _timeout(value: object, fallback: int = DEFAULT_TIMEOUT) -> int:
    try:
        number = int(value) if value else fallback
    except (TypeError, ValueError):
        number = fallback
    return max(30, min(number, 3600))


def _clamp_index(value: object, count: int) -> int:
    try:
        index = int(value) if value is not None else 0
    except (TypeError, ValueError):
        index = 0
    return max(0, min(index, count - 1))


def _keys_from(raw: object, old: ImageProfile | None) -> list[str]:
    """读取页面传来的密钥；留空或未传时沿用原方案的密钥。"""
    if raw is _UNSET or raw is None:
        keys: list[str] = []
    elif isinstance(raw, list):
        keys = [str(key).strip() for key in raw if str(key or "").strip()]
    else:
        raise ValueError("密钥必须是文本列表")
    if not keys and old is not None:
        keys = list(old.api_keys)
    return keys


class ImageSettings:
    def __init__(self, plugin):
        self.plugin = plugin
        self.test_task: asyncio.Task | None = None
        self._test = {"status": "idle"}
        self._closed = False

    # ---------- 方案读取 ----------

    def _manager(self) -> ProfileManager:
        # 复用插件自己的方案管理器，旧单字段回退逻辑只保留一份。
        return self.plugin._profile_manager()

    async def get_settings(self) -> dict:
        manager = self._manager()
        return {
            "profiles": [
                {
                    "id": profile.id,
                    "name": profile.name,
                    "base_url": profile.base_url,
                    "model": profile.model,
                    "timeout": profile.timeout,
                    "key_count": len(profile.api_keys),
                }
                for profile in manager.profiles
            ],
            "active": manager.active_index,
            "auto_fallback": bool(
                self.plugin.conf.get("image_api_auto_fallback", True)
            ),
            "backend": self.plugin._text("image_backend", API_BACKEND),
        }

    # ---------- 方案保存 ----------

    async def save(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise TypeError("请求参数必须为对象")
        items = payload.get("profiles")
        if not isinstance(items, list) or not items:
            raise ValueError("至少保留一个接口方案")
        existing = {profile.id: profile for profile in self._manager().profiles}
        profiles: list[ImageProfile] = []
        seen: set[str] = set()
        for index, item in enumerate(items, 1):
            profiles.append(self._build_profile(item, existing, seen, index))
        active = _clamp_index(payload.get("active"), len(profiles))
        auto_fallback = payload.get("auto_fallback")
        if not isinstance(auto_fallback, bool):
            auto_fallback = bool(
                self.plugin.conf.get("image_api_auto_fallback", True)
            )

        primary = profiles[active]
        new_values = {
            "image_api_profiles": [profile.to_dict() for profile in profiles],
            "image_api_active_profile": active,
            "image_api_auto_fallback": auto_fallback,
            "image_api_base_url": primary.base_url,
            "image_api_key": list(primary.api_keys),
            "image_api_model": primary.model,
        }
        previous = {
            key: self.plugin.conf.get(key, _UNSET) for key in new_values
        }
        self.plugin.conf.update(new_values)
        try:
            self.plugin.conf.save_config()
        except Exception:
            for key, value in previous.items():
                if value is _UNSET:
                    self.plugin.conf.pop(key, None)
                else:
                    self.plugin.conf[key] = value
            raise
        return await self.get_settings()

    def _build_profile(
        self,
        item: object,
        existing: dict[str, ImageProfile],
        seen: set[str],
        index: int,
    ) -> ImageProfile:
        if not isinstance(item, dict):
            raise ValueError(f"第 {index} 个方案格式无效")
        profile_id = str(item.get("id") or "").strip()
        if not profile_id or profile_id in seen:
            profile_id = new_profile_id()
        seen.add(profile_id)
        old = existing.get(profile_id)
        name = str(item.get("name") or "").strip() or f"方案 {index}"
        base_url = normalize_api_base(
            item.get("base_url") or (old.base_url if old else "")
        )
        if not base_url:
            raise ValueError(f"方案「{name}」缺少接口地址")
        model = str(item.get("model") or (old.model if old else "")).strip()
        if not model:
            raise ValueError(f"方案「{name}」缺少模型名")
        keys = _keys_from(item.get("api_key", _UNSET), old)
        if not keys:
            raise ValueError(f"方案「{name}」缺少密钥")
        timeout = _timeout(
            item.get("timeout"), old.timeout if old else DEFAULT_TIMEOUT
        )
        return ImageProfile(
            id=profile_id,
            name=name,
            base_url=base_url,
            api_keys=keys,
            model=model,
            timeout=timeout,
        )

    # ---------- 临时方案：模型检测 / 连通性 / 实际生图测试 ----------

    def _resolve_profile(self, payload: object) -> ImageProfile:
        if not isinstance(payload, dict):
            raise TypeError("请求参数必须为对象")
        profile_id = str(payload.get("id") or "").strip()
        old = next(
            (
                profile
                for profile in self._manager().profiles
                if profile.id == profile_id
            ),
            None,
        )
        base_url = normalize_api_base(
            payload.get("base_url") or (old.base_url if old else "")
        )
        if not base_url:
            raise ValueError("请填写生图接口地址")
        raw = payload.get("api_key", _UNSET)
        keys = _keys_from(raw, old)
        if not keys:
            raise ValueError("请填写至少一条生图密钥")
        model = str(payload.get("model") or (old.model if old else "")).strip()
        model = model or DEFAULT_MODEL
        timeout = _timeout(
            payload.get("timeout"), old.timeout if old else DEFAULT_TIMEOUT
        )
        return ImageProfile(
            id=old.id if old else "",
            name=old.name if old else "",
            base_url=base_url,
            api_keys=keys,
            model=model,
            timeout=timeout,
        )

    async def models(self, payload: dict) -> dict:
        profile = self._resolve_profile(payload)
        client = make_client_for_profile(profile)
        return {"models": await client.list_models()}

    async def connectivity(self, payload: dict) -> dict:
        profile = self._resolve_profile(payload)
        client = make_client_for_profile(profile)
        started = perf_counter()
        models = await client.list_models()
        return {
            "reachable": True,
            "models": models,
            "model": profile.model,
            "model_listed": profile.model in models,
            "image_tested": False,
            "elapsed_ms": round((perf_counter() - started) * 1000),
        }

    async def start_test(self, payload: dict) -> dict:
        if self._closed:
            raise ValueError("生图设置服务已关闭，无法开始测试")
        profile = self._resolve_profile(payload)
        client = make_client_for_profile(profile)
        if self.test_task is not None and not self.test_task.done():
            raise ValueError("已有实际生图测试中，请等待结果")
        if self.plugin._active >= self.plugin._limit():
            raise ValueError("当前生成任务已达上限，请稍后再测试")
        self.plugin._active += 1
        self._test = {"status": "running", "model": client.model}
        self.test_task = asyncio.create_task(self._run_test(client))
        self.test_task.add_done_callback(self._test_finished)
        return dict(self._test)

    def _test_finished(self, task: asyncio.Task) -> None:
        # 即使任务在首次执行前被取消，也必须释放 start_test 预留的名额。
        self.plugin._active -= 1
        if (
            task is self.test_task
            and task.cancelled()
            and self._test["status"] == "running"
        ):
            self._test = {
                "status": "cancelled",
                "model": self._test["model"],
                "elapsed_ms": 0,
            }

    async def _run_test(self, client) -> None:
        started = perf_counter()
        try:
            with tempfile.TemporaryDirectory(prefix="jx3_image_test_") as directory:
                reference = Path(directory) / "reference.png"
                image = Image.new("RGB", (256, 256), "#eef2ff")
                ImageDraw.Draw(image).ellipse((64, 64, 192, 192), fill="#6366f1")
                image.save(reference)
                blob = await client.edit(
                    "将参考图中的圆形改为蓝色，保留纯色背景，不添加文字。",
                    [reference],
                    size="1024x1024",
                    quality="high",
                    retries=0,
                )
            with Image.open(BytesIO(blob)) as result:
                mime = Image.MIME.get(result.format, "image/png")
                result.verify()
            self._test = {
                "status": "success",
                "model": client.model,
                "image": f"data:{mime};base64,"
                + base64.b64encode(blob).decode("ascii"),
            }
        except asyncio.CancelledError:
            self._test = {"status": "cancelled", "model": client.model}
            raise
        except Exception as exc:  # noqa: BLE001
            self._test = {
                "status": "error",
                "model": client.model,
                "error": client.safe_error(exc),
            }
        finally:
            self._test["elapsed_ms"] = round((perf_counter() - started) * 1000)

    async def test_status(self) -> dict:
        return dict(self._test)

    async def close(self) -> None:
        self._closed = True
        if self.test_task is not None and not self.test_task.done():
            self.test_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.test_task

    def register(self, context, plugin_name: str) -> None:
        for path, method, action in (
            ("settings", "GET", self.get_settings),
            ("settings", "POST", self.save),
            ("models", "POST", self.models),
            ("connectivity", "POST", self.connectivity),
            ("image-test", "POST", self.start_test),
            ("image-test", "GET", self.test_status),
        ):
            context.register_web_api(
                f"/{plugin_name}/{path}",
                self._handler(action, method),
                [method],
                "生图设置",
            )

    def _handler(self, action, method):
        async def handle():
            from astrbot.api.web import error_response, json_response, request

            headers = {"Cache-Control": "no-store"}
            try:
                if self._closed:
                    return error_response(
                        "生图设置服务已关闭", status_code=503, headers=headers
                    )
                payload = await request.json(default={}) if method == "POST" else None
                result = (
                    await action(payload)
                    if method == "POST"
                    else await action()
                )
                return json_response(
                    {"status": "ok", "message": "", "data": result},
                    headers=headers,
                )
            except (TypeError, ValueError, ImageAPIError) as exc:
                return error_response(str(exc), status_code=400, headers=headers)
            except Exception:  # noqa: BLE001
                logger.exception("生图设置操作失败")
                return error_response(
                    "操作失败，请查看管理员日志", status_code=500, headers=headers
                )

        return handle
