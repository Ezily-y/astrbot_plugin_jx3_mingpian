"""剑网3 图片创作：@机器人 生图 + 参数 + 附图或引用图片。"""

from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path
from urllib.request import url2pathname

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

from .core.access import DENIED_MESSAGE, is_allowed, load_whitelist
from .core.backend import create_image_client
from .core.card import CardSource, build_card, build_closeup, generate_base
from .core.image_api import ImageAPIError, normalize_api_base
from .core.input_image import read_image
from .core.mingpian_data import SECT_POEMS, canonical_school
from .core.profiles import ImageProfile, ProfileManager
from .core.prompts import (
    default_prompts,
    empty_prompt_names,
    load_prompts,
    menu_text,
    select_prompt,
    task_hint,
)
from .core.settings import ImageSettings
from .core.sizes import card_size
from .core.tujian import (
    PORTRAIT_PROMPT,
    PORTRAIT_SIZE,
    SYSTEM_PROMPT,
    TujianError,
    build_prompt,
    is_empty,
    parse_profile,
)
from .core.tujian import payload as tujian_payload

PLUGIN_NAME = "astrbot_plugin_jx3_mingpian"
PLUGIN_VERSION = "2.3.0"
DEFAULT_CONCURRENT = 3
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates" / "standalone"
RENDER_OPTIONS = {
    "quality": 100,
    "device_scale_factor_level": "normal",
    "full_page": True,
    "omit_background": False,
    "type": "png",
}


@lru_cache(maxsize=1)
def load_template() -> str:
    return (TEMPLATE_DIR / "mingpian.html").read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def load_tujian_template() -> str:
    return (TEMPLATE_DIR / "tujian.html").read_text(encoding="utf-8")


def usage() -> str:
    return (
        "用法：/生图 ... 或 @机器人 生图 ...（@ 时斜杠可省略）；群里直接发送「生图 帮助」也能查看帮助。\n"
        "同条消息附一张图片，或引用一条含图片的消息。\n"
        "/生图 名片 [角色名] [门派] [区服]\n"
        "/生图 特写 提示词名称或编号\n"
        "/生图 图鉴 [角色名]\n"
        "/生图 接口 — 查看/切换接口方案\n"
        "/生图 帮助\n\n"
        "角色名、门派、区服均为选填，未填留空；用 - 跳过中间字段。\n"
        "示例（不填角色信息）：/生图 名片\n"
        "示例（只填门派）：/生图 名片 - 七秀\n"
        "示例：/生图 名片 小螺卜头 七秀 梦江南\n"
        "示例：/生图 特写 1\n"
        "示例：/生图 图鉴 小螺卜头\n"
        "示例：/生图 接口\n"
        "示例：/生图 接口 切换 2"
    )


@register(
    PLUGIN_NAME,
    "Ezily-y",
    "附图或引用图片，@机器人 发送「生图」创作名片、特写和图鉴。",
    PLUGIN_VERSION,
    "",
)
class JX3MingpianPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        self._active = 0
        self.settings = ImageSettings(self)
        if context is not None and hasattr(context, "register_web_api"):
            self.settings.register(context, PLUGIN_NAME)
        logger.info("%s 初始化完成（同时生成上限 %d）", PLUGIN_NAME, self._limit())

    def _text(self, key: str, default: str = "") -> str:
        return str(self.conf.get(key) or default).strip()

    def _list(self, key: str) -> list[str]:
        raw = self.conf.get(key)
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return []
        return [text for item in raw if (text := str(item or "").strip())]

    def _prompt_state(self):
        raw = (
            self.conf["mingpian_prompts"]
            if "mingpian_prompts" in self.conf
            else default_prompts()
        )
        return load_prompts(raw), empty_prompt_names(raw)

    def _limit(self) -> int:
        raw = str(self.conf.get("image_api_max_concurrent") or "").strip()
        try:
            number = int(raw) if raw else DEFAULT_CONCURRENT
        except ValueError:
            number = DEFAULT_CONCURRENT
        return max(1, min(number, 20))

    def _profile_manager(self) -> ProfileManager:
        return ProfileManager(
            self.conf.get("image_api_profiles") or [],
            int(self.conf.get("image_api_active_profile") or 0),
            fallback_url=self.conf.get("image_api_base_url", ""),
            fallback_keys=self._list("image_api_key"),
            fallback_model=self.conf.get("image_api_model", ""),
        )

    def _image_client(self):
        return create_image_client(self.conf, self._list("image_api_key"))

    def _save_profiles(self, manager: ProfileManager) -> None:
        """把方案列表和激活索引写回配置，并同步旧字段。"""
        self.conf["image_api_profiles"] = manager.to_list()
        self.conf["image_api_active_profile"] = manager.active_index
        current = manager.current()
        if current is not None:
            self.conf["image_api_base_url"] = current.base_url
            self.conf["image_api_key"] = list(current.api_keys)
            self.conf["image_api_model"] = current.model
        try:
            self.conf.save_config()
        except Exception:
            logger.exception("保存接口方案失败")

    def _auto_fallback(self) -> bool:
        return bool(self.conf.get("image_api_auto_fallback", True))

    async def _render(self, payload: dict, template: str | None = None) -> str:
        from astrbot.core import html_renderer

        return await html_renderer.render_custom_template(
            template or load_template(),
            payload,
            return_url=True,
            options=RENDER_OPTIONS,
        )

    def _allowed(self, event: AstrMessageEvent) -> bool:
        return is_allowed(
            self.conf.get("whitelist_enabled"),
            load_whitelist(self.conf.get("whitelist")),
            event.get_sender_id(),
            getattr(event, "unified_msg_origin", ""),
        )

    async def _send(self, event: AstrMessageEvent, *comps) -> None:
        await event.send(MessageChain(chain=list(comps)))
        event.stop_event()

    async def _send_text(self, event: AstrMessageEvent, text: str) -> None:
        await self._send(event, Comp.Plain(text))

    @staticmethod
    def _cleanup(path: object) -> None:
        text = str(path or "").strip()
        if not text or text.startswith(("http://", "https://", "base64://")):
            return
        local = Path(url2pathname(text.removeprefix("file://")))
        try:
            if local.is_file():
                local.unlink()
        except OSError:
            logger.warning("临时文件清理失败：%s", local)

    async def _profile_provider(self, event):
        from astrbot.core.provider.entities import ProviderType

        provider = await self.context.provider_manager.get_using_provider_async(
            ProviderType.CHAT_COMPLETION,
            getattr(event, "unified_msg_origin", "") or None,
        )
        if provider is None:
            raise TujianError("请先配置支持图片理解的对话模型，再生成图鉴")
        return provider

    @filter.command("生图")
    async def generate(self, event: AstrMessageEvent):
        """唯一入口；需要 @机器人 或唤醒前缀才能触发。"""
        # 从消息链提取 Plain 文本（去掉 @ 部分和命令名之后的内容）
        text = "".join(
            part.text for part in event.get_messages() if isinstance(part, Comp.Plain)
        ).strip()
        # filter.command("生图") 已确保消息以 "生图" 开头或被框架剥离了命令名，
        # 但 Plain 文本可能仍包含完整内容，手动跳过 "生图" 这个词
        parts = text.split()
        if parts and parts[0] in ("生图", "/生图"):
            args = parts[1:]
        else:
            args = parts
        if not self._allowed(event):
            await self._send_text(event, DENIED_MESSAGE)
            return

        prompts, skipped_prompts = self._prompt_state()
        if not args or args == ["帮助"]:
            await self._send_text(
                event,
                usage() + "\n\n" + menu_text(prompts, skipped=skipped_prompts),
            )
            return

        mode = args[0]

        # ---- 接口方案管理 ----
        if mode == "接口":
            await self._handle_profile_command(event, args[1:])
            return

        name = school = server = ""
        prompt = None
        try:
            if mode == "名片":
                if len(args) > 4:
                    raise ValueError(
                        "名片最多填写角色名、门派、区服三个参数。\n" + usage()
                    )
                name, school, server = [
                    "" if value == "-" else value for value in (args[1:] + [""] * 3)[:3]
                ]
                if school and canonical_school(school) not in SECT_POEMS:
                    raise ValueError("未识别的门派，可选：" + "、".join(SECT_POEMS))
            elif mode == "特写":
                prompt = select_prompt(prompts, " ".join(args[1:]))
            elif mode == "图鉴":
                if len(args) > 2:
                    raise ValueError("图鉴最多填写一个角色名。\n" + usage())
                name = args[1] if len(args) == 2 and args[1] != "-" else ""
            else:
                raise ValueError(usage())
            image = self._image_client()
            if not image.configured:
                raise ValueError(
                    "还没配置生图接口，请填写「生图接口地址 / 密钥 / 模型名」"
                )
        except ValueError as exc:
            await self._send_text(event, str(exc))
            return

        limit = self._limit()
        if self._active >= limit:
            await self._send_text(
                event, f"当前生成任务已达上限（{limit} 个），请稍后再试。"
            )
            return

        # 在首次 await 前占位，下载、生成、渲染和发送均计入并发上限。
        self._active += 1
        rendered = None
        try:
            provider = await self._profile_provider(event) if mode == "图鉴" else None
            art = await read_image(event)
            source = CardSource(art=art, nickname=name, school=school, server=server)
            await self._send_text(event, task_hint(self.conf.get("mingpian_task_hint")))
            if mode == "名片":
                payload = await build_card(
                    image, source, size=card_size(self.conf.get("mingpian_card_ratio"))
                )
                rendered = await self._render(payload)
            elif mode == "特写":
                blob = await build_closeup(image, source, prompt)
                await self._send(
                    event, Comp.Image.fromBase64(base64.b64encode(blob).decode("ascii"))
                )
                return
            else:
                portrait = await generate_base(
                    image,
                    source,
                    self._text("tujian_prompt", PORTRAIT_PROMPT),
                    size=PORTRAIT_SIZE,
                    with_card_ref=False,
                )
                resp = await provider.text_chat(
                    prompt=build_prompt(name),
                    image_urls=[
                        "base64://" + base64.b64encode(portrait).decode("ascii")
                    ],
                    system_prompt=SYSTEM_PROMPT,
                )
                profile = parse_profile(resp.completion_text)
                if is_empty(profile):
                    raise TujianError("模型没有返回可用的图鉴设定")
                rendered = await self._render(
                    tujian_payload(name=name, profile=profile, portrait=portrait),
                    load_tujian_template(),
                )
            await self._send(event, *(event.image_result(rendered).chain or []))
        except (ValueError, ImageAPIError, TujianError) as exc:
            await self._send_text(event, f"{mode}生成失败：{exc}")
        except Exception:  # noqa: BLE001
            logger.exception("%s生成异常", mode)
            await self._send_text(
                event, f"{mode}生成失败，请稍后再试；管理员可查看日志。"
            )
        finally:
            self._active -= 1
            if rendered:
                self._cleanup(rendered)

    @filter.regex(r"^生图\s+帮助$")
    async def help_direct(self, event: AstrMessageEvent):
        """群里不带 @ 或斜杠直接发「生图 帮助」也能查看帮助。"""
        await self.generate(event)

    async def _handle_profile_command(
        self, event: AstrMessageEvent, args: list[str]
    ) -> None:
        """接口方案管理：列出、切换、添加、删除。"""
        manager = self._profile_manager()

        if not args:
            # 列出所有方案
            fallback_note = ""
            if self._auto_fallback():
                fallback_note = "\n\n自动降级：已开启（当前方案失败时自动尝试下一个）"
            else:
                fallback_note = "\n\n自动降级：已关闭"
            await self._send_text(
                event,
                "当前接口方案：\n" + manager.format_list() + fallback_note,
            )
            return

        sub = args[0]

        if sub == "切换":
            if len(args) < 2:
                await self._send_text(
                    event,
                    "用法：生图 接口 切换 <编号或名称>\n"
                    "编号从 1 开始，也可以用方案名称。",
                )
                return
            result = manager.switch_to(args[1])
            if result is None:
                await self._send_text(
                    event, f"找不到方案「{args[1]}」，请用「生图 接口」查看列表。"
                )
                return
            self._save_profiles(manager)
            await self._send_text(
                event,
                f"已切换到方案「{result.name or '未命名'}」\n"
                f"地址：{result.base_url}\n"
                f"模型：{result.model}",
            )
            return

        if sub == "添加":
            if len(args) < 4:
                await self._send_text(
                    event,
                    "用法：生图 接口 添加 <名称> <URL> <密钥> [模型名]\n"
                    "示例：生图 接口 添加 备用接口 https://api.example.com/v1 sk-xxx gpt-image-2.5",
                )
                return
            profile_name = args[1]
            url = normalize_api_base(args[2])
            key = args[3]
            model = args[4] if len(args) > 4 else "gpt-image-2.5"
            new_profile = ImageProfile(
                name=profile_name,
                base_url=url,
                api_keys=[key],
                model=model,
            )
            index = manager.add(new_profile)
            self._save_profiles(manager)
            await self._send_text(
                event,
                f"已添加方案 #{index}「{profile_name}」\n"
                f"地址：{url}\n"
                f"模型：{model}",
            )
            return

        if sub == "删除":
            if len(args) < 2:
                await self._send_text(
                    event, "用法：生图 接口 删除 <编号或名称>"
                )
                return
            if len(manager.profiles) <= 1:
                await self._send_text(event, "至少保留一个方案，无法删除。")
                return
            if not manager.remove(args[1]):
                await self._send_text(
                    event, f"找不到方案「{args[1]}」，请用「生图 接口」查看列表。"
                )
                return
            self._save_profiles(manager)
            await self._send_text(event, f"已删除方案，当前激活：#{manager.active_index + 1}")
            return

        if sub == "降级":
            if len(args) < 2:
                state = "开启" if self._auto_fallback() else "关闭"
                await self._send_text(
                    event,
                    f"当前自动降级：{state}\n"
                    "用法：生图 接口 降级 <开启|关闭>",
                )
                return
            value = args[1].strip()
            if value in ("开启", "开", "on", "true", "1"):
                self.conf["image_api_auto_fallback"] = True
            elif value in ("关闭", "关", "off", "false", "0"):
                self.conf["image_api_auto_fallback"] = False
            else:
                await self._send_text(event, "请选择「开启」或「关闭」。")
                return
            try:
                self.conf.save_config()
            except Exception:
                logger.exception("保存自动降级设置失败")
            state = "开启" if self._auto_fallback() else "关闭"
            await self._send_text(event, f"自动降级已{state}")
            return

        await self._send_text(
            event,
            "接口管理命令：\n"
            "生图 接口 — 查看所有方案\n"
            "生图 接口 切换 <编号或名称> — 切换方案\n"
            "生图 接口 添加 <名称> <URL> <密钥> [模型] — 添加方案\n"
            "生图 接口 删除 <编号或名称> — 删除方案\n"
            "生图 接口 降级 <开启|关闭> — 控制自动降级",
        )

    async def terminate(self):
        await self.settings.close()
        logger.info("%s 已卸载", PLUGIN_NAME)
