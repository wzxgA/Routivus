"""User-facing error categories for web operations."""

from __future__ import annotations


class WebError(Exception):
    category = "web_error"
    retryable = False


class WebConfigError(WebError):
    category = "configuration"


class WebInputError(WebError):
    category = "invalid_input"


class WebSecurityError(WebError):
    category = "security_rejected"


class WebRateLimitError(WebError):
    category = "rate_limited"
    retryable = True


class WebTimeoutError(WebError):
    category = "timeout"
    retryable = True


class WebProviderError(WebError):
    category = "provider_error"


class WebContentError(WebError):
    category = "unsupported_content"


def user_error(error: Exception) -> str:
    """Convert an internal exception to a concise, safe tool message."""
    if isinstance(error, WebSecurityError):
        return f"为安全原因拒绝访问该地址：{error}"
    if isinstance(error, WebConfigError):
        return f"未配置搜索服务，请配置后重试：{error}"
    if isinstance(error, WebInputError):
        return f"输入参数不合法：{error}"
    if isinstance(error, WebRateLimitError):
        return f"服务限流，请稍后重试：{error}"
    if isinstance(error, WebTimeoutError):
        return f"请求超时，可稍后重试：{error}"
    if isinstance(error, WebContentError):
        return str(error)
    if isinstance(error, WebProviderError):
        return str(error)
    return f"Web 请求失败：{type(error).__name__}"
