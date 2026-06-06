from __future__ import annotations

import os
import math
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

# Unit tests must not accidentally consume real LLM credentials from backend/.env.
os.environ.setdefault("LLM_API_KEY", "")
os.environ.setdefault("LLM_PROVIDER", "openai")
os.environ.setdefault("LLM_MODEL", "gpt-4.1-mini")
os.environ.setdefault("LLM_BASE_URL", "")

from app.api import create_router
from app.database import Database, from_json
from app.seed_data import demo_raw_items
from app.scheduler import start_scheduler
from app.services.classifier import ClassificationError, classify_item
from app.services.collector import (
    FetchedDocument,
    _decode_bytes,
    clean_article_text,
    extract_source_items,
    fetch_source_items,
    normalize_raw_item,
)
from app.services.dedup import choose_representative_item
from app.services.ranking import RankInput, calculate_rank_score
from app.services.taxonomy import EVENT_TYPES, INDUSTRIES, INDUSTRY_SUBTAGS, SIGNAL_ATTRIBUTES
from app.services import workflow


class ClassificationContractTest(unittest.TestCase):
    def test_classification_uses_only_configured_taxonomy_values(self) -> None:
        result = classify_item(
            {
                "title": "工信部发布新能源汽车动力电池监管政策",
                "raw_content": "政策要求动力电池企业完善安全追溯体系。",
                "source_type": "government",
                "industry_hint": "new_energy_vehicle",
            }
        )

        self.assertIn(result.industry, INDUSTRIES)
        self.assertTrue(set(result.event_types).issubset(EVENT_TYPES))
        self.assertTrue(set(result.signal_attributes).issubset(SIGNAL_ATTRIBUTES))
        self.assertEqual(result.importance_level, "high")

    def test_classification_uses_configured_llm_structured_labels_when_available(self) -> None:
        class FakeSettings:
            provider = "openai"
            model = "test-classifier-model"

        class FakeLLMClient:
            settings = FakeSettings()
            is_configured = True

            def complete_json(self, system_prompt: str, user_payload: dict[str, object]) -> dict[str, object]:
                self.system_prompt = system_prompt
                self.user_payload = user_payload
                return {
                    "industry_subtags": ["基础模型"],
                    "event_types": ["产品发布"],
                    "importance_level": "medium",
                    "importance_reason": "模型服务发布，需观察商业化进展。",
                    "subject_roles": ["整车厂/模型厂商"],
                    "signal_attributes": ["商业化进展"],
                    "confidence": 0.88,
                    "entities": [{"text": "OpenAI", "type": "company", "confidence": 0.9}],
                    "key_actor_level": "core",
                }

        fake_client = FakeLLMClient()
        result = classify_item(
            {
                "title": "OpenAI 发布新一代企业级模型服务",
                "raw_content": "OpenAI 面向企业客户发布新一代模型服务，强化 API、智能体和数据安全能力。",
                "source_name": "OpenAI Blog",
                "source_type": "company",
                "published_at": "2026-06-05T09:00:00+00:00",
                "fetched_at": "2026-06-05T09:05:00+00:00",
                "relevance_industry": "人工智能",
            },
            llm_client=fake_client,
        )

        self.assertEqual(result.industry, "人工智能")
        self.assertEqual(result.industry_subtags, ["基础模型"])
        self.assertEqual(result.event_types, ["产品发布"])
        self.assertEqual(result.importance_level, "medium")
        self.assertEqual(result.llm_provider, "openai")
        self.assertEqual(result.llm_model, "test-classifier-model")
        self.assertIn("taxonomy_definitions", fake_client.user_payload)
        self.assertIn("decision_rules", fake_client.user_payload)
        self.assertIn("output_requirements", fake_client.user_payload)
        self.assertNotIn("rule_baseline", fake_client.user_payload)
        self.assertEqual(fake_client.user_payload["source_name"], "OpenAI Blog")
        self.assertEqual(fake_client.user_payload["source_type"], "company")
        self.assertEqual(fake_client.user_payload["published_at"], "2026-06-05T09:00:00+00:00")
        self.assertEqual(fake_client.user_payload["fetched_at"], "2026-06-05T09:05:00+00:00")
        self.assertIn("基础模型", fake_client.user_payload["taxonomy_definitions"]["industry_subtags"]["人工智能"])
        self.assertIn("产品发布", fake_client.user_payload["taxonomy_definitions"]["event_types"])
        self.assertIn("evidence", fake_client.user_payload["output_requirements"])
        self.assertIn("key_actor_level", fake_client.user_payload["taxonomy_definitions"])
        prompt_contract = str(fake_client.system_prompt) + " " + " ".join(fake_client.user_payload["decision_rules"])
        self.assertNotIn("rule_baseline", prompt_contract)
        self.assertNotIn("规则基线", prompt_contract)
        self.assertNotIn("本地规则", prompt_contract)
        self.assertIn("证据", fake_client.system_prompt)
        self.assertIn("不要编造", fake_client.system_prompt)
        self.assertIn("排序总分", fake_client.system_prompt)

    def test_classification_falls_back_when_llm_returns_out_of_scope_labels(self) -> None:
        class FakeSettings:
            provider = "openai"
            model = "bad-label-model"

        class FakeLLMClient:
            settings = FakeSettings()
            is_configured = True

            def complete_json(self, system_prompt: str, user_payload: dict[str, object]) -> dict[str, object]:
                return {
                    "industry_subtags": ["随便造的细分"],
                    "event_types": ["随便造的事件"],
                    "importance_level": "超高",
                    "subject_roles": ["随便造的主体"],
                    "signal_attributes": ["随便造的信号"],
                    "confidence": 0.99,
                }

        result = classify_item(
            {
                "title": "OpenAI 发布新一代企业级模型服务",
                "raw_content": "OpenAI 发布新一代模型服务，Agent 能力升级并面向企业服务场景。",
                "source_type": "company",
                "relevance_industry": "人工智能",
            },
            llm_client=FakeLLMClient(),
        )

        self.assertTrue(set(result.industry_subtags).issubset(set(INDUSTRY_SUBTAGS[result.industry])))
        self.assertTrue(set(result.event_types).issubset(EVENT_TYPES))
        self.assertTrue(set(result.signal_attributes).issubset(SIGNAL_ATTRIBUTES))
        self.assertEqual(result.llm_provider, "rules+structured-output")
        self.assertEqual(result.llm_model, "local-rules")

    def test_classification_falls_back_when_llm_returns_partial_payload(self) -> None:
        class FakeSettings:
            provider = "openai"
            model = "partial-label-model"

        class FakeLLMClient:
            settings = FakeSettings()
            is_configured = True

            def complete_json(self, system_prompt: str, user_payload: dict[str, object]) -> dict[str, object]:
                return {"event_types": ["产品发布"]}

        result = classify_item(
            {
                "title": "OpenAI 发布新一代企业级模型服务",
                "raw_content": "OpenAI 发布新一代模型服务，Agent 能力升级并面向企业服务场景。",
                "source_type": "company",
                "relevance_industry": "人工智能",
            },
            llm_client=FakeLLMClient(),
        )

        self.assertEqual(result.llm_provider, "rules+structured-output")
        self.assertEqual(result.llm_model, "local-rules")

    def test_invalid_llm_label_is_rejected_instead_of_silently_saved(self) -> None:
        with self.assertRaises(ClassificationError):
            classify_item(
                {
                    "title": "某公司发布营销软文",
                    "raw_content": "无明确产业事实。",
                    "source_type": "media",
                    "industry_hint": "ai",
                },
                llm_override={"event_types": ["随便造的标签"]},
            )


class RankingFormulaTest(unittest.TestCase):
    def test_rank_score_uses_explicit_weighted_formula(self) -> None:
        now = datetime(2026, 6, 3, 9, 0, tzinfo=timezone.utc)
        score = calculate_rank_score(
            RankInput(
                importance_level="high",
                confidence=0.8,
                source_reliability=1.0,
                event_types=["政策/监管"],
                published_at=(now - timedelta(hours=4)).isoformat(),
                fetched_at=now.isoformat(),
                reliable_source_count=2,
                key_actor_level="core",
            ),
            now=now,
        )

        importance_score = 1.0 * (0.7 + 0.3 * 0.8)
        coverage_score = min(1.0, math.log2(1 + 2) / math.log2(6))
        expected = (
            importance_score * 0.35
            + 1.0 * 0.20
            + 1.0 * 0.15
            + 1.0 * 0.15
            + coverage_score * 0.10
            + 1.0 * 0.05
        )

        self.assertAlmostEqual(score.weighted_total, expected, places=4)
        self.assertEqual(score.breakdown["importance_score"], round(importance_score, 4))
        self.assertIn("政策/监管", " ".join(score.reasons))


class RelevanceScreenContractTest(unittest.TestCase):
    def test_relevance_llm_receives_professional_screening_contract(self) -> None:
        from app.services.relevance import screen_relevance

        class FakeSettings:
            provider = "openai"
            model = "test-relevance-model"

        class FakeLLMClient:
            settings = FakeSettings()
            is_configured = True

            def complete_json(self, system_prompt: str, user_payload: dict[str, object]) -> dict[str, object]:
                self.system_prompt = system_prompt
                self.user_payload = user_payload
                return {
                    "is_relevant": True,
                    "industry": "人工智能",
                    "reason": "正文明确描述企业级模型服务发布，属于人工智能产业情报。",
                    "confidence": 0.88,
                    "matched_terms": ["模型服务", "人工智能"],
                }

        fake_client = FakeLLMClient()
        result = screen_relevance(
            {
                "title": "OpenAI 发布新一代企业级模型服务",
                "raw_content": "OpenAI 面向企业客户发布新一代模型服务，强化 API、智能体和数据安全能力。",
                "content_excerpt": "OpenAI 发布企业级模型服务。",
                "source_name": "OpenAI Blog",
                "source_type": "company",
                "url": "https://example.com/openai/model-service",
                "published_at": "2026-06-05T09:00:00+00:00",
                "fetched_at": "2026-06-05T09:05:00+00:00",
                "industry_hint": "ai",
            },
            llm_client=fake_client,
        )

        self.assertTrue(result.is_relevant)
        self.assertEqual(result.provider, "openai")
        self.assertIn("industry_definitions", fake_client.user_payload)
        self.assertIn("exclusion_rules", fake_client.user_payload)
        self.assertIn("decision_rules", fake_client.user_payload)
        self.assertIn("confidence_calibration", fake_client.user_payload)
        self.assertIn("output_requirements", fake_client.user_payload)
        self.assertEqual(fake_client.user_payload["source_name"], "OpenAI Blog")
        self.assertEqual(fake_client.user_payload["published_at"], "2026-06-05T09:00:00+00:00")
        self.assertIn("人工智能", fake_client.user_payload["industry_definitions"])
        self.assertIn("内部会议", " ".join(fake_client.user_payload["exclusion_rules"]))
        self.assertIn("industry_hint", " ".join(fake_client.user_payload["decision_rules"]))
        self.assertNotIn("local_rule_result", fake_client.user_payload)
        self.assertIn("证据", fake_client.system_prompt)
        self.assertIn("不要编造", fake_client.system_prompt)
        self.assertIn("只判断是否进入", fake_client.system_prompt)

    def test_rejects_fake_degree_fraud_even_when_source_hint_is_nev(self) -> None:
        from app.services.relevance import screen_relevance

        result = screen_relevance(
            {
                "title": "假冒学历认证等网站 公安部网安局发布典型案例",
                "raw_content": "公安部网安局发布假冒学历认证等网站典型案例，提醒公众防范网络诈骗。",
                "source_type": "government",
                "industry_hint": "new_energy_vehicle",
            }
        )

        self.assertFalse(result.is_relevant)
        self.assertEqual(result.industry, "")
        self.assertIn("未命中产业关键词", result.reason)

    def test_rejects_internal_study_meeting_even_when_source_is_government(self) -> None:
        from app.services.relevance import screen_relevance

        result = screen_relevance(
            {
                "title": "国家能源局召开青年理论学习小组组长座谈会",
                "raw_content": "会议围绕青年理论学习、机关党建和干部交流开展座谈。",
                "source_type": "government",
                "industry_hint": "new_energy_vehicle",
            }
        )

        self.assertFalse(result.is_relevant)
        self.assertIn("内部会议", result.reason)

    def test_short_english_terms_do_not_match_inside_ordinary_words(self) -> None:
        from app.services.relevance import screen_relevance

        result = screen_relevance(
            {
                "title": "Available representatives review every delegation notice",
                "raw_content": "The notice is about ordinary delegation logistics and every attendee list, not an industry event.",
                "source_type": "government",
                "industry_hint": "ai",
            }
        )

        self.assertFalse(result.is_relevant)
        self.assertEqual(result.industry, "")
        self.assertNotIn("AI", result.matched_terms)
        self.assertNotIn("EV", result.matched_terms)

    def test_accepts_industry_relevant_energy_ai_article(self) -> None:
        from app.services.relevance import screen_relevance

        result = screen_relevance(
            {
                "title": "国家能源局召开全国“人工智能+”能源现场推进会",
                "raw_content": "会议推动人工智能与能源系统融合，涉及算力基础设施、智能调度和电力行业应用。",
                "source_type": "government",
                "industry_hint": "ai",
            }
        )

        self.assertTrue(result.is_relevant)
        self.assertEqual(result.industry, "人工智能")
        self.assertGreaterEqual(result.confidence, 0.6)
        self.assertIn("人工智能", result.matched_terms)


    def test_rejects_low_information_listing_shell_even_with_industry_words(self) -> None:
        from app.services.relevance import screen_relevance

        result = screen_relevance(
            {
                "title": "最后一页",
                "raw_content": "最后一页 资讯栏目每天对新能源电动汽车行业的新闻资讯第一时间报道,并且有专业的产业报道小组为您分析解读。",
                "source_type": "media",
                "industry_hint": "new_energy_vehicle",
            }
        )

        self.assertFalse(result.is_relevant)
        self.assertEqual(result.industry, "")
        self.assertIn("正文信息不足", result.reason)

    def test_rejects_source_boilerplate_headline_without_article_body(self) -> None:
        from app.services.relevance import screen_relevance

        result = screen_relevance(
            {
                "title": "富士康旗下品牌发布电动SUV",
                "raw_content": "富士康旗下品牌发布电动SUV 盖世汽车提供及时全面的:汽车销量、汽车资讯、汽车行业新闻、汽车销量排行榜、新能源汽车资讯、电动汽车资讯、汽车新闻等,分分钟知晓汽车行业动态与趋势!",
                "source_type": "media",
                "industry_hint": "new_energy_vehicle",
            }
        )

        self.assertFalse(result.is_relevant)
        self.assertEqual(result.industry, "")
        self.assertIn("正文信息不足", result.reason)

    def test_accepts_catl_english_energy_storage_article_as_nev(self) -> None:
        from app.services.relevance import screen_relevance

        result = screen_relevance(
            {
                "title": "CATL Launches World’s Largest Energy Storage Testbed",
                "raw_content": "CATL advances battery and energy storage validation for real-world deployment.",
                "source_type": "company",
                "industry_hint": "new_energy_vehicle",
            }
        )

        self.assertTrue(result.is_relevant)
        self.assertEqual(result.industry, "新能源汽车")
        self.assertIn("CATL", result.matched_terms)

    def test_accepts_embodied_intelligence_financing_as_ai(self) -> None:
        from app.services.relevance import screen_relevance

        result = screen_relevance(
            {
                "title": "5月具身智能融资：喧嚣回落，量产提速",
                "raw_content": "具身智能公司融资节奏分化，产业进入量产交付和商业化验证阶段。",
                "source_type": "media",
                "industry_hint": "ai",
            }
        )

        self.assertTrue(result.is_relevant)
        self.assertEqual(result.industry, "人工智能")
        self.assertIn("具身智能", result.matched_terms)


class LLMConfigContractTest(unittest.TestCase):
    def test_llm_settings_report_rules_fallback_without_api_key(self) -> None:
        from app.config import get_settings
        from app.services.llm import build_llm_client

        with patch.dict(
            "os.environ",
            {
                "LLM_PROVIDER": "openai",
                "LLM_MODEL": "gpt-4.1-mini",
                "LLM_API_KEY": "",
                "LLM_BASE_URL": "",
            },
        ):
            client = build_llm_client(get_settings().llm)

        self.assertFalse(client.is_configured)
        self.assertEqual(client.runtime_status()["mode"], "rules-fallback")
        self.assertEqual(client.runtime_status()["model"], "gpt-4.1-mini")
        self.assertNotIn("api_key", client.runtime_status())

    def test_llm_settings_are_configured_when_model_and_key_exist(self) -> None:
        from app.config import get_settings
        from app.services.llm import build_llm_client

        with patch.dict(
            "os.environ",
            {
                "LLM_PROVIDER": "openai",
                "LLM_MODEL": "gpt-4.1-mini",
                "LLM_API_KEY": "test-key",
                "LLM_BASE_URL": "https://api.example.com/v1",
                "LLM_TIMEOUT_SECONDS": "45",
                "LLM_TEMPERATURE": "0",
            },
        ):
            client = build_llm_client(get_settings().llm)

        status = client.runtime_status()
        self.assertTrue(client.is_configured)
        self.assertEqual(status["mode"], "llm")
        self.assertEqual(status["provider"], "openai")
        self.assertEqual(status["model"], "gpt-4.1-mini")
        self.assertNotIn("api_key", status)

    def test_openai_compatible_provider_uses_configured_base_url(self) -> None:
        import json

        from app.config import LLMSettings
        from app.services.llm import LLMClient

        captured: dict[str, object] = {}

        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": json.dumps({"summary": "ok"}, ensure_ascii=False),
                                }
                            }
                        ]
                    },
                    ensure_ascii=False,
                ).encode("utf-8")

        def fake_urlopen(request: object, timeout: int) -> FakeResponse:
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            captured["headers"] = dict(request.header_items())
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse()

        settings = LLMSettings(
            provider="dashscope",
            model="qwen-max",
            api_key="test-key",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            timeout_seconds=12,
            temperature=0,
        )

        with patch("app.services.llm.urlopen", fake_urlopen):
            payload = LLMClient(settings).complete_json("system prompt", {"title": "测试"})

        self.assertEqual(payload, {"summary": "ok"})
        self.assertEqual(captured["url"], "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions")
        self.assertEqual(captured["timeout"], 12)
        self.assertEqual(captured["body"]["model"], "qwen-max")
        self.assertEqual(captured["headers"].get("Authorization"), "Bearer test-key")

    def test_llm_status_api_does_not_expose_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                "os.environ",
                {
                    "DATABASE_PATH": str(Path(tmp) / "agent.sqlite3"),
                    "LLM_PROVIDER": "openai",
                    "LLM_MODEL": "gpt-4.1-mini",
                    "LLM_API_KEY": "secret-value",
                },
            ):
                db = Database(Path(tmp) / "agent.sqlite3")
                db.init()
                app = FastAPI()
                app.include_router(create_router(db))
                client = TestClient(app)
                response = client.get("/api/llm/status")

        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["configured"])
        self.assertEqual(payload["mode"], "llm")
        self.assertNotIn("secret-value", str(payload))
        self.assertNotIn("api_key", payload)

    def test_llm_settings_can_be_loaded_from_env_file_without_overriding_environment(self) -> None:
        from app.config import get_settings
        from app.services.llm import build_llm_client

        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        "LLM_PROVIDER=openai",
                        "LLM_MODEL=file-model",
                        "LLM_API_KEY=file-secret",
                        "LLM_BASE_URL=https://file.example.com/v1",
                    ]
                ),
                encoding="utf-8",
            )
            with patch.dict(
                "os.environ",
                {
                    "ENV_FILE": str(env_file),
                    "LLM_MODEL": "env-model",
                },
                clear=True,
            ):
                client = build_llm_client(get_settings().llm)

        status = client.runtime_status()
        self.assertTrue(client.is_configured)
        self.assertEqual(status["provider"], "openai")
        self.assertEqual(status["model"], "env-model")
        self.assertEqual(status["base_url"], "https://file.example.com/v1")
        self.assertNotIn("file-secret", str(status))

    def test_database_path_from_env_file_is_resolved_against_backend_root(self) -> None:
        from app.config import get_settings

        backend_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        "DATABASE_PATH=backend/data/opinion_agent.sqlite3",
                        "LLM_API_KEY=",
                    ]
                ),
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"ENV_FILE": str(env_file)}, clear=True):
                settings = get_settings()

        self.assertEqual(settings.database_path, backend_root / "data" / "opinion_agent.sqlite3")
        self.assertNotIn("backend\\backend", str(settings.database_path))


class RawItemNormalizationTest(unittest.TestCase):
    def test_fetch_source_items_returns_empty_when_no_article_candidates_are_discovered(self) -> None:
        def fake_fetch_url(url: str, timeout: int = 8) -> FetchedDocument:
            return FetchedDocument(
                url=url,
                text="<html><head><title>News List</title></head><body><nav>Home</nav><p>No recent items.</p></body></html>",
                content_type="text/html; charset=utf-8",
            )

        with patch("app.services.collector.fetch_url", fake_fetch_url):
            items = fetch_source_items(
                {
                    "id": 1,
                    "url": "https://example.com/news",
                    "type": "media",
                    "industry_hint": "artificial_intelligence",
                }
            )

        self.assertEqual(items, [])

    def test_raw_item_preserves_full_article_and_derives_excerpt(self) -> None:
        content = "第一段说明政策背景。" * 40 + "最后一段包含产业影响。"
        item = normalize_raw_item(
            {
                "source_id": 1,
                "url": "https://www.miit.gov.cn/xwfb/gxdt/example.html",
                "title": "工信部发布新能源汽车产业政策",
                "raw_content": content,
                "published_at": "2026-06-03T08:00:00+08:00",
            },
            fetched_at="2026-06-03T09:00:00+08:00",
        )

        self.assertEqual(item["raw_content"], content)
        self.assertLess(len(item["content_excerpt"]), len(content))
        self.assertTrue(item["content_hash"])
        self.assertEqual(item["status"], "new")

    def test_html_collection_excludes_script_style_and_template_text_from_article_content(self) -> None:
        html = """
        <html><body>
        <a href="/20260605/industry-ai-agent.html">AI agent platform product launch report</a>
        </body></html>
        """

        def article_fetcher(url: str) -> str:
            return """
            <html>
              <head>
                <title>AI agent platform product launch report</title>
                <script>
                  (function(html){html.className = html.className.replace(/\\bno-js\\b/,'js')})(document.documentElement);
                </script>
                <style>body { display: block; }</style>
              </head>
              <body>
                <script>var _md = new MobileDetect(window.navigator.userAgent);</script>
                <noscript>Please enable JavaScript.</noscript>
                <template>Hidden card template should not be indexed.</template>
                <article>OpenAI launched an enterprise agent platform for AI applications and data security.</article>
              </body>
            </html>
            """

        items = extract_source_items(
            {"id": 1, "name": "test source"},
            html,
            "https://example.com/",
            limit=1,
            article_fetcher=article_fetcher,
        )

        self.assertEqual(len(items), 1)
        self.assertIn("OpenAI launched an enterprise agent platform", items[0]["raw_content"])
        self.assertNotIn("MobileDetect", items[0]["raw_content"])
        self.assertNotIn("document.documentElement", items[0]["raw_content"])
        self.assertNotIn("display: block", items[0]["raw_content"])
        self.assertNotIn("Hidden card template", items[0]["raw_content"])

    def test_html_collection_extracts_article_published_time_from_body_metadata(self) -> None:
        html = """
        <html><body>
        <a href="/kjbgz/202605/t20260518_196621.html">央地共建北京（京津冀）国际科技创新中心工作推进会在京召开</a>
        </body></html>
        """

        def article_fetcher(url: str) -> str:
            return """
            <html>
              <head><title>央地共建北京（京津冀）国际科技创新中心工作推进会在京召开-中华人民共和国科学技术部</title></head>
              <body>
                <div>日期： 2026年05月18日 16:49 来源： 科技部</div>
                <article>央地共建北京（京津冀）国际科技创新中心工作推进会在京召开，推进人工智能和算力基础设施相关工作。</article>
              </body>
            </html>
            """

        items = extract_source_items(
            {"id": 4, "name": "科学技术部"},
            html,
            "https://www.most.gov.cn/",
            limit=1,
            article_fetcher=article_fetcher,
        )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["published_at"], "2026-05-18T16:49:00+08:00")

    def test_html_collection_falls_back_to_article_url_date_when_body_time_is_missing(self) -> None:
        html = """
        <html><body>
        <a href="/20260527/ad612fe227504da0bdaafe63f7433b9f/c.html">国家能源局召开全国“人工智能+”能源现场推进会</a>
        </body></html>
        """

        items = extract_source_items(
            {"id": 3, "name": "国家能源局"},
            html,
            "https://www.nea.gov.cn/",
            limit=1,
            article_fetcher=lambda url: "<html><title>国家能源局召开全国“人工智能+”能源现场推进会</title><body>人工智能能源现场推进会完整正文</body></html>",
        )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["published_at"], "2026-05-27T00:00:00+08:00")

    def test_raw_item_normalization_removes_inline_script_noise_before_excerpting(self) -> None:
        item = normalize_raw_item(
            {
                "source_id": 1,
                "url": "https://example.com/ai/agent-product",
                "title": "AI agent platform product launch report",
                "raw_content": (
                    "OpenAI launched an enterprise agent platform. "
                    "$(document).ready(function () { var lefttmp = new Image(); lefttmp.src = 'x'; }); "
                    "The article discusses AI applications and data security."
                ),
            },
            fetched_at="2026-06-05T09:00:00+00:00",
        )

        self.assertIn("OpenAI launched an enterprise agent platform", item["raw_content"])
        self.assertIn("AI applications and data security", item["content_excerpt"])
        self.assertNotIn("document.ready", item["raw_content"])
        self.assertNotIn("lefttmp", item["content_excerpt"])

    def test_decode_bytes_prefers_valid_utf8_when_header_misreports_latin1(self) -> None:
        text = "CVPR 2026，英伟达特斯拉Waymo一块听中国公司讲物理AI"
        payload = text.encode("utf-8")

        decoded = _decode_bytes(payload, "text/html; charset=iso-8859-1")

        self.assertEqual(decoded, text)
        self.assertNotIn("ï¼", decoded)

    def test_clean_article_text_removes_ndrc_mobile_redirect_and_tab_scripts(self) -> None:
        text = """
        NDRC article title
        var mobilurl="./t20260604_1405695_ext.html" uaredirect(mobilurl);
        var tmp = "6839"; $("#fgw_"+tmp).siblings('li').removeClass("cur"); $("#fgw_"+tmp).addClass("cur");
        Homepage News Article body about policy remains visible.
        """

        cleaned = clean_article_text(text)

        self.assertIn("NDRC article title", cleaned)
        self.assertIn("Article body about policy remains visible", cleaned)
        self.assertNotIn("mobilurl", cleaned)
        self.assertNotIn("uaredirect", cleaned)
        self.assertNotIn("#fgw_", cleaned)
        self.assertNotIn("siblings", cleaned)
        self.assertNotIn("removeClass", cleaned)
        self.assertNotIn("addClass", cleaned)

    def test_clean_article_text_removes_gasgoo_purchase_widget_and_shell_noise(self) -> None:
        text = """
        新能源车险的“深圳解法”-盖世汽车资讯
        { var html4 = ""; for (var index in data.d.purchaseInfo) { html4 += " 采购 " + data.d.purchaseInfo[index].ProductName + " " + data.d.purchaseInfo[index].NewAuditSucDate + " "; } $(".quicknews").show(); $("#purchase_supply ul").html(html4); }
        function getRandom(n) { var ram = Math.floor(Math.random() * n); return ram; }
        首页 资讯 行业 车企 供应链 智能网联 新能源 新技术 销量 高端访谈 内参 上市公司 创投 AutoNews 新车 视频 数据报告 国际 活动 专题 政策 采供平台 企业库 销量查询 邮件订阅
        新能源车险正文介绍深圳如何通过数据定价、维修协同和风险减量推动商业化。
        版权声明：本文为盖世汽车原创文章，如欲转载请遵守转载说明。
        //盖世板块统计代码异步加载 联系我们 联系邮箱：info@gasgoo.com
        """

        cleaned = clean_article_text(text)

        self.assertIn("新能源车险正文介绍深圳", cleaned)
        self.assertNotIn("html4", cleaned)
        self.assertNotIn("purchaseInfo", cleaned)
        self.assertNotIn("ProductName", cleaned)
        self.assertNotIn("getRandom", cleaned)
        self.assertNotIn("首页 资讯 行业", cleaned)
        self.assertNotIn("版权声明", cleaned)
        self.assertNotIn("盖世板块统计代码", cleaned)

    def test_clean_article_text_removes_gasgoo_remaining_sidebar_footer_noise(self) -> None:
        text = """
        新能源车险的“深圳解法”-盖世汽车资讯
        盖世汽车资讯官方QQ 2569524782 点击进入 微博 领英 返回顶部 工具栏 寻求 报道 公众号 扫码关注
        盖世汽车社区 盖世汽车每日速递 APP 汽车从业人员必备工具 7*24小时专业陪伴，助力职业成长 扫码下载APP 即刻体验
        企业库 销量 查询 采购 项目 var isFirst = true; var talkListJson; var talkListJsonLength;
        {if(result.d != null&&result.d !="") {$('#articleBottomAd').html(result.d);}else{$('#articleBottomAd').remove();}}});})
        本文地址：https://auto.gasgoo.com/news/202606/5I70460730C108.shtml
        好文章，需要你的鼓励 微信扫一扫分享该文章 点击收藏该文章
        24小时热文 5月具身智能融资：喧嚣回落，量产提速 韩国人有多爱特斯拉？
        联系我们 联系邮箱：info@gasgoo.com 新闻热线：021-39586122 站内导航 盖世汽车APP下载 版权所有2011
        """

        cleaned = clean_article_text(text)

        self.assertIn("新能源车险的“深圳解法”", cleaned)
        self.assertNotIn("盖世汽车资讯官方QQ", cleaned)
        self.assertNotIn("扫码下载APP", cleaned)
        self.assertNotIn("var isFirst", cleaned)
        self.assertNotIn("articleBottomAd", cleaned)
        self.assertNotIn("24小时热文", cleaned)
        self.assertNotIn("联系我们", cleaned)

    def test_clean_article_text_removes_d1ev_header_and_template_noise(self) -> None:
        text = """
        EV晨报 | 特斯拉CVPR 2026：FSD引入“心理评分”实现拟人化驾驶 - 第一电动网
        .add-author { } 电动汽车 搜索 提问 APP下载 第一电动 充电桩 一度用车 登录 注册 投稿 首页 快讯 视频 专题活动 首页 资讯 市场
        EV晨报 | 特斯拉CVPR 2026：FSD引入“心理评分”实现拟人化驾驶 // 评论 收藏 点赞 市场 EV晨报
        EV晨报 | 特斯拉CVPR 2026：FSD引入“心理评分”实现拟人化驾驶 第一电动 第一电动编辑部 2026-06-05 07:44 //
        要闻 特斯拉CVPR 2026披露FSD V14核心突破，端到端大模型高频运行并提升拟人化驾驶能力。
        {{each data value i}} {{if value.cover_url !==""}} {{value.title}} {{/if}} {{/each}}
        $(function () { $(".share--wraped").share({ shareTitle: "", shareUrl: "" }); });
        """

        cleaned = clean_article_text(text)

        self.assertIn("要闻 特斯拉CVPR 2026披露FSD V14核心突破", cleaned)
        self.assertNotIn("add-author", cleaned)
        self.assertNotIn("APP下载", cleaned)
        self.assertNotIn("登录 注册", cleaned)
        self.assertNotIn("评论 收藏 点赞", cleaned)
        self.assertNotIn("{{each", cleaned)
        self.assertNotIn("share--wraped", cleaned)

    def test_clean_article_text_removes_truncated_d1ev_add_author_css(self) -> None:
        text = """
        5月具身智能融资：喧嚣回落，量产提速 - 第一电动网
        .add-author { /*display...
        在刚刚过去的5月，具身智能赛道交出了一份略显矛盾的成绩单。
        """

        cleaned = clean_article_text(text)

        self.assertIn("在刚刚过去的5月", cleaned)
        self.assertNotIn("add-author", cleaned)
        self.assertNotIn("display", cleaned)

    def test_html_collection_rejects_shell_page_without_substantive_article_body(self) -> None:
        html = """
        <html><body>
          <a href="/news/202606/5I70460730C108.shtml">新能源车险的“深圳解法”</a>
        </body></html>
        """

        def article_fetcher(url: str) -> str:
            return """
            <html>
              <head><title>新能源车险的“深圳解法”-盖世汽车资讯</title></head>
              <body>
                { var html4 = ""; for (var index in data.d.purchaseInfo) { html4 += data.d.purchaseInfo[index].ProductName; } }
                首页 资讯 行业 车企 供应链 智能网联 新能源 新技术 销量 高端访谈 内参 上市公司 创投 AutoNews
                用户 反馈 提示 验证码输入错误 提交成功
                版权声明：本文为盖世汽车原创文章。
                联系我们 联系邮箱：info@gasgoo.com 站内导航 盖世汽车APP下载 版权所有2011
              </body>
            </html>
            """

        items = extract_source_items(
            {"id": 23, "name": "盖世汽车", "type": "media"},
            html,
            "https://auto.gasgoo.com/",
            limit=1,
            article_fetcher=article_fetcher,
        )

        self.assertEqual(items, [])

    def test_html_collection_rejects_semantic_tag_text_without_article_body(self) -> None:
        html = """
        <html><body>
          <a href="/2026/frontier-ai">Microsoft announces AI transformation report</a>
        </body></html>
        """

        def article_fetcher(url: str) -> str:
            return """
            <html>
              <head><title>Microsoft AI transformation report</title></head>
              <body>p cite footer cite h1 h2 h3</body>
            </html>
            """

        items = extract_source_items(
            {"id": 18, "name": "Microsoft Blog", "type": "company"},
            html,
            "https://example.com/",
            limit=1,
            article_fetcher=article_fetcher,
            article_page_limit=1,
        )

        self.assertEqual(items, [])

    def test_clean_article_text_removes_flattened_analytics_css_and_json_ld_noise(self) -> None:
        text = """
        (function(html){html.className = html.className.replace(/\\bno-js\\b/,'js')})(document.documentElement);
        {"@context":"https:\\/\\/schema.org","@graph":[{"@type":"Article","headline":"AI needs humanity"}]}
        img:is([sizes=auto i],[sizes^="auto," i]){contain-intrinsic-size:3000px 1500px}
        :root{--wp-admin-theme-color:#007cba;--wp-admin-border-width-focus:2px}
        .wp-element-button{cursor:pointer}
        Article paragraph starts here with AI agent product evidence.
        (function(w,d,s,l,i){w[l]=w[l]||[];w[l].push({'gtm.start': new Date().getTime(),event:'gtm.js'});var f=d.getElementsByTagName(s)[0],j=d.createElement(s);})(window,document,'script','dataLayer','GTM-TEST');
        window.dataLayer = window.dataLayer || []; function gtag() { dataLayer.push(arguments); } gtag('js', new Date()); gtag('config', 'G-TEST');
        // 百度统计 var _hmt = _hmt || []; (function () { var hm = document.createElement("script"); hm.src = "//hm.baidu.com/hm.js"; })();
        """

        cleaned = clean_article_text(text)

        self.assertIn("Article paragraph starts here", cleaned)
        self.assertNotIn("document.documentElement", cleaned)
        self.assertNotIn("@context", cleaned)
        self.assertNotIn("schema.org", cleaned)
        self.assertNotIn("contain-intrinsic-size", cleaned)
        self.assertNotIn("--wp-admin", cleaned)
        self.assertNotIn("gtm.start", cleaned)
        self.assertNotIn("dataLayer", cleaned)
        self.assertNotIn("百度统计", cleaned)
        self.assertNotIn("document.createElement", cleaned)

    def test_clean_article_text_removes_remaining_flattened_site_scripts(self) -> None:
        text = """
        Article lead paragraph about AI infrastructure remains visible.
        generate one if it doesn't exist. skipLinkTargetID = skipLinkTarget.id;
        skipLink = document.createElement( 'a' );
        sibling.parentElement.insertBefore( skipLink, sibling ); }() );
        //# sourceURL=wp-block-template-skip-link-js-after
        var ttsPlayerConfig = {"ttsPlayerConfig":{"playerId":"abc"},"translations":{"play":"Play"}};
        new WX_Custom_Share().init();
        (function(){ var bp = document.createElement('script'); bp.src = 'https://zz.bdstatic.com/linksubmit/push.js';
        var s = document.getElementsByTagName("script")[0]; s.parentNode.insertBefore(bp, s); })();
        // gasgoo async stats (function () { var ma = document.createElement('script');
        ma.src = 'https://hm.gasgoo.com/AreaHits.js'; var s = document.getElementsByTagName('script')[0];
        s.parentNode.insertBefore(ma, s); })();
        var _gas_hm = _gas_hm || []; _gas_hm.push(['_webSiteId', '49e952cb']);
        Article conclusion about vehicle insurance remains visible.
        """

        cleaned = clean_article_text(text)

        self.assertIn("Article lead paragraph", cleaned)
        self.assertIn("Article conclusion", cleaned)
        self.assertNotIn("document.createElement", cleaned)
        self.assertNotIn("skipLink", cleaned)
        self.assertNotIn("ttsPlayerConfig", cleaned)
        self.assertNotIn("WX_Custom_Share", cleaned)
        self.assertNotIn("bdstatic", cleaned)
        self.assertNotIn("gasgoo.com/AreaHits", cleaned)
        self.assertNotIn("_gas_hm", cleaned)

    def test_clean_article_text_removes_visible_css_fragments_and_nav_jquery(self) -> None:
        text = """
        CVPR 2026 AI article title
        @media (min-resolution:192dpi){ }
        .has-fit-text{white-space:nowrap!important}
        .aligncenter{clear:both}
        html :where([style* html :where([style* html :where([style*
        html :where(img[class* @media screen and (max-width:600px){ }
        .is-layout-flex{flex-wrap: wrap;align-items: center;}
        .is-layout-flex > :is(* .is-layout-grid > :is(*
        Article paragraph about physical AI remains visible.
        $("#top_search").click(function(){ $("#search").toggle()
        $(document).click(function(){ $(".weixin_pop").hide();
        采购项目 $("#TopProcurement").mouseleave(function () { $(".topMenu .Procurementproject").hide();
        $("#TopProcurement").mousemove(function () { $(".topMenu .Procurementproject").show();
        —— 全球视野·中国声音 —— jQuery(function ($) { $ ); $ ); $.ajax({ url: '/Home.aspx/GetAdvert', type: 'POST' });
        Article paragraph about vehicle insurance remains visible.
        """

        cleaned = clean_article_text(text)

        self.assertIn("Article paragraph about physical AI", cleaned)
        self.assertIn("Article paragraph about vehicle insurance", cleaned)
        self.assertNotIn("@media", cleaned)
        self.assertNotIn("has-fit-text", cleaned)
        self.assertNotIn("aligncenter", cleaned)
        self.assertNotIn("html :where", cleaned)
        self.assertNotIn("is-layout-flex", cleaned)
        self.assertNotIn("top_search", cleaned)
        self.assertNotIn("weixin_pop", cleaned)
        self.assertNotIn("TopProcurement", cleaned)
        self.assertNotIn("jQuery(function", cleaned)
        self.assertNotIn("$.ajax", cleaned)

    def test_clean_article_text_removes_nested_media_and_wordpress_css_blocks(self) -> None:
        text = """
        .wp-block-column{flex-basis:100%!important}}@media (min-width:782px){
        .wp-block-columns:not(.is-not-stacked-on-mobile)>.wp-block-column{flex-basis:0;flex-grow:1}
        .wp-block-columns:not(.is-not-stacked-on-mobile)>.wp-block-column[style*=flex-basis]{flex-grow:0}}
        .wp-block-column.is-vertically-aligned-center{align-self:center}
        Microsoft article paragraph about AI transformation remains visible.
        """

        cleaned = clean_article_text(text)

        self.assertIn("Microsoft article paragraph about AI transformation", cleaned)
        self.assertNotIn("@media", cleaned)
        self.assertNotIn("wp-block", cleaned)
        self.assertNotIn("flex-basis", cleaned)

    def test_clean_article_text_removes_truncated_css_without_closing_boundary(self) -> None:
        text = """
        Government article title @media screen and (max-width: 767px) and (orientation: portrait) { .BreadcrumbNav { font-family: "Segoe
        """

        cleaned = clean_article_text(text)

        self.assertEqual(cleaned, "Government article title")
        self.assertNotIn("@media", cleaned)
        self.assertNotIn("BreadcrumbNav", cleaned)

    def test_clean_article_text_removes_truncated_media_without_opening_brace(self) -> None:
        text = """
        Government article title @media screen and (max-width: 767px) and (orientation: por 该信息被识别为政策监管信号。
        """

        cleaned = clean_article_text(text)

        self.assertIn("Government article title", cleaned)
        self.assertIn("该信息被识别为政策监管信号", cleaned)
        self.assertNotIn("@media", cleaned)

    def test_clean_article_text_removes_orphan_media_query_tail(self) -> None:
        text = """
        Government article title screen and (max-width: 767px) and (orientation: por 该信息被识别为政策监管信号。
        """

        cleaned = clean_article_text(text)

        self.assertIn("Government article title", cleaned)
        self.assertIn("该信息被识别为政策监管信号", cleaned)
        self.assertNotIn("max-width", cleaned)
        self.assertNotIn("orientation", cleaned)

    def test_clean_article_text_removes_orphan_braces(self) -> None:
        self.assertEqual(clean_article_text("}"), "")
        self.assertEqual(clean_article_text("Article title }"), "Article title")

    def test_clean_article_text_removes_broken_wordpress_css_fragments(self) -> None:
        text = """
        } .wp-block-columns.is-not-stacked-on-mobile{flex-wrap:nowrap!important}.wp-block-columns.is-not-stacked-on-mobile>
        .wp-block-post-template:has(.wp-block-bloginabox-theme-card--horizontal) .section__content{position:relative;z-index:2}
        Microsoft article paragraph about AI transformation remains visible.
        """

        cleaned = clean_article_text(text)

        self.assertIn("Microsoft article paragraph about AI transformation", cleaned)
        self.assertNotIn("wp-block", cleaned)
        self.assertNotIn("section__content", cleaned)
        self.assertNotIn("flex-wrap", cleaned)

    def test_clean_article_text_removes_broken_wordpress_selector_chains(self) -> None:
        text = """
        .is-style-search .section-background-image-wrapper--mobile.section-background-image-wrapper--light,
        .section__background[:not-has(.section-background-image-wrapper--mobile) :where(.wp-block-button.is-style-outline>
        h1,.wp-block-post-content h2,.wp-block-post-content h3{text-wrap:wrap
        Microsoft article paragraph about AI transformation remains visible.
        """

        cleaned = clean_article_text(text)

        self.assertIn("Microsoft article paragraph about AI transformation", cleaned)
        self.assertNotIn("wp-block", cleaned)
        self.assertNotIn("section-background", cleaned)
        self.assertNotIn("text-wrap", cleaned)

    def test_clean_article_text_removes_orphan_wordpress_selector_tails(self) -> None:
        text = """
        h1,.wp-block-post-content h2,.wp-block-post-content h3,.wp-block-post-content h4{text-wrap:wrap
        Microsoft article paragraph about AI transformation remains visible.
        """

        cleaned = clean_article_text(text)

        self.assertIn("Microsoft article paragraph about AI transformation", cleaned)
        self.assertNotIn("wp-block", cleaned)
        self.assertNotIn("text-wrap", cleaned)

    def test_clean_article_text_removes_government_header_search_scripts(self) -> None:
        text = """
        var INFO_FLAG={ channelinfo:"要闻最新5257729206" }
        @media screen and (max-width: 767px) and (orientation: portrait) { .BreadcrumbNav { font-family: "Segoe UI"; } }
        首页 EN 登录 个人中心 退出 邮箱 无障碍 EN
        function goSearch(event) { var e = window.event || event; var s = '', m = document.querySelector('.header .pchide [name=headSearchword]'); window.open(url, '_blank'); }
        function listenerKeyUpEventFn(){ if(event.keyCode==13){ goSearch(); }; }
        https://www.gov.cn/ //繁体和简体相互转换 var currUrl = window.location.href; $('.header_logo a').attr('href',jtzw);
        Article paragraph from the government source remains visible.
        """

        cleaned = clean_article_text(text)

        self.assertIn("Article paragraph from the government source", cleaned)
        self.assertNotIn("INFO_FLAG", cleaned)
        self.assertNotIn("@media", cleaned)
        self.assertNotIn("function goSearch", cleaned)
        self.assertNotIn("document.querySelector", cleaned)
        self.assertNotIn("header_logo", cleaned)

    def test_clean_article_text_removes_adobe_target_and_qbitai_popup_scripts(self) -> None:
        text = """
        // Define Adobe Target Property var at_property = "beedee45-3f39";
        // Workspace Name ! function () {
        window.tt_getCookie = function (t) { var e = RegExp(t + "[^;]+").exec(document.cookie); return decodeURIComponent(e ? e.toString() : "") }
        window.targetPageParams = function () { return { "at_property": at_property } }
        }();
        // ContentSquare functions function isEmpty(val) { return (val === undefined || val == null || val.length
        AI needs more than intelligence article paragraph remains visible.
        // $('.biaojiwei').click(function(){
        $('.biaojiwei').click(function(event){ event.stopPropagation(); $('#pophead').show();
        $('.weixin_pop').click(function(){ $('.weixin_pop').hide();
        // img id="wx_img" src="https://www.qbitai.com/wp-content/uploads/imgs/qbitai-logo-1.png" width="400" height="400">
        Physical AI article paragraph remains visible.
        """

        cleaned = clean_article_text(text)

        self.assertIn("AI needs more than intelligence article paragraph", cleaned)
        self.assertIn("Physical AI article paragraph", cleaned)
        self.assertNotIn("Adobe Target", cleaned)
        self.assertNotIn("at_property", cleaned)
        self.assertNotIn("tt_getCookie", cleaned)
        self.assertNotIn("targetPageParams", cleaned)
        self.assertNotIn("ContentSquare", cleaned)
        self.assertNotIn("biaojiwei", cleaned)
        self.assertNotIn("weixin_pop", cleaned)
        self.assertNotIn("pophead", cleaned)
        self.assertNotIn("wx_img", cleaned)

    def test_clean_article_text_removes_raw_archive_remaining_scripts(self) -> None:
        text = """
        Periodic reports-BYD
        $.ajax({headers:{Accept:"application/json;charset=utf-8;"},type:'get',datatype:'json',
        url:'/sites/REST/resources/v1/search/sites/BYD_EN/types/BydInvestorNotice/assets?fields=name',
        success:function(data){ acquirePdfUrl(pdfUrl); $(".content_table ul.message").append(str); }});
        BYD investor report paragraph remains visible.
        WX_Custom_Share = function(){ var xhr = null; var url = 'https://www.qbitai.com/wp-admin/admin-ajax.php';
        this.init = function(){ get_share_info(); } function get_share_info(){ setShareInfo( info ); } };
        QbitAI article paragraph remains visible.
        jQuery(function () { $.ajax({ url: '/Home.aspx/GetComments', type: 'POST', success: function (data) {
        var screenH = document.documentElement.clientHeight; $('#talkDlist').liMarquee({ direction: 'up' });
        talkChange(); } }); 正在热评 实时热评
        Gasgoo article paragraph remains visible.
        """

        cleaned = clean_article_text(text)

        self.assertIn("BYD investor report paragraph", cleaned)
        self.assertIn("QbitAI article paragraph", cleaned)
        self.assertIn("Gasgoo article paragraph", cleaned)
        self.assertNotIn("$.ajax", cleaned)
        self.assertNotIn("WX_Custom_Share", cleaned)
        self.assertNotIn("document.documentElement", cleaned)
        self.assertNotIn("jQuery(function", cleaned)

    def test_clean_article_text_removes_malformed_dynamic_ajax_blocks(self) -> None:
        text = """
        Periodic reports-BYD useful heading remains visible.
        page.init(data.items.length,1,options);if(data.items.length==0){layer.msg ("Check whether the keyword input is incorrect")}
        $.ajax({headers:{Accept:"application/json;charset=utf-8;"},type:'get',datatype:'json',global:false,
        url:'/sites/REST/resources/v1/search/sites/BYD_EN/types/BydInvestorNotice/assets?fields=name',
        success:function(data){var str=""; acquirePdfUrl(pdfUrl); function acquirePdfUrl(pdfUrl){$.ajax({url:pdfUrl,type:"get"
        Gasgoo useful article paragraph remains visible.
        $.ajax({ type: 'post', url: 'https://auto.gasgoo.com/Article.aspx/GetArticleVideos',
        contentType: 'application/json; charset=utf-8', data: "{ 'id':'70460639'}", success: function(html) { console.log(html)
        24小时热文 5月具身智能融资：喧嚣回落，量产提速
        """

        cleaned = clean_article_text(text)

        self.assertIn("Periodic reports-BYD useful heading", cleaned)
        self.assertIn("Gasgoo useful article paragraph", cleaned)
        self.assertNotIn("$.ajax", cleaned)
        self.assertNotIn("BydInvestorNotice", cleaned)
        self.assertNotIn("GetArticleVideos", cleaned)

    def test_clean_article_text_removes_late_nested_byd_ajax_function(self) -> None:
        text = """
        Periodic reports-BYD useful heading remains visible.
        function acquirePdfUrl(pdfUrl){$.ajax({url:pdfUrl,type:"get",headers:{Accept:"application/json; charset=utf-8" },
        success:function(pdf){var pdfJson=JSON.parse(pdf);$("#pdf"+pdfJson.id).attr("href",pdfOpen);}
        } var wid=$(window).width()/10;var url =location.search;function GetRequest() {var theRequest =new Object();}
        BYD investor report paragraph remains visible.
        """

        cleaned = clean_article_text(text)

        self.assertIn("Periodic reports-BYD useful heading", cleaned)
        self.assertIn("BYD investor report paragraph", cleaned)
        self.assertNotIn("function acquirePdfUrl", cleaned)
        self.assertNotIn("$.ajax", cleaned)

    def test_summary_and_key_fact_evidence_clean_legacy_script_noise(self) -> None:
        from app.services.extractor import build_summary, extract_key_facts

        classification = SimpleNamespace(
            entities=[],
            event_types=["产品发布"],
            importance_level="high",
            importance_reason="AI 产品发布，具备跟踪价值。",
            industry="人工智能",
            signal_attributes=["商业化进展"],
        )
        item = {
            "title": "AI agent platform product launch report",
            "content_excerpt": (
                "(function(html){html.className = html.className.replace(/\\bno-js\\b/,'js')})(document.documentElement); "
                "OpenAI launched an enterprise agent platform for AI applications."
            ),
            "raw_content": "",
            "published_at": "2026-06-05T09:00:00+00:00",
        }

        summary = build_summary(item, classification)
        facts = extract_key_facts(item, classification)

        self.assertIn("OpenAI launched an enterprise agent platform", summary)
        self.assertIn("OpenAI launched an enterprise agent platform", facts["evidence"])
        self.assertNotIn("document.documentElement", summary)
        self.assertNotIn("html.className", facts["evidence"])

    def test_summary_falls_back_when_excerpt_is_only_code_fragment(self) -> None:
        from app.services.extractor import build_summary, extract_key_facts

        classification = SimpleNamespace(
            entities=[],
            event_types=["龙头企业动向"],
            importance_level="high",
            importance_reason="AI 龙头企业动向，具备跟踪价值。",
            industry="人工智能",
            signal_attributes=["商业化进展"],
        )
        item = {
            "title": "Microsoft AI transformation report",
            "content_excerpt": "}",
            "raw_content": "Microsoft explains frontier transformation and practical enterprise AI adoption.",
            "published_at": "2026-06-05T09:00:00+00:00",
        }

        summary = build_summary(item, classification)
        facts = extract_key_facts(item, classification)

        self.assertIn("Microsoft explains frontier transformation", summary)
        self.assertIn("Microsoft explains frontier transformation", facts["evidence"])
        self.assertNotIn("}", summary)

    def test_extraction_uses_configured_llm_for_summary_and_key_facts(self) -> None:
        from app.services.extractor import extract_intelligence

        class FakeSettings:
            provider = "openai"
            model = "test-extractor-model"

        class FakeLLMClient:
            settings = FakeSettings()
            is_configured = True

            def complete_json(self, system_prompt: str, user_payload: dict[str, object]) -> dict[str, object]:
                self.system_prompt = system_prompt
                self.user_payload = user_payload
                return {
                    "summary": "OpenAI 企业级 Agent 平台升级，重点强化数据安全与 API 编排能力。",
                    "key_facts": {
                        "who": "OpenAI",
                        "what": "发布企业级 Agent 平台升级",
                        "when": "2026-06-05T09:00:00+00:00",
                        "where": "公开来源披露",
                        "why": "企业客户对智能体编排和数据安全能力需求提升",
                        "impact": "该事件显示 AI 应用商业化继续深化，需追踪企业服务竞争格局。",
                        "evidence": "OpenAI launched an enterprise agent platform for AI applications and data security.",
                    },
                    "impact_analysis": "该事件显示 AI 应用商业化继续深化，需追踪企业服务竞争格局。",
                    "source_spans": [
                        {
                            "field": "raw_content",
                            "start": 0,
                            "end": 86,
                            "quote": "OpenAI launched an enterprise agent platform for AI applications and data security.",
                        }
                    ],
                }

        classification = SimpleNamespace(
            industry="人工智能",
            industry_subtags=["智能体/Agent"],
            event_types=["产品发布"],
            importance_level="high",
            importance_reason="模型厂商发布企业级智能体平台，具备商业化跟踪价值。",
            subject_roles=["整车厂/模型厂商"],
            signal_attributes=["商业化进展"],
            confidence=0.91,
            evidence=["产品发布"],
            entities=[{"text": "OpenAI", "type": "company", "confidence": 0.9}],
        )
        item = {
            "title": "OpenAI 发布企业级 Agent 平台升级",
            "raw_content": "OpenAI launched an enterprise agent platform for AI applications and data security.",
            "content_excerpt": "OpenAI launched an enterprise agent platform.",
            "published_at": "2026-06-05T09:00:00+00:00",
            "source_name": "OpenAI News",
        }
        fake_client = FakeLLMClient()

        result = extract_intelligence(item, classification, llm_client=fake_client)

        self.assertEqual(result.summary, "OpenAI 企业级 Agent 平台升级，重点强化数据安全与 API 编排能力。")
        self.assertEqual(result.key_facts["who"], "OpenAI")
        self.assertEqual(result.impact_analysis, "该事件显示 AI 应用商业化继续深化，需追踪企业服务竞争格局。")
        self.assertEqual(result.mode, "llm")
        self.assertEqual(result.provider, "openai")
        self.assertEqual(result.model, "test-extractor-model")
        self.assertIsNone(result.fallback_reason)
        self.assertIn("source_name", fake_client.user_payload)
        self.assertEqual(fake_client.user_payload["classification"]["event_types"], ["产品发布"])
        self.assertIn("input_contract", fake_client.user_payload)
        self.assertIn("summary_requirements", fake_client.user_payload)
        self.assertIn("key_facts_schema", fake_client.user_payload)
        self.assertIn("impact_analysis_rules", fake_client.user_payload)
        self.assertIn("source_span_requirements", fake_client.user_payload)
        self.assertIn("output_requirements", fake_client.user_payload)
        self.assertIn("who", fake_client.user_payload["key_facts_schema"])
        self.assertIn("impact", fake_client.user_payload["key_facts_schema"])
        self.assertIn("raw_content", fake_client.user_payload["source_span_requirements"]["allowed_fields"])
        self.assertIn("证据", fake_client.system_prompt)
        self.assertIn("不要编造", fake_client.system_prompt)
        self.assertIn("产业影响", fake_client.system_prompt)

    def test_bounded_llm_content_caps_long_readable_text(self) -> None:
        from app.services.extractor import _bounded_llm_content

        content = "Microsoft explains practical enterprise AI adoption and frontier transformation. " * 300

        bounded = _bounded_llm_content({"raw_content": content})

        self.assertLessEqual(len(bounded), 3500)
        self.assertIn("Microsoft explains practical enterprise AI adoption", bounded)

    def test_extraction_sends_bounded_clean_content_to_llm(self) -> None:
        from app.services.extractor import extract_intelligence

        class FakeSettings:
            provider = "openai"
            model = "test-extractor-model"

        class FakeLLMClient:
            settings = FakeSettings()
            is_configured = True

            def complete_json(self, system_prompt: str, user_payload: dict[str, object]) -> dict[str, object]:
                self.user_payload = user_payload
                return {
                    "summary": "微软企业 AI 应用案例显示商业化推进。",
                    "key_facts": {
                        "who": "Microsoft",
                        "what": "说明企业 AI 应用进展",
                        "when": "2026-06-05T09:00:00+00:00",
                        "where": "公开来源披露",
                        "why": "企业 AI 应用落地加速",
                        "impact": "AI 企业服务商业化继续推进。",
                        "evidence": "Microsoft explains frontier transformation and practical enterprise AI adoption.",
                    },
                    "impact_analysis": "AI 企业服务商业化继续推进。",
                    "source_spans": [{"field": "raw_content", "start": 0, "end": 80}],
                }

        classification = SimpleNamespace(
            industry="人工智能",
            industry_subtags=["企业服务"],
            event_types=["龙头企业动向"],
            importance_level="high",
            importance_reason="AI 龙头企业动向，具备跟踪价值。",
            subject_roles=["整车厂/模型厂商"],
            signal_attributes=["商业化进展"],
            confidence=0.9,
            entities=[{"text": "Microsoft", "type": "company"}],
        )
        long_noise = "(function(html){html.className='js'})(document.documentElement);" * 40
        long_content = long_noise + "Microsoft explains frontier transformation and practical enterprise AI adoption. " * 300
        fake_client = FakeLLMClient()

        extract_intelligence(
            {
                "title": "Microsoft AI transformation report",
                "raw_content": long_content,
                "content_excerpt": long_content[:1000],
                "published_at": "2026-06-05T09:00:00+00:00",
            },
            classification,
            llm_client=fake_client,
        )

        self.assertLessEqual(len(fake_client.user_payload["content"]), 3500)
        self.assertIn("Microsoft explains frontier transformation", fake_client.user_payload["content"])
        self.assertNotIn("document.documentElement", fake_client.user_payload["content"])

    def test_extraction_accepts_single_source_span_object_from_llm(self) -> None:
        from app.services.extractor import extract_intelligence

        class FakeSettings:
            provider = "dashscope"
            model = "qwen-plus"

        class FakeLLMClient:
            settings = FakeSettings()
            is_configured = True

            def complete_json(self, system_prompt: str, user_payload: dict[str, object]) -> dict[str, object]:
                return {
                    "summary": "智在无界发布隐式世界模型产品，推进端侧实时部署。",
                    "key_facts": {
                        "who": "智在无界",
                        "what": "发布隐式世界模型产品 Being-H-Flash",
                        "when": "2026-06-05T09:00:00+00:00",
                        "where": "公开来源披露",
                        "why": "提升端侧芯片实时部署能力",
                        "impact": "世界模型商业化和端侧部署竞争加速。",
                        "evidence": "智在无界发布隐式世界模型产品。",
                    },
                    "impact_analysis": "世界模型商业化和端侧部署竞争加速。",
                    "source_spans": {"field": "content", "start": 0, "end": 20, "quote": "智在无界发布隐式世界模型产品"},
                }

        classification = SimpleNamespace(
            industry="人工智能",
            industry_subtags=["机器人"],
            event_types=["产品发布"],
            importance_level="high",
            importance_reason="世界模型产品发布。",
            subject_roles=["整车厂/模型厂商"],
            signal_attributes=["商业化进展"],
            confidence=0.9,
            entities=[{"text": "智在无界", "type": "company"}],
        )
        result = extract_intelligence(
            {
                "title": "智在无界发布隐式世界模型产品Being-H-Flash",
                "raw_content": "智在无界发布隐式世界模型产品Being-H-Flash，完成端侧芯片实时部署。",
                "content_excerpt": "智在无界发布隐式世界模型产品Being-H-Flash。",
                "published_at": "2026-06-05T09:00:00+00:00",
            },
            classification,
            llm_client=FakeLLMClient(),
        )

        self.assertEqual(result.mode, "llm")
        self.assertEqual(result.source_spans[0]["field"], "raw_content")
        self.assertIn("智在无界", result.source_spans[0]["quote"])

    def test_extraction_accepts_single_item_key_facts_array_from_llm(self) -> None:
        from app.services.extractor import extract_intelligence

        class FakeSettings:
            provider = "dashscope"
            model = "qwen-plus"

        class FakeLLMClient:
            settings = FakeSettings()
            is_configured = True

            def complete_json(self, system_prompt: str, user_payload: dict[str, object]) -> dict[str, object]:
                return {
                    "summary": "小鹏发布物理 AI 技术体系，展示第二代 VLA 与世界模型协同进展。",
                    "key_facts": [
                        {
                            "who": "小鹏汽车",
                            "what": "发布物理 AI 技术体系",
                            "when": "2026-06-05T09:00:00+00:00",
                            "where": "CVPR 2026",
                            "why": "提升自动驾驶与具身智能的世界模型能力",
                            "impact": "物理 AI 技术路线竞争加速。",
                            "evidence": "小鹏发布第二代 VLA 与世界模型协同架构。",
                        }
                    ],
                    "impact_analysis": "物理 AI 技术路线竞争加速。",
                    "source_spans": [{"field": "content", "start": 0, "end": 20, "quote": "小鹏发布第二代 VLA"}],
                }

        classification = SimpleNamespace(
            industry="人工智能",
            industry_subtags=["机器人"],
            event_types=["技术突破"],
            importance_level="high",
            importance_reason="龙头企业技术路线变化。",
            subject_roles=["整车厂/模型厂商"],
            signal_attributes=["技术路线变化"],
            confidence=0.9,
            entities=[{"text": "小鹏汽车", "type": "company"}],
        )
        result = extract_intelligence(
            {
                "title": "小鹏发布物理 AI 技术体系",
                "raw_content": "小鹏发布第二代 VLA 与世界模型协同架构。",
                "content_excerpt": "小鹏发布第二代 VLA 与世界模型协同架构。",
                "published_at": "2026-06-05T09:00:00+00:00",
            },
            classification,
            llm_client=FakeLLMClient(),
        )

        self.assertEqual(result.mode, "llm")
        self.assertEqual(result.key_facts["who"], "小鹏汽车")
        self.assertEqual(result.provider, "dashscope")
        self.assertEqual(result.model, "qwen-plus")

    def test_extraction_falls_back_to_rules_when_llm_payload_is_invalid(self) -> None:
        from app.services.extractor import extract_intelligence

        class FakeSettings:
            provider = "openai"
            model = "bad-extractor-model"

        class FakeLLMClient:
            settings = FakeSettings()
            is_configured = True

            def complete_json(self, system_prompt: str, user_payload: dict[str, object]) -> dict[str, object]:
                return {"summary": ""}

        classification = SimpleNamespace(
            entities=[],
            event_types=["龙头企业动向"],
            importance_level="high",
            importance_reason="AI 龙头企业动向，具备跟踪价值。",
            industry="人工智能",
            signal_attributes=["商业化进展"],
        )
        item = {
            "title": "Microsoft AI transformation report",
            "content_excerpt": "}",
            "raw_content": "Microsoft explains frontier transformation and practical enterprise AI adoption.",
            "published_at": "2026-06-05T09:00:00+00:00",
        }

        result = extract_intelligence(item, classification, llm_client=FakeLLMClient())

        self.assertEqual(result.mode, "rules_fallback")
        self.assertIn("invalid", result.fallback_reason)
        self.assertIn("Microsoft explains frontier transformation", result.summary)
        self.assertEqual(result.provider, "rules+extractor")
        self.assertEqual(result.model, "local-rules")

    def test_html_collection_fetches_every_valid_article_page(self) -> None:
        html = """
        <html><head><meta name="description" content="listing summary"></head><body>
        <a href="/news/1">AI chip capacity expansion article one</a>
        <a href="/news/2">AI chip capacity expansion article two</a>
        <a href="/news/3">AI chip capacity expansion article three</a>
        <a href="/news/4">AI chip capacity expansion article four</a>
        </body></html>
        """
        fetched_urls: list[str] = []

        def article_fetcher(url: str) -> str:
            fetched_urls.append(url)
            return f"<html><title>{url}</title><body>Full article body for {url}</body></html>"

        items = extract_source_items(
            {"id": 1, "name": "test source"},
            html,
            "https://example.com/",
            limit=4,
            article_fetcher=article_fetcher,
        )

        self.assertEqual(len(items), 4)
        self.assertEqual(len(fetched_urls), 4)
        for index, item in enumerate(items, start=1):
            self.assertIn(f"Full article body for https://example.com/news/{index}", item["raw_content"])

    def test_html_collection_filters_candidates_by_article_published_window(self) -> None:
        html = """
        <html><body>
        <a href="/news/before.html">AI chip capacity article before window</a>
        <a href="/news/inside-one.html">AI chip capacity article inside window one</a>
        <a href="/news/inside-two.html">AI chip capacity article inside window two</a>
        <a href="/news/after.html">AI chip capacity article after window</a>
        </body></html>
        """
        article_dates = {
            "https://example.com/news/before.html": "2026-06-04T23:59:00+00:00",
            "https://example.com/news/inside-one.html": "2026-06-05T00:00:00+00:00",
            "https://example.com/news/inside-two.html": "2026-06-05T08:30:00+00:00",
            "https://example.com/news/after.html": "2026-06-05T09:01:00+00:00",
        }

        def article_fetcher(url: str) -> str:
            published = article_dates[url]
            return f"""
            <html>
              <head>
                <title>{url}</title>
                <meta name="publishdate" content="{published}">
              </head>
              <body>Full article body for {url} about AI chip capacity expansion.</body>
            </html>
            """

        items = extract_source_items(
            {"id": 1, "name": "test source"},
            html,
            "https://example.com/",
            limit=10,
            article_fetcher=article_fetcher,
            published_after=datetime(2026, 6, 5, 0, 0, tzinfo=timezone.utc),
            published_before=datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(
            [item["url"] for item in items],
            [
                "https://example.com/news/inside-one.html",
                "https://example.com/news/inside-two.html",
            ],
        )

    def test_html_collection_ignores_navigation_and_column_links(self) -> None:
        html = """
        <html><body>
        <a href="/lxwm/index.htm">联系我们</a>
        <a href="/xwzx/index.htm">新闻中心</a>
        <a href="http://www.beian.gov.cn/portal/registerSystemInfo?recordcode=123">公网安备 123 号</a>
        <a href="/xwdt/xwfb/">新闻发布-国家发展和改革委员会</a>
        <a href="/20260604/ef70594173a14f74afe36fd8382fb592/c.html">国家能源局召开人工智能能源现场推进会</a>
        </body></html>
        """

        items = extract_source_items(
            {"id": 3, "name": "国家能源局"},
            html,
            "https://www.nea.gov.cn/",
            limit=8,
            article_fetcher=lambda url: f"<html><title>{url}</title><body>人工智能能源现场推进会完整正文</body></html>",
        )

        self.assertEqual([item["url"] for item in items], ["https://www.nea.gov.cn/20260604/ef70594173a14f74afe36fd8382fb592/c.html"])

    def test_html_collection_rejects_investor_report_listing_pages(self) -> None:
        html = """
        <html><body>
        <a href="/en/InvestorAnnals.html">Periodic reports-BYD</a>
        <a href="/en/news/20260605-byd-overseas-delivery.html">BYD expands overseas passenger vehicle delivery</a>
        </body></html>
        """

        def article_fetcher(url: str) -> str:
            if "InvestorAnnals" in url:
                return """
                <html><title>Periodic reports-BYD</title><body>
                .page> .m_icon{dispaly:block;} @media (max-width:1024px){.m_word{top:92vw;}}
                Home Sustainable Future Products & Solutions Choose Region/Language
                page.init(data.items.length,1,options); function GetRequest(){var theRequest=new Object();}
                </body></html>
                """
            return """
            <html><title>BYD expands overseas passenger vehicle delivery</title>
            <body>BYD reported overseas market delivery growth for new energy passenger vehicles.</body></html>
            """

        items = extract_source_items(
            {"id": 11, "name": "比亚迪新闻中心"},
            html,
            "https://www.bydglobal.com/en/News.html",
            limit=4,
            article_fetcher=article_fetcher,
        )

        self.assertEqual([item["url"] for item in items], ["https://www.bydglobal.com/en/news/20260605-byd-overseas-delivery.html"])
        self.assertNotIn("Periodic reports-BYD", " ".join(item["title"] for item in items))


class RepresentativeSelectionTest(unittest.TestCase):
    def test_same_event_uses_authoritative_representative_but_keeps_all_sources(self) -> None:
        items = [
            {
                "id": 10,
                "title": "转载：某地发布新能源汽车政策",
                "source_name": "行业媒体",
                "source_reliability": 0.75,
                "raw_content": "短内容",
                "published_at": "2026-06-03T09:20:00+08:00",
            },
            {
                "id": 11,
                "title": "某地印发新能源汽车产业支持政策",
                "source_name": "地方政府官网",
                "source_reliability": 1.0,
                "raw_content": "完整政策正文" * 80,
                "published_at": "2026-06-03T09:10:00+08:00",
            },
        ]

        representative = choose_representative_item(items)

        self.assertEqual(representative["id"], 11)
        self.assertEqual(len(items), 2)


class BriefGenerationContractTest(unittest.TestCase):
    def test_generate_brief_uses_fetched_at_when_published_at_is_missing(self) -> None:
        from app.services.briefs import generate_brief

        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "agent.sqlite3")
            db.init()
            source_id = db.execute(
                """
                INSERT INTO sources (name, type, url, industry_hint, reliability_score)
                VALUES ('Test Source', 'media', 'https://example.com/feed', 'artificial_intelligence', 0.8)
                """
            )
            raw_id = db.execute(
                """
                INSERT INTO raw_items
                    (source_id, url, canonical_url, title, published_at, fetched_at, raw_content, content_excerpt,
                     content_hash, status)
                VALUES (?, 'https://example.com/item', 'https://example.com/item', 'AI platform update',
                        NULL, '2026-06-05T08:00:00+00:00', 'AI platform update content', 'AI platform update',
                        'hash-1', 'processed')
                """,
                (source_id,),
            )
            processed_id = db.execute(
                """
                INSERT INTO processed_items
                    (raw_item_id, normalized_title, summary, key_facts_json, impact_analysis,
                     importance_level, rank_score, created_at)
                VALUES (?, 'AI platform update', 'Summary', '{"impact": "Industry impact"}', 'Impact analysis',
                        'medium', 0.82, '2026-06-05T08:05:00+00:00')
                """,
                (raw_id,),
            )

            brief = generate_brief(db, brief_type="manual")

            self.assertEqual(brief["time_range_start"], "2026-06-05T08:00:00+00:00")
            self.assertEqual(brief["time_range_end"], "2026-06-05T08:00:00+00:00")
            self.assertEqual(from_json(brief["item_ids_json"], []), [processed_id])


class IntelligenceApiContractTest(unittest.TestCase):
    def test_industry_subtag_filter_returns_only_items_with_that_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "agent.sqlite3")
            db.init()
            workflow.ensure_sources(db)
            workflow.run_workflow(db, trigger_type="scheduled_monitor", seed_demo_items=True)

            app = FastAPI()
            app.include_router(create_router(db))
            client = TestClient(app)
            response = client.get("/api/intelligence/items", params={"industry_subtag": "动力电池"})

            self.assertEqual(response.status_code, 200)
            items = response.json()
            self.assertGreater(len(items), 0)
            for item in items:
                self.assertIn("动力电池", item["tags_by_dimension"].get("industry_subtag", []))

    def test_items_default_to_recent_window_and_support_all_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "agent.sqlite3")
            db.init()
            workflow.ensure_sources(db)
            workflow.run_workflow(db, trigger_type="scheduled_monitor", seed_demo_items=True)
            old_row = db.query_one("SELECT id, title FROM raw_items ORDER BY id LIMIT 1")
            old_time = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
            db.execute("UPDATE raw_items SET published_at = ?, fetched_at = ? WHERE id = ?", (old_time, old_time, old_row["id"]))

            app = FastAPI()
            app.include_router(create_router(db))
            client = TestClient(app)

            recent_items = client.get("/api/intelligence/items").json()
            all_items = client.get("/api/intelligence/items", params={"time_range": "all"}).json()

            self.assertNotIn(old_row["title"], [item["title"] for item in recent_items])
            self.assertIn(old_row["title"], [item["title"] for item in all_items])

    def test_items_limit_caps_returned_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "agent.sqlite3")
            db.init()
            workflow.ensure_sources(db)
            workflow.run_workflow(db, trigger_type="scheduled_monitor", seed_demo_items=True)

            app = FastAPI()
            app.include_router(create_router(db))
            client = TestClient(app)
            response = client.get("/api/intelligence/items", params={"time_range": "all", "limit": 2})

            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.json()), 2)

    def test_item_detail_returns_item_scoped_processing_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "agent.sqlite3")
            db.init()
            workflow.ensure_sources(db)
            workflow.run_workflow(db, trigger_type="scheduled_monitor", seed_demo_items=True)

            app = FastAPI()
            app.include_router(create_router(db))
            client = TestClient(app)
            list_response = client.get("/api/intelligence/items", params={"time_range": "all", "limit": 1})
            item_id = list_response.json()[0]["id"]
            detail = client.get(f"/api/intelligence/items/{item_id}").json()

            trace = detail["item_processing_trace"]
            node_names = [node["node"] for node in trace]

            self.assertEqual(node_names, ["collect", "normalize", "deduplicate", "classify_tag", "rank", "extract"])
            for node in trace:
                self.assertIn("input", node)
                self.assertIn("output", node)
                self.assertEqual(node["status"], "success")
            self.assertEqual(trace[1]["output"]["raw_item_id"], detail["raw_item_id"])
            self.assertEqual(trace[2]["output"]["canonical_event_id"], detail["canonical_event_id"])
            self.assertEqual(trace[3]["output"]["tags_by_dimension"], detail["tags_by_dimension"])
            self.assertEqual(trace[4]["output"]["rank_score"], detail["rank_score"])
            self.assertEqual(trace[5]["output"]["key_facts"], detail["key_facts"])
            self.assertNotIn("workflow_trace", detail)


class SeedDataContractTest(unittest.TestCase):
    def test_demo_raw_items_use_complete_original_urls_not_demo_fragments(self) -> None:
        for item in demo_raw_items(datetime(2026, 6, 3, 9, 0, tzinfo=timezone.utc)):
            self.assertRegex(item["url"], r"^https://")
            self.assertNotIn("#industrial-agent-demo", item["url"])


class WorkflowTraceContractTest(unittest.TestCase):
    def test_workflow_trace_records_each_node_input_and_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "agent.sqlite3")
            db.init()
            workflow.ensure_sources(db)
            run = workflow.run_workflow(db, trigger_type="manual_collect", seed_demo_items=True)
            stored = db.query_one("SELECT node_trace_json FROM workflow_runs WHERE id = ?", (run["id"],))
            trace = from_json(stored["node_trace_json"], [])

            node_names = [node["node"] for node in trace]
            self.assertIn("collect", node_names)
            self.assertIn("normalize", node_names)
            self.assertIn("deduplicate", node_names)
            self.assertIn("classify_rank_extract", node_names)
            for node in trace:
                self.assertIn("input", node)
                self.assertIn("output", node)
            normalize = next(node for node in trace if node["node"] == "normalize")
            self.assertIn("raw_items", normalize["output"])
            self.assertIn("url", normalize["output"]["raw_items"][0])

    def test_workflow_trace_keeps_complete_node_payloads_for_future_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "agent.sqlite3")
            db.init()
            workflow.ensure_sources(db)
            run = workflow.run_workflow(db, trigger_type="manual_collect", seed_demo_items=True)
            stored = db.query_one("SELECT node_trace_json FROM workflow_runs WHERE id = ?", (run["id"],))
            trace = from_json(stored["node_trace_json"], [])

            collect = next(node for node in trace if node["node"] == "collect")
            normalize = next(node for node in trace if node["node"] == "normalize")
            deduplicate = next(node for node in trace if node["node"] == "deduplicate")
            classify_extract = next(node for node in trace if node["node"] == "classify_rank_extract")

            self.assertGreater(collect["output"]["candidate_count"], 5)
            self.assertEqual(len(collect["output"]["candidates"]), collect["output"]["candidate_count"])
            self.assertEqual(len(normalize["output"]["raw_items"]), normalize["output"]["raw_item_count"])
            self.assertEqual(len(deduplicate["output"]["clusters"]), deduplicate["output"]["cluster_count"])
            self.assertEqual(
                len(classify_extract["output"]["processed_items"]),
                classify_extract["output"]["processed_count"],
            )


class RealSourceCollectionContractTest(unittest.TestCase):
    def test_source_interval_scan_runs_through_langgraph_orchestrator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "agent.sqlite3")
            db.init()
            workflow.ensure_sources(db)

            def fake_fetcher(source: dict[str, object]) -> list[dict[str, object]]:
                return []

            run = workflow.run_source_interval_scan(
                db,
                now=datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc),
                fetcher=fake_fetcher,
                force_all=True,
            )
            trace = from_json(run["node_trace_json"], [])

            self.assertEqual(run["status"], "success")
            self.assertEqual(trace[0]["input"]["orchestrator"], "langgraph")
            self.assertEqual(trace[0]["output"]["orchestrator"], "langgraph")

            import app.services.workflow_graph as workflow_graph

            self.assertIn("StateGraph", Path(workflow_graph.__file__).read_text(encoding="utf-8"))
            self.assertTrue(callable(workflow_graph.build_source_interval_graph))

    def test_manual_collect_api_starts_incremental_scan_in_background(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "agent.sqlite3")
            db.init()
            workflow.ensure_sources(db)
            app = FastAPI()
            app.include_router(create_router(db))
            scheduled: list[dict[str, object]] = []

            def fake_background_start(db_arg: Database, **kwargs: object) -> dict[str, object]:
                scheduled.append({"db": db_arg, **kwargs})
                run_id = db_arg.execute(
                    """
                    INSERT INTO workflow_runs (trigger_type, started_at, status, node_trace_json)
                    VALUES (?, ?, 'running', '[]')
                    """,
                    (kwargs["trigger_type"], "2026-06-05T09:00:00+00:00"),
                )
                return db_arg.query_one("SELECT * FROM workflow_runs WHERE id = ?", (run_id,)) or {}

            with patch("app.api.start_source_interval_scan_background", side_effect=fake_background_start):
                response = TestClient(app).post("/api/collect/manual")

            payload = response.json()
            run = db.query_one("SELECT * FROM workflow_runs WHERE id = ?", (payload["id"],))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(payload["status"], "running")
            self.assertEqual(payload["trigger_type"], "manual_collect")
            self.assertEqual(run["status"], "running")
            self.assertEqual(len(scheduled), 1)
            self.assertEqual(scheduled[0]["trigger_type"], "manual_collect")
            self.assertTrue(scheduled[0]["force_all"])

    def test_background_source_interval_scan_finishes_incremental_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "agent.sqlite3")
            db.init()
            workflow.ensure_sources(db)
            db.execute(
                "UPDATE sources SET last_fetched_at = ? WHERE id = 1",
                ("2026-06-05T07:30:00+00:00",),
            )
            captured_windows: list[tuple[datetime, datetime]] = []
            now = datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc)

            def fake_fetcher(source: dict[str, object]) -> list[dict[str, object]]:
                if source["id"] == 1:
                    captured_windows.append((source["published_after"], source["published_before"]))
                return []

            run = workflow.start_source_interval_scan_background(
                db,
                trigger_type="manual_collect",
                now=now,
                fetcher=fake_fetcher,
                force_all=True,
            )

            for _ in range(50):
                stored = db.query_one("SELECT * FROM workflow_runs WHERE id = ?", (run["id"],))
                if stored and stored["status"] != "running":
                    break
                time.sleep(0.02)
            else:
                self.fail("background manual collect did not finish")

            self.assertEqual(stored["status"], "success")
            self.assertEqual(
                captured_windows,
                [(datetime(2026, 6, 5, 7, 30, tzinfo=timezone.utc), now)],
            )

    def test_source_interval_scan_collects_from_configured_sources_without_demo_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "agent.sqlite3")
            db.init()
            workflow.ensure_sources(db)

            def fake_fetcher(source: dict[str, object]) -> list[dict[str, object]]:
                return [
                    {
                        "source_id": source["id"],
                        "url": "https://example.com/real-source/article-1",
                        "title": "Real source reports AI chip capacity expansion",
                        "raw_content": "A configured real source reported AI chip and compute infrastructure capacity expansion.",
                        "published_at": "2026-06-05T08:00:00+00:00",
                    }
                ]

            run = workflow.run_source_interval_scan(db, now=datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc), fetcher=fake_fetcher)

            self.assertEqual(run["status"], "success")
            raw = db.query("SELECT * FROM raw_items ORDER BY id")
            self.assertEqual(len(raw), 1)
            self.assertEqual(raw[0]["url"], "https://example.com/real-source/article-1")
            self.assertNotIn("#industrial-agent-demo", raw[0]["url"])
            source = db.query_one("SELECT * FROM sources WHERE id = ?", (raw[0]["source_id"],))
            self.assertIsNotNone(source["last_checked_at"])
            self.assertIsNotNone(source["last_fetched_at"])
            self.assertIsNone(source["last_error"])

    def test_source_interval_scan_records_failures_without_fabricating_raw_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "agent.sqlite3")
            db.init()
            workflow.ensure_sources(db)

            def failing_fetcher(source: dict[str, object]) -> list[dict[str, object]]:
                raise RuntimeError("network unavailable")

            run = workflow.run_source_interval_scan(db, now=datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc), fetcher=failing_fetcher)

            self.assertEqual(run["status"], "success")
            self.assertEqual(db.query_one("SELECT COUNT(*) AS count FROM raw_items")["count"], 0)
            self.assertGreater(run["failed_count"], 0)
            failed_source = db.query_one("SELECT * FROM sources WHERE last_error IS NOT NULL LIMIT 1")
            self.assertIn("network unavailable", failed_source["last_error"])

    def test_source_interval_scan_treats_no_candidates_as_empty_not_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "agent.sqlite3")
            db.init()
            workflow.ensure_sources(db)

            run = workflow.run_source_interval_scan(
                db,
                now=datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc),
                fetcher=lambda source: [],
                force_all=True,
            )
            trace = from_json(run["node_trace_json"], [])
            collect = next(node for node in trace if node["node"] == "collect")

            self.assertEqual(run["status"], "success")
            self.assertEqual(run["failed_count"], 0)
            self.assertEqual(db.query_one("SELECT COUNT(*) AS count FROM raw_items")["count"], 0)
            self.assertTrue(collect["output"]["source_results"])
            self.assertTrue(all(result["status"] == "empty" for result in collect["output"]["source_results"]))
            self.assertIsNone(db.query_one("SELECT * FROM sources WHERE last_error IS NOT NULL LIMIT 1"))

    def test_due_sources_respects_source_fetch_interval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "agent.sqlite3")
            db.init()
            workflow.ensure_sources(db)
            now = datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc)
            db.execute(
                "UPDATE sources SET last_fetched_at = ?, fetch_interval_minutes = 60 WHERE id = 1",
                ((now - timedelta(minutes=30)).isoformat(),),
            )
            db.execute(
                "UPDATE sources SET last_fetched_at = ?, fetch_interval_minutes = 60 WHERE id = 2",
                ((now - timedelta(minutes=61)).isoformat(),),
            )

            due_ids = {source["id"] for source in workflow.due_sources(db, now)}

            self.assertNotIn(1, due_ids)
            self.assertIn(2, due_ids)

    def test_source_interval_scan_passes_first_run_24_hour_window_to_fetcher(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "agent.sqlite3")
            db.init()
            workflow.ensure_sources(db)
            captured_windows: list[tuple[datetime, datetime]] = []

            def fake_fetcher(source: dict[str, object]) -> list[dict[str, object]]:
                if source["id"] == 1:
                    captured_windows.append((source["published_after"], source["published_before"]))
                return []

            workflow.run_source_interval_scan(
                db,
                now=datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc),
                fetcher=fake_fetcher,
                force_all=True,
            )

            self.assertEqual(
                captured_windows,
                [
                    (
                        datetime(2026, 6, 4, 9, 0, tzinfo=timezone.utc),
                        datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc),
                    )
                ],
            )

    def test_source_interval_scan_passes_incremental_window_from_last_successful_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "agent.sqlite3")
            db.init()
            workflow.ensure_sources(db)
            db.execute(
                "UPDATE sources SET last_fetched_at = ? WHERE id = 1",
                ("2026-06-05T07:30:00+00:00",),
            )
            captured_windows: list[tuple[datetime, datetime]] = []

            def fake_fetcher(source: dict[str, object]) -> list[dict[str, object]]:
                if source["id"] == 1:
                    captured_windows.append((source["published_after"], source["published_before"]))
                return []

            workflow.run_source_interval_scan(
                db,
                now=datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc),
                fetcher=fake_fetcher,
                force_all=True,
            )

            self.assertEqual(
                captured_windows,
                [
                    (
                        datetime(2026, 6, 5, 7, 30, tzinfo=timezone.utc),
                        datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc),
                    )
                ],
            )

    def test_scheduler_registers_source_interval_scan_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "agent.sqlite3")
            db.init()

            scheduler = start_scheduler(db, enabled=True)
            try:
                job_ids = {job.id for job in scheduler.get_jobs()}
            finally:
                scheduler.shutdown(wait=False)

            self.assertIn("source_interval_scan", job_ids)

    def test_source_interval_scan_skips_when_another_workflow_is_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "agent.sqlite3")
            db.init()
            workflow.ensure_sources(db)
            db.execute(
                """
                INSERT INTO workflow_runs (trigger_type, started_at, status, node_trace_json)
                VALUES ('scheduled_monitor', '2026-06-05T09:00:00+00:00', 'running', '[]')
                """
            )
            fetch_calls = 0

            def fetcher(source: dict[str, object]) -> list[dict[str, object]]:
                nonlocal fetch_calls
                fetch_calls += 1
                return []

            run = workflow.run_source_interval_scan(db, now=datetime(2026, 6, 5, 9, 5, tzinfo=timezone.utc), fetcher=fetcher)

            self.assertEqual(run["status"], "skipped")
            self.assertEqual(fetch_calls, 0)
            self.assertIn("already running", run["error_summary"])

    def test_startup_recovery_marks_interrupted_running_workflows_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "agent.sqlite3")
            db.init()
            workflow.ensure_sources(db)
            db.execute(
                """
                INSERT INTO workflow_runs (trigger_type, started_at, status, node_trace_json)
                VALUES ('high_authority_monitor', '2026-06-05T09:30:00+00:00', 'running', '[]')
                """
            )

            recovered_count = workflow.recover_interrupted_runs(db, ended_at="2026-06-05T09:45:00+00:00")

            recovered = db.query_one("SELECT * FROM workflow_runs WHERE trigger_type = 'high_authority_monitor'")
            self.assertEqual(recovered_count, 1)
            self.assertEqual(recovered["status"], "failed")
            self.assertEqual(recovered["ended_at"], "2026-06-05T09:45:00+00:00")
            self.assertIn("interrupted", recovered["error_summary"])

            run = workflow.run_source_interval_scan(
                db,
                now=datetime(2026, 6, 5, 10, 0, tzinfo=timezone.utc),
                fetcher=lambda source: [],
                force_all=False,
            )
            self.assertNotEqual(run["status"], "skipped")

    def test_source_interval_scan_does_not_store_irrelevant_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "agent.sqlite3")
            db.init()
            workflow.ensure_sources(db)

            def fake_fetcher(source: dict[str, object]) -> list[dict[str, object]]:
                if source["id"] != 1:
                    return []
                return [
                    {
                        "source_id": source["id"],
                        "url": "https://www.news.cn/politics/20260605/fake-degree.html",
                        "title": "假冒学历认证等网站 公安部网安局发布典型案例",
                        "raw_content": "公安部网安局发布假冒学历认证等网站典型案例，提醒公众防范网络诈骗。",
                        "published_at": "2026-06-05T08:00:00+00:00",
                    },
                    {
                        "source_id": source["id"],
                        "url": "https://example.com/nev/battery-policy",
                        "title": "工信部发布新能源汽车动力电池安全监管政策",
                        "raw_content": "政策要求新能源汽车动力电池企业完善安全追溯体系。",
                        "published_at": "2026-06-05T08:10:00+00:00",
                    },
                ]

            run = workflow.run_source_interval_scan(db, now=datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc), fetcher=fake_fetcher)
            stored = db.query("SELECT title, url FROM raw_items ORDER BY id")
            trace = from_json(run["node_trace_json"], [])
            relevance = next(node for node in trace if node["node"] == "relevance_screen")

            self.assertEqual([row["title"] for row in stored], ["工信部发布新能源汽车动力电池安全监管政策"])
            self.assertEqual(relevance["output"]["accepted_count"], 1)
            self.assertEqual(relevance["output"]["rejected_count"], 1)
            self.assertIn("假冒学历认证", relevance["output"]["rejected_candidates"][0]["title"])

    def test_empty_source_interval_scan_does_not_reprocess_unscoped_existing_raw_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "agent.sqlite3")
            db.init()
            workflow.ensure_sources(db)
            db.execute(
                """
                INSERT INTO raw_items
                    (source_id, url, canonical_url, title, published_at, fetched_at, raw_content,
                     content_excerpt, content_hash, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'new')
                """,
                (
                    1,
                    "https://www.news.cn/politics/20260605/fake-degree.html",
                    "https://www.news.cn/politics/20260605/fake-degree.html",
                    "假冒学历认证等网站 公安部网安局发布典型案例",
                    "2026-06-05T08:00:00+00:00",
                    "2026-06-05T08:05:00+00:00",
                    "公安部网安局发布假冒学历认证等网站典型案例，提醒公众防范网络诈骗。",
                    "公安部网安局发布假冒学历认证等网站典型案例，提醒公众防范网络诈骗。",
                    "legacy-hash",
                ),
            )

            run = workflow.run_source_interval_scan(
                db,
                now=datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc),
                fetcher=lambda source: [],
                force_all=True,
            )
            trace = from_json(run["node_trace_json"], [])
            deduplicate = next(node for node in trace if node["node"] == "deduplicate")

            self.assertEqual(db.query_one("SELECT COUNT(*) AS count FROM processed_items")["count"], 0)
            self.assertEqual(run["deduped_count"], 0)
            self.assertEqual(deduplicate["input"]["raw_item_count"], 0)

    def test_source_interval_scan_uses_configured_llm_for_relevance_gate(self) -> None:
        class FakeSettings:
            provider = "openai"
            model = "test-relevance-model"

        class FakeLLMClient:
            settings = FakeSettings()
            is_configured = True

            def runtime_status(self) -> dict[str, object]:
                return {
                    "configured": True,
                    "mode": "llm",
                    "provider": self.settings.provider,
                    "model": self.settings.model,
                }

            def complete_json(self, system_prompt: str, user_payload: dict[str, object]) -> dict[str, object]:
                self.system_prompt = system_prompt
                self.user_payload = user_payload
                return {
                    "is_relevant": True,
                    "industry": "人工智能",
                    "reason": "LLM 判断该候选属于人工智能产业情报",
                    "confidence": 0.91,
                    "matched_terms": ["模型服务"],
                }

        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "agent.sqlite3")
            db.init()
            workflow.ensure_sources(db)
            fake_client = FakeLLMClient()

            def fake_fetcher(source: dict[str, object]) -> list[dict[str, object]]:
                if source["id"] != 1:
                    return []
                return [
                    {
                        "source_id": source["id"],
                        "url": "https://example.com/llm-only/relevance",
                        "title": "某公司发布新一代模型服务平台",
                        "raw_content": "该候选不依赖本地关键词规则，通过配置的 LLM 相关性节点判断是否进入产业情报工作流。",
                        "published_at": "2026-06-05T08:10:00+00:00",
                    }
                ]

            with patch("app.services.workflow.build_llm_client", return_value=fake_client):
                run = workflow.run_source_interval_scan(db, now=datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc), fetcher=fake_fetcher)

            stored = db.query("SELECT title, url FROM raw_items ORDER BY id")
            trace = from_json(run["node_trace_json"], [])
            relevance = next(node for node in trace if node["node"] == "relevance_screen")
            processed = db.query_one("SELECT id FROM processed_items ORDER BY id")
            industry_tags = db.query(
                "SELECT tag_value FROM item_tags WHERE processed_item_id = ? AND tag_dimension = 'industry'",
                (processed["id"],),
            )

            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0]["url"], "https://example.com/llm-only/relevance")
            self.assertEqual(relevance["output"]["accepted_count"], 1)
            self.assertEqual(relevance["output"]["accepted_candidates"][0]["provider"], "openai")
            self.assertEqual(relevance["output"]["accepted_candidates"][0]["model"], "test-relevance-model")
            self.assertEqual([tag["tag_value"] for tag in industry_tags], ["人工智能"])

    def test_source_interval_scan_persists_llm_extraction_summary_and_trace(self) -> None:
        class FakeSettings:
            provider = "openai"
            model = "test-workflow-extractor-model"

        class FakeLLMClient:
            settings = FakeSettings()
            is_configured = True

            def runtime_status(self) -> dict[str, object]:
                return {"configured": True, "mode": "llm", "provider": self.settings.provider, "model": self.settings.model}

            def complete_json(self, system_prompt: str, user_payload: dict[str, object]) -> dict[str, object]:
                required = user_payload.get("required_json_fields", [])
                if "summary" in required:
                    return {
                        "summary": "LLM 提炼：OpenAI 企业级 Agent 平台升级，重点关注商业化与数据安全能力。",
                        "key_facts": {
                            "who": "OpenAI",
                            "what": "企业级 Agent 平台升级",
                            "when": "2026-06-05T08:10:00+00:00",
                            "where": "公开来源披露",
                            "why": "企业客户需要更强的智能体编排和数据安全能力",
                            "impact": "AI 企业服务竞争进入平台化能力比拼阶段。",
                            "evidence": "OpenAI released an enterprise Agent platform upgrade for orchestration and data security.",
                        },
                        "impact_analysis": "AI 企业服务竞争进入平台化能力比拼阶段。",
                        "source_spans": [
                            {
                                "field": "raw_content",
                                "start": 0,
                                "end": 94,
                                "quote": "OpenAI released an enterprise Agent platform upgrade for orchestration and data security.",
                            }
                        ],
                    }
                if "industry_subtags" in required:
                    return {
                        "industry_subtags": ["智能体/Agent", "企业服务"],
                        "event_types": ["产品发布"],
                        "importance_level": "high",
                        "importance_reason": "OpenAI 发布企业级智能体平台升级，具备商业化跟踪价值。",
                        "subject_roles": ["整车厂/模型厂商"],
                        "signal_attributes": ["商业化进展"],
                        "confidence": 0.92,
                        "entities": [{"text": "OpenAI", "type": "company", "confidence": 0.9}],
                        "key_actor_level": "core",
                    }
                return {
                    "is_relevant": True,
                    "industry": "人工智能",
                    "reason": "LLM 判断该候选属于人工智能产业情报",
                    "confidence": 0.91,
                    "matched_terms": ["OpenAI", "Agent"],
                }

        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "agent.sqlite3")
            db.init()
            workflow.ensure_sources(db)
            fake_client = FakeLLMClient()

            def fake_fetcher(source: dict[str, object]) -> list[dict[str, object]]:
                if source["id"] != 1:
                    return []
                return [
                    {
                        "source_id": source["id"],
                        "url": "https://example.com/openai/agent-platform-upgrade",
                        "title": "OpenAI 发布企业级 Agent 平台升级",
                        "raw_content": "OpenAI released an enterprise Agent platform upgrade for orchestration and data security.",
                        "published_at": "2026-06-05T08:10:00+00:00",
                    }
                ]

            with patch("app.services.workflow.build_llm_client", return_value=fake_client):
                workflow.run_source_interval_scan(db, now=datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc), fetcher=fake_fetcher)

            processed = db.query_one("SELECT * FROM processed_items ORDER BY id")
            self.assertEqual(processed["summary"], "LLM 提炼：OpenAI 企业级 Agent 平台升级，重点关注商业化与数据安全能力。")
            self.assertEqual(from_json(processed["key_facts_json"], {})["who"], "OpenAI")
            self.assertEqual(processed["impact_analysis"], "AI 企业服务竞争进入平台化能力比拼阶段。")
            self.assertEqual(processed["extraction_mode"], "llm")
            self.assertEqual(processed["extraction_provider"], "openai")
            self.assertEqual(processed["extraction_model"], "test-workflow-extractor-model")

            app = FastAPI()
            app.include_router(create_router(db))
            client = TestClient(app)
            detail = client.get(f"/api/intelligence/items/{processed['id']}").json()
            extract_node = next(node for node in detail["item_processing_trace"] if node["node"] == "extract")
            self.assertEqual(extract_node["output"]["extraction_mode"], "llm")
            self.assertEqual(extract_node["output"]["extraction_provider"], "openai")
            self.assertEqual(extract_node["output"]["extraction_model"], "test-workflow-extractor-model")
            self.assertIsNone(extract_node["output"].get("extraction_fallback_reason"))


class SourceConfigContractTest(unittest.TestCase):
    def test_source_upsert_uses_stable_id_when_url_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "agent.sqlite3")
            db.init()
            db.execute(
                """
                INSERT INTO sources (id, name, type, url, industry_hint, reliability_score, enabled, fetch_interval_minutes)
                VALUES (99, '旧来源', 'media', 'https://old.example.com', 'ai', 0.5, 1, 120)
                """
            )

            original_sources = workflow.SOURCE_CONFIGS
            try:
                workflow.SOURCE_CONFIGS = [
                    {
                        "id": 99,
                        "name": "新来源",
                        "type": "media",
                        "industry_hint": "ai",
                        "url": "https://new.example.com/news",
                        "reliability_score": 0.8,
                        "fetch_interval_minutes": 60,
                    }
                ]
                workflow.ensure_sources(db)
            finally:
                workflow.SOURCE_CONFIGS = original_sources

            source = db.query_one("SELECT * FROM sources WHERE id = 99")
            self.assertEqual(source["name"], "新来源")
            self.assertEqual(source["url"], "https://new.example.com/news")
            self.assertEqual(source["fetch_interval_minutes"], 60)


if __name__ == "__main__":
    unittest.main()
