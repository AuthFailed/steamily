import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from cryptography.fernet import Fernet


class ConfigError(RuntimeError):
    def __init__(self, errors: dict[str, str]):
        self.errors = errors
        super().__init__(
            "Invalid configuration:\n"
            + "\n".join(f"- {name}: {message}" for name, message in errors.items())
            + "\nRun python -m bot.setup to configure a new installation. "
            "For an existing database, restore the original SECRET_KEY from your backup."
        )


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str = field(repr=False)
    database_url: str = field(repr=False)
    secret_key: str = field(repr=False)
    poll_interval_seconds: int
    login_location: str


def validate_config(values: Mapping[str, str], *, compose: bool = False) -> Config:
    token = values.get("TELEGRAM_BOT_TOKEN", "").strip()
    database_url = values.get("DATABASE_URL", "").strip()
    key = values.get("SECRET_KEY", "").strip()
    location = values.get("LOGIN_LOCATION", "").strip()
    errors = {}
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{35}", token):
        errors["TELEGRAM_BOT_TOKEN"] = "must be a bot token issued by @BotFather"
    try:
        url = urlsplit(database_url)
        if (
            url.scheme not in ("postgres", "postgresql")
            or not database_url.startswith(f"{url.scheme}://")
            or not url.path.strip("/")
            or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in database_url)
            or any(character in database_url for character in '<>"\\^`{|}')
            or re.search(r"%(?![0-9A-Fa-f]{2})", database_url)
            or url.fragment
        ):
            raise ValueError
        _ = url.port
    except ValueError:
        errors["DATABASE_URL"] = (
            "must be a PostgreSQL URI with a database name, valid port and URL-encoded special characters"
        )
    try:
        Fernet(key.encode("ascii"))
    except (ValueError, UnicodeError):
        errors["SECRET_KEY"] = "must be a valid Fernet key; restore the original key if a database already exists"
    try:
        interval = int(values.get("POLL_INTERVAL_SECONDS", "1800"))
        if interval < 300:
            raise ValueError
    except ValueError:
        interval = 1800
        errors["POLL_INTERVAL_SECONDS"] = "must be a whole number of at least 300"
    if any(character in location for character in "\r\n\x00`"):
        errors["LOGIN_LOCATION"] = "must be a single line of text without backticks"
    if (compose or "POSTGRES_PASSWORD" in values) and not re.fullmatch(
        r"[A-Za-z0-9_.~-]+", values.get("POSTGRES_PASSWORD", "")
    ):
        errors["POSTGRES_PASSWORD"] = "must be nonempty and use only letters, digits, underscore, dot, tilde or hyphen"
    if errors:
        raise ConfigError(errors)
    return Config(token, database_url, key, interval, location)


def load_config(environ: Mapping[str, str] | None = None) -> Config:
    return validate_config(os.environ if environ is None else environ)


# Show the server's real sign-in location, e.g. "Vienna, Wien, AT", before requesting approval.
LOGIN_LOCATION = os.environ.get("LOGIN_LOCATION", "").strip()
INSTANCE_OPERATOR = os.environ.get("INSTANCE_OPERATOR", "").strip()
INSTANCE_HOSTING = os.environ.get("INSTANCE_HOSTING", "").strip()
