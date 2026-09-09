"""SmartRouter 规则特征管线。

纯 Python 字符串统计，无需训练、零第三方依赖。
特征定义来源：XG-docs/smart-docs/ADAPTIVE_ROUTING.md §4.1。
"""

from __future__ import annotations

import re

from .keywords import KEYWORDS

# 附件/文件类提及词（has_attachment 特征用，不属于 KEYWORDS 打分类别）
_ATTACHMENT_WORDS = ("附件", "文件", "目录", "上传", "file", "attachment")

# 围栏代码块：``` 开头（可带语言标注）到 ``` 结束
_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.S)
# 围栏块的语言标注（用于 num_json / num_xml 统计）
_FENCE_LANG_RE = re.compile(r"```(json|xml)[^\n]*\n", re.I)
# 列表项：数字编号（1. / 1、 / 1)）或无序列表（- / * / +）
_LIST_ITEM_RE = re.compile(r"^\s*(?:\d+[.、)]|[-*+])\s+", re.M)


def code_blocks(text: str) -> list[str]:
    """提取 ``` 围栏代码块；无围栏时按行首缩进 >=4 空格的行聚合成块。"""
    blocks = _FENCE_RE.findall(text)
    if blocks:
        return blocks
    lines = [ln for ln in text.splitlines() if ln.startswith(("    ", "\t"))]
    return ["\n".join(lines)] if lines else []


def extract(text: str) -> dict:
    """从用户输入文本提取规则特征，返回扁平特征字典。"""
    lower = text.lower()
    blocks = code_blocks(text)
    code_chars = sum(len(b) for b in blocks)

    f: dict = {
        "len_chars": len(text),
        "len_words": len(text.split()),
        "num_code_blocks": len(blocks),
        "code_chars_ratio": (code_chars / len(text)) if text else 0.0,
    }

    # JSON / XML 片段数：统计带语言标注的围栏块（```json / ```xml）
    fence_langs = [m.lower() for m in _FENCE_LANG_RE.findall(text)]
    f["num_json"] = fence_langs.count("json")
    f["num_xml"] = fence_langs.count("xml")

    # 列表项数
    f["num_lists"] = len(_LIST_ITEM_RE.findall(text))

    # 是否提及文件/附件
    f["has_attachment"] = 1 if any(w in lower for w in _ATTACHMENT_WORDS) else 0

    # 各类关键词命中数（英文转小写后子串匹配，按出现次数累计）
    for cat, words in KEYWORDS.items():
        f[f"num_{cat}_kw"] = sum(lower.count(w.lower()) for w in words)

    # 闲聊标记：命中任一闲聊词
    f["is_chatty"] = 1 if f["num_chatty_kw"] > 0 else 0
    # 疑问句标记：含中/英文问号
    f["question_mark"] = 1 if ("?" in text or "？" in text) else 0

    return f
