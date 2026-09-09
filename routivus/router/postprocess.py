"""SmartRouter 后处理规则引擎（可解释的安全兜底）。

规则与执行顺序来源：Routivus-docs/smart-docs/ADAPTIVE_ROUTING.md §7（1→6，先升后降、最后防降级）。

与文档代码的一处有意差异：防降级规则按注释意图实现为
``t = max(t, prev_tier - 1)``（600s 内最多比上一轮低 1 档）。
文档示例代码的 ``t = min(t, prev_tier + 1)`` 实际限制的是"上升"，
与其注释"不能比上一轮低超过 1 档"矛盾，此处以注释语义为准。
"""

from __future__ import annotations

from dataclasses import dataclass

from .features import code_blocks
from .keywords import KEYWORDS

TIER = ["Basic", "Enhanced", "Superior", "Ultimate"]

# 防降级窗口（秒）：同会话内两次路由间隔小于该值时，档位最多下降 1 档
ANTI_DOWNGRADE_WINDOW = 600
# 迟滞窗口（秒，phase-04 A2）：窗口内档位变化超过 max_changes 次即冻结当前档
HYSTERESIS_WINDOW = 60


@dataclass
class Hysteresis:
    """会话内迟滞状态（稳定层）。

    防短时震荡：同一会话窗口内档位变化超过 ``max_changes`` 次即冻结当前档，
    直到窗口过期或命中硬规则（``forced``）才解冻。与 600s 防降级叠加复用，
    但语义不同——防降级只限制"下降不能太快"，迟滞是"上下都不允许频繁变"。

    由调用方按会话持有同一个实例，跨轮传入 ``postprocess``。导演顺序：
    防降级在前、迟滞最后一道闸（压制包括 learned_rules 在内的所有后续变化）。
    """

    window: float = HYSTERESIS_WINDOW
    max_changes: int = 1                     # 窗口内允许的档位变化次数，超过即冻结
    prev_tier: int | None = None             # 上一轮最终档位索引
    prev_ts: float | None = None             # 上一轮时间戳
    change_count: int = 0                    # 当前窗口内已变化次数
    last_change_ts: float | None = None      # 最近一次变化的时间戳
    frozen_tier: int | None = None           # 命中冻结档位（None=未冻结）
    frozen_ts: float | None = None           # 冻结开始时间

    def step(self, target: int, forced: bool, ts: float = 0.0) -> int:
        """对本次路由目标档位应用迟滞，返回最终档位索引。

        ``target`` 为防降级后的目标档；``forced`` 为是否已被硬规则强制
        （风险/长上下文/闲聊），硬规则输入允许突破冻结立即生效。
        """
        # 窗口过期：重置计数，解冻
        if self.last_change_ts is not None and not forced:
            if ts - self.last_change_ts > self.window:
                self.change_count = 0
                self.frozen_tier = None
                self.frozen_ts = None

        # 冻结持续期：非硬输入一律压回冻结档
        if self.frozen_tier is not None and not forced:
            if self.frozen_ts is not None and ts - self.frozen_ts <= self.window:
                return self.frozen_tier
            self.frozen_tier = None          # 冻结窗口过期，自动解冻
            self.frozen_ts = None

        prev = self.prev_tier
        if prev is None or target == prev:
            # 无历史或档位未变：不计数
            self.prev_tier = target
            self.prev_ts = ts
            return target

        # 档位将要变化
        if forced:
            # 硬规则强制：解冻并放行
            self.frozen_tier = None
            self.frozen_ts = None
            self.change_count = 0
        else:
            self.change_count += 1
            if self.change_count > self.max_changes:
                # 触发冻结：压回上一档
                self.frozen_tier = prev
                self.frozen_ts = ts
                self.last_change_ts = ts
                self.change_count = 0
                return prev                       # prev_tier 保持 prev
            self.last_change_ts = ts

        self.prev_tier = target
        self.prev_ts = ts
        return target


def hit(text: str, cat: str) -> bool:
    """判断文本是否命中某类关键词（英文转小写后子串匹配）。"""
    t = text.lower()
    return any(k in t for k in KEYWORDS[cat])


def postprocess(tier_idx: int, text: str, f: dict,
                prev_tier: int | None = None, prev_ts: float | None = None,
                ts: float = 0.0, context_tokens: int = 0,
                learned_rules=None, hysteresis=None) -> int:
    """按顺序应用规则，返回最终档位索引 0..3。

    ``learned_rules``（adaptive.LearnedRules，可选）在 6 条规则之后、且仅
    在未被硬规则强制时应用：命中的 ±1 档微调永不覆盖风险/闲聊/长上下文硬规则
    。
    ``hysteresis``（Hysteresis，可选，phase-04 A2）作为最后一道闸应用：
    session 内窗口变化超阈值即冻结当前档，直到窗口过期或命中硬规则才解冻；
    不传即 A1 之前行为，与其它机制完全向后兼容。
    """
    t = tier_idx
    forced = False  # 是否已被某条硬规则强制锁定（learned_rules 不再覆盖）

    # 1) 风险旗标 → 强制 >= Superior
    if hit(text, "risk"):
        t = max(t, 2)
        forced = True

    # 2) 长上下文旗标 → 强制 >= Superior
    blocks = code_blocks(text)
    if (len(text) > 6000
            or (blocks and max(len(b) for b in blocks) > 1500)
            or context_tokens > 2000):
        t = max(t, 2)
        forced = True

    # 3) 架构旗标 → 升一档
    if hit(text, "arch"):
        t = min(t + 1, 3)

    # 4) 调试旗标 → 升一档
    if hit(text, "debug"):
        t = min(t + 1, 3)

    # 5) 简短闲聊旗标 → 强制 <= Basic
    if (hit(text, "chatty") and f["num_code_blocks"] == 0
            and not hit(text, "teach") and not hit(text, "arch")
            and not hit(text, "risk") and not hit(text, "planning")):
        t = 0
        forced = True

    # 6) 防降级：同会话 600s 内，档位最多比上一轮低 1 档
    if prev_tier is not None and prev_ts is not None and ts - prev_ts < ANTI_DOWNGRADE_WINDOW:
        t = max(t, prev_tier - 1)

    # 7) learned_rules 局部规则：仅未被硬规则强制时 ±1 档微调
    if not forced and learned_rules is not None:
        action = learned_rules.apply(f)
        if action > 0:
            t = min(t + 1, 3)
        elif action < 0:
            t = max(t - 1, 0)

    # 8) 迟滞稳定层：最后一道闸，压制所有后续变化。
    #    防降级在前、迟滞在后；冻结时连 learned_rules 的微调也一并压住。
    if hysteresis is not None:
        # 首轮用调用方传入的上一轮档态初始化内部 prev（迟滞自身跨轮维护）
        if hysteresis.prev_tier is None and prev_tier is not None:
            hysteresis.prev_tier = prev_tier
            hysteresis.prev_ts = prev_ts if prev_ts is not None else ts
        t = hysteresis.step(t, forced, ts)

    return t
