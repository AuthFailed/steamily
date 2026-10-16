"""Looks shared by all screens: premium emoji, button colors, screen headers and the Home button."""

from aiogram_dialog import DialogManager, StartMode
from aiogram_dialog.widgets.kbd import Keyboard, Row, Start
from aiogram_dialog.widgets.style import Style
from aiogram_dialog.widgets.text import Text

from .i18n import TEXTS, Tr, language_of
from .states import Menu

GREEN, BLUE, RED = "success", "primary", "danger"

# Telegram custom emoji: (id, plain emoji shown where premium emoji can't be).
EMOJI = {
    "library": ("5337037846475720165", "🎮"),
    "filters": ("5188217332748527444", "⚙️"),
    "sort": ("5431736674147114227", "↕️"),
    "login": ("5375129357373165375", "🔑"),
    "family": ("5386539642369614675", "👥"),
    "admin": ("5467406098367521267", "🛠"),
    "language": ("5397575638146110953", "🌐"),
    "logout": ("5472055112702629499", "🚪"),
    "home": ("5465226866321268133", "🏠"),
    "cancel": ("5472354553527541051", "❌"),
    "retry": ("5264727218734524899", "🔄"),
    "notifications": ("5242628160297641831", "🔔"),
    "approve": ("5427009714745517609", "✅"),
    "block": ("5341673585066925528", "⛔"),
    "remove": ("5235929467309796721", "🗑"),
    "settings": ("5341715473882955310", "⚙️"),
}


def look(icon: str | None = None, color: str | None = None) -> Style:
    return Style(color, EMOJI[icon][0] if icon else None)


def premium(icon: str) -> str:
    emoji_id, plain = EMOJI[icon]
    return f'<tg-emoji emoji-id="{emoji_id}">{plain}</tg-emoji>'


class Header(Text):
    """Screen title with its premium emoji, then the screen description: texts `<key>_title` and `<key>_desc`."""

    def __init__(self, icon: str, key: str, when=None):
        super().__init__(when=when)
        self.icon, self.key = icon, key

    async def _render_text(self, data: dict, manager: DialogManager) -> str:
        texts = TEXTS[language_of(manager)]
        title = texts[f"{self.key}_title"].format_map(data)
        return f"{premium(self.icon)} <b>{title}</b>\n\n{texts[f'{self.key}_desc'].format_map(data)}\n"


def home() -> Start:
    return Start(Tr("btn_home"), id="home", state=Menu.main, mode=StartMode.RESET_STACK, style=look("home"))


def back_home(back: Keyboard) -> Row:
    """Back plus Home, for screens where Back doesn't lead to the main menu."""
    return Row(back, home())
