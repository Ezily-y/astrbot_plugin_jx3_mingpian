"""DashScope 原生接口的尺寸转换。

qwen-image 只接受 "<宽>*<高>"；未指定尺寸时省略 size，让接口自己决定。
之前把未指定转成 "2K"，DashScope 会返回 400 InvalidParameter。
"""

import pytest

from conftest import load_plugin_main

load_plugin_main()

from astrbot_plugin_jx3_mingpian.core.dashscope_image import (  # noqa: E402
    DashScopeImageClient,
)


def client(model="qwen-image-3.0"):
    return DashScopeImageClient(
        base_url="https://dashscope.aliyuncs.com/api/v1",
        api_key="test-key",
        model=model,
    )


@pytest.mark.parametrize(
    "size, expected",
    [
        ("1024x1024", "1024*1024"),
        ("2048x1152", "2048*1152"),
        ("2048X880", "2048*880"),
        (" 1152 × 2048 ", "1152*2048"),
    ],
)
def test_openai_size_becomes_star_size(size, expected):
    assert client()._convert_size(size) == expected


@pytest.mark.parametrize("size", ["", "auto", "AUTO", None])
def test_unspecified_size_is_omitted(size):
    assert client()._convert_size(size) is None


def test_qwen_image_never_receives_preset_size():
    assert client("qwen-image-3.0")._convert_size("2K") is None
    assert client("qwen-image-3.0")._convert_size("4K") is None


def test_wan_image_keeps_preset_size():
    assert client("wan2.7-image")._convert_size("2K") == "2K"


async def test_edit_payload_sends_star_size(monkeypatch):
    dashscope = client()
    seen = {}

    async def fake_post(payload):
        seen.update(payload)
        return "https://example.com/image.png"

    async def fake_download(url):
        return b"image-bytes"

    monkeypatch.setattr(dashscope, "_post_for_url", fake_post)
    monkeypatch.setattr(dashscope, "_download", fake_download)
    result = await dashscope.edit("提示词", [], size="2048x1152", retries=0)
    assert result == b"image-bytes"
    assert seen["parameters"]["size"] == "2048*1152"


async def test_edit_payload_omits_size_when_unspecified(monkeypatch):
    dashscope = client()
    seen = {}

    async def fake_post(payload):
        seen.update(payload)
        return "https://example.com/image.png"

    async def fake_download(url):
        return b"image-bytes"

    monkeypatch.setattr(dashscope, "_post_for_url", fake_post)
    monkeypatch.setattr(dashscope, "_download", fake_download)
    await dashscope.edit("破屏而出", [], size="auto", retries=0)
    assert "size" not in seen["parameters"]
