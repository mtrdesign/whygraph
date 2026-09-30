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
from pathlib import Path
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


def test_complete_strips_anthropic_api_key_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
    captured: dict = {}

    def fake_run(cmd, *, env, **_):
        captured["env"] = env
        return _ok("ok")

    with patch("whygraph.services.llm.claude_cli.subprocess.run", side_effect=fake_run):
        ClaudeCliAdapter(model="m").complete(CompletionRequest.of("hi"))

    assert "ANTHROPIC_API_KEY" not in captured["env"]


def test_complete_sets_anthropic_api_key_when_provided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    cfg = ClaudeCliConfig(
        model="claude-x",
        api_key="sk-cfg",
        timeout_sec=33,
        config_dir=Path("/profiles/work"),
    )
    client = ClaudeCliAdapter.from_config(cfg)
    assert client.model == "claude-x"
    assert client._api_key == "sk-cfg"
    assert client._default_timeout == 33
    assert client._config_dir == Path("/profiles/work")


# ---------- config_dir → CLAUDE_CONFIG_DIR ----------------------------------


def test_complete_sets_claude_config_dir_when_provided(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit profile dir overrides the ambient CLAUDE_CONFIG_DIR."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/ambient/profile")
    captured: dict = {}

    def fake_run(cmd, *, env, **_):
        captured["env"] = env
        return _ok("ok")

    with patch("whygraph.services.llm.claude_cli.subprocess.run", side_effect=fake_run):
        ClaudeCliAdapter(model="m", config_dir=tmp_path).complete(
            CompletionRequest.of("hi")
        )

    assert captured["env"]["CLAUDE_CONFIG_DIR"] == str(tmp_path)


def test_complete_inherits_ambient_claude_config_dir_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/ambient/profile")
    captured: dict = {}

    def fake_run(cmd, *, env, **_):
        captured["env"] = env
        return _ok("ok")

    with patch("whygraph.services.llm.claude_cli.subprocess.run", side_effect=fake_run):
        ClaudeCliAdapter(model="m").complete(CompletionRequest.of("hi"))

    assert captured["env"]["CLAUDE_CONFIG_DIR"] == "/ambient/profile"


def test_complete_raises_llm_error_when_config_dir_missing(tmp_path: Path) -> None:
    """A missing profile dir fails fast instead of spawning a logged-out CLI."""
    with patch("whygraph.services.llm.claude_cli.subprocess.run") as run:
        with pytest.raises(LlmError, match="config_dir"):
            ClaudeCliAdapter(model="m", config_dir=tmp_path / "nope").complete(
                CompletionRequest.of("hi")
            )
    run.assert_not_called()


# ---------------------------------------------------------------------------
# preflight() - fail the phase once, not every commit
# ---------------------------------------------------------------------------


def test_preflight_passes_when_cli_present(tmp_path: Path) -> None:
    adapter = ClaudeCliAdapter(config_dir=tmp_path)
    with patch("shutil.which", return_value="/usr/local/bin/claude"):
        adapter.preflight()


def test_preflight_raises_natively_when_cli_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WHYGRAPH_IN_IMAGE", raising=False)
    with patch("shutil.which", return_value=None):
        with pytest.raises(LlmError, match="claude CLI not found on PATH"):
            ClaudeCliAdapter().preflight()


def test_preflight_names_the_docker_image_when_cli_missing_inside_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WHYGRAPH_IN_IMAGE", "1")
    with patch("shutil.which", return_value=None):
        with pytest.raises(LlmError, match="Docker image does not include the claude"):
            ClaudeCliAdapter().preflight()


def test_preflight_raises_when_config_dir_missing(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    with patch("shutil.which", return_value="/usr/local/bin/claude"):
        with pytest.raises(LlmError, match="config_dir .* does not exist"):
            ClaudeCliAdapter(config_dir=missing).preflight()


def test_preflight_hints_at_container_home_for_a_tmp_config_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", "/tmp")
    missing = Path("/tmp/.claude-work-does-not-exist-whygraph-test")
    with patch("shutil.which", return_value="/usr/local/bin/claude"):
        with pytest.raises(LlmError, match="HOME is /tmp inside the container"):
            ClaudeCliAdapter(config_dir=missing).preflight()


def test_descriptor_and_generator_run_preflight(tmp_path: Path) -> None:
    """Both LLM factories refuse a claude-cli config without the binary."""
    from whygraph.analyze import LlmDescriptor, RationaleGenerator
    from whygraph.core.config import Config
    from whygraph.services.llm import LlmClientFactory

    config = Config.from_dict(
        {"llm": {"model": "claude-cli/claude-opus-4-7"}}, tmp_path
    )
    factory = LlmClientFactory(config.llm)
    with patch("shutil.which", return_value=None):
        with pytest.raises(LlmError, match="claude CLI"):
            LlmDescriptor.from_config(config, factory=factory)
        with pytest.raises(LlmError, match="claude CLI"):
            RationaleGenerator.from_config(config, factory=factory)
