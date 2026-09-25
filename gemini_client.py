"""Small Gemini REST client with configurable and API-discovered fallbacks."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request


DEFAULT_MODELS = ("gemini-3.8-flash", "gemini-3.5-flash", "gemini-2.5-flash")
TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}


def configured_models() -> list[str]:
    configured = os.environ.get("GEMINI_MODEL", "").strip()
    models = [configured] if configured else []
    for model in DEFAULT_MODELS:
        if model not in models:
            models.append(model)
    return models


def available_generate_models(api_key: str) -> list[str]:
    """Return models this API key can use with generateContent."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            result = json.loads(response.read())
    except Exception as exc:
        raise RuntimeError(f"Could not list Gemini models: {exc}") from exc

    models = []
    for item in result.get("models", []):
        methods = item.get("supportedGenerationMethods", [])
        name = str(item.get("name") or "").removeprefix("models/")
        if name and "generateContent" in methods:
            models.append(name)
    return models


def _request_content(api_key: str, model: str, payload: bytes, attempts: int = 3) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.loads(response.read())
            break
        except urllib.error.HTTPError as exc:
            if exc.code not in TRANSIENT_HTTP_CODES or attempt == attempts - 1:
                raise
            time.sleep(2 ** attempt)
    parts = result.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    return " ".join(part.get("text", "") for part in parts).strip()


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

    attempted: list[str] = []
    for model in candidates:
        attempted.append(model)
        try:
            text = _request_content(api_key, model, payload)
            if text:
                return text
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 404 or exc.code in TRANSIENT_HTTP_CODES:
                continue
            raise RuntimeError(f"Gemini request failed with HTTP {exc.code}") from exc
        except Exception as exc:
            last_error = exc
            break


    if isinstance(last_error, urllib.error.HTTPError) and (
        last_error.code == 404 or last_error.code in TRANSIENT_HTTP_CODES
    ):
        discovered = available_generate_models(api_key)
        preferred = [
            model for model in discovered
            if model not in attempted and any(kind in model for kind in ("flash", "pro"))
        ]
        for model in preferred:
            try:
                text = _request_content(api_key, model, payload)
                if text:
                    return text
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code in {400, 404} or exc.code in TRANSIENT_HTTP_CODES:
                    continue
                raise RuntimeError(f"Gemini request failed with HTTP {exc.code}") from exc
            except Exception as exc:
                last_error = exc
                break
    if isinstance(last_error, urllib.error.HTTPError) and last_error.code == 404:
        raise RuntimeError(
            "No Gemini text model available to this API key could answer the request."
        ) from last_error
    raise RuntimeError(f"Gemini request failed: {last_error or 'empty response'}")
