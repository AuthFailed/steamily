import asyncio
import logging
from html import escape

import aiohttp
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, ExceptionTypeFilter
from aiogram.types import CallbackQuery, ChatMemberUpdated, ErrorEvent, Message
from aiogram_dialog import DIALOG_EVENT_NAME, DialogManager, ShowMode, StartMode, setup_dialogs
from aiogram_dialog.api.exceptions import OutdatedIntent, UnknownIntent

from .config import poll_seconds, required
from .db import Db, PgStorage
from .dialogs.admin import admin_dialog
from .dialogs.family import family_dialog
from .dialogs.games import games_dialog
from .dialogs.menu import menu_dialog
from .family import Families, role
from .i18n import DEFAULT, t
from .inline import router as inline_router
from .login import Logins
from .states import Menu

router = Router()


class AccountMiddleware(BaseMiddleware):
    """Loads the sender's account and family. In groups only /link gets through."""

    async def __call__(self, handler, event, data: dict):
        # Messages, button presses, and aiogram-dialog's own background updates (which carry .chat).
        chat = (
            event.message.chat if isinstance(event, CallbackQuery) and event.message else getattr(event, "chat", None)
        )
        if chat is None or event.from_user is None:
            return None
        if chat.type != "private" and not (isinstance(event, Message) and (event.text or "").startswith("/link")):
            return None
        language = await data["db"].language(event.from_user.id)
        data["has_language"] = language is not None
        data["lang"] = language or ("ru" if (event.from_user.language_code or "").startswith("ru") else DEFAULT)
        account = await data["db"].account(event.from_user.id)
        user = event.from_user
        if account and (account["tg_username"], account["tg_name"]) != (user.username, user.full_name):
            await data["db"].set_telegram_profile(user.id, user.username, user.full_name)  # kept current for links
        data["account"] = account
        data["family"] = data["families"].get(account["family_id"]) if account else None
        return await handler(event, data)


@router.message(CommandStart(), F.chat.type == "private")
async def start(message: Message, dialog_manager: DialogManager, logins: Logins, has_language: bool):
    logins.cancel(message.from_user.id)
    await dialog_manager.start(Menu.main if has_language else Menu.language, mode=StartMode.RESET_STACK)


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


def create_dispatcher(db: Db, families: Families, logins: Logins, storage=None, **dialog_options) -> Dispatcher:
    dp = Dispatcher(storage=storage or PgStorage(db), db=db, families=families, logins=logins)
    dp.include_routers(router, inline_router, menu_dialog, games_dialog, family_dialog, admin_dialog)
    setup_dialogs(dp, **dialog_options)
    for observer in (dp.message, dp.callback_query, dp.observers[DIALOG_EVENT_NAME]):
        observer.outer_middleware(AccountMiddleware())
    return dp


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    bot = Bot(required("TELEGRAM_BOT_TOKEN"), default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    db = await Db.connect(required("DATABASE_URL"), required("SECRET_KEY"))
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as http:
        families = Families(db, http, bot)
        dp = create_dispatcher(db, families, Logins(families, bot))
        families.bot_username = (await bot.me()).username
        loops = [
            asyncio.create_task(families.refresh_loop(poll_seconds())),
            asyncio.create_task(families.presence_loop()),
        ]
        try:
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        finally:
            for loop in loops:
                loop.cancel()
            await db.pool.close()


if __name__ == "__main__":
    asyncio.run(main())
