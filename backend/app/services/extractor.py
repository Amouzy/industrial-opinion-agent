from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
from typing import Any

from app.services.collector import clean_article_text, compact_text
from app.services.llm import LLMClient


class ExtractionError(ValueError):
    """Raised when model output cannot be safely saved as intelligence extraction."""


@dataclass(frozen=True)
class ExtractionResult:
    summary: str
    key_facts: dict[str, Any]
    impact_analysis: str
    source_spans: list[dict[str, Any]]
    provider: str
    model: str
    mode: str
    fallback_reason: str | None = None


def extract_intelligence(
    item: dict[str, Any],
    classification: Any,
    llm_client: LLMClient | None = None,
) -> ExtractionResult:
    if llm_client and llm_client.is_configured:
        try:
            payload = _extract_with_llm(item, classification, llm_client)
            return _result_from_llm_payload(item, classification, payload, llm_client)
        except Exception as exc:
            return _fallback_result(item, classification, f"invalid_llm_payload: {exc}")
    return _fallback_result(item, classification, "llm_unconfigured")


def extract_key_facts(item: dict[str, Any], classification: Any) -> dict[str, Any]:
    content = _best_clean_content(item, "raw_content", "content_excerpt", "title")
    subject = _first_entity_text(getattr(classification, "entities", []) or []) or item.get("author") or "相关主体"
    return {
        "who": subject,
        "what": item["title"],
        "when": item.get("published_at") or item.get("fetched_at"),
        "where": "公开来源披露",
        "why": classification.importance_reason,
        "impact": build_impact_sentence(item, classification),
        "evidence": content[:180],
    }


def build_summary(item: dict[str, Any], classification: Any) -> str:
    excerpt = _best_clean_content(item, "content_excerpt", "raw_content", "title")
    tags = "、".join(classification.event_types[:2])
    return f"{excerpt[:150]} 该信息被识别为{tags}，重要性为 {classification.importance_level}。"


def _first_entity_text(entities: Any) -> str:
    if not isinstance(entities, list):
        return ""
    for entity in entities:
        if isinstance(entity, dict) and str(entity.get("text") or "").strip():
            return str(entity["text"]).strip()
        if isinstance(entity, str) and entity.strip():
            return entity.strip()
    return ""


def _extract_with_llm(item: dict[str, Any], classification: Any, llm_client: LLMClient) -> dict[str, Any]:
    content = _bounded_llm_content(item)
    return llm_client.complete_json(
        system_prompt=(
            "你是产业舆情 Agent 的核心信息提炼节点。必须只输出 JSON。"
            "摘要要基于原文证据提炼产业情报结论，不要复述噪声、导航、脚本或无关背景。"
            "不得编造原文没有的信息。key_facts 必须是 JSON object，不是数组。"
            "source_spans.field 只能是 raw_content、content_excerpt 或 title。"
        ),
        user_payload={
            "title": item.get("title"),
            "url": item.get("url"),
            "source_name": item.get("source_name"),
            "source_type": item.get("source_type"),
            "published_at": item.get("published_at"),
            "fetched_at": item.get("fetched_at"),
            "content": content,
            "content_excerpt": content[:600],
            "classification": {
                "industry": getattr(classification, "industry", None),
                "industry_subtags": getattr(classification, "industry_subtags", []),
                "event_types": getattr(classification, "event_types", []),
                "importance_level": getattr(classification, "importance_level", None),
                "importance_reason": getattr(classification, "importance_reason", None),
                "subject_roles": getattr(classification, "subject_roles", []),
                "signal_attributes": getattr(classification, "signal_attributes", []),
                "confidence": getattr(classification, "confidence", None),
                "entities": getattr(classification, "entities", []),
            },
            "required_json_fields": [
                "summary",
                "key_facts",
                "impact_analysis",
                "source_spans",
            ],
            "key_facts_schema": ["who", "what", "when", "where", "why", "impact", "evidence"],
            "source_span_schema": {"field": "raw_content|content_excerpt|title", "start": 0, "end": 120, "quote": "evidence text"},
        },
    )


def _result_from_llm_payload(
    item: dict[str, Any],
    classification: Any,
    payload: dict[str, Any],
    llm_client: LLMClient,
) -> ExtractionResult:
    if not isinstance(payload, dict):
        raise ExtractionError("payload is not a JSON object")
    summary = _clean_required_text(payload.get("summary"), "summary")
    impact_analysis = _clean_required_text(payload.get("impact_analysis"), "impact_analysis")
    key_facts = _validate_key_facts(payload.get("key_facts"), item, classification)
    source_spans = _validate_source_spans(payload.get("source_spans"), item)
    return ExtractionResult(
        summary=summary,
        key_facts=key_facts,
        impact_analysis=impact_analysis,
        source_spans=source_spans,
        provider=getattr(llm_client.settings, "provider", "llm"),
        model=getattr(llm_client.settings, "model", "unknown"),
        mode="llm",
    )


def _fallback_result(item: dict[str, Any], classification: Any, reason: str) -> ExtractionResult:
    key_facts = extract_key_facts(item, classification)
    return ExtractionResult(
        summary=build_summary(item, classification),
        key_facts=key_facts,
        impact_analysis=key_facts["impact"],
        source_spans=_fallback_source_spans(item),
        provider="rules+extractor",
        model="local-rules",
        mode="rules_fallback",
        fallback_reason=reason,
    )


def _validate_key_facts(value: Any, item: dict[str, Any], classification: Any) -> dict[str, Any]:
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
        value = value[0]
    if not isinstance(value, dict):
        raise ExtractionError("key_facts is not an object")
    fallback: dict[str, Any] | None = None
    validated: dict[str, Any] = {}
    for field in ["who", "what", "when", "where", "why", "impact", "evidence"]:
        raw = value.get(field)
        cleaned = clean_article_text(str(raw)) if raw is not None else ""
        if cleaned:
            validated[field] = cleaned
            continue
        if fallback is None:
            fallback = extract_key_facts(item, classification)
        validated[field] = fallback[field]
    return validated


def _validate_source_spans(value: Any, item: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return _fallback_source_spans(item)
    spans: list[dict[str, Any]] = []
    allowed_fields = {"raw_content", "content_excerpt", "title"}
    for raw_span in value:
        if not isinstance(raw_span, dict):
            continue
        field = str(raw_span.get("field") or "raw_content")
        if field == "content":
            field = "raw_content"
        if field not in allowed_fields:
            field = "raw_content"
        source_text = str(item.get(field) or "")
        start = _safe_int(raw_span.get("start"), 0)
        end = _safe_int(raw_span.get("end"), min(len(source_text), start + 180))
        start = max(0, min(start, len(source_text)))
        end = max(start, min(end, len(source_text)))
        quote = clean_article_text(str(raw_span.get("quote") or source_text[start:end]))
        spans.append({"field": field, "start": start, "end": end, "quote": quote[:260]})
    return spans or _fallback_source_spans(item)


def _fallback_source_spans(item: dict[str, Any]) -> list[dict[str, Any]]:
    field = "raw_content" if item.get("raw_content") else "content_excerpt" if item.get("content_excerpt") else "title"
    content = clean_article_text(str(item.get(field) or ""))
    return [{"field": field, "start": 0, "end": min(180, len(str(item.get(field) or ""))), "quote": content[:180]}]


def _bounded_llm_content(item: dict[str, Any], max_chars: int = 3500) -> str:
    # Keep LLM calls fast and cheap. raw_content may still contain long page shells,
    # so only clean a bounded slice with lightweight patterns here.
    for key in ["raw_content", "content_excerpt", "title"]:
        raw = str(item.get(key) or "")
        if not raw:
            continue
        cleaned = _light_clean_for_llm(raw[:max_chars], max_chars=max_chars)
        if cleaned:
            return cleaned
    return ""


def _light_clean_for_llm(value: str, max_chars: int) -> str:
    text = compact_text(unescape(value or ""))
    if not text:
        return ""
    light_patterns = [
        r"(?is)<(script|style|noscript|template|svg|canvas|iframe|object|embed)\b[^>]*>.*?</\1>",
        r"(?is)<!--.*?-->",
        r"(?is)\(function\s*\(\s*html\s*\)\s*\{.*?document\.documentElement\s*\)\s*;?",
        r"(?is)\$\.ajax\s*\(.*?(?=(?:[\u4e00-\u9fffA-Za-z]{4,}|$))",
        r"(?is)<[^>]+>",
    ]
    for pattern in light_patterns:
        text = re.sub(pattern, " ", text)
    text = compact_text(text)
    if not _has_readable_text(text):
        return ""
    return text[:max_chars]


def _has_readable_text(text: str) -> bool:
    readable_chars = sum(1 for char in text if "\u4e00" <= char <= "\u9fff" or char.isalpha() or char.isdigit())
    return readable_chars >= 12


def _clean_required_text(value: Any, field: str) -> str:
    cleaned = clean_article_text(str(value or ""))
    if not cleaned:
        raise ExtractionError(f"missing or empty {field}")
    return cleaned


def _best_clean_content(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        cleaned = clean_article_text(item.get(key) or "")
        if cleaned:
            return cleaned
    return ""


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def build_impact_sentence(item: dict[str, Any], classification: Any) -> str:
    industry = classification.industry
    signals = "、".join(classification.signal_attributes[:2])
    if classification.importance_level == "high":
        return f"对{industry}研究具有较高优先级，主要体现为{signals}，建议优先追踪政策落地、主体动作和后续相似报道。"
    if classification.importance_level == "medium":
        return f"对{industry}具有跟踪价值，主要体现为{signals}，建议纳入观察列表。"
    return f"对{industry}影响有限，适合作为背景素材保留。"
