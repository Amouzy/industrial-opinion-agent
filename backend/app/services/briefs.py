from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.database import Database, from_json, to_json


def generate_brief(db: Database, brief_type: str = "manual", item_limit: int = 8) -> dict[str, Any]:
    items = db.query(
        """
        SELECT p.*, r.title, r.url, r.published_at, s.name AS source_name
        FROM processed_items p
        JOIN raw_items r ON r.id = p.raw_item_id
        JOIN sources s ON s.id = r.source_id
        ORDER BY p.rank_score DESC, r.published_at DESC
        LIMIT ?
        """,
        (item_limit,),
    )
    now = datetime.now(timezone.utc).isoformat()
    start = items[-1]["published_at"] if items else now
    end = items[0]["published_at"] if items else now
    lines = [f"# 产业舆情{'晨报' if brief_type == 'morning' else '简报'}", ""]
    if not items:
        lines.append("当前筛选范围内暂无可生成简报的情报。")
    for index, item in enumerate(items, start=1):
        facts = from_json(item.get("key_facts_json"), {})
        lines.extend(
            [
                f"## {index}. {item['normalized_title']}",
                f"- 综合价值分：{item['rank_score']:.2f}",
                f"- 来源：{item['source_name']}",
                f"- 结论：{item.get('impact_analysis') or facts.get('impact', '')}",
                f"- 原文：{item['url']}",
                "",
            ]
        )
    title = "每日晨报" if brief_type == "morning" else "筛选简报"
    brief_id = db.execute(
        """
        INSERT INTO briefs (brief_type, time_range_start, time_range_end, title, content_markdown, item_ids_json, generated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (brief_type, start, end, title, "\n".join(lines), to_json([item["id"] for item in items]), now),
    )
    return db.query_one("SELECT * FROM briefs WHERE id = ?", (brief_id,)) or {}
