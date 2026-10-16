"""Inline mode: `@bot game name` in any chat finds games and posts a card in the sender's language and currency."""

import asyncio
import logging
from html import escape

from aiogram import Router
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultsButton,
    InputTextMessageContent,
    LinkPreviewOptions,
)

from .db import Db
from .family import DEFAULT_COUNTRY, Families, accounts_by_steam, person_link, role
from .i18n import DEFAULT, t
from .library import family_matches
from .steam import search_store, store_cards

log = logging.getLogger(__name__)
router = Router()
MAX_RESULTS = 15
STORE_TIMEOUT = 2  # two optional Store calls must leave time to answer the inline query


def card_text(lang: str, name: str, description: str, price: str | None, owners: list[str], url: str) -> str:
    """owners: ready HTML links, one list line each."""
    owners_line = (
        t(lang, "inline_owners", names="".join(f"\n• {o}" for o in owners))
        if owners
        else t(lang, "inline_not_in_family")
    )
    lines = [
        f"🎮 <b>{escape(name)}</b>",
        escape(description) if description else None,
        "",
        f"💲 {escape(price or t(lang, 'price_unknown'))}",
        owners_line,
        "",
        url,
    ]
    return "\n".join(line for line in lines if line is not None)


@router.inline_query()
async def search(query: InlineQuery, db: Db, families: Families):
    lang = await db.language(query.from_user.id) or (
        "ru" if (query.from_user.language_code or "").startswith("ru") else DEFAULT
    )
    account = await db.account(query.from_user.id)
    family = families.items.get(account["family_id"]) if account else None
    if role(account) not in ("member", "owner") or family is None or family.snapshot is None:
        await query.answer(
            [],
            cache_time=5,
            is_personal=True,
            button=InlineQueryResultsButton(text=t(lang, "inline_login"), start_parameter="login"),
        )
        return

    country = account["country"] or DEFAULT_COUNTRY
    term = query.query.strip()[:100]
    in_family = family_matches(family.apps, term)
    try:
        async with asyncio.timeout(STORE_TIMEOUT):
            from_store = await search_store(families.http, term, lang, country) if term else []
    except Exception as error:
        log.warning("Store search for inline query failed (%s)", type(error).__name__)
        from_store = []
    appids = list(dict.fromkeys(in_family + from_store))[:MAX_RESULTS]
    try:
        async with asyncio.timeout(STORE_TIMEOUT):
            cards = await store_cards(families.http, appids, lang, country)
    except Exception as error:  # still answer with what the family library knows
        log.warning("Store details for inline search failed (%s)", type(error).__name__)
        cards = {}

    names = {a["appid"]: a["name"] for a in family.apps}
    accounts = await accounts_by_steam(db, family.family_id)
    owners = {a["appid"]: a["owners"] for a in family.apps}
    results = []
    for appid in appids:
        card = cards.get(appid) or {"name": names.get(appid), "description": "", "price_text": None, "thumbnail": None}
        if not card["name"]:
            continue
        url = f"https://store.steampowered.com/app/{appid}/"
        who = owners.get(appid, [])
        results.append(
            InlineQueryResultArticle(
                id=str(appid),
                title=card["name"],
                description="\n".join(
                    filter(
                        None,
                        [
                            f"👥 {', '.join(family.name(o) for o in who)}"
                            if who
                            else t(lang, "inline_not_in_family_short"),
                            card["price_text"],
                            card["description"][:120],
                        ],
                    )
                ),
                thumbnail_url=card["thumbnail"],
                input_message_content=InputTextMessageContent(
                    message_text=card_text(
                        lang,
                        card["name"],
                        card["description"],
                        card["price_text"],
                        [person_link(family, o, accounts) for o in who],
                        url,
                    ),
                    parse_mode="HTML",
                    link_preview_options=LinkPreviewOptions(url=url, prefer_large_media=True),
                ),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text=t(lang, "btn_open_store"), url=url)]]
                ),
            )
        )
    await query.answer(results, cache_time=60, is_personal=True)
