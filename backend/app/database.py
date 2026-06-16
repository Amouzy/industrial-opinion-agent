from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    industry_hint TEXT,
    reliability_score REAL DEFAULT 0.5,
    enabled INTEGER DEFAULT 1,
    fetch_interval_minutes INTEGER DEFAULT 120,
    last_checked_at TEXT,
    last_fetched_at TEXT,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS raw_items (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL,
    url TEXT NOT NULL UNIQUE,
    canonical_url TEXT,
    title TEXT NOT NULL,
    author TEXT,
    published_at TEXT,
    fetched_at TEXT NOT NULL,
    raw_content TEXT,
    content_excerpt TEXT,
    content_hash TEXT,
    status TEXT DEFAULT 'new',
    relevance_industry TEXT,
    relevance_confidence REAL,
    relevance_reason TEXT,
    relevance_matched_terms_json TEXT,
    relevance_provider TEXT,
    relevance_model TEXT,
    FOREIGN KEY(source_id) REFERENCES sources(id)
);

CREATE TABLE IF NOT EXISTS event_clusters (
    id INTEGER PRIMARY KEY,
    canonical_title TEXT NOT NULL,
    representative_item_id INTEGER,
    duplicate_count INTEGER DEFAULT 1,
    first_seen_at TEXT,
    last_seen_at TEXT,
    similarity_reason TEXT
);

CREATE TABLE IF NOT EXISTS processed_items (
    id INTEGER PRIMARY KEY,
    raw_item_id INTEGER NOT NULL UNIQUE,
    canonical_event_id INTEGER,
    normalized_title TEXT NOT NULL,
    summary TEXT,
    key_facts_json TEXT,
    entities_json TEXT,
    impact_analysis TEXT,
    importance_level TEXT,
    importance_reason TEXT,
    rank_score REAL DEFAULT 0,
    score_breakdown_json TEXT,
    rank_reason TEXT,
    source_spans_json TEXT,
    llm_provider TEXT,
    llm_model TEXT,
    extraction_provider TEXT,
    extraction_model TEXT,
    extraction_mode TEXT,
    extraction_fallback_reason TEXT,
    confidence REAL DEFAULT 0,
    created_at TEXT NOT NULL,
    workflow_run_id INTEGER,
    FOREIGN KEY(raw_item_id) REFERENCES raw_items(id),
    FOREIGN KEY(canonical_event_id) REFERENCES event_clusters(id),
    FOREIGN KEY(workflow_run_id) REFERENCES workflow_runs(id)
);

CREATE TABLE IF NOT EXISTS item_tags (
    id INTEGER PRIMARY KEY,
    processed_item_id INTEGER NOT NULL,
    tag_dimension TEXT NOT NULL,
    tag_value TEXT NOT NULL,
    confidence REAL DEFAULT 0,
    evidence_json TEXT,
    FOREIGN KEY(processed_item_id) REFERENCES processed_items(id)
);

CREATE TABLE IF NOT EXISTS briefs (
    id INTEGER PRIMARY KEY,
    brief_type TEXT NOT NULL,
    time_range_start TEXT NOT NULL,
    time_range_end TEXT NOT NULL,
    title TEXT NOT NULL,
    content_markdown TEXT NOT NULL,
    item_ids_json TEXT,
    generated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_runs (
    id INTEGER PRIMARY KEY,
    trigger_type TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL,
    collected_count INTEGER DEFAULT 0,
    deduped_count INTEGER DEFAULT 0,
    classified_count INTEGER DEFAULT 0,
    extracted_count INTEGER DEFAULT 0,
    failed_count INTEGER DEFAULT 0,
    error_summary TEXT,
    node_trace_json TEXT
);

CREATE TABLE IF NOT EXISTS scheduler_locks (
    name TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sources_type ON sources(type);
CREATE INDEX IF NOT EXISTS idx_raw_items_content_hash ON raw_items(content_hash);
CREATE INDEX IF NOT EXISTS idx_raw_items_published_at ON raw_items(published_at);
CREATE INDEX IF NOT EXISTS idx_processed_items_cluster ON processed_items(canonical_event_id);
CREATE INDEX IF NOT EXISTS idx_processed_items_importance ON processed_items(importance_level);
CREATE INDEX IF NOT EXISTS idx_processed_items_rank ON processed_items(rank_score);
CREATE INDEX IF NOT EXISTS idx_item_tags_dimension_value ON item_tags(tag_dimension, tag_value);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_started ON workflow_runs(started_at);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        source_columns = {row["name"] for row in conn.execute("PRAGMA table_info(sources)").fetchall()}
        if "last_checked_at" not in source_columns:
            conn.execute("ALTER TABLE sources ADD COLUMN last_checked_at TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduler_locks (
                name TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                heartbeat_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
            """
        )
        self._deduplicate_running_workflows(conn)
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_workflow_runs_one_running
            ON workflow_runs(status)
            WHERE status = 'running'
            """
        )
        raw_item_columns = {row["name"] for row in conn.execute("PRAGMA table_info(raw_items)").fetchall()}
        for column_name, column_type in [
            ("relevance_industry", "TEXT"),
            ("relevance_confidence", "REAL"),
            ("relevance_reason", "TEXT"),
            ("relevance_matched_terms_json", "TEXT"),
            ("relevance_provider", "TEXT"),
            ("relevance_model", "TEXT"),
        ]:
            if column_name not in raw_item_columns:
                conn.execute(f"ALTER TABLE raw_items ADD COLUMN {column_name} {column_type}")
        processed_item_columns = {row["name"] for row in conn.execute("PRAGMA table_info(processed_items)").fetchall()}
        for column_name, column_type in [
            ("extraction_provider", "TEXT"),
            ("extraction_model", "TEXT"),
            ("extraction_mode", "TEXT"),
            ("extraction_fallback_reason", "TEXT"),
        ]:
            if column_name not in processed_item_columns:
                conn.execute(f"ALTER TABLE processed_items ADD COLUMN {column_name} {column_type}")

    def _deduplicate_running_workflows(self, conn: sqlite3.Connection) -> None:
        running = conn.execute(
            "SELECT id FROM workflow_runs WHERE status = 'running' ORDER BY started_at DESC, id DESC"
        ).fetchall()
        if len(running) <= 1:
            return
        keep_id = running[0]["id"]
        duplicate_ids = [row["id"] for row in running[1:]]
        placeholders = ",".join("?" for _ in duplicate_ids)
        conn.execute(
            f"""
            UPDATE workflow_runs
            SET ended_at = COALESCE(ended_at, started_at),
                status = 'failed',
                failed_count = failed_count + 1,
                error_summary = COALESCE(error_summary || ' | ', '') || 'Duplicate running workflow closed during migration'
            WHERE id IN ({placeholders}) AND id != ?
            """,
            (*duplicate_ids, keep_id),
        )

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self.connect() as conn:
            cursor = conn.execute(sql, params)
            return int(cursor.lastrowid)

    def executescript(self, sql: str) -> None:
        with self.connect() as conn:
            conn.executescript(sql)


def to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def from_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default
