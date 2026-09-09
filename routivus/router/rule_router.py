"""SmartRouter 规则路由器（开局纯规则判断，零训练、零依赖）。

评分公式与阈值来源：XG-docs/smart-docs/ADAPTIVE_ROUTING.md §6.1。

与文档的两处有意差异（均为文档内部自相矛盾处，按注释意图/验证表修正）：
1. Basic 档阈值取 ``s < 1.0`` 而非 ``s <= 1.0``：文档验证表中
   "写个二分查找"（score=1）应落 Enhanced，``<=`` 会错判为 Basic。
2. 防降级规则见 postprocess.py（不在本模块）。
"""

from __future__ import annotations

from dataclasses import dataclass

# 分档阈值：score < 1.0 → Basic；<= 5.0 → Enhanced；<= 12.0 → Superior；其余 Ultimate
_THRESHOLDS = (1.0, 5.0, 12.0)


@dataclass(frozen=True)
class RuleDecision:
    """规则路由结果。"""

    tier_idx: int      # 0..3 = Basic..Ultimate
    score: float       # 加权总分（硬规则命中时为占位分数）
    hard_rule: bool    # 是否由硬规则直接决定（不参与分数竞争）


def rule_score(f: dict) -> float:
    """对特征做加权求和，返回总分。"""
    s = 0.0
    s += min(f["len_chars"] // 100, 15) * 1.0        # 每 100 字符 +1（max 15）
    s += min(f["num_code_blocks"], 4) * 8.0          # 每个代码块 +8（max 4）
    s += f["num_json"] * 2.0                         # 每个 JSON/XML +2
    s += f["num_lists"] * 1.5                        # 每个列表项 +1.5
    s += f["num_arch_kw"] * 3.0                      # 每个架构词 +3
    s += f["num_risk_kw"] * 5.0                      # 每个风险词 +5
    s += f["num_planning_kw"] * 4.0                  # 每个规划词 +4
    s += f["num_impl_kw"] * 1.0                      # 每个实现词 +1
    s += f["num_teach_kw"] * 1.0                     # 每个教学词 +1
    s += f["num_constraint_kw"] * 2.0                # 每个约束词 +2
    s += f["is_chatty"] * -6.0                       # 闲聊直接扣大分
    s += f["question_mark"] * -1.5                   # 疑问句略偏简单
    return s


def rule_route(f: dict) -> RuleDecision:
    """硬规则优先（不参与分数竞争），其余按总分映射分档。"""
    # 硬规则 1：有风险词 / 强架构 → 至少 Superior
    if f["num_risk_kw"] >= 1 or f["num_arch_kw"] >= 2:
        return RuleDecision(2, rule_score(f), True)
    # 硬规则 2：大量代码块 / 超长文本 → Ultimate
    if f["num_code_blocks"] >= 3 or f["len_chars"] > 12000:
        return RuleDecision(3, rule_score(f), True)
    # 硬规则 3：纯闲聊（无代码块、无教学/架构/风险词）→ Basic
    if (f["is_chatty"] and f["num_code_blocks"] == 0 and f["num_teach_kw"] == 0
            and f["num_arch_kw"] == 0 and f["num_risk_kw"] == 0):
        return RuleDecision(0, rule_score(f), True)

    # 软规则：按总分映射
    s = rule_score(f)
    if s < _THRESHOLDS[0]:
        idx = 0
    elif s <= _THRESHOLDS[1]:
        idx = 1
    elif s <= _THRESHOLDS[2]:
        idx = 2
    else:
        idx = 3
    return RuleDecision(idx, s, False)


def confidence(decision: RuleDecision) -> float:
    """规则路由的置信度估计：硬规则 1.0；软规则按分数到最近阈值的距离估。"""
    if decision.hard_rule:
        return 1.0
    margin = min(abs(decision.score - b) for b in _THRESHOLDS)
    return 0.5 + 0.5 * min(1.0, margin / 4.0)
