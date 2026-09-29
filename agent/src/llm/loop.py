"""Bounded tool-use loop that lets the model inspect and edit the package."""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..changes import ChangeSet
from ..context import Context
from . import prompts
from .client import ChatClient, LLMError
from .tools import Toolbox


@dataclass
class Budget:
    max_steps: int = 40
    max_seconds: float = 1200.0
    max_tokens: int = 1_500_000


def _assistant_record(message: dict) -> dict:
    record = {"role": "assistant", "content": message.get("content") or ""}
    if message.get("tool_calls"):
        record["tool_calls"] = message["tool_calls"]
    return record


def run(ctx: Context, changes: ChangeSet, client: ChatClient, notes: list[str],
        budget: Budget, log=print) -> dict:
    tools = Toolbox(ctx, changes)
    schemas = tools.schemas()
    messages = [
        {"role": "system", "content": prompts.SYSTEM},
        {"role": "user", "content": prompts.dossier(ctx, notes)},
    ]
    started = time.monotonic()
    steps = 0
    stop = "max_steps"
    while steps < budget.max_steps:
        if time.monotonic() - started > budget.max_seconds:
            stop = "time"
            break
        if client.usage.total > budget.max_tokens:
            stop = "tokens"
            break
        steps += 1
        try:
            message = client.chat(messages, schemas)
        except LLMError as error:
            log(f"llm error: {error}")
            stop = "llm_error"
            break
        messages.append(_assistant_record(message))
        calls = message.get("tool_calls") or []
        if not calls:
            stop = "no_tool_call"
            break
        for call in calls:
            function = call.get("function") or {}
            name = function.get("name", "")
            result = tools.call(name, function.get("arguments") or "{}")
            log(f"step {steps}: {name}({str(function.get('arguments'))[:160]}) -> {result[:120]!r}")
            messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": result})
        if tools.finished is not None:
            stop = "finished"
            break
    return {
        "stop": stop,
        "steps": steps,
        "summary": tools.finished,
        "usage": {"prompt_tokens": client.usage.prompt_tokens,
                  "completion_tokens": client.usage.completion_tokens,
                  "calls": client.usage.calls},
        "seconds": round(time.monotonic() - started, 1),
        "messages": messages,
    }
