"""Create a private .env interactively: python -m bot.setup."""

import argparse
import os
import re
import secrets
import shlex
import sys
import tempfile
from getpass import getpass
from pathlib import Path

from cryptography.fernet import Fernet

from .config import ConfigError, validate_config

ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
BACKUP_WARNING = (
    "WARNING: Back up .env securely, especially SECRET_KEY, together with your database. "
    "Losing or changing SECRET_KEY makes saved Steam logins unreadable. Never commit .env."
)


def read_env_file(path: Path) -> dict[str, str]:
    if path.is_symlink():
        raise ConfigError({".env": "refusing a symbolic link; choose a regular private file"})
    if not path.exists():
        return {}
    values = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        assignment = ASSIGNMENT.match(line)
        if assignment is None:
            raise ConfigError({".env": f"line {number} must use NAME=value syntax"})
        raw = assignment[2]
        if raw.startswith(("'", '"')):
            try:
                value = " ".join(shlex.split(raw, comments=True))
            except ValueError:
                raise ConfigError({".env": f"line {number} has invalid quoting; use a single line per value"}) from None
            if raw.startswith('"'):
                value = re.sub(r"\\([$`])", r"\1", value)
        else:
            value = re.split(r"\s+#", raw, maxsplit=1)[0].rstrip()
        values[assignment[1]] = value
    return values


def _quote(value: str) -> str:
    if any(character in value for character in "\r\n\x00"):
        raise ConfigError({".env": "values must fit on one line"})
    if "'" in value and "`" in value:
        raise ConfigError({".env": "values containing both apostrophes and backticks must be URL-encoded"})
    if "'" not in value:
        return f"'{value}'"
    escaped = re.sub(r'([\\"$`])', r"\\\1", value)
    return f'"{escaped}"'


def write_env_file(path: Path, values: dict[str, str]) -> None:
    # Replace atomically so interruption cannot truncate the only copy of SECRET_KEY.
    read_env_file(path)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.exists() else []
    written = set()
    output = []
    for line in lines:
        assignment = ASSIGNMENT.match(line.rstrip("\r\n"))
        if assignment and assignment[1] in values:
            name = assignment[1]
            output.append(f"{name}={_quote(values[name])}\n")
            written.add(name)
        else:
            output.append(line)
    if output and not output[-1].endswith("\n"):
        output[-1] += "\n"
    output.extend(f"{name}={_quote(value)}\n" for name, value in values.items() if name not in written)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            os.fchmod(target.fileno(), 0o600)
            target.writelines(output)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _errors(values: dict[str, str], *, compose: bool = False) -> dict[str, str]:
    try:
        validate_config(values, compose=compose)
    except ConfigError as error:
        return error.errors
    return {}


def _prompt(values: dict[str, str], name: str, label: str, *, hidden: bool = False, compose: bool = False) -> None:
    while True:
        current = values.get(name, "")
        suffix = " [Enter to keep existing]" if current and hidden else f" [{current}]" if current else ""
        answer = (getpass if hidden else input)(f"{label}{suffix}: ")
        values[name] = answer.strip() or current
        error = _errors(values, compose=compose).get(name)
        if error is None:
            return
        print(f"{name}: {error}")


def setup_env(path: Path, *, external_db: bool = False) -> None:
    values = read_env_file(path)
    key = values.get("SECRET_KEY", "").strip()
    if key and "SECRET_KEY" in _errors(values):
        raise ConfigError({"SECRET_KEY": "invalid existing key; restore the original from backup before continuing"})
    if not key:
        if path.exists():
            print("No SECRET_KEY found. If this installation has a database, restore its original key from backup.")
            if input("Generate a key for a NEW installation only? [y/N]: ").strip().lower() not in ("y", "yes"):
                raise ConfigError(
                    {"SECRET_KEY": "restore the original key, or confirm that this is a new installation"}
                )
        values["SECRET_KEY"] = Fernet.generate_key().decode()
    _prompt(values, "TELEGRAM_BOT_TOKEN", "Telegram bot token from @BotFather", hidden=True)
    if external_db:
        _prompt(values, "DATABASE_URL", "PostgreSQL database URI", hidden=True)
    else:
        values.setdefault("POSTGRES_PASSWORD", "")
        if not values["POSTGRES_PASSWORD"]:
            values["POSTGRES_PASSWORD"] = secrets.token_urlsafe(32)
        _prompt(
            values,
            "POSTGRES_PASSWORD",
            "PostgreSQL password (Enter keeps the saved/generated password)",
            hidden=True,
            compose=True,
        )
        # Compose supplies its own URI; leave any saved native DATABASE_URL unchanged.
        values.pop("DATABASE_URL", None)
    values.setdefault("POLL_INTERVAL_SECONDS", "600")
    _prompt(values, "POLL_INTERVAL_SECONDS", "Polling interval in seconds")
    _prompt(values, "LOGIN_LOCATION", "Optional server location shown during Steam login")
    validation_values = values
    if not external_db:
        validation_values = values | {"DATABASE_URL": f"postgresql://bot:{values['POSTGRES_PASSWORD']}@db/bot"}
    validate_config(validation_values, compose=not external_db)
    names = {
        "TELEGRAM_BOT_TOKEN",
        "SECRET_KEY",
        "POSTGRES_PASSWORD",
        "DATABASE_URL",
        "POLL_INTERVAL_SECONDS",
        "LOGIN_LOCATION",
    }
    write_env_file(path, {name: value for name, value in values.items() if name in names})
    print(f"Saved private configuration to {path}.")
    print(BACKUP_WARNING)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"), help="configuration file (default: .env)")
    parser.add_argument(
        "--external-db", action="store_true", help="configure a PostgreSQL URI instead of Compose's database"
    )
    args = parser.parse_args(argv)
    try:
        setup_env(args.env_file, external_db=args.external_db)
    except ConfigError as error:
        print(str(error), file=sys.stderr)
        return 1
    except OSError:
        print(
            "Could not read or save .env. Check the path and directory permissions; existing files were preserved.",
            file=sys.stderr,
        )
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\nSetup cancelled; existing configuration was preserved.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
