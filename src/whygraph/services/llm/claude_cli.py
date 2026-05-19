"""Anthropic Claude via the ``claude --print`` CLI (subscription billing).

Wraps the same subprocess invocation that lived in
``whygraph.llm_subprocess.invoke_claude`` before this iteration:
lean flag set (no MCP, tools, slash commands, or session persistence),
optional system-prompt routing, optional API-key injection, and the
four error shapes (missing CLI, timeout, non-zero exit, empty output).

Useful when you have a Claude Code subscription and prefer to bill
against it rather than via the Anthropic API.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any

from whygraph.core.config import ClaudeCliConfig

from .client import LlmClient
from .exceptions import LlmError
from .types import CompletionRequest, CompletionResponse

# Flags passed to every ``claude --print`` invocation. Trims the agent
# runtime of work the prompt doesn't need: MCP servers, tool init,
# slash command/skill discovery, on-disk session persistence.
# Cuts cold start ~40-50% in this repo's measurements.
_LEAN_FLAGS: tuple[str, ...] = (
    "--strict-mcp-config",
    "--mcp-config",
    '{"mcpServers":{}}',
    "--tools",
    "",
    "--disable-slash-commands",
    "--no-session-persistence",
)


class ClaudeCliAdapter(LlmClient):
    """``claude --print`` adapter.

    Parameters
    ----------
    model : str, optional
        Claude model identifier. Default ``"claude-opus-4-7"``.
    api_key : str, optional
        ``None`` (default) strips ``ANTHROPIC_API_KEY`` from the
        subprocess env so the CLI falls through to subscription
        billing. Passing a value exports it as ``ANTHROPIC_API_KEY``
        (API billing).
    timeout_sec : int, optional
        Per-call timeout in seconds. Default ``120``.
    """

    provider = "claude-cli"

    def __init__(
        self,
        *,
        model: str = "claude-opus-4-7",
        api_key: str | None = None,
        timeout_sec: int = 120,
    ) -> None:
        super().__init__(model=model)
        self._api_key = api_key
        self._default_timeout = timeout_sec

    @classmethod
    def from_config(
        cls,
        config: ClaudeCliConfig,
        **overrides: Any,
    ) -> "ClaudeCliAdapter":
        """Build an adapter from a typed :class:`ClaudeCliConfig` section.

        ``overrides`` are forwarded to the constructor verbatim. The
        adapter has no injectable third-party SDK client, so this is
        primarily a hook for future use.
        """
        return cls(
            model=config.model,
            api_key=config.api_key,
            timeout_sec=config.timeout_sec,
            **overrides,
        )

    @staticmethod
    def is_available() -> bool:
        """``True`` iff the ``claude`` CLI is on the current ``PATH``."""
        return shutil.which("claude") is not None

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        system_parts = [m.content for m in request.messages if m.role == "system"]
        user_parts = [m.content for m in request.messages if m.role == "user"]
        if not user_parts:
            raise LlmError("ClaudeCliAdapter requires at least one user message")
        system_prompt = "\n\n".join(system_parts) if system_parts else None
        stdin_payload = "\n\n".join(user_parts)
        timeout = request.timeout_sec or self._default_timeout

        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        if self._api_key:
            env["ANTHROPIC_API_KEY"] = self._api_key

        cmd = ["claude", "--print", "--model", self.model, *_LEAN_FLAGS]
        if system_prompt is not None:
            cmd.extend(["--system-prompt", system_prompt])

        try:
            result = subprocess.run(
                cmd,
                input=stdin_payload,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout,
                env=env,
            )
        except FileNotFoundError as exc:
            raise LlmError("claude CLI is not installed") from exc
        except subprocess.TimeoutExpired as exc:
            raise LlmError(f"claude timed out after {timeout}s") from exc

        if result.returncode != 0:
            stderr = (result.stderr or "").strip() or (result.stdout or "").strip()
            raise LlmError(f"claude exited {result.returncode}: {stderr}")
        text = (result.stdout or "").strip()
        if not text:
            raise LlmError("claude returned empty output")

        return CompletionResponse(
            text=text,
            model=self.model,
            provider=self.provider,
        )
