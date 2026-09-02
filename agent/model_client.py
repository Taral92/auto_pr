"""Model calls, with record/replay.

Why this exists: every pipeline change - grounding, anchoring, budgets, parse
repair - used to cost a live API call to test. Record one real run, then
replay it for free, deterministically, forever.

Cassettes are keyed by call ORDER, not by request hash. Request hashing looks
tidier but breaks the moment you change the prompt, which is exactly when you
most want the old cassette to still replay.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from anthropic import APIConnectionError, APIStatusError, Anthropic

from config import ROOT, get_settings
from core.errors import PermanentError, TransientError

CASSETTE_DIR = ROOT / "evals" / "cassettes"
TRANSIENT_HTTP = {408, 409, 429, 500, 502, 503, 504, 529}


class ReplayExhausted(PermanentError):
    """The cassette ran out of responses before the graph finished."""


class _Usage:
    def __init__(self, d: dict) -> None:
        self.input_tokens = d.get("input_tokens", 0)
        self.output_tokens = d.get("output_tokens", 0)
        self.cache_creation_input_tokens = d.get("cache_creation_input_tokens", 0)
        self.cache_read_input_tokens = d.get("cache_read_input_tokens", 0)


class _Block:
    """Minimal stand-in for an SDK content block."""

    def __init__(self, d: dict) -> None:
        self._d = d
        self.type = d.get("type")
        self.text = d.get("text", "")
        self.name = d.get("name")
        self.id = d.get("id")
        self.input = d.get("input")

    def model_dump(self, **_: Any) -> dict:
        return {k: v for k, v in self._d.items() if v is not None}


class _Response:
    def __init__(self, d: dict) -> None:
        self.stop_reason = d.get("stop_reason")
        self.usage = _Usage(d.get("usage") or {})
        self.content = [_Block(b) for b in (d.get("content") or [])]


def _serialise(resp: Any) -> dict:
    return {
        "stop_reason": resp.stop_reason,
        "usage": {
            "input_tokens": resp.usage.input_tokens,
            "output_tokens": resp.usage.output_tokens,
            "cache_creation_input_tokens": getattr(
                resp.usage, "cache_creation_input_tokens", 0
            ) or 0,
            "cache_read_input_tokens": getattr(
                resp.usage, "cache_read_input_tokens", 0
            ) or 0,
        },
        "content": [b.model_dump(exclude_none=True) for b in resp.content],
    }


def cassette_path(name: str) -> Path:
    return CASSETTE_DIR / f"{name}.json"


class ModelClient:
    """One instance per run. Holds the cassette cursor."""

    def __init__(self, mode: str | None = None, cassette: str | None = None) -> None:
        s = get_settings()
        self.mode = mode or s.model_mode
        self.name = cassette or s.cassette
        self._recorded: list[dict] = []
        self._replay: list[dict] = []
        self._cursor = 0
        if self.mode == "replay":
            path = cassette_path(self.name)
            if not path.exists():
                raise PermanentError(f"cassette not found: {path}")
            self._replay = json.loads(path.read_text())["calls"]

    # -- public ----------------------------------------------------------
    def call(self, *, system: list | str, messages: list, tools: list | None = None):
        if self.mode == "replay":
            return self._from_cassette()
        resp = self._live(system=system, messages=messages, tools=tools)
        if self.mode == "record":
            self._recorded.append(_serialise(resp))
        return resp

    def save(self, meta: dict | None = None) -> Path | None:
        if self.mode != "record" or not self.name:
            return None
        CASSETTE_DIR.mkdir(parents=True, exist_ok=True)
        path = cassette_path(self.name)
        path.write_text(
            json.dumps({"meta": meta or {}, "calls": self._recorded}, indent=2)
        )
        return path

    # -- internals -------------------------------------------------------
    def _from_cassette(self) -> _Response:
        if self._cursor >= len(self._replay):
            raise ReplayExhausted(
                f"cassette '{self.name}' has {len(self._replay)} calls; "
                f"the graph asked for {self._cursor + 1}. Re-record it."
            )
        d = self._replay[self._cursor]
        self._cursor += 1
        return _Response(d)

    def _live(self, *, system, messages, tools):
        s = get_settings()
        kwargs: dict[str, Any] = dict(
            model=s.model,
            max_tokens=s.max_tokens,
            system=system,
            messages=messages,
        )
        if tools is not None:
            kwargs["tools"] = tools
        client = Anthropic(api_key=s.anthropic_api_key.get_secret_value())
        try:
            return client.messages.create(**kwargs)
        except APIConnectionError as e:
            raise TransientError(str(e)) from e
        except APIStatusError as e:
            if e.status_code in TRANSIENT_HTTP:
                raise TransientError(str(e), code=e.status_code) from e
            raise PermanentError(str(e), code=e.status_code) from e
