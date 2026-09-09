"""MCP JSON-RPC constants, validation and public exceptions."""

from __future__ import annotations

from typing import Any


SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]


class McpError(Exception):
    """Base MCP error with a user-safe message."""


class McpConfigError(McpError):
    pass


class McpProtocolError(McpError):
    pass


class McpUnavailableError(McpError):
    pass


class McpRequestError(McpError):
    def __init__(self, code: int | str, message: str, data: Any = None) -> None:
        self.code = code
        self.data = data
        super().__init__(f"MCP error {code}: {message}")


def request_message(request_id: int, method: str, params: dict | None = None) -> dict:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def notification_message(method: str, params: dict | None = None) -> dict:
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    return message


def parse_result(message: dict, expected_id: int | str | None = None) -> dict:
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        raise McpProtocolError("非法 JSON-RPC envelope")
    if expected_id is not None and message.get("id") != expected_id:
        raise McpProtocolError("JSON-RPC response id 不匹配")
    error = message.get("error")
    if isinstance(error, dict):
        raise McpRequestError(error.get("code", -32000), str(error.get("message", "unknown error")), error.get("data"))
    result = message.get("result", {})
    if result is None:
        return {}
    if not isinstance(result, dict):
        raise McpProtocolError("JSON-RPC result 必须是对象")
    return result
