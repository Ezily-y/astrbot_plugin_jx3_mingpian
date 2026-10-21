"""本机 Codex 协议适配，模拟子进程协议而不消耗账户生图额度。"""

import asyncio
import base64
import json
import sys
from io import BytesIO

import pytest
from conftest import load_plugin_main
from PIL import Image
from core.backend import CodexFirstImageClient
from core.image_api import ImageAPIError

main = load_plugin_main()


def client(**conf):
    plugin = main.JX3MingpianPlugin(None, {
        "image_backend": "本机 Codex CLI", "codex_executable": sys.executable,
        **conf,
    })
    return plugin._image_client()


def png():
    data = BytesIO()
    Image.new("RGB", (32, 32), "blue").save(data, "PNG")
    return data.getvalue()


class FakeProcess:
    def __init__(self, *, image=True):
        self.returncode = None
        self.stdout = asyncio.StreamReader(limit=64 * 1024 * 1024)
        self.stdin = self
        self.requests = []
        self.image = image
        self.stopped = False

    def emit(self, data):
        self.stdout.feed_data((json.dumps(data) + "\n").encode())

    def write(self, blob):
        data = json.loads(blob)
        self.requests.append(data)
        method = data["method"]
        if "id" not in data:
            return
        result = {}
        if method == "account/read":
            result = {"account": {"type": "chatgpt"}, "requiresOpenaiAuth": True}
        elif method == "modelProvider/capabilities/read":
            result = {"imageGeneration": True}
        elif method == "model/list":
            result = {"data": [{"model": "local-agent-model"}], "nextCursor": None}
        elif method == "thread/start":
            result = {"thread": {"id": "thread-1"}}
        elif method == "turn/start":
            result = {"turn": {"id": "turn-1"}}
        self.emit({"id": data["id"], "result": result})
        if method == "turn/start":
            if self.image:
                self.emit({"method": "item/completed", "params": {"item": {"type": "imageGeneration", "status": "completed", "result": base64.b64encode(png()).decode()}}})
            self.emit({"method": "turn/completed", "params": {"turn": {"id": "turn-1", "status": "completed", "error": None}}})

    async def drain(self):
        pass

    def terminate(self):
        self.stopped = True
        self.returncode = 0
        self.stdout.feed_eof()

    def kill(self):
        self.terminate()

    async def wait(self):
        return self.returncode or 0


@pytest.fixture
def process(monkeypatch):
    processes = []

    async def spawn(*args, **kwargs):
        assert args[1:4] == ("app-server", "--listen", "stdio://")
        assert kwargs["limit"] >= 32 * 1024 * 1024
        proc = FakeProcess()
        processes.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    return processes


async def test_codex_backend_does_not_require_api_key_and_reuses_native_account(process):
    backend = client()
    assert backend.configured
    result = await backend.probe()
    assert result["authenticated"] and result["image_generation"]
    assert result["models"] == ["local-agent-model"]
    assert processes_closed(process)


def processes_closed(processes):
    return bool(processes) and all(proc.stopped for proc in processes)


async def test_codex_edits_use_attached_images_and_only_return_native_image_results(process, tmp_path):
    reference = tmp_path / "reference.png"
    reference.write_bytes(png())
    backend = client(codex_model="local-agent-model")
    result = await backend.edit("双眼特写", [reference], size="2048x880")
    assert result == png()
    proc = process[0]
    start = next(msg["params"] for msg in proc.requests if msg["method"] == "thread/start")
    assert start["model"] == "local-agent-model" and start["ephemeral"] is True
    assert start["sandbox"] == "workspace-write" and start["approvalPolicy"] == "never"
    turn = next(msg["params"] for msg in proc.requests if msg["method"] == "turn/start")
    assert any(item["type"] == "localImage" for item in turn["input"])
    assert "双眼特写" in turn["input"][0]["text"] and "2048x880" in turn["input"][0]["text"]
    assert reference.exists() and processes_closed(process)


async def test_codex_text_success_is_not_image_success(process, tmp_path, monkeypatch):
    reference = tmp_path / "reference.png"
    reference.write_bytes(png())
    original = FakeProcess.__init__

    def no_image(self):
        original(self, image=False)

    monkeypatch.setattr(FakeProcess, "__init__", no_image)
    with pytest.raises(main.ImageAPIError, match="图片|生图"):
        await client().edit("画图", [reference], size="1024x1024")
    assert processes_closed(process)


async def test_codex_missing_executable_is_actionable():
    backend = client(codex_executable="missing-codex-jx3-test-binary")
    assert not backend.configured
    with pytest.raises(main.ImageAPIError, match="Codex|codex"):
        await backend.probe()


async def test_codex_first_falls_back_to_api_after_one_codex_error():
    calls = []

    class Stub:
        configured = True
        model = "stub-model"

        def safe_error(self, error):
            return str(error)

        async def edit(self, *args, **kwargs):
            calls.append("codex")
            raise ImageAPIError("codex unavailable")

    class APIStub:
        configured = True
        model = "api-model"

        async def edit(self, *args, **kwargs):
            calls.append("api")
            return png()

    result = await CodexFirstImageClient(Stub(), APIStub()).edit(
        "test", [], size="1024x1024", retries=0
    )
    assert result == png() and calls == ["codex", "api"]
