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
            "你是产业舆情 Agent 的 extract_intelligence 核心信息提炼节点。"
            "你的职责是从已入库文章中提炼可追踪的产业情报摘要、关键事实、产业影响和证据片段。"
            "classification 是上游结构化上下文，但 summary、key_facts、impact_analysis 必须回到原文证据。"
            "不要编造原文没有的主体、时间、金额、地点、因果或产业影响；不要复述导航、脚本、模板和无关背景。"
            "必须只输出 JSON。key_facts 必须是 JSON object，不是数组。source_spans.field 只能是 raw_content、content_excerpt 或 title。"
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
            "input_contract": _extraction_input_contract(),
            "summary_requirements": _summary_requirements(),
            "key_facts_schema": _key_facts_schema(),
            "impact_analysis_rules": _impact_analysis_rules(),
            "source_span_requirements": _source_span_requirements(),
            "output_requirements": _extraction_output_requirements(),
            "required_json_fields": [
                "summary",
                "key_facts",
                "impact_analysis",
                "source_spans",
            ],
            "source_span_schema": {"field": "raw_content|content_excerpt|title", "start": 0, "end": 120, "quote": "evidence text"},
        },
    )


def _extraction_input_contract() -> dict[str, str]:
    return {
        "title": "文章标题，可作为事实证据，但不能替代正文。",
        "content": "清洗并截断后的正文，是摘要、关键事实和证据片段的主要依据。",
        "content_excerpt": "正文短摘录，仅作定位和兜底参考。",
        "source_name": "来源名称，用于判断公开披露语境，不代表事实本身。",
        "source_type": "来源类型，用于解释信息属性，不得替代原文证据。",
        "published_at": "文章发布时间；只有输入存在时才能写入 key_facts.when。",
        "fetched_at": "采集时间；不得当作事件发生时间，除非 published_at 缺失且需要兜底。",
        "classification": "上游分类结果，用于约束产业语境和影响角度，不能用来编造原文没有的事实。",
    }


def _summary_requirements() -> dict[str, str]:
    return {
        "goal": "用 1-2 句提炼产业情报结论，优先回答谁做了什么、为什么值得跟踪。",
        "style": "客观、克制、信息密度高；不要营销话术、标题党、空泛判断或重复分类标签。",
        "grounding": "summary 中出现的主体、动作、时间、产品、能力和影响必须能在 title/content/content_excerpt 中找到依据。",
        "noise_filter": "忽略导航、脚本、CSS、站点模板、广告、免责声明和泛背景。",
    }


def _key_facts_schema() -> dict[str, str]:
    return {
        "who": "事件主体；优先使用原文明确出现的公司、机构、监管部门、产品或项目名称。",
        "what": "核心动作或事实；用短句说明发生了什么，不要写评论。",
        "when": "发布时间、事件时间或原文明确时间；没有时间时可用 published_at，仍缺失则留空字符串。",
        "where": "事件地点、市场或披露场景；原文未说明时写“公开来源披露”。",
        "why": "原文中给出的原因、背景或需求；没有明确原因时写“原文未披露明确原因”。",
        "impact": "对对应产业的直接跟踪价值，必须和 classification 的行业/事件类型保持一致。",
        "evidence": "一段能支撑关键事实的原文证据，优先直接摘取 title/content 中的短句。",
    }


def _impact_analysis_rules() -> list[str]:
    return [
        "只分析产业影响，不做投资建议、情绪化判断或泛泛评价。",
        "影响分析必须连接 classification.industry、event_types、signal_attributes 与原文事实。",
        "如果原文只披露产品发布，影响分析应聚焦商业化、竞争或技术路线的可观察变化，不扩写成确定性结果。",
        "如果原文证据有限，要明确使用保守表达，例如“显示”“可能提示”“需继续跟踪”。",
        "不得加入原文没有的市场份额、金额、客户、监管结论或因果链。",
    ]


def _source_span_requirements() -> dict[str, Any]:
    return {
        "allowed_fields": ["raw_content", "content_excerpt", "title"],
        "purpose": "每个 span 必须支撑 summary、key_facts 或 impact_analysis 中的一个关键判断。",
        "quote": "应为原文短摘录；不得改写、扩写或引用输出文本自身。",
        "positions": "start/end 尽量对应 quote 在原字段中的位置；不确定时仍需提供 quote，系统会校验和兜底。",
        "count": "返回 1-3 个最高价值证据片段即可。",
    }


def _extraction_output_requirements() -> dict[str, Any]:
    return {
        "format": "只返回单个 JSON object，不要 Markdown 或解释性正文。",
        "required_fields": ["summary", "key_facts", "impact_analysis", "source_spans"],
        "key_facts_required_fields": ["who", "what", "when", "where", "why", "impact", "evidence"],
        "source_spans": "数组；每项包含 field、start、end、quote。",
        "evidence_policy": "所有关键结论必须由 title/content/content_excerpt 中的证据支撑；证据不足时保守表达，不要编造。",
        "fallback_policy": "无法确定的字段填保守文本或空字符串，系统会在校验失败时回退到规则提炼。",
    }


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
