from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.services.llm import LLMClient


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
                "你是产业舆情 Agent 的相关性筛选节点。只判断文章是否属于新能源汽车或人工智能产业情报。"
                "必须输出 JSON：is_relevant(boolean), industry(string), reason(string), confidence(number), matched_terms(array)。"
                "industry 只能是 新能源汽车、人工智能 或空字符串。无关泛新闻必须 is_relevant=false。"
            ),
            user_payload={
                "title": item.get("title"),
                "content": item.get("raw_content") or item.get("content_excerpt"),
                "source_type": item.get("source_type"),
                "industry_hint": item.get("industry_hint"),
            },
        )
    except Exception:
        return None
    industry = str(payload.get("industry") or "")
    if industry not in {"新能源汽车", "人工智能", ""}:
        return None
    if bool(payload.get("is_relevant")) and industry not in {"新能源汽车", "人工智能"}:
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
