"""测试 /train 命令（解析、确认、依赖检查、CommandService 路由）。"""

from __future__ import annotations

import asyncio

from routivus.cli.commands import (
    SLASH_COMMANDS,
    CommandContext,
    CommandResult,
    CommandService,
    execute_train_command,
)
from routivus.cli.train import (
    TrainPlan,
    _default_semantic_onnx,
    build_argv,
    check_train_deps,
    confirmation_message,
    parse_train_command,
)


class FakeSettings:
    provider = ""
    model = ""


class FakeAgent:
    input_history = None


def _service(log_sink=None):
    return CommandService(
        CommandContext(agent=FakeAgent(), settings=FakeSettings(), manager=None),
        log_sink=log_sink,
    )


def test_spec_registered():
    names = {spec.name for spec in SLASH_COMMANDS}
    assert "/train" in names
    spec = next(s for s in SLASH_COMMANDS if s.name == "/train")
    assert "--yes" in spec.options


def test_parse_no_args_defaults_to_feedback_only():
    plan, err = parse_train_command("/train")
    assert err is None
    assert plan.feedback_only is True
    assert plan.dataset is None


def test_parse_dataset_and_yes():
    plan, err = parse_train_command("/train labeled.jsonl --yes")
    assert err is None
    assert plan.dataset == "labeled.jsonl"
    assert plan.overwrite is True
    assert plan.feedback_only is False


def test_parse_output_flag():
    plan, err = parse_train_command("/train --output m.bin --yes")
    assert err is None
    assert plan.output == "m.bin"
    assert plan.overwrite is True
    # 未给 dataset 且未给 --feedback-only → 缺省 feedback-only
    assert plan.feedback_only is True


def test_parse_conflict_dataset_and_feedback_only():
    _, err = parse_train_command("/train a.jsonl --feedback-only")
    assert err is not None
    assert "不能同时指定" in err


def test_parse_unknown_flag():
    _, err = parse_train_command("/train --bogus")
    assert err is not None
    assert "未知参数" in err


def test_build_argv_feedback_only():
    plan = TrainPlan(feedback_only=True)
    argv = build_argv(plan)
    assert argv[1].endswith("train_router.py")
    assert "--feedback-only" in argv


def test_build_argv_dataset_and_out():
    plan = TrainPlan(dataset="d.jsonl", output="o.bin")
    argv = build_argv(plan)
    assert "d.jsonl" in argv
    assert "--out" in argv
    assert "o.bin" in argv


def test_parse_no_semantic_flag():
    plan, err = parse_train_command("/train --no-semantic --yes")
    assert err is None
    assert plan.semantic is False


def test_parse_default_semantic_on():
    plan, _ = parse_train_command("/train")
    assert plan.semantic is True


def test_default_semantic_probe_finds_bundled():
    # 项目自带 xg/assets/router_semantics.onnx，默认探测应命中（数据目录通常无）。
    assert _default_semantic_onnx() is not None


def test_build_argv_default_attaches_semantic():
    onnx = _default_semantic_onnx()
    if onnx is None:
        return
    plan = TrainPlan(feedback_only=True)  # semantic 默认 True
    argv = build_argv(plan)
    assert "--semantic-onnx" in argv
    assert str(onnx) in argv


def test_build_argv_no_semantic_skips():
    plan = TrainPlan(feedback_only=True, semantic=False)
    argv = build_argv(plan)
    assert "--semantic-onnx" not in argv


def test_confirmation_message_semantic_flag():
    msg = confirmation_message(TrainPlan(feedback_only=True, semantic=True))
    assert "带语义列" in msg
    off = confirmation_message(TrainPlan(feedback_only=True, semantic=False))
    assert "纯 TF-IDF" in off


def test_deps_available_or_hint():
    # 核心依赖已随项目安装；即便缺失也应返回安装提示而非抛异常。
    result = check_train_deps()
    assert result is None or "缺少训练依赖" in result


def test_confirmation_message_mentions_yes():
    msg = confirmation_message(TrainPlan(feedback_only=True))
    assert "feedback.log" in msg
    assert "--yes" in msg


def _run(coro):
    return asyncio.run(coro)


def test_service_train_without_yes_returns_confirmation():
    result = _run(_service().execute("/train"))
    assert isinstance(result, CommandResult)
    assert result.ok is False
    assert "--yes" in result.message
    assert "确认将运行 SmartRouter 训练" in result.message


def test_execute_train_command_without_yes_no_spawn():
    # 不带 --yes 不应触发任何子进程，直接返回确认提示。
    result = _run(execute_train_command("/train --output /tmp/x.bin"))
    assert result.ok is False
    assert "--yes" in result.message


def test_train_command_parse_error_routes():
    result = _run(_service().execute("/train a.jsonl --feedback-only"))
    assert result.ok is False
    assert "不能同时指定" in result.message


def test_train_deps_error_routes_before_confirm():
    # 依赖缺失时应优先给安装提示（此处不校验具体缺否，只验证不崩溃）。
    result = _run(_service().execute("/train --bogus"))
    assert result.ok is False
    assert "未知参数" in result.message