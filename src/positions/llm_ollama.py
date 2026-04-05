"""Optional local LLM via Ollama's OpenAI-compatible chat endpoint (e.g. Gemma 4).

Set OLLAMA_API_KEY to any non-empty value (commonly `ollama`) to append this provider
after cloud APIs in AIAdvisor and NewsAI. Local Gemma runs only when Groq/Gemini/
OpenRouter/Anthropic are missing keys or all return errors. Ollama ignores the token;
it only gates whether we attempt local calls.

See: https://github.com/ollama/ollama/blob/main/docs/openai.md
"""
from __future__ import annotations

import os


def ollama_openai_provider_config() -> dict | None:
    """Return provider dict for OpenAI-style chat, or None if local LLM is disabled."""
    if not (os.environ.get("OLLAMA_API_KEY", "") or "").strip():
        return None
    return {
        "name": "ollama",
        "env_var": "OLLAMA_API_KEY",
        "base_url": os.environ.get(
            "OLLAMA_OPENAI_URL",
            "http://127.0.0.1:11434/v1/chat/completions",
        ),
        "model": os.environ.get("OLLAMA_CHAT_MODEL", os.environ.get("OLLAMA_MODEL", "gemma4")),
        "style": "openai",
    }


def with_ollama_last(providers: list[dict]) -> list[dict]:
    """Append Ollama provider when enabled — backup after cloud chain exhausts."""
    o = ollama_openai_provider_config()
    if not o:
        return list(providers)
    return [*list(providers), o]
