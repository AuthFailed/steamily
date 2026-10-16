"""Compatibility exports for the browser-session diagnostic script."""

from bot.steam_browser import (
    BROWSER_HEADERS,
    begin_browser_login,
    finalize_login,
    poll_browser_login,
    renew_access_token,
    revoke_session,
)

__all__ = [
    "BROWSER_HEADERS",
    "begin_browser_login",
    "finalize_login",
    "poll_browser_login",
    "renew_access_token",
    "revoke_session",
]
