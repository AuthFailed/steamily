import asyncio
import logging
import random
import tempfile
import time
from collections import defaultdict
from html import escape
from pathlib import Path
from weakref import WeakValueDictionary

import aiohttp
from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramNotFound,
    TelegramRetryAfter,
)
from aiogram.types import LinkPreviewOptions

from . import charts
from .db import Db
from .health import Health
from .library import ALERTS, family_events, format_event, format_notifications, presence_events, reconcile
from .steam import (
    NoFamily,
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
ALERT_RETRY_DELAY = 60
PRESENCE_ALERT_TTL = 5 * 60


DEFAULT_COUNTRY = "US"


class SessionRevocationFailed(RuntimeError):
    """Steam could not confirm revocation; retain the saved login for a retry."""


class Family:
    """Latest snapshot of one Steam family; prices come from the store cache of each viewer's country."""

    def __init__(self, family_id: str, stores: dict[str, dict] | None = None):
        self.family_id = family_id
        self.lock = asyncio.Lock()
        self.snapshot: dict | None = None
        self.stores = stores if stores is not None else {}
        self._charts: tempfile.TemporaryDirectory | None = None
        self._charts_removed = False
        # shortcut: process-local coordination; use distributed locking before running multiple bot workers.
        self._refresh_lock = asyncio.Lock()
        self._refresh_started = 0
        self._refresh_completed = 0

    @property
    def chart_dir(self) -> Path:
        if self._charts_removed:
            raise FileNotFoundError("This family's charts were removed")
        if self._charts is None:
            charts.CHART_DIR.mkdir(exist_ok=True)
            self._charts = tempfile.TemporaryDirectory(dir=charts.CHART_DIR)
        return Path(self._charts.name)

    def cleanup_charts(self) -> None:
        self._charts_removed = True
        if self._charts is not None:
            self._charts.cleanup()

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
    def __init__(self, db: Db, http: aiohttp.ClientSession, bot: Bot, health: Health | None = None):
        self.db, self.http, self.bot = db, http, bot
        self.health = health or Health()
        self.items: dict[str, Family] = {}
        self.auths: dict[int, SteamAuth] = {}
        self.auth_generations: dict[int, int] = {}
        self.account_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
        self.stores: dict[str, dict] = {}
        self.stats_cache: dict[str, tuple[float, dict]] = {}
        self.presence: dict[str, dict] = {}  # last seen online/playing state per family
        self.read_backoff: dict[str, tuple[int, float]] = {}
        self.last_steam_check: float | None = None
        self.last_family_check: dict[str, float] = {}
        self._cache_generation = 0
        self.delivery_locks = defaultdict(asyncio.Lock)
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
                current = await self.db.account(telegram_id)
                if (
                    self.auths.get(telegram_id) is not auth
                    or current is None
                    or current["steam_id"] != steam_id
                    or current["token_generation"] != generation
                    or self.db.refresh_token(current) not in (auth.refresh_token, token)
                ):
                    raise TokenRejected("This bot login was removed")
                if not await self.db.set_refresh_token(
                    telegram_id,
                    token,
                    renewed_at,
                    expected_generation=generation,
                    expected_refresh_token=current["refresh_token"],
                ):
                    raise TokenRejected("Steam login was replaced")

            auth = self.auths[telegram_id] = SteamAuth(self.http, refresh_token, steam_id, save, last_renewal)
            self.auth_generations[telegram_id] = generation
        return auth

    async def register(
        self, telegram_id: int, refresh_token: str, steam_id: str, *, on_refresh=None, on_registered=None
    ) -> Family:
        """Validate a new login, then commit it before cancellable follow-up work."""
        while True:
            account = await self.db.account(telegram_id)
            guard = self.get(account["family_id"]).lock if account else asyncio.Lock()
            async with guard, self.account_locks.setdefault(telegram_id, asyncio.Lock()):
                current = await self.db.account(telegram_id)
                if (current["family_id"] if current else None) != (account["family_id"] if account else None):
                    continue
                account = current
                await self.db.check_steam_link(telegram_id, steam_id)
                last_renewal = account["token_renewed_at"] if account and account["steam_id"] == steam_id else 0.0

                async def remember(token, renewed_at):
                    await on_refresh(token)

                auth = SteamAuth(
                    self.http, refresh_token, steam_id, remember if on_refresh else None, last_renewal=last_renewal
                )
                access_token = await auth.get_access_token()
                try:
                    snapshot = await fetch_family_snapshot(self.http, access_token)
                except NoFamily:
                    if account and account["steam_id"] == steam_id:
                        await self._revoke_account(account)
                    raise
                country = await fetch_user_country(self.http, access_token, steam_id)
                family_id = snapshot["family_id"]
                if steam_id not in snapshot["members"]:
                    raise RuntimeError("Steam returned a family without the signed-in account")
                await self.db.ensure_family(family_id, snapshot["name"])
                await self.db.save_account(
                    telegram_id,
                    steam_id,
                    family_id,
                    auth.refresh_token,
                    country,
                    auth.last_renewal,
                    claim_owner=account is None,
                )
                if on_registered:
                    on_registered()
                registered = await self.db.account(telegram_id)
                self.auths.pop(telegram_id, None)
                self.auth_generations.pop(telegram_id, None)
                self.read_backoff.pop(family_id, None)
                if account and (account["steam_id"] != steam_id or account["family_id"] != family_id):
                    self.stats_cache.pop(account["steam_id"], None)
                    self._cache_generation += 1
                break
        family = self.get(family_id)
        async with family.lock:
            await self._reconcile_accounts(family_id, snapshot["members"])
            if account and account["family_id"] == family_id and account["steam_id"] == steam_id and registered:
                # Only an explicit fresh Steam login can recover ownership without an approved successor.
                await self.db.claim_initial_owner(
                    family_id, telegram_id, expected_generation=registered["token_generation"]
                )
            family.snapshot = snapshot
            self._checked(family_id)
        refresh_ids = [family_id]
        if account and account["family_id"] != family_id:
            if await self.db.prune_family(account["family_id"]) or not await self.db.family(account["family_id"]):
                self._forget_family(account["family_id"])
            else:
                refresh_ids.insert(0, account["family_id"])
        for refresh_id in refresh_ids:
            try:
                await self.refresh(refresh_id)
            except Exception as error:
                log.warning("Family %s refresh after login failed (%s)", refresh_id, type(error).__name__)
        return family

    async def make_owner(self, family_id: str, telegram_id: int | None) -> None:
        await self.db.update_family(family_id, owner_telegram_id=telegram_id)
        if telegram_id is not None:
            await self.db.set_status(telegram_id, "approved")

    async def create_owner_transfer(self, family_id: str, owner_id: int, target_id: int):
        family = self.get(family_id)
        async with family.lock:
            actor = await self.db.account(owner_id)
            if role(actor) != "owner" or actor["family_id"] != family_id:
                return None
            try:
                snapshot = await self._fetch(family_id)
            except (aiohttp.ClientError, TimeoutError, OSError, RuntimeError):
                return None
            if snapshot is None or snapshot["family_id"] != family_id:
                return None
            family.snapshot = snapshot
            return await self.db.create_owner_transfer(family_id, owner_id, target_id, snapshot["members"])

    async def accept_owner_transfer(self, family_id: str, target_id: int, transfer_id: str):
        family = self.get(family_id)
        async with family.lock:
            invitation = await self.db.owner_transfer(family_id)
            if invitation is None or invitation["id"] != transfer_id or invitation["target_id"] != target_id:
                return None
            try:
                snapshot = await self._fetch(family_id)
            except (aiohttp.ClientError, TimeoutError, OSError, RuntimeError):
                return None
            if snapshot is None or snapshot["family_id"] != family_id:
                return None
            family.snapshot = snapshot
            return await self.db.accept_owner_transfer(family_id, target_id, transfer_id, snapshot["members"])

    async def leave(self, telegram_id: int) -> int | None:
        """Revoke this Steam session before deleting its account and handing over its bot role."""
        while True:
            account = await self.db.account(telegram_id)
            family = self.get(account["family_id"]) if account else None
            guard = family.lock if family else asyncio.Lock()
            async with guard, self.account_locks.setdefault(telegram_id, asyncio.Lock()):
                cached_account = account
                account = await self.db.account(telegram_id)
                if account and (family is None or account["family_id"] != family.family_id):
                    continue
                if account and account["refresh_token"]:
                    auth = self.auth(
                        telegram_id,
                        self.db.refresh_token(account),
                        account["steam_id"],
                        account["token_renewed_at"],
                        generation=account["token_generation"],
                    )
                    try:
                        await auth.revoke()
                    except Exception:
                        raise SessionRevocationFailed("Steam could not confirm logout") from None
                new_owner = None
                if account and account["owner_telegram_id"] == telegram_id:
                    others = [
                        a
                        for a in await self.db.family_accounts(account["family_id"])
                        if a["telegram_id"] != telegram_id
                        and a["status"] == "approved"
                        and a["refresh_token"]
                        and (a["steam_id"] in family.members if family.snapshot else a["status"] == "approved")
                    ]
                    if others:
                        new_owner = random.choice(others)["telegram_id"]
                    new_owner = await self.db.handover_owner(account["family_id"], telegram_id, new_owner)
                await self.db.delete_user(telegram_id)
                await self._forget(account or cached_account, telegram_id)
                return new_owner

    async def remove(self, telegram_id: int, *, family_id: str) -> bool:
        account = await self.db.account(telegram_id)
        changed = await self.db.delete_account(telegram_id, family_id=family_id)
        if changed:
            await self._forget(account, telegram_id)
        return changed

    async def _forget(self, account, telegram_id: int) -> None:
        self.auths.pop(telegram_id, None)
        self.auth_generations.pop(telegram_id, None)
        self._cache_generation += 1
        if account:
            self.stats_cache.pop(account["steam_id"], None)
            family_id = account["family_id"]
            row = await self.db.family(family_id)
            if row and row["reader_telegram_id"] == telegram_id:
                await self.db.update_family(family_id, reader_telegram_id=None)
                self.read_backoff.pop(family_id, None)
            if not await self.db.family_accounts(family_id):
                self._forget_family(family_id)

    def _forget_family(self, family_id: str) -> None:
        family = self.items.pop(family_id, None)
        if family is not None:
            family.cleanup_charts()
        self.presence.pop(family_id, None)
        self.delivery_locks.pop(family_id, None)
        self.last_family_check.pop(family_id, None)
        self.read_backoff.pop(family_id, None)
        self.health.families.pop(family_id, None)
        self.health.first_seen.pop(family_id, None)
        self.stats_cache.clear()  # Stats may include former members absent from the latest snapshot.
        self._cache_generation += 1

    def _checked(self, family_id: str) -> None:
        self.last_steam_check = self.last_family_check[family_id] = time.time()
        self.health.steam_read(family_id)

    async def _revoke_account(self, account) -> None:
        family_id, telegram_id = account["family_id"], account["telegram_id"]
        if not await self.db.revoke_membership(
            telegram_id, family_id, steam_id=account["steam_id"], expected_generation=account["token_generation"]
        ):
            return
        self.auths.pop(telegram_id, None)
        self.auth_generations.pop(telegram_id, None)
        self.stats_cache.pop(account["steam_id"], None)
        self._cache_generation += 1
        family = self.get(family_id)
        if family.snapshot:
            family.snapshot = {**family.snapshot, "members": [m for m in family.members if m != account["steam_id"]]}

    async def _reconcile_accounts(self, family_id: str, members: list[str]) -> None:
        await self.db.reconcile_members(family_id, members)
        for account in await self.db.family_accounts(family_id):
            if account["steam_id"] not in members:
                self.auths.pop(account["telegram_id"], None)
                self.auth_generations.pop(account["telegram_id"], None)
                self.stats_cache.pop(account["steam_id"], None)
                self._cache_generation += 1

    async def owner_token(self, account) -> str:
        """Access token of the acting user's own Steam account, for changes to the family."""
        async with self.account_locks.setdefault(account["telegram_id"], asyncio.Lock()):
            current = await self.db.account(account["telegram_id"])
            if (
                current is None
                or current["status"] == "blocked"
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
                token = await auth.get_access_token()
            except TokenRejected:
                await self._reject_token(current)
                raise
            if self.auths.get(current["telegram_id"]) is not auth:
                raise TokenRejected("This bot login was removed")
            return token

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
        if row is None:
            return None
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
        generation = self._cache_generation
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
        if generation == self._cache_generation:
            self.stats_cache[steam_id] = (time.monotonic(), stats)
        return stats

    async def refresh(self, family_id: str) -> None:
        if not self._read_ready(family_id):
            return
        family = self.get(family_id)
        requested = family._refresh_started
        async with family._refresh_lock:
            if not self._read_ready(family_id):
                return
            if self.items.get(family_id) is not family:
                return
            # Requests during a pass need a trailing fetch; other queued requests can share that pass.
            if family._refresh_completed > requested:
                return
            family._refresh_started += 1
            try:
                await self._refresh(family_id)
            except (aiohttp.ClientError, TimeoutError, OSError, RuntimeError):
                self._read_failed(family_id)
                raise
            else:
                self.read_backoff.pop(family_id, None)
            family._refresh_completed = family._refresh_started

    async def _refresh(self, family_id: str) -> None:
        family = self.get(family_id)
        async with family.lock:
            if self.items.get(family_id) is not family:
                return
            previous = family.snapshot  # Capture membership before revocation trims the cached snapshot.
            snapshot = await self._fetch(family_id)
            if snapshot is None:
                return
            row = await self.db.family(family_id)
            if row is None or self.items.get(family_id) is not family:
                return
            events, state = reconcile(row["state"], snapshot)
            alerts = alert_settings(row)
            pending = []
            if alerts["games"]:
                pending.extend(
                    {"text": text, "parse_mode": "HTML"}
                    for text in format_notifications(events, snapshot["names"], row["language"])
                )
            previous = (row["state"] or {}).get("observed", previous)
            if previous:
                for event in family_events(previous, snapshot):
                    if alerts[event["kind"]]:
                        pending.append(
                            {
                                "text": format_event(event, snapshot["names"], snapshot["apps"], row["language"]),
                                "parse_mode": "HTML" if event["type"] == "removed" else None,
                            }
                        )
            state["observed"] = {
                "members": snapshot["members"],
                "names": snapshot["names"],
                "apps": [
                    {key: app[key] for key in ("appid", "name", "owners", "exclude_reason") if key in app}
                    for app in snapshot["apps"]
                ],
            }
            await self.db.observe_family(family_id, state, snapshot["name"], pending)
            if self.items.get(family_id) is not family:
                return
            family.snapshot = snapshot
            self.presence.setdefault(family_id, snapshot.get("players", {}))
        await self.deliver_alerts(family_id)

        # Prices in the currency of every member who uses the bot.
        accounts = await self.db.family_accounts(family_id)
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
        """Read through the designated approved session without hiding failure behind another account."""
        account = await self._reader(family_id)
        if account is None:
            return None
        try:
            token = await self.owner_token(account)
        except TokenRejected:
            return None
        auth = self.auths.get(account["telegram_id"])
        try:
            snapshot = await fetch_family_snapshot(self.http, token)
        except TokenRejected:
            await self._reject_token(account)
            return None
        except NoFamily:
            if auth is not None and self.auths.get(account["telegram_id"]) is auth:
                await self._revoke_account(account)
            return None
        if auth is None or self.auths.get(account["telegram_id"]) is not auth:
            return None
        if account["steam_id"] not in snapshot["members"]:
            log.warning("Steam returned a family without account %s", account["telegram_id"])
            return None
        if snapshot["family_id"] == family_id:
            await self._reconcile_accounts(family_id, snapshot["members"])
            if self.auths.get(account["telegram_id"]) is not auth:
                return None
            self._checked(family_id)
            return snapshot
        await self._revoke_account(account)
        return None

    async def deliver_alerts(self, family_id: str) -> None:
        lock = self.delivery_locks[family_id]
        async with lock:
            for _ in range(20):  # bound each pass so a backlog yields to other families
                if self.delivery_locks.get(family_id) is not lock:
                    return
                pending = await self.db.pending_alerts(family_id, time.time())
                if not pending or self.delivery_locks.get(family_id) is not lock:
                    return
                event = pending[0]
                chat_id = event["chat_id"]
                try:
                    text = event["text"]
                    if event["coalesce_key"] is not None:
                        text = await self.presence_alert_text(family_id, event)
                        if text is None:
                            await self.db.delete_alert(event["id"])
                            continue
                    self.health.in_flight += 1
                    try:
                        await self.bot.send_message(
                            chat_id,
                            text,
                            parse_mode=event.get("parse_mode"),
                            link_preview_options=LinkPreviewOptions(is_disabled=True),
                        )
                    except Exception:
                        self.health.failed_notifications += 1
                        self.health.last_notification_failure = time.monotonic()
                        raise
                    finally:
                        self.health.in_flight -= 1
                except TelegramMigrateToChat as error:
                    if self.delivery_locks.get(family_id) is not lock:
                        return
                    await self.db.migrate_chat(
                        chat_id, error.migrate_to_chat_id, family_id=family_id, link_version=event["link_version"]
                    )
                    row = await self.db.family(family_id)
                    if row is None or row["alert_link_version"] != event["link_version"]:
                        return
                    if row["notify_chat"] == chat_id:
                        log.warning("Family %s migration destination is occupied; unlinking old group", family_id)
                        await self.db.unlink_chat(chat_id, family_id=family_id, link_version=event["link_version"])
                    else:
                        await self.db.defer_alerts(
                            family_id, chat_id, time.time() + ALERT_RETRY_DELAY, link_version=event["link_version"]
                        )
                    return
                except (TelegramForbiddenError, TelegramNotFound) as error:
                    if self.delivery_locks.get(family_id) is not lock:
                        return
                    log.warning("Family %s group is unavailable; unlinking: %s", family_id, error)
                    await self.db.unlink_chat(chat_id, family_id=family_id, link_version=event["link_version"])
                    return
                except TelegramBadRequest as error:
                    if self.delivery_locks.get(family_id) is not lock:
                        return
                    log.warning("Family %s alert %s permanently rejected: %s", family_id, event["id"], error)
                    if any(reason in error.message.lower() for reason in ("chat not found", "chat was deleted")):
                        await self.db.unlink_chat(chat_id, family_id=family_id, link_version=event["link_version"])
                        return
                    await self.db.delete_alert(event["id"])
                except (TelegramAPIError, aiohttp.ClientError, OSError) as error:
                    if self.delivery_locks.get(family_id) is not lock:
                        return
                    delay = error.retry_after if isinstance(error, TelegramRetryAfter) else ALERT_RETRY_DELAY
                    log.warning("Family %s alert %s deferred: %s", family_id, event["id"], error)
                    await self.db.defer_alerts(
                        family_id, chat_id, time.time() + max(1, delay), link_version=event["link_version"]
                    )
                    return
                else:
                    await self.db.delete_alert(event["id"])

    async def delivery_loop(self, interval: int = 5) -> None:
        while True:
            self.health.beat("delivery")
            failed = False
            try:
                for row in await self.db.alert_families():
                    self.health.beat("delivery")
                    try:
                        await self.deliver_alerts(row["family_id"])
                    except Exception as error:
                        failed = True
                        log.error("Family %s alert delivery failed (%s)", row["family_id"], type(error).__name__)
            except Exception as error:
                failed = True
                log.error("Pending alert scan failed (%s)", type(error).__name__)
            self.health.beat("delivery", failed=failed)
            await asyncio.sleep(interval)

    async def presence_alert_text(self, family_id: str, queued) -> str | None:
        # Keep events structured until delivery: queued text would outlive consent and membership.
        publisher = queued["publication"]
        if publisher is None:  # Presence queued before publication controls had no consent to verify.
            return None
        event = publisher["event"]
        async with self.get(family_id).lock:
            if not self._read_ready(family_id):
                raise OSError("Steam family reads are paused")
            try:
                snapshot = await self._fetch(family_id)
            except (aiohttp.ClientError, TimeoutError, OSError, RuntimeError):
                self._read_failed(family_id)
                raise OSError("Steam family could not be checked for delivery") from None
            if snapshot is None:
                return None
            self.read_backoff.pop(family_id, None)
            family = Family(family_id, self.stores)
            family.snapshot = snapshot
            account = await self.db.publication_account(publisher["telegram_id"])
            if (
                account is None
                or account["notify_chat"] != queued["chat_id"]
                or account["alert_link_version"] != queued["link_version"]
                or not alert_settings(account)[event["kind"]]
                or account["publication_version"] != publisher["publication_version"]
                or not publication_allowed(family, event["steamid"], {account["steam_id"]: account}, "publish_presence")
            ):
                return None
            return format_event(event, snapshot["names"], family.apps, account["language"])

    async def check_presence(self, family_id: str) -> None:
        """Online and now-playing alerts; Steam is asked once for all members of the family."""
        if not self._read_ready(family_id):
            return
        family = self.get(family_id)
        async with family._refresh_lock:
            if self.items.get(family_id) is not family or not self._read_ready(family_id):
                return
            try:
                await self._check_presence(family_id)
            except (aiohttp.ClientError, TimeoutError, OSError, RuntimeError):
                self._read_failed(family_id)
                raise
            else:
                self.read_backoff.pop(family_id, None)

    async def _check_presence(self, family_id: str) -> None:
        row = await self.db.family(family_id)
        if row is None:
            return
        alerts = alert_settings(row)
        family = self.items.get(family_id)
        if not row["notify_chat"] or not (alerts["online"] or alerts["playing"]) or not family or not family.snapshot:
            return
        account = await self._reader(family_id)
        if account is None:
            return
        accounts = await accounts_by_steam(self.db, family_id)
        try:
            token = await self.owner_token(account)
            presence = await fetch_presence(self.http, token, family.members)
        except TokenRejected:
            await self._reject_token(account)
            return
        if self.items.get(family_id) is not family:
            return
        pending = []
        for event in presence_events(self.presence.get(family_id, {}), presence):
            if alerts[event["kind"]] and publication_allowed(family, event["steamid"], accounts, "publish_presence"):
                pending.append(
                    {
                        "text": format_event(event, family.snapshot["names"], family.apps, row["language"]),
                        "coalesce_key": f"{event['kind']}:{event['steamid']}",
                        "expires_at": time.time() + PRESENCE_ALERT_TTL,
                        "publication": {
                            "event": event,
                            "telegram_id": accounts[event["steamid"]]["telegram_id"],
                            "publication_version": accounts[event["steamid"]]["publication_version"],
                        },
                    }
                )
        await self.db.enqueue_alerts(family_id, pending)
        if self.items.get(family_id) is not family:
            return
        self.presence[family_id] = presence
        for steam_id, now in presence.items():  # keeps the 🟢 marks in menus fresh too
            family.snapshot.setdefault("players", {}).setdefault(steam_id, {}).update(now)
        await self.deliver_alerts(family_id)

    async def presence_loop(self, interval: int = PRESENCE_INTERVAL) -> None:
        self.health.beat("presence")
        while True:
            await asyncio.sleep(interval)
            self.health.beat("presence")
            failed = False
            try:
                for row in await self.db.families_with_accounts():
                    self.health.beat("presence")
                    try:
                        await self.check_presence(row["family_id"])
                    except Exception as error:
                        failed = True
                        log.error("Family %s presence check failed (%s)", row["family_id"], type(error).__name__)
            except Exception as error:
                failed = True
                log.error("Presence check cycle failed (%s)", type(error).__name__)
            self.health.beat("presence", failed=failed)

    async def refresh_loop(self, interval: int) -> None:
        while True:
            self.health.beat("refresh")
            failed = False
            try:
                generation = self._cache_generation
                rows = await self.db.families_with_accounts()
                if generation == self._cache_generation:
                    self.health.expect_families([row["family_id"] for row in rows])
                for row in rows:
                    self.health.beat("refresh")
                    try:
                        await self.refresh(row["family_id"])
                    except Exception as error:
                        failed = True
                        log.error("Family %s check failed (%s)", row["family_id"], type(error).__name__)
            except Exception as error:
                failed = True
                log.error("Family refresh cycle failed (%s)", type(error).__name__)
            self.health.beat("refresh", failed=failed)
            await asyncio.sleep(interval)


def publication_allowed(family: Family, steam_id: str, accounts: dict, setting: str) -> bool:
    account = accounts.get(steam_id)
    return bool(
        account
        and steam_id in family.members
        and account["steam_id"] == steam_id
        and account["family_id"] == family.family_id
        and account["status"] == "approved"
        and account["refresh_token"]
        and account.get(setting, False)
    )


def person_link(family: Family, steam_id: str, accounts: dict) -> str:
    """Steam link, plus Telegram only with the active member's consent; no rendered identity cache."""
    url = profile_url(steam_id, (family.snapshot or {}).get("players", {}))
    steam = f'<a href="{url}">{escape(family.name(steam_id))}</a>'
    account = accounts.get(steam_id)
    if not publication_allowed(family, steam_id, accounts, "publish_telegram"):
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
    if account["status"] != "approved":
        return "pending"
    if account["owner_telegram_id"] == account["telegram_id"]:
        return "owner"
    return "member"
