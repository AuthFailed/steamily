from html import escape

from aiogram import F
from aiogram.enums import ContentType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery
from aiogram_dialog import Dialog, DialogManager, StartMode, Window
from aiogram_dialog.api.entities import MediaAttachment
from aiogram_dialog.widgets.kbd import Button, Group, Select, Start, SwitchTo, Url
from aiogram_dialog.widgets.media import DynamicMedia
from aiogram_dialog.widgets.text import Format

from ..config import LOGIN_LOCATION
from ..family import Family, alert_settings, role
from ..i18n import LANGUAGES, Tr, t
from ..library import ALERTS
from ..states import FamilyView, Games, Menu
from ..ui import GREEN, RED, Header, back_home, look, premium

STATUS_KEYS = {"none": "status_none", "pending": "status_pending", "blocked": "status_blocked"}


async def language_getter(**kwargs):
    return {"languages": list(LANGUAGES.items())}


async def on_language(callback: CallbackQuery, widget, manager: DialogManager, language: str):
    await manager.middleware_data["db"].set_language(callback.from_user.id, language)
    manager.middleware_data["lang"] = language  # this update already loaded the old one
    # A first-time choice continues to the main menu; a change made from settings goes back there.
    await manager.switch_to(Menu.settings if manager.middleware_data["has_language"] else Menu.main)


async def main_getter(account, family: Family | None, lang: str, **kwargs):
    user_role = role(account)
    allowed = user_role in ("member", "owner")
    loaded = allowed and family.snapshot is not None
    return {
        "status": t(lang, STATUS_KEYS[user_role]) if user_role in STATUS_KEYS else "",
        "loaded": loaded,
        "loading": allowed and not loaded,
        "family_name": escape(family.snapshot["name"]) if loaded else "",
        "games": len(family.apps) if loaded else 0,
        "members": len(family.members) if loaded else 0,
        "is_owner": user_role == "owner",
        "has_account": account is not None,
        "needs_login": account is None or account["refresh_token"] is None,
        "relogin": account is not None and account["refresh_token"] is None,
    }


async def on_login(callback: CallbackQuery, button: Button, manager: DialogManager):
    try:
        login = await manager.middleware_data["logins"].start(callback.from_user.id, manager.bg())
    except Exception:
        login = {"login": "failed"}
    manager.dialog_data.update(login)
    await manager.switch_to(Menu.login)


async def cancel_login(callback: CallbackQuery, button: Button, manager: DialogManager):
    manager.middleware_data["logins"].cancel(callback.from_user.id)
    await manager.switch_to(Menu.main)


async def login_getter(dialog_manager: DialogManager, lang: str, **kwargs):
    data = dialog_manager.dialog_data
    waiting = data.get("login") == "waiting"
    return {
        "waiting": waiting,
        "qr": MediaAttachment(ContentType.PHOTO, path=data["qr"]) if waiting else None,
        "url": data.get("url", ""),
        "location": LOGIN_LOCATION or t(lang, "location_unknown"),
        "failure": f"{premium('login')} <b>{t(lang, 'login_title')}</b>\n\n"
        + t(lang, {"timeout": "login_timeout", "no_family": "login_no_family"}.get(data.get("login"), "login_failed")),
    }


async def settings_getter(account, db, **kwargs):
    is_owner = role(account) == "owner"
    return {
        "has_account": account is not None,
        "is_owner": is_owner,
        "requests_on": is_owner and await db.notify_requests(account["telegram_id"]),
    }


async def toggle_requests(callback: CallbackQuery, button: Button, manager: DialogManager):
    db = manager.middleware_data["db"]
    await db.set_notify_requests(callback.from_user.id, not await db.notify_requests(callback.from_user.id))


async def group_alerts_getter(account, db, families, bot, lang: str, **kwargs):
    row = await db.family(account["family_id"])
    title = None
    if row["notify_chat"]:
        try:
            title = (await bot.get_chat(row["notify_chat"])).title
        except TelegramAPIError:
            title = str(row["notify_chat"])
    alerts = alert_settings(row)
    return {
        "linked": title is not None,
        "group": escape(title or ""),
        "add_url": f"https://t.me/{families.bot_username}?startgroup=link",
        "alert_items": [(kind, ("✅ " if alerts[kind] else "⬜ ") + t(lang, f"alerts_{kind}")) for kind in ALERTS],
    }


async def toggle_alert(callback: CallbackQuery, widget, manager: DialogManager, kind: str):
    db = manager.middleware_data["db"]
    family_id = manager.middleware_data["account"]["family_id"]
    alerts = alert_settings(await db.family(family_id))
    await db.update_family(family_id, alerts={**alerts, kind: not alerts[kind]})


async def unlink_group(callback: CallbackQuery, button: Button, manager: DialogManager):
    await manager.middleware_data["db"].update_family(manager.middleware_data["account"]["family_id"], notify_chat=None)


async def logout_getter(account, db, **kwargs):
    others = []
    if role(account) == "owner":
        others = [
            a
            for a in await db.family_accounts(account["family_id"])
            if a["telegram_id"] != account["telegram_id"] and a["status"] != "blocked"
        ]
    return {
        "owner_handover": role(account) == "owner" and bool(others),
        "owner_alone": role(account) == "owner" and not others,
    }


async def on_logout(callback: CallbackQuery, button: Button, manager: DialogManager):
    families = manager.middleware_data["families"]
    family = manager.middleware_data["family"]
    new_owner = await families.leave(callback.from_user.id)
    if new_owner is not None:
        lang = await families.db.language(new_owner)
        name = escape(family.snapshot["name"]) if family and family.snapshot else ""
        await families.bot.send_message(new_owner, t(lang, "new_owner", name=name))
        await manager.bg(user_id=new_owner, chat_id=new_owner).start(Menu.main, mode=StartMode.RESET_STACK)
    # Logout also forgets the language, so the user starts over with the language choice.
    manager.middleware_data.update(account=None, family=None, has_language=False)
    await callback.answer(t(manager.middleware_data["lang"], "logged_out"))
    await manager.start(Menu.language, mode=StartMode.RESET_STACK)


menu_dialog = Dialog(
    Window(
        Header("language", "language"),
        Select(
            Format("{item[1]}"),
            id="language",
            item_id_getter=lambda lang: lang[0],
            items="languages",
            on_click=on_language,
        ),
        getter=language_getter,
        state=Menu.language,
    ),
    Window(
        Header("family", "main"),
        Format("{status}", when="status"),
        Tr("family_summary", when="loaded"),
        Tr("loading", when="loading"),
        Tr("relogin", when="relogin"),
        Start(Tr("btn_library"), id="library", state=Games.list, when="loaded", style=look("library", GREEN)),
        Button(Tr("btn_login"), id="login", on_click=on_login, when="needs_login", style=look("login", GREEN)),
        Start(Tr("btn_family"), id="family", state=FamilyView.info, when="loaded", style=look("family")),
        SwitchTo(Tr("btn_settings"), id="settings", state=Menu.settings, style=look("settings")),
        getter=main_getter,
        state=Menu.main,
    ),
    Window(
        DynamicMedia("qr", when="waiting"),
        Header("login", "login", when="waiting"),
        Format("{failure}", when=~F["waiting"]),
        Url(Tr("btn_open_app"), Format("{url}"), id="open_app", when="waiting", style=look("login")),
        Button(Tr("btn_retry"), id="retry", on_click=on_login, when=~F["waiting"], style=look("retry", GREEN)),
        Button(Tr("btn_cancel"), id="cancel_login", on_click=cancel_login, style=look("cancel")),
        getter=login_getter,
        state=Menu.login,
    ),
    Window(
        Header("settings", "settings"),
        Button(
            Tr("btn_requests_on"),
            id="requests_off",
            on_click=toggle_requests,
            when=F["is_owner"] & F["requests_on"],
            style=look("notifications", GREEN),
        ),
        Button(
            Tr("btn_requests_off"),
            id="requests_on",
            on_click=toggle_requests,
            when=F["is_owner"] & ~F["requests_on"],
            style=look("notifications"),
        ),
        SwitchTo(
            Tr("btn_group_alerts"),
            id="group_alerts",
            state=Menu.group_alerts,
            when="is_owner",
            style=look("notifications"),
        ),
        SwitchTo(Tr("btn_language"), id="to_language", state=Menu.language, style=look("language")),
        SwitchTo(Tr("btn_logout"), id="logout", state=Menu.logout, when="has_account", style=look("logout", RED)),
        SwitchTo(Tr("btn_back"), id="settings_back", state=Menu.main),
        getter=settings_getter,
        state=Menu.settings,
    ),
    Window(
        Header("notifications", "group_alerts"),
        Tr("group_linked", when="linked"),
        Tr("group_not_linked", when=~F["linked"]),
        Url(Tr("btn_add_to_group"), Format("{add_url}"), id="add_to_group", style=look(color=GREEN)),
        Group(
            Select(
                Format("{item[1]}"),
                id="alert",
                item_id_getter=lambda a: a[0],
                items="alert_items",
                on_click=toggle_alert,
            ),
            width=1,
            when="linked",
        ),
        Button(Tr("btn_unlink"), id="unlink", on_click=unlink_group, when="linked"),
        back_home(SwitchTo(Tr("btn_back"), id="alerts_back", state=Menu.settings)),
        getter=group_alerts_getter,
        state=Menu.group_alerts,
    ),
    Window(
        Header("logout", "logout"),
        Tr("logout_owner_handover", when="owner_handover"),
        Tr("logout_owner_alone", when="owner_alone"),
        Button(Tr("btn_logout_yes"), id="confirm_logout", on_click=on_logout, style=look("logout", RED)),
        SwitchTo(Tr("btn_cancel"), id="cancel_logout", state=Menu.settings, style=look("cancel")),
        getter=logout_getter,
        state=Menu.logout,
    ),
)
