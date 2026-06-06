from __future__ import annotations

INDUSTRY_ORDER = ["新能源汽车", "人工智能"]
INDUSTRIES = set(INDUSTRY_ORDER)

INDUSTRY_HINTS = {
    "new_energy_vehicle": "新能源汽车",
    "nev": "新能源汽车",
    "ai": "人工智能",
    "artificial_intelligence": "人工智能",
}

INDUSTRY_SUBTAGS = {
    "新能源汽车": [
        "整车制造",
        "动力电池",
        "智能驾驶",
        "充换电基础设施",
        "汽车芯片",
        "车载软件",
        "上游材料",
        "出口与海外市场",
    ],
    "人工智能": [
        "基础模型",
        "AI 芯片",
        "算力基础设施",
        "AI 应用",
        "智能体/Agent",
        "数据与安全",
        "机器人",
        "企业服务",
    ],
}

EVENT_TYPE_ORDER = [
    "政策/监管",
    "融资/投资",
    "并购/重组",
    "产品发布",
    "技术突破",
    "战略合作",
    "产能/交付",
    "价格/商业化",
    "龙头企业动向",
    "供应链变化",
    "海外扩张",
    "风险/负面舆情",
]
EVENT_TYPES = set(EVENT_TYPE_ORDER)

SUBJECT_ROLE_ORDER = [
    "整车厂/模型厂商",
    "核心零部件/芯片企业",
    "电池/算力基础设施企业",
    "政府/监管机构",
    "高校/科研机构",
    "投资机构",
    "海外企业",
    "下游客户/应用方",
]
SUBJECT_ROLES = set(SUBJECT_ROLE_ORDER)

SIGNAL_ATTRIBUTE_ORDER = [
    "机会信号",
    "风险信号",
    "政策信号",
    "竞争格局变化",
    "技术路线变化",
    "商业化进展",
    "区域产业变化",
    "资本市场信号",
]
SIGNAL_ATTRIBUTES = set(SIGNAL_ATTRIBUTE_ORDER)

IMPORTANCE_LEVEL_ORDER = ["high", "medium", "low"]
IMPORTANCE_LEVELS = set(IMPORTANCE_LEVEL_ORDER)

IMPORTANCE_LEVEL_SCORES = {
    "high": 1.0,
    "medium": 0.6,
    "low": 0.25,
}

EVENT_TYPE_SCORES = {
    "政策/监管": 1.0,
    "风险/负面舆情": 0.95,
    "龙头企业动向": 0.9,
    "技术突破": 0.85,
    "供应链变化": 0.85,
    "并购/重组": 0.8,
    "融资/投资": 0.75,
    "产能/交付": 0.75,
    "战略合作": 0.65,
    "海外扩张": 0.65,
    "价格/商业化": 0.6,
    "产品发布": 0.5,
}

KEY_ACTORS = {
    "比亚迪": "core",
    "宁德时代": "core",
    "CATL": "core",
    "特斯拉": "core",
    "英伟达": "core",
    "NVIDIA": "core",
    "OpenAI": "core",
    "Anthropic": "core",
    "Google": "core",
    "Microsoft": "core",
    "工信部": "core",
    "国家发改委": "core",
    "国家能源局": "core",
    "国家市场监督管理总局": "core",
}

KEY_ACTOR_SCORES = {
    "core": 1.0,
    "important": 0.75,
    "normal": 0.45,
    "none": 0.0,
}

SOURCE_TYPE_LABELS = {
    "government": "政府/监管",
    "exchange": "交易所/公告",
    "company": "企业官网",
    "media": "行业媒体",
    "news_api": "新闻 API",
}
