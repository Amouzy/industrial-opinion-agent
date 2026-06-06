from __future__ import annotations

import re
from typing import Any

from app.services.time_utils import parse_iso


def normalize_title(title: str) -> str:
    text = re.sub(r"\s+", "", title.lower())
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", text)


def choose_representative_item(items: list[dict[str, Any]]) -> dict[str, Any]:
    if not items:
        raise ValueError("cannot choose representative item from empty list")

    def sort_key(item: dict[str, Any]) -> tuple[float, int, float]:
        reliability = float(item.get("source_reliability") or item.get("reliability_score") or 0.5)
        content_length = len(item.get("raw_content") or item.get("content_excerpt") or "")
        published = parse_iso(item.get("published_at"))
        timestamp = published.timestamp() if published else 0.0
        # Higher authority first, then richer source text, then newer update.
        return reliability, content_length, timestamp

    return max(items, key=sort_key)
