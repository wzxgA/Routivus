"""Ask-User 交互数据契约。

模型在信息不足 / 需要澄清 / 用户要求先确认时调用 ``ask_user`` 停下来提问。
本模块只定义数据与类型，不包含任何 UI 或网络逻辑。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable


@dataclass(frozen=True)
class AskOption:
    """可选中的一项。选中后回灌给模型的取值缺省等于 label。"""

    label: str
    value: str = ""


@dataclass(frozen=True)
class AskField:
    """一个待收集的问题字段。"""

    key: str
    question: str
    options: tuple[AskOption, ...] = ()
    allow_custom: bool = True
    default: str = ""
    required: bool = False


@dataclass(frozen=True)
class AskRequest:
    """一次 ask_user：可包含多个问题字段。"""

    id: str
    prompt: str = ""
    fields: tuple[AskField, ...] = ()
    origin: str = ""  # 来源标识：主会话 / plan 子任务 / team worker

    @staticmethod
    def new(prompt: str = "", fields: tuple[AskField, ...] = (), origin: str = "") -> "AskRequest":
        return AskRequest(id=f"ask-{uuid.uuid4().hex[:8]}", prompt=prompt, fields=fields, origin=origin)


# 用户答案：{field.key: 用户选中的值或自定义输入}。
# 用户跳过（Esc）时返回 None（fail-closed，不代选）。
AskAnswer = dict[str, str] | None
AskRequester = Callable[[AskRequest], Awaitable[AskAnswer]]