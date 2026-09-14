"""出厂「无语义兜底」产物的规则蒸馏脚本（方案 10 §4.5）。

产物：``routivus/assets/router.lgb.nosem``（``sem_dim = 0``，只吃 TF-IDF + 数值特征）。

为什么需要它：随包的语义版产物声明 ``sem_dim = 512``，而 ``MLRouter`` 在语义编码器
不可用（onnxruntime 未装/装坏/被隔离）时无法用它 → 整层 ML 精判消失，界面显示
「ML 精判不可用」。有了无语义版，那种机器上仍然有一层可用的 ML 精判，并且它还是
**本地进化的起点**（本地重训只要不比它差就会被采纳）。

标签来源：**规则路由本身**（``rule_route``）。这不是"用规则冒充 ML"，而是把规则行为
蒸馏成一个"带概率的、可被继续训练的"先验：数值特征 + 词面特征上的 LightGBM。

语料是脚本内置模板的确定性组合（无外部数据、可复现）：

    python tools/distill_nosem_router.py                    # 写 routivus/assets/router.lgb.nosem
    python tools/distill_nosem_router.py --out /tmp/x.lgb    # 写到别处
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from routivus.adaptive.training import train_and_save  # noqa: E402
from routivus.router.features import extract  # noqa: E402
from routivus.router.rule_router import rule_route  # noqa: E402

DEFAULT_OUT = _REPO_ROOT / "routivus" / "assets" / "router.lgb.nosem"

# 四个档位的代表性输入（人工撰写，覆盖各档典型用词）
_BASE: dict[int, tuple[str, ...]] = {
    0: (
        "你好", "早上好呀", "在吗", "随便聊聊", "今天天气怎么样", "谢谢你了",
        "哈哈哈太好笑了", "周末吃点什么好", "给我讲个冷笑话", "嗯嗯收到",
        "你是谁", "在忙什么呢", "晚安啦", "好久不见", "有啥推荐的吗",
        "聊聊最近的事吧",
    ),
    1: (
        "把这段代码格式化一下", "这个函数怎么用", "解释一下这行是什么意思",
        "帮我改个变量名", "正则里 .* 和 .+ 有什么区别", "print 和 return 的区别",
        "这段 Python 报错了帮我看看", "JSON 怎么转成字典", "git 怎么撤销上次提交",
        "写个函数把两个数相加", "怎么读取一个文件", "解释一下什么是闭包",
        "这段 sql 为什么慢", "npm 安装依赖失败怎么办",
        "帮我写个正则匹配手机号", "这个报错是什么原因",
    ),
    2: (
        "设计一个订单系统的数据库表结构", "帮我做一次代码评审，关注并发安全",
        "给我讲讲依赖注入的原理和适用场景", "重构这个模块，降低耦合度",
        "写单元测试覆盖这个类的边界情况", "解释一下 CAP 定理在实际系统里怎么取舍",
        "帮我排查线上接口偶发超时的问题", "设计一个限流中间件的方案",
        "这段缓存逻辑有什么隐患", "给我做个技术方案选型对比",
        "帮我评审这个 API 设计的合理性", "怎么给这个服务加可观测性",
        "写一份这个模块的设计文档", "这个查询计划为什么没有走索引",
        "帮我设计灰度发布的流程", "解释一下这个架构的扩展性瓶颈",
    ),
    3: (
        "设计生产环境分布式部署架构方案，含容量评估、灰度发布与回滚预案",
        "规划一次跨机房数据迁移，要求零停机、可回滚、含一致性校验方案",
        "从零设计一个支持千万级并发的消息系统架构，评估成本与降级策略",
        "给这套微服务做整体架构评审，给出版本演进路线与风险清单",
        "制定明年技术架构演进路线，包含容量规划、成本优化与组织分工",
        "设计多租户 SaaS 的数据隔离与配额体系，含安全边界与审计方案",
        "评估把单体拆成微服务的可行性，给出分期迁移方案与回滚点",
        "规划大促期间的容量评估与降级预案，含压测基线与应急预案",
        "设计一套跨区域容灾架构，要求 RPO 与 RTO 指标可验证",
        "制定数据治理规范，含分级、脱敏、审计与合规要求",
        "设计高可用消息队列集群方案，含脑裂处理与数据一致性权衡",
        "给核心交易链路做全链路压测与容量建模，输出扩容决策依据",
        "规划从 Kubernetes 迁移到多集群调度的技术路线与风险控制",
        "设计实时风控平台的架构，含规则引擎、特征平台与回溯能力",
        "制定服务治理标准，含超时、重试、熔断与限流的分层策略",
        "评审并重构核心支付链路，输出分期计划、监控指标与回滚方案",
    ),
}

# 形态变体：让数值特征（代码块 / 列表 / JSON / 长度 / 问号）有足够覆盖
_SUFFIXES: tuple[str, ...] = (
    "",
    "。",
    "？",
    "\n\n```python\ndef f(x):\n    return x + 1\n```",
    "\n\n- 先看现状\n- 再定方案\n- 最后落地",
    "\n\n补充背景：这是核心链路，涉及多个团队协作。",
    "\n\n{\"env\": \"prod\", \"replicas\": 3}",
    "\n\n要求：可观测、可回滚、有压测数据支撑，并给出风险评估与成本估算。",
)


def build_corpus() -> list[dict[str, object]]:
    """模板 × 变体 → 训练样本；标签由规则路由给出（确定性）。"""
    samples: list[dict[str, object]] = []
    for _tier, prompts in _BASE.items():
        for prompt in prompts:
            for suffix in _SUFFIXES:
                text = prompt + suffix
                decision = rule_route(extract(text))
                samples.append({
                    "text": text,
                    "tier": decision.tier_idx,
                    "weight": 1.0,
                    "features": None,
                    "sem": None,
                    "text_hash": "",
                })
    return samples


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="规则蒸馏出厂无语义兜底产物（routivus/assets/router.lgb.nosem）",
    )
    parser.add_argument("--out", default=None, help=f"产物路径，默认 {DEFAULT_OUT}")
    parser.add_argument("--n-estimators", type=int, default=300)
    args = parser.parse_args(argv)

    out_path = Path(args.out) if args.out else DEFAULT_OUT
    samples = build_corpus()
    dist = Counter(int(s["tier"]) for s in samples)
    print(f"语料：{len(samples)} 条；规则标签分布 "
          f"{ {f'档{k}': v for k, v in sorted(dist.items())} }")
    if len(dist) < 2:
        print("错误：规则标签只有一个档位，语料无区分度", file=sys.stderr)
        return 1

    report = train_and_save(
        samples, out_path, semantic=None, n_estimators=args.n_estimators,
    )
    acc = f"{report['val_accuracy']:.3f}" if report["val_accuracy"] is not None else "N/A"
    print(f"训练完成：{report['n_samples']} 样本"
          f"（训练 {report['n_train']} / 验证 {report['n_val']}），"
          f"验证准确率 {acc}，语义列 {report['sem_dim']} 维")
    print(f"产物：{out_path}（{report['artifact_bytes'] / 1024:.0f} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
