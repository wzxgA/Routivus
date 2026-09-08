from __future__ import annotations

from textual.widgets import Static

from xg.tui.state import TuiState
from xg.tui.i18n import normalize_language, translate


class QueueStatus(Static):
    """Compact preview of submissions waiting behind the active turn."""

    MAX_PREVIEW_ITEMS = 4
    PREVIEW_LENGTH = 72

    def update_state(self, state: TuiState) -> None:
        if not state.queue:
            self.update("")
            self.display = False
            return

        language = normalize_language(state.ui_language)
        lines = [translate(language, "ui.queue.title", count=len(state.queue))]
        for item in state.queue[: self.MAX_PREVIEW_ITEMS]:
            preview = " ".join(item.text.split())
            if len(preview) > self.PREVIEW_LENGTH:
                preview = preview[: self.PREVIEW_LENGTH - 1] + "…"
            lines.append(f"  #{item.id.removeprefix('queue-')} {preview}")
        remaining = len(state.queue) - self.MAX_PREVIEW_ITEMS
        if remaining > 0:
            lines.append(translate(language, "ui.queue.remaining", count=remaining))
        self.update("\n".join(lines))
        self.display = True
