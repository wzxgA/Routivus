"""自学习局部规则。

从 feedback.log 聚合"特征谓词 → 信号方向"的高频规律，生成受限规则
（±1 档微调、置信度上限、支持度门槛），原子写 learned_rules.json。

与 calibration 的区别：calibration 是**全局**档位偏置，
对同一档位的所有输入一视同仁；learned_rules 是**局部**规则，只影响
命中某个特征谓词（如"含 debug 词""文本 ≤60 字"）的输入。二者叠加生效，
learned_rules 注入点在 postprocess 的 6 条规则之后、防降级之后。

关键约束（验收）：
- 规则 action 恒为 ±1，不跳档；
- 永不覆盖风险/闲聊/长上下文硬规则（由 postprocess 的 forced 标志保证）；
- 单条规则 support < MIN_SUPPORT 不生成；
- 多数派占比 < MIN_PRECISION 不生成（避免弱规则）；
- 删除 learned_rules.json 即回到空态。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from .feedback import read_feedback
from .store import atomic_write_json, learned_rules_path, read_json_safe

MIN_SUPPORT = 20        # 单候选谓词加权样本量下限（与校准样本门槛一致）
MIN_PRECISION = 0.6     # 多数派占比下限（低于则不生成，避免弱规则）
MAX_CONFIDENCE = 0.9    # 规则置信度上限（受限规则不允许满置信）
MAX_RULES = 20          # 规则条数上限（按 support 降序保留，防爆炸）

# 特殊数值特征集合：非关键词类别计数，单独判定谓词
_SPECIAL_INT = ("has_attachment", "is_chatty", "question_mark", "num_code_blocks")

# 简短文本长度上限（len_chars 谓词阈值，与 signals 的 SHORT_TEXT_CHARS 一致）
_SHORT_CHARS = 60


def _predicate_key(feature: str, op: str, value: float) -> tuple[str, str, float]:
    """谓词的三元唯一标识（用于聚合去重）。"""
    return (feature, op, value)


def _candidates(f: dict) -> list[tuple[str, str, float]]:
    """从一条特征的 features dict 提取其命中的候选谓词列表。

    只描述"这一条满足什么条件"：
    - num_<cat>_kw 关键词类别计数 >0 → num_<cat>_kw >= 1
    - 特殊整型特征 ==1 → <feature> >= 1
    - len_chars <= _SHORT_CHARS 且 >0 → len_chars <= 60

    不 import router 类别名（避免 adaptive→router 包环），谓词由
    features dict 里实际存在的键动态推导。
    """
    preds: list[tuple[str, str, float]] = []
    for k, v in f.items():
        if k == "len_chars":
            if isinstance(v, (int, float)) and 0 < v <= _SHORT_CHARS:
                preds.append((k, "<=", float(_SHORT_CHARS)))
        elif k in _SPECIAL_INT:
            if v == 1:
                preds.append((k, ">=", 1.0))
        elif k.startswith("num_") and k.endswith("_kw"):
            if isinstance(v, (int, float)) and v >= 1:
                preds.append((k, ">=", 1.0))
    return preds


@dataclass(frozen=True)
class LearnedRule:
    """一条自学习规则。predicate 为单个特征谓词。"""

    feature: str
    op: str            # ">=" 或 "<="
    value: float
    action: int        # +1（相关输入升档）或 -1（降档）
    confidence: float  # 多数派占比，上限 MAX_CONFIDENCE
    support: float     # 加权样本量

    @property
    def predicate(self) -> dict[str, float]:
        return {f"{self.feature}{self.op}": self.value}

    def matches(self, f: dict) -> bool:
        """该记录的特征是否命中本条规则谓词。"""
        v = f.get(self.feature)
        if not isinstance(v, (int, float)):
            return False
        return (v >= self.value) if self.op == ">=" else (v <= self.value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "predicate": self.predicate,
            "action": self.action,
            "confidence": self.confidence,
            "support": self.support,
        }


@dataclass(frozen=True)
class LearnedRules:
    """规则集。rules 按 support 降序排列，apply 取第一条命中的 action。"""

    rules: tuple[LearnedRule, ...] = ()

    @property
    def count(self) -> int:
        return len(self.rules)

    def apply(self, f: dict) -> int:
        """返回命中规则的 action（+1/-1/0=无命中）。只取第一条（最确信）。

        同一 features 可能命中多条规则，取 support 最高的一条，不叠加，
        保证单次最多偏移一档。
        """
        for r in self.rules:
            if r.matches(f):
                return r.action
        return 0

    def to_dict(self) -> dict[str, Any]:
        return {"rules": [r.to_dict() for r in self.rules]}

    @staticmethod
    def from_dict(data: dict[str, Any] | None) -> LearnedRules:
        """从持久化 JSON 恢复；结构异常回退空规则集（不抛错）。"""
        if not isinstance(data, dict):
            return LearnedRules()
        try:
            rules: list[LearnedRule] = []
            for item in data.get("rules") or []:
                pred = item.get("predicate") or {}
                # predicate 格式 {"feature>=1": 1} 或 {"feature<=60": 60}
                key = next(iter(pred)) if pred else None
                value = float(pred.get(key, 0.0)) if key else 0.0
                if not key:
                    continue
                op = "<=" if "<=" in key else (">=" if ">=" in key else "")
                feature = key.split(op)[0] if op else ""
                if not feature or op not in (">=", "<="):
                    continue
                # 归一化 value 为谓词里声明的值
                value = float(pred.get(key, value))
                rules.append(LearnedRule(
                    feature=feature, op=op, value=value,
                    action=int(item.get("action", 0)),
                    confidence=float(item.get("confidence", 0.0)),
                    support=float(item.get("support", 0.0)),
                ))
            rules.sort(key=lambda r: (-r.support, r.feature))
            return LearnedRules(tuple(rules))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return LearnedRules()


def aggregate(
    records: Sequence[dict[str, Any]],
    min_support: float = MIN_SUPPORT,
    min_precision: float = MIN_PRECISION,
    max_confidence: float = MAX_CONFIDENCE,
    max_rules: int = MAX_RULES,
) -> LearnedRules:
    """聚合 feedback.log → 规则集。

    对每条带 features 的记录，生成其命中的候选谓词，按"upgrade/downgrade
    加权和"累计每个谓词的支持度；支持度过门槛且多数派占比清晰才生成规则。
    旧记录（无 features 字段）跳过不计，保证向后兼容。
    """
    # 谓词唯一键 -> [up, down]
    stats: dict[tuple[str, str, float], list[float]] = {}
    for rec in records:
        feats = rec.get("features")
        if not isinstance(feats, dict) or not feats:
            continue
        direction = rec.get("signal")
        if direction not in ("upgrade", "downgrade"):
            continue
        try:
            weight = float(rec.get("weight", 0.0))
        except (TypeError, ValueError):
            continue
        for pred in _candidates(feats):
            bucket = stats.setdefault(pred, [0.0, 0.0])
            if direction == "upgrade":
                bucket[0] += weight
            else:
                bucket[1] += weight

    rules: list[LearnedRule] = []
    for (feature, op, value), (up, down) in stats.items():
        n = up + down
        if n < min_support:
            continue
        # 多数派占比；up > down -> 升规则，down > up -> 降规则
        if up > down:
            prec = up / n
            action = 1
        elif down > up:
            prec = down / n
            action = -1
        else:
            continue  # 完全对半，无方向
        if prec < min_precision:
            continue
        rules.append(LearnedRule(
            feature=feature, op=op, value=value, action=action,
            confidence=min(prec, max_confidence), support=n,
        ))
    rules.sort(key=lambda r: (-r.support, r.feature))
    return LearnedRules(tuple(rules[:max_rules]))  # type: ignore[arg-type]


def save_learned_rules(rules: LearnedRules, path=None) -> None:
    """原子写 learned_rules.json。空规则集也写（清空语义）。"""
    from .store import ensure_dir

    p = path or learned_rules_path()
    if path is None:
        ensure_dir()
    atomic_write_json(p, rules.to_dict())


def load_learned_rules(path=None) -> LearnedRules:
    """读取 learned_rules.json；缺失/损坏回退空规则集，绝不抛错。"""
    return LearnedRules.from_dict(read_json_safe(path or learned_rules_path()))


def re_learn(log_path=None, rules_path=None) -> LearnedRules:
    """读 feedback.log → 聚合 → 落盘 learned_rules.json → 返回结果。

    无记录时返回空规则集且不写盘（沿用"删掉 ~/.routivus/adaptive/ 即
    回到纯规则行为"的验收：不重建目录）。
    """
    records = read_feedback(log_path)
    if not records:
        return LearnedRules()
    rules = aggregate(records)
    save_learned_rules(rules, rules_path)
    return rules


def rule_hit_stats(
    records: Sequence[dict[str, Any]], rules: LearnedRules,
) -> dict[str, Any]:
    """统计规则集在 feedback.log 上的命中情况，供 `/smartRouter status` 展示。

    返回 {"rule_count", "sample_records", "hit_records", "per_rule"}：
    - rule_count：规则条数；
    - sample_records：带 features 的可命中样本记录数；
    - hit_records：命中至少一条规则的样本记录数；
    - per_rule：每条规则的 predicate/action/confidence/support 及命中次数
      （一条记录可能命中多条，各自累计）。
    """
    per: dict[int, int] = {id(r): 0 for r in rules.rules}
    sample_records = 0
    hit_records = 0
    for rec in records:
        feats = rec.get("features")
        if not isinstance(feats, dict) or not feats:
            continue
        sample_records += 1
        hit_any = False
        for r in rules.rules:
            if r.matches(feats):
                per[id(r)] += 1
                hit_any = True
        if hit_any:
            hit_records += 1
    per_rule = [
        {
            "predicate": r.predicate,
            "action": r.action,
            "confidence": r.confidence,
            "support": r.support,
            "hits": per[id(r)],
        }
        for r in rules.rules
    ]
    return {
        "rule_count": rules.count,
        "sample_records": sample_records,
        "hit_records": hit_records,
        "per_rule": per_rule,
    }