"""SmartRouter 关键词表（中英双语）。

词表来源：XG-docs/smart-docs/ADAPTIVE_ROUTING.md §4.1。
命中判定用子串匹配（``kw in text``），中文词无需分词即可工作；
英文词统一转小写后匹配。可按业务增删，但不改变类别键名
（features/rule_router/postprocess 依赖类别名）。
"""

from __future__ import annotations

KEYWORDS: dict[str, list[str]] = {
    "arch": [  # 架构词（命中→高风险/高复杂度）
        "架构", "重构", "扩展", "依赖", "分布式", "微服务", "模块化", "集成",
        "迁移", "设计模式", "系统设计", "扩展性", "性能优化",
        "architecture", "refactor", "microservice", "distributed",
        "scalable", "modular", "integration",
    ],
    "risk": [  # 风险词（命中→强制升档）
        "部署", "回滚", "迁移", "生产", "客户", "法务", "上线", "宕机",
        "故障", "安全", "合规", "数据丢失", "备份", "恢复", "资损",
        "deploy", "rollback", "production", "compliance", "security",
        "outage", "failure", "data-loss", "incident",
    ],
    "planning": [  # 规划词（方案/设计类）
        "设计", "方案", "对比", "取舍", "路线图", "规划", "roadmap",
        "权衡", "tradeoff", "plan", "design", "compare", "evaluate",
    ],
    "impl": [  # 实现词（具体动手任务）
        "写", "实现", "改", "生成", "修复", "添加", "创建", "优化",
        "帮我", "实现一个", "封装", "接入",
        "write", "implement", "create", "fix", "build", "refactor",
    ],
    "teach": [  # 教学词（解释/问答类）
        "解释", "为什么", "原理", "区别", "对比", "是什么", "如何",
        "什么是", "讲解", "说明",
        "explain", "why", "difference", "how", "what is", "describe",
    ],
    "constraint": [  # 约束词（有硬性要求→偏复杂）
        "必须", "不能", "不允许", "性能", "安全", "精确", "严格",
        "restriction", "constraint", "must", "cannot", "strict",
    ],
    "chatty": [  # 闲聊词（命中→降档）
        "你好", "谢谢", "在吗", "ok", "好的", "嗨", "嗯", "没问题", "再见",
        "hello", "hi", "thanks", "bye", "okay", "sure", "fine",
    ],
    "debug": [  # 调试词（命中→升一档）
        "报错", "失败", "还是不行", "异常", "崩溃", "超时",
        "error", "exception", "debug", "crash", "timeout", "bug",
    ],
}
