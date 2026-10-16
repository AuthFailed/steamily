from aiogram.types import CallbackQuery, Message
from aiogram_dialog import DialogManager, StartMode

from ..family import role
from ..i18n import language_of, t
from ..states import Menu


async def deny_action(manager: DialogManager, reason: str = "owner_action_denied") -> None:
    text = t(language_of(manager), reason)
    if isinstance(manager.event, CallbackQuery):
        await manager.event.answer(text, show_alert=True)
    elif isinstance(manager.event, Message):
        await manager.event.answer(text)
    await manager.start(Menu.main, mode=StartMode.RESET_STACK)


async def current_owner(manager: DialogManager):
    account = await manager.middleware_data["db"].account(manager.event.from_user.id)
    manager.middleware_data.update(
        account=account,
        family=manager.middleware_data["families"].get(account["family_id"]) if account else None,
    )
    if role(account) != "owner" or account["family_id"] != manager.dialog_data.get("family_id"):
        return None
    return account


async def require_owner(manager: DialogManager):
    account = await current_owner(manager)
    if account is None:
        await deny_action(manager)
    return account
