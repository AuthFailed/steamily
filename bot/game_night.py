"""Game-night polls stay in the linked group; Steam never reserves copies."""

from datetime import UTC, datetime
from html import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .game_night_store import GameNightStore
from .i18n import TEXTS, sharing_warning, t
from .library import shareable_copies

router = Router()


def parse_proposal(text: str, now: datetime | None = None) -> tuple[datetime, str, list[int]]:
    parts = text.split()
    if not 5 <= len(parts) <= 8:
        raise ValueError("Expected date, time, timezone and two to five game IDs")
    date, time, timezone, *games = parts
    try:
        zone = ZoneInfo(timezone)
        local = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
        starts_at = local.replace(tzinfo=zone)
        # Reject skipped/repeated clock times rather than silently picking a DST offset.
        if starts_at.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != local:
            raise ValueError("Nonexistent local time")
        if starts_at.utcoffset() != starts_at.replace(fold=1).utcoffset():
            raise ValueError("Ambiguous local time; use UTC")
        appids = [int(game) for game in games]
    except (ZoneInfoNotFoundError, OverflowError) as error:
        raise ValueError("Invalid time or game ID") from error
    if len(set(appids)) != len(appids) or any(not 0 < appid < 2**63 for appid in appids):
        raise ValueError("Use distinct positive game IDs")
    if starts_at <= (now or datetime.now(UTC)):
        raise ValueError("The game night must be in the future")
    return starts_at, timezone, appids


def render_event(event: dict, lang: str) -> tuple[str, InlineKeyboardMarkup | None]:
    snapshot = event["snapshot"]
    apps = {app["appid"]: app for app in snapshot.get("apps", [])}
    participants = event["participants"]
    going = {person["steam_id"] for person in participants if person["rsvp"] == "going"}
    votes = {appid: sum(person["vote"] == appid for person in participants) for appid in event["appids"]}
    chosen = event.get("chosen_appid")
    local = event["starts_at"].astimezone(ZoneInfo(event["timezone"]))
    lines = [
        t(lang, "gn_title", state=t(lang, "gn_" + event["state"])),
        f"{local:%Y-%m-%d %H:%M %z} · {escape(event['timezone'])}",
        t(lang, "gn_chosen", game=escape(apps.get(chosen, {}).get("name", str(chosen))[:120]))
        if chosen is not None
        else t(lang, "gn_choose"),
    ]
    buttons = []
    for appid in event["appids"]:
        app = apps.get(appid, {})
        name = app.get("name", t(lang, "gn_game_gone", appid=appid))[:120]
        lines.append(f'\n<a href="https://store.steampowered.com/app/{appid}/">{escape(name)}</a> · {votes[appid]} 🗳')
        owners = set(app.get("owners", [])) & set(snapshot.get("members", []))
        reason = app.get("exclude_reason")
        known = "owners" in app and (reason == 0 or f"sharing_reason_{reason}" in TEXTS["en"])
        eligible = (
            (shareable_copies({**app, "owners": owners}) if reason == 0 else len(going & owners)) if known else None
        )
        lines.append(t(lang, "gn_copies", required=len(going), eligible=eligible if known else t(lang, "gn_unknown")))
        if eligible is not None and eligible < len(going):
            lines.append(t(lang, "gn_shortage"))
        if not known:
            lines.append(t(lang, "gn_sharing_unknown"))
        if reason:
            lines.append(sharing_warning(lang, reason))
        buttons.append([InlineKeyboardButton(text=f"🗳 {name[:55]}", callback_data=f"gn:{event['id']}:vote:{appid}")])
    for response in ("going", "maybe", "cannot"):
        names = [
            escape(snapshot.get("names", {}).get(person["steam_id"], person["steam_id"])[:80])
            for person in participants
            if person["rsvp"] == response
        ]
        lines.append(f"\n{t(lang, 'gn_' + response)} ({len(names)}): {', '.join(names) or '—'}")
    lines.extend(["\n" + t(lang, "gn_availability"), t(lang, "gn_ties")])
    if event["state"] != "open":
        return "\n".join(lines), None
    buttons.append(
        [
            InlineKeyboardButton(text=t(lang, "gn_" + response), callback_data=f"gn:{event['id']}:rsvp:{response}")
            for response in ("going", "maybe", "cannot")
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(text=t(lang, "gn_" + action), callback_data=f"gn:{event['id']}:{action}")
            for action in ("close", "cancel")
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command("gamenight"), flags={"game_night": True})
async def propose_night(message: Message, command: CommandObject, db, account, family, lang: str):
    if message.chat.type not in {"group", "supergroup"}:
        await message.answer(t(lang, "gn_group_only"))
        return
    if not account or not family:
        await message.reply(t(lang, "gn_denied"))
        return
    try:
        starts_at, timezone, appids = parse_proposal(command.args or "")
    except ValueError:
        await message.reply(t(lang, "gn_help"))
        return
    async with family.lock:
        store = GameNightStore(db)
        event = await store.create(
            account["family_id"],
            message.from_user.id,
            account["steam_id"],
            message.chat.id,
            starts_at,
            timezone,
            appids,
        )
        if event is None:
            await message.reply(t(lang, "gn_denied"))
            return
        text, keyboard = render_event(event, event.get("language", lang))
        sent = await message.answer(text, reply_markup=keyboard)
        await store.attach(event["id"], sent.message_id)


@router.callback_query(F.data.startswith("gn:"), flags={"game_night": True})
async def event_action(callback: CallbackQuery, db, account, family, lang: str):
    if not isinstance(callback.message, Message) or not account or not family:
        await callback.answer(t(lang, "gn_denied"), show_alert=True)
        return
    try:
        _, event_id, action, *values = callback.data.split(":")
        if not 0 < int(event_id) < 2**63 or len(values) > 1:
            raise ValueError("Invalid event action")
        value = int(values[0]) if action == "vote" and values else values[0] if values else None
        if action in {"close", "cancel"} and values:
            raise ValueError("Unexpected action argument")
    except (ValueError, IndexError):
        await callback.answer(t(lang, "gn_denied"), show_alert=True)
        return
    async with family.lock:
        store = GameNightStore(db)
        event = await store.act(
            int(event_id),
            callback.from_user.id,
            account["steam_id"],
            callback.message.chat.id,
            callback.message.message_id,
            action,
            value,
        )
        if event is None:
            await callback.answer(t(lang, "gn_denied"), show_alert=True)
            return
        text, keyboard = render_event(event, event.get("language", lang))
        try:
            await callback.message.edit_text(text, reply_markup=keyboard)
        except TelegramBadRequest as error:
            if "message is not modified" not in error.message.lower():
                raise
        await callback.answer()
