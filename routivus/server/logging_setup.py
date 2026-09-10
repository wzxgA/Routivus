"""访问日志脱敏。

WebSocket 没法自定义请求头，所以访问令牌只能放在查询参数里（``?token=``）。
uvicorn 会把整条请求行原样打出来，于是令牌进了日志文件 —— 而日志经常被当作
报障附件随项目一起发出去。这里在 logging 层把 ``token=`` 的值抹掉。

HTTP 侧前端走 Authorization 头，不走查询参数；但初始页面 URL 也可能是
``/?token=...``，所以对 HTTP 访问日志一并脱敏，不做区分。
"""

from __future__ import annotations

import logging
import re

_TOKEN_IN_QUERY = re.compile(r"(?i)(token=)[^&\s\"']+")
_MARKER = "_routivus_token_redaction"

DEFAULT_LOGGERS = ("uvicorn.access", "uvicorn.error", "uvicorn")


class RedactTokenFilter(logging.Filter):
    """把日志记录里的 ``token=xxx`` 换成 ``token=***``。"""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging 接口名
        record.args = _redact(record.args)
        return True


def _redact(value: object) -> object:
    if isinstance(value, str):
        return _TOKEN_IN_QUERY.sub(r"\1***", value)
    if isinstance(value, tuple):
        return tuple(_redact(item) for item in value)
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    return value


def install_token_redaction(logger_names: tuple[str, ...] = DEFAULT_LOGGERS) -> None:
    """给 uvicorn 的日志器挂脱敏过滤器（幂等，可重复调用）。"""
    for name in logger_names:
        logger = logging.getLogger(name)
        if any(getattr(item, _MARKER, False) for item in logger.filters):
            continue
        filter_ = RedactTokenFilter()
        setattr(filter_, _MARKER, True)
        logger.addFilter(filter_)
