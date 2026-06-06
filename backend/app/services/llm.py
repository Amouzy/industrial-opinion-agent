from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.request import Request, urlopen

from app.config import LLMSettings


@dataclass(frozen=True)
class LLMClient:
    settings: LLMSettings

    @property
    def is_configured(self) -> bool:
        return bool(self.settings.provider and self.settings.model and self.settings.api_key)

    def runtime_status(self) -> dict[str, Any]:
        return {
            "configured": self.is_configured,
            "mode": "llm" if self.is_configured else "rules-fallback",
            "provider": self.settings.provider,
            "model": self.settings.model,
            "base_url": self.settings.base_url,
            "timeout_seconds": self.settings.timeout_seconds,
            "temperature": self.settings.temperature,
        }

    def complete_json(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        if not self.is_configured:
            raise RuntimeError("LLM is not configured; running with rules fallback")
        # OpenAI-compatible providers such as DashScope expose the same
        # chat/completions contract when a compatible base_url is configured.
        url = self.settings.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.settings.model,
            "temperature": self.settings.temperature,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
        }
        request = Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.settings.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(request, timeout=self.settings.timeout_seconds) as response:
            data = json.loads(response.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
        return json.loads(content)


def build_llm_client(settings: LLMSettings) -> LLMClient:
    return LLMClient(settings=settings)
