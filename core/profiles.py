"""接口配置方案管理：支持多组 URL + 密钥 + 模型的完整配置，带自动降级。

每个方案包含独立的名称、接口地址、密钥列表、模型名和超时时间。
运行时可以按索引或名称手动切换当前方案；失败时可以自动降级到下一个方案。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from .image_api import DEFAULT_MODEL, DEFAULT_TIMEOUT, normalize_api_base


def new_profile_id() -> str:
    """生成一个方案 id，用于页面保存时识别同一条方案。"""
    return uuid.uuid4().hex


@dataclass
class ImageProfile:
    """一组完整的接口配置。"""

    name: str = ""
    base_url: str = ""
    api_keys: list[str] = field(default_factory=list)
    model: str = DEFAULT_MODEL
    timeout: int = DEFAULT_TIMEOUT
    id: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_keys and self.model)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "base_url": self.base_url,
            "api_key": list(self.api_keys),
            "model": self.model,
            "timeout": self.timeout,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ImageProfile:
        keys = data.get("api_key") or data.get("api_keys") or []
        if isinstance(keys, str):
            keys = [keys]
        return cls(
            name=str(data.get("name") or "").strip(),
            base_url=normalize_api_base(data.get("base_url") or ""),
            api_keys=[str(k).strip() for k in keys if str(k or "").strip()],
            model=str(data.get("model") or DEFAULT_MODEL).strip() or DEFAULT_MODEL,
            timeout=_parse_timeout(data.get("timeout")),
            id=str(data.get("id") or "").strip(),
        )


def _parse_timeout(value: object) -> int:
    try:
        number = int(value) if value else DEFAULT_TIMEOUT
    except (TypeError, ValueError):
        number = DEFAULT_TIMEOUT
    return max(30, min(number, 3600))


class ProfileManager:
    """管理多组接口配置方案，支持轮询和降级。

    profiles 为空时，从旧配置项构建一个默认方案，保证向后兼容。
    """

    def __init__(
        self,
        profiles: list[dict],
        active_index: int = 0,
        *,
        fallback_url: str = "",
        fallback_keys: list[str] | None = None,
        fallback_model: str = "",
        fallback_timeout: int = DEFAULT_TIMEOUT,
    ):
        self._profiles: list[ImageProfile] = []
        for item in profiles:
            if isinstance(item, dict):
                profile = ImageProfile.from_dict(item)
                if profile.configured:
                    self._profiles.append(profile)
        # 向后兼容：没有方案时，从旧配置项构建默认方案
        if not self._profiles:
            url = normalize_api_base(fallback_url)
            keys = [str(k).strip() for k in (fallback_keys or []) if str(k or "").strip()]
            model = str(fallback_model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
            if url and keys:
                self._profiles.append(
                    ImageProfile(
                        name="默认接口",
                        base_url=url,
                        api_keys=keys,
                        model=model,
                        timeout=_parse_timeout(fallback_timeout),
                    )
                )
        # 页面按 id 识别同一条方案；旧配置没有 id 时给一个稳定值，
        # 这样页面“读取 → 保存”之间即使没有落盘也能对上同一条方案。
        seen_ids: set[str] = set()
        for index, profile in enumerate(self._profiles):
            candidate = profile.id
            if not candidate or candidate in seen_ids:
                candidate = f"migrated-{index}"
            while candidate in seen_ids:
                candidate = f"migrated-{index}-{len(seen_ids)}"
            profile.id = candidate
            seen_ids.add(candidate)
        self._active = max(0, active_index) if self._profiles else 0

    @property
    def profiles(self) -> list[ImageProfile]:
        return list(self._profiles)

    @property
    def active_index(self) -> int:
        if not self._profiles:
            return 0
        return self._active % len(self._profiles)

    def current(self) -> ImageProfile | None:
        if not self._profiles:
            return None
        return self._profiles[self.active_index]

    def next_after(self, profile: ImageProfile) -> ImageProfile | None:
        """获取指定方案之后的下一个方案，循环。没有备选时返回 None。"""
        if len(self._profiles) <= 1:
            return None
        try:
            index = self._profiles.index(profile)
        except ValueError:
            return None
        next_index = (index + 1) % len(self._profiles)
        if next_index == self.active_index:
            return None
        return self._profiles[next_index]

    def switch_to(self, selector: str) -> ImageProfile | None:
        """按索引（从 1 起）或名称切换当前方案。成功返回方案，失败返回 None。"""
        selector = selector.strip()
        if not selector or not self._profiles:
            return None
        if selector.isdecimal():
            index = int(selector) - 1
            if 0 <= index < len(self._profiles):
                self._active = index
                return self._profiles[index]
            return None
        for profile in self._profiles:
            if profile.name == selector:
                self._active = self._profiles.index(profile)
                return profile
        return None

    def add(self, profile: ImageProfile) -> int:
        """添加方案，返回新方案的索引（从 1 起）。"""
        if not profile.id:
            profile.id = new_profile_id()
        self._profiles.append(profile)
        return len(self._profiles)

    def remove(self, selector: str) -> bool:
        """按索引或名称删除方案。"""
        target = None
        selector = selector.strip()
        if selector.isdecimal():
            index = int(selector) - 1
            if 0 <= index < len(self._profiles):
                target = self._profiles[index]
        else:
            for profile in self._profiles:
                if profile.name == selector:
                    target = profile
                    break
        if target is None:
            return False
        removed_index = self._profiles.index(target)
        self._profiles.remove(target)
        if not self._profiles:
            self._active = 0
        elif self._active >= len(self._profiles):
            self._active = len(self._profiles) - 1
        elif self._active > removed_index:
            self._active -= 1
        return True

    def to_list(self) -> list[dict]:
        return [profile.to_dict() for profile in self._profiles]

    def format_list(self) -> str:
        """格式化方案列表，用于展示。"""
        if not self._profiles:
            return "暂无接口方案，请先通过设置页面或命令添加。"
        lines = []
        for index, profile in enumerate(self._profiles):
            marker = " ← 当前" if index == self.active_index else ""
            status = "✓" if profile.configured else "✗ 未配置"
            lines.append(
                f"{index + 1}. {profile.name or '未命名'}{marker}\n"
                f"   地址：{profile.base_url or '未填写'}\n"
                f"   密钥：{len(profile.api_keys)} 条 | 模型：{profile.model} | {status}"
            )
        return "\n".join(lines)
