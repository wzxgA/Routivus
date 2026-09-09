"""LLM 工厂单元测试。"""

from __future__ import annotations

from routivus.llm.factory import create_client
from routivus.llm.openai_compat import OpenAICompatClient


def test_create_client_passthrough():
    client = create_client("https://api.deepseek.com/v1", "sk-123", "deepseek-reasoner")
    assert isinstance(client, OpenAICompatClient)
    assert client.api_base == "https://api.deepseek.com/v1"
    assert client.api_key == "sk-123"
    assert client.model == "deepseek-reasoner"
