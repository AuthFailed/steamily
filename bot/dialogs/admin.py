from html import escape

from aiogram import F
from aiogram.types import CallbackQuery
from aiogram_dialog import Dialog, DialogManager, StartMode, Window
from aiogram_dialog.widgets.kbd import Button, Cancel, ScrollingGroup, Select, SwitchTo
from aiogram_dialog.widgets.text import Format

from ..db import Db
from ..family import Family
from ..i18n import Tr, t
from ..states import Admin, Menu
from ..ui import GREEN, RED, Header, back_home, look

STATUS_ICONS = {"pending": "⏳", "approved": "✅", "blocked": "⛔"}


async def members_getter(db: Db, family: Family, account, **kwargs):
    accounts = await db.family_accounts(family.family_id)
    rows = [
        (str(a["telegram_id"]), f"{STATUS_ICONS[a['status']]} {family.name(a['steam_id'])}")
        for a in accounts
        if a["telegram_id"] != account["telegram_id"]
    ]  # the owner looking at the list
    return {"accounts": rows, "count": len(rows)}


async def on_member(callback: CallbackQuery, widget, manager: DialogManager, telegram_id: str):
    manager.dialog_data["telegram_id"] = int(telegram_id)
    await manager.switch_to(Admin.member)


async def member_getter(dialog_manager: DialogManager, db: Db, family: Family, lang: str, **kwargs):
    account = await db.account(dialog_manager.dialog_data["telegram_id"])
    if account is None or account["family_id"] != family.family_id:
        return {
            "name": "—",
            "steam": "—",
            "telegram": "—",
            "status_text": "",
            "warning": t(lang, "member_gone"),
            "status": None,
        }
    steam = account["steam_id"]
    return {
        "name": escape(family.name(steam)),
        "steam": steam,
        "telegram": account["telegram_id"],
        "status_text": t(lang, f"status_{account['status']}_short"),
        "warning": "" if not family.members or steam in family.members else t(lang, "member_left"),
        "status": account["status"],
    }


async def set_status(manager: DialogManager, status: str | None) -> None:
    db: Db = manager.middleware_data["db"]
    telegram_id = manager.dialog_data["telegram_id"]
    target = await db.account(telegram_id)
    family: Family = manager.middleware_data["family"]
    if (
        target is None
        or target["family_id"] != family.family_id
        or (status == "approved" and target["steam_id"] not in family.members)
    ):
        await manager.switch_to(Admin.members)
        return
    if status:
        await db.set_status(telegram_id, status)
    else:
        await db.delete_account(telegram_id)
    # Reset the user's own dialogs so they immediately see their new access level.
    await manager.bg(user_id=telegram_id, chat_id=telegram_id).start(Menu.main, mode=StartMode.RESET_STACK)


async def approve(callback: CallbackQuery, button: Button, manager: DialogManager):
    await set_status(manager, "approved")


async def block(callback: CallbackQuery, button: Button, manager: DialogManager):
    await set_status(manager, "blocked")


async def remove(callback: CallbackQuery, button: Button, manager: DialogManager):
    await set_status(manager, None)
    await manager.switch_to(Admin.members)


admin_dialog = Dialog(
    Window(
        Header("family", "members"),
        Tr("members_count", when=F["accounts"]),
        Tr("members_empty", when=~F["accounts"]),
        ScrollingGroup(
            Select(
                Format("{item[1]}"), id="account", item_id_getter=lambda a: a[0], items="accounts", on_click=on_member
            ),
            id="accounts_scroll",
            width=1,
            height=8,
            hide_on_single_page=True,
        ),
        back_home(Cancel(Tr("btn_back"))),  # back to family management, which opened this list
        getter=members_getter,
        state=Admin.members,
    ),
    Window(
        Header("family", "member"),
        Button(
            Tr("btn_approve"),
            id="approve",
            on_click=approve,
            style=look("approve", GREEN),
            when=F["status"].in_({"pending", "blocked"}),
        ),
        Button(
            Tr("btn_block"),
            id="block",
            on_click=block,
            style=look("block", RED),
            when=F["status"].in_({"pending", "approved"}),
        ),
        Button(Tr("btn_remove"), id="remove", on_click=remove, style=look("remove", RED), when="status"),
        back_home(SwitchTo(Tr("btn_back"), id="back_members", state=Admin.members)),
        getter=member_getter,
        state=Admin.member,
    ),
)
