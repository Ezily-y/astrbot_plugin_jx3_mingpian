"""模型发现、方案保存与实际生图测试；HTTP 使用本地服务器，不请求真实生图服务。"""

import asyncio
import base64
from io import BytesIO
from types import SimpleNamespace

import pytest
from aiohttp import web
from conftest import load_plugin_main
from PIL import Image

main = load_plugin_main()

from astrbot_plugin_jx3_mingpian.core.image_api import (  # noqa: E402
    ImageAPIClient,
    ImageAPIError,
)


@pytest.fixture
async def model_server():
    requests = []

    async def models(request):
        key = request.headers.get("Authorization")
        requests.append((request.path, key))
        if key == "Bearer bad-key":
            return web.json_response(
                {"error": {"message": "bad-key denied"}}, status=401
            )
        return web.json_response(
            {
                "data": [
                    {"id": "image-beta"},
                    {"id": "chat-alpha"},
                    {"id": "image-beta"},
                    {},
                ]
            }
        )

    app = web.Application()
    app.router.add_get("/v1/models", models)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield f"http://127.0.0.1:{runner.addresses[0][1]}", requests
    finally:
        await runner.cleanup()


async def test_models_uses_real_endpoint_and_falls_back_without_changing_generation_cursor(
    model_server,
):
    base, requests = model_server
    client = ImageAPIClient(base, ["bad-key", "good-key"])
    before = client.keys.order()
    models = await client.list_models()
    assert models == ["chat-alpha", "image-beta"]
    assert requests == [
        ("/v1/models", "Bearer bad-key"),
        ("/v1/models", "Bearer good-key"),
    ]
    assert client.keys.order() == before


class Config(dict):
    def __init__(self, **values):
        super().__init__(values)
        self.saved = []

    def save_config(self):
        self.saved.append(dict(self))


def make_plugin(**values):
    conf = Config(
        image_api_base_url="https://saved.example/v1",
        image_api_key=["saved-secret"],
        image_api_model="old-model",
        **values,
    )
    return main.JX3MingpianPlugin(None, conf)


def settings(instance):
    assert hasattr(instance, "settings"), "插件应提供生图设置服务"
    return instance.settings


async def first_profile(instance):
    data = await settings(instance).get_settings()
    assert data["profiles"], "旧配置应迁移出一个默认方案"
    return data["profiles"][0]


async def test_settings_never_returns_saved_keys():
    instance = make_plugin()
    result = await settings(instance).get_settings()
    assert len(result["profiles"]) == 1
    profile = result["profiles"][0]
    assert profile["model"] == "old-model"
    assert profile["base_url"] == "https://saved.example/v1"
    assert profile["key_count"] == 1
    assert "saved-secret" not in str(result)


async def test_unsaved_draft_fetches_models_and_preserves_configuration(model_server):
    base, requests = model_server
    instance = make_plugin()
    original = dict(instance.conf)
    result = await settings(instance).models(
        {"base_url": base, "api_key": ["good-key"]}
    )
    assert result["models"] == ["chat-alpha", "image-beta"]
    assert requests == [("/v1/models", "Bearer good-key")]
    assert dict(instance.conf) == original and not instance.conf.saved


async def test_model_discovery_does_not_require_selecting_a_model_first(model_server):
    base, _ = model_server
    result = await settings(make_plugin()).models(
        {"base_url": base, "api_key": ["good-key"], "model": ""}
    )
    assert result["models"] == ["chat-alpha", "image-beta"]


async def test_connectivity_reports_model_presence_without_claiming_image_support(
    model_server,
):
    base, _ = model_server
    result = await settings(make_plugin()).connectivity(
        {
            "base_url": base,
            "api_key": ["good-key"],
            "model": "image-beta",
        }
    )
    assert result["reachable"] is True and result["model_listed"] is True
    assert result["image_tested"] is False and result["elapsed_ms"] >= 0


async def test_saved_profile_keys_are_reused_when_keys_are_omitted(model_server):
    base, requests = model_server
    instance = make_plugin()
    profile = await first_profile(instance)
    result = await settings(instance).models(
        {"id": profile["id"], "base_url": base}
    )
    assert result["models"] == ["chat-alpha", "image-beta"]
    assert requests == [("/v1/models", "Bearer saved-secret")]


async def test_save_keeps_keys_when_omitted_and_applies_without_reload():
    instance = make_plugin()
    profile = await first_profile(instance)
    result = await settings(instance).save(
        {
            "profiles": [
                {
                    "id": profile["id"],
                    "name": profile["name"],
                    "base_url": "https://new.example",
                    "model": "custom-image",
                }
            ],
            "active": 0,
            "auto_fallback": True,
        }
    )
    assert (
        instance.conf["image_api_model"]
        == instance._image_client().model
        == "custom-image"
    )
    assert instance.conf["image_api_base_url"] == "https://new.example/v1"
    assert instance.conf["image_api_key"] == ["saved-secret"]
    assert instance.conf["image_api_profiles"][0]["id"] == profile["id"]
    assert len(instance.conf.saved) == 1 and "saved-secret" not in str(result)


async def test_legacy_profile_id_survives_first_save():
    instance = make_plugin()
    first = await first_profile(instance)
    assert first["id"] == "migrated-0"
    await settings(instance).save(
        {
            "profiles": [
                {
                    "id": first["id"],
                    "name": first["name"],
                    "base_url": first["base_url"],
                    "model": first["model"],
                }
            ]
        }
    )
    again = await first_profile(instance)
    assert again["id"] == first["id"]


async def test_save_preserves_order_and_active_index():
    instance = make_plugin()
    profile = await first_profile(instance)
    result = await settings(instance).save(
        {
            "profiles": [
                {
                    "id": profile["id"],
                    "name": "第二",
                    "base_url": "https://second.example/v1",
                    "model": "second-model",
                },
                {
                    "id": "brand-new",
                    "name": "第一",
                    "base_url": "https://first.example/v1",
                    "model": "first-model",
                    "api_key": ["first-key"],
                },
            ],
            "active": 1,
            "auto_fallback": False,
        }
    )
    assert [item["name"] for item in result["profiles"]] == ["第二", "第一"]
    assert result["active"] == 1
    assert result["auto_fallback"] is False
    assert instance.conf["image_api_active_profile"] == 1
    assert instance.conf["image_api_auto_fallback"] is False
    assert instance._image_client().model == "first-model"


async def test_failed_save_restores_active_configuration():
    instance = make_plugin()
    original = dict(instance.conf)
    profile = await first_profile(instance)

    def fail():
        raise OSError("disk full")

    instance.conf.save_config = fail
    with pytest.raises(OSError):
        await settings(instance).save(
            {
                "profiles": [
                    {
                        "id": profile["id"],
                        "name": profile["name"],
                        "base_url": profile["base_url"],
                        "model": "new",
                    }
                ]
            }
        )
    assert dict(instance.conf) == original


@pytest.mark.parametrize(
    "profiles",
    [
        [],
        [{"name": "x", "base_url": "bad", "model": "m", "api_key": ["k"]}],
        [
            {
                "name": "x",
                "base_url": "https://ok.example/v1",
                "model": "",
                "api_key": ["k"],
            }
        ],
        [{"name": "x", "base_url": "https://ok.example/v1", "model": "m"}],
        [
            {
                "name": "x",
                "base_url": "https://ok.example/v1",
                "model": "m",
                "api_key": "secret",
            }
        ],
    ],
)
async def test_invalid_settings_do_not_mutate_config(profiles):
    instance = make_plugin()
    original = dict(instance.conf)
    with pytest.raises(ValueError):
        await settings(instance).save({"profiles": profiles})
    assert dict(instance.conf) == original and not instance.conf.saved


def png():
    output = BytesIO()
    Image.new("RGB", (32, 32), "blue").save(output, "PNG")
    return output.getvalue()


async def test_actual_image_test_is_explicit_background_job_and_shares_limit(
    monkeypatch,
):
    instance = make_plugin(image_api_max_concurrent=1)
    service = settings(instance)
    profile = await first_profile(instance)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def edit(self, prompt, refs, **kwargs):
        calls.append((self.model, kwargs["retries"]))
        assert next(iter(refs)).is_file()
        entered.set()
        await release.wait()
        return png()

    monkeypatch.setattr(ImageAPIClient, "edit", edit)
    assert (await service.test_status())["status"] == "idle"
    result = await service.start_test({"id": profile["id"], "model": "new-image"})
    assert result["status"] == "running"
    await entered.wait()
    assert instance._active == 1
    with pytest.raises(ValueError, match="上限|测试中"):
        await service.start_test({"id": profile["id"]})
    release.set()
    await service.test_task
    result = await service.test_status()
    assert result["status"] == "success" and result["model"] == "new-image"
    assert base64.b64decode(result["image"].split(",", 1)[1]) == png()
    assert calls == [("new-image", 0)] and instance._active == 0


async def test_test_failure_redacts_secrets_and_releases_limit(monkeypatch):
    instance = make_plugin()
    service = settings(instance)
    profile = await first_profile(instance)

    async def fail(self, *args, **kwargs):
        raise ImageAPIError("saved-secret rejected")

    monkeypatch.setattr(ImageAPIClient, "edit", fail)
    await service.start_test({"id": profile["id"]})
    await service.test_task
    result = await service.test_status()
    assert result["status"] == "error" and "saved-secret" not in str(result)
    assert instance._active == 0


async def test_unload_immediately_after_start_releases_reserved_slot():
    instance = make_plugin()
    profile = await first_profile(instance)
    await settings(instance).start_test({"id": profile["id"]})
    await instance.terminate()
    assert instance._active == 0
    assert (await instance.settings.test_status())["status"] == "cancelled"


async def test_unloaded_plugin_cannot_start_a_new_paid_test():
    instance = make_plugin()
    profile = await first_profile(instance)
    await instance.terminate()
    with pytest.raises(ValueError, match="停用|卸载|关闭"):
        await instance.settings.start_test({"id": profile["id"]})
    assert instance._active == 0


async def test_plugin_registers_dashboard_routes():
    routes = []
    context = SimpleNamespace(
        register_web_api=lambda route, handler, methods, desc: routes.append(
            (route, methods)
        )
    )
    main.JX3MingpianPlugin(context, Config())
    root = "/astrbot_plugin_jx3_mingpian/"
    assert (root + "settings", ["GET"]) in routes
    assert (root + "settings", ["POST"]) in routes
    assert (root + "models", ["POST"]) in routes
    assert (root + "connectivity", ["POST"]) in routes
    assert (root + "image-test", ["POST"]) in routes
    assert (root + "image-test", ["GET"]) in routes
