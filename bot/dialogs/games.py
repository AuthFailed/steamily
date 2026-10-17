from html import escape

from aiogram.enums import ContentType
from aiogram.types import CallbackQuery, Message
from aiogram_dialog import Data, Dialog, DialogManager, Window
from aiogram_dialog.widgets.input import MessageInput
from aiogram_dialog.widgets.kbd import (
    Button,
    Checkbox,
    FirstPage,
    Group,
    LastPage,
    Multiselect,
    NextPage,
    PrevPage,
    Radio,
    Row,
    ScrollingGroup,
    Select,
    SwitchTo,
)
from aiogram_dialog.widgets.link_preview import LinkPreview
from aiogram_dialog.widgets.text import Format

from ..family import Family, accounts_by_steam, person_link
from ..i18n import Tr, sharing_warning, t
from ..library import PRICES, SORTS, query, shareable_copies, tag_counts
from ..states import Games
from ..ui import BLUE, Header, back_home, home, look

PAGE_SIZE = 6
SCROLL = "games_scroll"
BLANK = "\u2800"  # Telegram needs some button text; this shows an empty button at the list edges


async def on_start(start_data: Data, manager: DialogManager):
    manager.dialog_data.update(sort="name", desc=False)
    if isinstance(start_data, dict) and start_data.get("owners"):  # "games we both have" from a member card
        manager.dialog_data["owners"] = start_data["owners"]
    await manager.find("price").set_checked("any")
    await manager.find("players").set_checked(manager.middleware_data["account"]["steam_id"], True)


def viewer_store(manager: DialogManager) -> dict:
    """Store data in the currency of the Steam account looking at the list."""
    return manager.middleware_data["family"].store(manager.middleware_data["account"]["country"])


async def reset_page(event, widget, manager: DialogManager, *args):
    await manager.find(SCROLL).set_page(0)


def current_filters(manager: DialogManager) -> dict:
    return {
        "search": manager.dialog_data.get("search", ""),
        "tag": manager.dialog_data.get("tag"),
        "sort": manager.dialog_data.get("sort", "name"),
        "desc": manager.dialog_data.get("desc", False),
        "price": manager.find("price").get_checked() or "any",
        "multiplayer": manager.find("multiplayer").is_checked(),
        "players": manager.find("players").get_checked(),
        "owners": manager.dialog_data.get("owners", []),
    }


def filter_summary(manager: DialogManager, family: Family, store: dict, lang: str) -> str:
    filters = current_filters(manager)
    lines = []
    if filters["search"]:
        lines.append(f"🔎 “{escape(filters['search'])}”")
    if filters["tag"]:
        lines.append(f"🏷 {escape(store.get('tag_names', {}).get(filters['tag'], '?'))}")
    if filters["price"] != "any":
        lines.append(f"💲 {t(lang, 'price_' + filters['price'])}")
    if filters["multiplayer"]:
        lines.append(t(lang, "sum_multiplayer"))
    if len(filters["players"]) > 1:
        names = ", ".join(escape(family.name(p)) for p in filters["players"])
        lines.append(t(lang, "sum_players", names=names, n=len(filters["players"])))
    if filters["owners"]:
        lines.append(t(lang, "sum_owners", names=", ".join(escape(family.name(o)) for o in filters["owners"])))
    lines.append(f"↕️ {t(lang, 'sort_' + filters['sort'])} {'↓' if filters['desc'] else '↑'}")
    return "\n".join(lines)


async def list_getter(dialog_manager: DialogManager, family: Family, lang: str, **kwargs):
    store = viewer_store(dialog_manager)
    details = store.get("apps", {})
    games = []
    for game in query(family.apps, store, **current_filters(dialog_manager)):
        copies = len(game["owners"])
        price = details.get(str(game["appid"]), {}).get("price_text")
        label = game["name"] + (f" ×{copies}" if copies > 1 else "") + (f" · {price}" if price else "")
        games.append({"appid": game["appid"], "label": label})
    pages = max(1, -(-len(games) // PAGE_SIZE))
    page = min(await dialog_manager.find(SCROLL).get_page(), pages - 1) + 1
    return {
        "games": games,
        "count": len(games),
        "page": page,
        "pages": pages,
        "first": "«" if page > 1 else BLANK,
        "prev": "‹" if page > 1 else BLANK,
        "next": "›" if page < pages else BLANK,
        "last": "»" if page < pages else BLANK,
        "paged": pages > 1,
        "summary": filter_summary(dialog_manager, family, store, lang),
        "has_search": bool(dialog_manager.dialog_data.get("search")),
    }


async def on_search(message: Message, widget, manager: DialogManager):
    manager.dialog_data["search"] = message.text.strip()[:100]
    await reset_page(message, widget, manager)


async def clear_search(callback: CallbackQuery, button: Button, manager: DialogManager):
    manager.dialog_data.pop("search", None)
    await reset_page(callback, button, manager)


async def pages_getter(dialog_manager: DialogManager, family: Family, **kwargs):
    count = len(query(family.apps, viewer_store(dialog_manager), **current_filters(dialog_manager)))
    return {"page_numbers": list(range(1, max(1, -(-count // PAGE_SIZE)) + 1))}


async def on_page(callback: CallbackQuery, widget, manager: DialogManager, page: int):
    await manager.find(SCROLL).set_page(page - 1)
    await manager.switch_to(Games.list)


async def on_game(callback: CallbackQuery, widget, manager: DialogManager, appid: int):
    manager.dialog_data["appid"] = appid
    await manager.switch_to(Games.game)


async def game_getter(dialog_manager: DialogManager, family: Family, db, lang: str, **kwargs):
    appid = dialog_manager.dialog_data["appid"]
    game = next((a for a in family.apps if a["appid"] == appid), None)
    if game is None:
        return {"game": "—", "details": t(lang, "game_gone")}
    store = viewer_store(dialog_manager)
    info = store.get("apps", {}).get(str(appid), {})
    tag_names = store.get("tag_names", {})
    tags = ", ".join(tag_names[str(tag)] for tag in info.get("tags", []) if str(tag) in tag_names)
    accounts = await accounts_by_steam(db, family.family_id)
    owners = "".join(f"\n• {person_link(family, o, accounts)}" for o in game["owners"])
    shareable = shareable_copies(game)
    access = (
        "owned"
        if dialog_manager.middleware_data["account"]["steam_id"] in game["owners"]
        else ("borrowed" if shareable else "unavailable")
    )
    lines = [
        f"💲 {escape(info.get('price_text') or t(lang, 'price_unknown'))}",
        f"🏷 {escape(tags)}" if tags else None,
        t(lang, info.get("play_mode") or "mode_unknown"),
        t(lang, "copies_line", n=len(game["owners"]), owners=owners),
        t(lang, "shareable_copies_line", n=shareable),
        t(lang, "access_" + access),
        sharing_warning(lang, game.get("exclude_reason", 0)),
        t(lang, "availability_unknown"),
        f"\nhttps://store.steampowered.com/app/{appid}/",
    ]
    return {
        "game": escape(game["name"]),
        "details": "\n".join(line for line in lines if line),
        "store_url": f"https://store.steampowered.com/app/{appid}/",
    }


async def genre_getter(dialog_manager: DialogManager, family: Family, **kwargs):
    store = viewer_store(dialog_manager)
    names = store.get("tag_names", {})
    return {"tags": [(tag, f"{names[tag]} ({count})") for tag, count in tag_counts(family.apps, store)]}


async def on_genre(callback: CallbackQuery, widget, manager: DialogManager, tag: str):
    manager.dialog_data["tag"] = tag
    await reset_page(callback, widget, manager)
    await manager.switch_to(Games.filters)


async def any_genre(callback: CallbackQuery, button: Button, manager: DialogManager):
    manager.dialog_data.pop("tag", None)
    await reset_page(callback, button, manager)
    await manager.switch_to(Games.filters)


async def filters_getter(dialog_manager: DialogManager, lang: str, **kwargs):
    tag = dialog_manager.dialog_data.get("tag")
    names = viewer_store(dialog_manager).get("tag_names", {})
    return {
        "prices": [(key, t(lang, "price_" + key)) for key in PRICES],
        "genre": names.get(tag, "?") if tag else t(lang, "genre_any"),
        "player_count": len(dialog_manager.find("players").get_checked()),
    }


async def sort_getter(dialog_manager: DialogManager, lang: str, **kwargs):
    active, desc = dialog_manager.dialog_data.get("sort", "name"), dialog_manager.dialog_data.get("desc", False)
    return {
        "sorts": [(key, t(lang, "sort_" + key) + ((" ↓" if desc else " ↑") if key == active else "")) for key in SORTS]
    }


async def on_sort(callback: CallbackQuery, widget, manager: DialogManager, key: str):
    """A new field starts in its natural direction; tapping the active field flips it."""
    if manager.dialog_data.get("sort") == key:
        manager.dialog_data["desc"] = not manager.dialog_data.get("desc", False)
    else:
        manager.dialog_data.update(sort=key, desc=SORTS[key])
    await reset_page(callback, widget, manager)


async def reset_filters(callback: CallbackQuery, button: Button, manager: DialogManager):
    manager.dialog_data.pop("tag", None)
    manager.dialog_data.pop("owners", None)
    await manager.find("multiplayer").set_checked(False)
    await manager.find("players").reset_checked()
    await on_start(None, manager)
    await reset_page(callback, button, manager)


async def players_getter(family: Family, **kwargs):
    return {"members": sorted(((m, family.name(m)) for m in family.members), key=lambda m: m[1].lower())}


async def on_players(event, widget, manager: DialogManager, *args):
    # Playing together means a multiplayer game.
    if len(manager.find("players").get_checked()) > 1:
        await manager.find("multiplayer").set_checked(True)
    await reset_page(event, widget, manager)


def back_to_list():
    return back_home(SwitchTo(Tr("btn_back"), id="back", state=Games.list))


def back_to_filters():
    return back_home(SwitchTo(Tr("btn_back"), id="back_filters", state=Games.filters))


games_dialog = Dialog(
    Window(
        Header("library", "library"),
        Tr("library_stats"),
        ScrollingGroup(
            Select(
                Format("{item[label]}"),
                id="game",
                item_id_getter=lambda g: g["appid"],
                items="games",
                type_factory=int,
                on_click=on_game,
            ),
            id=SCROLL,
            width=1,
            height=PAGE_SIZE,
            hide_pager=True,
        ),
        Row(
            FirstPage(scroll=SCROLL, id="first", text=Format("{data[first]}")),
            PrevPage(scroll=SCROLL, id="prev", text=Format("{data[prev]}")),
            SwitchTo(Format("{page} / {pages}"), id="pick_page", state=Games.pages),
            NextPage(scroll=SCROLL, id="next", text=Format("{data[next]}")),
            LastPage(scroll=SCROLL, id="last", text=Format("{data[last]}")),
            when="paged",
        ),
        Row(
            SwitchTo(Tr("btn_filters"), id="filters", state=Games.filters, style=look("filters", BLUE)),
            SwitchTo(Tr("btn_sort"), id="sorting", state=Games.sort, style=look("sort", BLUE)),
        ),
        Button(Tr("btn_clear_search"), id="clear", on_click=clear_search, when="has_search"),
        home(),
        MessageInput(on_search, content_types=[ContentType.TEXT]),
        getter=list_getter,
        state=Games.list,
    ),
    Window(
        Header("library", "pages"),
        ScrollingGroup(
            Select(
                Format("{item}"),
                id="page",
                item_id_getter=lambda page: page,
                items="page_numbers",
                type_factory=int,
                on_click=on_page,
            ),
            id="pages_scroll",
            width=5,
            height=6,
            hide_on_single_page=True,
        ),
        back_to_list(),
        getter=pages_getter,
        state=Games.pages,
    ),
    Window(
        Header("filters", "filters"),
        Group(
            Radio(
                Format("• {item[1]}"),
                Format("{item[1]}"),
                id="price",
                items="prices",
                item_id_getter=lambda p: p[0],
                on_state_changed=reset_page,
            ),
            width=5,
        ),
        Checkbox(Tr("multiplayer_on"), Tr("multiplayer_off"), id="multiplayer", on_state_changed=reset_page),
        SwitchTo(Tr("btn_genre"), id="genre", state=Games.genre),
        SwitchTo(Tr("btn_players"), id="players_btn", state=Games.players),
        Button(Tr("btn_reset_filters"), id="reset", on_click=reset_filters),
        back_to_list(),
        getter=filters_getter,
        state=Games.filters,
    ),
    Window(
        Header("sort", "sort"),
        Group(
            Select(Format("{item[1]}"), id="sort", item_id_getter=lambda s: s[0], items="sorts", on_click=on_sort),
            width=2,
        ),
        back_to_list(),
        getter=sort_getter,
        state=Games.sort,
    ),
    Window(
        Header("filters", "genre"),
        ScrollingGroup(
            Select(Format("{item[1]}"), id="tag", item_id_getter=lambda tag: tag[0], items="tags", on_click=on_genre),
            id="tags_scroll",
            width=2,
            height=PAGE_SIZE,
            hide_on_single_page=True,
        ),
        Button(Tr("btn_any_genre"), id="any_genre", on_click=any_genre),
        back_to_filters(),
        getter=genre_getter,
        state=Games.genre,
    ),
    Window(
        Header("family", "players"),
        Group(
            Multiselect(
                Format("✅ {item[1]}"),
                Format("⬜ {item[1]}"),
                id="players",
                items="members",
                item_id_getter=lambda m: m[0],
                on_state_changed=on_players,
            ),
            width=2,
        ),
        back_to_filters(),
        getter=players_getter,
        state=Games.players,
    ),
    Window(
        Header("library", "game"),
        # The card also links owner profiles; the preview should still show the game.
        LinkPreview(url=Format("{store_url}"), prefer_large_media=True, when="store_url"),
        back_to_list(),
        getter=game_getter,
        state=Games.game,
    ),
    on_start=on_start,
)
