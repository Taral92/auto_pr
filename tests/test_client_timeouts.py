"""Provider clients must use the outer retry policy and bounded I/O."""

import sys
import types

import config
from agent import model_client as M


def _settings(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    config.get_settings.cache_clear()


def test_anthropic_client_has_an_explicit_timeout(monkeypatch):
    _settings(monkeypatch)
    created = {}

    class Client:
        def __init__(self, **kwargs):
            created.update(kwargs)
            self.messages = types.SimpleNamespace(create=lambda **_: object())

    monkeypatch.setattr(M, "Anthropic", Client)
    M.ModelClient()._anthropic(system="s", messages=[], tools=None)
    assert created["timeout"] == 120


def test_openai_client_disables_sdk_retries_and_has_a_timeout(monkeypatch):
    _settings(monkeypatch)
    created = {}

    class Client:
        def __init__(self, **kwargs):
            created.update(kwargs)
            self.responses = types.SimpleNamespace(
                create=lambda **_: types.SimpleNamespace(
                    output=[], usage=types.SimpleNamespace(
                        input_tokens=0, output_tokens=0
                    ), status="completed", incomplete_details=None,
                )
            )

    fake_openai = types.SimpleNamespace(
        APIConnectionError=Exception, APIStatusError=Exception, OpenAI=Client
    )
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    M.ModelClient()._openai(system="s", messages=[], tools=None)
    assert created["timeout"] == 120
    assert created["max_retries"] == 0
