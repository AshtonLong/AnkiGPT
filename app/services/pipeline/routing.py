"""Role-based model routing.

The pipeline asks for a *role* (planner, worker, critic, ...) and this client resolves
the model, reasoning effort and request settings for it. It is deliberately free of
Flask/DB state so it can be used from worker threads.
"""

import logging
from dataclasses import dataclass, field

from .. import llm as llm_module

logger = logging.getLogger(__name__)

ROLES = ("mapper", "planner", "worker", "critic", "reconcile", "vision")


@dataclass
class ChatResult:
    content: str
    message: dict
    usage: dict
    model: str
    finish_reason: str = ""
    cached: bool = False
    raw: dict = field(default_factory=dict)

    @property
    def input_tokens(self):
        return int(self.usage.get("prompt_tokens") or 0)

    @property
    def output_tokens(self):
        return int(self.usage.get("completion_tokens") or 0)

    @property
    def cost(self):
        value = self.usage.get("cost")
        if value is None:
            value = self.usage.get("total_cost")
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0


class LLMClient:
    """Resolves roles to models and performs calls. Built once per generation run."""

    def __init__(self, config):
        get = config.get if hasattr(config, "get") else (lambda k, d=None: getattr(config, k, d))
        self.api_key = get("OPENROUTER_API_KEY", "")
        self.site_url = get("OPENROUTER_SITE_URL", "")
        self.app_name = get("OPENROUTER_APP_NAME", "AnkiGPT")
        self.default_model = get("OPENROUTER_MODEL", "openai/gpt-5.6-luna")
        self.embedding_model = get("OPENROUTER_EMBEDDING_MODEL", "openai/text-embedding-3-small")
        self.max_retries = int(get("OPENROUTER_MAX_RETRIES", 2))
        self.backoff_seconds = float(get("OPENROUTER_RETRY_BACKOFF_SECONDS", 1.5))
        self.timeout_seconds = float(get("OPENROUTER_TIMEOUT_SECONDS", 180))
        self.max_tokens = int(get("OPENROUTER_MAX_TOKENS", 16000))
        raw_temp = get("OPENROUTER_TEMPERATURE", "")
        self.temperature = None
        if raw_temp not in ("", None):
            try:
                self.temperature = float(raw_temp)
            except (TypeError, ValueError):
                self.temperature = None
        self.models = {}
        self.reasoning = {}
        for role in ROLES:
            self.models[role] = get(f"OPENROUTER_MODEL_{role.upper()}", "") or self.default_model
            self.reasoning[role] = (get(f"OPENROUTER_REASONING_{role.upper()}", "") or "").strip() or None

    def model_for(self, role):
        return self.models.get(role, self.default_model)

    def reasoning_for(self, role):
        return self.reasoning.get(role)

    def chat(
        self,
        role,
        messages,
        response_format=None,
        tools=None,
        tool_choice=None,
        max_tokens=None,
        reasoning_effort=None,
        model=None,
    ):
        model = model or self.model_for(role)
        effort = reasoning_effort if reasoning_effort is not None else self.reasoning_for(role)
        # Resolved at call time on purpose: tests monkeypatch llm.openrouter_chat.
        response = llm_module.openrouter_chat(
            messages,
            model,
            self.api_key,
            self.site_url,
            self.app_name,
            temperature=self.temperature,
            max_retries=self.max_retries,
            backoff_seconds=self.backoff_seconds,
            timeout_seconds=self.timeout_seconds,
            response_format=response_format,
            max_tokens=max_tokens or self.max_tokens,
            tools=tools,
            tool_choice=tool_choice,
            reasoning_effort=effort or None,
        )
        message = llm_module.assistant_message(response)
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return ChatResult(
            content=content or "",
            message=message,
            usage=llm_module.usage_of(response),
            model=model,
            finish_reason=llm_module.finish_reason(response),
            raw=response,
        )

    def embed(self, texts):
        return llm_module.openrouter_embeddings(
            texts,
            self.embedding_model,
            self.api_key,
            self.site_url,
            self.app_name,
            max_retries=self.max_retries,
            backoff_seconds=self.backoff_seconds,
            timeout_seconds=self.timeout_seconds,
        )

    def repair_json(self, raw_text, role="worker", schema_hint=None):
        return llm_module.repair_json(
            raw_text,
            self.model_for(role),
            self.api_key,
            self.site_url,
            self.app_name,
            max_retries=self.max_retries,
            backoff_seconds=self.backoff_seconds,
            timeout_seconds=self.timeout_seconds,
            schema_hint=schema_hint,
        )
