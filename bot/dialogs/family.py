"""The family section: who is in it, stats with charts, member cards and the owner's Steam family actions."""

import asyncio
import logging
from datetime import UTC, datetime
from html import escape

from aiogram import F
from aiogram.enums import ContentType
from aiogram.types import CallbackQuery, Message
from aiogram_dialog import Data, Dialog, DialogManager, Window
from aiogram_dialog.api.entities import MediaAttachment
from aiogram_dialog.widgets.input import TextInput
from aiogram_dialog.widgets.kbd import Button, Cancel, Group, Row, ScrollingGroup, Select, Start, SwitchTo
from aiogram_dialog.widgets.link_preview import LinkPreview
from aiogram_dialog.widgets.media import DynamicMedia
from aiogram_dialog.widgets.text import Format

from .. import charts, library
from ..family import Families, Family, accounts_by_steam, person_link, role
from ..i18n import Tr, t
from ..states import Admin, FamilyView, Games
from ..steam import (
    SteamActionError,
    cancel_invite,
    decline_purchase,
    invite_member,
    purchase_requests,
    remove_member,
    rename_family,
    resolve_profile,
)
from ..ui import BLUE, GREEN, RED, Header, back_home, look

log = logging.getLogger(__name__)
# Previews only for store pages: profile links would show a big avatar card.
NO_PREVIEW = LinkPreview(is_disabled=True)
STATS = ["games", "value", "exclusive", "added", "genres", "borrowed_games", "borrowed_members"]


def date(timestamp) -> str:
    return datetime.fromtimestamp(int(timestamp), UTC).strftime("%d.%m.%Y") if timestamp else "—"


def viewer_store(manager: DialogManager) -> dict:
    return manager.middleware_data["family"].store(manager.middleware_data["account"]["country"])


# Family overview
async def info_getter(dialog_manager: DialogManager, family: Family, account, db, lang: str, **kwargs):
    owner = await db.owner_steam_id(family.family_id)
    accounts = await accounts_by_steam(db, family.family_id)
    players = family.snapshot.get("players", {})
    lines = []
    for member in sorted(family.members, key=lambda m: family.snapshot.get("joined", {}).get(m) or 0):
        marks = (" 👑" if member == owner else "") + (" 🟢" if players.get(member, {}).get("online") else "")
        child = f" ({t(lang, 'role_child')})" if family.snapshot.get("roles", {}).get(member) == 2 else ""
        lines.append(f"• {person_link(family, member, accounts)}{child}{marks}")
    free = family.snapshot.get("free_spots", 0)
    return {
        "name": escape(family.snapshot["name"]),
        "members_list": "\n".join(lines),
        "count": len(family.members),
        "size": len(family.members) + free,
        "free": free,
        "invites": len(family.snapshot.get("pending_invites", [])),
        "is_owner": role(account) == "owner",
    }


# Stats
async def stats_getter(dialog_manager: DialogManager, family: Family, lang: str, **kwargs):
    store = viewer_store(dialog_manager)
    numbers = library.summary(family.apps, store)
    return {
        **numbers,
        "value": f"{numbers['value']:,.0f}".replace(",", " "),
        "currency": library.currency(store),
        "stats": [(key, t(lang, f"stat_{key}")) for key in STATS],
    }


async def on_stat(callback: CallbackQuery, widget, manager: DialogManager, key: str):
    manager.dialog_data["stat"] = key
    await manager.switch_to(FamilyView.stat)


def render_stat(key: str, family: Family, store: dict, lang: str) -> str:
    title = t(lang, f"stat_{key}")
    names = {m: family.name(m) for m in family.members}
    by_member = None
    if key == "games":
        by_member = library.games_per_member(family.apps, family.members)
    elif key == "value":
        return charts.bars(
            title,
            sorted(
                ((names[m], v) for m, v in library.value_per_member(family.apps, store, family.members).items()),
                key=lambda item: -item[1],
            ),
            " " + library.currency(store),
        )
    elif key == "exclusive":
        by_member = library.exclusive_per_member(family.apps, family.members)
    elif key == "added":
        return charts.columns(title, library.added_per_month(family.apps))
    elif key == "genres":
        tag_names = store.get("tag_names", {})
        return charts.bars(
            title, [(tag_names[tag], count) for tag, count in library.tag_counts(family.apps, store)[:10]]
        )
    elif key == "borrowed_games":
        return charts.bars(
            title, library.borrowed_hours_per_game(family.snapshot.get("playtime", []), family.apps), " h"
        )
    elif key == "borrowed_members":
        hours = library.borrowed_hours_per_member(family.snapshot.get("playtime", []), family.members)
        return charts.bars(title, sorted(((names[m], h) for m, h in hours.items()), key=lambda item: -item[1]), " h")
    return charts.bars(title, sorted(((names[m], n) for m, n in by_member.items()), key=lambda item: -item[1]))


async def stat_getter(dialog_manager: DialogManager, family: Family, lang: str, **kwargs):
    key = dialog_manager.dialog_data["stat"]
    path = await asyncio.to_thread(render_stat, key, family, viewer_store(dialog_manager), lang)
    return {
        "chart": MediaAttachment(ContentType.PHOTO, path=path),
        "stat_name": t(lang, f"stat_{key}"),
        "stat_hint": t(lang, f"stat_{key}_hint"),
    }


# Members
async def members_getter(family: Family, **kwargs):
    players = family.snapshot.get("players", {})
    return {
        "members": [
            (m, ("🟢 " if players.get(m, {}).get("online") else "") + family.name(m))
            for m in sorted(family.members, key=lambda m: family.name(m).lower())
        ]
    }


async def on_member(callback: CallbackQuery, widget, manager: DialogManager, steam_id: str):
    manager.dialog_data["member"] = steam_id
    await manager.switch_to(FamilyView.member)


async def member_getter(
    dialog_manager: DialogManager, family: Family, families: Families, account, db, lang: str, **kwargs
):
    steam_id = dialog_manager.dialog_data["member"]
    links = person_link(family, steam_id, await accounts_by_steam(db, family.family_id))
    store = viewer_store(dialog_manager)
    player = family.snapshot.get("players", {}).get(steam_id, {})
    stats = await families.member_stats(family.family_id, steam_id)
    unknown = t(lang, "private")
    own = [a for a in family.apps if steam_id in a["owners"]]
    hours_borrowed = library.borrowed_hours_per_member(family.snapshot.get("playtime", []), [steam_id])[steam_id]
    in_family = steam_id in family.members
    return {
        "member_name": escape(family.name(steam_id)),
        "links": links,
        "status": t(lang, "online")
        if player.get("online")
        else t(lang, "last_online", when=date(player.get("last_online"))),
        "level": stats.get("level") if stats.get("level") is not None else unknown,
        "created": date(player.get("created")),
        "steam_games": stats.get("games") if stats.get("games") is not None else unknown,
        "hours": stats.get("hours") if stats.get("hours") is not None else unknown,
        "joined": date(family.snapshot.get("joined", {}).get(steam_id)),
        "family_games": len(own),
        "exclusive": sum(a["owners"] == [steam_id] for a in own),
        "value": f"{sum(library.price_of(store, a['appid']) or 0 for a in own) / 100:,.0f}".replace(",", " "),
        "currency": library.currency(store),
        "borrowed": hours_borrowed,
        "not_me": in_family and steam_id != account["steam_id"],
        "can_remove": in_family and role(account) == "owner" and steam_id != account["steam_id"],
    }


async def common_games(callback: CallbackQuery, button: Button, manager: DialogManager):
    me, them = manager.middleware_data["account"]["steam_id"], manager.dialog_data["member"]
    await manager.start(Games.list, data={"owners": [me, them]})


# Owner actions on the Steam family
async def act(manager: DialogManager, action, *args) -> None:
    """Runs a Steam family change with the acting user's own login and shows the outcome on the manage screen."""
    families: Families = manager.middleware_data["families"]
    family: Family = manager.middleware_data["family"]
    lang = manager.middleware_data["lang"]
    try:
        token = await families.owner_token(manager.middleware_data["account"])
        await action(families.http, token, family.family_id, *args)
    except SteamActionError as error:
        manager.dialog_data["notice"] = t(lang, "action_failed", error=escape(str(error)))
    except Exception:
        log.exception("Steam family action failed")
        manager.dialog_data["notice"] = t(lang, "action_failed", error="—")
    else:
        manager.dialog_data["notice"] = t(lang, "action_done")
        asyncio.create_task(families.refresh(family.family_id))
    await manager.switch_to(FamilyView.manage)


async def manage_getter(dialog_manager: DialogManager, family: Family, db, account, **kwargs):
    accounts = await db.family_accounts(family.family_id)
    return {
        "pending": sum(a["status"] == "pending" and a["telegram_id"] != account["telegram_id"] for a in accounts),
        "notice": dialog_manager.dialog_data.get("notice", ""),
        "free": family.snapshot.get("free_spots", 0),
        "invites": len(family.snapshot.get("pending_invites", [])),
    }


async def clear_notice(callback: CallbackQuery, widget, manager: DialogManager, *args):
    manager.dialog_data.pop("notice", None)
    manager.dialog_data.pop("error", None)


async def on_invitee(message: Message, widget, manager: DialogManager, text: str):
    steam_id = await resolve_profile(manager.middleware_data["families"].http, text)
    if steam_id is None:
        manager.dialog_data["error"] = True
        return
    manager.dialog_data.pop("error", None)
    manager.dialog_data["invitee"] = steam_id
    await manager.switch_to(FamilyView.invite_role)


async def invite_role_getter(dialog_manager: DialogManager, **kwargs):
    steam_id = dialog_manager.dialog_data["invitee"]
    return {"invitee": f'<a href="https://steamcommunity.com/profiles/{steam_id}/">{steam_id}</a>'}


async def invite_adult(callback: CallbackQuery, button: Button, manager: DialogManager):
    await act(manager, invite_member, manager.dialog_data["invitee"], 1)


async def invite_child(callback: CallbackQuery, button: Button, manager: DialogManager):
    await act(manager, invite_member, manager.dialog_data["invitee"], 2)


async def invites_getter(family: Family, **kwargs):
    return {"invites": [(i["steamid"], i["steamid"]) for i in family.snapshot.get("pending_invites", [])]}


async def on_cancel_invite(callback: CallbackQuery, widget, manager: DialogManager, steam_id: str):
    await act(manager, cancel_invite, steam_id)


async def on_remove(callback: CallbackQuery, button: Button, manager: DialogManager):
    await act(manager, remove_member, manager.dialog_data["member"])


async def on_rename(message: Message, widget, manager: DialogManager, name: str):
    name = name.strip()
    if not 1 <= len(name) <= 64:
        manager.dialog_data["error"] = True
        return
    manager.dialog_data.pop("error", None)
    await act(manager, rename_family, name)


async def purchases_getter(dialog_manager: DialogManager, family: Family, families: Families, account, **kwargs):
    try:
        requests = await purchase_requests(families.http, await families.owner_token(account), family.family_id)
    except Exception:
        log.exception("Could not load purchase requests")
        requests = []
    return {
        "requests": [
            (str(r["request_id"]), f"❌ {family.name(str(r['requester_steamid']))} · {date(r.get('time_requested'))}")
            for r in requests
        ]
    }


async def on_decline(callback: CallbackQuery, widget, manager: DialogManager, request_id: str):
    await act(manager, decline_purchase, request_id)


async def on_start(start_data: Data, manager: DialogManager):
    if manager.middleware_data["family"].snapshot is None:  # opened before the family finished loading
        await manager.done()


def back_to(state, id_: str):
    return back_home(SwitchTo(Tr("btn_back"), id=id_, state=state, on_click=clear_notice))


family_dialog = Dialog(
    Window(
        Header("family", "family"),
        Tr("family_info"),
        NO_PREVIEW,
        Row(
            SwitchTo(Tr("btn_stats"), id="stats", state=FamilyView.stats, style=look(color=BLUE)),
            SwitchTo(Tr("btn_members_list"), id="members", state=FamilyView.members, style=look("family", BLUE)),
        ),
        SwitchTo(Tr("btn_manage"), id="manage", state=FamilyView.manage, when="is_owner", style=look("admin")),
        Cancel(Tr("btn_back")),
        getter=info_getter,
        state=FamilyView.info,
    ),
    Window(
        Header("family", "stats"),
        Tr("stats_summary"),
        Group(
            Select(Format("{item[1]}"), id="stat", item_id_getter=lambda s: s[0], items="stats", on_click=on_stat),
            width=2,
        ),
        back_to(FamilyView.info, "back_info"),
        getter=stats_getter,
        state=FamilyView.stats,
    ),
    Window(
        DynamicMedia("chart"),
        Header("family", "stat"),
        back_to(FamilyView.stats, "back_stats"),
        getter=stat_getter,
        state=FamilyView.stat,
    ),
    Window(
        Header("family", "members_list"),
        ScrollingGroup(
            Select(
                Format("{item[1]}"), id="member", item_id_getter=lambda m: m[0], items="members", on_click=on_member
            ),
            id="members_scroll",
            width=2,
            height=5,
            hide_on_single_page=True,
        ),
        back_to(FamilyView.info, "back_info"),
        getter=members_getter,
        state=FamilyView.members,
    ),
    Window(
        Header("family", "member_card"),
        NO_PREVIEW,
        Button(Tr("btn_common_games"), id="common", on_click=common_games, when="not_me", style=look("library", GREEN)),
        SwitchTo(
            Tr("btn_remove_member"), id="remove", state=FamilyView.remove, when="can_remove", style=look("remove", RED)
        ),
        back_to(FamilyView.members, "back_members"),
        getter=member_getter,
        state=FamilyView.member,
    ),
    Window(
        Header("remove", "remove_member"),
        Button(Tr("btn_remove_member_yes"), id="remove_yes", on_click=on_remove, style=look("remove", RED)),
        back_to(FamilyView.member, "back_member"),
        getter=member_getter,
        state=FamilyView.remove,
    ),
    Window(
        Header("admin", "manage"),
        Format("{notice}", when="notice"),
        Start(Tr("btn_members"), id="bot_members", state=Admin.members, style=look("family"), on_click=clear_notice),
        SwitchTo(
            Tr("btn_invite"),
            id="invite",
            state=FamilyView.invite,
            when="free",
            on_click=clear_notice,
            style=look(color=GREEN),
        ),
        SwitchTo(Tr("btn_invites"), id="invites", state=FamilyView.invites, when="invites", on_click=clear_notice),
        SwitchTo(Tr("btn_rename"), id="rename", state=FamilyView.rename, on_click=clear_notice),
        SwitchTo(Tr("btn_purchases"), id="purchases", state=FamilyView.purchases, on_click=clear_notice),
        back_to(FamilyView.info, "back_info"),
        getter=manage_getter,
        state=FamilyView.manage,
    ),
    Window(
        Header("admin", "invite"),
        Tr("invite_error", when=F["dialog_data"]["error"]),
        TextInput(id="invitee", on_success=on_invitee),
        back_to(FamilyView.manage, "back_manage"),
        state=FamilyView.invite,
    ),
    Window(
        Header("admin", "invite_role"),
        NO_PREVIEW,
        Row(
            Button(Tr("btn_role_adult"), id="adult", on_click=invite_adult, style=look(color=GREEN)),
            Button(Tr("btn_role_child"), id="child", on_click=invite_child, style=look(color=BLUE)),
        ),
        back_to(FamilyView.invite, "back_invite"),
        getter=invite_role_getter,
        state=FamilyView.invite_role,
    ),
    Window(
        Header("admin", "invites"),
        Group(
            Select(
                Format("❌ {item[1]}"),
                id="invite_item",
                item_id_getter=lambda i: i[0],
                items="invites",
                on_click=on_cancel_invite,
            ),
            width=1,
        ),
        back_to(FamilyView.manage, "back_manage"),
        getter=invites_getter,
        state=FamilyView.invites,
    ),
    Window(
        Header("admin", "rename"),
        Tr("rename_error", when=F["dialog_data"]["error"]),
        TextInput(id="family_name", on_success=on_rename),
        back_to(FamilyView.manage, "back_manage"),
        state=FamilyView.rename,
    ),
    Window(
        Header("admin", "purchases"),
        Tr("purchases_empty", when=~F["requests"]),
        Group(
            Select(
                Format("{item[1]}"), id="request", item_id_getter=lambda r: r[0], items="requests", on_click=on_decline
            ),
            width=1,
        ),
        back_to(FamilyView.manage, "back_manage"),
        getter=purchases_getter,
        state=FamilyView.purchases,
    ),
    on_start=on_start,
)
