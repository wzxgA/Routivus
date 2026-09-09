"""LlmClient 工厂：按解析后的 api_base / key / model 组装客户端。

URL 与 Key 的解析（环境变量、配置合并）由 ConfigManager 负责，
工厂只负责组装客户端，是后续 provider 差异化（能力、协议）的扩展点。
"""

from __future__ import annotations

from routivus.llm.client import LlmClient
from routivus.llm.openai_compat import OpenAICompatClient

DEFAULT_TIMEOUT = 120.0


def create_client(
    api_base: str,
    api_key: str,
    model: str,
    timeout: float = DEFAULT_TIMEOUT,
    retry_enabled: bool = True,
    max_retries: int = 2,
    retry_base_delay: float = 1.0,
    retry_max_delay: float = 8.0,
    retry_jitter: float = 0.25,
    retry_total_timeout: float = 30.0,
    respect_retry_after: bool = True,
) -> LlmClient:
    return OpenAICompatClient(
        api_base=api_base,
        api_key=api_key,
        model=model,
        timeout=timeout,
        retry_enabled=retry_enabled,
        max_retries=max_retries,
        retry_base_delay=retry_base_delay,
        retry_max_delay=retry_max_delay,
        retry_jitter=retry_jitter,
        retry_total_timeout=retry_total_timeout,
        respect_retry_after=respect_retry_after,
    )
