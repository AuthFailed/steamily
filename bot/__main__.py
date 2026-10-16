import asyncio
import logging
from html import escape

import aiohttp
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.dispatcher.flags import get_flag
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, ExceptionTypeFilter
from aiogram.types import CallbackQuery, ChatMemberUpdated, ErrorEvent, Message
from aiogram_dialog import DIALOG_EVENT_NAME, DialogManager, ShowMode, StartMode, setup_dialogs
from aiogram_dialog.api.entities import DialogUpdateEvent
from aiogram_dialog.api.exceptions import OutdatedIntent, UnknownIntent
from aiogram_dialog.utils import remove_intent_id
from aiogram_dialog.widgets.kbd import Start, SwitchTo

from . import charts
from .config import ConfigError, load_config
from .db import Db, PgStorage
from .dialogs import deny_action
from .dialogs.admin import admin_dialog
from .dialogs.family import family_dialog
from .dialogs.games import games_dialog
from .dialogs.menu import menu_dialog
from .family import PRESENCE_INTERVAL, Families, role
from .health import STATUS_PATH, Health
from .i18n import DEFAULT, t
from .inline import router as inline_router
from .login import Logins
from .states import Admin, FamilyView, Games, Menu

router = Router()
log = logging.getLogger(__name__)


class GroupMigrationMiddleware(BaseMiddleware):
    """Handle service notices before dialog middleware, which requires a sender."""

    async def __call__(self, handler, message: Message, data: dict):
        if (
            message
            and message.chat.type != "private"
            and (message.migrate_to_chat_id is not None or message.migrate_from_chat_id is not None)
        ):
            await data["db"].migrate_chat(
                message.migrate_from_chat_id or message.chat.id,
                message.migrate_to_chat_id or message.chat.id,
            )
            return None
        return await handler(message, data)


class AccountMiddleware(BaseMiddleware):
    """Loads the sender's account and family. In groups only /link gets through."""

    async def __call__(self, handler, event, data: dict):
        # Messages, button presses, and aiogram-dialog's own background updates (which carry .chat).
        source = event.update.event if isinstance(event, ErrorEvent) else event
        chat = (
            source.message.chat
            if isinstance(source, CallbackQuery) and source.message
            else getattr(source, "chat", None)
        )
        if chat is None or source.from_user is None:
            return None
        private = chat.type == "private" or isinstance(source, DialogUpdateEvent) and chat.id == source.from_user.id
        if not private and not (isinstance(source, Message) and (source.text or "").startswith("/link")):
            return None
        language = await data["db"].language(source.from_user.id)
        data["has_language"] = language is not None
        data["lang"] = language or ("ru" if (source.from_user.language_code or "").startswith("ru") else DEFAULT)
        account = await data["db"].account(source.from_user.id)
        user = source.from_user
        if (
            account
            and isinstance(source, (Message, CallbackQuery))
            and (account["tg_username"], account["tg_name"]) != (user.username, user.full_name)
        ):
            await data["db"].set_telegram_profile(user.id, user.username, user.full_name)  # kept current for links
        data["account"] = account
        data["family"] = data["families"].get(account["family_id"]) if account else None
        data["deleted_dialogs"] = deleted_dialogs = []
        try:
            return await handler(event, data)
        finally:
            for telegram_id in deleted_dialogs:
                # Dialog middleware saves the final screen after its callback returns.
                await data["db"].delete_dialogs(telegram_id)


OWNER_STATES = {
    *Admin.__all_states__,
    Menu.group_alerts,
    FamilyView.remove,
    FamilyView.manage,
    FamilyView.invite,
    FamilyView.invite_role,
    FamilyView.invites,
    FamilyView.rename,
    FamilyView.purchases,
}


class AccessMiddleware(BaseMiddleware):
    """Recheck access before processing an existing dialog or one of its stale buttons."""

    async def __call__(self, handler, event, data: dict):
        manager: DialogManager = data["dialog_manager"]
        account = data["account"]
        family_id = account["family_id"] if account else None
        user_role = role(account)
        if manager.has_context() and not get_flag(data, "public_info"):
            context = manager.current_context()
            states = [context.state]
            widget_id = ""
            if isinstance(event, CallbackQuery) and event.data:
                _, callback_data = remove_intent_id(event.data)
                widget_id = callback_data.split(":", 1)[0]
                widget = manager.dialog().find(widget_id)
                if isinstance(widget, (Start, SwitchTo)):
                    states.append(widget.state)
            owner_action = any(state in OWNER_STATES for state in states) or (
                context.state == Menu.settings and widget_id in {"requests_on", "requests_off"}
            )
            denied = context.dialog_data.get("_family_id") != family_id
            denied |= owner_action and user_role != "owner"
            denied |= any(state.group in (Games, FamilyView, Admin) for state in states) and user_role not in (
                "member",
                "owner",
            )
            if denied:
                if owner_action:
                    await deny_action(manager)
                else:
                    await manager.start(Menu.main, mode=StartMode.RESET_STACK)
                    if isinstance(event, CallbackQuery):
                        await manager.answer_callback()
                manager.dialog_data["_family_id"] = family_id
                return None
        result = await handler(event, data)
        if manager.has_context():
            account = data["account"]  # logout clears it while handling this update
            manager.dialog_data["_family_id"] = account["family_id"] if account else None
        return result


@router.message(CommandStart(), F.chat.type == "private")
async def start(message: Message, dialog_manager: DialogManager, logins: Logins, has_language: bool):
    logins.cancel(message.from_user.id)
    await dialog_manager.start(Menu.main if has_language else Menu.language, mode=StartMode.RESET_STACK)


@router.message(Command("about", "privacy", "status", "delete"), F.chat.type == "private", flags={"public_info": True})
async def public_info(message: Message, dialog_manager: DialogManager, logins: Logins):
    logins.cancel(message.from_user.id)
    command = message.text.split()[0].split("@")[0]
    state = {"/about": Menu.about, "/privacy": Menu.privacy, "/status": Menu.status, "/delete": Menu.logout}[command]
    await dialog_manager.start(state, mode=StartMode.RESET_STACK)


@router.message(Command("link"), F.chat.type != "private")
async def link_group(message: Message, db: Db, account, family, lang: str):
    if role(account) != "owner":
        await message.reply(t(lang, "link_denied"))
        return
    # Alerts are written in the language of the owner who linked the group.
    await db.update_family(family.family_id, notify_chat=message.chat.id, language=lang)
    await message.reply(t(lang, "link_done", name=escape(family.snapshot["name"])))


@router.my_chat_member(F.chat.type != "private")
async def added_to_group(event: ChatMemberUpdated, db: Db, families: Families):
    """Adding the bot to a group (the "Add bot to a group" button) links it when the family owner does it."""
    if event.new_chat_member.status in ("left", "kicked"):
        await db.unlink_chat(event.chat.id)
        return
    if event.old_chat_member.status not in ("left", "kicked"):
        return  # only a promotion or similar
    lang = await db.language(event.from_user.id) or DEFAULT
    account = await db.account(event.from_user.id)
    if role(account) != "owner":
        await families.bot.send_message(event.chat.id, t(lang, "link_denied"))
        return
    await db.update_family(account["family_id"], notify_chat=event.chat.id, language=lang)
    family = families.get(account["family_id"])
    name = escape(family.snapshot["name"]) if family.snapshot else ""
    await families.bot.send_message(event.chat.id, t(lang, "link_done", name=name))


@router.errors(ExceptionTypeFilter(UnknownIntent, OutdatedIntent))
async def stale_button(event: ErrorEvent, dialog_manager: DialogManager, lang: str = DEFAULT):
    # Buttons from a dialog that was reset or replaced: reopen the menu.
    if event.update.callback_query:
        await event.update.callback_query.answer(t(lang, "stale"))
    await dialog_manager.start(Menu.main, mode=StartMode.RESET_STACK, show_mode=ShowMode.SEND)


@router.errors(F.update.event_type == DIALOG_EVENT_NAME)
async def background_dialog_failed(event: ErrorEvent):
    update = event.update.event
    log.error(
        "Background dialog delivery failed for user %s in chat %s",
        update.from_user.id,
        update.chat.id,
        exc_info=event.exception,
    )
    return True


def create_dispatcher(db: Db, families: Families, logins: Logins, storage=None, **dialog_options) -> Dispatcher:
    dp = Dispatcher(storage=storage or PgStorage(db), db=db, families=families, logins=logins)
    dp.message.outer_middleware(GroupMigrationMiddleware())
    dp.include_routers(router, inline_router, menu_dialog, games_dialog, family_dialog, admin_dialog)
    setup_dialogs(dp, **dialog_options)
    for observer in (dp.message, dp.callback_query, dp.observers[DIALOG_EVENT_NAME]):
        observer.outer_middleware(AccountMiddleware())
        observer.middleware(AccessMiddleware())
    router.errors.middleware(AccountMiddleware())
    return dp


def background_finished(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        log.error("Background task %s failed", task.get_name(), exc_info=error)
    else:
        log.error("Background task %s stopped unexpectedly", task.get_name())


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = load_config()
    except ConfigError as error:
        raise SystemExit(str(error)) from None
    STATUS_PATH.unlink(missing_ok=True)
    charts.reset_cache()
    bot = Bot(settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        db = await Db.connect(settings.database_url, settings.secret_key)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as http:
                health = Health(settings.poll_interval_seconds)
                health.intervals["presence"] = PRESENCE_INTERVAL
                families = Families(db, http, bot, health=health)
                logins = Logins(families, bot)
                dp = create_dispatcher(db, families, logins)
                families.bot_username = (await bot.me()).username
                loops = {
                    "refresh": asyncio.create_task(
                        families.refresh_loop(settings.poll_interval_seconds), name="family-refresh"
                    ),
                    "presence": asyncio.create_task(families.presence_loop(), name="family-presence"),
                    "delivery": asyncio.create_task(families.delivery_loop(), name="family-delivery"),
                }
                monitor = asyncio.create_task(health.monitor(loops, db), name="health-monitor")
                for task in [*loops.values(), monitor]:
                    task.add_done_callback(background_finished)
                try:
                    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(), close_bot_session=False)
                finally:
                    for task in [*loops.values(), monitor]:
                        task.cancel()
                    await asyncio.gather(*loops.values(), monitor, return_exceptions=True)
                    STATUS_PATH.unlink(missing_ok=True)
                    await logins.close()
        finally:
            await db.pool.close()
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
