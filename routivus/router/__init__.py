"""SmartRouter 路由核心包。

按输入文本的规则特征判断复杂度档位（Basic / Enhanced / Superior / Ultimate），
并解析出该档位应使用的 provider / model。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .features import extract
from .model_tiers import TIER_NAMES, TierTarget, resolve
from .postprocess import postprocess
from .rule_router import RuleDecision, confidence, rule_route, rule_score


@dataclass(frozen=True)
class RouteResult:
    """一次完整路由的结果。"""

    tier: str                  # Basic / Enhanced / Superior / Ultimate
    tier_idx: int              # 0..3
    provider: str
    model: str
    configured: bool           # 档位是否来自用户显式配置
    confidence: float          # 0~1，规则路由的置信度估计
    score: float               # 规则加权总分
    hard_rule: bool            # 是否由硬规则直接决定
    features: dict = field(default_factory=dict)  # 特征快照（测试/调试/后续反馈采集用）


def _tier_index(prev_tier: str | int | None) -> int | None:
    """把档位名（或索引）统一转成索引；未知名称返回 None。"""
    if prev_tier is None:
        return None
    if isinstance(prev_tier, int):
        return prev_tier
    try:
        return TIER_NAMES.index(prev_tier)
    except ValueError:
        return None


def route(text: str, *,
          prev_tier: str | int | None = None,
          prev_ts: float | None = None,
          ts: float | None = None,
          context_tokens: int = 0,
          fallback_provider: str = "",
          fallback_model: str = "",
          tiers_config: dict | None = None,
          manager=None,
          calibration=None,
          learned_rules=None,
          hysteresis=None,
          ml_router=None) -> RouteResult:
    """对一段用户输入做完整路由：特征 → 规则打分 → 校准/ML精判 → 后处理 → 档位解析。

    ``manager``（ConfigManager）可选，传入时对显式配置档做 provider/API Key 校验。
    ``calibration``（adaptive.Calibration）可选，只读注入：
    在规则打分后、安全后处理前应用档位偏置与置信门；不传即纯规则行为。
    ``learned_rules``（adaptive.LearnedRules）可选， 只读注入：
    postprocess 末尾对其命中的 ±1 档微调，硬规则强制时无效；不传即无局部规则行为。
    ``hysteresis``（router.postprocess.Hysteresis）可选， 只读注入：
    作为最后一道闸抑制会话内短时跳档，硬规则强制时解冻；不传即无迟滞行为。
    ``ml_router``（router.ml_router.MLRouter）可选， 只读注入：
    规则打分后参与精判（概率最高档 + 置信门 + 校准偏置），不可用/信心不足时
    静默回落规则档位；硬规则决策不参与精判；不传即纯规则行为。
    """
    f = extract(text)
    decision: RuleDecision = rule_route(f)
    tier_idx = decision.tier_idx
    hard_rule = decision.hard_rule
    from routivus.adaptive.calibrate import apply_calibration

    # 优先：ML 精判（仅软规则决策、且产物可用时）→ 置信门 + 校准偏置
    if ml_router is not None and not hard_rule and ml_router.available:
        ml_tier = ml_router.decide(text, f, calibration)
        if ml_tier is not None:
            tier_idx = ml_tier
    else:
        if calibration is not None:
            tier_idx = apply_calibration(
                tier_idx, confidence(decision), hard_rule, calibration,
            )
    final_idx = postprocess(
        tier_idx, text, f,
        prev_tier=_tier_index(prev_tier), prev_ts=prev_ts,
        ts=ts if ts is not None else time.time(),
        context_tokens=context_tokens,
        learned_rules=learned_rules,
        hysteresis=hysteresis,
    )
    target: TierTarget = resolve(final_idx, fallback_provider, fallback_model,
                                 tiers_config, manager)
    return RouteResult(
        tier=target.tier,
        tier_idx=final_idx,
        provider=target.provider,
        model=target.model,
        configured=target.configured,
        confidence=confidence(decision),
        score=decision.score,
        hard_rule=decision.hard_rule,
        features=f,
    )


__all__ = [
    "RouteResult",
    "TierTarget",
    "TIER_NAMES",
    "extract",
    "route",
    "resolve",
    "rule_route",
    "rule_score",
    "postprocess",
    "KEYWORDS",
]
