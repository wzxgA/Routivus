"""Translate Agent/Plan events to UI state.

No terminal, network, database, or Textual operation belongs in this module.
That makes event ordering and stale-event handling straightforward to test.
"""

from __future__ import annotations

from dataclasses import replace
import re
from typing import Any

from xg.agent.plan import PlanEvent
from xg.agent.react import AgentEvent
from xg.agent.team import TeamEvent
from xg.llm.types import Usage
from xg.tui.state import (
    AgentGroupState,
    PlanInspectorSnapshot,
    PlanTaskSnapshot,
    SafetyInspectorSnapshot,
    SessionInspectorSnapshot,
    SmartRouterSnapshot,
    TuiState,
    TranscriptItem,
)


def _copy(state: TuiState) -> TuiState:
    return replace(
        state,
        transcript=[replace(item) for item in state.transcript],
        plan_tasks=dict(state.plan_tasks),
        agent_groups={
            key: replace(group, entries=[replace(item) for item in group.entries])
            for key, group in state.agent_groups.items()
        },
        agent_group_order=list(state.agent_group_order),
        team_input_scope=list(state.team_input_scope),
        inspector=replace(
            state.inspector,
            session=replace(state.inspector.session),
            plan=replace(state.inspector.plan),
            memory=replace(state.inspector.memory),
            safety=replace(state.inspector.safety),
            smart_router=replace(state.inspector.smart_router),
        ),
    )


def set_smart_router_snapshot(state: TuiState, snapshot: SmartRouterSnapshot) -> TuiState:
    """Replace the inspector SmartRouter snapshot (routing result / toggle).

    纯数据层替换（phase-02 步骤 A）：不产生 transcript 项、不影响 phase。
    """
    out = _copy(state)
    out.inspector = replace(out.inspector, smart_router=snapshot)
    return out


def _append(state: TuiState, item: TranscriptItem) -> None:
    state.transcript.append(item)


def _team_group_key(event: TeamEvent) -> str:
    """Return a stable UI identity for one logical Team AgentRun."""
    if event.agent_id:
        return f"{event.team_id}:{event.agent_id}"
    task_id = event.task.id if event.task is not None else "unknown"
    role = event.role or ("reviewer" if event.kind.startswith("task_review") else "agent")
    return f"{event.team_id}:{role}:{task_id}"


def _ensure_agent_group(
    state: TuiState,
    event: TeamEvent,
    *,
    group_id: str | None = None,
    role: str | None = None,
    task=None,
) -> tuple[AgentGroupState, str]:
    task = task or event.task
    key = group_id or _team_group_key(event)
    group = state.agent_groups.get(key)
    if group is not None:
        return group, key
    task_id = task.id if task is not None else ""
    task_title = task.title if task is not None else ""
    group_role = role or event.role or "agent"
    agent_id = event.agent_id or key.rsplit(":", 1)[-1]
    group = AgentGroupState(
        group_id=key,
        team_id=event.team_id,
        agent_id=agent_id,
        role=group_role,
        task_id=task_id,
        task_title=task_title or task_id or group_role,
        resource_scope_mode=getattr(task, "resource_scope_mode", "targeted"),
        attempt=event.attempt,
        effective_steps=event.effective_steps,
        failure_category=event.failure_category,
    )
    state.agent_groups[key] = group
    state.agent_group_order.append(key)
    state.transcript.append(TranscriptItem(
        id=f"agent-group-{len(state.transcript)}",
        kind="agent_group",
        text=group.task_title,
        agent_group_id=key,
        turn_id=state.active_turn_id,
        trace_id=f"{state.active_turn_id}:{key}",
        collapsible=True,
        collapsed=True,
    ))
    return group, key


def _update_agent_group(state: TuiState, group_id: str, **changes) -> None:
    group = state.agent_groups[group_id]
    state.agent_groups[group_id] = replace(group, **changes)


def _append_group_entry(
    state: TuiState,
    group_id: str,
    item: TranscriptItem,
    *,
    summary: str = "",
    error: str = "",
    tool: bool = False,
    artifact: bool = False,
) -> None:
    group = state.agent_groups[group_id]
    item.agent_group_id = group_id
    group.entries.append(item)
    state.agent_groups[group_id] = replace(
        group,
        event_count=group.event_count + 1,
        tool_count=group.tool_count + (1 if tool else 0),
        artifact_count=group.artifact_count + (1 if artifact else 0),
        latest_summary=summary[:240] if summary else group.latest_summary,
        latest_error=error[:240] if error else group.latest_error,
    )


def _reduce_agent_event_in_group(
    state: TuiState,
    group_id: str,
    event: AgentEvent,
    turn_id: str,
    trace_id: str,
) -> TuiState:
    """Reuse the Agent reducer against a group's private entry list."""
    group = state.agent_groups[group_id]
    sandbox = replace(state, transcript=[replace(item) for item in group.entries])
    reduced = reduce_agent_event(sandbox, event, turn_id, trace_id)
    out = _copy(state)
    entries = [replace(item, agent_group_id=group_id) for item in reduced.transcript]
    out.agent_groups[group_id] = replace(
        out.agent_groups[group_id],
        entries=entries,
        event_count=out.agent_groups[group_id].event_count + 1,
        tool_count=out.agent_groups[group_id].tool_count + (1 if event.kind == "tool_call" else 0),
        latest_summary=(event.text or out.agent_groups[group_id].latest_summary)[:240],
        latest_error=(event.text if event.kind == "error" else out.agent_groups[group_id].latest_error)[:240],
    )
    out.inspector = reduced.inspector
    out.pending_approval = reduced.pending_approval
    out.pending_confirmation = reduced.pending_confirmation
    out.notification = reduced.notification
    out.notification_level = reduced.notification_level
    if event.kind not in {"error", "context_overflow", "budget_exceeded"}:
        out.phase = "running"
    return out


def _finish_agent_groups(state: TuiState, status: str) -> None:
    for key, group in state.agent_groups.items():
        if group.status in {"pending", "running", "reviewing", "repairing"}:
            state.agent_groups[key] = replace(group, status=status)


def _remove_progress(state: TuiState, turn_id: str) -> None:
    """Remove local waiting indicators without touching Agent history."""
    state.transcript[:] = [
        item for item in state.transcript
        if not (item.kind == "progress" and item.turn_id == turn_id)
    ]


def _update_progress(state: TuiState, turn_id: str, text: str) -> None:
    """Update a local waiting indicator without creating a transcript item."""
    if not text:
        return
    for item in reversed(state.transcript):
        if item.kind == "progress" and item.turn_id == turn_id:
            item.progress_kind = "context"
            item.text = text
            return


def _trace_id(turn_id: str, trace_id: str | None) -> str:
    return trace_id or turn_id


def _ratio(value: int, limit: int) -> float:
    return value / limit if limit > 0 else 0.0


def _valid_usage(usage: Usage | None) -> bool:
    if usage is None:
        return False
    values = (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens)
    return all(
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
        for value in values
    )


def _update_context_usage(out: TuiState, event: AgentEvent) -> None:
    old = out.inspector.usage
    estimated = max(0, event.estimated_prompt_tokens or 0)
    window = event.context_window if event.context_window is not None else old.context_window
    limit = event.request_token_limit if event.request_token_limit is not None else old.request_token_limit
    compacted = event.compaction_before is not None and event.compaction_after is not None
    usage = replace(
        old,
        estimated_prompt_tokens=estimated,
        context_window=max(0, window or 0),
        request_token_limit=max(0, limit or 0),
        window_ratio=_ratio(estimated, window or 0),
        budget_usage_ratio=_ratio(estimated, limit or 0),
        usage_source="estimated",
        compaction_count=old.compaction_count + (1 if compacted else 0),
        last_compaction_before=(event.compaction_before or 0) if compacted else old.last_compaction_before,
        last_compaction_after=(event.compaction_after or 0) if compacted else old.last_compaction_after,
    )
    out.inspector = replace(
        out.inspector,
        usage=usage,
        context_tokens=estimated,
        context_window=max(0, window or 0),
        session=replace(out.inspector.session, status=_phase_status(out.phase)),
    )


def _update_provider_usage(out: TuiState, usage_value: Usage | None) -> None:
    if not _valid_usage(usage_value):
        return
    assert usage_value is not None
    old = out.inspector.usage
    usage = replace(
        old,
        last_prompt_tokens=usage_value.prompt_tokens,
        last_completion_tokens=usage_value.completion_tokens,
        last_total_tokens=usage_value.total_tokens,
        session_prompt_tokens=old.session_prompt_tokens + usage_value.prompt_tokens,
        session_completion_tokens=old.session_completion_tokens + usage_value.completion_tokens,
        session_total_tokens=old.session_total_tokens + usage_value.total_tokens,
        usage_source="provider",
    )
    out.inspector = replace(
        out.inspector,
        usage=usage,
        session=replace(out.inspector.session, status=_phase_status(out.phase)),
    )


def _phase_status(phase: str) -> str:
    return {
        "idle": "Idle",
        "running": "Working",
        "awaiting_approval": "Waiting approval",
        "awaiting_plan_review": "Plan review",
        "error": "Error",
    }.get(phase, phase)


def _plan_snapshot(
    plan,
    *,
    status: str | None = None,
    current_round: int | None = None,
    total_rounds: int | None = None,
    previous: PlanInspectorSnapshot | None = None,
) -> PlanInspectorSnapshot:
    tasks = tuple(
        PlanTaskSnapshot(id=task.id, title=task.title, status=task.status)
        for task in (plan.tasks if plan is not None else ())
    )
    done = sum(task.status == "done" for task in tasks)
    failures = sum(task.status == "failed" for task in tasks)
    old = previous or PlanInspectorSnapshot()
    return PlanInspectorSnapshot(
        goal=plan.goal if plan is not None else old.goal,
        status=status if status is not None else old.status,
        current_round=current_round if current_round is not None else old.current_round,
        total_rounds=total_rounds if total_rounds is not None else old.total_rounds,
        completed_tasks=done,
        total_tasks=len(tasks),
        failure_count=failures,
        tasks=tasks or old.tasks,
    )


def _set_safety_decision(out: TuiState, event: AgentEvent) -> None:
    decision = event.decision
    if decision is None:
        return
    status = event.text or ("approved" if decision.allow else "rejected")
    out.inspector = replace(
        out.inspector,
        safety=replace(
            out.inspector.safety,
            approval_status=status,
            current_tool="",
            current_level="",
            last_decision=status,
            last_reason=decision.reason,
            last_rejection=(decision.reason if not decision.allow else out.inspector.safety.last_rejection),
        ),
    )


def _finish_streaming_text(state: TuiState, trace_id: str) -> None:
    for item in reversed(state.transcript):
        if item.trace_id != trace_id:
            continue
        if item.kind in ("assistant", "thinking") and item.streaming:
            item.streaming = False
            if item.kind == "thinking":
                item.status = "done"
                if item.user_collapsed is None:
                    item.collapsed = True
            return


def _reclassify_intermediate_assistant(state: TuiState, turn_id: str, trace_id: str) -> None:
    """Treat pre-tool plain text as thinking when no explicit event exists."""
    for item in reversed(state.transcript):
        if item.turn_id != turn_id or item.trace_id != trace_id:
            continue
        if item.kind != "assistant" or not item.text:
            return
        item.kind = "thinking"
        item.collapsible = True
        item.collapsed = True
        item.user_collapsed = None
        item.streaming = False
        item.status = "done"
        return


def _assistant(state: TuiState, turn_id: str, trace_id: str) -> TranscriptItem:
    if state.transcript and state.transcript[-1].kind == "assistant" and state.transcript[-1].streaming and state.transcript[-1].trace_id == trace_id:
        return state.transcript[-1]
    item = TranscriptItem(
        id=f"assistant-{len(state.transcript)}", kind="assistant", streaming=True,
        collapsed=False, turn_id=turn_id, trace_id=trace_id, status="streaming",
    )
    _append(state, item)
    return item


def _thinking(state: TuiState, turn_id: str, trace_id: str) -> TranscriptItem:
    if state.transcript and state.transcript[-1].kind == "thinking" and state.transcript[-1].streaming and state.transcript[-1].trace_id == trace_id:
        return state.transcript[-1]
    item = TranscriptItem(
        id=f"thinking-{len(state.transcript)}", kind="thinking", collapsible=True,
        collapsed=False, streaming=True, turn_id=turn_id, trace_id=trace_id,
        status="streaming",
    )
    _append(state, item)
    return item


def _collapse_trace(state: TuiState, trace_id: str, *, status: str = "done") -> None:
    for item in state.transcript:
        if item.trace_id != trace_id or not item.collapsible:
            continue
        item.streaming = False
        if item.user_collapsed is None:
            item.collapsed = True
        if item.status in ("streaming", "running"):
            item.status = status  # type: ignore[assignment]


def _collapse_turn(state: TuiState, turn_id: str, *, status: str = "done") -> None:
    for item in state.transcript:
        if item.trace_id == turn_id or item.trace_id.startswith(f"{turn_id}:"):
            _collapse_trace(state, item.trace_id, status=status)
            if item.kind == "assistant" and item.trace_id.startswith(f"{turn_id}:"):
                item.collapsible = True
                if item.user_collapsed is None:
                    item.collapsed = True


def finalize_trace(state: TuiState, turn_id: str, *, status: str = "cancelled") -> TuiState:
    """Finalize a turn when its worker is cancelled outside the event stream."""
    out = _copy(state)
    _remove_progress(out, turn_id)
    _collapse_turn(out, turn_id, status=status)
    return out


def reduce_agent_event(
    state: TuiState,
    event: AgentEvent,
    turn_id: str | None = None,
    trace_id: str | None = None,
) -> TuiState:
    """Return the next state for one ReAct event."""
    turn_id = turn_id or state.active_turn_id
    if state.active_turn_id and turn_id and state.active_turn_id != turn_id:
        return state
    out = _copy(state)
    trace_id = _trace_id(turn_id, trace_id)
    kind = event.kind
    if kind in {
        "thinking", "content", "tool_call", "tool_result", "approval", "ask_user",
        "error", "context_overflow", "budget_exceeded", "step_limit", "retrying", "done",
    }:
        _remove_progress(out, turn_id)
    if event.estimated_prompt_tokens is not None:
        _update_context_usage(out, event)
    if kind == "context_usage":
        return out
    if kind == "usage":
        _update_provider_usage(out, event.usage)
        return out
    if kind == "retrying":
        _append(out, TranscriptItem(
            id=f"retry-{len(out.transcript)}", kind="system",
            text=(
                f"{event.text or 'API 临时故障，正在重试'} "
                f"({event.retry_attempts}/{event.retry_max_attempts})，"
                f"等待 {event.retry_delay or 0:.1f}s"
            ),
            turn_id=turn_id, trace_id=trace_id,
        ))
        out.notification = event.text or "API 临时故障，正在重试"
        out.notification_level = "warning"
        return out
    if kind == "thinking":
        if not (out.transcript and out.transcript[-1].kind == "thinking" and out.transcript[-1].streaming and out.transcript[-1].trace_id == trace_id):
            _finish_streaming_text(out, trace_id)
        item = _thinking(out, turn_id, trace_id)
        item.text += event.text
        return out
    if kind == "content":
        if out.transcript and out.transcript[-1].kind == "thinking" and out.transcript[-1].trace_id == trace_id:
            _finish_streaming_text(out, trace_id)
        item = _assistant(out, turn_id, trace_id)
        item.text += event.text
        return out
    if kind == "tool_call" and event.tool_call:
        _finish_streaming_text(out, trace_id)
        _reclassify_intermediate_assistant(out, turn_id, trace_id)
        call = event.tool_call
        _append(out, TranscriptItem(
            id=call.id or f"tool-{len(out.transcript)}", kind="tool_call",
            tool_name=call.name, tool_args=call.arguments, tool_call_id=call.id,
            turn_id=turn_id, trace_id=trace_id, collapsible=True,
            collapsed=False, status="running",
        ))
        out.phase = "running"
        return out
    if kind == "ask_user" and event.ask:
        out.phase = "awaiting_ask"
        out.pending_ask = event.ask
        _append(out, TranscriptItem(
            id=f"ask-{event.ask.id}", kind="system",
            text=f"[需要你确认 @{event.ask.origin}]" if event.ask.origin else "[需要你确认]",
            turn_id=turn_id, trace_id=trace_id,
        ))
        return out
    if kind == "tool_result" and event.tool_result:
        result = event.tool_result
        for item in reversed(out.transcript):
            if item.kind == "tool_call" and item.tool_call_id == result.tool_call_id:
                item.tool_ok = result.ok
                item.status = "success" if result.ok else "failed"
                if item.user_collapsed is None:
                    item.collapsed = True
                break
        # ask_user 的 tool_result 到达时收回等待态，恢复执行。
        if result.name == "ask_user":
            out.pending_ask = None
            out.phase = "running"
        _append(out, TranscriptItem(
            id=f"result-{result.tool_call_id or len(out.transcript)}", kind="tool_result",
            text=result.output or result.error, tool_name=result.name,
            tool_call_id=result.tool_call_id, tool_ok=result.ok, turn_id=turn_id,
            trace_id=trace_id, collapsible=True, collapsed=True,
            status="success" if result.ok else "failed", parent_call_id=result.tool_call_id,
        ))
        if not result.ok:
            out.inspector = replace(
                out.inspector,
                safety=replace(
                    out.inspector.safety,
                    last_rejection=result.error or result.output or "tool_failed",
                ),
            )
        return out
    if kind == "approval":
        if event.tool_call:
            _append(out, TranscriptItem(
                id=f"approval-{event.tool_call.id or len(out.transcript)}", kind="approval",
                text=event.text, tool_name=event.tool_call.name,
                tool_args=event.tool_call.arguments, turn_id=turn_id, trace_id=trace_id,
                collapsible=True, collapsed=True, status="done",
            ))
        out.pending_approval = None
        out.phase = "running"
        _set_safety_decision(out, event)
        return out
    if kind in ("context_compacted", "context_warning"):
        if kind == "context_compacted":
            _update_progress(out, turn_id, "正在整理上下文")
        _append(out, TranscriptItem(id=f"context-{len(out.transcript)}", kind="context", text=event.text, turn_id=turn_id))
        out.notification = event.text
        out.notification_level = "warning" if kind == "context_warning" else "info"
        return out
    if kind in ("error", "context_overflow", "budget_exceeded"):
        text = event.text or "任务执行失败"
        _append(out, TranscriptItem(id=f"error-{len(out.transcript)}", kind="error", text=text, turn_id=turn_id))
        out.phase = "error"
        out.notification = text
        out.notification_level = "error"
        out.pending_approval = None
        out.pending_ask = None
        _collapse_trace(out, trace_id, status="failed")
        return out
    if kind in ("step_limit", "done"):
        _update_provider_usage(out, event.usage)
        for item in reversed(out.transcript):
            if item.kind == "assistant" and item.trace_id == trace_id:
                item.streaming = False
                item.status = "done"
                break
        _collapse_trace(out, trace_id, status="done")
        out.phase = "idle"
        out.pending_approval = None
        out.pending_ask = None
        if kind == "step_limit":
            out.notification = "已达到本轮步骤上限"
            out.notification_level = "warning"
        return out
    return out


def reduce_plan_event(state: TuiState, event: PlanEvent, turn_id: str | None = None) -> TuiState:
    """Return the next state for one Plan event, including nested agent events."""
    turn_id = turn_id or state.active_turn_id
    if state.active_turn_id and turn_id and state.active_turn_id != turn_id:
        return state
    out = _copy(state)
    kind = event.kind
    if kind in {"plan_generated", "plan_done", "cancelled", "plan_failed"}:
        _remove_progress(out, turn_id)
    if kind == "plan_generated" and event.plan:
        if event.estimated_prompt_tokens is not None:
            _update_context_usage(out, AgentEvent(
                kind="context_usage",
                estimated_prompt_tokens=event.estimated_prompt_tokens,
                request_token_limit=event.request_token_limit,
                context_window=event.context_window,
                compaction_before=event.compaction_before,
                compaction_after=event.compaction_after,
            ))
        _update_provider_usage(out, event.usage)
        out.pending_plan = event.plan
        out.phase = "awaiting_plan_review"
        out.inspector = replace(
            out.inspector,
            plan_status="review",
            plan=_plan_snapshot(
                event.plan,
                status="review",
                current_round=0,
                total_rounds=len(event.plan.batches),
                previous=out.inspector.plan,
            ),
            session=replace(out.inspector.session, status="Plan review"),
        )
        out.plan_tasks = {task.id: task.status for task in event.plan.tasks}
        _append(out, TranscriptItem(
            id=f"plan-{len(out.transcript)}", kind="plan", text=event.plan.goal,
            plan=event.plan, plan_review=True, collapsed=True, turn_id=turn_id,
        ))
        return out
    if kind == "review":
        out.phase = "awaiting_plan_review"
        return out
    if kind in ("approved", "replanned"):
        for item in reversed(out.transcript):
            if item.kind == "plan" and item.turn_id == turn_id and (event.plan is None or item.plan is event.plan):
                item.plan_review = False
                break
        out.phase = "running"
        out.pending_plan = None
        out.inspector = replace(
            out.inspector,
            plan_status="running",
            plan=replace(out.inspector.plan, status="running"),
            session=replace(out.inspector.session, status="Working"),
        )
        return out
    if kind == "batch_started":
        out.inspector = replace(
            out.inspector,
            batch=event.message,
            plan=_plan_snapshot(
                event.plan,
                status="running",
                current_round=(
                    event.plan.batches.index(event.batch) + 1
                    if event.plan is not None and event.batch in event.plan.batches else out.inspector.plan.current_round
                ),
                total_rounds=len(event.plan.batches) if event.plan is not None else out.inspector.plan.total_rounds,
                previous=out.inspector.plan,
            ),
        )
        out.phase = "running"
        return out
    if kind == "subtask_started" and event.task:
        out.plan_tasks[event.task.id] = event.task.status
        plan = event.plan
        tasks = list(out.inspector.plan.tasks)
        for index, task in enumerate(tasks):
            if task.id == event.task.id:
                tasks[index] = PlanTaskSnapshot(task.id, task.title, event.task.status)
                break
        out.inspector = replace(
            out.inspector,
            plan=replace(
                out.inspector.plan,
                goal=plan.goal if plan is not None else out.inspector.plan.goal,
                tasks=tuple(tasks),
                total_tasks=len(tasks),
                completed_tasks=sum(task.status == "done" for task in tasks),
                failure_count=sum(task.status == "failed" for task in tasks),
            ),
        )
        return out
    if kind == "subtask_event" and event.agent_event:
        task_trace = f"{turn_id}:{event.task.id}" if event.task else turn_id
        return reduce_agent_event(out, event.agent_event, turn_id, task_trace)
    if kind == "planner_usage" and event.agent_event:
        return reduce_agent_event(out, event.agent_event, turn_id, f"{turn_id}:planner")
    if kind in ("subtask_done", "subtask_failed") and event.task:
        out.plan_tasks[event.task.id] = event.task.status
        task_trace = f"{turn_id}:{event.task.id}"
        _collapse_trace(out, task_trace, status="failed" if kind == "subtask_failed" else "done")
        for item in out.transcript:
            if item.trace_id == task_trace and item.kind == "assistant":
                item.collapsible = True
                if item.user_collapsed is None:
                    item.collapsed = True
        tasks = [
            PlanTaskSnapshot(task.id, task.title, event.task.status)
            if task.id == event.task.id else task
            for task in out.inspector.plan.tasks
        ]
        out.inspector = replace(
            out.inspector,
            plan=replace(
                out.inspector.plan,
                tasks=tuple(tasks),
                completed_tasks=sum(task.status == "done" for task in tasks),
                failure_count=sum(task.status == "failed" for task in tasks),
            ),
        )
        return out
    if kind == "plan_resume_requested":
        out.phase = "running"
        out.pending_plan = None
        out.inspector = replace(
            out.inspector,
            plan_status="running",
            plan=_plan_snapshot(event.plan, status="running", previous=out.inspector.plan),
            session=replace(out.inspector.session, status="Working"),
        )
        _append(out, TranscriptItem(
            id=f"plan-resume-{len(out.transcript)}", kind="system",
            text=event.message, turn_id=turn_id,
        ))
        out.notification = event.message
        out.notification_level = "info"
        return out
    if kind in ("cancelled", "plan_done"):
        _collapse_turn(out, turn_id, status="cancelled" if kind == "cancelled" else "done")
        out.phase = "idle"
        out.pending_plan = None
        final_status = "done" if kind == "plan_done" else "cancelled"
        plan = _plan_snapshot(
            event.plan,
            status=final_status,
            previous=out.inspector.plan,
        )
        out.inspector = replace(
            out.inspector,
            plan_status=final_status,
            plan=plan,
            session=replace(out.inspector.session, status="Idle"),
        )
        out.notification = event.message
        out.notification_level = "info"
        return out
    if kind == "plan_failed":
        _collapse_turn(out, turn_id, status="failed")
        out.phase = "error"
        out.pending_plan = None
        out.inspector = replace(
            out.inspector,
            plan_status="failed",
            plan=_plan_snapshot(event.plan, status="failed", previous=out.inspector.plan),
            session=replace(out.inspector.session, status="Error"),
        )
        out.notification = event.message
        out.notification_level = "error"
        _append(out, TranscriptItem(id=f"plan-error-{len(out.transcript)}", kind="error", text=event.message, turn_id=turn_id))
        return out
    return out


def reduce_team_event(state: TuiState, event: TeamEvent, turn_id: str | None = None) -> TuiState:
    """将 Team 协作事件归约为现有计划/执行视图。"""
    turn_id = turn_id or state.active_turn_id
    if state.active_turn_id and turn_id and state.active_turn_id != turn_id:
        return state
    out = _copy(state)
    kind = event.kind
    if kind in {"team_plan_generated", "team_done", "cancelled", "team_failed"}:
        _remove_progress(out, turn_id)
    if kind == "team_plan_generated" and event.plan:
        out.pending_plan = event.plan
        out.phase = "awaiting_plan_review"
        out.inspector = replace(
            out.inspector,
            plan_status="review",
            plan=_plan_snapshot(
                event.plan, status="review", current_round=0,
                total_rounds=len(event.plan.batches), previous=out.inspector.plan,
            ),
            session=replace(out.inspector.session, status="Team review"),
        )
        out.plan_tasks = {task.id: task.status for task in event.plan.tasks}
        _append(out, TranscriptItem(
            id=f"team-plan-{len(out.transcript)}", kind="plan", text=event.plan.goal,
            plan=event.plan, plan_review=True, collapsed=True, turn_id=turn_id,
        ))
        if event.message:
            out.notification = event.message
            out.notification_level = "warning"
        return out
    if kind == "team_review":
        out.phase = "awaiting_plan_review"
        return out
    if kind in {"approved", "replanned"}:
        for item in reversed(out.transcript):
            if item.kind == "plan" and item.turn_id == turn_id:
                item.plan_review = False
                break
        out.phase = "running"
        out.pending_plan = None
        out.inspector = replace(
            out.inspector,
            plan_status="running",
            plan=replace(out.inspector.plan, status="running"),
            session=replace(out.inspector.session, status="Working"),
        )
        return out
    if kind == "batch_started":
        out.phase = "running"
        if event.plan:
            round_number = event.plan.batches.index(event.batch) + 1 if event.batch in event.plan.batches else out.inspector.plan.current_round
            out.inspector = replace(
                out.inspector,
                batch=event.message,
                plan=_plan_snapshot(
                    event.plan, status="running", current_round=round_number,
                    total_rounds=len(event.plan.batches), previous=out.inspector.plan,
                ),
            )
        return out
    if kind == "task_retry_started" and event.task:
        _append(out, TranscriptItem(
            id=f"team-task-retry-{len(out.transcript)}", kind="system",
            text=(
                f"[{event.role or event.task.owner_role}/{event.task.id}] 恢复执行："
                f"第 {event.attempt} 次，预算 {event.retry_steps} 步；"
                f"已保留 Artifact {len(event.preserved_artifacts)} 个"
            ),
            turn_id=turn_id, trace_id=f"{turn_id}:{event.task.id}:retry",
        ))
        out.notification = event.message
        out.notification_level = "warning"
        return out
    if kind in {"task_started", "task_done", "task_failed", "task_blocked"} and event.task:
        task_status = {
            "task_started": "running",
            "task_done": "done",
            "task_failed": "failed",
            "task_blocked": "blocked",
        }[kind]
        out.plan_tasks[event.task.id] = task_status
        tasks = list(out.inspector.plan.tasks)
        for index, snapshot in enumerate(tasks):
            if snapshot.id == event.task.id:
                tasks[index] = PlanTaskSnapshot(snapshot.id, snapshot.title, task_status)
                break
        out.inspector = replace(
            out.inspector,
            plan=replace(
                out.inspector.plan,
                tasks=tuple(tasks),
                completed_tasks=sum(item.status == "done" for item in tasks),
                failure_count=sum(item.status == "failed" for item in tasks),
            ),
        )
        if kind == "task_started":
            _append(out, TranscriptItem(
                id=f"team-task-{len(out.transcript)}", kind="system",
                text=f"[{event.role or event.task.owner_role}/{event.task.id}] 开始：{event.task.title}",
                turn_id=turn_id, trace_id=f"{turn_id}:{event.task.id}",
            ))
        elif kind == "task_blocked":
            _append(out, TranscriptItem(
                id=f"team-task-blocked-{len(out.transcript)}", kind="system",
                text=f"[{event.role or event.task.owner_role}/{event.task.id}] 阻塞：{event.message or event.task.result}",
                turn_id=turn_id, trace_id=f"{turn_id}:{event.task.id}",
                status="cancelled",
            ))
        else:
            next_status = "done" if kind == "task_done" else "failed"
            for group_id, group in list(out.agent_groups.items()):
                if group.task_id == event.task.id and group.role not in {"reviewer", "repairer"}:
                    out.agent_groups[group_id] = replace(group, status=next_status)
        return out
    if kind == "agent_started" and event.task:
        group, group_id = _ensure_agent_group(out, event)
        out.agent_groups[group_id] = replace(
            group,
            status="repairing" if event.role == "repairer" else "running",
            attempt=event.attempt or group.attempt,
            effective_steps=event.effective_steps or group.effective_steps,
            failure_category=event.failure_category or group.failure_category,
            latest_summary="Agent 已启动",
        )
        return out
    if kind == "subtask_event" and event.agent_event:
        group, group_id = _ensure_agent_group(out, event)
        trace = f"{turn_id}:{group_id}"
        return _reduce_agent_event_in_group(out, group_id, event.agent_event, turn_id, trace)
    if kind == "agent_done" and event.task:
        group, group_id = _ensure_agent_group(out, event)
        out.agent_groups[group_id] = replace(
            group,
            status="done",
            latest_summary=event.message[:240] or "Agent 已完成",
        )
        return out
    if kind == "agent_failed" and event.task:
        group, group_id = _ensure_agent_group(out, event)
        out.agent_groups[group_id] = replace(
            group,
            status="failed",
            attempt=event.attempt or group.attempt,
            effective_steps=event.effective_steps or group.effective_steps,
            failure_category=event.failure_category or group.failure_category,
            latest_error=event.message[:240] or "Agent 执行失败",
        )
        return out
    if kind == "artifact_produced" and event.artifact:
        group, group_id = _ensure_agent_group(out, event)
        summary = event.artifact.summary.replace("\n", " ")[:240]
        _append_group_entry(
            out,
            group_id,
            TranscriptItem(
                id=f"team-artifact-{group.event_count}", kind="context",
                text=f"Artifact {event.artifact.kind}: {summary}",
                turn_id=turn_id, trace_id=f"{turn_id}:{group_id}:artifact",
                collapsible=True, collapsed=True,
            ),
            summary=summary,
            artifact=True,
        )
        return out
    if kind == "task_review_started" and event.task:
        group, group_id = _ensure_agent_group(
            out, event, group_id=f"{event.team_id}:reviewer:{event.task.id}", role="reviewer"
        )
        out.agent_groups[group_id] = replace(
            group, status="reviewing", latest_summary="正在审查任务证据"
        )
        return out
    if kind == "task_review_done" and event.task and event.review:
        detail = "；".join(event.review.findings) or "验收通过"
        group, group_id = _ensure_agent_group(
            out, event, group_id=f"{event.team_id}:reviewer:{event.task.id}", role="reviewer"
        )
        _append_group_entry(
            out,
            group_id,
            TranscriptItem(
                id=f"team-review-result-{group.event_count}", kind="context",
                text=f"{event.review.verdict}: {detail[:320]}",
                turn_id=turn_id, trace_id=f"{turn_id}:{group_id}:review",
                collapsible=True, collapsed=True,
                status="success" if event.review.verdict == "pass" else "failed",
            ),
            summary=f"{event.review.verdict}: {detail}",
            error=detail if event.review.verdict != "pass" else "",
        )
        out.agent_groups[group_id] = replace(
            out.agent_groups[group_id],
            status=(
                "done" if event.review.verdict == "pass"
                else "needs_input" if event.review.verdict == "needs_input"
                else "failed"
            ),
        )
        return out
    if kind in {"review_output_invalid", "review_output_retry"} and event.task:
        group, group_id = _ensure_agent_group(
            out, event, group_id=f"{event.team_id}:reviewer:{event.task.id}", role="reviewer"
        )
        summary = event.message[:240] or "Reviewer 输出异常"
        out.agent_groups[group_id] = replace(
            group,
            status="reviewing",
            failure_category=event.failure_category or group.failure_category,
            latest_summary=summary,
            latest_error=summary if kind == "review_output_invalid" else "",
        )
        return out
    if kind in {"task_needs_input", "repair_scope_required"} and event.task:
        status = "needs_input"
        out.plan_tasks[event.task.id] = status
        tasks = [
            PlanTaskSnapshot(item.id, item.title, status) if item.id == event.task.id else item
            for item in out.inspector.plan.tasks
        ]
        out.inspector = replace(
            out.inspector,
            plan=replace(
                out.inspector.plan,
                tasks=tuple(tasks),
                completed_tasks=sum(item.status == "done" for item in tasks),
                failure_count=sum(item.status == "failed" for item in tasks),
            ),
        )
        out.phase = "awaiting_team_input"
        out.team_input_task_id = event.task.id
        out.team_input_category = event.failure_category
        out.team_input_message = event.message or event.task.result
        out.team_input_scope = list(event.scope_claims)
        out.notification = out.team_input_message
        out.notification_level = "warning"
        return out
    if kind == "repair_requested" and event.task:
        group, group_id = _ensure_agent_group(out, event, role="repairer")
        repair_attempt = 0
        match = re.search(r"-repair-(\d+)$", event.task.id)
        if match:
            repair_attempt = int(match.group(1))
        out.agent_groups[group_id] = replace(
            group,
            status="repairing",
            repair_attempt=repair_attempt,
            latest_summary=event.message[:240] or "等待 Repairer 启动",
        )
        return out
    if kind == "team_resume_requested":
        out.phase = "running"
        out.pending_plan = None
        if event.plan:
            out.inspector = replace(
                out.inspector,
                plan_status="running",
                plan=_plan_snapshot(event.plan, status="running", previous=out.inspector.plan),
                session=replace(out.inspector.session, status="Working"),
            )
        _append(out, TranscriptItem(
            id=f"team-resume-{len(out.transcript)}", kind="system",
            text=event.message, turn_id=turn_id,
        ))
        out.notification = event.message
        out.notification_level = "info"
        return out
    if kind in {"team_done", "cancelled"}:
        _collapse_turn(out, turn_id, status="cancelled" if kind == "cancelled" else "done")
        _finish_agent_groups(out, "cancelled" if kind == "cancelled" else "done")
        out.phase = "idle"
        out.pending_plan = None
        out.team_input_task_id = ""
        out.team_input_category = ""
        out.team_input_message = ""
        out.team_input_scope = []
        final_status = "done" if kind == "team_done" else "cancelled"
        out.inspector = replace(
            out.inspector,
            plan_status=final_status,
            plan=_plan_snapshot(event.plan, status=final_status, previous=out.inspector.plan),
            session=replace(out.inspector.session, status="Idle"),
        )
        out.notification = event.message
        out.notification_level = "info"
        return out
    if kind == "team_failed":
        _collapse_turn(out, turn_id, status="failed")
        _finish_agent_groups(out, "failed")
        out.phase = "error"
        out.pending_plan = None
        out.team_input_task_id = ""
        out.team_input_category = ""
        out.team_input_message = ""
        out.team_input_scope = []
        out.inspector = replace(
            out.inspector,
            plan_status="failed",
            plan=_plan_snapshot(event.plan, status="failed", previous=out.inspector.plan),
            session=replace(out.inspector.session, status="Error"),
        )
        out.notification = event.message
        out.notification_level = "error"
        _append(out, TranscriptItem(
            id=f"team-error-{len(out.transcript)}", kind="error", text=event.message, turn_id=turn_id,
        ))
        return out
    return out


def reduce_event(state: TuiState, event: AgentEvent | PlanEvent | TeamEvent, turn_id: str | None = None) -> TuiState:
    if isinstance(event, AgentEvent):
        return reduce_agent_event(state, event, turn_id)
    if isinstance(event, TeamEvent):
        return reduce_team_event(state, event, turn_id)
    return reduce_plan_event(state, event, turn_id)
