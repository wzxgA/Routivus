"""Small Textual messages used to bridge controller callbacks to widgets."""

from __future__ import annotations

from textual.message import Message

from routivus.tui.state import InspectorView, TuiState


class StateChanged(Message):
    def __init__(self, state: TuiState) -> None:
        super().__init__()
        self.state = state


class TraceCardToggled(Message):
    """A user clicked or activated one collapsible execution card."""

    def __init__(self, item_id: str) -> None:
        super().__init__()
        self.item_id = item_id


class AgentGroupToggled(Message):
    """A user expanded or collapsed one Team Agent conversation group."""

    def __init__(self, group_id: str) -> None:
        super().__init__()
        self.group_id = group_id


class CommandSuggestionSelected(Message):
    """A local command suggestion was selected without being submitted."""

    def __init__(self, candidate) -> None:
        super().__init__()
        self.candidate = candidate


class InspectorViewSelected(Message):
    """A user selected one of the Inspector's stable view tabs."""

    def __init__(self, view: InspectorView) -> None:
        super().__init__()
        self.view = view


class AskOptionSelected(Message):
    """A user selected an Ask-User option without submitting it yet."""

    def __init__(self, request_id: str, option_index: int) -> None:
        super().__init__()
        self.request_id = request_id
        self.option_index = option_index
