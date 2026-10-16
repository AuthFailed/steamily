"""Steam QR login inside the menu message: the QR photo is edited in place when Steam rotates it."""

import asyncio
import hashlib
import io
import logging
import tempfile
import time
from html import escape
from pathlib import Path

import aiohttp
import qrcode
from aiogram import Bot
from aiogram_dialog import BaseDialogManager, StartMode
from qrcode.image.pure import PyPNGImage

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
QR_DIR = Path(tempfile.gettempdir()) / "steam-qr"


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

    def cancel(self, telegram_id: int) -> None:
        if task := self.tasks.pop(telegram_id, None):
            task.cancel()

    async def start(self, telegram_id: int, manager: BaseDialogManager) -> dict:
        """Starts a QR session; returns the dialog data for the login window and keeps polling in the background."""
        previous = self.tasks.get(telegram_id)
        self.cancel(telegram_id)
        if previous:
            try:
                await previous
            except asyncio.CancelledError:
                pass
        http = aiohttp.ClientSession(headers=BROWSER_HEADERS, timeout=aiohttp.ClientTimeout(total=30))
        try:
            login = await begin_qr_login(http)
            qr = qr_file(login["url"])
        except BaseException:
            await http.close()
            raise
        task = self.tasks[telegram_id] = asyncio.create_task(self._wait(telegram_id, login, qr, manager, http))

        def finished(_):
            # A task cancelled before its first turn never enters its own finally block.
            Path(qr).unlink(missing_ok=True)
            if not http.closed:
                asyncio.create_task(http.close())

        task.add_done_callback(finished)
        return {"login": "waiting", "qr": qr, "url": login["url"]}

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
            deadline = time.monotonic() + LOGIN_TIMEOUT
            while refresh_token is None and time.monotonic() < deadline:
                await asyncio.sleep(login["interval"])
                old_url = login["url"]
                refresh_token = await poll_qr_login(http, login)
                if refresh_token is None and login["url"] != old_url:
                    old_qr, qr = qr, qr_file(login["url"])
                    await manager.update({"qr": qr, "url": login["url"]})  # edits the photo in place
                    Path(old_qr).unlink(missing_ok=True)
            if refresh_token is None:
                result = "timeout"
                return

            steam_id = jwt_payload(refresh_token)["sub"]
            durable = await finalize_login(http, refresh_token, steam_id, on_refresh=remember)
            try:
                family = await self.families.register(
                    telegram_id, durable, steam_id, on_refresh=remember, on_registered=mark_registered
                )
            except NoFamily:
                result = "no_family"
                return
            registered = True
            if (await self.families.db.account(telegram_id))["status"] == "pending":
                await self._tell_owner(family, steam_id)
            result = None
            await manager.start(Menu.main, mode=StartMode.RESET_STACK)
        except asyncio.CancelledError:
            result = None
            raise
        except Exception as error:
            log.warning("Steam login failed for %s (%s)", telegram_id, type(error).__name__)
        finally:
            credential = durable or refresh_token
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
            await http.close()
            Path(qr).unlink(missing_ok=True)
            if self.tasks.get(telegram_id) is asyncio.current_task():
                del self.tasks[telegram_id]
            if result:
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
