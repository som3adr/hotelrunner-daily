"""Minimal OpenAI-compatible client for CodeCraft API."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request


BASE_URL = "https://codecraftapi.com/v1"
TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}


def available_models(api_key: str) -> list[dict]:
    request = urllib.request.Request(
        f"{BASE_URL}/models",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            payload = json.loads(exc.read())
            error = payload.get("error") or {}
            detail = str(error.get("code") or error.get("message") or "")
        except Exception:
            pass
        suffix = f" ({detail})" if detail else ""
        raise RuntimeError(f"CodeCraft model access failed with HTTP {exc.code}{suffix}") from exc
    return [item for item in result.get("data", []) if item.get("id")]


def candidate_models(api_key: str) -> list[str]:
    configured = os.environ.get("CODECRAFT_MODEL", "").strip()
    models = available_models(api_key)
    chat_models = [item for item in models if item.get("type", "chat") == "chat"]

    def price(item: dict) -> float:
        pricing = item.get("pricing") or {}
        try:
            return float(pricing.get("input_per_1k") or 0) + float(pricing.get("output_per_1k") or 0)
        except (TypeError, ValueError):
            return 0.0

    ordered = sorted(chat_models or models, key=price)
    ids = [str(item["id"]) for item in ordered]
    if configured:
        ids = [configured] + [model for model in ids if model != configured]
    return ids[:5]


def _request_content(api_key: str, model: str, prompt: str, max_tokens: int) -> str:
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        result = json.loads(response.read())
    return str(result.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()


def generate_content(api_key: str, prompt: str, *, max_output_tokens: int = 512) -> str:
    models = candidate_models(api_key)
    if not models:
        raise RuntimeError("CodeCraft returned no available chat models.")

    last_error: Exception | None = None
    for model in models:
        for attempt in range(3):
            try:
                text = _request_content(api_key, model, prompt, max_output_tokens)
                if text:
                    print(f"[codecraft] Answered with model {model}")
                    return text
                last_error = RuntimeError(f"CodeCraft model {model} returned an empty answer")
                break
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code in {401, 403}:
                    raise RuntimeError(f"CodeCraft authentication failed with HTTP {exc.code}") from exc
                if exc.code in TRANSIENT_HTTP_CODES and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                break
            except Exception as exc:
                last_error = exc
                break
    raise RuntimeError(f"CodeCraft request failed: {last_error or 'no model answered'}")
