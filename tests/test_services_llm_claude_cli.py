"""Behavioral tests for ``ClaudeCliAdapter``.

Locks the same contract the old ``invoke_claude`` had: lean flag set,
env stripping by default, env-var injection when ``api_key`` is set,
system-prompt routing, and the four error shapes (missing CLI, timeout,
non-zero exit, empty output). Tests patch
``whygraph.services.llm.claude_cli.subprocess.run`` so no real
subprocess is launched.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from whygraph.services.llm import (
    ClaudeCliAdapter,
    CompletionRequest,
    LlmError,
)


def _ok(stdout: str = "  hello\n", stderr: str = "", returncode: int = 0):
    """Build a fake CompletedProcess-like result."""
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def test_complete_passes_prompt_on_stdin_and_uses_lean_flags() -> None:
    captured: dict = {}

    def fake_run(cmd, *, input, text, capture_output, check, timeout, env):
        captured["cmd"] = cmd
        captured["input"] = input
        captured["env"] = env
        captured["timeout"] = timeout
        return _ok("answer text")

    with patch("whygraph.services.llm.claude_cli.subprocess.run", side_effect=fake_run):
        client = ClaudeCliAdapter(model="claude-haiku-4-5", timeout_sec=42)
        resp = client.complete(CompletionRequest.of("hi"))

    assert resp.text == "answer text"
    assert resp.model == "claude-haiku-4-5"
    assert resp.provider == "claude-cli"
    cmd = captured["cmd"]
    assert cmd[:4] == ["claude", "--print", "--model", "claude-haiku-4-5"]
    assert "--strict-mcp-config" in cmd
    assert "--tools" in cmd and "" in cmd
    assert "--disable-slash-commands" in cmd
    assert "--no-session-persistence" in cmd
    assert "--system-prompt" not in cmd
    assert captured["input"] == "hi"
    assert captured["timeout"] == 42


def test_complete_routes_system_messages_to_system_prompt_flag() -> None:
    captured: dict = {}

    def fake_run(cmd, *, input, **_):
        captured["cmd"] = cmd
        captured["input"] = input
        return _ok("ok")

    with patch("whygraph.services.llm.claude_cli.subprocess.run", side_effect=fake_run):
        client = ClaudeCliAdapter(model="m")
        client.complete(CompletionRequest.of("user content", system="be terse"))

    assert "--system-prompt" in captured["cmd"]
    idx = captured["cmd"].index("--system-prompt")
    assert captured["cmd"][idx + 1] == "be terse"
    assert captured["input"] == "user content"


def test_complete_omits_system_prompt_flag_when_no_system_message() -> None:
    captured: dict = {}

    def fake_run(cmd, **_):
        captured["cmd"] = cmd
        return _ok("ok")

    with patch("whygraph.services.llm.claude_cli.subprocess.run", side_effect=fake_run):
        ClaudeCliAdapter(model="m").complete(CompletionRequest.of("hi"))

    assert "--system-prompt" not in captured["cmd"]


def test_complete_strips_anthropic_api_key_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
    captured: dict = {}

    def fake_run(cmd, *, env, **_):
        captured["env"] = env
        return _ok("ok")

    with patch("whygraph.services.llm.claude_cli.subprocess.run", side_effect=fake_run):
        ClaudeCliAdapter(model="m").complete(CompletionRequest.of("hi"))

    assert "ANTHROPIC_API_KEY" not in captured["env"]


def test_complete_sets_anthropic_api_key_when_provided(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    captured: dict = {}

    def fake_run(cmd, *, env, **_):
        captured["env"] = env
        return _ok("ok")

    with patch("whygraph.services.llm.claude_cli.subprocess.run", side_effect=fake_run):
        ClaudeCliAdapter(model="m", api_key="sk-explicit").complete(
            CompletionRequest.of("hi")
        )

    assert captured["env"]["ANTHROPIC_API_KEY"] == "sk-explicit"


def test_complete_raises_llm_error_when_cli_missing() -> None:
    with patch(
        "whygraph.services.llm.claude_cli.subprocess.run",
        side_effect=FileNotFoundError,
    ):
        with pytest.raises(LlmError, match="not installed"):
            ClaudeCliAdapter(model="m").complete(CompletionRequest.of("hi"))


def test_complete_raises_llm_error_on_timeout() -> None:
    with patch(
        "whygraph.services.llm.claude_cli.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["claude"], timeout=5),
    ):
        with pytest.raises(LlmError, match="timed out"):
            ClaudeCliAdapter(model="m", timeout_sec=5).complete(
                CompletionRequest.of("hi")
            )


def test_complete_raises_llm_error_on_nonzero_exit() -> None:
    with patch(
        "whygraph.services.llm.claude_cli.subprocess.run",
        return_value=_ok(stdout="", stderr="boom", returncode=2),
    ):
        with pytest.raises(LlmError, match=r"exited 2"):
            ClaudeCliAdapter(model="m").complete(CompletionRequest.of("hi"))


def test_complete_raises_llm_error_on_empty_output() -> None:
    with patch(
        "whygraph.services.llm.claude_cli.subprocess.run",
        return_value=_ok(stdout="   \n", stderr="", returncode=0),
    ):
        with pytest.raises(LlmError, match="empty"):
            ClaudeCliAdapter(model="m").complete(CompletionRequest.of("hi"))


def test_complete_requires_at_least_one_user_message() -> None:
    req = CompletionRequest(
        messages=(),  # no messages at all
    )
    with pytest.raises(LlmError, match="user message"):
        ClaudeCliAdapter(model="m").complete(req)


def test_is_available_returns_bool() -> None:
    assert isinstance(ClaudeCliAdapter.is_available(), bool)


def test_from_config_maps_fields() -> None:
    from whygraph.core.config import ClaudeCliConfig

    cfg = ClaudeCliConfig(model="claude-x", api_key="sk-cfg", timeout_sec=33)
    client = ClaudeCliAdapter.from_config(cfg)
    assert client.model == "claude-x"
    assert client._api_key == "sk-cfg"
    assert client._default_timeout == 33
