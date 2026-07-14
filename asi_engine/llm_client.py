"""Shared LLM client for the 3-layer research loop.

Single source of truth for LLM configuration across Karpathy / ASI-Evolve /
SIA layers. All layers MUST import from here — no direct `os.environ.get` of
LLM credentials inside layer code.

Design choices
--------------
* The only supported backend is **ZAI (Zhipu / GLM)** at
  ``https://api.z.ai/api/paas/v4/``. ZAI exposes an OpenAI-compatible
  Chat Completions API, so we use the ``openai`` SDK as a thin HTTP client.
  The SDK is NOT used to talk to OpenAI itself.
* Credentials are read from ``ZAI_API_KEY`` / ``ZAI_BASE_URL`` env vars.
  No ``OPENAI_*`` env vars are read anywhere in this codebase.
* glm-4.5-flash is a reasoning model — chain-of-thought goes into
  ``message.reasoning_content`` and the final answer into ``message.content``.
  Callers should extract ``.content`` (the helper does this for them).
* Each call is wrapped in a try/except returning ``None`` — the layers
  gracefully fall back to their deterministic mutation ladders when the
  LLM is unavailable.

Env vars
--------
``ZAI_API_KEY``    — Required. Format: ``<id>.<secret>`` (classic ZhipuAI).
``ZAI_BASE_URL``   — Optional. Default: ``https://api.z.ai/api/paas/v4/``.
``LLM_MODEL``      — Optional. Default: ``glm-4.5-flash``.
                     Per-layer overrides: ``KARPATHY_LLM_MODEL``,
                     ``ASI_LLM_MODEL``, ``SIA_LLM_MODEL``.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("LLM_CLIENT")

DEFAULT_BASE_URL = "https://api.z.ai/api/paas/v4/"
DEFAULT_MODEL = "glm-4.5-flash"


class LLMConfig:
    """Resolved LLM configuration (read from env at construction time)."""

    def __init__(self, *, layer: str = ""):
        self.api_key = os.environ.get("ZAI_API_KEY", "").strip()
        self.base_url = os.environ.get("ZAI_BASE_URL", DEFAULT_BASE_URL).strip()
        # Per-layer override falls back to generic LLM_MODEL, then DEFAULT_MODEL.
        layer_var = f"{layer.upper()}_LLM_MODEL" if layer else ""
        if layer_var and os.environ.get(layer_var):
            self.model = os.environ[layer_var].strip()
        elif os.environ.get("LLM_MODEL"):
            self.model = os.environ["LLM_MODEL"].strip()
        else:
            self.model = DEFAULT_MODEL

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)


def get_client(
    cfg: LLMConfig | None = None, *, layer: str = ""
) -> tuple[Any, LLMConfig] | None:
    """Build and return (openai client, config), or None if not configured.

    Returns None when:
      * ZAI_API_KEY is not set
      * the ``openai`` SDK is not installed

    The caller MUST treat None as "no LLM available — use fallback".
    """
    cfg = cfg or LLMConfig(layer=layer)
    if not cfg.is_configured:
        return None
    try:
        # The openai SDK is used purely as an HTTP client for ZAI's
        # OpenAI-compatible endpoint. We do NOT send traffic to OpenAI.
        from openai import OpenAI  # type: ignore
    except ImportError:
        logger.warning(
            "openai SDK not installed — LLM hook disabled. "
            "Install with: pip install openai"
        )
        return None

    client = OpenAI(
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        max_retries=3,
        timeout=120.0,
    )
    return client, cfg


def chat_json(
    prompt: str,
    *,
    layer: str = "",
    temperature: float = 0.7,
    max_tokens: int | None = 1024,
    response_format: dict | None = None,
    system: str | None = None,
) -> str | None:
    """Send a chat completion request and return the assistant's content string.

    Returns None on any failure (no API key, network error, parse error,
    empty content). Callers should fall back to their deterministic path.
    """
    result = get_client(layer=layer)
    if result is None:
        return None
    client, cfg = result

    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    kwargs: dict[str, Any] = {
        "model": cfg.model,
        "messages": messages,
        "temperature": temperature,
    }
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if response_format is not None:
        kwargs["response_format"] = response_format

    try:
        resp = client.chat.completions.create(**kwargs)
    except Exception as exc:
        # Broad catch is intentional: the LLM hook is OPTIONAL and any
        # failure (network, auth, rate-limit, malformed response, timeout)
        # must let the layer fall back to its deterministic mutation
        # ladder rather than crashing the whole research loop.
        logger.warning("[%s] LLM call failed: %s", layer or "LLM", exc)
        return None

    try:
        content = resp.choices[0].message.content or ""
    except (AttributeError, IndexError):
        logger.warning("[%s] LLM returned no choices", layer or "LLM")
        return None

    if not content.strip():
        # Common with reasoning models when max_tokens is too small — all
        # budget was consumed by reasoning_content and content is empty.
        finish = getattr(resp.choices[0], "finish_reason", "?") if resp.choices else "?"
        logger.warning(
            "[%s] LLM returned empty content (finish_reason=%s) — "
            "consider increasing max_tokens",
            layer or "LLM",
            finish,
        )
        return None

    return content
