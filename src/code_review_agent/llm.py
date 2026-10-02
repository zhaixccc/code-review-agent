"""DeepSeek chat model (OpenAI-compatible endpoint) and robust JSON extraction."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from .config import Settings


def make_llm(settings: Settings) -> BaseChatModel:
    settings.require_llm()
    return ChatOpenAI(
        model=settings.deepseek_model,
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        temperature=0.2,
        timeout=90,
        max_retries=3,
        # DeepSeek JSON mode: the prompt must contain the word "JSON" (all prompts here do).
        model_kwargs={"response_format": {"type": "json_object"}},
    )


def message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
    return str(content)


def extract_json(content: Any) -> dict[str, Any] | None:
    """Parse the first JSON object in the model output, tolerating code fences and stray text."""
    text = message_text(content).strip()
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None
