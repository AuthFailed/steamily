"""Credential-free runtime report and Docker health command: python -m bot.health."""

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from time import monotonic

STATUS_PATH = Path("/tmp/steamily-health.json")
MAX_REPORT_AGE = 30


class Health:
    def __init__(self, poll_interval: int = 600):
        self.started = monotonic()
        self.intervals = {"refresh": poll_interval, "presence": 60, "delivery": 5}
        self.workers = {name: {"last_progress": self.started, "failed": False} for name in self.intervals}
        self.families: dict[str, float | None] = {}
        self.first_seen: dict[str, float] = {}
        self.last_steam_read: float | None = None
        self.in_flight = 0
        self.failed_notifications = 0
        self.last_notification_failure: float | None = None
        self.queue_health = {"pending_retry": None, "deferred": None, "queue_status": "unknown"}

    def beat(self, name: str, failed: bool | None = None) -> None:
        self.workers[name]["last_progress"] = monotonic()
        if failed is not None:
            self.workers[name]["failed"] = failed

    def expect_families(self, family_ids: list[str]) -> None:
        self.families = {key: self.families.get(key) for key in family_ids}
        self.first_seen = {key: self.first_seen.get(key, monotonic()) for key in family_ids}

    def steam_read(self, family_id: str) -> None:
        self.last_steam_read = self.families[family_id] = monotonic()
        self.first_seen.setdefault(family_id, self.last_steam_read)

    def snapshot(self, tasks: dict[str, asyncio.Task]) -> dict:
        now = monotonic()

        def age(value):
            return round(max(0, now - value), 1) if value is not None else None

        workers = {}
        for name, worker in self.workers.items():
            task = tasks[name]
            status = "ok"
            if task.done() or worker["failed"]:
                status = "failed"
            elif now - worker["last_progress"] > 2 * self.intervals[name] + 60:
                status = "stalled"
            workers[name] = {"status": status, "age_seconds": age(worker["last_progress"])}
            if task.done() and not task.cancelled() and (error := task.exception()):
                workers[name]["error_type"] = type(error).__name__
        families = [
            {
                "family_id": key,
                "last_success_age_seconds": age(last),
                "delayed": now - (last if last is not None else self.first_seen[key])
                > 2 * self.intervals["refresh"] + 60,
            }
            for key, last in sorted(self.families.items())
        ]
        return {
            "healthy": all(worker["status"] == "ok" for worker in workers.values())
            and not any(family["delayed"] for family in families)
            and self.queue_health["queue_status"] != "failed",
            "updated_at": time.time(),
            "workers": workers,
            "steam": {"last_success_age_seconds": age(self.last_steam_read), "families": families},
            "notifications": {
                "in_flight": self.in_flight,
                "failed_total": self.failed_notifications,
                "last_failure_age_seconds": age(self.last_notification_failure),
                **self.queue_health,
            },
        }

    async def monitor(self, tasks: dict[str, asyncio.Task], db) -> None:
        while True:
            try:
                counts = await asyncio.wait_for(db.alert_health(time.time()), timeout=5)
                self.queue_health = {
                    "pending_retry": counts["pending_retry"],
                    "deferred": counts["deferred"],
                    "queue_status": "ok",
                }
            except Exception as error:
                self.queue_health = {
                    "pending_retry": None,
                    "deferred": None,
                    "queue_status": "failed",
                    "queue_error_type": type(error).__name__,
                }
            write_report(self.snapshot(tasks))
            await asyncio.sleep(5)


def write_report(report: dict, path: Path = STATUS_PATH) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            json.dump(report, output)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def check(path: Path = STATUS_PATH) -> dict:
    try:
        report = json.loads(path.read_text())
        if not 0 <= time.time() - report["updated_at"] <= MAX_REPORT_AGE:
            return {"healthy": False, "reason": "runtime report is stale"}
        if not isinstance(report["healthy"], bool):
            raise ValueError("invalid health status")
        return report
    except (OSError, ValueError, TypeError, KeyError):
        return {"healthy": False, "reason": "runtime report is unavailable or invalid"}


def main() -> int:
    report = check()
    print(json.dumps(report))
    return 0 if report["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
