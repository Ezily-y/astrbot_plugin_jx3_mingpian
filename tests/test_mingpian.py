"""名片卡独立插件：单元测试（不发网络请求）。"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import inspect
import json
import re
import sys
import types
from pathlib import Path

import pytest
from aiohttp import FormData

from core.access import (
    is_allowed,
    load_whitelist,
)
from core.card import AVATAR_SIZE, CARD_SIZE, build_payload
from core.image_api import (
    DEFAULT_MODEL,
    ImageAPIClient,
    ImageAPIError,
    normalize_api_base,
)
from core.keypool import KeyPool
from core.mingpian_data import (
    BASE_PROMPT,
    SECT_POEMS,
    SECT_SCENE,
    sect_accent,
    sect_card_fields,
    sect_emblem,
    sect_poem,
)
from core.prompts import (
    DEFAULT_TASK_HINT,
    Prompt,
    empty_prompt_names,
    load_prompts,
    menu_text,
    task_hint,
)
from core.sizes import (
    AUTO,
    DEFAULT_CARD_RATIO,
    RATIO_OPTIONS,
    RATIO_SIZES,
    UNSPECIFIED,
    card_size,
    prompt_size,
)
from core.tujian import (
    FIELDS,
    build_prompt,
    is_empty,
    parse_profile,
)

ROOT = Path(__file__).resolve().parent.parent


TEMPLATE = ROOT / "templates" / "standalone" / "mingpian.html"


REF_IMAGE = ROOT / "templates" / "img" / "剑网3标识.png"


_PKG = "astrbot_plugin_jx3_mingpian"


def _load_plugin_main():
    if f"{_PKG}.main" in sys.modules:
        return sys.modules[f"{_PKG}.main"]
    package = types.ModuleType(_PKG)
    package.__path__ = [str(ROOT)]
    sys.modules[_PKG] = package
    spec = importlib.util.spec_from_file_location(f"{_PKG}.main", ROOT / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{_PKG}.main"] = module
    spec.loader.exec_module(module)
    return module


plugin_main = _load_plugin_main()


class TestNormalizeApiBase:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("https://hub.example.com/v1", "https://hub.example.com/v1"),
            ("https://hub.example.com", "https://hub.example.com/v1"),
            ("https://hub.example.com/", "https://hub.example.com/v1"),
            ("  https://hub.example.com/v1  ", "https://hub.example.com/v1"),
            ("", ""),
            (None, ""),
        ],
    )
    def test_自动补_v1(self, raw, expected):
        assert normalize_api_base(raw) == expected

    @pytest.mark.parametrize("raw", ["hub.example.com", "ftp://x/v1"])
    def test_非法地址报错(self, raw):
        with pytest.raises(ValueError):
            normalize_api_base(raw)


class TestImageClient:
    def test_接口自动拼接(self):
        client = ImageAPIClient("https://hub.example.com", "k", "gpt-image-2.5")
        assert client.endpoint("edits") == "https://hub.example.com/v1/images/edits"

    def test_没配全就不算可用(self):
        assert not ImageAPIClient("", "k", "m").configured
        assert not ImageAPIClient("https://h/v1", "", "m").configured
        assert ImageAPIClient("https://h/v1", "k", "m").configured

    def test_模型名缺省(self):
        assert ImageAPIClient("https://h/v1", "k", "").model == DEFAULT_MODEL

    def _client(self):
        return ImageAPIClient("https://h/v1", "k", "m")

    async def test_取_b64_图片(self):
        blob = b"\x89PNG-fake"
        payload = {"data": [{"b64_json": base64.b64encode(blob).decode()}]}
        assert await self._client()._extract_image(payload) == blob

    async def test_取_data_uri_图片(self):
        blob = b"\x89PNG-fake"
        encoded = base64.b64encode(blob).decode()
        payload = {"data": [{"b64_json": f"data:image/png;base64,{encoded}"}]}
        assert await self._client()._extract_image(payload) == blob

    async def test_容忍折行的_base64(self):
        """服务端按列折行时不得丢弃图片。"""
        blob = b"\x89PNG-fake" * 20
        encoded = base64.b64encode(blob).decode()
        folded = "\n".join(encoded[i : i + 76] for i in range(0, len(encoded), 76))
        payload = {"data": [{"b64_json": folded}]}
        assert await self._client()._extract_image(payload) == blob

    async def test_下载_url_图片(self, monkeypatch):
        blob = b"\x89PNG-from-url"
        client = self._client()

        async def fake_fetch(self, url):
            return blob

        monkeypatch.setattr(ImageAPIClient, "_fetch_image", fake_fetch)
        payload = {"data": [{"url": "https://x/a.png"}]}
        assert await client._extract_image(payload) == blob

    @pytest.mark.parametrize("payload", [{}, {"data": []}, {"data": [{"nothing": 1}]}])
    async def test_没有可用图片就报错(self, payload):
        with pytest.raises(ImageAPIError):
            await self._client()._extract_image(payload)

    @pytest.mark.parametrize(
        "status, keyword",
        [
            (401, "密钥被拒绝"),
            (404, "地址或模型名"),
            (429, "限流"),
            (500, "生图接口报错"),
        ],
    )
    def test_错误提示可读(self, status, keyword):
        assert keyword in ImageAPIClient._explain_http(
            status, '{"error":{"message":"boom"}}'
        )

    @pytest.mark.parametrize(
        "message, stable",
        [
            ("生图密钥被拒绝（HTTP 401）：bad", True),
            ("生图接口地址或模型名不对（HTTP 404）：nf", True),
            ("生图超时（300 秒），请稍后再试", False),
            ("生图接口限流或额度不足（HTTP 429）：slow", False),
        ],
    )
    def test_配置类错误不重试(self, message, stable):
        assert ImageAPIClient._is_stable_failure(message) is stable


class TestSectData:
    def test_有诗的门派都是四句(self):
        for school, lines in SECT_POEMS.items():
            assert len(lines) == 4, school

    def test_每个有诗的门派都有场景描述(self):
        for school in SECT_POEMS:
            assert SECT_SCENE.get(school), f"{school} 缺场景描述"

    def test_门派徽记都在(self):
        for school in SECT_POEMS:
            assert sect_emblem(school) is not None, f"{school} 缺徽记图标"

    def test_别名归一(self):
        assert sect_poem("凌雪阁") == sect_poem("凌雪")
        assert sect_poem("衍天宗") == sect_poem("衍天")
        assert sect_poem("北天药宗") == sect_poem("药宗")

    def test_取色合法且有区分度(self):
        accents = {s: sect_accent(s)[0] for s in SECT_POEMS}
        assert all(a.startswith("#") and len(a) == 7 for a in accents.values())
        assert len(set(accents.values())) >= len(accents) - 2

    def test_未收录门派不炸(self):
        fields = sect_card_fields("不存在的门派")
        assert fields["poem"] == []
        assert fields["accent"].startswith("#")

    def test_提示词占位符齐全(self):
        rendered = BASE_PROMPT.format(
            school="唐门", scene=SECT_SCENE["唐门"], accent="#123456"
        )
        assert "唐门" in rendered and "#123456" in rendered


class TestPayload:
    def _payload(self, school="唐门"):
        return build_payload(
            base_bytes=b"base",
            avatar_bytes=b"avatar",
            school=school,
            nickname="小螺卜头",
            server="飞龙在天",
        )

    def test_图片走_data_uri(self):
        payload = self._payload()
        assert payload["bgImage"].startswith("data:image/png;base64,")
        assert payload["avatar"].startswith("data:image/png;base64,")

    def test_门派字段齐全(self):
        payload = self._payload()
        assert payload["school"] == "唐门"
        assert payload["accent"].startswith("#")
        assert payload["schoolIcon"].startswith("data:image/png;base64,")
        assert len(payload["poem"]) == 4

    def test_模板用到的变量都在_payload_里(self):
        """模板引用的变量必须由 payload 提供，缺失时渲染为空白。"""
        template = TEMPLATE.read_text(encoding="utf-8")
        # {% set x = ... %} 和 {% for x in ... %} 定义的是模板内部变量，不算 payload 的
        internal = set(re.findall(r"\{%-?\s*set\s+([a-zA-Z_][a-zA-Z0-9_]*)", template))
        internal |= set(
            re.findall(r"\{%-?\s*for\s+([a-zA-Z_][a-zA-Z0-9_]*)\s+in", template)
        )

        needed = set()
        for block in re.findall(r"\{\{(.*?)\}\}", template, re.DOTALL):
            if "| default" in block or "|default" in block:
                continue
            match = re.match(r"\s*([a-zA-Z_][a-zA-Z0-9_]*)", block)
            if match:
                needed.add(match.group(1))
        missing = sorted(needed - set(self._payload()) - internal)
        assert not missing, f"模板要这些变量，payload 没给：{missing}"

    def test_payload_没有多余字段(self):
        """反向：payload 里不该留下模板已经不用的字段。"""
        template = TEMPLATE.read_text(encoding="utf-8")
        used: set[str] = set()
        for block in re.findall(r"\{\{(.*?)\}\}", template, re.DOTALL):
            used |= set(re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", block))
        for block in re.findall(r"\{%(.*?)%\}", template, re.DOTALL):
            used |= set(re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", block))
        unused = sorted(set(self._payload()) - used)
        assert not unused, f"payload 多给了模板不用的字段：{unused}"

    def test_字体与标识都嵌进去了(self):
        payload = self._payload()
        assert payload["cardFont"].startswith("data:font/woff2;base64,")
        assert payload["brushFont"].startswith("data:font/ttf;base64,")
        assert payload["logo"].startswith("data:image/png;base64,")


class TestPrompts:
    def test_按顺序连续编号(self):
        prompts = load_prompts(
            [
                {"name": "甲", "prompt": "内容甲"},
                {"name": "乙", "prompt": "内容乙"},
                {"name": "丙", "prompt": "内容丙"},
            ]
        )
        assert [p.index for p in prompts] == [1, 2, 3]
        assert [p.name for p in prompts] == ["甲", "乙", "丙"]

    def test_跳过空内容并重新编号(self):
        prompts = load_prompts(
            [
                {"name": "甲", "prompt": "内容甲"},
                {"name": "空", "prompt": "   "},
                {"name": "丙", "prompt": "内容丙"},
                {"name": "缺字段"},
            ]
        )
        assert [p.index for p in prompts] == [1, 2]
        assert [p.name for p in prompts] == ["甲", "丙"]

    def test_名称留空时用默认名(self):
        prompts = load_prompts([{"name": "", "prompt": "内容"}])
        assert prompts[0].name == "提示词1"

    def test_读取每条的比例(self):
        prompts = load_prompts(
            [
                {"name": "甲", "prompt": "内容甲", "ratio": "21:9"},
                {"name": "乙", "prompt": "内容乙"},
                {"name": "丙", "prompt": "内容丙", "ratio": "   "},
            ]
        )
        assert [p.ratio for p in prompts] == ["21:9", UNSPECIFIED, UNSPECIFIED]

    @pytest.mark.parametrize("raw", [None, "", {}, 123, [1, 2], ["字符串"]])
    def test_配置异常返回空清单(self, raw):
        assert load_prompts(raw) == []

    def test_清单文本(self):
        prompts = load_prompts(
            [{"name": "甲", "prompt": "x"}, {"name": "乙", "prompt": "y"}]
        )
        text = menu_text(prompts)
        assert "名片特写" in text
        assert "1. 甲" in text
        assert "2. 乙" in text
        assert "/生图" in text and "@机器人 生图" in text and "特写" in text
        assert "自动选" not in text

    def test_空内容的提示词在帮助里列出(self):
        raw = [
            {"name": "甲", "prompt": "x"},
            {"name": "测试提示词", "prompt": ""},
            {"prompt": "  "},
            {"name": "乙", "prompt": "y"},
        ]
        prompts = load_prompts(raw)
        skipped = empty_prompt_names(raw)
        assert [prompt.name for prompt in prompts] == ["甲", "乙"]
        assert skipped == ["测试提示词", "未命名条目（第 3 条）"]
        text = menu_text(prompts, skipped=skipped)
        assert "内容为空、未启用的条目" in text
        assert "测试提示词" in text
        assert "1. 甲" in text and "2. 乙" in text

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("", DEFAULT_TASK_HINT),
            (None, DEFAULT_TASK_HINT),
            ("  ", DEFAULT_TASK_HINT),
            ("自定义", "自定义"),
        ],
    )
    def test_等待提示回退默认(self, raw, expected):
        assert task_hint(raw) == expected


class TestArtFormat:
    """角色形象图按真实字节定后缀。后缀写错时接口会把它当坏图丢掉，
    出图就变成与角色无关的随机生成。"""

    @pytest.mark.parametrize(
        "art, expected",
        [
            (b"\x89PNG\r\n\x1a\n" + b"x" * 20, ".png"),
            (b"\xff\xd8\xff" + b"x" * 20, ".jpg"),
            (b"RIFF\x00\x00\x00\x00WEBP" + b"x" * 20, ".webp"),
            (b"GIF89a" + b"x" * 20, ".gif"),
            (b"<!DOCTYPE html><html><body>403</body></html>", ""),
            (b'{"code":500,"msg":"error"}', ""),
            (b"", ""),
        ],
    )
    def test_按文件头判断后缀(self, art, expected):
        from core.card import _art_suffix

        assert _art_suffix(art) == expected

    def test_后缀随真实格式(self, tmp_path):
        from core.card import _art_path

        path = _art_path(str(tmp_path), b"\xff\xd8\xff" + b"x" * 20)
        assert path.name == "art.jpg"
        assert path.read_bytes().startswith(b"\xff\xd8\xff")

    def test_认不出图片就中止(self, tmp_path):
        from core.card import _art_path

        with pytest.raises(ImageAPIError):
            _art_path(str(tmp_path), b"<!DOCTYPE html><html>403</html>")

    async def test_参考图的_content_type_按后缀(self, monkeypatch, tmp_path):
        seen: dict = {}
        original = FormData.add_field

        def spy(self, name, value, **kwargs):
            if name == "image[]":
                seen["content_type"] = kwargs.get("content_type")
            return original(self, name, value, **kwargs)

        monkeypatch.setattr(FormData, "add_field", spy)

        async def fake_post(self, url, key, *, form):
            return {"data": [{"b64_json": base64.b64encode(b"png").decode()}]}

        monkeypatch.setattr(ImageAPIClient, "_post", fake_post)
        ref = tmp_path / "art.jpg"
        ref.write_bytes(b"\xff\xd8\xff" + b"x" * 20)
        await ImageAPIClient("https://hub.example.com", "k").edit(
            "提示词", [ref], size="2048x880"
        )
        assert seen["content_type"] == "image/jpeg"


class TestReferenceImages:
    """名片特写只传角色图；名片卡额外传卡面样式参考图。"""

    def _source(self):
        from core.card import CardSource

        return CardSource(
            server="飞龙在天",
            nickname="小螺卜头",
            school="唐门",
            art=b"\x89PNG\r\n\x1a\n-art",
        )

    async def _run(self, image, **kwargs):
        from core.card import generate_base

        return await generate_base(image, self._source(), "提示词", **kwargs)

    async def test_默认带卡面参考图(self):
        seen = {}

        class FakeImage:
            async def edit(self, prompt, images, **kwargs):
                seen["images"] = [Path(p).name for p in images]
                return b"ok"

        await self._run(FakeImage())
        assert len(seen["images"]) == 2
        assert seen["images"][0] == "卡面参考.png"
        assert seen["images"][1] == "art.png"

    async def test_关闭时只传角色图(self):
        seen = {}

        class FakeImage:
            async def edit(self, prompt, images, **kwargs):
                seen["images"] = [Path(p).name for p in images]
                return b"ok"

        await self._run(FakeImage(), with_card_ref=False)
        assert seen["images"] == ["art.png"]

    async def test_特写走关闭分支(self, monkeypatch):
        from core import card as card_mod

        seen = {}

        async def fake_generate_base(image, source, prompt, **kwargs):
            seen.update(kwargs)
            return b"ok"

        monkeypatch.setattr(card_mod, "generate_base", fake_generate_base)

        class FakeImage:
            configured = True

        prompt = Prompt(index=1, name="甲", text="提示词")
        await card_mod.build_closeup(FakeImage(), self._source(), prompt)
        assert seen.get("with_card_ref") is False


class TestConfigSchema:
    def _schema(self) -> dict:
        import json

        return json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))

    def test_等待提示配置(self):
        item = self._schema()["mingpian_task_hint"]
        assert item["type"] == "text"
        assert item["default"] == DEFAULT_TASK_HINT

    def test_提示词为模板列表(self):
        item = self._schema()["mingpian_prompts"]
        assert item["type"] == "template_list"
        assert item["templates"]["prompt_item"]["display_item"] == "name"
        assert set(item["templates"]["prompt_item"]["items"]) == {
            "name",
            "ratio",
            "prompt",
        }

    def test_默认提示词可被解析(self):
        defaults = self._schema()["mingpian_prompts"]["default"]
        prompts = load_prompts(defaults)
        assert len(prompts) == 5
        assert [p.name for p in prompts] == [
            "双眼特写",
            "双眼特写（电影黑条）",
            "多视角海报",
            "手办化",
            "破屏而出",
        ]
        assert [p.ratio for p in prompts] == [
            "21:9",
            "21:9",
            "21:9",
            UNSPECIFIED,
            UNSPECIFIED,
        ]
        # 前四条是长文提示词；破屏而出是有意留短的指令
        assert all(len(p.text) > 500 for p in prompts[:4])
        assert prompts[4].text == "破屏而出"


class TestKeyPool:
    def test_单条字符串按一条处理(self):
        assert KeyPool("k1").keys == ["k1"]
        assert len(KeyPool("k1")) == 1

    @pytest.mark.parametrize("raw", [None, "", [], ["", "  "], [None]])
    def test_空来源视为无密钥(self, raw):
        pool = KeyPool(raw)
        assert not pool
        assert pool.order() == []

    def test_忽略空项与前后空白(self):
        assert KeyPool([" k1 ", "", "k2", None]).keys == ["k1", "k2"]

    def test_从副本构造保留游标(self):
        pool = KeyPool(["k1", "k2", "k3"])
        pool.take_order()
        copy = KeyPool(pool)
        assert copy.order() == ["k2", "k3", "k1"]

    def test_取顺序时推进游标(self):
        pool = KeyPool(["k1", "k2", "k3"])
        assert pool.order() == ["k1", "k2", "k3"]
        assert pool.take_order() == ["k1", "k2", "k3"]
        assert pool.order() == ["k2", "k3", "k1"]
        assert pool.take_order() == ["k2", "k3", "k1"]
        assert pool.order() == ["k3", "k1", "k2"]

    def test_并发取键各得不同起点(self):
        """并发请求必须从不同密钥起步，否则密钥池在最需要分流时不生效。"""
        pool = KeyPool(["k1", "k2", "k3"])
        assert [pool.take_order()[0] for _ in range(4)] == ["k1", "k2", "k3", "k1"]

    def test_sync_保留游标位置(self):
        pool = KeyPool(["k1", "k2", "k3"])
        pool.take_order()
        pool.sync(["k1", "k2", "k3"])
        assert pool.order() == ["k2", "k3", "k1"]

    def test_sync_缩短后游标取模(self):
        pool = KeyPool(["k1", "k2", "k3"])
        pool.take_order()
        pool.take_order()
        pool.sync(["k1", "k2"])
        assert pool.order() in (["k1", "k2"], ["k2", "k1"])

    def test_sync_清空后无密钥(self):
        pool = KeyPool(["k1", "k2"])
        pool.sync([])
        assert not pool and pool.order() == []

    @pytest.mark.parametrize(
        "message, expected",
        [
            ("生图密钥被拒绝（HTTP 401）：bad", True),
            ("JX3API 令牌无效：expired", True),
            ("生图接口限流或额度不足（HTTP 429）：slow", True),
            ("生图超时（300 秒），请稍后再试", False),
            ("生图接口地址或模型名不对（HTTP 404）：nf", False),
        ],
    )
    def test_判定是否值得换密钥(self, message, expected):
        assert KeyPool.is_credential_failure(message) is expected


class TestMultiKeyRotation:
    async def test_生图第一条失败换下一条(self, monkeypatch):
        client = ImageAPIClient("https://hub.example.com", ["bad", "good"])
        tried = []

        async def fake_post(self, url, key, *, form):
            tried.append(key)
            if key == "bad":
                raise ImageAPIError("生图密钥被拒绝（HTTP 401）：bad key")
            return {"data": [{"b64_json": base64.b64encode(b"png").decode()}]}

        monkeypatch.setattr(ImageAPIClient, "_post", fake_post)
        assert await client.edit("提示词", [REF_IMAGE], size=CARD_SIZE) == b"png"
        assert tried == ["bad", "good"]

    async def test_生图成功后游标前进(self, monkeypatch):
        client = ImageAPIClient("https://hub.example.com", ["k1", "k2"])

        async def fake_post(self, url, key, *, form):
            return {"data": [{"b64_json": base64.b64encode(b"png").decode()}]}

        monkeypatch.setattr(ImageAPIClient, "_post", fake_post)
        await client.edit("提示词", [REF_IMAGE], size=CARD_SIZE)
        assert client.keys.order() == ["k2", "k1"]

    async def test_并发生图从不同密钥起步(self, monkeypatch):
        """底卡与头像并发发出，两次请求必须各用一条密钥。"""
        client = ImageAPIClient("https://hub.example.com", ["k1", "k2"])
        seen: list[str] = []

        async def fake_post(self, url, key, *, form):
            seen.append(key)
            return {"data": [{"b64_json": base64.b64encode(b"png").decode()}]}

        monkeypatch.setattr(ImageAPIClient, "_post", fake_post)
        await asyncio.gather(
            client.edit("提示词", [REF_IMAGE], size=CARD_SIZE),
            client.edit("提示词", [REF_IMAGE], size=CARD_SIZE),
        )
        assert seen == ["k1", "k2"]

    async def test_生图非凭据错误不换密钥(self, monkeypatch):
        client = ImageAPIClient("https://hub.example.com", ["k1", "k2"])
        tried = []

        async def fake_post(self, url, key, *, form):
            tried.append(key)
            raise ImageAPIError("生图接口地址或模型名不对（HTTP 404）：nf")

        monkeypatch.setattr(ImageAPIClient, "_post", fake_post)
        with pytest.raises(ImageAPIError):
            await client.edit("提示词", [REF_IMAGE], size=CARD_SIZE)
        assert tried == ["k1"]


class TestWhitelist:
    def test_未启用时放行(self):
        assert is_allowed(False, {"u1"}, "u2") is True
        assert is_allowed(None, set(), "u2") is True

    def test_启用后仅名单内放行(self):
        assert is_allowed(True, {"u1", "u2"}, "u1") is True
        assert is_allowed(True, {"u1"}, "u9") is False

    def test_启用但名单为空时全部拒绝(self):
        assert is_allowed(True, set(), "u1") is False

    def test_编号前后空白被忽略(self):
        assert is_allowed(True, load_whitelist([" u1 ", ""]), "u1") is True

    # --- UMO ---

    def test_UMO_命中整个会话(self):
        umo = "aiocqhttp:GroupMessage:987654321"
        assert is_allowed(True, {umo}, "u9", umo) is True

    def test_UMO_不匹配其他会话(self):
        allowed = {"aiocqhttp:GroupMessage:987654321"}
        assert (
            is_allowed(True, allowed, "u9", "aiocqhttp:GroupMessage:111111111") is False
        )

    def test_UMO_区分消息类型(self):
        allowed = {"aiocqhttp:GroupMessage:123"}
        assert is_allowed(True, allowed, "u9", "aiocqhttp:FriendMessage:123") is False

    def test_UMO_区分平台(self):
        allowed = {"aiocqhttp:GroupMessage:123"}
        assert is_allowed(True, allowed, "u9", "telegram:GroupMessage:123") is False

    def test_用户ID与UMO可混用(self):
        allowed = {"u1", "aiocqhttp:GroupMessage:987654321"}
        assert is_allowed(True, allowed, "u1", "aiocqhttp:FriendMessage:1") is True
        assert (
            is_allowed(True, allowed, "u9", "aiocqhttp:GroupMessage:987654321") is True
        )
        assert is_allowed(True, allowed, "u9", "aiocqhttp:GroupMessage:222") is False

    def test_UMO_条目不会被当成用户ID(self):
        umo = "aiocqhttp:GroupMessage:987654321"
        assert is_allowed(True, {umo}, umo, "") is False

    def test_事件无UMO属性时不报错(self):
        class Bare:
            pass

        assert (
            is_allowed(True, {"u1"}, "u1", getattr(Bare(), "unified_msg_origin", ""))
            is True
        )

    @pytest.mark.parametrize(
        "raw, expected",
        [
            (["a", "b"], {"a", "b"}),
            (["a", "a"], {"a"}),
            (["", "  ", None], set()),
            ("a", set()),
            (None, set()),
            ([123, "456"], {"123", "456"}),
        ],
    )
    def test_规整名单(self, raw, expected):
        assert load_whitelist(raw) == expected


class TestSizePassthrough:
    async def test_尺寸与响应格式都传给接口(self, monkeypatch):
        client = ImageAPIClient("https://hub.example.com", "k")
        seen = {}
        original = FormData.add_field

        def spy(self, name, value, **kwargs):
            seen[name] = value
            return original(self, name, value, **kwargs)

        monkeypatch.setattr(FormData, "add_field", spy)

        async def fake_post(self, url, key, *, form):
            return {"data": [{"b64_json": base64.b64encode(b"png").decode()}]}

        monkeypatch.setattr(ImageAPIClient, "_post", fake_post)
        await client.edit("提示词", [REF_IMAGE], size="2048x880")
        assert seen["size"] == "2048x880"
        assert seen["response_format"] == "b64_json"


class TestCleanup:
    def test_删除本地文件(self, tmp_path):
        target = tmp_path / "card.png"
        target.write_bytes(b"x")
        plugin_main.JX3MingpianPlugin._cleanup(str(target))
        assert not target.exists()

    def test_删除_file_协议路径(self, tmp_path):
        target = tmp_path / "card.png"
        target.write_bytes(b"x")
        plugin_main.JX3MingpianPlugin._cleanup(target.as_uri())
        assert not target.exists()

    @pytest.mark.parametrize(
        "value",
        ["", None, "http://img/card.png", "https://img/card.png", "base64://AAAA"],
    )
    def test_远程地址与空值跳过(self, value):
        plugin_main.JX3MingpianPlugin._cleanup(value)

    def test_不存在的路径不报错(self, tmp_path):
        plugin_main.JX3MingpianPlugin._cleanup(str(tmp_path / "nope.png"))


class TestTujianProfile:
    """图鉴字段解析。只依赖角色名与立绘，不碰 JX3API。"""

    def test_解析完整_JSON(self):
        raw = json.dumps(
            {
                "quote": "月光并不冰冷。",
                "alias": "月下的祈祷者",
                "race": "人类",
                "age": "18",
                "height": "162 cm",
                "identity": "旅者",
                "camp": "中立",
                "temperament": "清冷",
                "likes": "古书",
                "weapon": "折扇",
                "pros": ["温柔", "坚韧"],
                "cons": ["敏感"],
                "points": ["一", "二", "三", "四", "五"],
            }
        )
        profile = parse_profile(raw)
        assert profile["alias"] == "月下的祈祷者"
        assert profile["quote"] == "月光并不冰冷。"
        assert profile["cons"] == ["敏感"]
        assert len(profile["points"]) == 4, "超出约定条数应截断"
        assert is_empty(profile) is False

    def test_容忍代码块围栏与前后废话(self):
        raw = '好的，结果如下：\n```json\n{"quote": "x", "alias": "y"}\n```\n希望满意'
        profile = parse_profile(raw)
        assert profile["quote"] == "x"
        assert profile["alias"] == "y"

    @pytest.mark.parametrize(
        "raw", ["", "没有 JSON", "{坏 json}", "[1,2,3]", None, "{}"]
    )
    def test_格式不合不中断出图(self, raw):
        profile = parse_profile(raw)
        assert profile["alias"] == ""
        assert profile["pros"] == []
        assert profile["points"] == []
        assert is_empty(profile) is True

    def test_字段类型异常不炸(self):
        raw = '{"alias": {"a": 1}, "pros": "只有一条", "points": [null, "", "有效"]}'
        profile = parse_profile(raw)
        assert profile["alias"] == ""
        assert profile["pros"] == ["只有一条"]
        assert profile["points"] == ["有效"]

    def test_提示词含角色名与全部字段(self):
        text = build_prompt("小螺卜头")
        assert "小螺卜头" in text
        for key, label in FIELDS:
            assert f'"{key}"' in text
            assert label in text

    def test_payload_只带非空字段(self):
        from core.tujian import payload

        profile = parse_profile('{"alias": "甲", "race": "", "quote": "q"}')
        data = payload("小螺卜头", profile, b"\x89PNG\r\n\x1a\n-art")
        assert data["fields"] == [{"label": "别称", "value": "甲"}]
        assert data["portrait"].startswith("data:image/png;base64,")
        assert data["name"] == "小螺卜头"


class TestSizes:
    """比例映射只在 core/sizes.py 一处；只发 OpenAI 原生的 size 字段。"""

    def test_名片卡尺寸(self):
        assert CARD_SIZE == "2048x1152"
        assert AVATAR_SIZE == "1024x1024"

    def test_名片卡比例缺失时退回默认(self):
        assert card_size("") == "2048x1152"
        assert card_size(None) == "2048x1152"
        assert card_size("不指定") == "2048x1152"
        assert card_size("瞎写") == "2048x1152"

    def test_名片卡比例可配(self):
        assert card_size("21:9") == "2048x880"
        assert card_size("1:1") == "1024x1024"

    def test_特写不指定时交给接口(self):
        assert prompt_size(UNSPECIFIED) == AUTO
        assert prompt_size("") == AUTO
        assert prompt_size(None) == AUTO
        assert prompt_size("瞎写") == AUTO

    def test_特写比例(self):
        assert prompt_size("21:9") == "2048x880"
        assert prompt_size("16:9") == "2048x1152"

    def test_比例映射都过_OpenAI_约束(self):
        """宽高被 16 整除、最长边不超 3840、总像素 655360~8294400。

        比例容差 0.01：21:9 用的是 2048x880（2.327），不是精确的 7:3。
        """
        for ratio, size in RATIO_SIZES.items():
            width, height = (int(v) for v in size.split("x"))
            left, right = (int(v) for v in ratio.split(":"))
            assert width % 16 == 0 and height % 16 == 0, size
            assert max(width, height) <= 3840, size
            assert 655360 <= width * height <= 8294400, size
            assert abs(width / height - left / right) < 0.01, (ratio, size)

    def test_配置下拉与映射表同步(self):
        """schema 的下拉选项与 core/sizes.py 的映射表必须一一对应。"""
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        items = schema["mingpian_prompts"]["templates"]["prompt_item"]["items"]
        assert items["ratio"]["options"] == list(RATIO_OPTIONS)
        assert items["ratio"]["default"] == UNSPECIFIED
        assert schema["mingpian_card_ratio"]["options"] == list(RATIO_SIZES)
        assert schema["mingpian_card_ratio"]["default"] == DEFAULT_CARD_RATIO

    def test_生图接口不设默认尺寸(self):
        """size 为必填，避免调用方漏传时静默用错尺寸。"""
        params = inspect.signature(ImageAPIClient.edit).parameters
        assert params["size"].default is inspect.Parameter.empty

    def test_配置里没有尺寸项(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        assert "card_size" not in schema
        items = schema["mingpian_prompts"]["templates"]["prompt_item"]["items"]
        assert "size" not in items
