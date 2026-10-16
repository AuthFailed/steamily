import os


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def poll_seconds() -> int:
    value = int(os.environ.get("POLL_INTERVAL_SECONDS", "600"))
    if value < 60:
        raise RuntimeError("POLL_INTERVAL_SECONDS must be at least 60")
    return value


# Steam asks users to pick the location nearest to the sign-in, which is where this bot's server runs,
# e.g. "Vienna, Wien, AT". Unset, the login steps describe it in general words.
LOGIN_LOCATION = os.environ.get("LOGIN_LOCATION", "").strip()
INSTANCE_OPERATOR = os.environ.get("INSTANCE_OPERATOR", "").strip()
INSTANCE_HOSTING = os.environ.get("INSTANCE_HOSTING", "").strip()
