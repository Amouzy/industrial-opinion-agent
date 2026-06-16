from __future__ import annotations

import logging
import time
from datetime import datetime
from functools import lru_cache
from typing import Any, Callable, TypedDict

from langgraph.graph import END, StateGraph

from app.database import Database, to_json
from app.logging_config import APP_LOGGER_NAME
from app.services.llm import LLMClient
from app.services.time_utils import utc_now_iso

logger = logging.getLogger(APP_LOGGER_NAME)


class SourceIntervalState(TypedDict, total=False):
    db: Database
    run_id: int
    trigger_type: str
    current: datetime
    fetcher: Callable[[dict[str, Any]], list[dict[str, Any]]]
    force_all: bool
    trace: list[dict[str, Any]]
    raw_candidates: list[dict[str, Any]]
    source_results: list[dict[str, Any]]
    source_windows: list[dict[str, Any]]
    failed_count: int
    llm_client: LLMClient
    relevant_candidates: list[dict[str, Any]]
    rejected_candidates: list[dict[str, Any]]
    completion: dict[str, Any]
    result: dict[str, Any]


@lru_cache(maxsize=1)
def build_source_interval_graph() -> Any:
    graph = StateGraph(SourceIntervalState)
    graph.add_node("collect", _collect_node)
    graph.add_node("relevance_screen", _relevance_screen_node)
    graph.add_node("process_candidates", _process_candidates_node)
    graph.set_entry_point("collect")
    graph.add_edge("collect", "relevance_screen")
    graph.add_edge("relevance_screen", "process_candidates")
    graph.add_edge("process_candidates", END)
    return graph.compile()


def run_source_interval_graph(
    db: Database,
    run_id: int,
    trigger_type: str,
    current: datetime,
    fetcher: Callable[[dict[str, Any]], list[dict[str, Any]]],
    force_all: bool,
) -> dict[str, Any]:
    started = time.perf_counter()
    logger.info("workflow_graph_start run_id=%s trigger_type=%s force_all=%s", run_id, trigger_type, force_all)
    compiled_graph = build_source_interval_graph()
    final_state = compiled_graph.invoke(
        {
            "db": db,
            "run_id": run_id,
            "trigger_type": trigger_type,
            "current": current,
            "fetcher": fetcher,
            "force_all": force_all,
            "trace": [],
            "raw_candidates": [],
            "source_results": [],
            "source_windows": [],
            "failed_count": 0,
        }
    )
    completion = final_state.get("completion", {})
    result = _mark_success(db, run_id, completion)
    logger.info(
        "workflow_graph_finish run_id=%s trigger_type=%s collected=%s deduped=%s classified=%s extracted=%s failed=%s duration_seconds=%.3f",
        run_id,
        trigger_type,
        completion.get("collected_count", 0),
        completion.get("deduped_count", 0),
        completion.get("classified_count", 0),
        completion.get("extracted_count", 0),
        completion.get("failed_count", 0),
        time.perf_counter() - started,
    )
    return result


def _collect_node(state: SourceIntervalState) -> dict[str, Any]:
    from app.services.workflow import (
        _sample_candidates,
        _source_collection_window,
        _source_result,
        _trace_node,
        due_sources,
    )

    db = state["db"]
    current = state["current"]
    checked_at = current.isoformat()
    raw_candidates: list[dict[str, Any]] = []
    source_results: list[dict[str, Any]] = []
    source_windows: list[dict[str, Any]] = []
    failed_count = int(state.get("failed_count") or 0)
    target_sources = (
        db.query("SELECT * FROM sources WHERE enabled = 1 ORDER BY reliability_score DESC, id")
        if state["force_all"]
        else due_sources(db, current)
    )
    started = time.perf_counter()
    logger.info(
        "workflow_collect_start run_id=%s trigger_type=%s orchestrator=langgraph force_all=%s target_sources=%s",
        state["run_id"],
        state["trigger_type"],
        state["force_all"],
        len(target_sources),
    )
    try:
        for source in target_sources:
            logger.info(
                "workflow_source_collect_start run_id=%s source_id=%s source_name=%s",
                state["run_id"],
                source["id"],
                source["name"],
            )
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
                candidates = state["fetcher"](
                    {
                        **source,
                        "published_after": window_start,
                        "published_before": window_end,
                    }
                )
            except Exception as exc:
                failed_count += 1
                error = str(exc)
                logger.warning(
                    "workflow_source_collect_failed run_id=%s source_id=%s source_name=%s error=%s",
                    state["run_id"],
                    source["id"],
                    source["name"],
                    error,
                )
                db.execute(
                    "UPDATE sources SET last_checked_at = ?, last_error = ? WHERE id = ?",
                    (checked_at, error, source["id"]),
                )
                source_results.append(_source_result(source, "failed", 0, error))
                continue

            if not candidates:
                logger.info(
                    "workflow_source_collect_empty run_id=%s source_id=%s source_name=%s",
                    state["run_id"],
                    source["id"],
                    source["name"],
                )
                db.execute(
                    "UPDATE sources SET last_checked_at = ?, last_error = NULL WHERE id = ?",
                    (checked_at, source["id"]),
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
            logger.info(
                "workflow_source_collect_success run_id=%s source_id=%s source_name=%s candidates=%s",
                state["run_id"],
                source["id"],
                source["name"],
                len(source_candidates),
            )
            db.execute(
                "UPDATE sources SET last_checked_at = ?, last_fetched_at = ?, last_error = NULL WHERE id = ?",
                (checked_at, checked_at, source["id"]),
            )
            source_results.append(_source_result(source, "success", len(source_candidates), None))

        enabled_count = db.query_one("SELECT COUNT(*) AS count FROM sources WHERE enabled = 1")["count"]
        logger.info(
            "workflow_collect_finish run_id=%s target_sources=%s candidates=%s failed_sources=%s duration_seconds=%.3f",
            state["run_id"],
            len(target_sources),
            len(raw_candidates),
            failed_count,
            time.perf_counter() - started,
        )
        trace = list(state.get("trace") or [])
        trace.append(
            _trace_node(
                "collect",
                {
                    "trigger_type": state["trigger_type"],
                    "orchestrator": "langgraph",
                    "force_all": state["force_all"],
                    "enabled_source_count": enabled_count,
                    "target_source_count": len(target_sources),
                    "target_source_ids": [source["id"] for source in target_sources],
                    "source_windows": source_windows,
                },
                {
                    "orchestrator": "langgraph",
                    "candidate_count": len(raw_candidates),
                    "source_results": source_results,
                    "candidates": _sample_candidates(raw_candidates),
                },
            )
        )
        return {
            "trace": trace,
            "raw_candidates": raw_candidates,
            "source_results": source_results,
            "source_windows": source_windows,
            "failed_count": failed_count,
        }
    except Exception as exc:
        logger.exception("workflow_collect_error run_id=%s trigger_type=%s error=%s", state["run_id"], state["trigger_type"], exc)
        _mark_failed(state, exc)
        raise


def _relevance_screen_node(state: SourceIntervalState) -> dict[str, Any]:
    from app.services.workflow import _sample_candidates, _screen_relevant_candidates, _trace_node, build_llm_client, get_settings

    try:
        started = time.perf_counter()
        llm_client = build_llm_client(get_settings().llm)
        relevant_candidates, rejected_candidates = _screen_relevant_candidates(state["raw_candidates"], llm_client)
        logger.info(
            "workflow_relevance_finish run_id=%s candidates=%s accepted=%s rejected=%s duration_seconds=%.3f",
            state["run_id"],
            len(state["raw_candidates"]),
            len(relevant_candidates),
            len(rejected_candidates),
            time.perf_counter() - started,
        )
        trace = list(state.get("trace") or [])
        trace.append(
            _trace_node(
                "relevance_screen",
                {"candidate_count": len(state["raw_candidates"]), "llm_status": llm_client.runtime_status()},
                {
                    "accepted_count": len(relevant_candidates),
                    "rejected_count": len(rejected_candidates),
                    "accepted_candidates": _sample_candidates(relevant_candidates),
                    "rejected_candidates": rejected_candidates,
                },
            )
        )
        return {
            "trace": trace,
            "llm_client": llm_client,
            "relevant_candidates": relevant_candidates,
            "rejected_candidates": rejected_candidates,
        }
    except Exception as exc:
        logger.exception("workflow_relevance_error run_id=%s trigger_type=%s error=%s", state["run_id"], state["trigger_type"], exc)
        _mark_failed(state, exc)
        raise


def _process_candidates_node(state: SourceIntervalState) -> dict[str, Any]:
    from app.services.workflow import _process_candidates

    try:
        started = time.perf_counter()
        completion = _process_candidates(
            state["db"],
            state["run_id"],
            state["trigger_type"],
            state.get("relevant_candidates", []),
            state["trace"],
            int(state.get("failed_count") or 0),
            llm_client=state.get("llm_client"),
            finalize_run=False,
        )
        logger.info(
            "workflow_process_node_finish run_id=%s collected=%s deduped=%s classified=%s extracted=%s failed=%s duration_seconds=%.3f",
            state["run_id"],
            completion.get("collected_count", 0),
            completion.get("deduped_count", 0),
            completion.get("classified_count", 0),
            completion.get("extracted_count", 0),
            completion.get("failed_count", 0),
            time.perf_counter() - started,
        )
        return {"completion": completion}
    except Exception as exc:
        logger.exception("workflow_process_error run_id=%s trigger_type=%s error=%s", state["run_id"], state["trigger_type"], exc)
        _mark_failed(state, exc)
        raise


def _mark_success(db: Database, run_id: int, completion: dict[str, Any]) -> dict[str, Any]:
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE workflow_runs
            SET ended_at = ?, status = 'success', collected_count = ?, deduped_count = ?,
                classified_count = ?, extracted_count = ?, failed_count = ?, node_trace_json = ?
            WHERE id = ?
            """,
            (
                utc_now_iso(),
                completion.get("collected_count", 0),
                completion.get("deduped_count", 0),
                completion.get("classified_count", 0),
                completion.get("extracted_count", 0),
                completion.get("failed_count", 0),
                to_json(completion.get("trace", [])),
                run_id,
            ),
        )
        row = conn.execute("SELECT * FROM workflow_runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else {}


def _mark_failed(state: SourceIntervalState, exc: Exception) -> None:
    state["db"].execute(
        """
        UPDATE workflow_runs
        SET ended_at = ?, status = 'failed', failed_count = failed_count + 1, error_summary = ?, node_trace_json = ?
        WHERE id = ?
        """,
        (utc_now_iso(), str(exc), to_json(state.get("trace") or []), state["run_id"]),
    )
