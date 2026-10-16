import asyncio
import logging
import random
import time
from html import escape

import aiohttp
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from .db import Db
from .library import ALERTS, family_events, format_event, format_notification, presence_events, reconcile
from .steam import (
    fetch_family_snapshot,
    fetch_presence,
    fetch_user_country,
    member_stats,
    profile_url,
    update_store_cache,
)
from .steam_auth import SteamAuth, TokenRejected

log = logging.getLogger(__name__)
STATS_MAX_AGE = 10 * 60
PRESENCE_INTERVAL = 60


DEFAULT_COUNTRY = "US"


class Family:
    """Latest snapshot of one Steam family; prices come from the store cache of each viewer's country."""

    def __init__(self, family_id: str, stores: dict[str, dict] | None = None):
        self.family_id = family_id
        self.snapshot: dict | None = None
        self.stores = stores if stores is not None else {}

    @property
    def apps(self) -> list[dict]:
        return self.snapshot["apps"] if self.snapshot else []

    @property
    def members(self) -> list[str]:
        return self.snapshot["members"] if self.snapshot else []

    def name(self, steam_id: str) -> str:
        return (self.snapshot or {}).get("names", {}).get(steam_id, steam_id)

    def store(self, country: str | None) -> dict:
        return self.stores.get(country or DEFAULT_COUNTRY) or self.stores.get(DEFAULT_COUNTRY) or {}


class Families:
    def __init__(self, db: Db, http: aiohttp.ClientSession, bot: Bot):
        self.db, self.http, self.bot = db, http, bot
        self.items: dict[str, Family] = {}
        self.auths: dict[int, SteamAuth] = {}
        self.stores: dict[str, dict] = {}
        self.stats_cache: dict[str, tuple[float, dict]] = {}
        self.presence: dict[str, dict] = {}  # last seen online/playing state per family
        self.bot_username = ""

    def get(self, family_id: str) -> Family:
        return self.items.setdefault(family_id, Family(family_id, self.stores))

    def auth(self, telegram_id: int, refresh_token: str, steam_id: str, last_renewal: float = 0.0) -> SteamAuth:
        auth = self.auths.get(telegram_id)
        if not auth or auth.refresh_token != refresh_token:

            async def save(token, renewed_at):
                await self.db.set_refresh_token(telegram_id, token, renewed_at)

            auth = self.auths[telegram_id] = SteamAuth(self.http, refresh_token, steam_id, save, last_renewal)
        return auth

    async def register(self, telegram_id: int, refresh_token: str, steam_id: str) -> Family:
        """Called after a successful Steam login. Raises RuntimeError if the account has no family."""
        account = await self.db.account(telegram_id)
        last_renewal = account["token_renewed_at"] if account and account["steam_id"] == steam_id else 0.0
        auth = SteamAuth(self.http, refresh_token, steam_id, last_renewal=last_renewal)
        access_token = await auth.get_access_token()
        snapshot = await fetch_family_snapshot(self.http, access_token)
        country = await fetch_user_country(self.http, access_token, steam_id)
        family_id = snapshot["family_id"]
        await self.db.ensure_family(family_id, snapshot["name"])
        await self.db.save_account(telegram_id, steam_id, family_id, auth.refresh_token, country, auth.last_renewal)
        self.auths.pop(telegram_id, None)
        if (await self.db.family(family_id))["owner_telegram_id"] is None:  # nobody owns it: the next to register does
            await self.make_owner(family_id, telegram_id)
        family = self.get(family_id)
        family.snapshot = snapshot
        try:  # the login itself succeeded; store details can catch up on the next poll
            await self.refresh(family_id)
        except Exception:
            log.exception("Family %s refresh after login failed", family_id)
        return family

    async def make_owner(self, family_id: str, telegram_id: int | None) -> None:
        await self.db.update_family(family_id, owner_telegram_id=telegram_id)
        if telegram_id is not None:
            await self.db.set_status(telegram_id, "approved")

    async def leave(self, telegram_id: int) -> int | None:
        """Logout. An owner's role goes to a random other registered member (approved ones first), who is
        returned; with nobody else registered the family has no owner until the next registration."""
        account = await self.db.account(telegram_id)
        new_owner = None
        if account and account["owner_telegram_id"] == telegram_id:
            others = [
                a
                for a in await self.db.family_accounts(account["family_id"])
                if a["telegram_id"] != telegram_id and a["status"] != "blocked"
            ]
            approved = [a for a in others if a["status"] == "approved"]
            if others:
                new_owner = random.choice(approved or others)["telegram_id"]
            await self.make_owner(account["family_id"], new_owner)
        await self.db.delete_user(telegram_id)
        self.auths.pop(telegram_id, None)
        return new_owner

    async def owner_token(self, account) -> str:
        """Access token of the acting user's own Steam account, for changes to the family."""
        auth = self.auth(
            account["telegram_id"], self.db.refresh_token(account), account["steam_id"], account["token_renewed_at"]
        )
        return await auth.get_access_token()

    async def any_token(self, family_id: str) -> str | None:
        for account in await self.db.family_accounts(family_id):
            if account["refresh_token"] and account["status"] != "blocked":
                try:
                    return await self.owner_token(account)
                except TokenRejected:
                    continue
        return None

    async def member_stats(self, family_id: str, steam_id: str) -> dict:
        """Live Steam numbers for a member card, cached for a few minutes."""
        cached = self.stats_cache.get(steam_id)
        if cached and time.monotonic() - cached[0] < STATS_MAX_AGE:
            return cached[1]
        token = await self.any_token(family_id)
        stats = await member_stats(self.http, token, steam_id) if token else {}
        self.stats_cache[steam_id] = (time.monotonic(), stats)
        return stats

    async def refresh(self, family_id: str) -> None:
        row = await self.db.family(family_id)
        snapshot = await self._fetch(family_id)
        if snapshot is None:
            return
        family = self.get(family_id)
        previous, family.snapshot = family.snapshot, snapshot
        self.presence.setdefault(family_id, snapshot.get("players", {}))

        events, state = reconcile(row["state"], snapshot)
        alerts = alert_settings(row)
        if alerts["games"]:
            for event in events:
                await self.send_alert(row, format_notification(event, snapshot["names"], row["language"]))
        if previous:  # ponytail: compared in memory, so changes while the bot was down are not announced
            for event in family_events(previous, snapshot):
                if alerts[event["kind"]]:
                    await self.send_alert(
                        row, format_event(event, snapshot["names"], snapshot["apps"], row["language"])
                    )
        await self.db.update_family(family_id, state=state, name=snapshot["name"])

        # Prices in the currency of every member who uses the bot.
        accounts = await self.db.family_accounts(family_id)
        if row["owner_telegram_id"] is None and accounts:  # families registered before owners were stored
            approved = [a for a in accounts if a["status"] == "approved"]
            await self.make_owner(family_id, (approved or accounts)[0]["telegram_id"])
        for account in accounts:
            if account["country"] is None and account["refresh_token"] and account["status"] != "blocked":
                await self._fill_country(account)
        accounts = await self.db.family_accounts(family_id)
        appids = [a["appid"] for a in snapshot["apps"]]
        for country in sorted({a["country"] or DEFAULT_COUNTRY for a in accounts} | {DEFAULT_COUNTRY}):
            if country not in self.stores:
                self.stores[country] = await self.db.store_cache(country)
            if await update_store_cache(self.http, self.stores[country], appids, country):
                await self.db.save_store_cache(country, self.stores[country])

    async def _fill_country(self, account) -> None:
        """For accounts that logged in before countries were stored."""
        auth = self.auth(
            account["telegram_id"], self.db.refresh_token(account), account["steam_id"], account["token_renewed_at"]
        )
        try:
            country = await fetch_user_country(self.http, await auth.get_access_token(), account["steam_id"])
        except TokenRejected:
            return  # _fetch handles rejected tokens
        if country:
            await self.db.set_country(account["telegram_id"], country)

    async def _fetch(self, family_id: str) -> dict | None:
        """Reads the family with any member's token; tokens Steam rejects are dropped so the user re-logs in."""
        for account in await self.db.family_accounts(family_id):
            if account["status"] == "blocked" or not account["refresh_token"]:
                continue
            auth = self.auth(
                account["telegram_id"], self.db.refresh_token(account), account["steam_id"], account["token_renewed_at"]
            )
            try:
                token = await auth.get_access_token()
            except TokenRejected as error:
                log.warning("Steam rejected account %s, it must log in again: %s", account["telegram_id"], error)
                await self.db.set_refresh_token(account["telegram_id"], None)
                continue
            try:
                snapshot = await fetch_family_snapshot(self.http, token)
            except RuntimeError as error:  # e.g. this member left every family
                log.warning("Account %s cannot read family %s: %s", account["telegram_id"], family_id, error)
                continue
            if snapshot["family_id"] == family_id:  # a member may have moved to another family
                return snapshot
        log.warning("Family %s has no working Steam login", family_id)
        return None

    async def send_alert(self, row, text: str) -> None:
        if not row["notify_chat"]:
            return
        try:
            await self.bot.send_message(row["notify_chat"], text, parse_mode=None)
        except TelegramAPIError as error:  # e.g. the bot was removed from the group
            log.warning("Alert to family %s group failed: %s", row["family_id"], error)

    async def check_presence(self, family_id: str) -> None:
        """Online and now-playing alerts; Steam is asked once for all members of the family."""
        row = await self.db.family(family_id)
        alerts = alert_settings(row)
        family = self.items.get(family_id)
        if not row["notify_chat"] or not (alerts["online"] or alerts["playing"]) or not family or not family.snapshot:
            return
        token = await self.any_token(family_id)
        if token is None:
            return
        presence = await fetch_presence(self.http, token, family.members)
        for event in presence_events(self.presence.get(family_id, {}), presence):
            if alerts[event["kind"]]:
                await self.send_alert(row, format_event(event, family.snapshot["names"], family.apps, row["language"]))
        self.presence[family_id] = presence
        for steam_id, now in presence.items():  # keeps the 🟢 marks in menus fresh too
            family.snapshot.setdefault("players", {}).setdefault(steam_id, {}).update(now)

    async def presence_loop(self, interval: int = PRESENCE_INTERVAL) -> None:
        while True:
            await asyncio.sleep(interval)
            for row in await self.db.families_with_accounts():
                try:
                    await self.check_presence(row["family_id"])
                except Exception:
                    log.exception("Family %s presence check failed", row["family_id"])

    async def refresh_loop(self, interval: int) -> None:
        while True:
            for row in await self.db.families_with_accounts():
                try:
                    await self.refresh(row["family_id"])
                except Exception:
                    log.exception("Family %s check failed", row["family_id"])
            await asyncio.sleep(interval)


def person_link(family: Family, steam_id: str, accounts: dict) -> str:
    """Steam profile link, plus the member's Telegram when they use the bot. accounts: steam_id -> account row."""
    url = profile_url(steam_id, (family.snapshot or {}).get("players", {}))
    steam = f'<a href="{url}">{escape(family.name(steam_id))}</a>'
    account = accounts.get(steam_id)
    if account is None:
        return steam
    if account["tg_username"]:
        telegram = f'<a href="https://t.me/{account["tg_username"]}">@{escape(account["tg_username"])}</a>'
    else:
        telegram = f'<a href="tg://user?id={account["telegram_id"]}">{escape(account["tg_name"] or "Telegram")}</a>'
    return f"{steam} · {telegram}"


async def accounts_by_steam(db: Db, family_id: str) -> dict:
    return {a["steam_id"]: a for a in await db.family_accounts(family_id)}


def alert_settings(row) -> dict:
    return {**ALERTS, **(row["alerts"] or {})}


def role(account) -> str:
    """none | blocked | pending | member | owner"""
    if account is None:
        return "none"
    if account["status"] == "blocked":
        return "blocked"
    if account["owner_telegram_id"] == account["telegram_id"]:
        return "owner"
    return "member" if account["status"] == "approved" else "pending"
