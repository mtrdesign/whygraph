"""Shared `claude --print` subprocess plumbing.

Used by the scan's per-commit description writer and the MCP rationale tool.
Both layers ship their own prompt templates and parsing rules — this module
just wraps the common invocation: lean-flag set, env stripping, timeout, and
error shape.
"""

from __future__ import annotations

import os
import shutil
import subprocess


# Flags passed to every `claude --print` invocation. Trims the agent
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


class LlmError(RuntimeError):
    pass


def claude_cli_available() -> bool:
    return shutil.which("claude") is not None


def invoke_claude(
    prompt: str,
    *,
    model: str,
    timeout_sec: int,
    anthropic_api_key: str | None = None,
    system_prompt: str | None = None,
) -> str:
    """Run `claude --print --model <model>` with `prompt` on stdin.

    `anthropic_api_key=None` strips ANTHROPIC_API_KEY from the subprocess
    env so `claude` falls through to subscription billing. Passing a value
    exports it as ANTHROPIC_API_KEY (API billing).

    `system_prompt`, when provided, is passed via `--system-prompt` and
    REPLACES Claude Code's default agentic system prompt. Use this for
    one-shot deterministic generation (e.g. JSON output) where the model
    must not "decide to read a file" mid-response.

    Returns trimmed stdout. Raises `LlmError` on missing CLI, timeout,
    non-zero exit, or empty output.
    """
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    if anthropic_api_key:
        env["ANTHROPIC_API_KEY"] = anthropic_api_key
    cmd = ["claude", "--print", "--model", model, *_LEAN_FLAGS]
    if system_prompt is not None:
        cmd.extend(["--system-prompt", system_prompt])
    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_sec,
            env=env,
        )
    except FileNotFoundError as exc:
        raise LlmError("claude CLI is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise LlmError(f"claude timed out after {timeout_sec}s") from exc
    if result.returncode != 0:
        stderr = (result.stderr or "").strip() or (result.stdout or "").strip()
        raise LlmError(f"claude exited {result.returncode}: {stderr}")
    text = (result.stdout or "").strip()
    if not text:
        raise LlmError("claude returned empty output")
    return text
