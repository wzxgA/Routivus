from textual.widgets import Static
from xg.tui.i18n import UiLanguage, normalize_language, translate


class FooterBar(Static):
    def __init__(self) -> None:
        super().__init__(translate("en", "ui.footer"), id="footer")

    def update_language(self, language: UiLanguage) -> None:
        self.update(translate(normalize_language(language), "ui.footer"))
