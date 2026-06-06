from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.services.taxonomy import EVENT_TYPE_SCORES, IMPORTANCE_LEVEL_SCORES, KEY_ACTOR_SCORES
from app.services.time_utils import parse_iso


@dataclass(frozen=True)
class RankInput:
    importance_level: str | None
    confidence: float | None
    source_reliability: float | None
    event_types: list[str]
    published_at: str | None
    fetched_at: str | None
    reliable_source_count: int
    key_actor_level: str = "none"


@dataclass(frozen=True)
class RankScore:
    weighted_total: float
    breakdown: dict[str, float]
    reasons: list[str]


def calculate_rank_score(rank_input: RankInput, now: datetime | None = None) -> RankScore:
    now = now or datetime.now(timezone.utc)
    importance_score = _importance_score(rank_input.importance_level, rank_input.confidence)
    source_score = _clamp(rank_input.source_reliability if rank_input.source_reliability is not None else 0.5)
    freshness_score = _freshness_score(rank_input.published_at, rank_input.fetched_at, now)
    event_type_score = _event_type_score(rank_input.event_types)
    coverage_score = _coverage_score(rank_input.reliable_source_count)
    key_actor_score = KEY_ACTOR_SCORES.get(rank_input.key_actor_level, 0.0)
    weighted_total = (
        importance_score * 0.35
        + source_score * 0.20
        + freshness_score * 0.15
        + event_type_score * 0.15
        + coverage_score * 0.10
        + key_actor_score * 0.05
    )
    breakdown = {
        "importance_score": round(importance_score, 4),
        "source_score": round(source_score, 4),
        "freshness_score": round(freshness_score, 4),
        "event_type_score": round(event_type_score, 4),
        "coverage_score": round(coverage_score, 4),
        "key_actor_score": round(key_actor_score, 4),
        "weighted_total": round(weighted_total, 4),
    }
    reasons = [
        f"重要性 {rank_input.importance_level or 'unknown'} 得分 {breakdown['importance_score']:.2f}",
        f"来源权威性得分 {breakdown['source_score']:.2f}",
        f"时效性得分 {breakdown['freshness_score']:.2f}",
        f"事件类型 {', '.join(rank_input.event_types) or '未识别'} 得分 {breakdown['event_type_score']:.2f}",
        f"可靠相似报道 {rank_input.reliable_source_count} 个，覆盖度得分 {breakdown['coverage_score']:.2f}",
        f"关键主体等级 {rank_input.key_actor_level} 得分 {breakdown['key_actor_score']:.2f}",
    ]
    return RankScore(weighted_total=round(weighted_total, 4), breakdown=breakdown, reasons=reasons)


def _importance_score(level: str | None, confidence: float | None) -> float:
    level_score = IMPORTANCE_LEVEL_SCORES.get(level or "", 0.4)
    if confidence is None:
        return level_score
    return level_score * (0.7 + 0.3 * _clamp(confidence))


def _freshness_score(published_at: str | None, fetched_at: str | None, now: datetime) -> float:
    timestamp = parse_iso(published_at) or parse_iso(fetched_at)
    if timestamp is None:
        return 0.3
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    age_hours = max(0.0, (now - timestamp.astimezone(timezone.utc)).total_seconds() / 3600)
    if age_hours <= 6:
        return 1.0
    if age_hours <= 24:
        return 0.85
    if age_hours <= 72:
        return 0.6
    if age_hours <= 168:
        return 0.35
    return 0.15


def _event_type_score(event_types: list[str]) -> float:
    if not event_types:
        return 0.4
    return max(EVENT_TYPE_SCORES.get(event_type, 0.4) for event_type in event_types)


def _coverage_score(reliable_source_count: int) -> float:
    count = max(0, reliable_source_count)
    if count <= 1:
        return 0.2
    return min(1.0, math.log2(1 + count) / math.log2(6))


def _clamp(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))
