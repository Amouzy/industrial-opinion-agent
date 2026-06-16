from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.config import get_settings
from app.database import Database, from_json
from app.services.briefs import generate_brief
from app.services.llm import build_llm_client
from app.services.time_utils import parse_iso
from app.services.taxonomy import (
    EVENT_TYPE_SCORES,
    EVENT_TYPE_ORDER,
    IMPORTANCE_LEVEL_ORDER,
    INDUSTRY_ORDER,
    INDUSTRY_SUBTAGS,
    SIGNAL_ATTRIBUTE_ORDER,
    SOURCE_TYPE_LABELS,
    SUBJECT_ROLE_ORDER,
)
from app.services.workflow import start_source_interval_scan_background


class SourceCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    type: str = Field(min_length=1, max_length=50)
    url: str = Field(min_length=1, max_length=1000)
    industry_hint: str | None = Field(default=None, max_length=200)
    reliability_score: float = Field(default=0.5, ge=0, le=1)
    enabled: bool = True
    fetch_interval_minutes: int = Field(default=120, ge=1, le=10080)


class SourceUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    type: str | None = Field(default=None, min_length=1, max_length=50)
    url: str | None = Field(default=None, min_length=1, max_length=1000)
    industry_hint: str | None = Field(default=None, max_length=200)
    reliability_score: float | None = Field(default=None, ge=0, le=1)
    enabled: bool | None = None
    fetch_interval_minutes: int | None = Field(default=None, ge=1, le=10080)


def create_router(db: Database) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/intelligence/summary")
    def summary() -> dict[str, Any]:
        latest_run = db.query_one("SELECT * FROM workflow_runs ORDER BY started_at DESC LIMIT 1")
        high_count = db.query_one("SELECT COUNT(*) AS count FROM processed_items WHERE importance_level = 'high'")["count"]
        total_count = db.query_one("SELECT COUNT(*) AS count FROM processed_items")["count"]
        source_count = db.query_one("SELECT COUNT(*) AS count FROM sources WHERE enabled = 1")["count"]
        last_monitor = db.query_one(
            "SELECT * FROM workflow_runs WHERE trigger_type = 'scheduled_monitor' ORDER BY started_at DESC LIMIT 1"
        )
        return {
            "window": "最近 72 小时",
            "today_judgement": "政策、龙头企业动作和 AI 算力基础设施仍是当前最高优先级信号。",
            "total_count": total_count,
            "high_count": high_count,
            "evidence_to_verify_count": max(0, total_count - high_count),
            "enabled_source_count": source_count,
            "latest_run": latest_run,
            "last_monitor_at": last_monitor["ended_at"] if last_monitor else None,
            "next_monitor_at": "工作日每 2 小时执行，下一轮由 APScheduler 触发",
            "morning_brief_status": "已生成" if db.query_one("SELECT id FROM briefs LIMIT 1") else "待生成",
        }

    @router.get("/intelligence/items")
    def items(
        keyword: str = "",
        industry: str = "",
        industry_subtag: str = "",
        importance: str = "",
        event_type: str = "",
        signal_attribute: str = "",
        subject_role: str = "",
        source_type: str = "",
        time_range: str = Query("7d", pattern="^(24h|72h|7d|30d|all)$"),
        limit: int = Query(50, ge=1, le=200),
        sort: str = Query("rank", pattern="^(rank|time)$"),
    ) -> list[dict[str, Any]]:
        rows = db.query(
            """
            SELECT p.*, r.title, r.url, r.canonical_url, r.published_at, r.fetched_at, r.content_excerpt, r.raw_content,
                   s.name AS source_name, s.type AS source_type, s.reliability_score,
                   c.duplicate_count, c.canonical_title
            FROM processed_items p
            JOIN raw_items r ON r.id = p.raw_item_id
            JOIN sources s ON s.id = r.source_id
            LEFT JOIN event_clusters c ON c.id = p.canonical_event_id
            ORDER BY p.rank_score DESC, r.published_at DESC
            """
        )
        filtered = [_decorate_item(db, row) for row in rows]
        if keyword:
            filtered = [
                item
                for item in filtered
                if keyword.lower()
                in " ".join(
                    [
                        item["title"],
                        item.get("summary") or "",
                        item.get("impact_analysis") or "",
                        item.get("content_excerpt") or "",
                    ]
                ).lower()
            ]
        for dim, value in [
            ("industry", industry),
            ("industry_subtag", industry_subtag),
            ("event_type", event_type),
            ("signal_attribute", signal_attribute),
            ("subject_role", subject_role),
        ]:
            if value:
                filtered = [item for item in filtered if value in item["tags_by_dimension"].get(dim, [])]
        if importance:
            filtered = [item for item in filtered if item["importance_level"] == importance]
        if source_type:
            filtered = [item for item in filtered if item["source_type"] == source_type]
        filtered = _filter_by_time_range(filtered, time_range)
        if sort == "time":
            filtered.sort(key=lambda item: item.get("published_at") or item.get("fetched_at") or "", reverse=True)
        else:
            filtered.sort(key=lambda item: item.get("rank_score") or 0, reverse=True)
        return filtered[:limit]

    @router.get("/intelligence/items/{item_id}")
    def item_detail(item_id: int) -> dict[str, Any]:
        row = db.query_one(
            """
            SELECT p.*, r.title, r.url, r.canonical_url, r.published_at, r.fetched_at, r.content_excerpt, r.raw_content,
                   s.name AS source_name, s.type AS source_type, s.reliability_score,
                   c.duplicate_count, c.canonical_title
            FROM processed_items p
            JOIN raw_items r ON r.id = p.raw_item_id
            JOIN sources s ON s.id = r.source_id
            LEFT JOIN event_clusters c ON c.id = p.canonical_event_id
            WHERE p.id = ?
            """,
            (item_id,),
        )
        if not row:
            return {}
        item = _decorate_item(db, row)
        item["similar_reports"] = _similar_reports(db, item["canonical_event_id"])
        item["item_processing_trace"] = _item_processing_trace(db, item)
        return item

    @router.get("/events/{cluster_id}")
    def event_detail(cluster_id: int) -> dict[str, Any]:
        cluster = db.query_one("SELECT * FROM event_clusters WHERE id = ?", (cluster_id,))
        if not cluster:
            return {}
        return {**cluster, "reports": _similar_reports(db, cluster_id)}

    @router.get("/briefs")
    def briefs() -> list[dict[str, Any]]:
        rows = db.query("SELECT * FROM briefs ORDER BY generated_at DESC LIMIT 20")
        return [{**row, "item_ids": from_json(row.get("item_ids_json"), [])} for row in rows]

    @router.post("/briefs/generate")
    def create_brief() -> dict[str, Any]:
        row = generate_brief(db, brief_type="manual")
        return {**row, "item_ids": from_json(row.get("item_ids_json"), [])}

    @router.get("/runs")
    def runs(
        page: int = Query(1, ge=1),
        page_size: int = Query(10, ge=1, le=100),
    ) -> dict[str, Any]:
        total = db.query_one("SELECT COUNT(*) AS count FROM workflow_runs")["count"]
        offset = (page - 1) * page_size
        rows = db.query(
            "SELECT * FROM workflow_runs ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
            (page_size, offset),
        )
        return {
            "items": [{**row, "node_trace": from_json(row.get("node_trace_json"), [])} for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    @router.post("/collect/manual")
    def manual_collect() -> dict[str, Any]:
        row = start_source_interval_scan_background(db, trigger_type="manual_collect", force_all=True)
        return {**row, "node_trace": from_json(row.get("node_trace_json"), [])}

    @router.get("/sources")
    def sources() -> list[dict[str, Any]]:
        rows = db.query("SELECT * FROM sources ORDER BY type, reliability_score DESC, id")
        return [_source_response(row) for row in rows]

    @router.post("/sources", status_code=201)
    def create_source(payload: SourceCreateRequest) -> dict[str, Any]:
        data = _source_payload(payload.model_dump())
        try:
            source_id = db.execute(
                """
                INSERT INTO sources (name, type, url, industry_hint, reliability_score, enabled, fetch_interval_minutes)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data["name"],
                    data["type"],
                    data["url"],
                    data.get("industry_hint"),
                    data["reliability_score"],
                    data["enabled"],
                    data["fetch_interval_minutes"],
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="数据源 URL 已存在") from exc
        return _source_response(_source_or_404(db, source_id))

    @router.patch("/sources/{source_id}")
    def update_source(source_id: int, payload: SourceUpdateRequest) -> dict[str, Any]:
        _source_or_404(db, source_id)
        updates = _source_payload(payload.model_dump(exclude_unset=True))
        if updates:
            assignments = ", ".join(f"{key} = ?" for key in updates)
            try:
                db.execute(
                    f"UPDATE sources SET {assignments} WHERE id = ?",
                    (*updates.values(), source_id),
                )
            except sqlite3.IntegrityError as exc:
                raise HTTPException(status_code=409, detail="数据源 URL 已存在") from exc
        return _source_response(_source_or_404(db, source_id))

    @router.delete("/sources/{source_id}")
    def delete_source(source_id: int) -> dict[str, int]:
        _source_or_404(db, source_id)
        raw_count = db.query_one("SELECT COUNT(*) AS count FROM raw_items WHERE source_id = ?", (source_id,))["count"]
        if raw_count:
            raise HTTPException(status_code=409, detail="该数据源已有采集记录，不能删除；可先停用该数据源。")
        db.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        return {"deleted": source_id}

    @router.get("/taxonomy")
    def taxonomy() -> dict[str, Any]:
        return {
            "industries": INDUSTRY_ORDER,
            "industry_subtags": INDUSTRY_SUBTAGS,
            "event_types": EVENT_TYPE_ORDER,
            "importance_levels": IMPORTANCE_LEVEL_ORDER,
            "subject_roles": SUBJECT_ROLE_ORDER,
            "signal_attributes": SIGNAL_ATTRIBUTE_ORDER,
            "source_types": SOURCE_TYPE_LABELS,
        }

    @router.get("/ranking-rules")
    def ranking_rules() -> dict[str, Any]:
        return {
            "formula": "importance*0.35 + source*0.20 + freshness*0.15 + event_type*0.15 + coverage*0.10 + key_actor*0.05",
            "weights": {
                "importance_score": 0.35,
                "source_score": 0.20,
                "freshness_score": 0.15,
                "event_type_score": 0.15,
                "coverage_score": 0.10,
                "key_actor_score": 0.05,
            },
            "event_type_scores": EVENT_TYPE_SCORES,
        }

    @router.get("/llm/status")
    def llm_status() -> dict[str, Any]:
        return build_llm_client(get_settings().llm).runtime_status()

    return router


def _decorate_item(db: Database, row: dict[str, Any]) -> dict[str, Any]:
    tags = db.query("SELECT tag_dimension, tag_value, confidence, evidence_json FROM item_tags WHERE processed_item_id = ?", (row["id"],))
    tags_by_dimension: dict[str, list[str]] = {}
    for tag in tags:
        tags_by_dimension.setdefault(tag["tag_dimension"], []).append(tag["tag_value"])
    return {
        **row,
        "key_facts": from_json(row.get("key_facts_json"), {}),
        "entities": from_json(row.get("entities_json"), []),
        "score_breakdown": from_json(row.get("score_breakdown_json"), {}),
        "source_spans": from_json(row.get("source_spans_json"), []),
        "tags": tags,
        "tags_by_dimension": tags_by_dimension,
        "source_type_label": SOURCE_TYPE_LABELS.get(row.get("source_type"), row.get("source_type")),
    }


def _source_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, str):
            value = value.strip()
        if key == "industry_hint" and value == "":
            value = None
        if key == "enabled" and value is not None:
            value = 1 if value else 0
        normalized[key] = value
    return normalized


def _source_or_404(db: Database, source_id: int) -> dict[str, Any]:
    source = db.query_one("SELECT * FROM sources WHERE id = ?", (source_id,))
    if not source:
        raise HTTPException(status_code=404, detail="数据源不存在")
    return source


def _source_response(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "enabled": bool(row.get("enabled"))}


def _similar_reports(db: Database, cluster_id: int | None) -> list[dict[str, Any]]:
    if not cluster_id:
        return []
    return db.query(
        """
        SELECT p.id AS processed_item_id, r.title, r.url, r.published_at,
               s.name AS source_name, s.type AS source_type, s.reliability_score
        FROM processed_items p
        JOIN raw_items r ON r.id = p.raw_item_id
        JOIN sources s ON s.id = r.source_id
        WHERE p.canonical_event_id = ?
        ORDER BY s.reliability_score DESC, r.published_at DESC
        """,
        (cluster_id,),
    )


def _item_processing_trace(db: Database, item: dict[str, Any]) -> list[dict[str, Any]]:
    similar_reports = _similar_reports(db, item.get("canonical_event_id"))
    similar_sources = [
        {
            "source_name": report.get("source_name"),
            "url": report.get("url"),
            "reliability_score": report.get("reliability_score"),
        }
        for report in similar_reports
    ]
    classification_input = {
        "title": item.get("title"),
        "content_excerpt": item.get("content_excerpt"),
        "source_type": item.get("source_type"),
        "industry_hint": _source_industry_hint(db, item.get("raw_item_id")),
    }
    rank_input = {
        "importance_level": item.get("importance_level"),
        "confidence": item.get("confidence"),
        "source_reliability": item.get("reliability_score"),
        "published_at": item.get("published_at"),
        "fetched_at": item.get("fetched_at"),
        "duplicate_count": item.get("duplicate_count") or 1,
        "tags_by_dimension": item.get("tags_by_dimension", {}),
    }
    return [
        {
            "node": "collect",
            "label": "采集",
            "status": "success",
            "input": {
                "source_name": item.get("source_name"),
                "source_type": item.get("source_type"),
                "source_url": _source_url(db, item.get("raw_item_id")),
                "trigger": "scheduled_or_manual_monitor",
            },
            "output": {
                "title": item.get("title"),
                "url": item.get("url"),
                "published_at": item.get("published_at"),
                "fetched_at": item.get("fetched_at"),
                "content_excerpt": item.get("content_excerpt"),
            },
        },
        {
            "node": "normalize",
            "label": "标准化",
            "status": "success",
            "input": {
                "title": item.get("title"),
                "url": item.get("url"),
                "raw_content_length": len(item.get("raw_content") or ""),
            },
            "output": {
                "raw_item_id": item.get("raw_item_id"),
                "normalized_title": item.get("normalized_title"),
                "canonical_url": item.get("canonical_url") or item.get("url"),
                "content_excerpt": item.get("content_excerpt"),
                "status": "processed",
            },
        },
        {
            "node": "deduplicate",
            "label": "去重聚类",
            "status": "success",
            "input": {
                "raw_item_id": item.get("raw_item_id"),
                "title": item.get("title"),
                "url": item.get("url"),
            },
            "output": {
                "canonical_event_id": item.get("canonical_event_id"),
                "canonical_title": item.get("canonical_title") or item.get("normalized_title"),
                "duplicate_count": item.get("duplicate_count") or 1,
                "similar_sources": similar_sources,
            },
        },
        {
            "node": "classify_tag",
            "label": "分类打标",
            "status": "success",
            "input": classification_input,
            "output": {
                "tags_by_dimension": item.get("tags_by_dimension", {}),
                "importance_level": item.get("importance_level"),
                "importance_reason": item.get("importance_reason"),
                "confidence": item.get("confidence"),
                "entities": item.get("entities", []),
            },
        },
        {
            "node": "rank",
            "label": "排序评分",
            "status": "success",
            "input": rank_input,
            "output": {
                "rank_score": item.get("rank_score"),
                "score_breakdown": item.get("score_breakdown", {}),
                "rank_reason": item.get("rank_reason"),
            },
        },
        {
            "node": "extract",
            "label": "核心信息提炼",
            "status": "success",
            "input": {
                "title": item.get("title"),
                "content_excerpt": item.get("content_excerpt"),
                "source_spans": item.get("source_spans", []),
            },
            "output": {
                "summary": item.get("summary"),
                "key_facts": item.get("key_facts", {}),
                "impact_analysis": item.get("impact_analysis"),
                "source_spans": item.get("source_spans", []),
                "extraction_mode": item.get("extraction_mode"),
                "extraction_provider": item.get("extraction_provider"),
                "extraction_model": item.get("extraction_model"),
                "extraction_fallback_reason": item.get("extraction_fallback_reason"),
            },
        },
    ]


def _source_row_for_raw_item(db: Database, raw_item_id: int | None) -> dict[str, Any] | None:
    if not raw_item_id:
        return None
    return db.query_one(
        """
        SELECT s.*
        FROM raw_items r
        JOIN sources s ON s.id = r.source_id
        WHERE r.id = ?
        """,
        (raw_item_id,),
    )


def _source_url(db: Database, raw_item_id: int | None) -> str | None:
    source = _source_row_for_raw_item(db, raw_item_id)
    return source.get("url") if source else None


def _source_industry_hint(db: Database, raw_item_id: int | None) -> str | None:
    source = _source_row_for_raw_item(db, raw_item_id)
    return source.get("industry_hint") if source else None


def _filter_by_time_range(items: list[dict[str, Any]], time_range: str) -> list[dict[str, Any]]:
    if time_range == "all":
        return items
    window_hours = {
        "24h": 24,
        "72h": 72,
        "7d": 24 * 7,
        "30d": 24 * 30,
    }[time_range]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    filtered: list[dict[str, Any]] = []
    for item in items:
        timestamp = parse_iso(item.get("published_at")) or parse_iso(item.get("fetched_at"))
        if timestamp and timestamp >= cutoff:
            filtered.append(item)
    return filtered
