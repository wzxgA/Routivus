"""SmartRouter 档位→模型映射。

接入 ConfigManager（phase-01 子步骤 B）：
- ``tiers_config`` 形如 ``{"Basic": {"provider": "glm", "model": "glm-4-flash"}, ...}``，
  通常来自 ``ConfigManager.smart_router_config()["tiers"]``；
- 传入 ``manager`` 时启用完整校验链：provider 必须可解析、API Key 必须已配置，
  否则该档回落 fallback（active 模型）并标记 ``configured=False``；
- 未配置的档位同样回落 fallback。

校验失败的档位在 UI 上应显示为不可用（阶段 2 的暗淡 + (x) 标记）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .postprocess import TIER

if TYPE_CHECKING:
    from routivus.config.manager import ConfigManager

TIER_NAMES = TIER


@dataclass(frozen=True)
class TierTarget:
    """某档位解析出的模型目标。"""

    tier: str            # Basic / Enhanced / Superior / Ultimate
    provider: str
    model: str
    configured: bool     # 显式配置且校验通过（False 表示回落默认/校验失败）


def resolve(tier_idx: int, fallback_provider: str, fallback_model: str,
            tiers_config: dict | None = None,
            manager: "ConfigManager | None" = None) -> TierTarget:
    """把档位索引解析成 (provider, model)。

    结算规则：
    1. 档位指定了 ``provider``：model 缺省时用该 provider 的默认模型（D5），
       需 ``manager`` 解析；传 None 时无法取默认模型，回落 fallback_model。
    2. 档位未指定 ``provider``：整体回落 fallback（base provider 及其生效模型），
       model 取档位显式 ``model`` 或 fallback_model。
    3. 显式配置经 ``manager`` 校验：provider 必须可解析、API Key 必须已配置，
       否则整档回落 fallback 并标记 ``configured=False``。
    """
    tier_name = TIER_NAMES[tier_idx]
    cfg = (tiers_config or {}).get(tier_name) or {}
    provided_provider = str(cfg.get("provider") or "")

    if provided_provider:
        provider = provided_provider
        default_model = fallback_model
        if manager is not None:
            prov = manager.resolve_provider(provider)
            if prov is not None and prov.default_model:
                default_model = prov.default_model
        model = str(cfg.get("model") or "") or default_model
        configured = bool(cfg.get("provider") or cfg.get("model"))
    else:
        provider = fallback_provider
        model = str(cfg.get("model") or "") or fallback_model
        configured = bool(cfg.get("model"))

    if configured and manager is not None and (provider, model) != (fallback_provider, fallback_model):
        prov = manager.resolve_provider(provider)
        if prov is None or not manager.resolve_api_key(prov):
            # provider 不存在或 API Key 未配置：整档回落 active
            return TierTarget(tier=tier_name, provider=fallback_provider,
                              model=fallback_model, configured=False)
    return TierTarget(tier=tier_name, provider=provider, model=model, configured=configured)
