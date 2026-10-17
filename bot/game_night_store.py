"""Durable game polls bound to the family's current Telegram group."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bot.db import Db


class GameNightStore:
    def __init__(self, db: Db):
        self.db = db

    async def create(
        self,
        family_id: str,
        telegram_id: int,
        steam_id: str,
        chat_id: int,
        starts_at: datetime,
        timezone: str,
        appids: list[int],
    ) -> dict | None:
        if (
            starts_at.utcoffset() is None
            or starts_at <= datetime.now(UTC)
            or not 2 <= len(appids) <= 5
            or len(set(appids)) != len(appids)
        ):
            return None
        if any(type(appid) is not int or not 0 < appid < 2**63 for appid in appids):
            return None
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return None
        async with self.db.pool.acquire() as connection, connection.transaction():
            family = await connection.fetchrow("SELECT * FROM families WHERE family_id = $1 FOR UPDATE", family_id)
            if not await self._allowed(connection, family, telegram_id, steam_id, chat_id):
                return None
            snapshot = (family["state"] or {}).get("observed", {})
            if not set(appids) <= {app["appid"] for app in snapshot.get("apps", [])}:
                return None
            event = await connection.fetchrow(
                """
                INSERT INTO game_nights
                    (family_id, creator_telegram_id, creator_steam_id, chat_id, link_version,
                     starts_at, timezone, appids)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING *
                """,
                family_id,
                telegram_id,
                steam_id,
                chat_id,
                family["alert_link_version"],
                starts_at,
                timezone,
                appids,
            )
            return await self._result(connection, event, family)

    async def get(self, event_id: int, chat_id: int) -> dict | None:
        async with self.db.pool.acquire() as connection, connection.transaction():
            event, family = await self._load(connection, event_id, chat_id)
            return await self._result(connection, event, family) if event else None

    async def attach(self, event_id: int, message_id: int) -> None:
        await self.db.pool.execute(
            "UPDATE game_nights SET message_id = $2 WHERE id = $1 AND message_id IS NULL", event_id, message_id
        )

    async def act(
        self,
        event_id: int,
        telegram_id: int,
        steam_id: str,
        chat_id: int,
        message_id: int,
        action: str,
        value=None,
    ) -> dict | None:
        if action not in {"vote", "rsvp", "close", "cancel"}:
            return None
        if action == "rsvp" and value not in {"going", "maybe", "cannot"}:
            return None
        async with self.db.pool.acquire() as connection, connection.transaction():
            event, family = await self._load(connection, event_id, chat_id)
            if event is None or event["message_id"] != message_id:
                return None
            if not await self._allowed(connection, family, telegram_id, steam_id, chat_id):
                return None
            if event["state"] != "open":
                return await self._result(connection, event, family)
            if action in {"close", "cancel"}:
                if event["creator_telegram_id"] != telegram_id or event["creator_steam_id"] != steam_id:
                    return None
                chosen = (await self._result(connection, event, family))["chosen_appid"] if action == "close" else None
                current_appids = {app["appid"] for app in (family["state"] or {}).get("observed", {}).get("apps", [])}
                if action == "close" and chosen not in current_appids:
                    return None
                event = await connection.fetchrow(
                    "UPDATE game_nights SET state = $2, chosen_appid = $3 WHERE id = $1 RETURNING *",
                    event_id,
                    "closed" if action == "close" else "cancelled",
                    chosen,
                )
            else:
                current_appids = {app["appid"] for app in (family["state"] or {}).get("observed", {}).get("apps", [])}
                if action == "vote" and (
                    type(value) is not int or value not in event["appids"] or value not in current_appids
                ):
                    return None
                column = "vote" if action == "vote" else "rsvp"
                other = "rsvp" if action == "vote" else "vote"
                await connection.execute(
                    f"""
                    INSERT INTO game_night_participants (event_id, telegram_id, steam_id, {column})
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (event_id, telegram_id) DO UPDATE SET
                        {other} = CASE WHEN game_night_participants.steam_id = $3
                            THEN game_night_participants.{other} ELSE NULL END,
                        steam_id = $3, {column} = $4
                    """,
                    event_id,
                    telegram_id,
                    steam_id,
                    value,
                )
            return await self._result(connection, event, family)

    async def _load(self, connection, event_id: int, chat_id: int):
        family_id = await connection.fetchval(
            "SELECT family_id FROM game_nights WHERE id = $1 AND chat_id = $2", event_id, chat_id
        )
        if family_id is None:
            return None, None
        family = await connection.fetchrow("SELECT * FROM families WHERE family_id = $1 FOR UPDATE", family_id)
        event = await connection.fetchrow("SELECT * FROM game_nights WHERE id = $1 FOR UPDATE", event_id)
        if (
            family is None
            or event is None
            or family["notify_chat"] != chat_id
            or family["alert_link_version"] != event["link_version"]
        ):
            return None, None
        return event, family

    async def _allowed(self, connection, family, telegram_id: int, steam_id: str, chat_id: int) -> bool:
        if family is None or family["notify_chat"] != chat_id:
            return False
        if steam_id not in (family["state"] or {}).get("observed", {}).get("members", []):
            return False
        account = await connection.fetchrow("SELECT * FROM accounts WHERE telegram_id = $1 FOR UPDATE", telegram_id)
        return bool(
            account
            and account["family_id"] == family["family_id"]
            and account["steam_id"] == steam_id
            and account["status"] == "approved"
            and account["refresh_token"]
        )

    async def _result(self, connection, event, family) -> dict:
        snapshot = (family["state"] or {}).get("observed", {})
        participants = await connection.fetch(
            """
            SELECT p.telegram_id, p.steam_id, p.vote, p.rsvp
            FROM game_night_participants p JOIN accounts a USING (telegram_id)
            WHERE p.event_id = $1 AND a.family_id = $2 AND a.steam_id = p.steam_id
                AND a.status = 'approved' AND a.refresh_token IS NOT NULL AND a.steam_id = ANY($3::text[])
            ORDER BY p.telegram_id
            """,
            event["id"],
            event["family_id"],
            snapshot.get("members", []),
        )
        votes = {appid: 0 for appid in event["appids"]}
        for participant in participants:
            if participant["vote"] in votes:
                votes[participant["vote"]] += 1
        return {
            **dict(event),
            "language": family["language"],
            "snapshot": snapshot,
            "participants": [dict(participant) for participant in participants],
            "chosen_appid": (max(votes, key=votes.get) if any(votes.values()) else None)
            if event["state"] == "open"
            else event["chosen_appid"],
        }
