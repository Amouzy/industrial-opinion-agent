from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class LLMSettings:
    provider: str
    model: str
    api_key: str
    base_url: str
    timeout_seconds: int
    temperature: float


@dataclass(frozen=True)
class Settings:
    database_path: Path
    cors_origins: list[str]
    seed_on_start: bool
    scheduler_enabled: bool
    app_timezone: str
    llm: LLMSettings


def get_settings() -> Settings:
    root = Path(__file__).resolve().parents[1]
    env_file = Path(os.getenv("ENV_FILE", root / ".env"))
    if env_file.exists():
        load_dotenv(env_file, override=False)
    database_path = _resolve_database_path(os.getenv("DATABASE_PATH"), root)
    cors_origins = [
        item.strip()
        for item in os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")
        if item.strip()
    ]
    return Settings(
        database_path=database_path,
        cors_origins=cors_origins,
        seed_on_start=os.getenv("SEED_ON_START", "1") != "0",
        scheduler_enabled=os.getenv("SCHEDULER_ENABLED", "1") != "0",
        app_timezone=os.getenv("APP_TIMEZONE", "Asia/Shanghai"),
        llm=LLMSettings(
            provider=os.getenv("LLM_PROVIDER", "openai").strip() or "openai",
            model=os.getenv("LLM_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini",
            api_key=os.getenv("LLM_API_KEY", "").strip(),
            base_url=os.getenv("LLM_BASE_URL", "").strip() or "https://api.openai.com/v1",
            timeout_seconds=int(os.getenv("LLM_TIMEOUT_SECONDS", "60")),
            temperature=float(os.getenv("LLM_TEMPERATURE", "0")),
        ),
    )


def _resolve_database_path(value: str | None, root: Path) -> Path:
    if not value:
        return root / "data" / "opinion_agent.sqlite3"
    path = Path(value)
    if path.is_absolute():
        return path
    if path.parts and path.parts[0] == root.name:
        return root.parent / path
    return root / path
