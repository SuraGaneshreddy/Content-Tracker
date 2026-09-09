"""
Background scheduler.

A single asyncio task that wakes up every ``CHECK_INTERVAL_MINUTES`` and runs:

1. URL health checks for links not checked within the interval.
2. Update checks for items not checked within their own interval.
3. Housekeeping (trimming logs, pruning old history).

Everything is wrapped in a try/except so a single bad item can never kill the
loop, and every outbound request goes through the throttled safe fetcher.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from sqlalchemy import select

from ..config import settings
from ..database import SessionLocal
from ..models import UserSettings
from ..utils.safe_http import rate_limiter
from . import url_checker, updates

logger = logging.getLogger("tracker.scheduler")


class Scheduler:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self.last_run_at: Optional[float] = None
        self.last_run_summary: dict = {}
        self.run_count = 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if not settings.enable_scheduler:
            logger.info("Scheduler disabled by configuration.")
            return
        if self.running:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="pct-scheduler")
        logger.info("Scheduler started (every %s minutes).", settings.check_interval_minutes)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None
        logger.info("Scheduler stopped.")

    async def _loop(self) -> None:
        interval = max(1, settings.check_interval_minutes) * 60
        # Give the app a moment to boot before the first pass.
        await asyncio.sleep(min(30, interval))
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self.run_once)
            except Exception:  # pragma: no cover - defensive
                logger.exception("Scheduled run failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    def run_once(self, *, url_limit: int = 40, update_limit: int = 30) -> dict:
        """One maintenance pass. Safe to call manually ("Run checks now")."""
        started = time.monotonic()
        summary = {"url": {}, "updates": {}, "seconds": 0.0}
        rate_limiter.reset_run()

        db = SessionLocal()
        try:
            prefs = db.scalar(select(UserSettings).order_by(UserSettings.id).limit(1))
            want_urls = True if prefs is None else bool(prefs.auto_check_urls)
            want_updates = True if prefs is None else bool(prefs.auto_check_updates)

            if want_urls:
                due = url_checker.items_due_for_check(db, settings.url_health_interval_hours, limit=url_limit)
                summary["url"] = url_checker.check_many(db, due) if due else {"checked": 0}
                url_checker.prune_check_logs(db)

            if want_updates:
                summary["updates"] = updates.run_update_checks(db, limit=update_limit)
                updates.prune_history(db)

            summary["seconds"] = round(time.monotonic() - started, 2)
        except Exception as exc:  # pragma: no cover - defensive
            summary["error"] = "One or more checks could not be completed."
            logger.exception("Maintenance pass failed")
        finally:
            db.close()

        self.last_run_at = time.monotonic()
        self.last_run_summary = summary
        self.run_count += 1
        return summary

    def status(self) -> dict:
        return {
            "enabled": settings.enable_scheduler,
            "running": self.running,
            "runs": self.run_count,
            "interval_minutes": settings.check_interval_minutes,
            "last_run_seconds_ago": int(time.monotonic() - self.last_run_at) if self.last_run_at else None,
            "last_summary": self.last_run_summary,
        }


scheduler = Scheduler()
