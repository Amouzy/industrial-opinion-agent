from __future__ import annotations

import os
import sqlite3
import logging
import time
from datetime import datetime, timedelta, timezone
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from app.database import Database
from app.logging_config import SCHEDULER_LOGGER_NAME
from app.services.briefs import generate_brief
from app.services.workflow import run_source_interval_scan


_RUNNING_SCHEDULERS: dict[tuple[str, str], Any] = {}
_SCHEDULER_LEASE_SECONDS = 120
logger = logging.getLogger(SCHEDULER_LOGGER_NAME)


def start_scheduler(db: Database, timezone: str = "Asia/Shanghai", enabled: bool = True) -> Any:
    if not enabled:
        return None
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except Exception as exc:
        logger.warning("scheduler_unavailable error=%s", exc, exc_info=True)
        return None

    key = (str(db.path.resolve()), timezone)
    existing = _RUNNING_SCHEDULERS.get(key)
    if existing is not None:
        if getattr(existing, "running", False):
            return existing
        _RUNNING_SCHEDULERS.pop(key, None)

    owner_id = f"{os.getpid()}-{uuid4()}"
    if not _acquire_scheduler_lease(db, owner_id):
        logger.info("scheduler_lease_refused owner_id=%s database=%s", owner_id, db.path)
        return None

    scheduler = BackgroundScheduler(timezone=timezone)
    scheduler.add_job(
        lambda: _run_logged_job(
            "source_interval_scan",
            lambda: run_source_interval_scan(db, trigger_type="source_interval_scan"),
            trigger_type="source_interval_scan",
            force_all=False,
        ),
        "interval",
        minutes=10,
        id="source_interval_scan",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        lambda: _run_logged_job(
            "scheduled_monitor_every_2h",
            lambda: run_source_interval_scan(db, trigger_type="scheduled_monitor", force_all=True),
            trigger_type="scheduled_monitor",
            force_all=True,
        ),
        "cron",
        day_of_week="mon-fri",
        hour="9,11,13,15,17",
        minute=0,
        id="scheduled_monitor_every_2h",
        replace_existing=True,
    )
    scheduler.add_job(
        lambda: _run_logged_job(
            "high_authority_hourly",
            lambda: run_source_interval_scan(db, trigger_type="high_authority_monitor"),
            trigger_type="high_authority_monitor",
            force_all=False,
        ),
        "cron",
        day_of_week="mon-fri",
        hour="9-18",
        minute=30,
        id="high_authority_hourly",
        replace_existing=True,
    )
    scheduler.add_job(
        lambda: _run_logged_job(
            "scheduled_morning_collect",
            lambda: run_source_interval_scan(db, trigger_type="scheduled_morning", force_all=True),
            trigger_type="scheduled_morning",
            force_all=True,
        ),
        "cron",
        hour=8,
        minute=20,
        id="scheduled_morning_collect",
        replace_existing=True,
    )
    scheduler.add_job(
        lambda: _run_logged_job(
            "scheduled_morning_brief",
            lambda: generate_brief(db, brief_type="morning"),
            trigger_type="scheduled_morning_brief",
        ),
        "cron",
        hour=8,
        minute=30,
        id="scheduled_morning_brief",
        replace_existing=True,
    )
    scheduler.add_job(
        lambda: _renew_scheduler_lease(db, owner_id),
        "interval",
        seconds=max(15, _SCHEDULER_LEASE_SECONDS // 3),
        id="scheduler_lease_heartbeat",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _patch_scheduler_shutdown(scheduler, db, owner_id, key)
    scheduler.start()
    _RUNNING_SCHEDULERS[key] = scheduler
    logger.info("scheduler_started owner_id=%s timezone=%s database=%s", owner_id, timezone, db.path)
    return scheduler


def _run_logged_job(job_id: str, job: Callable[[], Any], **context: Any) -> Any:
    started = time.perf_counter()
    context_message = _format_log_context(context)
    logger.info("job_start job_id=%s%s", job_id, context_message)
    try:
        result = job()
    except Exception:
        logger.exception("job_error job_id=%s%s duration_seconds=%.3f", job_id, context_message, time.perf_counter() - started)
        raise
    logger.info(
        "job_finish job_id=%s%s duration_seconds=%.3f%s",
        job_id,
        context_message,
        time.perf_counter() - started,
        _format_job_result(result),
    )
    return result


def _format_log_context(context: dict[str, Any]) -> str:
    if not context:
        return ""
    return " " + " ".join(f"{key}={value}" for key, value in context.items())


def _format_job_result(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    fields = {
        "run_id": result.get("id"),
        "status": result.get("status"),
        "collected": result.get("collected_count"),
        "deduped": result.get("deduped_count"),
        "classified": result.get("classified_count"),
        "extracted": result.get("extracted_count"),
        "failed": result.get("failed_count"),
    }
    return " " + " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)


def _lease_times() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    return now.isoformat(), (now + timedelta(seconds=_SCHEDULER_LEASE_SECONDS)).isoformat()


def _acquire_scheduler_lease(db: Database, owner_id: str) -> bool:
    now_iso, expires_iso = _lease_times()
    with db.connect() as conn:
        try:
            cursor = conn.execute(
                """
                INSERT INTO scheduler_locks (name, owner_id, acquired_at, heartbeat_at, expires_at)
                VALUES ('main', ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    owner_id = excluded.owner_id,
                    acquired_at = excluded.acquired_at,
                    heartbeat_at = excluded.heartbeat_at,
                    expires_at = excluded.expires_at
                WHERE scheduler_locks.expires_at <= ?
                """,
                (owner_id, now_iso, now_iso, expires_iso, now_iso),
            )
        except sqlite3.OperationalError as exc:
            logger.warning("scheduler_lease_acquire_failed owner_id=%s database=%s error=%s", owner_id, db.path, exc, exc_info=True)
            return False
    return cursor.rowcount > 0


def _renew_scheduler_lease(db: Database, owner_id: str) -> None:
    now_iso, expires_iso = _lease_times()
    db.execute(
        """
        UPDATE scheduler_locks
        SET heartbeat_at = ?, expires_at = ?
        WHERE name = 'main' AND owner_id = ?
        """,
        (now_iso, expires_iso, owner_id),
    )


def _release_scheduler_lease(db: Database, owner_id: str) -> None:
    db.execute("DELETE FROM scheduler_locks WHERE name = 'main' AND owner_id = ?", (owner_id,))


def _patch_scheduler_shutdown(scheduler: Any, db: Database, owner_id: str, key: tuple[str, str]) -> None:
    original_shutdown = scheduler.shutdown

    def shutdown_with_lease_release(*args: Any, **kwargs: Any) -> Any:
        try:
            return original_shutdown(*args, **kwargs)
        finally:
            _RUNNING_SCHEDULERS.pop(key, None)
            _release_scheduler_lease(db, owner_id)

    scheduler.shutdown = shutdown_with_lease_release
