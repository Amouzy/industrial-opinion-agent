from __future__ import annotations

from typing import Any

from app.database import Database
from app.services.briefs import generate_brief
from app.services.workflow import run_source_interval_scan


def start_scheduler(db: Database, timezone: str = "Asia/Shanghai", enabled: bool = True) -> Any:
    if not enabled:
        return None
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except Exception:
        return None

    scheduler = BackgroundScheduler(timezone=timezone)
    scheduler.add_job(
        lambda: run_source_interval_scan(db, trigger_type="source_interval_scan"),
        "interval",
        minutes=10,
        id="source_interval_scan",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        lambda: run_source_interval_scan(db, trigger_type="scheduled_monitor", force_all=True),
        "cron",
        day_of_week="mon-fri",
        hour="9,11,13,15,17",
        minute=0,
        id="scheduled_monitor_every_2h",
        replace_existing=True,
    )
    scheduler.add_job(
        lambda: run_source_interval_scan(db, trigger_type="high_authority_monitor"),
        "cron",
        day_of_week="mon-fri",
        hour="9-18",
        minute=30,
        id="high_authority_hourly",
        replace_existing=True,
    )
    scheduler.add_job(
        lambda: run_source_interval_scan(db, trigger_type="scheduled_morning", force_all=True),
        "cron",
        hour=8,
        minute=20,
        id="scheduled_morning_collect",
        replace_existing=True,
    )
    scheduler.add_job(
        lambda: generate_brief(db, brief_type="morning"),
        "cron",
        hour=8,
        minute=30,
        id="scheduled_morning_brief",
        replace_existing=True,
    )
    scheduler.start()
    return scheduler
