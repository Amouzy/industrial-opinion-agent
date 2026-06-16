from __future__ import annotations

import logging

try:
    import uvicorn
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
except ImportError as exc:  # pragma: no cover - shown when dependencies are missing.
    raise RuntimeError("FastAPI dependencies are not installed. Run `pip install -r requirements.txt`.") from exc

from app.api import create_router
from app.config import get_settings
from app.database import Database
from app.logging_config import APP_LOGGER_NAME, configure_logging
from app.scheduler import start_scheduler
from app.services.workflow import ensure_seed_data, recover_interrupted_runs


settings = get_settings()
configure_logging()
logger = logging.getLogger(APP_LOGGER_NAME)
db = Database(settings.database_path)
db.init()
recovered_runs = recover_interrupted_runs(db)
if recovered_runs:
    logger.warning("startup_recovered_interrupted_runs count=%s", recovered_runs)
if settings.seed_on_start:
    ensure_seed_data(db)
logger.info(
    "app_startup database=%s scheduler_enabled=%s timezone=%s",
    settings.database_path,
    settings.scheduler_enabled,
    settings.app_timezone,
)

app = FastAPI(title="Industrial Opinion Agent V2", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(create_router(db))
scheduler = start_scheduler(db, timezone=settings.app_timezone, enabled=settings.scheduler_enabled)


@app.get("/health")
def health() -> dict[str, object]:
    return {"ok": True, "scheduler": scheduler is not None}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
