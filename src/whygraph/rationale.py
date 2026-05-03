from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Protocol

import anthropic
from pydantic import BaseModel, ValidationError

from whygraph.config import Config
from whygraph.prompts import PROMPT_VERSION, Rationale


@dataclass(frozen=True)
class LLMUsage:
    input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class LLMResult:
    rationale: Rationale
    model: str
    backend: str
    prompt_version: str
    usage: LLMUsage


class LLMClient(Protocol):
    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: type[BaseModel] = Rationale,
    ) -> LLMResult: ...


_FENCE_RE = re.compile(r"^```(?:json|JSON)?\s*\r?\n|\r?\n```\s*$")


def _extract_text(envelope: dict[str, Any]) -> str:
    # Claude CLI's --output-format json envelope: typically
    # {type: "result", subtype: "success", result: "...", usage: {...}, ...}
    result = envelope.get("result")
    if isinstance(result, str):
        return result
    content = envelope.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ):
                return block["text"]
    text = envelope.get("text")
    if isinstance(text, str):
        return text
    raise ValueError("claude CLI envelope did not contain a text result")


def _extract_json(text: str) -> Any:
    trimmed = text.strip()
    # 1. Try whole text.
    try:
        return json.loads(trimmed)
    except ValueError:
        pass
    # 2. Strip a leading ```json / ``` fence and a trailing ``` if present.
    stripped = re.sub(r"^```(?:json|JSON)?\s*\r?\n", "", trimmed)
    stripped = re.sub(r"\r?\n```\s*$", "", stripped)
    if stripped != trimmed:
        try:
            return json.loads(stripped.strip())
        except ValueError:
            pass
    # v1 deviation from v0: no "first { to last }" desperate fallback.
    # Add it back only when a real test fails.
    raise ValueError(
        f"could not parse JSON from claude output (first 200 chars: {trimmed[:200]})"
    )


def _extract_usage(envelope: dict[str, Any]) -> LLMUsage:
    usage = envelope.get("usage")
    if not isinstance(usage, dict):
        return LLMUsage()

    def _num(v: Any) -> int:
        return int(v) if isinstance(v, (int, float)) else 0

    return LLMUsage(
        input_tokens=_num(usage.get("input_tokens")),
        cache_read_input_tokens=_num(usage.get("cache_read_input_tokens")),
        cache_creation_input_tokens=_num(usage.get("cache_creation_input_tokens")),
        output_tokens=_num(usage.get("output_tokens")),
    )


class ClaudeCliClient:
    """Spawns the local `claude` CLI; routes via the user's Claude Code OAuth.

    ANTHROPIC_API_KEY is stripped from the child environment so the CLI falls
    back to the subscription path instead of the direct-API billing path.
    """

    backend = "claude_cli"

    def __init__(self, model: str, *, timeout_seconds: float = 120.0) -> None:
        self.model = model
        self._timeout = timeout_seconds

    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: type[BaseModel] = Rationale,
    ) -> LLMResult:
        args = [
            "-p",
            "--system-prompt",
            system_prompt,
            "--output-format",
            "json",
            "--model",
            self.model,
        ]
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}

        try:
            proc = subprocess.run(
                ["claude", *args],
                input=user_prompt,
                capture_output=True,
                text=True,
                env=env,
                timeout=self._timeout,
                check=False,
            )
        except FileNotFoundError as e:
            raise RuntimeError(
                "claude CLI not found on PATH. Install it from "
                "https://claude.ai/download or set "
                "WHYGRAPH_RATIONALE_BACKEND=api with ANTHROPIC_API_KEY."
            ) from e

        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            suffix = f": {stderr}" if stderr else ""
            raise RuntimeError(
                f"claude CLI exited {proc.returncode}{suffix}"
            )

        try:
            envelope = json.loads(proc.stdout)
        except ValueError as e:
            head = proc.stdout[:120]
            raise ValueError(
                f"claude CLI did not return JSON envelope (got {head!r}...)"
            ) from e
        if not isinstance(envelope, dict):
            raise ValueError(
                "claude CLI envelope was not a JSON object"
            )

        text = _extract_text(envelope)
        parsed = _extract_json(text)
        try:
            validated = schema.model_validate(parsed)
        except ValidationError as e:
            raise ValueError(f"claude CLI output failed schema validation: {e}") from e
        if not isinstance(validated, Rationale):
            # Caller passed a custom schema — coerce by re-validating raw dict.
            validated = Rationale.model_validate(parsed)

        return LLMResult(
            rationale=validated,
            model=self.model,
            backend=self.backend,
            prompt_version=PROMPT_VERSION,
            usage=_extract_usage(envelope),
        )


def _extract_text_from_sdk_response(response: Any) -> str:
    content = getattr(response, "content", None)
    if not isinstance(content, list):
        raise ValueError("Anthropic response.content is not a list")
    for block in content:
        if getattr(block, "type", None) == "text":
            text = getattr(block, "text", None)
            if isinstance(text, str):
                return text
    raise ValueError("Anthropic response had no text content block")


def _sdk_usage(response: Any) -> LLMUsage:
    usage = getattr(response, "usage", None)
    if usage is None:
        return LLMUsage()

    def _num(v: Any) -> int:
        return int(v) if isinstance(v, (int, float)) else 0

    return LLMUsage(
        input_tokens=_num(getattr(usage, "input_tokens", 0)),
        cache_read_input_tokens=_num(getattr(usage, "cache_read_input_tokens", 0)),
        cache_creation_input_tokens=_num(
            getattr(usage, "cache_creation_input_tokens", 0)
        ),
        output_tokens=_num(getattr(usage, "output_tokens", 0)),
    )


class AnthropicSdkClient:
    """Direct API path. Activated via WHYGRAPH_RATIONALE_BACKEND=api or by
    setting ANTHROPIC_API_KEY (which auto-selects this backend in load_config).

    Uses plain messages.create + Pydantic validation rather than v0's
    messages.parse + output_config (a beta SDK helper).
    """

    backend = "api"

    def __init__(
        self,
        model: str,
        api_key: str,
        *,
        max_tokens: int = 2048,
    ) -> None:
        self.model = model
        self._client = anthropic.Anthropic(api_key=api_key)
        self._max_tokens = max_tokens

    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: type[BaseModel] = Rationale,
    ) -> LLMResult:
        response = self._client.messages.create(
            model=self.model,
            max_tokens=self._max_tokens,
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_prompt}],
        )

        text = _extract_text_from_sdk_response(response)
        parsed = _extract_json(text)
        try:
            validated = Rationale.model_validate(parsed)
        except ValidationError as e:
            raise ValueError(
                f"Anthropic SDK output failed schema validation: {e}"
            ) from e

        return LLMResult(
            rationale=validated,
            model=self.model,
            backend=self.backend,
            prompt_version=PROMPT_VERSION,
            usage=_sdk_usage(response),
        )


def make_llm_client(config: Config) -> LLMClient:
    if config.rationale_backend == "api":
        if not config.anthropic_api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY is not set. Set it in the MCP server "
                "environment, or set WHYGRAPH_RATIONALE_BACKEND=claude_cli "
                "to use the local claude CLI."
            )
        return AnthropicSdkClient(config.model, config.anthropic_api_key)
    return ClaudeCliClient(config.model)
