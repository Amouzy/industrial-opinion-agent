from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.logging_config import APP_LOGGER_NAME
from app.services.llm import LLMClient


logger = logging.getLogger(APP_LOGGER_NAME)


@dataclass(frozen=True)
class RelevanceResult:
    is_relevant: bool
    industry: str
    reason: str
    confidence: float
    matched_terms: list[str] = field(default_factory=list)
    provider: str = "rules-fallback"
    model: str = "local-rules"

    def to_trace(self, candidate: dict[str, Any]) -> dict[str, Any]:
        return {
            "source_id": candidate.get("source_id"),
            "title": candidate.get("title"),
            "url": candidate.get("url"),
            "is_relevant": self.is_relevant,
            "industry": self.industry,
            "reason": self.reason,
            "confidence": self.confidence,
            "matched_terms": self.matched_terms,
            "provider": self.provider,
            "model": self.model,
        }


NEV_TERMS = [
    "新能源汽车",
    "新能源车",
    "电动汽车",
    "动力电池",
    "电池",
    "智能驾驶",
    "自动驾驶",
    "辅助驾驶",
    "充电",
    "换电",
    "充换电",
    "汽车芯片",
    "车载软件",
    "整车",
    "比亚迪",
    "宁德时代",
    "CATL",
    "battery",
    "energy storage",
    "chassis",
    "zero-carbon",
    "Togg",
    "特斯拉",
    "蔚来",
    "小鹏",
    "理想汽车",
    "FSD",
    "EV",
]

AI_TERMS = [
    "人工智能",
    "AI",
    "大模型",
    "基础模型",
    "模型",
    "智能体",
    "Agent",
    "算力",
    "GPU",
    "AI芯片",
    "AI 芯片",
    "数据中心",
    "机器人",
    "人形机器人",
    "具身智能",
    "物理AI",
    "物理 AI",
    "英伟达",
    "NVIDIA",
    "OpenAI",
    "Anthropic",
    "Claude",
    "Microsoft AI",
    "Google AI",
]

INTERNAL_OR_GENERAL_NEGATIVE_TERMS = [
    "假冒学历",
    "学历认证",
    "公安部网安",
    "网络诈骗",
    "青年理论学习",
    "机关党建",
    "共青团",
    "代表大会",
    "纪录片",
    "文化情缘",
    "儿童成长",
    "学习小组",
]


def screen_relevance(item: dict[str, Any], llm_client: LLMClient | None = None) -> RelevanceResult:
    """Decide whether a raw candidate belongs in the industry pipeline.

    Source hints are only supporting context. A candidate must contain concrete
    industry terms in the title/content to continue into tagging and ranking.
    """
    if llm_client and llm_client.is_configured:
        llm_result = _screen_with_llm(item, llm_client)
        if llm_result:
            return llm_result
    return _screen_with_rules(item)


def _screen_with_llm(item: dict[str, Any], llm_client: LLMClient) -> RelevanceResult | None:
    try:
        payload = llm_client.complete_json(
            system_prompt=(
                "你是产业舆情 Agent 的 relevance_screen 相关性筛选节点。"
                "你的职责是只判断是否进入新能源汽车或人工智能产业情报流水线；不做分类打标、摘要、排序或事实扩写。"
                "必须基于标题、正文和来源元数据中的证据判断，industry_hint 只能作为弱上下文。"
                "不要编造正文没有出现的主体、事件、产业归属或关键词。"
                "必须只输出 JSON，industry 只能是 新能源汽车、人工智能 或空字符串。"
            ),
            user_payload={
                "title": item.get("title"),
                "content": item.get("raw_content") or item.get("content_excerpt"),
                "content_excerpt": item.get("content_excerpt"),
                "source_name": item.get("source_name"),
                "source_type": item.get("source_type"),
                "url": item.get("url"),
                "published_at": item.get("published_at"),
                "fetched_at": item.get("fetched_at"),
                "industry_hint": item.get("industry_hint"),
                "industry_definitions": _industry_definitions(),
                "exclusion_rules": _relevance_exclusion_rules(),
                "decision_rules": _relevance_decision_rules(),
                "confidence_calibration": _relevance_confidence_calibration(),
                "output_requirements": _relevance_output_requirements(),
                "required_json_fields": ["is_relevant", "industry", "reason", "confidence", "matched_terms"],
            },
        )
    except Exception as exc:
        logger.warning(
            "relevance_llm_failed source_id=%s title=%r url=%s provider=%s model=%s error=%s",
            item.get("source_id"),
            item.get("title"),
            item.get("url"),
            getattr(llm_client.settings, "provider", "unknown"),
            getattr(llm_client.settings, "model", "unknown"),
            exc,
            exc_info=True,
        )
        return None
    industry = str(payload.get("industry") or "")
    if industry not in {"新能源汽车", "人工智能", ""}:
        logger.warning(
            "relevance_llm_invalid_payload source_id=%s title=%r url=%s provider=%s model=%s invalid_industry=%r payload_keys=%s",
            item.get("source_id"),
            item.get("title"),
            item.get("url"),
            getattr(llm_client.settings, "provider", "unknown"),
            getattr(llm_client.settings, "model", "unknown"),
            industry,
            sorted(payload.keys()) if isinstance(payload, dict) else type(payload).__name__,
        )
        return None
    if bool(payload.get("is_relevant")) and industry not in {"新能源汽车", "人工智能"}:
        logger.warning(
            "relevance_llm_invalid_payload source_id=%s title=%r url=%s provider=%s model=%s relevant_without_industry payload_keys=%s",
            item.get("source_id"),
            item.get("title"),
            item.get("url"),
            getattr(llm_client.settings, "provider", "unknown"),
            getattr(llm_client.settings, "model", "unknown"),
            sorted(payload.keys()) if isinstance(payload, dict) else type(payload).__name__,
        )
        return None
    matched_terms = [str(term) for term in payload.get("matched_terms", []) if str(term).strip()]
    return RelevanceResult(
        is_relevant=bool(payload.get("is_relevant")),
        industry=industry,
        reason=str(payload.get("reason") or "LLM 相关性判断"),
        confidence=_clamp(payload.get("confidence"), 0.0, 1.0),
        matched_terms=matched_terms,
        provider=llm_client.settings.provider,
        model=llm_client.settings.model,
    )


def _industry_definitions() -> dict[str, str]:
    return {
        "新能源汽车": (
            "与新能源整车、动力电池、智能驾驶、充换电、车载软件、汽车芯片、上游材料、"
            "产能交付、海外市场、政策监管或核心企业动向直接相关的产业事实。"
        ),
        "人工智能": (
            "与基础模型、AI 芯片、算力基础设施、AI 应用、智能体/Agent、数据与安全、"
            "机器人、企业服务、政策监管、核心企业动向或商业化进展直接相关的产业事实。"
        ),
    }


def _relevance_exclusion_rules() -> list[str]:
    return [
        "泛社会新闻、网络诈骗、学历认证、治安通报等不因来源为政府或含弱提示而进入产业情报库。",
        "内部会议、党建学习、青年理论学习、代表大会、宣传纪录片等组织活动，除非正文有明确产业政策或产业项目事实，否则拒绝。",
        "列表页、分页导航、栏目模板、站点宣传语、广告导流、采购索引等低信息正文必须拒绝。",
        "只出现 AI、EV、电池、模型等短词，但上下文不是产业事件时必须拒绝。",
        "只有 industry_hint 或 source_type 指向某产业、正文没有实质产业事实时必须拒绝。",
    ]


def _relevance_decision_rules() -> list[str]:
    return [
        "只判断候选是否应进入后续 normalize/deduplicate/classify/rank/extract 流水线。",
        "is_relevant=true 必须同时满足：正文或标题有实质产业事实，并且能归入且只能归入一个目标行业。",
        "industry_hint 只能辅助理解来源配置，不能替代正文证据；正文不足时 is_relevant=false。",
        "matched_terms 必须来自标题、正文或摘要中的实际词语，不要输出同义扩展词。",
        "reason 用一句话说明放行或拒绝的证据，指出关键行业事实或拒绝原因。",
        "无法区分新能源汽车与人工智能时，选择正文证据更强的行业；证据不足则拒绝。",
    ]


def _relevance_confidence_calibration() -> dict[str, str]:
    return {
        "0.90-1.00": "标题和正文都有明确产业主体、事件和行业归属，且排除规则不命中。",
        "0.75-0.89": "正文有明确产业事实，但细节较少或来源上下文仍需后续节点补充。",
        "0.55-0.74": "存在产业相关事实但证据有限；可放行但 reason 必须说明不确定性。",
        "0.00-0.54": "只有弱相关词、来源提示、模板文本或泛新闻，应拒绝。",
    }


def _relevance_output_requirements() -> dict[str, Any]:
    return {
        "format": "只返回单个 JSON object，不要 Markdown 或解释性正文。",
        "required_fields": ["is_relevant", "industry", "reason", "confidence", "matched_terms"],
        "industry_enum": ["新能源汽车", "人工智能", ""],
        "matched_terms": "数组；只能包含输入文本中真实出现并支撑判断的关键词或短语。",
        "reason": "一句话，必须引用标题、正文、来源属性或排除规则中的证据。",
        "negative_case": "is_relevant=false 时 industry 必须为空字符串，matched_terms 可为空或列出导致拒绝的噪声词。",
    }


def _screen_with_rules(item: dict[str, Any]) -> RelevanceResult:
    text = _text(item)
    low_information_reason = _low_information_reason(item, text)
    if low_information_reason:
        return RelevanceResult(False, "", low_information_reason, 0.9, [])
    negative = [term for term in INTERNAL_OR_GENERAL_NEGATIVE_TERMS if term in text]
    nev_matches = _matches(text, NEV_TERMS)
    ai_matches = _matches(text, AI_TERMS)
    if negative and not nev_matches and not ai_matches:
        reason = "内部会议或泛社会新闻，未命中产业关键词"
        if any(term in text for term in ["青年理论学习", "机关党建", "共青团", "代表大会", "学习小组"]):
            reason = "内部会议，未命中产业关键词"
        return RelevanceResult(False, "", reason, 0.9, negative)
    if not nev_matches and not ai_matches:
        return RelevanceResult(False, "", "未命中产业关键词，来源行业提示不足以入库", 0.85, [])
    if ai_matches and (not nev_matches or len(ai_matches) >= len(nev_matches)):
        return RelevanceResult(True, "人工智能", "命中人工智能产业关键词", 0.72, ai_matches)
    return RelevanceResult(True, "新能源汽车", "命中新能源汽车产业关键词", 0.72, nev_matches)


def _text(item: dict[str, Any]) -> str:
    return " ".join(
        str(item.get(key) or "")
        for key in ["title", "raw_content", "content_excerpt", "summary"]
    )


def _low_information_reason(item: dict[str, Any], text: str) -> str:
    title = str(item.get("title") or "").strip()
    raw_content = str(item.get("raw_content") or item.get("content_excerpt") or "").strip()
    if title in {"最后一页", "上一页", "下一页", "尾页"}:
        return "正文信息不足：疑似列表页或分页导航，不应进入产业情报库"
    shell_markers = [
        "资讯栏目每天对新能源电动汽车行业的新闻资讯第一时间报道",
        "盖世汽车提供及时全面的:汽车销量、汽车资讯、汽车行业新闻、汽车销量排行榜、新能源汽车资讯、电动汽车资讯",
        "企业库 销量 查询 采购 项目",
    ]
    if any(marker in text for marker in shell_markers):
        if len(raw_content) < 140 or _mostly_source_boilerplate(title, raw_content):
            return "正文信息不足：疑似来源栏目模板或站点宣传语，不应进入产业情报库"
    return ""


def _mostly_source_boilerplate(title: str, raw_content: str) -> bool:
    remainder = raw_content.replace(title, "", 1).strip(" -_｜|：:")
    boilerplate_terms = ["盖世汽车", "汽车销量", "汽车资讯", "汽车行业新闻", "新能源汽车资讯", "电动汽车资讯", "资讯栏目", "第一时间报道"]
    return bool(remainder) and sum(1 for term in boilerplate_terms if term in remainder) >= 2


def _matches(text: str, terms: list[str]) -> list[str]:
    lowered = text.lower()
    matched: list[str] = []
    for term in terms:
        if _contains_cjk(term):
            if term in text:
                matched.append(term)
            continue
        if _ascii_term_matches(term, lowered):
            matched.append(term)
    return list(dict.fromkeys(matched))


def _contains_cjk(value: str) -> bool:
    return any("一" <= char <= "鿿" for char in value)


def _ascii_term_matches(term: str, lowered_text: str) -> bool:
    lowered_term = term.lower()
    if not lowered_term.strip():
        return False
    escaped = re.escape(lowered_term)
    return re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", lowered_text) is not None


def _clamp(value: Any, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return minimum
    return max(minimum, min(maximum, number))
