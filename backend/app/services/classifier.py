from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.logging_config import APP_LOGGER_NAME
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
    SOURCE_TYPE_LABELS,
    SUBJECT_ROLE_ORDER,
    SUBJECT_ROLES,
)


logger = logging.getLogger(APP_LOGGER_NAME)


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
            except ClassificationError as exc:
                logger.warning(
                    "classifier_llm_invalid_payload source_id=%s title=%r url=%s provider=%s model=%s error=%s payload_keys=%s",
                    item.get("source_id"),
                    item.get("title"),
                    item.get("url"),
                    getattr(llm_client.settings, "provider", "unknown"),
                    getattr(llm_client.settings, "model", "unknown"),
                    exc,
                    sorted(llm_payload.keys()) if isinstance(llm_payload, dict) else type(llm_payload).__name__,
                    exc_info=True,
                )
        elif llm_payload is not None:
            logger.warning(
                "classifier_llm_incomplete_payload source_id=%s title=%r url=%s provider=%s model=%s payload_keys=%s",
                item.get("source_id"),
                item.get("title"),
                item.get("url"),
                getattr(llm_client.settings, "provider", "unknown"),
                getattr(llm_client.settings, "model", "unknown"),
                sorted(llm_payload.keys()) if isinstance(llm_payload, dict) else type(llm_payload).__name__,
            )

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


def _classify_with_llm(
    item: dict[str, Any],
    industry: str,
    llm_client: LLMClient,
) -> dict[str, Any] | None:
    try:
        return llm_client.complete_json(
            system_prompt=(
                "你是产业舆情 Agent 的 classify_item 分类打标节点。"
                "任务是基于标题、正文、来源元数据和 taxonomy_definitions 选择结构化标签。"
                "必须只输出 JSON；所有标签只能从给定枚举中选择，不能新增、改写或翻译标签。"
                "每个分类判断必须有标题、正文或来源属性中的证据支撑；不要编造主体、事件、标签或时间。"
                "不确定时保守选择更少标签和较低置信度。不要输出最终排序总分。"
            ),
            user_payload={
                "title": item.get("title"),
                "content": item.get("raw_content") or item.get("content_excerpt"),
                "content_excerpt": item.get("content_excerpt"),
                "source_name": item.get("source_name"),
                "source_type": item.get("source_type"),
                "source_type_label": SOURCE_TYPE_LABELS.get(str(item.get("source_type") or ""), ""),
                "url": item.get("url"),
                "published_at": item.get("published_at"),
                "fetched_at": item.get("fetched_at"),
                "industry": industry,
                "allowed_industry_subtags": INDUSTRY_SUBTAGS[industry],
                "allowed_event_types": EVENT_TYPE_ORDER,
                "allowed_importance_levels": IMPORTANCE_LEVEL_ORDER,
                "allowed_subject_roles": SUBJECT_ROLE_ORDER,
                "allowed_signal_attributes": SIGNAL_ATTRIBUTE_ORDER,
                "allowed_key_actor_levels": list(_key_actor_level_definitions()),
                "taxonomy_definitions": _taxonomy_definitions(industry),
                "decision_rules": _classification_decision_rules(),
                "output_requirements": _classification_output_requirements(),
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
    except Exception as exc:
        logger.warning(
            "classifier_llm_failed source_id=%s title=%r url=%s provider=%s model=%s industry=%s error=%s",
            item.get("source_id"),
            item.get("title"),
            item.get("url"),
            getattr(llm_client.settings, "provider", "unknown"),
            getattr(llm_client.settings, "model", "unknown"),
            industry,
            exc,
            exc_info=True,
        )
        return None


def _taxonomy_definitions(industry: str) -> dict[str, Any]:
    return {
        "industry": {
            "selected": industry,
            "definition": "候选内容已经通过相关性节点进入该产业；分类节点只需在该产业内细分打标。",
        },
        "industry_subtags": {
            industry: {label: _industry_subtag_definitions(industry).get(label, "") for label in INDUSTRY_SUBTAGS[industry]}
        },
        "event_types": {label: _event_type_definitions().get(label, "") for label in EVENT_TYPE_ORDER},
        "importance_levels": _importance_level_definitions(),
        "subject_roles": {label: _subject_role_definitions().get(label, "") for label in SUBJECT_ROLE_ORDER},
        "signal_attributes": {label: _signal_attribute_definitions().get(label, "") for label in SIGNAL_ATTRIBUTE_ORDER},
        "key_actor_level": _key_actor_level_definitions(),
        "entities": {
            "text": "正文中实际出现的机构、公司、产品、人物或地点名称。",
            "type": "company、agency、institution、product、person、location、other 之一；不确定用 other。",
            "confidence": "0 到 1。只有正文中能定位或高度确定的实体才输出。",
        },
    }


def _classification_decision_rules() -> list[str]:
    return [
        "只依据 title、content、content_excerpt 和 source metadata 判断，不使用外部知识补全事实。",
        "industry_subtags 选择 1-3 个最具体细分；必须与正文里的产品、技术、环节或业务场景直接相关。",
        "event_types 选择 1-3 个主要事件类型；若只是背景提及，不要打成事件类型。",
        "subject_roles 选择实际参与事件的主体角色，而不是文章受众或泛泛提到的生态角色。",
        "signal_attributes 描述该事件对产业跟踪的信号含义；机会、风险、政策、竞争、技术、商业化、区域、资本只能在有证据时选择。",
        "importance_level 评估事件本身的产业影响，不输出排序总分；政策监管、重大风险、核心主体和格局变化通常更高。",
        "key_actor_level 根据正文实际出现的主体选择 core、important、normal、none；不要把未出现主体算入。",
        "entities 只输出正文或标题中实际出现的实体，不能编造别名、上下游公司或监管机构。",
        "confidence 表示分类可靠性：0.90 以上需事实清楚且标签直接命中；0.70-0.89 表示较可靠；0.50-0.69 表示证据有限；低于 0.50 表示不确定。",
        "当正文信息不足时，使用更少标签并降低 confidence，不要用猜测补齐字段。",
    ]


def _classification_output_requirements() -> dict[str, Any]:
    return {
        "format": "只返回单个 JSON object，不要 Markdown、解释文字或排序分数。",
        "required_fields": [
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
        "enum_only_fields": [
            "industry_subtags",
            "event_types",
            "importance_level",
            "subject_roles",
            "signal_attributes",
            "key_actor_level",
        ],
        "max_counts": {
            "industry_subtags": 3,
            "event_types": 3,
            "subject_roles": 3,
            "signal_attributes": 3,
            "entities": 8,
        },
        "evidence": (
            "importance_reason 必须用一句话说明来自标题、正文或来源类型的关键证据；"
            "不得用没有在输入中出现的事实作为依据。"
        ),
        "fallback_policy": "证据不足时保守选择较少标签，并降低 confidence。",
    }


def _industry_subtag_definitions(industry: str) -> dict[str, str]:
    definitions = {
        "新能源汽车": {
            "整车制造": "整车品牌、车型、销量、交付、制造和整车平台相关事件。",
            "动力电池": "电芯、电池包、储能电池、动力电池材料体系和电池企业相关事件。",
            "智能驾驶": "自动驾驶、辅助驾驶、车端感知、智驾算法和驾驶系统相关事件。",
            "充换电基础设施": "充电桩、换电站、补能网络、运营平台和基础设施政策相关事件。",
            "汽车芯片": "车规芯片、座舱芯片、智驾芯片、功率半导体和供应相关事件。",
            "车载软件": "座舱系统、车机、OTA、车载操作系统和软件服务相关事件。",
            "上游材料": "锂、镍、钴、正负极材料、电解液、隔膜等上游材料相关事件。",
            "出口与海外市场": "出口、海外建厂、海外销售、海外监管和国际市场拓展相关事件。",
        },
        "人工智能": {
            "基础模型": "大模型、基础模型、多模态模型、模型训练、模型服务和 API 能力相关事件。",
            "AI 芯片": "GPU、AI 加速器、训练/推理芯片、芯片供应和算力硬件相关事件。",
            "算力基础设施": "数据中心、算力集群、云算力、训练基础设施和算力调度相关事件。",
            "AI 应用": "面向具体行业或用户场景的 AI 产品、应用落地和解决方案相关事件。",
            "智能体/Agent": "Agent、自动化工作流、工具调用、多智能体协作和智能体平台相关事件。",
            "数据与安全": "数据治理、隐私、安全、内容安全、合规和模型安全相关事件。",
            "机器人": "机器人、具身智能、人形机器人和物理 AI 相关事件。",
            "企业服务": "企业级 SaaS、B 端平台、企业客户服务、API 商业化和工作流集成相关事件。",
        },
    }
    return definitions.get(industry, {})


def _event_type_definitions() -> dict[str, str]:
    return {
        "政策/监管": "政府、监管机构、行业主管部门发布政策、规则、通知、处罚、监管口径或合规要求。",
        "融资/投资": "融资、投资、基金、股权投资、战略投资或资本注入事件。",
        "并购/重组": "收购、合并、资产重组、控制权变更或业务整合事件。",
        "产品发布": "新产品、新服务、新版本、新平台上线或发布。",
        "技术突破": "明确技术创新、性能突破、研发成果、专利或关键能力升级。",
        "战略合作": "签约、合作、联盟、联合研发、生态伙伴或客户合作。",
        "产能/交付": "产能扩张、量产、交付、订单履约、工厂投产或产线变化。",
        "价格/商业化": "价格调整、商业模式、收费、订阅、商业化进展或收入化。",
        "龙头企业动向": "核心企业、头部公司、关键监管机构或行业风向标主体的重要动作。",
        "供应链变化": "供应短缺、断供、供应商切换、上游材料和核心部件供需变化。",
        "海外扩张": "出口、海外市场、海外建厂、国际合作或跨境监管事件。",
        "风险/负面舆情": "事故、召回、处罚、诉讼、安全问题、经营风险或明显负面舆情。",
    }


def _importance_level_definitions() -> dict[str, str]:
    return {
        "high": "对产业格局、监管环境、核心主体、供应链、风险暴露或技术路线可能产生显著影响。",
        "medium": "有明确事实和跟踪价值，但影响范围、持续性或产业传导仍需后续验证。",
        "low": "事实较弱、影响较局部、重复性较高，或只有一般资讯价值。",
    }


def _subject_role_definitions() -> dict[str, str]:
    return {
        "整车厂/模型厂商": "新能源汽车整车企业，或 AI 基础模型、模型服务、平台型模型厂商。",
        "核心零部件/芯片企业": "汽车核心零部件、车规芯片、AI 芯片、GPU 和关键硬件供应商。",
        "电池/算力基础设施企业": "动力电池、储能、电池材料、数据中心、云算力和算力基础设施企业。",
        "政府/监管机构": "政府部门、监管机构、行业主管部门和公共政策制定或执法主体。",
        "高校/科研机构": "大学、实验室、研究院、科研团队和产学研机构。",
        "投资机构": "基金、VC/PE、产业资本、金融机构和投资主体。",
        "海外企业": "境外企业、跨国公司或主要事件发生在海外市场的企业主体。",
        "下游客户/应用方": "采购方、行业客户、场景方、渠道方和实际应用落地主体。",
    }


def _signal_attribute_definitions() -> dict[str, str]:
    return {
        "机会信号": "可能带来市场扩张、需求增长、产业链机会或新业务窗口。",
        "风险信号": "可能带来合规、安全、经营、供应或声誉风险。",
        "政策信号": "反映政策导向、监管尺度、补贴规则、准入标准或政府重点。",
        "竞争格局变化": "可能改变头部企业地位、市场份额、竞争边界或上下游议价关系。",
        "技术路线变化": "体现技术范式、架构、材料、算法、路线或关键性能方向变化。",
        "商业化进展": "体现产品落地、付费、客户转化、收入化、规模部署或商业模式成熟。",
        "区域产业变化": "体现地方产业集群、区域政策、园区、海外或地区市场变化。",
        "资本市场信号": "体现融资、并购、估值、上市、股价敏感信息或资本偏好变化。",
    }


def _key_actor_level_definitions() -> dict[str, str]:
    return {
        "core": "taxonomy.KEY_ACTORS 中的核心企业或监管主体，或文本中明确具有行业风向标地位的主体。",
        "important": "非核心名单内，但对该细分产业有明显影响力的主体。",
        "normal": "普通参与方、客户、供应商或区域性企业。",
        "none": "未出现可识别关键主体，或主体与产业事件关系不明确。",
    }


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
