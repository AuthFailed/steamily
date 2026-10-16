"""Steam QR login inside the menu message: the QR photo is edited in place when Steam rotates it."""

import asyncio
import hashlib
import io
import logging
import tempfile
import time
from html import escape
from math import ceil
from pathlib import Path

import aiohttp
import qrcode
from aiogram import Bot
from aiogram_dialog import BaseDialogManager, StartMode
from qrcode.image.pure import PyPNGImage

from .db import SteamAlreadyLinked
from .family import Families
from .i18n import t
from .states import Menu
from .steam import NoFamily
from .steam_auth import jwt_payload
from .steam_browser import BROWSER_HEADERS, finalize_login, revoke_session
from .steam_browser import begin_browser_login as begin_qr_login
from .steam_browser import poll_browser_login as poll_qr_login

log = logging.getLogger(__name__)
LOGIN_TIMEOUT = 300
LOGIN_COOLDOWN = 15
MAX_ACTIVE_LOGINS = 10
QR_DIR = Path(tempfile.gettempdir()) / "steam-qr"


class LoginRejected(Exception):
    def __init__(self, reason: str, retry_after: int = 0):
        super().__init__(reason)
        self.reason, self.retry_after = reason, retry_after


def qr_png(url: str) -> bytes:
    buffer = io.BytesIO()
    qrcode.make(url, image_factory=PyPNGImage, box_size=8, border=2).save(buffer)
    return buffer.getvalue()


def qr_file(url: str) -> str:
    # One file per challenge URL: aiogram-dialog caches uploads by path, so a reused path would show a stale code.
    QR_DIR.mkdir(mode=0o700, exist_ok=True)
    path = QR_DIR / f"{hashlib.sha256(url.encode()).hexdigest()[:24]}.png"
    path.write_bytes(qr_png(url))
    path.chmod(0o600)
    return str(path)


class Logins:
    def __init__(self, families: Families, bot: Bot):
        self.families, self.bot = families, bot
        self.tasks: dict[int, asyncio.Task] = {}
        self.retry_at: dict[int, float] = {}
        self.closed = False
        self._closing_sessions: set[asyncio.Task] = set()

    def cancel(self, telegram_id: int) -> None:
        if (task := self.tasks.get(telegram_id)) and not task.cancelling():
            task.cancel()

    async def forget(self, telegram_id: int) -> None:
        """Drain sign-in before deletion so a late Steam response cannot recreate the account."""
        if task := self.tasks.get(telegram_id):
            self.cancel(telegram_id)
            await asyncio.gather(task, return_exceptions=True)
        await asyncio.gather(*self._closing_sessions, return_exceptions=True)
        self.retry_at.pop(telegram_id, None)

    async def start(self, telegram_id: int, manager: BaseDialogManager) -> dict:
        """Starts a QR session; returns the dialog data for the login window and keeps polling in the background."""
        now = time.monotonic()
        self.retry_at = {user: until for user, until in self.retry_at.items() if until > now}
        if self.closed:
            raise LoginRejected("login_stopping")
        if telegram_id in self.retry_at:
            raise LoginRejected("login_cooldown", ceil(self.retry_at[telegram_id] - now))
        if telegram_id in self.tasks:
            raise LoginRejected("login_active")
        if len(self.tasks) >= MAX_ACTIVE_LOGINS:
            raise LoginRejected("login_busy", LOGIN_COOLDOWN)

        # Reserve before the first await, including requests still waiting for Steam to begin.
        self.tasks[telegram_id] = task = asyncio.current_task()
        self.retry_at[telegram_id] = now + LOGIN_COOLDOWN
        http, poller = None, None
        try:
            http = aiohttp.ClientSession(headers=BROWSER_HEADERS, timeout=aiohttp.ClientTimeout(total=30))
            async with asyncio.timeout(LOGIN_TIMEOUT):
                login = await begin_qr_login(http)
            if not self._current(telegram_id):
                raise asyncio.CancelledError
            qr = qr_file(login["url"])
            poller = asyncio.create_task(self._wait(telegram_id, login, qr, manager, http))
            self.tasks[telegram_id] = poller
            poller.add_done_callback(lambda done: self._finished(telegram_id, qr, done, http))
            return {"login": "waiting", "qr": qr, "url": login["url"]}
        finally:
            if self.tasks.get(telegram_id) is task:
                del self.tasks[telegram_id]
            if http is not None and poller is None:
                await http.close()

    def _current(self, telegram_id: int) -> bool:
        task = asyncio.current_task()
        return self.tasks.get(telegram_id) is task and not task.cancelling()

    def _finished(self, telegram_id: int, qr: str, task: asyncio.Task, http: aiohttp.ClientSession) -> None:
        if self.tasks.get(telegram_id) is task:
            del self.tasks[telegram_id]
        Path(qr).unlink(missing_ok=True)  # also runs when cancelled before the coroutine's first step
        if not http.closed:
            cleanup = asyncio.create_task(http.close())
            self._closing_sessions.add(cleanup)
            cleanup.add_done_callback(self._closing_sessions.discard)

    async def close(self) -> None:
        self.closed = True
        tasks = list(self.tasks.values())
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*self._closing_sessions, return_exceptions=True)

    async def _wait(
        self, telegram_id: int, login: dict, qr: str, manager: BaseDialogManager, http: aiohttp.ClientSession
    ) -> None:
        result = "failed"
        durable, refresh_token, steam_id, registered = None, None, None, False

        async def remember(cookie):
            nonlocal durable
            durable = cookie

        def mark_registered():
            nonlocal registered
            registered = True

        try:
            async with asyncio.timeout(LOGIN_TIMEOUT):
                while refresh_token is None:
                    await asyncio.sleep(login["interval"])
                    old_url = login["url"]
                    refresh_token = await poll_qr_login(http, login)
                    if refresh_token:
                        steam_id = jwt_payload(refresh_token).get("sub")
                    if not self._current(telegram_id):
                        raise asyncio.CancelledError
                    if refresh_token is None and login["url"] != old_url:
                        old_qr, qr = qr, qr_file(login["url"])
                        try:
                            await manager.update({"qr": qr, "url": login["url"]})  # edits the photo in place
                        finally:
                            Path(old_qr).unlink(missing_ok=True)

                steam_id = jwt_payload(refresh_token)["sub"]
                durable = await finalize_login(http, refresh_token, steam_id, on_refresh=remember)
                try:
                    family = await self.families.register(
                        telegram_id, durable, steam_id, on_refresh=remember, on_registered=mark_registered
                    )
                except SteamAlreadyLinked:
                    result = "already_linked"
                    return
                except NoFamily:
                    result = "no_family"
                    return
                registered = True
                if not self._current(telegram_id):
                    raise asyncio.CancelledError
                result = None
        except TimeoutError:
            if registered:
                result = None
                await self._deliver_login(telegram_id, steam_id, manager)
            else:
                result = "timeout"
        except asyncio.CancelledError:
            result = None
            raise
        except Exception as error:
            log.error("Steam login failed for %s (%s)", telegram_id, type(error).__name__)
        else:
            await self._deliver_login(telegram_id, steam_id, manager, family)
        finally:
            cleanup = asyncio.create_task(
                self._cleanup_login(telegram_id, http, durable or refresh_token, steam_id, registered, qr)
            )
            self._closing_sessions.add(cleanup)
            cleanup.add_done_callback(self._closing_sessions.discard)
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise
            if result and self._current(telegram_id):
                try:
                    await manager.update({"login": result})
                except Exception:  # the user already left the login screen
                    log.info("Could not show login result %s to %s", result, telegram_id)

    async def _cleanup_login(self, telegram_id, http, credential, steam_id, registered, qr) -> None:
        try:
            async with asyncio.timeout(30):
                if credential and steam_id and not registered:
                    cleanup_safe = True
                    try:
                        account = await self.families.db.account(telegram_id)
                        registered = bool(
                            account
                            and account.get("steam_id") == steam_id
                            and self.families.db.refresh_token(account) == credential
                        )
                    except Exception as error:
                        cleanup_safe = False
                        log.warning("Steam registration state is unavailable (%s)", type(error).__name__)
                    if cleanup_safe and not registered:
                        try:
                            await revoke_session(http, credential, steam_id)
                        except Exception as error:
                            log.warning("Unregistered Steam session could not be revoked (%s)", type(error).__name__)
        except TimeoutError:
            log.warning("Steam login cleanup timed out")
        finally:
            await http.close()
            Path(qr).unlink(missing_ok=True)

    async def _deliver_login(self, telegram_id: int, steam_id: str, manager: BaseDialogManager, family=None) -> None:
        try:
            await manager.start(Menu.main, mode=StartMode.RESET_STACK)
        except Exception as error:
            log.error("Menu delivery after login failed for %s (%s)", telegram_id, type(error).__name__)
        try:
            account = await self.families.db.account(telegram_id)
            if account["status"] == "pending":
                family = family or self.families.get(account["family_id"])
                await self._tell_owner(family, steam_id)
        except Exception as error:
            log.error("Owner notification after login failed for %s (%s)", telegram_id, type(error).__name__)

    async def _tell_owner(self, family, steam_id: str) -> None:
        owner = (await self.families.db.family(family.family_id))["owner_telegram_id"]
        if owner is None or not await self.families.db.notify_requests(owner):
            return
        lang = await self.families.db.language(owner)
        await self.bot.send_message(owner, t(lang, "owner_new_member", name=escape(family.name(steam_id))))
