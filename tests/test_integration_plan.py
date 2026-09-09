"""集成测试：mock LLM 驱动真实工具，/plan 端到端流程。"""

from __future__ import annotations

import json
from typing import AsyncIterator

from routivus.agent.plan import PlanEvent, PlanExecutor, ReviewDecision
from routivus.llm.client import LlmClient
from routivus.llm.types import StreamEvent, ToolCall
from routivus.safety.audit import AuditLogger
from routivus.safety.guards import guard_tool_call
from routivus.safety.hitl import HITLPolicy
from routivus.tool.builtin import build_registry


class RoutingScriptClient(LlmClient):
    """按调用角色路由的脚本 mock：

    - 规划调用（system 含「任务规划器」）：依次弹出 plan_script
    - 子任务调用：从 user 消息提取「子任务 tN」，弹出对应脚本
      脚本项为 (content, [(tool, args_json), ...])，最终回答以空 calls 结束
    """

    def __init__(self, plan_script: list[str], subtask_script: dict[str, list]):
        self.plan_script = list(plan_script)
        self.subtask_script = {k: list(v) for k, v in subtask_script.items()}
        self.seen_messages: list[list[dict]] = []

    async def stream_chat(self, messages, tools=None) -> AsyncIterator[StreamEvent]:
        import re

        self.seen_messages.append([m.to_api() for m in messages])
        system = messages[0].content if messages else ""
        if "任务规划器" in system:
            raw = self.plan_script.pop(0)
            yield StreamEvent(kind="content", text=raw)
            yield StreamEvent(kind="done", finish_reason="stop")
            return

        user = next(m.content for m in messages if m.role == "user")
        m = re.search(r"子任务 (t\d+)", user)
        assert m is not None, f"无法识别子任务 id: {user[:80]}"
        tid = m.group(1)
        content, calls = self.subtask_script[tid].pop(0)
        if content:
            yield StreamEvent(kind="content", text=content)
        for name, args in calls:
            yield StreamEvent(
                kind="tool_call",
                tool_call=ToolCall(id=f"call_{tid}_{name}", name=name, arguments=args),
            )
        yield StreamEvent(kind="done", finish_reason="tool_calls" if calls else "stop")


PLAN = json.dumps({"tasks": [
    {"id": "t1", "title": "写入文件", "description": "用 write_file 写 out.txt", "deps": []},
    {"id": "t2", "title": "读取验证", "description": "用 read_file 读 out.txt", "deps": ["t1"]},
]}, ensure_ascii=False)


async def test_plan_end_to_end(tmp_path, settings):
    """拆解 → 审阅通过 → 按批次执行（真实工具落盘）→ 汇总 plan_done。"""
    client = RoutingScriptClient(
        plan_script=[PLAN],
        subtask_script={
            "t1": [
                ("", [("write_file", json.dumps({"path": "out.txt", "content": "hello plan"}))]),
                ("out.txt 已写入 hello plan", []),
            ],
            "t2": [
                ("", [("read_file", json.dumps({"path": "out.txt"}))]),
                ("内容验证一致: hello plan", []),
            ],
        },
    )
    audit = AuditLogger(tmp_path / ".routivus" / "audit.log")
    registry = build_registry(
        base_dir=tmp_path,
        guard=lambda n, a: guard_tool_call(tmp_path, n, a),
        audit=audit,
    )

    async def reviewer(plan):
        return ReviewDecision(action="execute")

    executor = PlanExecutor(
        llm=client, tools=registry, settings=settings, reviewer=reviewer, audit=audit,
    )
    events = [e async for e in executor.run("写入并验证文件")]

    # 文件真实落盘
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "hello plan"

    # 事件流：拆解 → 审阅 → 批次1（t1）→ 批次2（t2）→ 汇总
    kinds = [e.kind for e in events]
    assert kinds[0] == "plan_generated"
    assert kinds[1] == "review"
    assert kinds[2] == "approved"
    assert events[-1].kind == "plan_done"
    assert events[-1].message == "计划完成: 2/2 个子任务成功"

    # 批次正确：t1 先于 t2
    started = [e.task.id for e in events if e.kind == "subtask_started"]
    assert started == ["t1", "t2"]

    # 依赖结果注入：t2 的 system 上下文含 t1 的汇报
    # 请求序：0=规划, 1=t1 首轮, 2=t1 收尾, 3=t2 首轮
    t2_request = client.seen_messages[3]
    assert "out.txt 已写入 hello plan" in t2_request[0]["content"]

    # 审计含 subtask 事件
    log_lines = (tmp_path / ".routivus" / "audit.log").read_text(encoding="utf-8").strip().splitlines()
    actions = [json.loads(line)["action"] for line in log_lines]
    assert "subtask_started" in actions
    assert "subtask_done" in actions
    assert "tool_call" in actions  # 子任务内工具调用也入审计


async def test_plan_review_cancel_no_side_effect(tmp_path, settings):
    """审阅取消：不执行任何工具，无文件副作用。"""
    client = RoutingScriptClient(
        plan_script=[PLAN],
        subtask_script={
            "t1": [("", [("write_file", json.dumps({"path": "out.txt", "content": "x"}))])],
            "t2": [],
        },
    )
    registry = build_registry(base_dir=tmp_path)
    audit = AuditLogger(tmp_path / ".routivus" / "audit.log")

    async def reviewer(plan):
        return ReviewDecision(action="cancel")

    executor = PlanExecutor(
        llm=client, tools=registry, settings=settings, reviewer=reviewer, audit=audit,
    )
    events = [e async for e in executor.run("写入并验证文件")]

    assert [e.kind for e in events] == ["plan_generated", "review", "cancelled"]
    assert not (tmp_path / "out.txt").exists()
    # 只有规划调用，无任何子任务 / 工具调用
    assert len(client.seen_messages) == 1
    log_path = tmp_path / ".routivus" / "audit.log"
    if log_path.exists():  # 取消时可能未写入任何审计记录
        log_lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert all(
            json.loads(l)["action"] not in ("tool_call", "subtask_started") for l in log_lines
        )


async def test_plan_replan_then_execute(tmp_path, settings):
    """重规划后按新计划执行。"""
    new_plan = json.dumps({"tasks": [
        {"id": "t1", "title": "直接写新文件", "description": "写 new.txt", "deps": []},
    ]}, ensure_ascii=False)
    client = RoutingScriptClient(
        plan_script=[PLAN, new_plan],
        subtask_script={
            "t1": [
                ("", [("write_file", json.dumps({"path": "new.txt", "content": "v2"}))]),
                ("new.txt 已写入", []),
            ],
            "t2": [],
        },
    )
    registry = build_registry(base_dir=tmp_path)

    calls = []

    async def reviewer(plan):
        calls.append(plan)
        if len(calls) == 1:
            return ReviewDecision(action="replan", feedback="不需要验证步骤")
        return ReviewDecision(action="execute")

    executor = PlanExecutor(llm=client, tools=registry, settings=settings, reviewer=reviewer)
    events = [e async for e in executor.run("写文件")]

    kinds = [e.kind for e in events]
    assert kinds[:4] == ["plan_generated", "review", "replanned", "plan_generated"]
    assert events[-1].kind == "plan_done"
    # 执行的是新计划（唯一子任务 t1 标题为「直接写新文件」），旧计划的验证任务不存在
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "v2"
    assert not (tmp_path / "out.txt").exists()


async def test_plan_subtask_hitl_integration(tmp_path, settings):
    """子任务内写文件需 HITL 审批：批准后执行。"""
    client = RoutingScriptClient(
        plan_script=[PLAN],
        subtask_script={
            "t1": [
                ("", [("write_file", json.dumps({"path": "out.txt", "content": "hitl"}))]),
                ("写入完成", []),
            ],
            "t2": [("无需验证", [])],
        },
    )
    registry = build_registry(base_dir=tmp_path)

    decisions = []

    async def requester(tool_name, level, args):
        decisions.append(tool_name)
        from routivus.safety.hitl import ApprovalDecision

        return ApprovalDecision(allow=True, reason="user_approved")

    policy = HITLPolicy(enabled=True, requester=requester)
    audit = AuditLogger(tmp_path / ".routivus" / "audit.log")

    async def reviewer(plan):
        return ReviewDecision(action="execute")

    executor = PlanExecutor(
        llm=client, tools=registry, settings=settings,
        reviewer=reviewer, approval_policy=policy, audit=audit,
    )
    events = [e async for e in executor.run("写文件")]

    # 审批发生在子任务执行内
    assert decisions == ["write_file"]
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "hitl"
    # 审批事件被转发
    approvals = [e for e in events if e.kind == "subtask_event" and e.agent_event.kind == "approval"]
    assert approvals and approvals[0].agent_event.text == "approved"
