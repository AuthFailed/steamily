"""Pure logic over the family snapshot: new-game detection and game list filtering."""

import difflib
import re
import time
from datetime import UTC, datetime

from .i18n import t

# Sort fields and their direction when first picked: descending (True) or ascending.
SORTS = {"name": False, "price": False, "copies": True, "added": True}


def parse_budget(text: str) -> int:
    """Parse local currency units into Steam's integer hundredths without rounding."""
    text = text.strip().replace(",", ".")
    if not re.fullmatch(r"[0-9]{1,12}(?:\.[0-9]{1,2})?", text):
        raise ValueError("Invalid budget")
    whole, _, fraction = text.partition(".")
    return int(whole) * 100 + int(fraction.ljust(2, "0"))


def reconcile(previous: dict | None, snapshot: dict) -> tuple[list[dict], dict]:
    """Returns (events, new_state). State format is shared with the old Node notifier."""
    members = sorted(set(snapshot["members"]))
    if not previous:
        return [], {"members": members, "ownership": {str(a["appid"]): a["owners"] for a in snapshot["apps"]}}

    known_members = set(previous.get("members", []))
    new_members = set(members) - known_members
    ownership = {k: list(v) for k, v in previous.get("ownership", {}).items()}
    events = []
    for app in snapshot["apps"]:
        appid = str(app["appid"])
        known = set(ownership.get(appid, []))
        for owner in app["owners"]:
            if owner not in known and owner not in new_members:
                events.append({"appid": app["appid"], "name": app["name"], "owner": owner})
            known.add(owner)
        # ponytail: ownership history only grows; add removal tracking if reacquisitions must alert.
        ownership[appid] = sorted(known)
    return events, {"members": sorted(known_members | set(members)), "ownership": ownership}


def format_notification(event: dict, names: dict, lang: str) -> str:
    owner = names.get(event["owner"]) or t(lang, "steam_user", steam_id=event["owner"])
    return t(
        lang, "alert", name=event["name"], owner=owner, url=f"https://store.steampowered.com/app/{event['appid']}/"
    )


def shareable_copies(app: dict) -> int:
    return len(app["owners"]) if app.get("exclude_reason", 0) == 0 else 0


def query(
    apps: list[dict],
    store: dict,
    *,
    search: str = "",
    tag: str | None = None,
    price: str | int = "any",
    multiplayer: bool = False,
    players: list[str] = (),
    owners: list[str] = (),
    sort: str = "name",
    desc: bool = False,
) -> list[dict]:
    """Filter by ownership/sharing eligibility and copy capacity; live availability is not known."""
    details = store.get("apps", {})
    words = search.lower().split()
    high = 0 if price == "free" else price if isinstance(price, int) else None
    result = []
    for app in apps:
        info = details.get(str(app["appid"]), {})
        if words and not all(w in app["name"].lower() for w in words):
            continue
        if tag and int(tag) not in info.get("tags", []):
            continue
        if multiplayer and not info.get("multiplayer"):
            continue
        if len(app["owners"]) < len(players):
            continue
        if not shareable_copies(app) and not set(players) <= set(app["owners"]):
            continue
        if not set(owners) <= set(app["owners"]):  # "games we both have"
            continue
        if high is not None:
            cents = info.get("price")
            if cents is None or not 0 <= cents <= high:
                continue
        result.append(app)

    result.sort(key=lambda a: a["name"].lower())  # ties keep name order: sorted() is stable
    if sort == "price":

        def cents(app):
            return details.get(str(app["appid"]), {}).get("price")

        known = sorted((a for a in result if cents(a) is not None), key=cents, reverse=desc)
        return known + [a for a in result if cents(a) is None]  # unknown prices always last
    keys = {"copies": lambda a: len(a["owners"]), "added": lambda a: a.get("acquired", 0)}
    if sort in keys:
        return sorted(result, key=keys[sort], reverse=desc)
    return result[::-1] if desc else result


def tag_counts(apps: list[dict], store: dict) -> list[tuple[str, int]]:
    """Tags present in the library, most common first: the genre filter choices."""
    counts: dict[str, int] = {}
    details = store.get("apps", {})
    for app in apps:
        for tag in details.get(str(app["appid"]), {}).get("tags", []):
            counts[str(tag)] = counts.get(str(tag), 0) + 1
    names = store.get("tag_names", {})
    return sorted(((t, c) for t, c in counts.items() if t in names), key=lambda tc: -tc[1])


# Family stats. Prices are in cents of the viewer's store currency.
def price_of(store: dict, appid) -> int | None:
    return store.get("apps", {}).get(str(appid), {}).get("price")


def currency(store: dict) -> str:
    """The currency sign of a store, taken from one of its formatted prices ("$9.99", "1 999₸")."""
    for info in store.get("apps", {}).values():
        if info.get("price") and info.get("price_text"):
            return re.sub(r"[\d\s.,]", "", info["price_text"]) or "¤"
    return "¤"


def summary(apps: list[dict], store: dict) -> dict:
    priced = [price_of(store, a["appid"]) for a in apps]
    return {
        "games": len(apps),
        "copies": sum(len(a["owners"]) for a in apps),
        "shared": sum(len(a["owners"]) > 1 for a in apps),
        "value": sum(p for p in priced if p) / 100,
        "unpriced": sum(p is None for p in priced),
    }


def games_per_member(apps: list[dict], members: list[str]) -> dict[str, int]:
    return {m: sum(m in a["owners"] for a in apps) for m in members}


def value_per_member(apps: list[dict], store: dict, members: list[str]) -> dict[str, float]:
    """What each member's own copies would cost in the store today."""
    return {m: sum(price_of(store, a["appid"]) or 0 for a in apps if m in a["owners"]) / 100 for m in members}


def exclusive_per_member(apps: list[dict], members: list[str]) -> dict[str, int]:
    """Games only this member brings to the family."""
    return {m: sum(a["owners"] == [m] for a in apps) for m in members}


def added_per_month(apps: list[dict], months: int = 24, now: float | None = None) -> list[tuple[str, int]]:
    current = datetime.fromtimestamp(now or time.time(), UTC)
    keys = []
    year, month = current.year, current.month
    for _ in range(months):
        keys.append(f"{year}-{month:02}")
        year, month = (year, month - 1) if month > 1 else (year - 1, 12)
    counts = dict.fromkeys(reversed(keys), 0)
    for app in apps:
        if app.get("acquired"):
            key = datetime.fromtimestamp(app["acquired"], UTC).strftime("%Y-%m")
            if key in counts:
                counts[key] += 1
    return list(counts.items())


def borrowed_hours_per_game(playtime: list[dict], apps: list[dict], top: int = 10) -> list[tuple[str, float]]:
    names = {a["appid"]: a["name"] for a in apps}
    hours: dict[int, float] = {}
    for entry in playtime:
        hours[entry["appid"]] = hours.get(entry["appid"], 0) + entry["seconds"] / 3600
    best = sorted(hours.items(), key=lambda item: -item[1])[:top]
    return [(names.get(appid, f"App {appid}"), round(h, 1)) for appid, h in best]


def borrowed_hours_per_member(playtime: list[dict], members: list[str]) -> dict[str, float]:
    """Hours each member spent in games borrowed from the family."""
    hours = dict.fromkeys(members, 0.0)
    for entry in playtime:
        if entry["steamid"] in hours:
            hours[entry["steamid"]] += entry["seconds"] / 3600
    return {m: round(h, 1) for m, h in hours.items()}


# Group alerts: which kinds a family can turn on, and how changes between two checks become events.
ALERTS = {"games": True, "removed": False, "members": True, "online": False, "playing": False}


def presence_events(old: dict, new: dict) -> list[dict]:
    """Online/offline and started/stopped playing, between two presence checks of the same members."""
    events = []
    for steam_id, now in new.items():
        before = old.get(steam_id)
        if before is None:
            continue  # first sight of this member: nothing to compare with
        if before.get("online") != now.get("online"):
            events.append({"kind": "online", "type": "online" if now.get("online") else "offline", "steamid": steam_id})
        if before.get("game") != now.get("game"):
            if before.get("game"):
                events.append(
                    {
                        "kind": "playing",
                        "type": "stopped",
                        "steamid": steam_id,
                        "appid": before["game"],
                        "game": before.get("game_name"),
                    }
                )
            if now.get("game"):
                events.append(
                    {
                        "kind": "playing",
                        "type": "started",
                        "steamid": steam_id,
                        "appid": now["game"],
                        "game": now.get("game_name"),
                    }
                )
    return events


def family_events(old: dict, new: dict) -> list[dict]:
    """Members who joined or left the Steam family, and games no member owns any more."""
    events = [{"kind": "members", "type": "joined", "steamid": m} for m in new["members"] if m not in old["members"]]
    events += [{"kind": "members", "type": "left", "steamid": m} for m in old["members"] if m not in new["members"]]
    still_there = {a["appid"] for a in new["apps"]}
    events += [
        {"kind": "removed", "type": "removed", "appid": a["appid"], "game": a["name"]}
        for a in old["apps"]
        if a["appid"] not in still_there
    ]
    return events


def format_event(event: dict, names: dict, apps: list[dict], lang: str) -> str:
    name = names.get(event.get("steamid")) or t(lang, "steam_user", steam_id=event.get("steamid"))
    game = (
        event.get("game")
        or next((a["name"] for a in apps if a["appid"] == event.get("appid")), None)
        or f"App {event.get('appid')}"
    )
    return t(lang, f"alert_{event['type']}", name=name, game=game)


def family_matches(apps: list[dict], term: str, limit: int = 5) -> list[int]:
    """Family games for a search: names containing every word; close spellings only when nothing matches (typos)."""
    words = term.lower().split()
    if not words:  # empty search: the newest family games
        return [a["appid"] for a in sorted(apps, key=lambda a: -a.get("acquired", 0))[: limit * 2]]
    exact = [a for a in apps if all(w in a["name"].lower() for w in words)]
    exact.sort(key=lambda a: (not a["name"].lower().startswith(words[0]), len(a["name"])))
    if exact:
        return [a["appid"] for a in exact[:limit]]
    by_name = {a["name"].lower(): a["appid"] for a in apps}
    return [by_name[name] for name in difflib.get_close_matches(term.lower(), list(by_name), n=limit, cutoff=0.7)]
