from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


SOURCE_CONFIGS: list[dict[str, Any]] = [
    {"id": 1, "name": "工业和信息化部", "type": "government", "industry_hint": "new_energy_vehicle,ai", "url": "https://www.miit.gov.cn/xwfb/gxdt/index.html", "reliability_score": 1.0, "fetch_interval_minutes": 60},
    {"id": 2, "name": "国家发展改革委", "type": "government", "industry_hint": "new_energy_vehicle,ai", "url": "https://www.ndrc.gov.cn/xwdt/", "reliability_score": 1.0, "fetch_interval_minutes": 60},
    {"id": 3, "name": "国家能源局", "type": "government", "industry_hint": "new_energy_vehicle", "url": "https://www.nea.gov.cn/", "reliability_score": 1.0, "fetch_interval_minutes": 60},
    {"id": 4, "name": "国家市场监督管理总局", "type": "government", "industry_hint": "new_energy_vehicle,ai", "url": "https://www.samr.gov.cn/", "reliability_score": 1.0, "fetch_interval_minutes": 60},
    {"id": 5, "name": "科学技术部", "type": "government", "industry_hint": "ai", "url": "https://www.most.gov.cn/kjbgz/", "reliability_score": 1.0, "fetch_interval_minutes": 60},
    {"id": 6, "name": "商务部", "type": "government", "industry_hint": "new_energy_vehicle", "url": "https://www.mofcom.gov.cn/", "reliability_score": 1.0, "fetch_interval_minutes": 60},
    {"id": 7, "name": "中国政府网政策文件库", "type": "government", "industry_hint": "new_energy_vehicle,ai", "url": "https://sousuo.www.gov.cn/zcwjk/", "reliability_score": 1.0, "fetch_interval_minutes": 60},
    {"id": 8, "name": "上海证券交易所公告", "type": "exchange", "industry_hint": "new_energy_vehicle,ai", "url": "https://www.sse.com.cn/disclosure/listedinfo/announcement/", "reliability_score": 0.95, "fetch_interval_minutes": 60},
    {"id": 9, "name": "深圳证券交易所公告", "type": "exchange", "industry_hint": "new_energy_vehicle,ai", "url": "https://www.szse.cn/disclosure/listed/notice/index.html", "reliability_score": 0.95, "fetch_interval_minutes": 60},
    {"id": 10, "name": "香港交易所披露易", "type": "exchange", "industry_hint": "new_energy_vehicle,ai", "url": "https://www.hkexnews.hk/index_c.htm", "reliability_score": 0.95, "fetch_interval_minutes": 60},
    {"id": 11, "name": "比亚迪新闻中心", "type": "company", "industry_hint": "new_energy_vehicle", "url": "https://www.bydglobal.com/en/News.html", "reliability_score": 0.85, "fetch_interval_minutes": 60},
    {"id": 12, "name": "宁德时代新闻中心", "type": "company", "industry_hint": "new_energy_vehicle", "url": "https://www.catl.com/en/news/", "reliability_score": 0.85, "fetch_interval_minutes": 60},
    {"id": 13, "name": "特斯拉官方新闻", "type": "company", "industry_hint": "new_energy_vehicle", "url": "https://www.tesla.com/blog", "reliability_score": 0.85, "fetch_interval_minutes": 60},
    {"id": 14, "name": "蔚来新闻中心", "type": "company", "industry_hint": "new_energy_vehicle", "url": "https://www.nio.com/news", "reliability_score": 0.85, "fetch_interval_minutes": 60},
    {"id": 15, "name": "小鹏汽车新闻中心", "type": "company", "industry_hint": "new_energy_vehicle", "url": "https://www.xpeng.com/newsroom", "reliability_score": 0.85, "fetch_interval_minutes": 60},
    {"id": 16, "name": "英伟达新闻中心", "type": "company", "industry_hint": "ai", "url": "https://nvidianews.nvidia.com/news", "reliability_score": 0.85, "fetch_interval_minutes": 60},
    {"id": 17, "name": "OpenAI News", "type": "company", "industry_hint": "ai", "url": "https://openai.com/news/", "reliability_score": 0.85, "fetch_interval_minutes": 60},
    {"id": 18, "name": "Anthropic News", "type": "company", "industry_hint": "ai", "url": "https://www.anthropic.com/news", "reliability_score": 0.85, "fetch_interval_minutes": 60},
    {"id": 19, "name": "Google AI Blog", "type": "company", "industry_hint": "ai", "url": "https://blog.google/technology/google-deepmind/", "reliability_score": 0.85, "fetch_interval_minutes": 60},
    {"id": 20, "name": "Microsoft AI Blog", "type": "company", "industry_hint": "ai", "url": "https://www.microsoft.com/en-us/ai/blog", "reliability_score": 0.85, "fetch_interval_minutes": 60},
    {"id": 21, "name": "机器之心", "type": "media", "industry_hint": "ai", "url": "https://www.jiqizhixin.com/", "reliability_score": 0.75, "fetch_interval_minutes": 120},
    {"id": 22, "name": "量子位", "type": "media", "industry_hint": "ai", "url": "https://www.qbitai.com/", "reliability_score": 0.75, "fetch_interval_minutes": 120},
    {"id": 23, "name": "盖世汽车", "type": "media", "industry_hint": "new_energy_vehicle", "url": "https://auto.gasgoo.com/", "reliability_score": 0.75, "fetch_interval_minutes": 120},
    {"id": 24, "name": "第一电动网", "type": "media", "industry_hint": "new_energy_vehicle", "url": "https://www.d1ev.com/news", "reliability_score": 0.75, "fetch_interval_minutes": 120},
]

LEGACY_URL_REPLACEMENTS = {
    "https://www.miit.gov.cn/xwfb/gxdt/index.html#industrial-agent-demo-policy": "https://www.miit.gov.cn/xwfb/gxdt/index.html",
    "https://www.gasgoo.com/#industrial-agent-demo-policy": "https://auto.gasgoo.com/",
    "https://www.catl.com/news/#industrial-agent-demo-capacity": "https://www.catl.com/en/news/6815.html",
    "https://nvidianews.nvidia.com/news#industrial-agent-demo-ai-chip": "https://nvidianews.nvidia.com/news",
    "https://openai.com/news/#industrial-agent-demo-agent": "https://openai.com/news/",
    "https://www.szse.cn/disclosure/listed/notice/index.html#industrial-agent-demo-investment": "https://www.szse.cn/disclosure/listed/notice/index.html",
    "https://www.gov.cn/zhengce/#industrial-agent-demo-region": "https://sousuo.www.gov.cn/zcwjk/",
    "https://www.bydglobal.com/en/news/list#industrial-agent-demo-oversea": "https://www.bydglobal.com/en/News.html",
    "https://www.anthropic.com/news#industrial-agent-demo-safety": "https://www.anthropic.com/news",
}


def demo_raw_items(now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    return [
        {
            "source_id": 1,
            "url": "https://www.miit.gov.cn/xwfb/gxdt/index.html",
            "title": "工信部发布新能源汽车动力电池安全追溯政策",
            "raw_content": "工信部发布政策通知，要求动力电池企业完善安全追溯体系，强化生产、回收和梯次利用环节的数据管理。该政策覆盖整车企业、电池企业和回收企业，强调监管闭环与产业链协同。",
            "published_at": (now - timedelta(hours=3)).isoformat(),
        },
        {
            "source_id": 23,
            "url": "https://auto.gasgoo.com/",
            "title": "多地新能源汽车电池追溯政策升温，产业链监管趋严",
            "raw_content": "行业媒体报道，近期动力电池追溯和回收监管持续升温，整车厂与电池企业需要加强数据对接。报道援引公开政策信息，认为该变化将影响动力电池回收和上游材料企业。",
            "published_at": (now - timedelta(hours=2, minutes=40)).isoformat(),
        },
        {
            "source_id": 12,
            "url": "https://www.catl.com/en/news/6815.html",
            "title": "宁德时代披露新一代动力电池产能与交付进展",
            "raw_content": "宁德时代新闻中心披露，新一代动力电池产线进入交付爬坡阶段，面向整车客户的规模化供应能力提升。信息涉及动力电池、产能交付和核心企业动向。",
            "published_at": (now - timedelta(hours=5)).isoformat(),
        },
        {
            "source_id": 16,
            "url": "https://nvidianews.nvidia.com/news",
            "title": "英伟达发布面向大模型训练的新一代 AI 芯片平台",
            "raw_content": "英伟达发布新一代 AI 芯片平台，面向基础模型训练和推理场景提升算力密度。该平台将影响算力基础设施、AI 芯片供应和大型模型厂商采购节奏。",
            "published_at": (now - timedelta(hours=6)).isoformat(),
        },
        {
            "source_id": 17,
            "url": "https://openai.com/news/",
            "title": "OpenAI 更新企业级智能体产品能力",
            "raw_content": "OpenAI News 披露企业级智能体产品能力更新，强调工具调用、数据安全和企业服务集成。该事件涉及基础模型、智能体/Agent、AI 应用与商业化进展。",
            "published_at": (now - timedelta(hours=10)).isoformat(),
        },
        {
            "source_id": 9,
            "url": "https://www.szse.cn/disclosure/listed/notice/index.html",
            "title": "上市公司公告拟投资建设智能驾驶零部件项目",
            "raw_content": "深交所公告显示，某上市公司拟投资建设智能驾驶核心零部件项目，项目覆盖车载软件、汽车芯片和智能驾驶供应链。公告披露投资规模、建设周期和风险提示。",
            "published_at": (now - timedelta(hours=18)).isoformat(),
        },
        {
            "source_id": 7,
            "url": "https://sousuo.www.gov.cn/zcwjk/",
            "title": "地方出台人工智能产业园区支持政策",
            "raw_content": "政策栏目显示，地方政府出台人工智能产业园区支持政策，围绕算力基础设施、数据安全和企业服务提供支持措施。政策强调区域产业变化和人工智能创新生态建设。",
            "published_at": (now - timedelta(hours=26)).isoformat(),
        },
        {
            "source_id": 11,
            "url": "https://www.bydglobal.com/en/News.html",
            "title": "比亚迪海外市场交付增长并扩大充电合作",
            "raw_content": "比亚迪新闻中心披露海外市场交付增长，并与当地合作伙伴推进充电基础设施建设。事件涉及整车制造、出口与海外市场、充换电基础设施和龙头企业动向。",
            "published_at": (now - timedelta(hours=32)).isoformat(),
        },
        {
            "source_id": 18,
            "url": "https://www.anthropic.com/news",
            "title": "Anthropic 发布模型安全与企业数据治理进展",
            "raw_content": "Anthropic News 发布模型安全和企业数据治理进展，涉及基础模型、数据与安全和企业服务。该信息对企业采用 AI 应用的安全评估有参考价值。",
            "published_at": (now - timedelta(hours=50)).isoformat(),
        },
    ]
