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

import qrcode
from aiogram import Bot
from aiogram_dialog import BaseDialogManager, StartMode
from qrcode.image.pure import PyPNGImage

from .family import Families
from .i18n import t
from .states import Menu
from .steam import NoFamily
from .steam_auth import begin_qr_login, jwt_payload, poll_qr_login

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
    QR_DIR.mkdir(exist_ok=True)
    path = QR_DIR / f"{hashlib.sha256(url.encode()).hexdigest()[:24]}.png"
    path.write_bytes(qr_png(url))
    return str(path)


class Logins:
    def __init__(self, families: Families, bot: Bot):
        self.families, self.bot = families, bot
        self.tasks: dict[int, asyncio.Task] = {}
        self.retry_at: dict[int, float] = {}
        self.closed = False

    def cancel(self, telegram_id: int) -> None:
        if task := self.tasks.get(telegram_id):
            task.cancel()

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
        try:
            async with asyncio.timeout(LOGIN_TIMEOUT):
                login = await begin_qr_login(self.families.http)
            if not self._current(telegram_id):
                raise asyncio.CancelledError
            qr = qr_file(login["url"])
            poller = asyncio.create_task(self._wait(telegram_id, login, qr, manager))
            self.tasks[telegram_id] = poller
            poller.add_done_callback(lambda done: self._finished(telegram_id, qr, done))
            return {"login": "waiting", "qr": qr, "url": login["url"]}
        finally:
            if self.tasks.get(telegram_id) is task:
                del self.tasks[telegram_id]

    def _current(self, telegram_id: int) -> bool:
        task = asyncio.current_task()
        return self.tasks.get(telegram_id) is task and not task.cancelling()

    def _finished(self, telegram_id: int, qr: str, task: asyncio.Task) -> None:
        if self.tasks.get(telegram_id) is task:
            del self.tasks[telegram_id]
        Path(qr).unlink(missing_ok=True)  # also runs when cancelled before the coroutine's first step

    async def close(self) -> None:
        self.closed = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _wait(self, telegram_id: int, login: dict, qr: str, manager: BaseDialogManager) -> None:
        result = "failed"
        try:
            async with asyncio.timeout(LOGIN_TIMEOUT):
                refresh_token = None
                while refresh_token is None:
                    await asyncio.sleep(login["interval"])
                    old_url = login["url"]
                    refresh_token = await poll_qr_login(self.families.http, login)
                    if not self._current(telegram_id):
                        raise asyncio.CancelledError
                    if refresh_token is None and login["url"] != old_url:
                        old_qr, qr = qr, qr_file(login["url"])
                        try:
                            await manager.update({"qr": qr, "url": login["url"]})  # edits the photo in place
                        finally:
                            Path(old_qr).unlink(missing_ok=True)

                steam_id = jwt_payload(refresh_token)["sub"]
                try:
                    family = await self.families.register(telegram_id, refresh_token, steam_id)
                except NoFamily:
                    result = "no_family"
                    return
                if not self._current(telegram_id):
                    raise asyncio.CancelledError
                if (await self.families.db.account(telegram_id))["status"] == "pending":
                    await self._tell_owner(family, steam_id)
                result = None
                await manager.start(Menu.main, mode=StartMode.RESET_STACK)
        except TimeoutError:
            result = "timeout"
        except asyncio.CancelledError:
            result = None
            raise
        except Exception:
            log.exception("Steam login failed for %s", telegram_id)
        finally:
            Path(qr).unlink(missing_ok=True)
            if result and self._current(telegram_id):
                try:
                    await manager.update({"login": result})
                except Exception:  # the user already left the login screen
                    log.info("Could not show login result %s to %s", result, telegram_id)

    async def _tell_owner(self, family, steam_id: str) -> None:
        owner = (await self.families.db.family(family.family_id))["owner_telegram_id"]
        if owner is None or not await self.families.db.notify_requests(owner):
            return
        lang = await self.families.db.language(owner)
        await self.bot.send_message(owner, t(lang, "owner_new_member", name=escape(family.name(steam_id))))
