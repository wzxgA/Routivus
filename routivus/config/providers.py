"""Provider 数据模型与注册表。

provider 不内置预设，全部由用户在配置（config.json 的 ``providers`` 节）
中自定义 name / api_base / default_model / api_key 等；API Key 与 provider
的一切配置都写入 config.json，不再走 ``.env``。能力参数（window / cache /
vision）为可选预设值。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    name: str                 # 用户自定义名称
    display_name: str
    api_base: str             # base url（来自 config.json）
    default_model: str
    models: tuple[str, ...] = ()   # 该 provider 可切换的模型列表（config.json 的 providers.<name>.models）
    api_key: str = ""         # API Key（明文存于 config.json 的 providers.<name>.api_key）
    api_key_env: str = ""     # 兼容保留项：旧「环境变量读取」模式，现已不使用
    context_window: int = 0   # 上下文窗口（token），用于预算控制
    supports_cache: bool = False   # 是否支持 prompt cache（预留）
    supports_vision: bool = False  # 是否支持图片输入（预留）


class ProviderRegistry:
    """用户自定义 provider 的注册表（无内置预设，初始为空）。"""

    def __init__(self, providers: list[Provider] | None = None) -> None:
        self._providers: dict[str, Provider] = {
            p.name: p for p in (providers if providers is not None else [])
        }

    def get(self, name: str) -> Provider | None:
        return self._providers.get(name)

    def all(self) -> list[Provider]:
        return list(self._providers.values())

    def names(self) -> list[str]:
        return list(self._providers)

    def register(self, provider: Provider) -> None:
        self._providers[provider.name] = provider
