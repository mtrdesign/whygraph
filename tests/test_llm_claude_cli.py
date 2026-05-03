from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from whygraph.prompts import PROMPT_VERSION, Rationale
from whygraph.rationale import (
    ClaudeCliClient,
    LLMUsage,
    _extract_json,
    _extract_text,
    _extract_usage,
)

VALID_RATIONALE_JSON = json.dumps(
    {
        "purpose": "Validates JWT tokens.",
        "why": "Replaces legacy cookie validator after compliance audit.",
        "constraints": ["must be sync"],
        "tradeoffs": ["JWK lookup cached"],
        "risks": ["claim shape change breaks RoleResolver"],
    }
)


def _envelope(result_text: str = VALID_RATIONALE_JSON, **extra) -> dict:
    base = {"type": "result", "subtype": "success", "result": result_text}
    base.update(extra)
    return base


def _completed(stdout: str, *, returncode: int = 0, stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def test_extract_text_prefers_result_field() -> None:
    assert _extract_text({"result": "hello"}) == "hello"


def test_extract_text_falls_back_to_string_content() -> None:
    assert _extract_text({"content": "hello"}) == "hello"


def test_extract_text_walks_content_blocks() -> None:
    env = {
        "content": [
            {"type": "thinking", "text": "..."},
            {"type": "text", "text": "the actual text"},
        ]
    }
    assert _extract_text(env) == "the actual text"


def test_extract_text_falls_back_to_text_field() -> None:
    assert _extract_text({"text": "hello"}) == "hello"


def test_extract_text_raises_when_nothing_present() -> None:
    with pytest.raises(ValueError, match="text result"):
        _extract_text({"unrelated": "x"})


def test_extract_json_parses_raw() -> None:
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_strips_json_fence() -> None:
    fenced = "```json\n{\"a\": 1}\n```"
    assert _extract_json(fenced) == {"a": 1}


def test_extract_json_strips_bare_fence() -> None:
    fenced = "```\n{\"a\": 1}\n```"
    assert _extract_json(fenced) == {"a": 1}


def test_extract_json_raises_when_unparseable() -> None:
    with pytest.raises(ValueError, match="could not parse"):
        _extract_json("this is not JSON at all")


def test_extract_json_no_brace_fallback_per_v1_decision() -> None:
    # v1 deviation: text wrapping JSON without a fence is NOT auto-rescued.
    text = "Here is the answer: {\"a\": 1} hope that helps."
    with pytest.raises(ValueError):
        _extract_json(text)


def test_extract_usage_returns_zeros_when_missing() -> None:
    assert _extract_usage({}) == LLMUsage()


def test_extract_usage_handles_partial_fields() -> None:
    u = _extract_usage({"usage": {"input_tokens": 100, "output_tokens": 50}})
    assert u.input_tokens == 100
    assert u.output_tokens == 50
    assert u.cache_read_input_tokens == 0
    assert u.cache_creation_input_tokens == 0


def test_extract_usage_handles_full_fields() -> None:
    u = _extract_usage(
        {
            "usage": {
                "input_tokens": 1,
                "output_tokens": 2,
                "cache_read_input_tokens": 3,
                "cache_creation_input_tokens": 4,
            }
        }
    )
    assert u == LLMUsage(
        input_tokens=1,
        output_tokens=2,
        cache_read_input_tokens=3,
        cache_creation_input_tokens=4,
    )


def test_extract_usage_ignores_non_dict() -> None:
    assert _extract_usage({"usage": "not a dict"}) == LLMUsage()


# ---------------------------------------------------------------------------
# ClaudeCliClient.generate
# ---------------------------------------------------------------------------


def test_generate_returns_validated_rationale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _completed(json.dumps(_envelope()))
    )
    client = ClaudeCliClient(model="claude-sonnet-4-6")
    result = client.generate(
        system_prompt="sys", user_prompt="user", schema=Rationale
    )
    assert isinstance(result.rationale, Rationale)
    assert result.rationale.purpose.startswith("Validates")
    assert result.model == "claude-sonnet-4-6"
    assert result.backend == "claude_cli"
    assert result.prompt_version == PROMPT_VERSION


def test_generate_strips_anthropic_api_key_from_child_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    captured: dict = {}

    def fake_run(*args, **kwargs):
        captured["env"] = kwargs.get("env")
        return _completed(json.dumps(_envelope()))

    monkeypatch.setattr(subprocess, "run", fake_run)
    ClaudeCliClient(model="m").generate(system_prompt="s", user_prompt="u")
    assert "ANTHROPIC_API_KEY" not in captured["env"]


def test_generate_passes_user_prompt_via_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def fake_run(*args, **kwargs):
        captured["input"] = kwargs.get("input")
        captured["argv"] = args[0]
        return _completed(json.dumps(_envelope()))

    monkeypatch.setattr(subprocess, "run", fake_run)
    ClaudeCliClient(model="m").generate(
        system_prompt="SYS", user_prompt="USER PROMPT"
    )
    assert captured["input"] == "USER PROMPT"
    argv = captured["argv"]
    assert argv[0] == "claude"
    assert "-p" in argv
    assert "--system-prompt" in argv
    assert "SYS" in argv
    assert "--output-format" in argv
    assert "json" in argv
    assert "--model" in argv
    assert "m" in argv


def test_generate_raises_on_non_zero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: _completed("", returncode=2, stderr="boom"),
    )
    with pytest.raises(RuntimeError, match="exited 2.*boom"):
        ClaudeCliClient(model="m").generate(system_prompt="s", user_prompt="u")


def test_generate_raises_on_unparseable_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _completed("not json")
    )
    with pytest.raises(ValueError, match="JSON envelope"):
        ClaudeCliClient(model="m").generate(system_prompt="s", user_prompt="u")


def test_generate_raises_when_envelope_is_not_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _completed("[1, 2]"))
    with pytest.raises(ValueError, match="not a JSON object"):
        ClaudeCliClient(model="m").generate(system_prompt="s", user_prompt="u")


def test_generate_raises_on_schema_validation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad = json.dumps(_envelope(result_text=json.dumps({"purpose": "x"})))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _completed(bad))
    with pytest.raises(ValueError, match="schema validation"):
        ClaudeCliClient(model="m").generate(system_prompt="s", user_prompt="u")


def test_generate_handles_fenced_json_inside_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fenced = f"```json\n{VALID_RATIONALE_JSON}\n```"
    env = json.dumps(_envelope(result_text=fenced))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _completed(env))
    result = ClaudeCliClient(model="m").generate(
        system_prompt="s", user_prompt="u"
    )
    assert isinstance(result.rationale, Rationale)


def test_generate_propagates_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = json.dumps(
        _envelope(
            usage={
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 80,
                "cache_creation_input_tokens": 0,
            }
        )
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _completed(env))
    result = ClaudeCliClient(model="m").generate(
        system_prompt="s", user_prompt="u"
    )
    assert result.usage.input_tokens == 100
    assert result.usage.cache_read_input_tokens == 80


def test_generate_raises_clear_error_when_claude_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args, **kwargs):
        raise FileNotFoundError("no claude")

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(RuntimeError, match="claude CLI not found"):
        ClaudeCliClient(model="m").generate(system_prompt="s", user_prompt="u")
