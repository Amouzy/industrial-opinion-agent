from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.llm import LLMClient
from app.services.taxonomy import (
    EVENT_TYPE_ORDER,
    EVENT_TYPES,
    IMPORTANCE_LEVELS,
    IMPORTANCE_LEVEL_ORDER,
    INDUSTRIES,
    INDUSTRY_HINTS,
    INDUSTRY_SUBTAGS,
    KEY_ACTORS,
    SIGNAL_ATTRIBUTE_ORDER,
    SIGNAL_ATTRIBUTES,
    SUBJECT_ROLE_ORDER,
    SUBJECT_ROLES,
)


class ClassificationError(ValueError):
    """Raised when a model or rule output violates the fixed taxonomy."""


@dataclass(frozen=True)
class ClassificationResult:
    industry: str
    industry_subtags: list[str]
    event_types: list[str]
    importance_level: str
    importance_reason: str
    subject_roles: list[str]
    signal_attributes: list[str]
    confidence: float
    evidence: list[str] = field(default_factory=list)
    entities: list[dict[str, Any]] = field(default_factory=list)
    key_actor_level: str = "none"
    llm_provider: str = "rules+structured-output"
    llm_model: str = "local-rules"

    def to_tags(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = [{"tag_dimension": "industry", "tag_value": self.industry}]
        rows.extend({"tag_dimension": "industry_subtag", "tag_value": item} for item in self.industry_subtags)
        rows.extend({"tag_dimension": "event_type", "tag_value": item} for item in self.event_types)
        rows.extend({"tag_dimension": "subject_role", "tag_value": item} for item in self.subject_roles)
        rows.extend({"tag_dimension": "signal_attribute", "tag_value": item} for item in self.signal_attributes)
        return rows


def classify_item(
    item: dict[str, Any],
    llm_override: dict[str, Any] | None = None,
    llm_client: LLMClient | None = None,
) -> ClassificationResult:
    text = f"{item.get('title', '')} {item.get('raw_content', '')} {item.get('content_excerpt', '')}"
    source_type = item.get("source_type") or ""
    industry = _detect_industry(item, text)
    rules = _classify_with_rules(item, text, source_type, industry)
    subtags = rules["industry_subtags"]
    event_types = rules["event_types"]
    subject_roles = rules["subject_roles"]
    signal_attributes = rules["signal_attributes"]
    entities = rules["entities"]
    key_actor_level = rules["key_actor_level"]
    importance_level = rules["importance_level"]
    importance_reason = rules["importance_reason"]
    confidence = rules["confidence"]
    llm_provider = "rules+structured-output"
    llm_model = "local-rules"

    if llm_override:
        (
            subtags,
            event_types,
            subject_roles,
            signal_attributes,
            importance_level,
            importance_reason,
            confidence,
            entities,
            key_actor_level,
        ) = _apply_structured_override(industry, llm_override, rules)
    elif llm_client and llm_client.is_configured:
        llm_payload = _classify_with_llm(item, industry, llm_client)
        if llm_payload and _has_complete_llm_payload(llm_payload):
            try:
                (
                    subtags,
                    event_types,
                    subject_roles,
                    signal_attributes,
                    importance_level,
                    importance_reason,
                    confidence,
                    entities,
                    key_actor_level,
                ) = _apply_structured_override(industry, llm_payload, rules)
                llm_provider = llm_client.settings.provider
                llm_model = llm_client.settings.model
            except ClassificationError:
                pass

    _validate_result(industry, subtags, event_types, importance_level, subject_roles, signal_attributes)
    return ClassificationResult(
        industry=industry,
        industry_subtags=subtags,
        event_types=event_types,
        importance_level=importance_level,
        importance_reason=importance_reason,
        subject_roles=subject_roles,
        signal_attributes=signal_attributes,
        confidence=max(0.0, min(1.0, confidence)),
        evidence=_build_evidence(event_types, source_type, key_actor_level),
        entities=entities,
        key_actor_level=key_actor_level,
        llm_provider=llm_provider,
        llm_model=llm_model,
    )


def _classify_with_rules(item: dict[str, Any], text: str, source_type: str, industry: str) -> dict[str, Any]:
    subtags = _detect_subtags(industry, text)
    event_types = _detect_event_types(text, source_type)
    subject_roles = _detect_subject_roles(text, source_type)
    signal_attributes = _detect_signal_attributes(event_types, text)
    entities, key_actor_level = _extract_entities(text)
    importance_level, importance_reason, confidence = _detect_importance(
        event_types=event_types,
        source_type=source_type,
        key_actor_level=key_actor_level,
        text=text,
    )
    return {
        "industry_subtags": subtags,
        "event_types": event_types,
        "subject_roles": subject_roles,
        "signal_attributes": signal_attributes,
        "importance_level": importance_level,
        "importance_reason": importance_reason,
        "confidence": confidence,
        "entities": entities,
        "key_actor_level": key_actor_level,
    }


def _classify_with_llm(item: dict[str, Any], industry: str, llm_client: LLMClient) -> dict[str, Any] | None:
    try:
        return llm_client.complete_json(
            system_prompt=(
                "你是产业舆情 Agent 的分类打标节点。必须只输出 JSON，且所有标签只能从给定枚举中选择。"
                "不要输出最终排序总分。"
            ),
            user_payload={
                "title": item.get("title"),
                "content": item.get("raw_content") or item.get("content_excerpt"),
                "industry": industry,
                "allowed_industry_subtags": INDUSTRY_SUBTAGS[industry],
                "allowed_event_types": EVENT_TYPE_ORDER,
                "allowed_importance_levels": IMPORTANCE_LEVEL_ORDER,
                "allowed_subject_roles": SUBJECT_ROLE_ORDER,
                "allowed_signal_attributes": SIGNAL_ATTRIBUTE_ORDER,
                "required_json_fields": [
                    "industry_subtags",
                    "event_types",
                    "importance_level",
                    "importance_reason",
                    "subject_roles",
                    "signal_attributes",
                    "confidence",
                    "entities",
                    "key_actor_level",
                ],
            },
        )
    except Exception:
        return None


def _apply_structured_override(
    industry: str,
    override: dict[str, Any],
    rules: dict[str, Any],
) -> tuple[list[str], list[str], list[str], list[str], str, str, float, list[dict[str, Any]], str]:
    subtags = _string_list(override.get("industry_subtags", rules["industry_subtags"]))
    event_types = _string_list(override.get("event_types", rules["event_types"]))
    subject_roles = _string_list(override.get("subject_roles", rules["subject_roles"]))
    signal_attributes = _string_list(override.get("signal_attributes", rules["signal_attributes"]))
    importance_level = str(override.get("importance_level", rules["importance_level"]))
    importance_reason = str(override.get("importance_reason", rules["importance_reason"]) or rules["importance_reason"])
    confidence = _clamp(override.get("confidence", rules["confidence"]), 0.0, 1.0)
    entities = override.get("entities", rules["entities"])
    if not isinstance(entities, list):
        entities = rules["entities"]
    key_actor_level = str(override.get("key_actor_level", rules["key_actor_level"]) or rules["key_actor_level"])
    _validate_result(industry, subtags, event_types, importance_level, subject_roles, signal_attributes)
    return (
        subtags,
        event_types,
        subject_roles,
        signal_attributes,
        importance_level,
        importance_reason,
        confidence,
        entities,
        key_actor_level,
    )


def _has_complete_llm_payload(payload: dict[str, Any]) -> bool:
    required_fields = {
        "industry_subtags",
        "event_types",
        "importance_level",
        "importance_reason",
        "subject_roles",
        "signal_attributes",
        "confidence",
        "entities",
        "key_actor_level",
    }
    if not required_fields.issubset(payload):
        return False
    for key in ["industry_subtags", "event_types", "subject_roles", "signal_attributes"]:
        if not isinstance(payload.get(key), list) or not payload.get(key):
            return False
    return True


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _clamp(value: Any, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return minimum
    return max(minimum, min(maximum, number))


def _detect_industry(item: dict[str, Any], text: str) -> str:
    relevance_industry = item.get("relevance_industry")
    if relevance_industry in INDUSTRIES:
        return relevance_industry
    hint = (item.get("industry_hint") or "").split(",")[0].strip()
    if hint in INDUSTRY_HINTS:
        return INDUSTRY_HINTS[hint]
    if any(word in text for word in ["新能源汽车", "动力电池", "智能驾驶", "充换电", "汽车芯片", "比亚迪", "宁德时代", "特斯拉"]):
        return "新能源汽车"
    return "人工智能"


def _detect_subtags(industry: str, text: str) -> list[str]:
    rules = {
        "新能源汽车": [
            ("整车制造", ["整车", "汽车", "比亚迪", "蔚来", "小鹏", "特斯拉"]),
            ("动力电池", ["动力电池", "电池", "宁德时代", "CATL", "battery", "energy storage"]),
            ("智能驾驶", ["智能驾驶", "自动驾驶", "辅助驾驶"]),
            ("充换电基础设施", ["充电", "换电", "充换电"]),
            ("汽车芯片", ["汽车芯片", "车规芯片"]),
            ("车载软件", ["车载软件", "座舱", "车机"]),
            ("上游材料", ["锂", "镍", "上游材料"]),
            ("出口与海外市场", ["出口", "海外", "欧洲", "东南亚"]),
        ],
        "人工智能": [
            ("基础模型", ["基础模型", "大模型", "模型"]),
            ("AI 芯片", ["AI 芯片", "GPU", "英伟达", "NVIDIA"]),
            ("算力基础设施", ["算力", "数据中心", "集群"]),
            ("AI 应用", ["AI 应用", "应用"]),
            ("智能体/Agent", ["智能体", "Agent"]),
            ("数据与安全", ["数据安全", "安全", "治理"]),
            ("机器人", ["机器人", "人形机器人", "具身智能", "物理AI", "物理 AI"]),
            ("企业服务", ["企业服务", "SaaS"]),
        ],
    }
    matched = [label for label, keywords in rules[industry] if any(keyword in text for keyword in keywords)]
    return matched or [INDUSTRY_SUBTAGS[industry][0]]


def _detect_event_types(text: str, source_type: str) -> list[str]:
    matched: list[str] = []
    if source_type == "government" or any(word in text for word in ["政策", "监管", "办法", "通知", "指南"]):
        matched.append("政策/监管")
    if any(word in text for word in ["融资", "投资", "募资"]):
        matched.append("融资/投资")
    if any(word in text for word in ["并购", "收购", "重组"]):
        matched.append("并购/重组")
    if any(word in text for word in ["发布", "推出", "上线"]):
        matched.append("产品发布")
    if any(word in text for word in ["突破", "刷新", "首个", "性能提升"]):
        matched.append("技术突破")
    if any(word in text for word in ["合作", "签署", "联盟"]):
        matched.append("战略合作")
    if any(word in text for word in ["产能", "交付", "量产"]):
        matched.append("产能/交付")
    if any(word in text for word in ["价格", "降价", "商业化"]):
        matched.append("价格/商业化")
    if any(actor in text for actor in KEY_ACTORS):
        matched.append("龙头企业动向")
    if any(word in text for word in ["供应链", "短缺", "断供"]):
        matched.append("供应链变化")
    if any(word in text for word in ["海外", "出口", "欧洲", "美国"]):
        matched.append("海外扩张")
    if any(word in text for word in ["风险", "处罚", "事故", "召回", "负面"]):
        matched.append("风险/负面舆情")
    return list(dict.fromkeys(matched or ["龙头企业动向"]))


def _detect_subject_roles(text: str, source_type: str) -> list[str]:
    roles: list[str] = []
    if source_type == "government" or any(word in text for word in ["工信部", "发改委", "监管", "政府"]):
        roles.append("政府/监管机构")
    if any(word in text for word in ["比亚迪", "特斯拉", "蔚来", "小鹏", "OpenAI", "Anthropic", "模型"]):
        roles.append("整车厂/模型厂商")
    if any(word in text for word in ["芯片", "英伟达", "NVIDIA"]):
        roles.append("核心零部件/芯片企业")
    if any(word in text for word in ["电池", "宁德时代", "算力", "数据中心"]):
        roles.append("电池/算力基础设施企业")
    if any(word in text for word in ["大学", "高校", "研究院"]):
        roles.append("高校/科研机构")
    if any(word in text for word in ["投资", "基金", "资本"]):
        roles.append("投资机构")
    if any(word in text for word in ["海外", "美国", "欧洲", "Google", "Microsoft"]):
        roles.append("海外企业")
    if any(word in text for word in ["客户", "应用方", "采购"]):
        roles.append("下游客户/应用方")
    return list(dict.fromkeys(roles or ["整车厂/模型厂商"]))


def _detect_signal_attributes(event_types: list[str], text: str) -> list[str]:
    signals: list[str] = []
    if "政策/监管" in event_types:
        signals.append("政策信号")
    if "风险/负面舆情" in event_types:
        signals.append("风险信号")
    if any(item in event_types for item in ["融资/投资", "海外扩张", "战略合作", "产能/交付"]):
        signals.append("机会信号")
    if any(item in event_types for item in ["龙头企业动向", "供应链变化"]):
        signals.append("竞争格局变化")
    if "技术突破" in event_types:
        signals.append("技术路线变化")
    if any(item in event_types for item in ["价格/商业化", "产品发布"]):
        signals.append("商业化进展")
    if any(word in text for word in ["地方", "区域", "园区"]):
        signals.append("区域产业变化")
    if any(item in event_types for item in ["融资/投资", "并购/重组"]):
        signals.append("资本市场信号")
    return list(dict.fromkeys(signals or ["机会信号"]))


def _detect_importance(event_types: list[str], source_type: str, key_actor_level: str, text: str) -> tuple[str, str, float]:
    if source_type in {"government", "exchange"} or "政策/监管" in event_types or "风险/负面舆情" in event_types:
        return "high", "命中政府/监管、交易所或风险类高置信信号。", 0.82
    if key_actor_level == "core" or any(item in event_types for item in ["技术突破", "供应链变化", "并购/重组"]):
        return "high", "涉及核心主体或可能改变产业格局的事件。", 0.78
    if any(word in text for word in ["具体指标", "商业化", "产能", "融资"]):
        return "medium", "包含明确进展，但影响范围仍需后续验证。", 0.68
    return "low", "产业影响较弱或事实支撑不足。", 0.55


def _extract_entities(text: str) -> tuple[list[dict[str, Any]], str]:
    entities: list[dict[str, Any]] = []
    actor_level = "none"
    for actor, level in KEY_ACTORS.items():
        index = text.find(actor)
        if index >= 0:
            actor_type = "agency" if actor in {"工信部", "国家发改委", "国家能源局", "国家市场监督管理总局"} else "company"
            entities.append({"text": actor, "type": actor_type, "start": index, "end": index + len(actor), "confidence": 0.9})
            if level == "core":
                actor_level = "core"
    return entities, actor_level


def _build_evidence(event_types: list[str], source_type: str, key_actor_level: str) -> list[str]:
    evidence = [f"事件类型命中：{', '.join(event_types)}"]
    if source_type:
        evidence.append(f"来源类型：{source_type}")
    if key_actor_level != "none":
        evidence.append(f"关键主体等级：{key_actor_level}")
    return evidence


def _validate_result(
    industry: str,
    subtags: list[str],
    event_types: list[str],
    importance_level: str,
    subject_roles: list[str],
    signal_attributes: list[str],
) -> None:
    if industry not in INDUSTRIES:
        raise ClassificationError(f"invalid industry: {industry}")
    invalid_subtags = set(subtags) - set(INDUSTRY_SUBTAGS[industry])
    invalid_events = set(event_types) - EVENT_TYPES
    invalid_roles = set(subject_roles) - SUBJECT_ROLES
    invalid_signals = set(signal_attributes) - SIGNAL_ATTRIBUTES
    if importance_level not in IMPORTANCE_LEVELS:
        raise ClassificationError(f"invalid importance level: {importance_level}")
    if invalid_subtags:
        raise ClassificationError(f"invalid industry subtags: {sorted(invalid_subtags)}")
    if invalid_events:
        raise ClassificationError(f"invalid event types: {sorted(invalid_events)}")
    if invalid_roles:
        raise ClassificationError(f"invalid subject roles: {sorted(invalid_roles)}")
    if invalid_signals:
        raise ClassificationError(f"invalid signal attributes: {sorted(invalid_signals)}")
