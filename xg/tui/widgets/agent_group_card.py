"""Collapsible conversation group for one Team AgentRun."""

from __future__ import annotations

from textual.events import Click
from textual.widgets import Static

from xg.tui.messages import AgentGroupToggled
from xg.tui.renderables import agent_group_renderable
from xg.tui.state import AgentGroupState
from xg.tui.i18n import UiLanguage, normalize_language


class AgentGroupCard(Static):
    """One Agent conversation inside the shared TranscriptView."""

    can_focus = True
    BINDINGS = [
        ("enter", "toggle", "展开/折叠 Agent"),
        ("space", "toggle", "展开/折叠 Agent"),
    ]

    def __init__(self, group: AgentGroupState, language: UiLanguage = "zh") -> None:
        self.group_id = group.group_id
        self.group = group
        self.language = normalize_language(language)
        super().__init__(agent_group_renderable(group, self.language))

    def update_group(self, group: AgentGroupState) -> None:
        self.group = group
        self.update(agent_group_renderable(group, self.language))

    def set_language(self, language: UiLanguage) -> None:
        self.language = normalize_language(language)
        self.update(agent_group_renderable(self.group, self.language))

    def _toggle(self) -> None:
        self.post_message(AgentGroupToggled(self.group_id))

    def on_click(self, event: Click) -> None:
        event.stop()
        self._toggle()

    def action_toggle(self) -> None:
        self._toggle()
