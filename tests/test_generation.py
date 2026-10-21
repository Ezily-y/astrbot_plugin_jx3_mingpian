"""图片创作入口：只在 /生图 下执行，参数错误不消耗生成请求。"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from astrbot.api import message_components as Comp
from conftest import load_plugin_main
from PIL import Image

main = load_plugin_main()

from astrbot_plugin_jx3_mingpian.core.image_api import ImageAPIClient  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class Event:
    def __init__(self, text, images=(), quoted=None):
        self.message_str = text
        self.unified_msg_origin = "test:GroupMessage:1"
        self.chain = [Comp.Plain(text), *images]
        if quoted is not None:
            self.chain.insert(0, Comp.Reply(chain=quoted))
        self.sent = []
        self.stopped = False

    def get_messages(self):
        return self.chain

    def get_sender_id(self):
        return "user"

    async def send(self, result):
        self.sent.extend(result.chain)

    def image_result(self, url):
        return SimpleNamespace(chain=[Comp.Image(url)])

    def stop_event(self):
        self.stopped = True

    @property
    def text(self):
        return "\n".join(c.text for c in self.sent if isinstance(c, Comp.Plain))


def plugin(**overrides):
    conf = {
        "image_api_base_url": "https://image.example/v1",
        "image_api_key": ["key"],
        "mingpian_prompts": [
            {"name": "双眼特写", "prompt": "eyes", "ratio": "21:9"},
            {"name": "手办化", "prompt": "figure", "ratio": "1:1"},
        ],
    }
    conf.update(overrides)
    return main.JX3MingpianPlugin(None, conf)


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "source.png"
    Image.new("RGB", (32, 32), "red").save(path)
    return Comp.Image(str(path))


def test_generate_is_registered_as_explicit_command():
    # 命令过滤由 AstrBot 的 @filter.command("生图") 完成，handler 不再自己判断消息前缀
    assert getattr(main.JX3MingpianPlugin.generate, "__astrbot_command__", None) == "生图"


async def test_help_lists_current_templates_without_image_or_credentials():
    event = Event("/生图 帮助")
    await plugin(image_api_key=[]).generate(event)
    assert "/生图 名片" in event.text and "/生图 图鉴" in event.text
    assert "1. 双眼特写" in event.text and "2. 手办化" in event.text
    assert "引用" in event.text and "自动选" not in event.text


async def test_framework_stripped_slash_still_uses_original_command():
    event = Event("/生图 帮助")
    event.message_str = "生图 帮助"
    await plugin().generate(event)
    assert "/生图 名片" in event.text


def test_help_direct_is_registered_as_regex():
    assert (
        getattr(main.JX3MingpianPlugin.help_direct, "__astrbot_command__", None)
        == r"^生图\s+帮助$"
    )


async def test_plain_generation_help_triggers_without_wake():
    event = Event("生图 帮助")
    await plugin().help_direct(event)
    assert "/生图 名片" in event.text
    assert event.stopped


async def test_help_lists_empty_prompt_names():
    instance = plugin(
        mingpian_prompts=[
            {"name": "可用", "prompt": "x"},
            {"name": "测试提示词", "prompt": ""},
        ]
    )
    event = Event("/生图 帮助")
    await instance.generate(event)
    assert "1. 可用" in event.text
    assert "测试提示词" in event.text
    assert "内容为空、未启用的条目" in event.text


async def test_reading_media_does_not_detach_temporary_files(image, monkeypatch):
    async def must_not_detach(self):
        raise AssertionError("convert_to_file_path 会转移临时文件清理责任")

    async def edit(self, text, refs, **kwargs):
        return b"result"

    monkeypatch.setattr(Comp.Image, "convert_to_file_path", must_not_detach)
    monkeypatch.setattr(ImageAPIClient, "edit", edit)
    event = Event("/生图 特写 1", [image])
    await plugin().generate(event)
    assert any(isinstance(c, Comp.Image) for c in event.sent)
    assert Path(image.file).exists()


async def test_official_school_alias_is_accepted(image, monkeypatch):
    async def edit(self, text, refs, **kwargs):
        return b"result"

    async def render(self, payload, template=None):
        assert payload["school"] == "凌雪阁"
        return "https://image.example/card.png"

    monkeypatch.setattr(ImageAPIClient, "edit", edit)
    monkeypatch.setattr(main.JX3MingpianPlugin, "_render", render)
    event = Event("/生图 名片 小螺卜头 凌雪阁", [image])
    await plugin().generate(event)
    assert any(isinstance(c, Comp.Image) for c in event.sent)


async def test_help_uses_updated_config_and_builtin_defaults():
    instance = plugin()
    instance.conf["mingpian_prompts"] = [{"name": "新模板", "prompt": "new"}]
    event = Event("/生图 帮助")
    await instance.generate(event)
    assert "1. 新模板" in event.text and "双眼特写" not in event.text
    instance.conf.pop("mingpian_prompts")
    event = Event("/生图 帮助")
    await instance.generate(event)
    assert "多视角海报" in event.text and "破屏而出" in event.text


@pytest.mark.parametrize(
    "text, error",
    [
        ("/生图", "用法"),
        ("/生图 名片 小螺卜头 不存在", "门派"),
        ("/生图 名片 名字 七秀 区服 多余", "用法"),
        ("/生图 图鉴 名字 多余", "用法"),
        ("/生图 特写", "可选"),
        ("/生图 特写 0", "可选"),
        ("/生图 特写 3", "可选"),
        ("/生图 特写 不存在", "可选"),
        ("/生图 未知", "用法"),
    ],
)
async def test_invalid_parameters_do_not_start_generation(
    text, error, image, monkeypatch
):
    async def forbidden(*args, **kwargs):
        pytest.fail("参数无效时不能调用生图接口")

    monkeypatch.setattr(ImageAPIClient, "edit", forbidden)
    event = Event(text, [image])
    await plugin().generate(event)
    assert error in event.text and "正在" not in event.text


@pytest.mark.parametrize(
    "selector,prompt,size",
    [("1", "eyes", "2048x880"), ("手办化", "figure", "1024x1024")],
)
@pytest.mark.parametrize("quoted", [False, True])
async def test_template_selection_and_image_sources(
    selector, prompt, size, quoted, image, monkeypatch
):
    seen = []

    async def edit(self, text, refs, **kwargs):
        seen.append((text, Path(refs[0]).read_bytes(), kwargs["size"]))
        return b"result"

    monkeypatch.setattr(ImageAPIClient, "edit", edit)
    event = Event(
        f"/生图 特写 {selector}", [] if quoted else [image], [image] if quoted else None
    )
    instance = plugin()
    await instance.generate(event)
    assert seen == [(prompt, Path(image.file).read_bytes(), size)]
    assert any(isinstance(c, Comp.Image) for c in event.sent)
    assert instance._active == 0


async def test_missing_image_returns_actionable_hint():
    event = Event("/生图 特写 1", quoted=[])
    await plugin().generate(event)
    assert "图片" in event.text and "引用" in event.text and "正在" not in event.text


async def test_direct_image_takes_priority_over_quote(image, tmp_path, monkeypatch):
    other = tmp_path / "other.png"
    Image.new("RGB", (32, 32), "blue").save(other)
    seen = []

    async def edit(self, text, refs, **kwargs):
        seen.append(Path(refs[0]).read_bytes())
        return b"result"

    monkeypatch.setattr(ImageAPIClient, "edit", edit)
    await plugin().generate(Event("/生图 特写 1", [image], [Comp.Image(str(other))]))
    assert seen == [Path(image.file).read_bytes()]


@pytest.mark.parametrize("invalid", [False, True])
async def test_ambiguous_or_invalid_image_never_generates(
    invalid, image, tmp_path, monkeypatch
):
    async def forbidden(*args, **kwargs):
        pytest.fail("图片无效时不能调用生图接口")

    monkeypatch.setattr(ImageAPIClient, "edit", forbidden)
    if invalid:
        path = tmp_path / "broken.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\nbroken")
        images = [Comp.Image(str(path))]
    else:
        images = [image, image]
    event = Event("/生图 特写 1", images)
    await plugin().generate(event)
    assert "图片" in event.text and "正在" not in event.text


async def test_card_uses_supplied_identity_without_game_queries(image, monkeypatch):
    async def edit(self, text, refs, **kwargs):
        return b"generated"

    async def render(self, payload, template=None):
        assert payload["nickname"] == "小螺卜头" and payload["school"] == "七秀"
        assert payload["server"] == "梦江南"
        assert not ({"body", "camp", "tong", "level"} & payload.keys())
        return "https://image.example/card.png"

    monkeypatch.setattr(ImageAPIClient, "edit", edit)
    monkeypatch.setattr(main.JX3MingpianPlugin, "_render", render)
    event = Event("/生图 名片 小螺卜头 七秀 梦江南", [image])
    await plugin().generate(event)
    assert any(
        getattr(c, "file", "") == "https://image.example/card.png" for c in event.sent
    )


@pytest.mark.parametrize(
    "args, identity",
    [
        ("", ("", "", "")),
        ("小螺卜头", ("小螺卜头", "", "")),
        ("小螺卜头 七秀", ("小螺卜头", "七秀", "")),
        ("- 七秀", ("", "七秀", "")),
        ("- - 梦江南", ("", "", "梦江南")),
        ("小螺卜头 - 梦江南", ("小螺卜头", "", "梦江南")),
    ],
)
async def test_card_identity_fields_are_optional(args, identity, image, monkeypatch):
    rendered = []

    async def edit(self, text, refs, **kwargs):
        return b"result"

    async def render(self, payload, template=None):
        from jinja2 import Template

        rendered.append(payload)
        html = Template(main.load_template()).render(**payload)
        if not identity[2]:
            assert 'class="kv kv--server"' not in html
        return "https://image.example/card.png"

    monkeypatch.setattr(ImageAPIClient, "edit", edit)
    monkeypatch.setattr(main.JX3MingpianPlugin, "_render", render)
    event = Event("/生图 名片 " + args, [image])
    await plugin().generate(event)
    assert len(rendered) == 1
    assert tuple(rendered[0][key] for key in ("nickname", "school", "server")) == identity
    if not identity[1]:
        assert not rendered[0].get("schoolIcon") and not rendered[0].get("poem")


@pytest.mark.parametrize("name", ["小螺卜头", "", "-"])
async def test_tujian_preserves_generation_and_name(image, monkeypatch, name):
    expected_name = "" if name == "-" else name
    class Provider:
        async def text_chat(self, **kwargs):
            assert kwargs["image_urls"]
            assert kwargs["prompt"].splitlines()[0] == "角色名：" + expected_name
            if not expected_name:
                assert "不要自行起名" in kwargs["prompt"]
            return SimpleNamespace(completion_text='{"quote":"江湖一梦"}')

    async def provider(*args):
        return Provider()

    async def edit(self, text, refs, **kwargs):
        return b"portrait"

    async def render(self, payload, template=None):
        assert payload["name"] == expected_name and payload["quote"] == "江湖一梦"
        return "https://image.example/profile.png"

    monkeypatch.setattr(ImageAPIClient, "edit", edit)
    monkeypatch.setattr(main.JX3MingpianPlugin, "_render", render)
    instance = plugin()
    instance.context = SimpleNamespace(
        provider_manager=SimpleNamespace(get_using_provider_async=provider)
    )
    event = Event("/生图 图鉴 " + name, [image])
    await instance.generate(event)
    assert any(getattr(c, "file", "").endswith("profile.png") for c in event.sent)


async def test_concurrent_limit_reserves_slot_before_await(image, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def edit(self, text, refs, **kwargs):
        entered.set()
        await release.wait()
        return b"result"

    monkeypatch.setattr(ImageAPIClient, "edit", edit)
    instance = plugin(image_api_max_concurrent=1)
    first = asyncio.create_task(instance.generate(Event("/生图 特写 1", [image])))
    await entered.wait()
    second = Event("/生图 特写 2", [image])
    await instance.generate(second)
    assert "上限" in second.text
    release.set()
    await first
    assert instance._active == 0


async def test_cancel_releases_slot_and_generated_reference_files(image, monkeypatch):
    entered = asyncio.Event()
    references = []

    async def edit(self, text, refs, **kwargs):
        references.extend(Path(ref) for ref in refs)
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(ImageAPIClient, "edit", edit)
    instance = plugin()
    task = asyncio.create_task(instance.generate(Event("/生图 特写 1", [image])))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert instance._active == 0
    assert references and all(not path.exists() for path in references)
    assert Path(image.file).exists()


async def test_whitelist_still_applies_to_help_and_generation(image):
    instance = plugin(whitelist_enabled=True, whitelist=["other"])
    for text in ["/生图 帮助", "/生图 特写 1"]:
        event = Event(text, [image])
        await instance.generate(event)
        assert "白名单" in event.text


async def test_unconfigured_provider_and_image_errors_release_slot(image, monkeypatch):
    event = Event("/生图 特写 1", [image])
    await plugin(image_api_key=[]).generate(event)
    assert "配置" in event.text and "正在" not in event.text

    async def broken(self, *args, **kwargs):
        raise main.ImageAPIError("额度不足")

    monkeypatch.setattr(ImageAPIClient, "edit", broken)
    instance = plugin()
    event = Event("/生图 特写 1", [image])
    await instance.generate(event)
    assert "额度不足" in event.text and instance._active == 0


def test_schema_no_longer_requires_game_credentials():
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    assert not (
        {"jx3api_base_url", "jx3api_token", "proxy", "jx3api_ssl_verify"}
        & schema.keys()
    )
