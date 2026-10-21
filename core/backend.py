"""创建与当前配置对应的生图客户端，支持多方案降级。"""

from __future__ import annotations

from astrbot.api import logger

from .codex_image import CodexImageClient
from .dashscope_image import DashScopeImageClient
from .image_api import DEFAULT_TIMEOUT, ImageAPIClient, ImageAPIError
from .keypool import KeyPool
from .profiles import ImageProfile, ProfileManager

API_BACKEND = "OpenAI 兼容接口"
CODEX_BACKEND = "本机 Codex CLI"


class CodexFirstImageClient:
    """优先调用 Codex；单次请求失败后才回退到兼容接口。"""

    def __init__(self, codex: CodexImageClient, api: ImageAPIClient):
        self.codex = codex
        self.api = api
        self.model = codex.model

    @property
    def configured(self) -> bool:
        return self.codex.configured or self.api.configured

    async def list_models(self) -> list[str]:
        if self.codex.configured:
            try:
                return await self.codex.list_models()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Codex 模型列表获取失败，改用接口模型列表：%s", self.codex.safe_error(exc))
        return await self.api.list_models()

    async def edit(self, prompt, images, *, size, quality="high", retries=1) -> bytes:
        codex_error = None
        if self.codex.configured:
            try:
                return await self.codex.edit(
                    prompt, images, size=size, quality=quality, retries=0
                )
            except Exception as exc:  # noqa: BLE001
                codex_error = exc
                logger.warning("Codex 生图失败，改用接口生图：%s", self.codex.safe_error(exc))
        if self.api.configured:
            return await self.api.edit(
                prompt, images, size=size, quality=quality, retries=retries
            )
        if codex_error is not None:
            raise codex_error
        raise ImageAPIError("Codex 和生图接口均未配置")

    def safe_error(self, error: object) -> str:
        return self.codex.safe_error(error)


class FallbackImageClient:
    """带自动降级的客户端包装。

    从 ProfileManager 获取当前方案，失败时按 auto_fallback 设置尝试下一个方案。
    """

    def __init__(
        self,
        profile_manager: ProfileManager,
        *,
        auto_fallback: bool = True,
        codex: CodexImageClient | None = None,
    ):
        self._profile_manager = profile_manager
        self._auto_fallback = auto_fallback
        self._codex = codex

    @property
    def configured(self) -> bool:
        if self._codex and self._codex.configured:
            return True
        current = self._profile_manager.current()
        return current is not None and current.configured

    @property
    def model(self) -> str:
        if self._codex and self._codex.configured:
            return self._codex.model
        current = self._profile_manager.current()
        return current.model if current else ""

    def safe_error(self, error: object) -> str:
        current = self._profile_manager.current()
        if current:
            message = str(error)
            for key in sorted(current.api_keys, key=len, reverse=True):
                message = message.replace(key, "***")
            return message
        return str(error)[:1000]

    async def list_models(self) -> list[str]:
        if self._codex and self._codex.configured:
            try:
                return await self._codex.list_models()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Codex 模型列表获取失败，改用接口模型列表：%s", self._codex.safe_error(exc))
        current = self._profile_manager.current()
        if current and current.configured:
            client = _make_api_client(current)
            return await client.list_models()
        raise ImageAPIError("没有可用的接口方案")

    def _client_for(self, profile: ImageProfile):
        return _make_api_client(profile)

    @staticmethod
    def _is_stable_failure(message: str) -> bool:
        """判断是否为配置类错误（重试无效，应直接切换方案）。"""
        # OpenAI 格式的错误标记
        for marker in ("密钥被拒绝", "地址或模型名不对", "不是 JSON"):
            if marker in message:
                return True
        # DashScope 格式的错误标记
        for marker in (
            "InvalidApiKey",
            "Arrearage",
            "密钥无效",
            "接口地址或模型名不对",
        ):
            if marker in message:
                return True
        return False

    async def edit(self, prompt, images, *, size, quality="high", retries=1) -> bytes:
        # Codex 优先（如果配置了）
        if self._codex and self._codex.configured:
            try:
                return await self._codex.edit(
                    prompt, images, size=size, quality=quality, retries=0
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Codex 生图失败，改用接口方案：%s", self._codex.safe_error(exc))

        current = self._profile_manager.current()
        if current is None:
            raise ImageAPIError("没有可用的接口方案，请先配置")
        if not current.configured:
            raise ImageAPIError(
                f"当前方案「{current.name or '未命名'}」配置不完整，请检查地址、密钥和模型名"
            )

        tried: set[int] = set()
        last_error: str = ""

        while current is not None and id(current) not in tried:
            tried.add(id(current))
            client = self._client_for(current)
            try:
                return await client.edit(
                    prompt, images, size=size, quality=quality, retries=retries
                )
            except ImageAPIError as exc:
                last_error = str(exc)
                if not self._auto_fallback:
                    raise
                # 配置类错误（密钥无效、地址错误），不值得重试当前方案，直接切换
                if self._is_stable_failure(last_error):
                    logger.warning(
                        "方案「%s」配置错误，切换备选：%s",
                        current.name or "未命名",
                        last_error,
                    )
                    current = self._profile_manager.next_after(current)
                    continue
                # 凭据类错误（401/403/429），密钥池已经尝试过所有密钥，切换方案
                if KeyPool.is_credential_failure(last_error):
                    logger.warning(
                        "方案「%s」密钥均不可用，切换备选：%s",
                        current.name or "未命名",
                        last_error,
                    )
                    current = self._profile_manager.next_after(current)
                    continue
                # 其他错误（超时、网络等），重试已耗尽，切换方案
                logger.warning(
                    "方案「%s」生图失败，切换备选：%s",
                    current.name or "未命名",
                    last_error,
                )
                current = self._profile_manager.next_after(current)

        if last_error:
            raise ImageAPIError(last_error)
        raise ImageAPIError("所有接口方案均已尝试，仍然失败")


def _make_api_client(profile: ImageProfile):
    """从 ImageProfile 构建合适的客户端。

    自动检测 DashScope 格式的 URL 和模型名，使用 DashScopeImageClient；
    否则使用标准 OpenAI Images API 客户端。
    """
    if _is_dashscope(profile):
        logger.info("使用 DashScope 客户端：model=%s", profile.model)
        return DashScopeImageClient(
            base_url=profile.base_url,
            api_key=profile.api_keys[0] if profile.api_keys else "",
            model=profile.model,
            timeout=profile.timeout,
        )
    key_pool = KeyPool(profile.api_keys)
    return ImageAPIClient(
        base_url=profile.base_url,
        api_key=key_pool,
        model=profile.model,
        timeout=profile.timeout,
    )


def make_client_for_profile(profile: ImageProfile):
    """按一条接口方案创建客户端，供设置页的模型检测和测试使用。"""
    return _make_api_client(profile)


def _is_dashscope(profile: ImageProfile) -> bool:
    """判断是否应使用 DashScope 客户端。"""
    url = (profile.base_url or "").lower()
    model = (profile.model or "").lower()
    # URL 特征
    if any(
        marker in url
        for marker in (
            "dashscope",
            "maas.aliyuncs.com",
            "maas.qianwenaiapi.com",
            "aliyuncs.com/api/v1",
        )
    ):
        return True
    # 模型名特征
    if any(
        marker in model
        for marker in (
            "wanx",
            "wan2.",
            "qwen-image",
            "wan-image",
        )
    ):
        return True
    return False


def create_image_client(config, keys=None):
    """向后兼容的工厂函数。优先使用 FallbackImageClient。"""
    profile_manager = ProfileManager(
        config.get("image_api_profiles") or [],
        int(config.get("image_api_active_profile") or 0),
        fallback_url=config.get("image_api_base_url", ""),
        fallback_keys=keys if keys is not None else config.get("image_api_key", []),
        fallback_model=config.get("image_api_model", ""),
        fallback_timeout=int(config.get("image_api_timeout") or DEFAULT_TIMEOUT),
    )
    auto_fallback = bool(config.get("image_api_auto_fallback", True))

    codex: CodexImageClient | None = None
    codex_enabled = bool(config.get("codex_enabled", True))
    backend = config.get("image_backend", API_BACKEND)

    if backend == CODEX_BACKEND:
        if not codex_enabled:
            raise ValueError("本机 Codex 生图未开启，请先在「生图设置」中开启")
        return CodexImageClient(
            str(config.get("codex_executable") or "codex"),
            str(config.get("codex_model") or ""),
            int(config.get("codex_timeout") or 600),
        )

    if bool(config.get("codex_preferred", False)) and codex_enabled:
        codex = CodexImageClient(
            str(config.get("codex_executable") or "codex"),
            str(config.get("codex_model") or ""),
            int(config.get("codex_timeout") or 600),
        )

    return FallbackImageClient(
        profile_manager,
        auto_fallback=auto_fallback,
        codex=codex,
    )
