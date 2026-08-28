"""
llm.py - the only place that talks to OpenAI.

WHAT THIS FILE IS
    One client, reused by every stage, plus the unglamorous parts: retry with
    backoff, coercing replies into JSON, and recording a trace of every call.

WHAT IT IS NOT
    Anything to do with stories. This file does not know what a bedtime story
    is and would work unchanged for any other task.

WHY THE JSON HANDLING LOOKS LIKE THAT
    3.5 wraps JSON in markdown fences, adds "Sure, here you go!", or trails a
    sentence after the closing brace. complete_json handles all three: strip
    fences, brace-match the object (surviving braces inside strings), and if it
    is still broken, spend one cheap call asking the model to fix its own
    syntax. That repair call is far more reliable than re-rolling the request.

NOTE ON THE MODEL
    gpt-3.5-turbo is pinned, as the assignment requires. The call style is the
    modern one; the skeleton's openai.ChatCompletion was removed in openai v1,
    so it will not run on a current install. The skeleton's call_model() still
    exists at the bottom of this file with its original contract.

READ NEXT
    pipeline.py, which calls complete() and complete_json() and nothing else.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

try:  # optional convenience, never required
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass

from openai import OpenAI

MODEL = "gpt-3.5-turbo"


class LLMError(RuntimeError):
    pass


@dataclass
class TraceEntry:
    stage: str
    temperature: float
    seconds: float
    prompt_tokens: int
    completion_tokens: int
    system: str
    user: str
    output: str


@dataclass
class Usage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def estimated_cost_usd(self) -> float:
        # gpt-3.5-turbo-0125 list price, for the run summary only.
        return (self.prompt_tokens * 0.50 + self.completion_tokens * 1.50) / 1_000_000


class StoryLLM:
    """Thin, retrying, traced wrapper around one chat model."""

    def __init__(self, model: str = MODEL, api_key: str | None = None, max_attempts: int = 4):
        key = api_key or os.getenv("OPENAI_API_KEY")
        if not key:
            raise LLMError(
                "OPENAI_API_KEY is not set. Export it, or drop it in a .env file "
                "next to this script (see .env.example)."
            )
        self.model = model
        self.max_attempts = max_attempts
        self.usage = Usage()
        self.trace: list[TraceEntry] = []
        self._client = OpenAI(api_key=key)
        self._json_mode_supported = True

    # -- core call ---------------------------------------------------------

    def complete(
        self,
        system: str,
        user: str,
        *,
        stage: str = "unnamed",
        temperature: float = 0.7,
        max_tokens: int = 1200,
        json_mode: bool = False,
    ) -> str:
        """One chat call, retried on transient failure, recorded in the trace."""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        started = time.time()
        last_error: Exception | None = None

        for attempt in range(self.max_attempts):
            kwargs: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": False,
            }
            if json_mode and self._json_mode_supported:
                kwargs["response_format"] = {"type": "json_object"}

            try:
                resp = self._client.chat.completions.create(**kwargs)
            except Exception as exc:  # noqa: BLE001 - SDK exception names vary by version
                message = str(exc)
                if "response_format" in message and self._json_mode_supported:
                    # Older snapshots reject JSON mode. Fall back and retry now.
                    self._json_mode_supported = False
                    continue
                if any(w in message.lower() for w in ("api key", "authentication", "401")):
                    raise LLMError(f"OpenAI rejected the API key: {message}") from exc
                last_error = exc
                time.sleep(min(8.0, 1.5**attempt + random.random()))
                continue

            text = (resp.choices[0].message.content or "").strip()
            usage = getattr(resp, "usage", None)
            p_tok = getattr(usage, "prompt_tokens", 0) or 0
            c_tok = getattr(usage, "completion_tokens", 0) or 0

            self.usage.calls += 1
            self.usage.prompt_tokens += p_tok
            self.usage.completion_tokens += c_tok
            self.trace.append(
                TraceEntry(
                    stage=stage,
                    temperature=temperature,
                    seconds=round(time.time() - started, 2),
                    prompt_tokens=p_tok,
                    completion_tokens=c_tok,
                    system=system,
                    user=user,
                    output=text,
                )
            )
            return text

        raise LLMError(f"'{stage}' failed after {self.max_attempts} attempts: {last_error}")

    # -- json ---------------------------------------------------------------

    def complete_json(
        self,
        system: str,
        user: str,
        *,
        stage: str = "unnamed",
        temperature: float = 0.2,
        max_tokens: int = 900,
    ) -> dict[str, Any]:
        """As complete(), but guarantees a dict back or raises. See the header."""
        raw = self.complete(
            system,
            user,
            stage=stage,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=True,
        )
        parsed = _loads(raw)
        if parsed is not None:
            return parsed

        # One repair pass. Cheaper and far more reliable than a full re-roll.
        repaired = self.complete(
            "You fix malformed JSON. Return only the corrected JSON object, "
            "nothing else. Preserve the content; fix only the syntax.",
            raw,
            stage=f"{stage}:repair",
            temperature=0.0,
            max_tokens=max_tokens,
            json_mode=True,
        )
        parsed = _loads(repaired)
        if parsed is None:
            raise LLMError(f"'{stage}' did not return usable JSON:\n{raw[:400]}")
        return parsed

    # -- reporting ---------------------------------------------------------

    def trace_as_dicts(self) -> list[dict[str, Any]]:
        return [asdict(t) for t in self.trace]


_FENCE = re.compile(r"^```(?:json)?|```$", re.MULTILINE)


def _loads(text: str) -> dict[str, Any] | None:
    """Best-effort JSON parse of a model response."""
    if not text:
        return None
    cleaned = _FENCE.sub("", text).strip()
    try:
        value = json.loads(cleaned)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    if start == -1:
        return None
    depth, in_string, escaped = 0, False, False
    for i, ch in enumerate(cleaned[start:], start=start):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    value = json.loads(cleaned[start : i + 1])
                    return value if isinstance(value, dict) else None
                except json.JSONDecodeError:
                    return None
    return None


def call_model(prompt: str, max_tokens: int = 3000, temperature: float = 0.1) -> str:
    """
    The original skeleton's entry point, kept so the old contract still works.

    Real work goes through StoryLLM, which reuses one client and records a trace;
    this helper spins up a throwaway client per call and is here for parity only.
    """
    return StoryLLM().complete(
        "You are a helpful assistant.",
        prompt,
        stage="call_model",
        temperature=temperature,
        max_tokens=max_tokens,
    )
