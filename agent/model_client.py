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
        # Reasoning tokens are billed as output and are invisible in the
        # response body. Without this a tokens_out blowout has no explanation.
        self.reasoning_tokens = d.get("reasoning_tokens", 0)


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
            "reasoning_tokens": getattr(resp.usage, "reasoning_tokens", 0) or 0,
        },
        "content": [b.model_dump(exclude_none=True) for b in resp.content],
    }


def cassette_path(name: str) -> Path:
    return CASSETTE_DIR / f"{name}.json"


def inline_defs(schema: dict) -> dict:
    """Resolve every $ref/$defs so a schema survives OpenAI strict mode.

    Strict structured outputs reject `$defs` and `$ref` outright, and pydantic
    emits both the moment one model references another - `ReviewFindings` has
    a `list[Finding]`, so our completion schema is unusable as generated.

    Recursion is bounded by the fact that the findings schema is a tree; a
    self-referential model would loop, so a seen-set guards the edge.
    """
    defs = schema.get("$defs") or {}

    def resolve(node: Any, seen: frozenset[str]) -> Any:
        if isinstance(node, list):
            return [resolve(item, seen) for item in node]
        if not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            name = ref.split("/")[-1]
            if name in seen or name not in defs:
                return {"type": "object"}      # cycle: stop, stay valid
            merged = {**defs[name], **{k: v for k, v in node.items() if k != "$ref"}}
            return resolve(merged, seen | {name})
        return {k: resolve(v, seen) for k, v in node.items() if k != "$defs"}

    return resolve({k: v for k, v in schema.items() if k != "$defs"}, frozenset())


def usage_of(resp: Any) -> dict[str, int]:
    """The four numbers a turn costs, in one shape.

    Cached input does not arrive in `input_tokens`. It arrives in
    `cache_read_input_tokens` and `cache_creation_input_tokens`, which were
    parsed here and then read by nobody - so the moment the cache starts
    working, a run's recorded `tokens_in` would collapse and the token budget
    would stop seeing the context it governs. Every caller gets all three.

    The live SDK can return None for the cache fields; the replay stub defaults
    them to 0. Both are normalised to int here so no caller has to care.
    """
    u = getattr(resp, "usage", None)

    def n(name: str) -> int:
        return int(getattr(u, name, 0) or 0)

    return {
        "input": n("input_tokens"),
        "cache_read": n("cache_read_input_tokens"),
        "cache_write": n("cache_creation_input_tokens"),
        "output": n("output_tokens"),
        "reasoning": n("reasoning_tokens"),
    }


def context_in(u: dict[str, int]) -> int:
    """Input tokens the turn carried, cached or not.

    This is context PRESSURE, not spend: a cache read is billed at a fraction
    of a fresh token but occupies the same window and is re-sent on every
    turn. Counting it keeps `max_tokens_total` meaning exactly what it meant
    before the cache worked. Weighting these by price is a separate concern
    and deliberately not done here.

    One formula serves both providers because the OpenAI adapter normalises to
    Anthropic's INCLUSION semantics before this is ever called. The two APIs
    disagree: Anthropic's `input_tokens` EXCLUDES cached and cache-write
    tokens, OpenAI's INCLUDES them (its docs define ordinary input as total
    minus cached minus cache-write). Summing OpenAI's raw numbers here would
    count every cached token twice, inflate `tokens_in`, and fire the hard
    budget early - degrading healthy runs. `_openai_usage` subtracts them back
    out, so this sum always equals OpenAI's own `input_tokens`.
    """
    return u["input"] + u["cache_read"] + u["cache_write"]


# -- OpenAI: canonical <-> Responses API ------------------------------------
#
# The canonical format is the one the graph already speaks. It is not ported
# to OpenAI; OpenAI is translated to it, so everything above this boundary -
# the loop, budgets, fuses, completion, grounding, the change map - is
# untouched, and the cassette format keeps replaying on either provider.


def _nullable(sub: dict) -> dict:
    """Widen a type so an optional field can be required-but-null."""
    kind = sub.get("type")
    if kind is None:
        return sub                      # enum/anyOf: leave it alone
    kinds = kind if isinstance(kind, list) else [kind]
    return sub if "null" in kinds else {**sub, "type": [*kinds, "null"]}


def strictify(schema: dict) -> dict:
    """Make a schema satisfy OpenAI strict mode's two hard preconditions.

    Strict mode demands `additionalProperties: false` on every object and
    every property listed in `required`. Our tool schemas satisfy neither by
    default: `read_file` and `search_code` are hand-written dicts in
    `agent/tools.py`, and `search_code.path` is genuinely optional.

    Doing this here rather than in `tools.py` is the point - the tool
    definitions stay provider-neutral, and only the OpenAI boundary knows what
    OpenAI insists on. An originally-optional field becomes required AND
    nullable, which is the encoding OpenAI documents for optionality; the
    response translation then drops the nulls so the tools still see an
    absent argument rather than a None they were never written to accept.
    """
    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(item) for item in node]
        if not isinstance(node, dict):
            return node
        out = {k: walk(v) for k, v in node.items()}
        props = out.get("properties")
        if isinstance(props, dict):
            required = set(out.get("required") or [])
            for name, sub in list(props.items()):
                if name not in required and isinstance(sub, dict):
                    props[name] = _nullable(sub)
            out["additionalProperties"] = False
            out["required"] = list(props.keys())
        return out

    return walk(schema)


def _openai_tools(tools: list | None) -> list | None:
    """Canonical tool schemas -> Responses function tools, strict mode on."""
    if tools is None:
        return None
    return [
        {
            "type": "function",
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": strictify(inline_defs(tool["input_schema"])),
            "strict": True,
        }
        for tool in tools
    ]


def _openai_input(messages: list) -> list:
    """Canonical messages -> Responses input items.

    `cache_control` is dropped on the way through. OpenAI has no explicit
    breakpoints in implicit mode - it places one at the end of the latest
    eligible message, which is exactly what Phase 2's rolling breakpoint does
    by hand. The design was right; the platform does it for us, so the markers
    stay in the canonical transcript (the Anthropic path needs them) and are
    stripped here.
    """
    items: list[dict] = []
    for message in messages:
        role = message.get("role", "user")
        content = message.get("content")
        if isinstance(content, str):
            items.append({"role": role, "content": content})
            continue
        text_parts: list[str] = []
        for block in content or []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "tool_use":
                items.append(
                    {
                        "type": "function_call",
                        "call_id": block["id"],
                        "name": block["name"],
                        "arguments": json.dumps(block.get("input") or {}),
                    }
                )
            elif kind == "tool_result":
                body = block.get("content")
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": block["tool_use_id"],
                        "output": body if isinstance(body, str) else json.dumps(body),
                    }
                )
            elif kind == "text":
                text_parts.append(block.get("text") or "")
        if text_parts:
            items.append({"role": role, "content": "\n".join(text_parts)})
    return items


def _openai_canonical(resp: Any) -> dict:
    """A Responses result -> the canonical dict `_Response` is built from."""
    content: list[dict] = []
    for item in getattr(resp, "output", None) or []:
        kind = getattr(item, "type", None)
        if kind == "function_call":
            try:
                parsed = json.loads(getattr(item, "arguments", "") or "{}")
            except json.JSONDecodeError:
                # Not fatal: an unparseable submission is exactly what Phase 1's
                # invalid-submission path already handles, and a non-dict input
                # fails validation there with a message the model can act on.
                parsed = {"__unparsed_arguments__": getattr(item, "arguments", "")}
            if isinstance(parsed, dict):
                # `strictify` made optional arguments required-and-nullable, so
                # "not supplied" arrives as an explicit null. The tools were
                # written against absence, not None - `search_code(path=None)`
                # would fall straight through its default and crash on resolve.
                parsed = {k: v for k, v in parsed.items() if v is not None}
            content.append(
                {
                    "type": "tool_use",
                    "id": getattr(item, "call_id", None) or getattr(item, "id", ""),
                    "name": getattr(item, "name", ""),
                    "input": parsed,
                }
            )
        elif kind == "message":
            for part in getattr(item, "content", None) or []:
                text = getattr(part, "text", None)
                if text:
                    content.append({"type": "text", "text": text})

    stop = "tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn"
    if getattr(resp, "status", None) == "incomplete":
        reason = getattr(getattr(resp, "incomplete_details", None), "reason", None)
        stop = reason or "incomplete"
    return {"stop_reason": stop, "usage": _openai_usage(resp), "content": content}


def _openai_usage(resp: Any) -> dict:
    """Responses usage -> canonical usage, in Anthropic inclusion semantics.

    See `context_in`: OpenAI counts cached and cache-write tokens INSIDE
    `input_tokens`, so they are subtracted back out here. `context_in` then
    re-adds them and lands exactly on OpenAI's own `input_tokens`, and the
    budget keeps the meaning it had before the migration.
    """
    usage = getattr(resp, "usage", None)

    def n(obj: Any, name: str) -> int:
        return int(getattr(obj, name, 0) or 0)

    detail_in = getattr(usage, "input_tokens_details", None)
    detail_out = getattr(usage, "output_tokens_details", None)
    cached = n(detail_in, "cached_tokens")
    written = n(detail_in, "cache_write_tokens")
    total_in = n(usage, "input_tokens")
    return {
        "input_tokens": max(total_in - cached - written, 0),
        "output_tokens": n(usage, "output_tokens"),
        "cache_read_input_tokens": cached,
        "cache_creation_input_tokens": written,
        "reasoning_tokens": n(detail_out, "reasoning_tokens"),
    }


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
    def call(
        self,
        *,
        system: list | str,
        messages: list,
        tools: list | None = None,
        tool_choice: dict | None = None,
    ):
        if self.mode == "replay":
            return self._from_cassette()
        resp = self._live(
            system=system, messages=messages, tools=tools, tool_choice=tool_choice
        )
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

    def _live(self, *, system, messages, tools, tool_choice=None):
        if get_settings().provider == "openai":
            return self._openai(
                system=system, messages=messages, tools=tools, tool_choice=tool_choice
            )
        return self._anthropic(
            system=system, messages=messages, tools=tools, tool_choice=tool_choice
        )

    def _anthropic(self, *, system, messages, tools, tool_choice=None):
        s = get_settings()
        kwargs: dict[str, Any] = dict(
            model=s.model,
            max_tokens=s.max_tokens,
            system=system,
            messages=messages,
        )
        if tools is not None:
            kwargs["tools"] = tools
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
        client = Anthropic(api_key=s.anthropic_api_key.get_secret_value())
        try:
            return client.messages.create(**kwargs)
        except APIConnectionError as e:
            raise TransientError(str(e)) from e
        except APIStatusError as e:
            if e.status_code in TRANSIENT_HTTP:
                raise TransientError(str(e), code=e.status_code) from e
            raise PermanentError(str(e), code=e.status_code) from e

    def _openai(self, *, system, messages, tools, tool_choice=None):
        """One Responses call, returned in the canonical shape."""
        from openai import APIConnectionError as OpenAIConnectionError
        from openai import APIStatusError as OpenAIStatusError
        from openai import OpenAI

        s = get_settings()
        kwargs: dict[str, Any] = dict(
            model=s.model,
            instructions=system if isinstance(system, str) else str(system),
            input=_openai_input(messages),
            max_output_tokens=s.max_output_tokens,
            reasoning={"effort": s.reasoning_effort},
        )
        converted = _openai_tools(tools)
        if converted is not None:
            kwargs["tools"] = converted
        if tool_choice is not None:
            kwargs["tool_choice"] = {"type": "function", "name": tool_choice["name"]}

        client = OpenAI(api_key=s.openai_api_key.get_secret_value())
        try:
            resp = client.responses.create(**kwargs)
        except OpenAIConnectionError as e:
            raise TransientError(str(e)) from e
        except OpenAIStatusError as e:
            if e.status_code in TRANSIENT_HTTP:
                raise TransientError(str(e), code=e.status_code) from e
            raise PermanentError(str(e), code=e.status_code) from e

        canonical = _openai_canonical(resp)
        # A reasoning model can burn the whole output allowance thinking and
        # return nothing visible. Silence would read to `after_agent` as "no
        # tool call" and cost a forced_submit that is just as likely to come
        # back empty, so it is named here instead.
        if canonical["stop_reason"] == "max_output_tokens" and not canonical["content"]:
            raise PermanentError(
                f"openai returned no output: reasoning consumed all "
                f"{s.max_output_tokens} output tokens (effort="
                f"{s.reasoning_effort}). Raise MAX_OUTPUT_TOKENS or lower "
                f"REASONING_EFFORT."
            )
        return _Response(canonical)
