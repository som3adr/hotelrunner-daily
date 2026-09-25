"""Small Gemini REST client with configurable model and 404 fallback."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


DEFAULT_MODELS = ("gemini-2.5-flash", "gemini-2.0-flash")


def configured_models() -> list[str]:
    configured = os.environ.get("GEMINI_MODEL", "").strip()
    models = [configured] if configured else []
    for model in DEFAULT_MODELS:
        if model not in models:
            models.append(model)
    return models


def generate_content(
    api_key: str,
    prompt: str,
    *,
    models: list[str] | None = None,
    max_output_tokens: int = 512,
) -> str:
    candidates = models or configured_models()
    last_error: Exception | None = None
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": max_output_tokens},
    }).encode("utf-8")

    for model in candidates:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.loads(response.read())
            parts = result.get("candidates", [{}])[0].get("content", {}).get("parts", [])
            text = " ".join(part.get("text", "") for part in parts).strip()
            if text:
                return text
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 404:
                continue
            raise RuntimeError(f"Gemini request failed with HTTP {exc.code}") from exc
        except Exception as exc:
            last_error = exc
            break
    if isinstance(last_error, urllib.error.HTTPError) and last_error.code == 404:
        raise RuntimeError(
            "No configured Gemini model is available. Set the GEMINI_MODEL GitHub secret to a model enabled for this API key."
        ) from last_error
    raise RuntimeError(f"Gemini request failed: {last_error or 'empty response'}")
