"""Minimal OpenAI-compatible chat-completions client (standard library only).

Configuration comes from the environment so no credential is ever part of the bundle:
  ARCHFIX_LLM_BASE_URL / ARCHFIX_LLM_API_KEY / ARCHFIX_LLM_MODEL
with fallbacks to BB_LLM_* and OPENAI_* names. Without a base URL and model the Agent
runs with deterministic fixers only.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field


def _env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value.strip()
    return None


@dataclass
class LLMConfig:
    base_url: str
    model: str
    api_key: str | None = None
    timeout: float = 180.0
    max_tokens: int = 4096
    temperature: float = 0.2
    retries: int = 4

    @classmethod
    def from_env(cls) -> "LLMConfig | None":
        base = _env("ARCHFIX_LLM_BASE_URL", "BB_LLM_BASE_URL", "OPENAI_BASE_URL")
        model = _env("ARCHFIX_LLM_MODEL", "BB_LLM_MODEL", "OPENAI_MODEL")
        if not base or not model:
            return None
        cfg = cls(base_url=base.rstrip("/"), model=model,
                  api_key=_env("ARCHFIX_LLM_API_KEY", "BB_LLM_API_KEY", "OPENAI_API_KEY"))
        for attr, name, cast in (("timeout", "ARCHFIX_LLM_TIMEOUT", float),
                                 ("max_tokens", "ARCHFIX_LLM_MAX_TOKENS", int),
                                 ("temperature", "ARCHFIX_LLM_TEMPERATURE", float)):
            value = _env(name)
            if value:
                setattr(cfg, attr, cast(value))
        return cfg


class LLMError(RuntimeError):
    pass


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0

    def add(self, usage: dict | None) -> None:
        self.calls += 1
        if usage:
            self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
            self.completion_tokens += int(usage.get("completion_tokens") or 0)

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class ChatClient:
    config: LLMConfig
    usage: Usage = field(default_factory=Usage)

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        """Return the assistant message dict ({"content", "tool_calls"?})."""
        payload: dict = {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        url = f"{self.config.base_url}/chat/completions"

        last_error: Exception | None = None
        for attempt in range(self.config.retries):
            request = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                    data = json.loads(response.read().decode("utf-8"))
                self.usage.add(data.get("usage"))
                choices = data.get("choices") or []
                if not choices:
                    raise LLMError(f"no choices in response: {str(data)[:300]}")
                return choices[0].get("message") or {}
            except urllib.error.HTTPError as error:
                detail = error.read().decode("utf-8", errors="replace")[:500]
                last_error = LLMError(f"HTTP {error.code}: {detail}")
                if error.code not in (408, 409, 429, 500, 502, 503, 504):
                    break
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
                last_error = error
            time.sleep(min(30, 2 ** attempt * 2))
        raise LLMError(f"chat request failed: {last_error!r}")
