import json
import sys
from pathlib import Path
from urllib.error import HTTPError


def test_morning_delivery_guard_marks_only_current_day(tmp_path):
    from morning_delivery import mark_sent, was_sent

    state = tmp_path / "morning.json"
    assert not was_sent(state, "2026-09-25")
    mark_sent(state, "2026-09-25")
    assert was_sent(state, "2026-09-25")
    assert not was_sent(state, "2026-09-26")


def test_gemini_discovers_model_available_to_key(monkeypatch):
    import gemini_client

    calls = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(self.payload).encode()

    def fake_open(request, timeout=30):
        url = request.full_url if hasattr(request, "full_url") else request
        calls.append(url)
        if ":generateContent" in url and "missing-model" in url:
            raise HTTPError(url, 404, "Not Found", {}, None)
        if url.endswith("/v1beta/models?key=key"):
            return Response({
                "models": [
                    {"name": "models/embedding-only", "supportedGenerationMethods": ["embedContent"]},
                    {"name": "models/gemini-key-flash", "supportedGenerationMethods": ["generateContent"]},
                ]
            })
        return Response({"candidates": [{"content": {"parts": [{"text": "Working"}]}}]})

    monkeypatch.setattr(gemini_client.urllib.request, "urlopen", fake_open)

    assert gemini_client.generate_content("key", "prompt", models=["missing-model"]) == "Working"
    assert any("/v1beta/models?key=key" in call for call in calls)
    assert any("gemini-key-flash:generateContent" in call for call in calls)


def test_gemini_defaults_do_not_include_shutdown_model():
    import gemini_client

    assert "gemini-2.0-flash" not in gemini_client.DEFAULT_MODELS


def test_gemini_retries_temporary_503(monkeypatch):
    import gemini_client

    calls = 0

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"candidates": [{"content": {"parts": [{"text": "Recovered"}]}}]}).encode()

    def fake_open(request, timeout=30):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise HTTPError(request.full_url, 503, "Unavailable", {}, None)
        return Response()

    monkeypatch.setattr(gemini_client.urllib.request, "urlopen", fake_open)
    monkeypatch.setattr(gemini_client.time, "sleep", lambda seconds: None)

    assert gemini_client.generate_content("key", "prompt", models=["working-model"]) == "Recovered"
    assert calls == 3


def test_gemini_tries_next_model_after_repeated_503(monkeypatch):
    import gemini_client

    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"candidates": [{"content": {"parts": [{"text": "Fallback worked"}]}}]}).encode()

    def fake_open(request, timeout=30):
        calls.append(request.full_url)
        if "overloaded-model" in request.full_url:
            raise HTTPError(request.full_url, 503, "Unavailable", {}, None)
        return Response()

    monkeypatch.setattr(gemini_client.urllib.request, "urlopen", fake_open)
    monkeypatch.setattr(gemini_client.time, "sleep", lambda seconds: None)

    answer = gemini_client.generate_content(
        "key", "prompt", models=["overloaded-model", "fallback-model"]
    )

    assert answer == "Fallback worked"
    assert sum("overloaded-model" in call for call in calls) == 3
    assert any("fallback-model" in call for call in calls)


def test_telegram_status_replies_without_gemini(tmp_path, monkeypatch):
    import telegram_bot

    replies = []
    cache = tmp_path / "reservations_cache.json"
    cache.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(telegram_bot, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(
        telegram_bot,
        "_telegram_get_updates",
        lambda token, offset: [{"update_id": 4, "message": {"chat": {"id": 123}, "text": "/status"}}],
    )
    monkeypatch.setattr(
        telegram_bot,
        "_telegram_reply",
        lambda token, chat_id, text: replies.append((chat_id, text)) or True,
    )
    monkeypatch.setattr(telegram_bot, "_ask_ai", lambda *args: (_ for _ in ()).throw(AssertionError("AI called")))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.setenv("CODECRAFT_API_KEY", "key")
    monkeypatch.setattr(sys, "argv", ["telegram_bot.py", "--poll", "--cache-file", str(cache)])

    telegram_bot.main()

    assert replies[0][0] == 123
    assert "Q&A is running" in replies[0][1]
    assert "Reservation cache: ready" in replies[0][1]


def test_codecraft_lists_models_and_generates_answer(monkeypatch):
    import codecraft_client

    calls = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(self.payload).encode()

    def fake_open(request, timeout=45):
        calls.append(request.full_url)
        if request.full_url.endswith("/models"):
            return Response({"data": [{"id": "fast-chat", "type": "chat", "pricing": {"input_per_1k": 0.1, "output_per_1k": 0.1}}]})
        return Response({"choices": [{"message": {"content": "CodeCraft works"}}]})

    monkeypatch.setattr(codecraft_client.urllib.request, "urlopen", fake_open)

    assert codecraft_client.generate_content("cc_test", "hello") == "CodeCraft works"
    assert calls == [
        "https://codecraftapi.com/v1/models",
        "https://codecraftapi.com/v1/chat/completions",
    ]


def test_ai_uses_codecraft(monkeypatch):
    import codecraft_client
    import telegram_bot

    monkeypatch.setattr(codecraft_client, "generate_content", lambda *args, **kwargs: "CodeCraft answer")

    answer = telegram_bot._ask_ai("cc_key", "question", "context", "12:00")

    assert answer == "CodeCraft answer"


def test_workflows_separate_operations_from_qa_and_use_morocco_time():
    daily = Path(".github/workflows/daily_summary.yml").read_text(encoding="utf-8")
    qa = Path(".github/workflows/telegram_qa.yml").read_text(encoding="utf-8")

    assert 'cron: "7 7 * * *"' in daily
    assert 'cron: "22 7 * * *"' in daily
    assert daily.count('timezone: "Africa/Casablanca"') == 3
    assert "github.event.schedule" in daily
    assert "morning_delivery.py check" in daily
    assert "morning_delivery.py mark" in daily
    assert "Telegram Q&A Bot" not in daily
    assert "telegram_bot.py --poll" in qa
    assert "Refresh missing reservation cache" in qa
    assert "Save compatible reservation cache" in qa
    assert 'timezone: "Africa/Casablanca"' in qa
    assert "telegram-qa-state-" in qa
    assert "GEMINI_API_KEY" not in daily + qa
    assert "CodeCraft Operations Monitor" in daily
    assert daily.index("Deploy to GitHub Pages") < daily.index("CodeCraft Operations Monitor")


def test_operations_monitor_does_not_turn_provider_failure_into_alert(monkeypatch):
    import operations_monitor

    monkeypatch.setattr(
        "codecraft_client.generate_content",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("timed out")),
    )

    assert operations_monitor._call_codecraft("key", "briefing", "12:00") is None
