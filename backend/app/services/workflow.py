from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from threading import Thread
from typing import Any, Callable

from app.config import get_settings
from app.database import Database, from_json, to_json
from app.seed_data import LEGACY_URL_REPLACEMENTS, SOURCE_CONFIGS, demo_raw_items
from app.services.briefs import generate_brief
from app.services.classifier import classify_item
from app.services.collector import fetch_source_items, normalize_raw_item
from app.services.dedup import choose_representative_item, normalize_title
from app.services.extractor import extract_intelligence
from app.services.llm import LLMClient, build_llm_client
from app.services.ranking import RankInput, calculate_rank_score
from app.services.relevance import screen_relevance
from app.services.time_utils import parse_iso, utc_now_iso


SourceFetcher = Callable[[dict[str, Any]], list[dict[str, Any]]]


def ensure_seed_data(db: Database) -> None:
    """Prepare stable configuration without creating fake intelligence."""
    ensure_sources(db)
    repair_legacy_urls(db)


def recover_interrupted_runs(db: Database, ended_at: str | None = None) -> int:
    """Close workflow runs left open by a killed or restarted process."""
    interrupted = db.query("SELECT id FROM workflow_runs WHERE status = 'running'")
    if not interrupted:
        return 0
    finished_at = ended_at or utc_now_iso()
    db.execute(
        """
        UPDATE workflow_runs
        SET ended_at = ?,
            status = 'failed',
            failed_count = failed_count + 1,
            error_summary = COALESCE(error_summary || ' | ', '') || 'Workflow interrupted by process restart'
        WHERE status = 'running'
        """,
        (finished_at,),
    )
    return len(interrupted)


def ensure_sources(db: Database) -> None:
    for source in SOURCE_CONFIGS:
        db.execute(
            """
            INSERT INTO sources (id, name, type, url, industry_hint, reliability_score, enabled, fetch_interval_minutes)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                type = excluded.type,
                url = excluded.url,
                industry_hint = excluded.industry_hint,
                reliability_score = excluded.reliability_score,
                fetch_interval_minutes = excluded.fetch_interval_minutes
            """,
            (
                source["id"],
                source["name"],
                source["type"],
                source["url"],
                source["industry_hint"],
                source["reliability_score"],
                source["fetch_interval_minutes"],
            ),
        )


def repair_legacy_urls(db: Database) -> None:
    """Replace earlier demo fragment URLs in existing local databases."""
    for legacy_url, replacement_url in LEGACY_URL_REPLACEMENTS.items():
        db.execute(
            """
            UPDATE raw_items
            SET url = ?, canonical_url = ?
            WHERE url = ?
            """,
            (replacement_url, replacement_url, legacy_url),
        )


def due_sources(db: Database, now: datetime | None = None) -> list[dict[str, Any]]:
    current = now or datetime.now(timezone.utc)
    sources = db.query("SELECT * FROM sources WHERE enabled = 1 ORDER BY reliability_score DESC, id")
    due: list[dict[str, Any]] = []
    for source in sources:
        interval_minutes = int(source.get("fetch_interval_minutes") or 120)
        last_seen = parse_iso(source.get("last_fetched_at")) or parse_iso(source.get("last_checked_at"))
        if last_seen is None or current - last_seen >= timedelta(minutes=interval_minutes):
            due.append(source)
    return due


def _source_collection_window(source: dict[str, Any], current: datetime) -> tuple[datetime, datetime]:
    last_success = parse_iso(source.get("last_fetched_at"))
    start = last_success or (current - timedelta(hours=24))
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return start, current


def run_source_interval_scan(
    db: Database,
    trigger_type: str = "source_interval_scan",
    now: datetime | None = None,
    fetcher: SourceFetcher = fetch_source_items,
    force_all: bool = False,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    running = db.query_one("SELECT id, trigger_type, started_at FROM workflow_runs WHERE status = 'running' ORDER BY started_at DESC LIMIT 1")
    if running:
        return _insert_skipped_run(db, trigger_type, running)
    run_id = _insert_running_run(db, trigger_type)
    from app.services.workflow_graph import run_source_interval_graph

    return run_source_interval_graph(db, run_id, trigger_type, current, fetcher, force_all)


def start_source_interval_scan_background(
    db: Database,
    trigger_type: str = "manual_collect",
    now: datetime | None = None,
    fetcher: SourceFetcher = fetch_source_items,
    force_all: bool = False,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    running = db.query_one("SELECT id, trigger_type, started_at FROM workflow_runs WHERE status = 'running' ORDER BY started_at DESC LIMIT 1")
    if running:
        return _insert_skipped_run(db, trigger_type, running)
    run_id = _insert_running_run(db, trigger_type)
    row = db.query_one("SELECT * FROM workflow_runs WHERE id = ?", (run_id,)) or {}
    from app.services.workflow_graph import build_source_interval_graph

    build_source_interval_graph()
    worker = Thread(
        target=_execute_source_interval_scan_safely,
        args=(db, run_id, trigger_type, current, fetcher, force_all),
        daemon=True,
    )
    worker.start()
    return row


def _insert_running_run(db: Database, trigger_type: str) -> int:
    return db.execute(
        """
        INSERT INTO workflow_runs (trigger_type, started_at, status, node_trace_json)
        VALUES (?, ?, 'running', ?)
        """,
        (trigger_type, utc_now_iso(), to_json([])),
    )


def _insert_skipped_run(db: Database, trigger_type: str, running: dict[str, Any]) -> dict[str, Any]:
    reason = f"Workflow already running: #{running['id']} {running['trigger_type']} started_at={running['started_at']}"
    run_id = db.execute(
        """
        INSERT INTO workflow_runs (trigger_type, started_at, ended_at, status, error_summary, node_trace_json)
        VALUES (?, ?, ?, 'skipped', ?, ?)
        """,
        (
            trigger_type,
            utc_now_iso(),
            utc_now_iso(),
            reason,
            to_json([_trace_node("collect", {"trigger_type": trigger_type}, {"skipped": True, "reason": reason})]),
        ),
    )
    return db.query_one("SELECT * FROM workflow_runs WHERE id = ?", (run_id,)) or {}


def _execute_source_interval_scan_safely(
    db: Database,
    run_id: int,
    trigger_type: str,
    current: datetime,
    fetcher: SourceFetcher,
    force_all: bool,
) -> None:
    try:
        from app.services.workflow_graph import run_source_interval_graph

        run_source_interval_graph(db, run_id, trigger_type, current, fetcher, force_all)
    except Exception:
        pass


def _execute_source_interval_scan(
    db: Database,
    run_id: int,
    trigger_type: str,
    current: datetime,
    fetcher: SourceFetcher,
    force_all: bool,
    orchestrator: str = "python-sequential",
) -> dict[str, Any]:
    checked_at = current.isoformat()
    target_sources = (
        db.query("SELECT * FROM sources WHERE enabled = 1 ORDER BY reliability_score DESC, id")
        if force_all
        else due_sources(db, current)
    )
    trace: list[dict[str, Any]] = []
    raw_candidates: list[dict[str, Any]] = []
    source_results: list[dict[str, Any]] = []
    source_windows: list[dict[str, Any]] = []
    failed_count = 0
    try:
        for source in target_sources:
            window_start, window_end = _source_collection_window(source, current)
            source_windows.append(
                {
                    "source_id": source["id"],
                    "published_after": window_start.isoformat(),
                    "published_before": window_end.isoformat(),
                }
            )
            db.execute("UPDATE sources SET last_checked_at = ? WHERE id = ?", (checked_at, source["id"]))
            try:
                candidates = fetcher(
                    {
                        **source,
                        "published_after": window_start,
                        "published_before": window_end,
                    }
                )
            except Exception as exc:
                failed_count += 1
                error = str(exc)
                db.execute(
                    "UPDATE sources SET last_checked_at = ?, last_error = ? WHERE id = ?",
                    (checked_at, error, source["id"]),
                )
                source_results.append(_source_result(source, "failed", 0, error))
                continue

            if not candidates:
                db.execute(
                    "UPDATE sources SET last_checked_at = ?, last_fetched_at = ?, last_error = NULL WHERE id = ?",
                    (checked_at, checked_at, source["id"]),
                )
                source_results.append(_source_result(source, "empty", 0, None))
                continue

            source_candidates = [
                {
                    **candidate,
                    "source_id": candidate.get("source_id") or source["id"],
                    "source_type": source["type"],
                    "industry_hint": source["industry_hint"],
                }
                for candidate in candidates
            ]
            raw_candidates.extend(source_candidates)
            db.execute(
                "UPDATE sources SET last_checked_at = ?, last_fetched_at = ?, last_error = NULL WHERE id = ?",
                (checked_at, checked_at, source["id"]),
            )
            source_results.append(_source_result(source, "success", len(source_candidates), None))

        enabled_count = db.query_one("SELECT COUNT(*) AS count FROM sources WHERE enabled = 1")["count"]
        trace.append(
            _trace_node(
                "collect",
                {
                    "trigger_type": trigger_type,
                    "orchestrator": orchestrator,
                    "force_all": force_all,
                    "enabled_source_count": enabled_count,
                    "target_source_count": len(target_sources),
                    "target_source_ids": [source["id"] for source in target_sources],
                    "source_windows": source_windows,
                },
                {
                    "orchestrator": orchestrator,
                    "candidate_count": len(raw_candidates),
                    "source_results": source_results,
                    "candidates": _sample_candidates(raw_candidates),
                },
            )
        )
        llm_client = build_llm_client(get_settings().llm)
        relevant_candidates, rejected_candidates = _screen_relevant_candidates(raw_candidates, llm_client)
        trace.append(
            _trace_node(
                "relevance_screen",
                {"candidate_count": len(raw_candidates), "llm_status": llm_client.runtime_status()},
                {
                    "accepted_count": len(relevant_candidates),
                    "rejected_count": len(rejected_candidates),
                    "accepted_candidates": _sample_candidates(relevant_candidates),
                    "rejected_candidates": rejected_candidates,
                },
            )
        )
        _process_candidates(db, run_id, trigger_type, relevant_candidates, trace, failed_count, llm_client=llm_client)
    except Exception as exc:
        db.execute(
            """
            UPDATE workflow_runs
            SET ended_at = ?, status = 'failed', failed_count = failed_count + 1, error_summary = ?, node_trace_json = ?
            WHERE id = ?
            """,
            (utc_now_iso(), str(exc), to_json(trace), run_id),
        )
        raise
    return db.query_one("SELECT * FROM workflow_runs WHERE id = ?", (run_id,)) or {}


def run_workflow(db: Database, trigger_type: str = "manual_collect", seed_demo_items: bool = False) -> dict[str, Any]:
    """Compatibility runner.

    Runtime product paths should call run_source_interval_scan(). Passing
    seed_demo_items=True is kept only for contract tests that exercise the
    downstream processing pipeline with deterministic records.
    """
    run_id = db.execute(
        """
        INSERT INTO workflow_runs (trigger_type, started_at, status, node_trace_json)
        VALUES (?, ?, 'running', ?)
        """,
        (trigger_type, utc_now_iso(), to_json([])),
    )
    trace: list[dict[str, Any]] = []
    raw_candidates = demo_raw_items() if seed_demo_items else []
    try:
        sources = db.query("SELECT * FROM sources WHERE enabled = 1")
        trace.append(
            _trace_node(
                "collect",
                {
                    "trigger_type": trigger_type,
                    "enabled_source_count": len(sources),
                    "seed_demo_items": seed_demo_items,
                },
                {
                    "candidate_count": len(raw_candidates),
                    "candidates": _sample_candidates(raw_candidates),
                },
            )
        )
        _process_candidates(db, run_id, trigger_type, raw_candidates, trace, 0)
    except Exception as exc:
        db.execute(
            """
            UPDATE workflow_runs
            SET ended_at = ?, status = 'failed', failed_count = failed_count + 1, error_summary = ?, node_trace_json = ?
            WHERE id = ?
            """,
            (utc_now_iso(), str(exc), to_json(trace), run_id),
        )
        raise
    return db.query_one("SELECT * FROM workflow_runs WHERE id = ?", (run_id,)) or {}


def _process_candidates(
    db: Database,
    run_id: int,
    trigger_type: str,
    raw_candidates: list[dict[str, Any]],
    trace: list[dict[str, Any]],
    failed_count: int,
    llm_client: LLMClient | None = None,
    finalize_run: bool = True,
) -> dict[str, Any]:
    sources = {row["id"]: row for row in db.query("SELECT * FROM sources WHERE enabled = 1")}
    raw_item_ids: list[int] = []
    raw_item_summaries: list[dict[str, Any]] = []
    for candidate in raw_candidates:
        source = sources.get(candidate.get("source_id"))
        if not source:
            failed_count += 1
            continue
        normalized = normalize_raw_item(
            {
                **candidate,
                "source_type": source["type"],
                "industry_hint": source["industry_hint"],
            }
        )
        row = db.query_one("SELECT id FROM raw_items WHERE url = ?", (normalized["url"],))
        if row:
            db.execute(
                """
                UPDATE raw_items
                SET relevance_industry = COALESCE(?, relevance_industry),
                    relevance_confidence = COALESCE(?, relevance_confidence),
                    relevance_reason = COALESCE(?, relevance_reason),
                    relevance_matched_terms_json = COALESCE(?, relevance_matched_terms_json),
                    relevance_provider = COALESCE(?, relevance_provider),
                    relevance_model = COALESCE(?, relevance_model)
                WHERE id = ?
                """,
                (
                    normalized.get("relevance_industry"),
                    normalized.get("relevance_confidence"),
                    normalized.get("relevance_reason"),
                    to_json(normalized.get("relevance_matched_terms") or []),
                    normalized.get("relevance_provider"),
                    normalized.get("relevance_model"),
                    row["id"],
                ),
            )
            raw_item_ids.append(row["id"])
            raw_item_summaries.append(
                _raw_item_trace_summary(
                    {
                        **normalized,
                        "id": row["id"],
                        "source_name": source["name"],
                        "status": "existing",
                    }
                )
            )
            continue
        raw_id = db.execute(
            """
            INSERT INTO raw_items
                (source_id, url, canonical_url, title, author, published_at, fetched_at, raw_content, content_excerpt,
                 content_hash, status, relevance_industry, relevance_confidence, relevance_reason,
                 relevance_matched_terms_json, relevance_provider, relevance_model)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized["source_id"],
                normalized["url"],
                normalized["url"],
                normalized["title"],
                normalized["author"],
                normalized["published_at"],
                normalized["fetched_at"],
                normalized["raw_content"],
                normalized["content_excerpt"],
                normalized["content_hash"],
                normalized["status"],
                normalized.get("relevance_industry"),
                normalized.get("relevance_confidence"),
                normalized.get("relevance_reason"),
                to_json(normalized.get("relevance_matched_terms") or []),
                normalized.get("relevance_provider"),
                normalized.get("relevance_model"),
            ),
        )
        raw_item_ids.append(raw_id)
        raw_item_summaries.append(
            _raw_item_trace_summary(
                {
                    **normalized,
                    "id": raw_id,
                    "source_name": source["name"],
                }
            )
        )
    trace.append(
        _trace_node(
            "normalize",
            {"candidate_count": len(raw_candidates)},
            {
                "raw_item_count": len(raw_item_ids),
                "raw_items": raw_item_summaries,
            },
        )
    )

    raw_items = _load_scoped_raw_items(db, raw_item_ids)
    clusters = _cluster_raw_items(db, raw_items)
    trace.append(
        _trace_node(
            "deduplicate",
            {
                "raw_item_count": len(raw_items),
                "raw_item_ids": raw_item_ids,
            },
            {
                "cluster_count": len(clusters),
                "clusters": _cluster_trace_summaries(clusters),
            },
        )
    )

    processed_count = 0
    processed_summaries: list[dict[str, Any]] = []
    for cluster_id, cluster_items in clusters.items():
        reliable_source_count = len({item["source_id"] for item in cluster_items if float(item["reliability_score"] or 0) >= 0.65})
        representative = choose_representative_item(
            [
                {
                    **item,
                    "source_reliability": item["reliability_score"],
                }
                for item in cluster_items
            ]
        )
        _update_cluster_representative(db, cluster_id, representative, len(cluster_items))
        for item in cluster_items:
            existing_processed = db.query_one("SELECT * FROM processed_items WHERE raw_item_id = ?", (item["id"],))
            if existing_processed:
                processed_summaries.append(_processed_item_trace_summary(db, existing_processed, item, cluster_id))
                continue
            classification = classify_item(item, llm_client=llm_client)
            extraction = extract_intelligence(item, classification, llm_client=llm_client)
            key_facts = extraction.key_facts
            summary = extraction.summary
            rank = calculate_rank_score(
                RankInput(
                    importance_level=classification.importance_level,
                    confidence=classification.confidence,
                    source_reliability=item["reliability_score"],
                    event_types=classification.event_types,
                    published_at=item["published_at"],
                    fetched_at=item["fetched_at"],
                    reliable_source_count=reliable_source_count,
                    key_actor_level=classification.key_actor_level,
                )
            )
            score_payload = {**rank.breakdown, "reasons": rank.reasons}
            processed_id = db.execute(
                """
                INSERT INTO processed_items
                    (raw_item_id, canonical_event_id, normalized_title, summary, key_facts_json, entities_json,
                     impact_analysis, importance_level, importance_reason, rank_score, score_breakdown_json,
                     rank_reason, source_spans_json, llm_provider, llm_model, extraction_provider, extraction_model,
                     extraction_mode, extraction_fallback_reason, confidence, created_at, workflow_run_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["id"],
                    cluster_id,
                    item["title"],
                    summary,
                    to_json(key_facts),
                    to_json(classification.entities),
                    extraction.impact_analysis,
                    classification.importance_level,
                    classification.importance_reason,
                    rank.weighted_total,
                    to_json(score_payload),
                    " | ".join(rank.reasons),
                    to_json(extraction.source_spans),
                    classification.llm_provider,
                    classification.llm_model,
                    extraction.provider,
                    extraction.model,
                    extraction.mode,
                    extraction.fallback_reason,
                    classification.confidence,
                    utc_now_iso(),
                    run_id,
                ),
            )
            for tag in classification.to_tags():
                db.execute(
                    """
                    INSERT INTO item_tags (processed_item_id, tag_dimension, tag_value, confidence, evidence_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        processed_id,
                        tag["tag_dimension"],
                        tag["tag_value"],
                        classification.confidence,
                        to_json(classification.evidence),
                    ),
                )
            db.execute("UPDATE raw_items SET status = 'processed' WHERE id = ?", (item["id"],))
            processed_count += 1
            processed_summaries.append(
                {
                    "processed_item_id": processed_id,
                    "raw_item_id": item["id"],
                    "cluster_id": cluster_id,
                    "title": item["title"],
                    "url": item["url"],
                    "importance_level": classification.importance_level,
                    "industry": classification.industry,
                    "industry_subtags": classification.industry_subtags,
                    "event_types": classification.event_types,
                    "rank_score": rank.weighted_total,
                    "extraction_mode": extraction.mode,
                    "extraction_provider": extraction.provider,
                    "extraction_model": extraction.model,
                    "extraction_fallback_reason": extraction.fallback_reason,
                }
            )
    trace.append(
        _trace_node(
            "classify_rank_extract",
            {
                "cluster_count": len(clusters),
                "raw_item_count": len(raw_items),
            },
            {
                "processed_count": processed_count,
                "processed_items": processed_summaries,
            },
        )
    )
    if trigger_type in {"scheduled_morning", "manual_brief"}:
        brief = generate_brief(db, brief_type="morning" if trigger_type == "scheduled_morning" else "manual")
        trace.append(
            _trace_node(
                "generate_brief",
                {"trigger_type": trigger_type, "processed_item_count": processed_count},
                {"brief_id": brief.get("id"), "title": brief.get("title")},
            )
        )
    completion = {
        "collected_count": len(raw_item_ids),
        "deduped_count": len(clusters),
        "classified_count": processed_count,
        "extracted_count": processed_count,
        "failed_count": failed_count,
        "trace": trace,
    }
    if finalize_run:
        db.execute(
            """
            UPDATE workflow_runs
            SET ended_at = ?, status = 'success', collected_count = ?, deduped_count = ?,
                classified_count = ?, extracted_count = ?, failed_count = ?, node_trace_json = ?
            WHERE id = ?
            """,
            (
                utc_now_iso(),
                completion["collected_count"],
                completion["deduped_count"],
                completion["classified_count"],
                completion["extracted_count"],
                completion["failed_count"],
                to_json(trace),
                run_id,
            ),
        )
    return completion


def _screen_relevant_candidates(
    raw_candidates: list[dict[str, Any]],
    llm_client: LLMClient | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for candidate in raw_candidates:
        result = screen_relevance(candidate, llm_client=llm_client)
        if result.is_relevant:
            accepted.append(
                {
                    **candidate,
                    "relevance_industry": result.industry,
                    "relevance_confidence": result.confidence,
                    "relevance_reason": result.reason,
                    "relevance_matched_terms": result.matched_terms,
                    "relevance_provider": result.provider,
                    "relevance_model": result.model,
                }
            )
        else:
            rejected.append(result.to_trace(candidate))
    return accepted, rejected


def _load_scoped_raw_items(db: Database, raw_item_ids: list[int]) -> list[dict[str, Any]]:
    if not raw_item_ids:
        return []
    placeholders = ",".join("?" for _ in raw_item_ids)
    return db.query(
        f"""
        SELECT r.*, s.name AS source_name, s.type AS source_type, s.industry_hint, s.reliability_score
        FROM raw_items r
        JOIN sources s ON s.id = r.source_id
        WHERE r.id IN ({placeholders})
        ORDER BY r.published_at DESC
        """,
        tuple(raw_item_ids),
    )


def _cluster_raw_items(db: Database, raw_items: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in raw_items:
        grouped[_cluster_key(item)].append(item)
    clusters: dict[int, list[dict[str, Any]]] = {}
    for key, items in grouped.items():
        title = choose_representative_item([{**item, "source_reliability": item["reliability_score"]} for item in items])["title"]
        existing = db.query_one("SELECT id FROM event_clusters WHERE similarity_reason = ?", (f"rule:{key}",))
        if existing:
            cluster_id = existing["id"]
        else:
            first_seen = min(item["fetched_at"] for item in items)
            last_seen = max(item["fetched_at"] for item in items)
            cluster_id = db.execute(
                """
                INSERT INTO event_clusters (canonical_title, duplicate_count, first_seen_at, last_seen_at, similarity_reason)
                VALUES (?, ?, ?, ?, ?)
                """,
                (title, len(items), first_seen, last_seen, f"rule:{key}"),
            )
        clusters[cluster_id] = items
    return clusters


def _cluster_key(item: dict[str, Any]) -> str:
    title = normalize_title(item["title"])
    lowered = title.lower()
    if ("电池" in title or "battery" in lowered) and ("政策" in title or "监管" in title or "policy" in lowered):
        return "battery-policy"
    if "英伟达" in title or "nvidia" in lowered or "ai芯片" in lowered or "ai chip" in lowered:
        return "nvidia-ai-chip"
    if "openai" in lowered or "智能体" in title or "agent" in lowered:
        return "openai-agent"
    if "宁德时代" in title or "catl" in lowered:
        return "catl-capacity"
    if "比亚迪" in title or "byd" in lowered:
        if "海外" in title or "oversea" in lowered or "export" in lowered:
            return "byd-overseas"
    return title[:28]


def _update_cluster_representative(db: Database, cluster_id: int, representative: dict[str, Any], duplicate_count: int) -> None:
    processed = db.query_one("SELECT id FROM processed_items WHERE raw_item_id = ?", (representative["id"],))
    representative_item_id = processed["id"] if processed else None
    db.execute(
        """
        UPDATE event_clusters
        SET canonical_title = ?, duplicate_count = ?, representative_item_id = COALESCE(?, representative_item_id),
            last_seen_at = ?
        WHERE id = ?
        """,
        (representative["title"], duplicate_count, representative_item_id, representative.get("fetched_at"), cluster_id),
    )


def _source_result(source: dict[str, Any], status: str, candidate_count: int, error: str | None) -> dict[str, Any]:
    result = {
        "source_id": source["id"],
        "source_name": source["name"],
        "source_url": source["url"],
        "status": status,
        "candidate_count": candidate_count,
    }
    if error:
        result["error"] = error
    return result


def _trace_node(node: str, input_payload: dict[str, Any], output_payload: dict[str, Any]) -> dict[str, Any]:
    return {"node": node, "input": input_payload, "output": output_payload}


def _sample_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for candidate in candidates:
        summary = {
            "source_id": candidate.get("source_id"),
            "title": candidate.get("title"),
            "url": candidate.get("url"),
            "published_at": candidate.get("published_at"),
        }
        if "relevance_industry" in candidate:
            summary.update(
                {
                    "industry": candidate.get("relevance_industry"),
                    "reason": candidate.get("relevance_reason"),
                    "confidence": candidate.get("relevance_confidence"),
                    "matched_terms": candidate.get("relevance_matched_terms"),
                    "provider": candidate.get("relevance_provider"),
                    "model": candidate.get("relevance_model"),
                }
            )
        summaries.append(summary)
    return summaries


def _raw_item_trace_summary(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "raw_item_id": item.get("id"),
        "source_id": item.get("source_id"),
        "source_name": item.get("source_name"),
        "title": item.get("title"),
        "url": item.get("url"),
        "published_at": item.get("published_at"),
        "fetched_at": item.get("fetched_at"),
        "status": item.get("status"),
        "content_excerpt": item.get("content_excerpt"),
        "relevance": {
            "industry": item.get("relevance_industry"),
            "confidence": item.get("relevance_confidence"),
            "reason": item.get("relevance_reason"),
            "matched_terms": item.get("relevance_matched_terms"),
            "provider": item.get("relevance_provider"),
            "model": item.get("relevance_model"),
        },
    }


def _cluster_trace_summaries(clusters: dict[int, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for cluster_id, items in clusters.items():
        representative = choose_representative_item([{**item, "source_reliability": item["reliability_score"]} for item in items])
        summaries.append(
            {
                "cluster_id": cluster_id,
                "canonical_title": representative["title"],
                "raw_item_count": len(items),
                "raw_item_ids": [item["id"] for item in items],
                "source_names": sorted({item.get("source_name") for item in items if item.get("source_name")}),
            }
        )
    return summaries


def _processed_item_trace_summary(
    db: Database,
    processed: dict[str, Any],
    raw_item: dict[str, Any],
    cluster_id: int,
) -> dict[str, Any]:
    tags = db.query(
        "SELECT tag_dimension, tag_value FROM item_tags WHERE processed_item_id = ? ORDER BY tag_dimension, tag_value",
        (processed["id"],),
    )
    tags_by_dimension: dict[str, list[str]] = {}
    for tag in tags:
        tags_by_dimension.setdefault(tag["tag_dimension"], []).append(tag["tag_value"])
    return {
        "processed_item_id": processed["id"],
        "raw_item_id": raw_item["id"],
        "cluster_id": cluster_id,
        "title": raw_item["title"],
        "url": raw_item["url"],
        "importance_level": processed.get("importance_level"),
        "industry": tags_by_dimension.get("industry", [""])[0],
        "industry_subtags": tags_by_dimension.get("industry_subtag", []),
        "event_types": tags_by_dimension.get("event_type", []),
        "rank_score": processed.get("rank_score"),
        "extraction_mode": processed.get("extraction_mode"),
        "extraction_provider": processed.get("extraction_provider"),
        "extraction_model": processed.get("extraction_model"),
        "extraction_fallback_reason": processed.get("extraction_fallback_reason"),
        "status": "existing",
    }


def decode_item(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    defaults = {
        "key_facts_json": {},
        "entities_json": [],
        "score_breakdown_json": {},
        "source_spans_json": [],
        "node_trace_json": [],
    }
    for key, default in defaults.items():
        if key in item:
            item[key.replace("_json", "")] = from_json(item.get(key), default)
    return item
