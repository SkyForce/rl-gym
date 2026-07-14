"""Minimal OpenAI-compatible client for Nebius Token Factory (serverless, pay-per-token).

Token Factory hosts 60+ open models (Qwen3-235B, DeepSeek-V4-Pro, Qwen3-Next-Thinking, GPT-OSS) and
your own uploaded fine-tunes behind an OpenAI-compatible /v1/chat/completions endpoint.
This is the ONE external call in the platform — used offline to draft verifier rules with
a big model (rl_gym.gym.rulegen), and optionally to serve our fine-tuned 8B per-token
instead of self-hosting. Stdlib only (urllib), so no dependency is added.

Config via env:
  TOKEN_FACTORY_API_KEY   your key
  TOKEN_FACTORY_BASE_URL  default https://api.tokenfactory.nebius.com/v1
"""
from __future__ import annotations

import json
import os
import ssl
import urllib.request

DEFAULT_BASE = os.environ.get("TOKEN_FACTORY_BASE_URL", "https://api.studio.nebius.com/v1")


def _ssl_ctx():
    """Verified TLS context. Prefer certifi's CA bundle (robust where the OS/Python bundle
    is missing, e.g. framework Python on macOS); fall back to the system default."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


class TokenFactory:
    def __init__(self, api_key: str | None = None, base_url: str = DEFAULT_BASE):
        self.api_key = api_key or os.environ.get("TOKEN_FACTORY_API_KEY", "")
        self.base_url = base_url.rstrip("/")

    def available(self) -> bool:
        return bool(self.api_key)

    def _post(self, model: str, messages: list[dict], temperature: float,
              max_tokens: int, timeout: int) -> dict:
        if not self.api_key:
            raise RuntimeError("TOKEN_FACTORY_API_KEY not set")
        body = json.dumps({"model": model, "messages": messages,
                           "temperature": temperature, "max_tokens": max_tokens}).encode()
        req = urllib.request.Request(
            self.base_url + "/chat/completions", data=body,
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as r:
            return json.loads(r.read().decode())

    def chat(self, model: str, messages: list[dict], temperature: float = 0.2,
             max_tokens: int = 2048, timeout: int = 120) -> str:
        """One chat completion → assistant text. Raises on transport/HTTP error."""
        data = self._post(model, messages, temperature, max_tokens, timeout)
        return data["choices"][0]["message"]["content"]

    def chat_usage(self, model: str, messages: list[dict], temperature: float = 0.2,
                   max_tokens: int = 2048, timeout: int = 120) -> tuple[str, dict]:
        """Like chat() but also returns the OpenAI-style usage dict
        ({prompt_tokens, completion_tokens, ...}) so callers can price the call.
        Usage is {} if the endpoint omits it."""
        data = self._post(model, messages, temperature, max_tokens, timeout)
        return data["choices"][0]["message"]["content"], (data.get("usage") or {})
