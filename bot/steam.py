import json
import re
import time

import aiohttp

API = "https://api.steampowered.com"
STORE_BATCH = 50
STORE_MAX_AGE = 24 * 60 * 60
TOP_TAGS = 5
# Steam store category ids meaning "more than one player" (multi-player, co-op, PvP, split screen, LAN, MMO...).
MULTIPLAYER_CATEGORIES = {1, 9, 20, 24, 27, 36, 37, 38, 39, 47, 48, 49}


class NoFamily(RuntimeError):
    pass


async def get_json(http: aiohttp.ClientSession, method: str, params: dict) -> dict:
    async with http.get(f"{API}/{method}/", params=params) as response:
        if response.status != 200:
            raise RuntimeError(f"Steam {method} failed with HTTP {response.status}")
        return await response.json(content_type=None)


async def fetch_family_snapshot(http: aiohttp.ClientSession, access_token: str) -> dict:
    family = await get_json(
        http,
        "IFamilyGroupsService/GetFamilyGroupForUser/v1",
        {
            "access_token": access_token,
            "include_family_group_response": "true",
        },
    )
    group_id = family.get("response", {}).get("family_groupid")
    members = family.get("response", {}).get("family_group", {}).get("members")
    if not group_id or not isinstance(members, list):
        raise NoFamily("This Steam account is not joined to a Steam Family")

    library = await get_json(
        http,
        "IFamilyGroupsService/GetSharedLibraryApps/v1",
        {
            "access_token": access_token,
            "family_groupid": group_id,
            "include_own": "true",
            "include_excluded": "true",
            "include_free": "false",
            "include_non_games": "false",
            "language": "english",
        },
    )
    players = {f"steamids[{i}]": m["steamid"] for i, m in enumerate(members)}
    try:
        players = await get_json(
            http, "IPlayerService/GetPlayerLinkDetails/v1", {"access_token": access_token, **players}
        )
    except (aiohttp.ClientError, RuntimeError):
        players = {}
    snapshot = parse_snapshot(family, library, players)
    try:  # who played which borrowed game, for stats; optional
        playtime = await post_json(
            http,
            "IFamilyGroupsService/GetPlaytimeSummary/v1",
            {"access_token": access_token, "family_groupid": group_id},
        )
        snapshot["playtime"] = [
            {"steamid": str(e["steamid"]), "appid": e["appid"], "seconds": int(e.get("seconds_played", 0))}
            for e in playtime.get("response", {}).get("entries", [])
        ]
    except (aiohttp.ClientError, RuntimeError):
        pass
    return snapshot


async def post_json(http: aiohttp.ClientSession, method: str, params: dict) -> dict:
    """POST-only WebAPI methods. Steam explains refusals in the x-error_message header."""
    async with http.post(f"{API}/{method}/", data=params) as response:
        eresult = response.headers.get("x-eresult", "1")
        if response.status != 200 or eresult != "1":
            detail = response.headers.get("x-error_message") or f"HTTP {response.status}, EResult {eresult}"
            raise SteamActionError(detail)
        return await response.json(content_type=None)


class SteamActionError(RuntimeError):
    pass


# Changes to the Steam family itself. They run on the acting user's own Steam account.
async def invite_member(http, access_token: str, family_id: str, steam_id: str, role: int) -> dict:
    return await post_json(
        http,
        "IFamilyGroupsService/InviteToFamilyGroup/v1",
        {
            "access_token": access_token,
            "family_groupid": family_id,
            "receiver_steamid": steam_id,
            "receiver_role": role,
        },
    )


async def cancel_invite(http, access_token: str, family_id: str, steam_id: str) -> None:
    await post_json(
        http,
        "IFamilyGroupsService/CancelFamilyGroupInvite/v1",
        {"access_token": access_token, "family_groupid": family_id, "steamid_to_cancel": steam_id},
    )


async def remove_member(http, access_token: str, family_id: str, steam_id: str) -> None:
    await post_json(
        http,
        "IFamilyGroupsService/RemoveFromFamilyGroup/v1",
        {"access_token": access_token, "family_groupid": family_id, "steamid_to_remove": steam_id},
    )


async def rename_family(http, access_token: str, family_id: str, name: str) -> None:
    await post_json(
        http,
        "IFamilyGroupsService/ModifyFamilyGroupDetails/v1",
        {"access_token": access_token, "family_groupid": family_id, "name": name},
    )


async def purchase_requests(http, access_token: str, family_id: str) -> list[dict]:
    data = await get_json(
        http, "IFamilyGroupsService/GetPurchaseRequests/v1", {"access_token": access_token, "family_groupid": family_id}
    )
    return [r for r in data.get("response", {}).get("requests", []) if not r.get("is_completed")]


async def decline_purchase(http, access_token: str, family_id: str, request_id: str) -> None:
    await post_json(
        http,
        "IFamilyGroupsService/RespondToRequestedPurchase/v1",
        {"access_token": access_token, "family_groupid": family_id, "request_id": request_id, "action": 1},
    )  # k_EPurchaseRequestAction_Decline


async def member_stats(http, access_token: str, steam_id: str) -> dict:
    """Steam level, games owned and total playtime; the game numbers are missing for private profiles."""
    level = await get_json(http, "IPlayerService/GetSteamLevel/v1", {"access_token": access_token, "steamid": steam_id})
    owned = await get_json(
        http,
        "IPlayerService/GetOwnedGames/v1",
        {"access_token": access_token, "steamid": steam_id, "include_appinfo": "false"},
    )
    games = owned.get("response", {}).get("games")
    return {
        "level": level.get("response", {}).get("player_level"),
        "games": owned.get("response", {}).get("game_count"),
        "hours": round(sum(g.get("playtime_forever", 0) for g in games) / 60) if games else None,
    }


async def resolve_profile(http: aiohttp.ClientSession, text: str) -> str | None:
    """SteamID64 from a SteamID64, a /profiles/ link or a /id/<name> link (public profile XML, no API key)."""
    text = text.strip().rstrip("/")
    if match := re.fullmatch(r"(?:https?://steamcommunity\.com/profiles/)?(7656119\d{10})", text):
        return match.group(1)
    match = re.fullmatch(r"(?:https?://steamcommunity\.com/id/)?([A-Za-z0-9_-]{2,32})", text)
    if not match:
        return None
    async with http.get(f"https://steamcommunity.com/id/{match.group(1)}/", params={"xml": "1"}) as response:
        found = re.search(r"<steamID64>(\d{17})</steamID64>", await response.text())
    return found.group(1) if found else None


def profile_url(steam_id: str, players: dict) -> str:
    vanity = players.get(steam_id, {}).get("vanity")
    return f"https://steamcommunity.com/id/{vanity}/" if vanity else f"https://steamcommunity.com/profiles/{steam_id}/"


async def fetch_user_country(http: aiohttp.ClientSession, access_token: str, steam_id: str) -> str | None:
    """The account's Steam store country, which decides its price currency."""
    try:
        async with http.post(
            f"{API}/IUserAccountService/GetUserCountry/v1/", data={"access_token": access_token, "steamid": steam_id}
        ) as response:
            if response.status != 200:
                return None
            return (await response.json(content_type=None)).get("response", {}).get("country") or None
    except aiohttp.ClientError:  # prices then fall back to the default country; never block a login on this
        return None


def parse_snapshot(family: dict, library: dict, players: dict) -> dict:
    members = family.get("response", {}).get("family_group", {}).get("members")
    apps = library.get("response", {}).get("apps")
    if not isinstance(members, list):
        raise NoFamily("This Steam account is not joined to a Steam Family")
    if not isinstance(apps, list):
        raise RuntimeError("Steam returned an incomplete family-library response")

    names, profiles = parse_players(players)
    group = family["response"].get("family_group", {})
    return {
        "family_id": str(family["response"].get("family_groupid", "")),
        "name": group.get("name", ""),
        "members": [str(m["steamid"]) for m in members],
        "joined": {str(m["steamid"]): m.get("time_joined") for m in members},
        "roles": {str(m["steamid"]): m.get("role", 1) for m in members},
        "free_spots": group.get("free_spots", 0),
        "pending_invites": [
            {"steamid": str(i["steamid"]), "role": i.get("role", 1)} for i in group.get("pending_invites", [])
        ],
        "names": names,
        "players": profiles,
        "playtime": [],
        "apps": [
            {
                "appid": app["appid"],
                "name": app.get("name") or f"App {app['appid']}",
                "owners": sorted({str(o) for o in app.get("owner_steamids", [])}),
                "acquired": app.get("rt_time_acquired", 0),
            }
            for app in apps
        ],
    }


def parse_players(players: dict) -> tuple[dict, dict]:
    """GetPlayerLinkDetails: names, plus profile and presence (online, current game) per SteamID."""
    names, profiles = {}, {}
    for account in players.get("response", {}).get("accounts", []):
        public, private = account.get("public_data") or {}, account.get("private_data") or {}
        if not public.get("steamid"):
            continue
        steam_id = str(public["steamid"])
        if public.get("persona_name"):
            names[steam_id] = public["persona_name"]
        game = int(private.get("game_id") or 0)
        profiles[steam_id] = {
            "vanity": public.get("profile_url") or None,
            "online": private.get("persona_state", 0) > 0,
            "created": private.get("time_created"),
            "last_online": private.get("last_seen_online"),
            "game": game if 0 < game < 2**32 else None,  # bigger ids are non-Steam shortcuts
            "game_name": private.get("game_extra_info") or None,
        }
    return names, profiles


async def fetch_presence(http: aiohttp.ClientSession, access_token: str, members: list[str]) -> dict:
    data = await get_json(
        http,
        "IPlayerService/GetPlayerLinkDetails/v1",
        {"access_token": access_token, **{f"steamids[{i}]": m for i, m in enumerate(members)}},
    )
    return parse_players(data)[1]


STORE_LANGUAGES = {"ru": "russian", "en": "english"}
ASSETS = "https://shared.akamai.steamstatic.com/store_item_assets/"


async def search_store(http: aiohttp.ClientSession, term: str, lang: str, country: str) -> list[int]:
    """Steam's own store search: fuzzy and typo tolerant, best matches first."""
    async with http.get(
        "https://store.steampowered.com/api/storesearch/",
        params={"term": term, "l": STORE_LANGUAGES.get(lang, "english"), "cc": country},
    ) as response:
        if response.status != 200:
            return []
        data = await response.json(content_type=None)
    return [item["id"] for item in data.get("items", []) if item.get("type") == "app"]


async def store_cards(http: aiohttp.ClientSession, appids: list[int], lang: str, country: str) -> dict[int, dict]:
    """Name, localized short description, price in the country's currency and cover pictures."""
    if not appids:
        return {}
    request = {
        "ids": [{"appid": a} for a in appids],
        "context": {"language": STORE_LANGUAGES.get(lang, "english"), "country_code": country},
        "data_request": {"include_basic_info": True, "include_assets": True},
    }
    data = await get_json(http, "IStoreBrowseService/GetItems/v1", {"input_json": json.dumps(request)})
    cards = {}
    for item in data.get("response", {}).get("store_items", []):
        if not item.get("appid") or not item.get("name"):
            continue
        assets = item.get("assets") or {}
        url_format, capsule = assets.get("asset_url_format"), assets.get("small_capsule")
        option = item.get("best_purchase_option") or {}
        cards[item["appid"]] = {
            "name": item["name"],
            "description": (item.get("basic_info") or {}).get("short_description", ""),
            "price_text": "Free" if item.get("is_free") else option.get("formatted_final_price"),
            "thumbnail": ASSETS + url_format.replace("${FILENAME}", capsule) if url_format and capsule else None,
        }
    return cards


def parse_store_item(item: dict) -> dict:
    option = item.get("best_purchase_option") or {}
    cents = option.get("final_price_in_cents")
    tags = sorted(item.get("tags", []), key=lambda t: -t.get("weight", 0))
    categories = set(item.get("categories", {}).get("supported_player_categoryids", []))
    return {
        "fetched": time.time(),
        "price": 0 if item.get("is_free") else (int(cents) if cents is not None else None),
        "price_text": "Free" if item.get("is_free") else option.get("formatted_final_price"),
        "tags": [t["tagid"] for t in tags[:TOP_TAGS]],
        "multiplayer": bool(categories & MULTIPLAYER_CATEGORIES),
    }


async def update_store_cache(http: aiohttp.ClientSession, cache: dict, appids: list[int], country: str) -> bool:
    """Refreshes stale store details (price, tags, categories) in place; returns True if anything changed."""
    apps = cache.setdefault("apps", {})
    now = time.time()
    stale = [a for a in appids if now - apps.get(str(a), {}).get("fetched", 0) > STORE_MAX_AGE]
    for start in range(0, len(stale), STORE_BATCH):
        request = {
            "ids": [{"appid": a} for a in stale[start : start + STORE_BATCH]],
            "context": {"language": "english", "country_code": country},
            "data_request": {"include_basic_info": True, "include_tag_count": 20},
        }
        data = await get_json(http, "IStoreBrowseService/GetItems/v1", {"input_json": json.dumps(request)})
        items = {str(i.get("appid")): i for i in data.get("response", {}).get("store_items", [])}
        for appid in stale[start : start + STORE_BATCH]:
            # Delisted apps come back empty; cache them anyway so they are not refetched every poll.
            apps[str(appid)] = parse_store_item(items.get(str(appid), {}))

    if stale or not cache.get("tag_names"):
        data = await get_json(http, "IStoreService/GetTagList/v1", {"language": "english"})
        cache["tag_names"] = {str(t["tagid"]): t["name"] for t in data.get("response", {}).get("tags", [])}
    return bool(stale)
