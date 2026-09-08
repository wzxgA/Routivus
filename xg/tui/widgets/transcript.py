"""Stable, incrementally reconciled Transcript widgets for the fullscreen TUI."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Static

from xg.tui.renderables import render_item
from xg.tui.state import TuiState
from xg.tui.widgets.action_card import InlineApprovalCard, InlineConfirmationCard
from xg.tui.widgets.agent_group_card import AgentGroupCard
from xg.tui.widgets.collapsible_card import CollapsibleCard
from xg.tui.i18n import UiLanguage, normalize_language

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _DesiredWidget:
    key: str
    kind: str
    value: Any


class TranscriptView(VerticalScroll):
    """Render Transcript state without rebuilding the whole DOM on every event.

    Textual schedules ``remove`` and ``mount`` operations asynchronously. The
    view therefore keeps current widgets by stable business keys and runs one
    reconciliation loop at a time. This keeps mouse hit-testing attached to
    the same widgets while streaming content changes.
    """

    CONTENT_DEBOUNCE_SECONDS = 0.04

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._latest_state: TuiState | None = None
        self._widgets_by_key: dict[str, Widget] = {}
        self._widget_kinds: dict[str, str] = {}
        self._rendered_keys: tuple[str, ...] = ()
        self._reconcile_task: asyncio.Task | None = None
        self._request_generation = 0
        self._rendered_generation = -1
        self._content_refresh_pending = False
        self._content_refresh_immediate = False
        self._layout_refresh_pending = False
        self._attach_refresh_pending = False
        self._unmounted = False
        self._approval_mode = ""
        self._approval_modified_args: dict | None = None
        self.language: UiLanguage = "zh"

    def update_progress(self, item_id: str, item) -> bool:
        """Update a local progress card without scheduling a full refresh."""
        widget = self._widgets_by_key.get(f"item:{item_id}")
        if widget is None or widget.parent is not self:
            return False
        if isinstance(widget, CollapsibleCard):
            widget.update_item(item)
        elif isinstance(widget, Static):
            widget.update(render_item(item, self.language))
        else:
            return False
        return True

    def update_state(self, state: TuiState) -> None:
        """Backward-compatible alias for callers using the old method name."""
        self.request_state(state)

    def request_state(self, state: TuiState) -> None:
        """Queue the newest state; DOM work is performed by one async worker."""
        if self._unmounted:
            return
        self._latest_state = state
        self.language = normalize_language(state.ui_language)
        self._request_generation += 1
        self._content_refresh_pending = True
        desired_keys = tuple(item.key for item in self._desired_widgets(state))
        self._content_refresh_immediate |= desired_keys != self._rendered_keys
        self._schedule_reconcile()

    def request_layout_refresh(self) -> None:
        """Request layout-only work without rebuilding or changing content."""
        if self._unmounted:
            return
        self._layout_refresh_pending = True
        self._schedule_reconcile()

    def set_approval_mode(self, mode: str = "", modified_args: dict | None = None) -> None:
        """Apply approval editing state to the stable card, including a pending mount."""
        self._approval_mode = mode
        self._approval_modified_args = modified_args
        for widget in self._widgets_by_key.values():
            if isinstance(widget, InlineApprovalCard) and widget.parent is self:
                widget.set_mode(mode, modified_args)

    def _schedule_reconcile(self) -> None:
        if self._unmounted:
            return
        if not self.is_attached:
            if not self._attach_refresh_pending:
                self._attach_refresh_pending = True
                self.call_after_refresh(self._schedule_after_attach)
            return
        if self._reconcile_task is None or self._reconcile_task.done():
            self._reconcile_task = asyncio.create_task(self._reconcile_loop())

    def _schedule_after_attach(self) -> None:
        self._attach_refresh_pending = False
        self._schedule_reconcile()

    async def _reconcile_loop(self) -> None:
        """Serialize DOM operations and coalesce high-frequency state events."""
        try:
            while not self._unmounted:
                if not self._content_refresh_pending and not self._layout_refresh_pending:
                    break

                if self._content_refresh_pending:
                    immediate = self._content_refresh_immediate
                    self._content_refresh_pending = False
                    self._content_refresh_immediate = False
                    if not immediate:
                        await asyncio.sleep(self.CONTENT_DEBOUNCE_SECONDS)

                if self._unmounted or not self.is_attached:
                    break

                state = self._latest_state
                generation = self._request_generation
                if state is not None and generation != self._rendered_generation:
                    await self._reconcile_state(state)
                    self._rendered_generation = generation

                if self._layout_refresh_pending:
                    self._layout_refresh_pending = False
                    self._refresh_layout()

                if generation != self._request_generation:
                    self._content_refresh_pending = True
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Transcript reconciliation failed")
        finally:
            self._reconcile_task = None
            if (
                not self._unmounted
                and self.is_attached
                and (self._content_refresh_pending or self._layout_refresh_pending)
            ):
                self._schedule_reconcile()

    def _desired_widgets(self, state: TuiState) -> list[_DesiredWidget]:
        desired: list[_DesiredWidget] = []
        if not state.transcript:
            desired.append(_DesiredWidget("empty", "empty", None))
        else:
            for item in state.transcript:
                if item.kind == "agent_group" and item.agent_group_id:
                    group = state.agent_groups.get(item.agent_group_id)
                    if group is not None:
                        desired.append(_DesiredWidget(
                            f"agent_group:{item.agent_group_id}", "agent_group", group,
                        ))
                    continue
                kind = "trace" if item.collapsible and item.kind in {
                    "thinking", "tool_call", "tool_result", "approval",
                } else "text"
                desired.append(_DesiredWidget(f"item:{item.id}", kind, item))

        if state.pending_approval is not None:
            request = state.pending_approval
            key = f"approval:{request.turn_id}:{request.tool_name}"
            desired.append(_DesiredWidget(key, "approval", request))
        elif state.pending_confirmation is not None:
            request = state.pending_confirmation
            key = f"confirmation:{request.kind}:{request.title}"
            desired.append(_DesiredWidget(key, "confirmation", request))
        return desired

    async def _reconcile_state(self, state: TuiState) -> None:
        if self._unmounted or not self.is_attached:
            return

        was_at_bottom = self._is_at_bottom()
        previous_scroll_y = self.scroll_y
        focused_key, focused_widget = self._focused_target()
        desired = self._desired_widgets(state)
        desired_keys = tuple(record.key for record in desired)
        desired_by_key = {record.key: record for record in desired}

        # Update stable widgets first. A type change is the only case that
        # replaces a widget, and it is limited to that one stable key.
        for record in desired:
            widget = self._widgets_by_key.get(record.key)
            if widget is not None and (
                self._widget_kinds.get(record.key) != record.kind
                or widget.parent is not self
            ):
                await self._remove_widget(record.key, widget)
                widget = None
            if widget is None:
                widget = self._create_widget(record)
                self._widgets_by_key[record.key] = widget
                self._widget_kinds[record.key] = record.kind
            else:
                self._update_widget(widget, record)

        # Remove only keys no longer present. In particular, don't use
        # remove_children(), which creates a stale-widget hit-testing window.
        for key in tuple(self._widgets_by_key):
            if key not in desired_by_key:
                await self._remove_widget(key, self._widgets_by_key[key])

        if self._unmounted or not self.is_attached:
            return

        # Mount new widgets in desired order. Existing widgets are used as
        # anchors, so each mount is deterministic and avoids a full rebuild.
        for index, record in enumerate(desired):
            widget = self._widgets_by_key.get(record.key)
            if widget is None or widget.parent is self:
                continue
            before = next(
                (
                    self._widgets_by_key[key]
                    for key in desired_keys[index + 1:]
                    if key in self._widgets_by_key
                    and self._widgets_by_key[key].parent is self
                ),
                None,
            )
            if before is None:
                await self.mount(widget)
            else:
                await self.mount(widget, before=before)

        # Reorder existing children without removing them. This normally is a
        # no-op because mount anchors preserve order, but handles insertions.
        self._reorder_children(desired_keys)
        self._rendered_keys = desired_keys
        self._restore_view(was_at_bottom, previous_scroll_y, focused_key, focused_widget)

    def _create_widget(self, record: _DesiredWidget) -> Widget:
        if record.kind == "empty":
            return Static(
                "Enter a task to start chatting with Agent\n\nTry: /help   /plan <task>   /model"
                if self.language == "en" else
                "输入任务开始与 Agent 对话\n\n试试：/help   /plan <任务>   /model",
                classes="transcript-empty-state",
            )
        if record.kind == "agent_group":
            return AgentGroupCard(record.value, self.language)
        if record.kind == "trace":
            return CollapsibleCard(record.value, self.language)
        if record.kind == "approval":
            widget = InlineApprovalCard(record.value, self.language)
            widget.set_mode(self._approval_mode, self._approval_modified_args)
            return widget
        if record.kind == "confirmation":
            return InlineConfirmationCard(record.value, self.language)
        widget = Static(render_item(record.value, self.language))
        widget.transcript_item_id = record.value.id
        return widget

    def _update_widget(self, widget: Widget, record: _DesiredWidget) -> None:
        if record.kind == "agent_group" and isinstance(widget, AgentGroupCard):
            widget.update_group(record.value)
            widget.set_language(self.language)
        elif record.kind == "trace" and isinstance(widget, CollapsibleCard):
            widget.update_item(record.value)
            widget.set_language(self.language)
        elif record.kind == "approval" and isinstance(widget, InlineApprovalCard):
            widget.update_request(record.value)
            widget.set_language(self.language)
            widget.set_mode(self._approval_mode, self._approval_modified_args)
        elif record.kind == "confirmation" and isinstance(widget, InlineConfirmationCard):
            widget.update_request(record.value)
            widget.set_language(self.language)
        elif record.kind == "text" and isinstance(widget, Static):
            widget.update(render_item(record.value, self.language))

    async def _remove_widget(self, key: str, widget: Widget) -> None:
        if widget.parent is self:
            await widget.remove()
        self._widgets_by_key.pop(key, None)
        self._widget_kinds.pop(key, None)

    def _reorder_children(self, desired_keys: tuple[str, ...]) -> None:
        for index, key in enumerate(desired_keys):
            widget = self._widgets_by_key.get(key)
            children = list(self.children)
            if widget is None or widget not in children or index >= len(children):
                continue
            if children[index] is not widget:
                self.move_child(widget, before=children[index])

    def _focused_target(self) -> tuple[str | None, Widget | None]:
        focused = getattr(getattr(self, "app", None), "focused", None)
        if focused is None:
            return None, None
        for key, widget in self._widgets_by_key.items():
            if focused is widget:
                return key, widget
        return None, None

    def _is_at_bottom(self) -> bool:
        return self.scroll_y >= max(0, self.max_scroll_y - 1)

    def _restore_view(
        self,
        was_at_bottom: bool,
        scroll_y: int,
        focused_key: str | None,
        focused_widget: Widget | None,
    ) -> None:
        def restore() -> None:
            if self._unmounted or not self.is_attached:
                return
            if was_at_bottom:
                self.scroll_end(animate=False)
            else:
                self.scroll_to(y=max(0, scroll_y), animate=False, immediate=True)
            current_focus = getattr(getattr(self, "app", None), "focused", None)
            if focused_key and (current_focus is None or current_focus is focused_widget):
                widget = self._widgets_by_key.get(focused_key)
                if widget is not None and widget.parent is self:
                    widget.focus()

        self.call_after_refresh(restore)

    def _refresh_layout(self) -> None:
        if self._unmounted or not self.is_attached:
            return
        self.refresh(layout=True)
        for widget in tuple(self._widgets_by_key.values()):
            if widget.parent is self:
                widget.refresh(layout=True)

    async def on_unmount(self) -> None:
        self._unmounted = True
        self._latest_state = None
        self._content_refresh_pending = False
        self._content_refresh_immediate = False
        self._layout_refresh_pending = False
        task = self._reconcile_task
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._reconcile_task = None
        self._widgets_by_key.clear()
        self._widget_kinds.clear()
