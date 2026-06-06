from __future__ import annotations

from typing import Any


class VectorStore:
    """Small adapter boundary for Chroma.

    The MVP can run without Chroma installed; production deployments can replace
    this adapter with a Chroma-backed implementation while keeping workflow nodes
    and tests stable.
    """

    def upsert_item(self, raw_item_id: int, text: str, metadata: dict[str, Any]) -> None:
        return None

    def find_similar(self, text: str, limit: int = 5) -> list[dict[str, Any]]:
        return []
