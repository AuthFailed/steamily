import logging
from html import escape

from aiogram import F
from aiogram.types import CallbackQuery
from aiogram_dialog import Dialog, DialogManager, StartMode, Window
from aiogram_dialog.widgets.kbd import Cancel, Group, Select
from aiogram_dialog.widgets.text import Format

from ..family import role
from ..i18n import Tr, language_of, t
from ..states import Menu, Ownership
from ..ui import GREEN, RED, Header, back_home, look
from . import require_owner

log = logging.getLogger(__name__)


async def on_start(start_data, manager: DialogManager):
    account = manager.middleware_data["account"]
    if account:
        manager.dialog_data["family_id"] = account["family_id"]


async def ownership_getter(dialog_manager: DialogManager, db, family, account, **kwargs):
    transfer = await db.owner_transfer(account["family_id"])
    invited = transfer is not None and transfer["target_id"] == account["telegram_id"]
    candidates = []
    if role(account) == "owner" and transfer is None:
        candidates = [
            (candidate["telegram_id"], family.name(candidate["steam_id"]))
            for candidate in await db.family_accounts(account["family_id"])
            if candidate["telegram_id"] != account["telegram_id"]
            and candidate["status"] == "approved"
            and candidate["refresh_token"]
            and candidate["steam_id"] in family.members
        ]
    return {
        "pending": transfer is not None,
        "invited": invited,
        "candidates": candidates,
        "target": escape(family.name(transfer["target_steam_id"])) if transfer else "",
        "requester": escape(family.name(transfer["owner_steam_id"])) if transfer else "",
        "expires": transfer["expires_at"].strftime("%Y-%m-%d %H:%M UTC") if transfer else "",
        "transfer_ids": [transfer["id"]] if transfer else [],
    }


async def notify_participants(manager: DialogManager, transfer, key: str):
    db = manager.middleware_data["db"]
    family = manager.middleware_data["families"].get(transfer["family_id"])
    values = {
        "requester": escape(family.name(transfer["owner_steam_id"])),
        "target": escape(family.name(transfer["target_steam_id"])),
    }
    for telegram_id in (transfer["owner_id"], transfer["target_id"]):
        try:
            lang = await db.language(telegram_id)
            await manager.middleware_data["bot"].send_message(telegram_id, t(lang, key, **values))
        except Exception:
            log.exception("Could not notify ownership transfer participant %s", telegram_id)


async def refresh_recipient(manager: DialogManager, transfer):
    for telegram_id in (transfer["owner_id"], transfer["target_id"]):
        if telegram_id == manager.event.from_user.id:
            continue
        try:
            await manager.bg(user_id=telegram_id, chat_id=telegram_id).start(Menu.main, mode=StartMode.RESET_STACK)
        except Exception:
            log.exception("Could not refresh ownership transfer participant %s", telegram_id)


async def answer_result(callback: CallbackQuery, manager: DialogManager, key: str):
    try:
        await callback.answer(t(language_of(manager), key), show_alert=key == "ownership_unavailable")
    except Exception:
        log.exception("Could not answer ownership transfer callback for %s", callback.from_user.id)


async def invite(callback: CallbackQuery, widget, manager: DialogManager, target_id: str):
    account = await require_owner(manager)
    if account is None:
        return
    if not target_id.isdecimal() or not 0 < int(target_id) < 2**63:
        await answer_result(callback, manager, "ownership_unavailable")
        return
    transfer = await manager.middleware_data["families"].create_owner_transfer(
        account["family_id"], callback.from_user.id, int(target_id)
    )
    if transfer is None:
        await answer_result(callback, manager, "ownership_unavailable")
        return
    await notify_participants(manager, transfer, "ownership_requested")
    await answer_result(callback, manager, "ownership_sent")


async def cancel(callback: CallbackQuery, widget, manager: DialogManager, transfer_id: str):
    account = await require_owner(manager)
    if account is None:
        return
    transfer = await manager.middleware_data["db"].cancel_owner_transfer(
        account["family_id"], callback.from_user.id, transfer_id
    )
    if transfer is None:
        await answer_result(callback, manager, "ownership_unavailable")
        return
    await notify_participants(manager, transfer, "ownership_cancelled")
    await answer_result(callback, manager, "ownership_cancelled_short")


async def accept(callback: CallbackQuery, widget, manager: DialogManager, transfer_id: str):
    db = manager.middleware_data["db"]
    account = await db.account(callback.from_user.id)
    if account is None or account["family_id"] != manager.dialog_data.get("family_id"):
        await answer_result(callback, manager, "ownership_unavailable")
        return
    transfer = await manager.middleware_data["families"].accept_owner_transfer(
        account["family_id"], callback.from_user.id, transfer_id
    )
    if transfer is None:
        await answer_result(callback, manager, "ownership_unavailable")
        return
    await notify_participants(manager, transfer, "ownership_completed")
    manager.middleware_data["account"] = await db.account(callback.from_user.id)
    await answer_result(callback, manager, "ownership_completed_short")
    try:
        await manager.start(Menu.main, mode=StartMode.RESET_STACK)
    except Exception:
        log.exception("Could not reset menu after ownership transfer for %s", callback.from_user.id)
    await refresh_recipient(manager, transfer)


ownership_dialog = Dialog(
    Window(
        Header("admin", "ownership"),
        Tr("ownership_pending", when="pending"),
        Tr("ownership_empty", when=~F["pending"] & ~F["candidates"]),
        Group(
            Select(
                Format("{item[1]}"),
                id="transfer_to",
                item_id_getter=lambda item: item[0],
                items="candidates",
                on_click=invite,
            ),
            width=1,
        ),
        Select(
            Tr("btn_cancel_ownership"),
            id="cancel_transfer",
            item_id_getter=lambda item: item,
            items="transfer_ids",
            on_click=cancel,
            when="pending",
            style=look("cancel", RED),
        ),
        back_home(Cancel(Tr("btn_back"))),
        getter=ownership_getter,
        state=Ownership.manage,
    ),
    Window(
        Header("admin", "ownership"),
        Tr("ownership_invitation", when="invited"),
        Tr("ownership_unavailable", when=~F["invited"]),
        Select(
            Tr("btn_accept_ownership"),
            id="accept_transfer",
            item_id_getter=lambda item: item,
            items="transfer_ids",
            on_click=accept,
            when="invited",
            style=look("approve", GREEN),
        ),
        back_home(Cancel(Tr("btn_back"))),
        getter=ownership_getter,
        state=Ownership.invitation,
    ),
    on_start=on_start,
)
