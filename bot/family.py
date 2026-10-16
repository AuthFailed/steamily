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
PRESENCE_INTERVAL = 300


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
        self.auth_generations: dict[int, int] = {}
        self.account_locks: dict[int, asyncio.Lock] = {}
        self.stores: dict[str, dict] = {}
        self.stats_cache: dict[str, tuple[float, dict]] = {}
        self.presence: dict[str, dict] = {}  # last seen online/playing state per family
        self.read_backoff: dict[str, tuple[int, float]] = {}
        self.bot_username = ""

    def get(self, family_id: str) -> Family:
        return self.items.setdefault(family_id, Family(family_id, self.stores))

    def auth(
        self, telegram_id: int, refresh_token: str, steam_id: str, last_renewal: float = 0.0, *, generation: int = 0
    ) -> SteamAuth:
        auth = self.auths.get(telegram_id)
        if (
            not auth
            or self.auth_generations.get(telegram_id) != generation
            or auth.steam_id != steam_id
            or (
                auth.refresh_token != refresh_token
                and auth._pending_renewal is None
                and last_renewal > auth.last_renewal
            )
        ):

            async def save(token, renewed_at):
                if not await self.db.set_refresh_token(telegram_id, token, renewed_at, expected_generation=generation):
                    raise TokenRejected("Steam login was replaced")

            auth = self.auths[telegram_id] = SteamAuth(self.http, refresh_token, steam_id, save, last_renewal)
            self.auth_generations[telegram_id] = generation
        return auth

    async def register(
        self, telegram_id: int, refresh_token: str, steam_id: str, *, on_refresh=None, on_registered=None
    ) -> Family:
        """Called after a successful Steam login. Raises RuntimeError if the account has no family."""
        async with self.account_locks.setdefault(telegram_id, asyncio.Lock()):
            account = await self.db.account(telegram_id)
            last_renewal = account["token_renewed_at"] if account and account["steam_id"] == steam_id else 0.0

            async def remember(token, renewed_at):
                await on_refresh(token)

            auth = SteamAuth(
                self.http, refresh_token, steam_id, remember if on_refresh else None, last_renewal=last_renewal
            )
            access_token = await auth.get_access_token()
            snapshot = await fetch_family_snapshot(self.http, access_token)
            country = await fetch_user_country(self.http, access_token, steam_id)
            family_id = snapshot["family_id"]
            await self.db.ensure_family(family_id, snapshot["name"])
            await self.db.save_account(
                telegram_id, steam_id, family_id, auth.refresh_token, country, auth.last_renewal, claim_owner=True
            )
            if on_registered:
                on_registered()
            self.auths.pop(telegram_id, None)
            self.auth_generations.pop(telegram_id, None)
            self.read_backoff.pop(family_id, None)
            family = self.get(family_id)
            family.snapshot = snapshot
        try:  # the login itself succeeded; store details can catch up on the next poll
            await self.refresh(family_id)
        except Exception as error:
            log.warning("Family %s refresh after login failed (%s)", family_id, type(error).__name__)
        return family

    async def make_owner(self, family_id: str, telegram_id: int | None) -> None:
        await self.db.update_family(family_id, owner_telegram_id=telegram_id)
        if telegram_id is not None:
            await self.db.set_status(telegram_id, "approved")

    async def leave(self, telegram_id: int) -> int | None:
        """Logout. An owner's role goes to a random other registered member (approved ones first), who is
        returned; with nobody else registered the family has no owner until the next registration."""
        async with self.account_locks.setdefault(telegram_id, asyncio.Lock()):
            return await self._leave(telegram_id)

    async def _leave(self, telegram_id: int) -> int | None:
        account = await self.db.account(telegram_id)
        if account and account["refresh_token"]:
            auth = self.auth(
                telegram_id,
                self.db.refresh_token(account),
                account["steam_id"],
                account["token_renewed_at"],
                generation=account["token_generation"],
            )
            await auth.revoke()
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
        self.auth_generations.pop(telegram_id, None)
        if account and (await self.db.family(account["family_id"]))["reader_telegram_id"] == telegram_id:
            await self.db.update_family(account["family_id"], reader_telegram_id=None)
            self.read_backoff.pop(account["family_id"], None)
        return new_owner

    async def owner_token(self, account) -> str:
        """Access token of the acting user's own Steam account, for changes to the family."""
        async with self.account_locks.setdefault(account["telegram_id"], asyncio.Lock()):
            current = await self.db.account(account["telegram_id"])
            if (
                current is None
                or not current["refresh_token"]
                or any(current[field] != account[field] for field in ("token_generation", "steam_id", "family_id"))
            ):
                raise TokenRejected("Steam login changed; reload the menu")
            auth = self.auth(
                current["telegram_id"],
                self.db.refresh_token(current),
                current["steam_id"],
                current["token_renewed_at"],
                generation=current["token_generation"],
            )
            try:
                return await auth.get_access_token()
            except TokenRejected:
                await self._reject_token(current)
                raise

    async def _reject_token(self, account) -> None:
        telegram_id, generation = account["telegram_id"], account["token_generation"]
        cleared = await self.db.set_refresh_token(telegram_id, None, expected_generation=generation)
        if self.auth_generations.get(telegram_id) == generation:
            self.auths.pop(telegram_id, None)
            self.auth_generations.pop(telegram_id, None)
        if cleared:
            log.warning("Steam rejected account %s; login is required", telegram_id)

    async def _reader(self, family_id: str):
        row = await self.db.family(family_id)
        accounts = [
            account
            for account in await self.db.family_accounts(family_id)
            if account["status"] == "approved" and account["refresh_token"]
        ]
        reader = row["reader_telegram_id"]
        if reader is not None:
            return next((account for account in accounts if account["telegram_id"] == reader), None)
        if not accounts:
            return None
        account = next((a for a in accounts if a["telegram_id"] == row["owner_telegram_id"]), accounts[0])
        await self.db.update_family(family_id, reader_telegram_id=account["telegram_id"])
        return account

    def _read_ready(self, family_id: str) -> bool:
        return time.monotonic() >= self.read_backoff.get(family_id, (0, 0.0))[1]

    def _read_failed(self, family_id: str) -> None:
        failures = self.read_backoff.get(family_id, (0, 0.0))[0]
        delay = min(60 * 2 ** min(failures, 6), 3600)
        self.read_backoff[family_id] = (failures + 1, time.monotonic() + delay)

    async def any_token(self, family_id: str) -> str | None:
        if not self._read_ready(family_id):
            return None
        account = await self._reader(family_id)
        if account is None:
            return None
        try:
            return await self.owner_token(account)
        except TokenRejected:
            return None

    async def member_stats(self, family_id: str, steam_id: str) -> dict:
        """Live Steam numbers for a member card, cached for a few minutes."""
        cached = self.stats_cache.get(steam_id)
        if cached and time.monotonic() - cached[0] < STATS_MAX_AGE:
            return cached[1]
        if not self._read_ready(family_id):
            return {}
        account = await self._reader(family_id)
        if account is None:
            return {}
        try:
            token = await self.owner_token(account)
            stats = await member_stats(self.http, token, steam_id)
        except TokenRejected:
            await self._reject_token(account)
            return {}
        except (aiohttp.ClientError, TimeoutError, OSError, RuntimeError):
            self._read_failed(family_id)
            raise
        self.read_backoff.pop(family_id, None)
        self.stats_cache[steam_id] = (time.monotonic(), stats)
        return stats

    async def refresh(self, family_id: str) -> None:
        if not self._read_ready(family_id):
            return
        try:
            await self._refresh(family_id)
        except (aiohttp.ClientError, TimeoutError, OSError, RuntimeError):
            self._read_failed(family_id)
            raise
        else:
            self.read_backoff.pop(family_id, None)

    async def _refresh(self, family_id: str) -> None:
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
        reader = await self._reader(family_id)
        if reader and reader["country"] is None:
            await self._fill_country(reader)
        accounts = await self.db.family_accounts(family_id)
        appids = [a["appid"] for a in snapshot["apps"]]
        for country in sorted({a["country"] or DEFAULT_COUNTRY for a in accounts} | {DEFAULT_COUNTRY}):
            if country not in self.stores:
                self.stores[country] = await self.db.store_cache(country)
            if await update_store_cache(self.http, self.stores[country], appids, country):
                await self.db.save_store_cache(country, self.stores[country])

    async def _fill_country(self, account) -> None:
        """For accounts that logged in before countries were stored."""
        try:
            token = await self.owner_token(account)
        except TokenRejected:
            return
        try:
            country = await fetch_user_country(self.http, token, account["steam_id"])
        except TokenRejected:
            await self._reject_token(account)
            return
        if country:
            await self.db.set_country(account["telegram_id"], country)

    async def _fetch(self, family_id: str) -> dict | None:
        """Read with the designated session; a rejected login waits for that user to log in again."""
        account = await self._reader(family_id)
        if account is None:
            return None
        try:
            token = await self.owner_token(account)
        except TokenRejected:
            return None
        try:
            snapshot = await fetch_family_snapshot(self.http, token)
        except TokenRejected:
            await self._reject_token(account)
            return None
        if snapshot["family_id"] == family_id:
            return snapshot
        log.warning("Family %s reader belongs to a different family", family_id)
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
        if not self._read_ready(family_id):
            return
        row = await self.db.family(family_id)
        alerts = alert_settings(row)
        family = self.items.get(family_id)
        if not row["notify_chat"] or not (alerts["online"] or alerts["playing"]) or not family or not family.snapshot:
            return
        account = await self._reader(family_id)
        if account is None:
            return
        try:
            token = await self.owner_token(account)
            presence = await fetch_presence(self.http, token, family.members)
        except TokenRejected:
            await self._reject_token(account)
            return
        except (aiohttp.ClientError, TimeoutError, OSError, RuntimeError):
            self._read_failed(family_id)
            raise
        self.read_backoff.pop(family_id, None)
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
                except Exception as error:
                    log.warning("Family %s presence check failed (%s)", row["family_id"], type(error).__name__)

    async def refresh_loop(self, interval: int) -> None:
        while True:
            for row in await self.db.families_with_accounts():
                try:
                    await self.refresh(row["family_id"])
                except Exception as error:
                    log.warning("Family %s check failed (%s)", row["family_id"], type(error).__name__)
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
