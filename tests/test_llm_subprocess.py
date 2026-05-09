import subprocess
from unittest.mock import patch

import pytest

from whygraph.llm_subprocess import LlmError, claude_cli_available, invoke_claude


class _FakeResult:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_invoke_claude_passes_prompt_on_stdin_and_uses_lean_flags() -> None:
    captured: dict = {}

    def fake_run(args, *, input, **kw):
        captured["args"] = args
        captured["input"] = input
        captured["timeout"] = kw.get("timeout")
        return _FakeResult(returncode=0, stdout="  hello world  \n")

    with patch("whygraph.llm_subprocess.subprocess.run", side_effect=fake_run):
        out = invoke_claude("PROMPT", model="haiku", timeout_sec=11)

    assert out == "hello world"
    args = captured["args"]
    assert args[0] == "claude"
    assert "--print" in args
    assert args[args.index("--model") + 1] == "haiku"
    assert "--strict-mcp-config" in args
    assert "--tools" in args
    assert "--disable-slash-commands" in args
    assert "--no-session-persistence" in args
    assert captured["input"] == "PROMPT"
    assert captured["timeout"] == 11


def test_invoke_claude_passes_system_prompt_when_provided() -> None:
    captured: dict = {}

    def fake_run(args, *, input, **kw):
        captured["args"] = args
        return _FakeResult(returncode=0, stdout="ok")

    with patch("whygraph.llm_subprocess.subprocess.run", side_effect=fake_run):
        invoke_claude(
            "user msg",
            model="m",
            timeout_sec=5,
            system_prompt="be terse",
        )

    args = captured["args"]
    assert "--system-prompt" in args
    assert args[args.index("--system-prompt") + 1] == "be terse"


def test_invoke_claude_omits_system_prompt_flag_when_none() -> None:
    captured: dict = {}

    def fake_run(args, *, input, **kw):
        captured["args"] = args
        return _FakeResult(returncode=0, stdout="ok")

    with patch("whygraph.llm_subprocess.subprocess.run", side_effect=fake_run):
        invoke_claude("p", model="m", timeout_sec=5)

    assert "--system-prompt" not in captured["args"]


def test_invoke_claude_strips_api_key_by_default(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-leak")
    captured: dict = {}

    def fake_run(args, *, input, env, **kw):
        captured["env"] = env
        return _FakeResult(returncode=0, stdout="ok")

    with patch("whygraph.llm_subprocess.subprocess.run", side_effect=fake_run):
        invoke_claude("p", model="m", timeout_sec=5)

    assert "ANTHROPIC_API_KEY" not in captured["env"]


def test_invoke_claude_sets_api_key_when_provided(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    captured: dict = {}

    def fake_run(args, *, input, env, **kw):
        captured["env"] = env
        return _FakeResult(returncode=0, stdout="ok")

    with patch("whygraph.llm_subprocess.subprocess.run", side_effect=fake_run):
        invoke_claude("p", model="m", timeout_sec=5, anthropic_api_key="sk-explicit")

    assert captured["env"]["ANTHROPIC_API_KEY"] == "sk-explicit"


def test_invoke_claude_explicit_key_overrides_inherited(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-inherited")
    captured: dict = {}

    def fake_run(args, *, input, env, **kw):
        captured["env"] = env
        return _FakeResult(returncode=0, stdout="ok")

    with patch("whygraph.llm_subprocess.subprocess.run", side_effect=fake_run):
        invoke_claude("p", model="m", timeout_sec=5, anthropic_api_key="sk-explicit")

    assert captured["env"]["ANTHROPIC_API_KEY"] == "sk-explicit"


def test_invoke_claude_handles_missing_cli() -> None:
    with patch(
        "whygraph.llm_subprocess.subprocess.run", side_effect=FileNotFoundError
    ):
        with pytest.raises(LlmError, match="not installed"):
            invoke_claude("p", model="m", timeout_sec=1)


def test_invoke_claude_handles_timeout() -> None:
    with patch(
        "whygraph.llm_subprocess.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=1),
    ):
        with pytest.raises(LlmError, match="timed out"):
            invoke_claude("p", model="m", timeout_sec=1)


def test_invoke_claude_handles_nonzero_exit() -> None:
    with patch(
        "whygraph.llm_subprocess.subprocess.run",
        return_value=_FakeResult(returncode=2, stderr="bad model"),
    ):
        with pytest.raises(LlmError, match="exited 2"):
            invoke_claude("p", model="m", timeout_sec=1)


def test_invoke_claude_handles_empty_output() -> None:
    with patch(
        "whygraph.llm_subprocess.subprocess.run",
        return_value=_FakeResult(returncode=0, stdout="   "),
    ):
        with pytest.raises(LlmError, match="empty"):
            invoke_claude("p", model="m", timeout_sec=1)


def test_claude_cli_available_truthy_or_falsy() -> None:
    assert claude_cli_available() in (True, False)
