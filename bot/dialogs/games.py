from html import escape

import aiohttp
from aiogram.enums import ContentType
from aiogram.types import CallbackQuery, Message
from aiogram_dialog import Data, Dialog, DialogManager, Window
from aiogram_dialog.widgets.input import MessageInput, TextInput
from aiogram_dialog.widgets.kbd import (
    Button,
    Checkbox,
    FirstPage,
    Group,
    LastPage,
    Multiselect,
    NextPage,
    PrevPage,
    Row,
    ScrollingGroup,
    Select,
    SwitchTo,
)
from aiogram_dialog.widgets.link_preview import LinkPreview
from aiogram_dialog.widgets.text import Format

from ..family import DEFAULT_COUNTRY, Family, accounts_by_steam, person_link
from ..i18n import Tr, sharing_warning, t
from ..library import (
    PLAY_MODES,
    SORTS,
    copy_plan,
    currency,
    parse_budget,
    play_modes,
    query,
    shareable_copies,
    tag_counts,
)
from ..states import Games
from ..ui import BLUE, Header, back_home, home, look

PAGE_SIZE = 6
SCROLL = "games_scroll"
BLANK = "\u2800"  # Telegram needs some button text; this shows an empty button at the list edges
RESPONSES = {"r": "recommend", "n": "recommend", "t": "together", "i": "not_interested", "c": None}
STATUSES = ("backlog", "playing", "completed")


async def on_start(start_data: Data, manager: DialogManager):
    manager.dialog_data.update(sort="name", desc=False, price="any", budget_changed=False)
    if isinstance(start_data, dict) and start_data.get("owners"):  # "games we both have" from a member card
        manager.dialog_data["owners"] = start_data["owners"]
    await manager.find("players").set_checked(manager.middleware_data["account"]["steam_id"], True)


def viewer_store(manager: DialogManager) -> dict:
    """Store data in the currency of the Steam account looking at the list."""
    country = manager.middleware_data["account"]["country"] or DEFAULT_COUNTRY
    return manager.middleware_data["family"].stores.get(country, {})


async def reset_page(event, widget, manager: DialogManager, *args):
    await manager.find(SCROLL).set_page(0)


def current_price(manager: DialogManager) -> str | int:
    price = manager.dialog_data.get("price", "any")
    country = manager.middleware_data["account"]["country"] or DEFAULT_COUNTRY
    if isinstance(price, int) and manager.dialog_data.get("price_country") != country:
        manager.dialog_data.update(price="any", budget_changed=True)
        return "any"
    return price


def current_filters(manager: DialogManager) -> dict:
    return {
        "search": manager.dialog_data.get("search", ""),
        "tag": manager.dialog_data.get("tag"),
        "sort": manager.dialog_data.get("sort", "name"),
        "desc": manager.dialog_data.get("desc", False),
        "price": current_price(manager),
        "multiplayer": manager.find("multiplayer").is_checked(),
        "mode": manager.dialog_data.get("mode"),
        "players": manager.find("players").get_checked(),
        "owners": manager.dialog_data.get("owners", []),
    }


def price_label(manager: DialogManager, store: dict, lang: str) -> str:
    price = current_price(manager)
    if isinstance(price, int):
        return t(
            lang,
            "price_max",
            amount=f"{price // 100}.{price % 100:02}",
            currency=escape(manager.dialog_data.get("price_currency") or currency(store)),
        )
    return t(lang, "price_" + price)


def filter_summary(manager: DialogManager, family: Family, store: dict, lang: str) -> str:
    filters = current_filters(manager)
    lines = []
    if filters["search"]:
        lines.append(f"🔎 “{escape(filters['search'])}”")
    if filters["tag"]:
        lines.append(f"🏷 {escape(store.get('tag_names', {}).get(filters['tag'], '?'))}")
    if filters["price"] != "any":
        lines.append(f"💲 {price_label(manager, store, lang)}")
    if filters["multiplayer"]:
        lines.append(t(lang, "sum_multiplayer"))
    if manager.find("recommended").is_checked():
        lines.append(t(lang, "recommended_filter"))
    if filters["mode"]:
        lines.append(t(lang, "mode_" + filters["mode"]))
    if len(filters["players"]) > 1:
        names = ", ".join(escape(family.name(p)) for p in filters["players"])
        key = "sum_players_hosted" if filters["mode"] in ("local_coop", "remote_play") else "sum_players"
        lines.append(t(lang, key, names=names, n=len(filters["players"])))
    if filters["owners"]:
        lines.append(t(lang, "sum_owners", names=", ".join(escape(family.name(o)) for o in filters["owners"])))
    if status := manager.dialog_data.get("status"):
        lines.append(t(lang, "personal_status", status=t(lang, "status_" + status)))
    lines.append(f"↕️ {t(lang, 'sort_' + filters['sort'])} {'↓' if filters['desc'] else '↑'}")
    return "\n".join(lines)


async def response_rows(manager: DialogManager, family: Family, db):
    rows = await db.game_responses(manager.middleware_data["account"])
    return [row for row in rows if row["steam_id"] in family.members]


async def listed_games(manager: DialogManager, family: Family, marks: dict):
    games = filtered_games(manager, family, marks)
    if manager.find("recommended").is_checked():
        rows = await response_rows(manager, family, manager.middleware_data["db"])
        recommended = {row["appid"] for row in rows if row["response"] == "recommend"}
        games = [game for game in games if game["appid"] in recommended]
    return games


def filtered_games(manager: DialogManager, family: Family, marks: dict) -> list[dict]:
    status = manager.dialog_data.get("status")
    apps = family.apps
    if status:
        apps = [app for app in apps if marks.get(app["appid"], {}).get("status") == status]
    return query(apps, viewer_store(manager), **current_filters(manager))


def missing_games(manager: DialogManager, family: Family, marks: dict) -> list[dict]:
    appids = {app["appid"] for app in family.apps}
    status = manager.dialog_data.get("status")
    return sorted(
        [
            {"appid": appid, **mark}
            for appid, mark in marks.items()
            if appid not in appids and (not status or mark["status"] == status)
        ],
        key=lambda game: game["name"].lower(),
    )


async def list_getter(dialog_manager: DialogManager, family: Family, db, lang: str, **kwargs):
    store = viewer_store(dialog_manager)
    details = store.get("apps", {})
    marks = await db.game_marks(dialog_manager.middleware_data["account"]["telegram_id"])
    games = []
    for game in await listed_games(dialog_manager, family, marks):
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
        "budget_changed": dialog_manager.dialog_data.get("budget_changed", False),
        "missing_count": len(missing_games(dialog_manager, family, marks)),
    }


async def on_search(message: Message, widget, manager: DialogManager):
    manager.dialog_data["search"] = message.text.strip()[:100]
    await reset_page(message, widget, manager)


async def clear_search(callback: CallbackQuery, button: Button, manager: DialogManager):
    manager.dialog_data.pop("search", None)
    await reset_page(callback, button, manager)


async def pages_getter(dialog_manager: DialogManager, family: Family, db, **kwargs):
    marks = await db.game_marks(dialog_manager.middleware_data["account"]["telegram_id"])
    count = len(await listed_games(dialog_manager, family, marks))
    return {"page_numbers": list(range(1, max(1, -(-count // PAGE_SIZE)) + 1))}


async def on_page(callback: CallbackQuery, widget, manager: DialogManager, page: int):
    await manager.find(SCROLL).set_page(page - 1)
    await manager.switch_to(Games.list)


async def on_game(callback: CallbackQuery, widget, manager: DialogManager, appid: int):
    manager.dialog_data["appid"] = appid
    game = next((a for a in manager.middleware_data["family"].apps if a["appid"] == appid), None)
    manager.dialog_data["game_name"] = game["name"] if game else str(appid)
    await manager.switch_to(Games.game)


async def on_response(callback: CallbackQuery, widget, manager: DialogManager, item_id: str):
    db = manager.middleware_data["db"]
    account = await db.account(callback.from_user.id)
    manager.middleware_data["account"] = account
    parts = item_id.split(":")
    valid = (
        len(parts) == 3
        and parts[0] == str(manager.dialog_data.get("appid"))
        and parts[2] in RESPONSES
        and account is not None
        and account["game_response_version"] == parts[1]
        and account["family_id"] == manager.dialog_data.get("_family_id")
        and account["status"] == "approved"
        and bool(account["refresh_token"])
    )
    if valid:
        families = manager.middleware_data["families"]
        family = families.get(account["family_id"])
        async with family.lock:
            if parts[2] != "c":
                try:
                    snapshot = await families._fetch(account["family_id"])
                except (aiohttp.ClientError, TimeoutError, RuntimeError):
                    snapshot = None
                if snapshot and snapshot["family_id"] == family.family_id:
                    family.snapshot = snapshot
                valid = bool(
                    snapshot
                    and snapshot["family_id"] == account["family_id"]
                    and account["steam_id"] in snapshot["members"]
                    and any(str(a["appid"]) == parts[0] for a in snapshot["apps"])
                )
            if valid:
                valid = await db.set_game_response(account, int(parts[0]), RESPONSES[parts[2]], named=parts[2] == "n")
    manager.middleware_data["account"] = await db.account(callback.from_user.id)
    if not valid:
        await callback.answer(t(manager.middleware_data["lang"], "response_unavailable"), show_alert=True)


async def game_getter(dialog_manager: DialogManager, family: Family, db, lang: str, **kwargs):
    appid = dialog_manager.dialog_data["appid"]
    account = dialog_manager.middleware_data["account"]
    if (
        not account
        or account["family_id"] != family.family_id
        or account["status"] != "approved"
        or not account["refresh_token"]
    ):
        return {
            "game": "—",
            "details": t(lang, "response_unavailable"),
            "store_url": "",
            "response_items": [],
            "status_choices": [],
            "has_game": False,
        }
    marks = await db.game_marks(account["telegram_id"])
    rows = [row for row in await response_rows(dialog_manager, family, db) if row["appid"] == appid]
    accounts = await accounts_by_steam(db, family.family_id)
    viewer = accounts.get(account["steam_id"])
    if (
        not viewer
        or viewer["status"] != "approved"
        or not viewer["refresh_token"]
        or account["steam_id"] not in family.members
        or any(
            viewer.get(key) != account.get(key)
            for key in ("telegram_id", "family_id", "created_at", "publication_version", "game_response_version")
        )
    ):
        return {
            "game": "—",
            "details": t(lang, "response_unavailable"),
            "store_url": "",
            "response_items": [],
            "status_choices": [],
            "has_game": False,
        }
    game = next((a for a in family.apps if a["appid"] == appid), None)
    mark = marks.get(appid)
    status = mark["status"] if mark else "unmarked"
    status_text = t(lang, "personal_status", status=t(lang, "status_" + status)) + "\n" + t(lang, "status_private")
    if game or mark:
        dialog_manager.dialog_data["game_name"] = (game or mark)["name"]
    rows = [
        row
        for row in rows
        if (current := accounts.get(row["steam_id"]))
        and current["status"] == "approved"
        and current["refresh_token"]
        and current["game_response_version"] == row["game_response_version"]
        and row["steam_id"] in family.members
    ]
    own = next((row for row in rows if row["telegram_id"] == account["telegram_id"]), None)
    response = "none" if own is None else ("named_recommend" if own["named"] else own["response"])
    opinions = [
        t(
            lang,
            "response_counts",
            recommend=sum(r["response"] == "recommend" for r in rows),
            together=sum(r["response"] == "together" for r in rows),
        ),
        t(
            lang,
            "response_named",
            names=", ".join(
                person_link(family, r["steam_id"], accounts)
                for r in rows
                if r["response"] == "recommend" and r["named"]
            ),
        )
        if any(r["named"] for r in rows)
        else None,
        t(lang, "response_own", response=t(lang, "response_" + response)),
        t(lang, "response_privacy"),
    ]
    data = {
        "has_game": game is not None,
        "status_choices": [
            (key, ("• " if key == status else "") + t(lang, "status_" + key))
            for key in (*STATUSES, "clear")
            if (key == "clear" and mark) or (key in STATUSES and game)
        ],
        "game": escape(game["name"] if game else dialog_manager.dialog_data.get("game_name", str(appid))),
        "store_url": f"https://store.steampowered.com/app/{appid}/",
        "response_items": [
            (f"{appid}:{account['game_response_version']}:{code}", t(lang, "response_btn_" + code))
            for code in RESPONSES
            if game or code == "c"
        ]
        if account["steam_id"] in family.members
        else [],
    }
    if game is None:
        data["details"] = "\n".join(filter(None, [t(lang, "game_gone"), status_text, *opinions, data["store_url"]]))
        return data
    store = viewer_store(dialog_manager)
    info = store.get("apps", {}).get(str(appid), {})
    tag_names = store.get("tag_names", {})
    tags = ", ".join(tag_names[str(tag)] for tag in info.get("tags", []) if str(tag) in tag_names)
    owners = "".join(f"\n• {person_link(family, o, accounts)}" for o in game["owners"])
    shareable = shareable_copies(game)
    access = (
        "owned"
        if dialog_manager.middleware_data["account"]["steam_id"] in game["owners"]
        else ("borrowed" if shareable else "unavailable")
    )
    modes = play_modes(info)
    lines = [
        status_text,
        f"💲 {escape(info.get('price_text') or t(lang, 'price_unknown'))}",
        f"🏷 {escape(tags)}" if tags else None,
        t(lang, info.get("play_mode") or "mode_unknown"),
        *[
            t(lang, "mode_" + mode) + ": " + t(lang, "mode_status_" + str(supported).lower())
            for mode, supported in modes.items()
        ],
        t(lang, "online_copy_rule") if modes["online_coop"] or modes["online_pvp"] else None,
        t(lang, "local_copy_rule") if modes["local_coop"] else None,
        t(lang, "remote_copy_rule") if modes["remote_play"] else None,
        t(lang, "host_copy_rule") if modes["local_coop"] or modes["remote_play"] else None,
        t(lang, "session_limits") if any(modes.values()) else None,
        t(lang, "copies_line", n=len(game["owners"]), owners=owners),
        t(lang, "shareable_copies_line", n=shareable),
        t(lang, "access_" + access),
        sharing_warning(lang, game.get("exclude_reason", 0)),
        t(lang, "availability_unknown"),
        *opinions,
        f"\nhttps://store.steampowered.com/app/{appid}/",
    ]
    data["details"] = "\n".join(line for line in lines if line)
    return data


async def open_planner(callback: CallbackQuery, button: Button, manager: DialogManager):
    players = manager.find("plan_players")
    await players.reset_checked()
    members = manager.middleware_data["family"].members
    for player in manager.find("players").get_checked():
        if player in members:
            await players.set_checked(player, True)
    await manager.switch_to(Games.planner)


async def planner_getter(dialog_manager: DialogManager, family: Family, lang: str, **kwargs):
    game = next((a for a in family.apps if a["appid"] == dialog_manager.dialog_data["appid"]), None)
    if game is None:
        return {"game": "—", "details": t(lang, "game_gone"), "members": [], "has_game": False}
    players = [p for p in dialog_manager.find("plan_players").get_checked() if p in family.members]
    plan = copy_plan(game, players)
    values = {key: t(lang, "planner_unknown") if value is None else value for key, value in plan.items()}
    info = viewer_store(dialog_manager).get("apps", {}).get(str(game["appid"]), {})
    price = info.get("price_text") if info.get("price") is not None else None
    reason = game.get("exclude_reason")
    lines = [
        t(lang, "planner_summary", **values) if players else t(lang, "planner_pick_players"),
        t(lang, "planner_owned", **values),
        t(lang, "planner_borrowable", **values),
        t(lang, "planner_eligibility_unknown") if reason is None else sharing_warning(lang, reason),
        t(lang, "planner_price", price=escape(price or t(lang, "price_unknown"))),
        t(lang, "planner_price_note"),
        t(lang, "availability_unknown"),
        f"\nhttps://store.steampowered.com/app/{game['appid']}/",
    ]
    return {
        "game": escape(game["name"]),
        "details": "\n".join(line for line in lines if line),
        "members": sorted(((m, family.name(m)) for m in family.members), key=lambda m: m[1].lower()),
        "has_game": True,
    }


async def on_game_status(callback: CallbackQuery, widget, manager: DialogManager, status: str):
    if status not in (*STATUSES, "clear"):
        return
    db, family = manager.middleware_data["db"], manager.middleware_data["family"]
    account = manager.middleware_data["account"]
    appid = manager.dialog_data.get("appid")
    game = next((app for app in family.apps if app["appid"] == appid), None)
    if game is None:
        mark = (await db.game_marks(callback.from_user.id)).get(appid)
        if status != "clear" or mark is None:
            await callback.answer(t(manager.middleware_data["lang"], "game_gone"), show_alert=True)
            return
        name = mark["name"]
    else:
        name = game["name"]
    await db.set_game_status(
        callback.from_user.id,
        account["steam_id"],
        family.family_id,
        appid,
        name,
        None if status == "clear" else status,
        expected_generation=account["token_generation"],
    )
    await reset_page(callback, widget, manager)


async def missing_getter(dialog_manager: DialogManager, family: Family, db, **kwargs):
    marks = await db.game_marks(dialog_manager.middleware_data["account"]["telegram_id"])
    return {"missing_games": missing_games(dialog_manager, family, marks)}


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
    store = viewer_store(dialog_manager)
    names = store.get("tag_names", {})
    sign = currency(store)
    price = current_price(dialog_manager)
    return {
        "prices": [(key, ("• " if price == key else "") + t(lang, "price_" + key)) for key in ("any", "free")],
        "budget": "• " + price_label(dialog_manager, store, lang)
        if isinstance(price, int)
        else t(lang, "btn_budget", currency=sign),
        "currency": escape(sign),
        "currency_known": sign != "¤",
        "currency_unknown": sign == "¤",
        "budget_error": dialog_manager.dialog_data.get("budget_error", False),
        "budget_changed": dialog_manager.dialog_data.get("budget_changed", False),
        "genre": names.get(tag, "?") if tag else t(lang, "genre_any"),
        "modes": [
            (key, ("• " if dialog_manager.dialog_data.get("mode") == key else "") + t(lang, "mode_" + (key or "any")))
            for key in (None, *PLAY_MODES)
        ],
        "player_count": len(dialog_manager.find("players").get_checked()),
        "statuses": [
            (key, ("• " if dialog_manager.dialog_data.get("status", "any") == key else "") + t(lang, "status_" + key))
            for key in ("any", *STATUSES)
        ],
    }


async def on_status_filter(callback: CallbackQuery, widget, manager: DialogManager, status: str):
    if status not in ("any", *STATUSES):
        return
    if status == "any":
        manager.dialog_data.pop("status", None)
    else:
        manager.dialog_data["status"] = status
    await reset_page(callback, widget, manager)


async def on_price(callback: CallbackQuery, widget, manager: DialogManager, price: str):
    manager.dialog_data.update(price=price, budget_changed=False)
    await reset_page(callback, widget, manager)


async def on_mode(callback: CallbackQuery, widget, manager: DialogManager, mode: str):
    manager.dialog_data["mode"] = None if mode == "any" else mode
    if manager.dialog_data["mode"]:
        await manager.find("multiplayer").set_checked(False)
    await reset_page(callback, widget, manager)


async def on_multiplayer(event, widget, manager: DialogManager, *args):
    if manager.find("multiplayer").is_checked():
        manager.dialog_data.pop("mode", None)
    await reset_page(event, widget, manager)


async def open_budget(callback: CallbackQuery, button: Button, manager: DialogManager):
    manager.dialog_data.pop("budget_error", None)
    manager.dialog_data.update(
        budget_country=manager.middleware_data["account"]["country"] or DEFAULT_COUNTRY, budget_changed=False
    )
    await manager.switch_to(Games.budget)


async def on_budget_error(message: Message, widget, manager: DialogManager, error: ValueError):
    manager.dialog_data["budget_error"] = True


async def on_budget(message: Message, widget, manager: DialogManager, price: int):
    sign = currency(viewer_store(manager))
    country = manager.middleware_data["account"]["country"] or DEFAULT_COUNTRY
    if manager.dialog_data.get("budget_country") != country:
        manager.dialog_data["budget_changed"] = True
    elif sign != "¤":
        manager.dialog_data.update(price=price, price_currency=sign, price_country=country, budget_changed=False)
        await reset_page(message, widget, manager)
    await manager.switch_to(Games.filters)


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
    manager.dialog_data.pop("mode", None)
    manager.dialog_data.pop("status", None)
    await manager.find("multiplayer").set_checked(False)
    await manager.find("recommended").set_checked(False)
    await manager.find("players").reset_checked()
    await on_start(None, manager)
    await reset_page(callback, button, manager)


async def players_getter(family: Family, **kwargs):
    return {"members": sorted(((m, family.name(m)) for m in family.members), key=lambda m: m[1].lower())}


async def on_players(event, widget, manager: DialogManager, *args):
    # Playing together means a multiplayer game.
    if len(manager.find("players").get_checked()) > 1 and not manager.dialog_data.get("mode"):
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
        Tr("budget_region_changed", when="budget_changed"),
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
        SwitchTo(Tr("btn_missing_games"), id="missing", state=Games.missing, when="missing_count"),
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
        Tr("budget_unknown_prices"),
        Tr("budget_currency_unknown", when="currency_unknown"),
        Tr("budget_region_changed", when="budget_changed"),
        Group(
            Select(
                Format("{item[1]}"),
                id="price",
                items="prices",
                item_id_getter=lambda p: p[0],
                on_click=on_price,
            ),
            width=2,
        ),
        Button(Format("{budget}"), id="budget", on_click=open_budget, when="currency_known"),
        Checkbox(Tr("multiplayer_on"), Tr("multiplayer_off"), id="multiplayer", on_state_changed=on_multiplayer),
        Group(
            Select(
                Format("{item[1]}"),
                id="mode",
                items="modes",
                item_id_getter=lambda item: item[0] or "any",
                on_click=on_mode,
            ),
            width=1,
        ),
        Checkbox(Tr("recommended_on"), Tr("recommended_off"), id="recommended", on_state_changed=reset_page),
        SwitchTo(Tr("btn_genre"), id="genre", state=Games.genre),
        SwitchTo(Tr("btn_players"), id="players_btn", state=Games.players),
        Group(
            Select(
                Format("{item[1]}"),
                id="status_filter",
                items="statuses",
                item_id_getter=lambda item: item[0],
                on_click=on_status_filter,
            ),
            width=2,
        ),
        Button(Tr("btn_reset_filters"), id="reset", on_click=reset_filters),
        back_to_list(),
        getter=filters_getter,
        state=Games.filters,
    ),
    Window(
        Header("filters", "budget"),
        Tr("budget_invalid", when="budget_error"),
        TextInput(id="budget_input", type_factory=parse_budget, on_success=on_budget, on_error=on_budget_error),
        back_to_filters(),
        getter=filters_getter,
        state=Games.budget,
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
        Header("library", "missing_games"),
        ScrollingGroup(
            Select(
                Format("{item[name]}"),
                id="missing_game",
                items="missing_games",
                item_id_getter=lambda game: game["appid"],
                type_factory=int,
                on_click=on_game,
            ),
            id="missing_scroll",
            width=1,
            height=PAGE_SIZE,
            hide_on_single_page=True,
        ),
        back_to_list(),
        getter=missing_getter,
        state=Games.missing,
    ),
    Window(
        Header("library", "game"),
        # The card also links owner profiles; the preview should still show the game.
        LinkPreview(url=Format("{store_url}"), prefer_large_media=True, when="store_url"),
        Select(
            Format("{item[1]}"),
            id="vote",
            items="response_items",
            item_id_getter=lambda item: item[0],
            on_click=on_response,
        ),
        Button(Tr("btn_plan_copies"), id="plan_copies", on_click=open_planner, when="has_game"),
        Group(
            Select(
                Format("{item[1]}"),
                id="game_status",
                items="status_choices",
                item_id_getter=lambda item: item[0],
                on_click=on_game_status,
            ),
            width=2,
        ),
        back_to_list(),
        getter=game_getter,
        state=Games.game,
    ),
    Window(
        Header("library", "planner"),
        Group(
            Multiselect(
                Format("✅ {item[1]}"),
                Format("⬜ {item[1]}"),
                id="plan_players",
                items="members",
                item_id_getter=lambda m: m[0],
            ),
            width=2,
            when="has_game",
        ),
        back_home(SwitchTo(Tr("btn_back"), id="back_game", state=Games.game)),
        getter=planner_getter,
        state=Games.planner,
    ),
    on_start=on_start,
)
