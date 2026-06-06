from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import get_settings
from app.database import from_json, to_json
from app.services.collector import extract_published_at_from_text, extract_published_at_from_url
from app.services.ranking import RankInput, calculate_rank_score


KEY_ACTOR_BY_SCORE = {
    1.0: "core",
    0.75: "important",
    0.45: "normal",
    0.0: "none",
}


def main() -> None:
    database_path = get_settings().database_path
    updated = backfill(database_path)
    print(json.dumps({"updated_count": len(updated), "updated": updated}, ensure_ascii=False, indent=2))


def backfill(database_path: Path) -> list[dict[str, object]]:
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT r.id AS raw_item_id, r.raw_content, r.fetched_at, r.title, r.url,
                   p.id AS processed_item_id, p.canonical_event_id, p.importance_level,
                   p.confidence, p.score_breakdown_json,
                   s.reliability_score
            FROM raw_items r
            JOIN processed_items p ON p.raw_item_id = r.id
            JOIN sources s ON s.id = r.source_id
            WHERE r.published_at IS NULL
            ORDER BY r.id
            """
        ).fetchall()
        updated: list[dict[str, object]] = []
        for row in rows:
            published_at = extract_published_at_from_text(row["raw_content"]) or extract_published_at_from_url(row["url"])
            if not published_at:
                continue
            rank = _recalculate_rank(conn, row, published_at)
            score_payload = {**rank.breakdown, "reasons": rank.reasons}
            conn.execute("UPDATE raw_items SET published_at = ? WHERE id = ?", (published_at, row["raw_item_id"]))
            conn.execute(
                """
                UPDATE processed_items
                SET rank_score = ?, score_breakdown_json = ?, rank_reason = ?
                WHERE id = ?
                """,
                (
                    rank.weighted_total,
                    to_json(score_payload),
                    " | ".join(rank.reasons),
                    row["processed_item_id"],
                ),
            )
            updated.append(
                {
                    "raw_item_id": row["raw_item_id"],
                    "processed_item_id": row["processed_item_id"],
                    "title": row["title"],
                    "published_at": published_at,
                    "rank_score": rank.weighted_total,
                }
            )
        conn.commit()
        return updated
    finally:
        conn.close()


def _recalculate_rank(conn: sqlite3.Connection, row: sqlite3.Row, published_at: str):
    event_types = [
        tag["tag_value"]
        for tag in conn.execute(
            """
            SELECT tag_value
            FROM item_tags
            WHERE processed_item_id = ? AND tag_dimension = 'event_type'
            """,
            (row["processed_item_id"],),
        ).fetchall()
    ]
    reliable_source_count = conn.execute(
        """
        SELECT COUNT(DISTINCT r2.source_id) AS count
        FROM processed_items p2
        JOIN raw_items r2 ON r2.id = p2.raw_item_id
        JOIN sources s2 ON s2.id = r2.source_id
        WHERE p2.canonical_event_id = ? AND COALESCE(s2.reliability_score, 0) >= 0.65
        """,
        (row["canonical_event_id"],),
    ).fetchone()["count"]
    return calculate_rank_score(
        RankInput(
            importance_level=row["importance_level"],
            confidence=row["confidence"],
            source_reliability=row["reliability_score"],
            event_types=event_types,
            published_at=published_at,
            fetched_at=row["fetched_at"],
            reliable_source_count=reliable_source_count,
            key_actor_level=_key_actor_level(row["score_breakdown_json"]),
        )
    )


def _key_actor_level(score_breakdown_json: str | None) -> str:
    breakdown = from_json(score_breakdown_json, {})
    score = round(float(breakdown.get("key_actor_score") or 0.0), 2)
    return KEY_ACTOR_BY_SCORE.get(score, "none")


if __name__ == "__main__":
    main()
