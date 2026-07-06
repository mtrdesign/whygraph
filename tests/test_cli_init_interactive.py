"""Tests for the interactive ``whygraph init`` flow (no TTY required).

Drives :func:`whygraph.cli.interactive.prompt_for_init` through a
:class:`ScriptedPrompter` stand-in for the ``Prompter`` protocol, so the
whole guided flow — overwrite gate, rationale-defaults-to-analyze, per-
provider key prompting, the token gate, the abort paths, and the
secret-masking summary — is exercised without a terminal.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from whygraph.cli.interactive import InitAborted, prompt_for_init, render_summary
from whygraph.core.config import InitAnswers

# Sentinels the scripted prompter understands.
DEFAULT = object()  # accept the shown default (a bare Enter)
ABORT = object()  # simulate Ctrl-C / EOF (questionary returns None)


class ScriptedPrompter:
    """A :class:`~whygraph.cli.interactive.Prompter` driven by fixed scripts.

    Each prompt kind pops from its own queue in call order. An exhausted
    queue (or the :data:`DEFAULT` sentinel) yields the prompt's default;
    the :data:`ABORT` sentinel yields ``None`` (a Ctrl-C). Every call is
    recorded in :attr:`calls` for assertions on what was asked.
    """

    def __init__(
        self, *, selects=(), texts=(), passwords=(), confirms=()
    ) -> None:
        self.selects = list(selects)
        self.texts = list(texts)
        self.passwords = list(passwords)
        self.confirms = list(confirms)
        self.calls: list[tuple[str, str]] = []

    def select(self, message, choices, default):
        self.calls.append(("select", message))
        if not self.selects:
            return default
        v = self.selects.pop(0)
        return None if v is ABORT else default if v is DEFAULT else v

    def text(self, message, default):
        self.calls.append(("text", message))
        if not self.texts:
            return default
        v = self.texts.pop(0)
        return None if v is ABORT else default if v is DEFAULT else v

    def password(self, message):
        self.calls.append(("password", message))
        if not self.passwords:
            return ""
        v = self.passwords.pop(0)
        return None if v is ABORT else "" if v is DEFAULT else v

    def confirm(self, message, *, default):
        self.calls.append(("confirm", message))
        if not self.confirms:
            return default
        v = self.confirms.pop(0)
        return None if v is ABORT else default if v is DEFAULT else v


def _kinds(prompter: ScriptedPrompter, kind: str) -> list[str]:
    return [msg for k, msg in prompter.calls if k == kind]


# ---------- 5. rationale defaults to the analyze choice ----------------------


def test_rationale_defaults_to_analyze(tmp_path: Path) -> None:
    prompter = ScriptedPrompter(
        # agent, analyze_provider, rationale_provider(default), scan
        selects=["claude", "openai", DEFAULT, "off"],
        # analyze_model(default), rationale_model(default)
        texts=[DEFAULT, DEFAULT],
        confirms=[True],  # final "Write these files?"
    )
    answers = prompt_for_init(tmp_path, preset_agent=None, prompter=prompter)

    assert answers.analyze_provider == "openai"
    assert answers.rationale_provider == "openai"
    assert answers.analyze_model == "gpt-4o"
    # Same provider → rationale model defaults to the analyze model.
    assert answers.rationale_model == "gpt-4o"


# ---------- 6. API key prompted once per unique provider --------------------


def test_api_key_prompted_once_when_shared(tmp_path: Path) -> None:
    prompter = ScriptedPrompter(
        selects=["claude", "openai", DEFAULT, "off"],
        texts=[DEFAULT, DEFAULT],
        passwords=["sk-shared"],
        confirms=[True],
    )
    answers = prompt_for_init(tmp_path, preset_agent=None, prompter=prompter)

    assert len(_kinds(prompter, "password")) == 1
    assert answers.api_keys == {"openai": "sk-shared"}


def test_api_key_prompted_twice_when_providers_differ(tmp_path: Path) -> None:
    prompter = ScriptedPrompter(
        selects=["claude", "openai", "deepseek", "off"],
        texts=[DEFAULT, DEFAULT],
        passwords=["sk-openai", "sk-deepseek"],
        confirms=[True],
    )
    answers = prompt_for_init(tmp_path, preset_agent=None, prompter=prompter)

    assert len(_kinds(prompter, "password")) == 2
    assert answers.api_keys == {"openai": "sk-openai", "deepseek": "sk-deepseek"}


def test_no_key_prompt_for_non_key_bearing_providers(tmp_path: Path) -> None:
    """ollama / claude-cli carry no key, so they never prompt one."""
    prompter = ScriptedPrompter(
        selects=["claude", "claude-cli", DEFAULT, "off"],
        texts=[DEFAULT, DEFAULT],
        confirms=[True],
    )
    answers = prompt_for_init(tmp_path, preset_agent=None, prompter=prompter)

    assert _kinds(prompter, "password") == []
    assert answers.api_keys == {}


# ---------- 7. token prompt only for github/auto ----------------------------


def test_token_not_prompted_when_scan_off(tmp_path: Path) -> None:
    prompter = ScriptedPrompter(
        selects=["claude", "claude-cli", DEFAULT, "off"],
        texts=[DEFAULT, DEFAULT],
        confirms=[True],
    )
    answers = prompt_for_init(tmp_path, preset_agent=None, prompter=prompter)

    assert _kinds(prompter, "password") == []
    assert answers.scan_token is None


def test_token_prompted_for_github(tmp_path: Path) -> None:
    prompter = ScriptedPrompter(
        selects=["claude", "claude-cli", DEFAULT, "github"],
        texts=[DEFAULT, DEFAULT],
        passwords=["ghp_real"],
        confirms=[True],
    )
    answers = prompt_for_init(tmp_path, preset_agent=None, prompter=prompter)

    assert len(_kinds(prompter, "password")) == 1
    assert answers.scan_provider == "github"
    assert answers.scan_token == "ghp_real"


# ---------- 8. overwrite gate -----------------------------------------------


def test_overwrite_gate_no_skips_config_prompts(tmp_path: Path) -> None:
    (tmp_path / "whygraph.toml").write_text("x = 1\n", encoding="utf-8")
    prompter = ScriptedPrompter(
        selects=["claude"],  # only the agent prompt should run
        confirms=[False, True],  # overwrite? No ; Write these files? Yes
    )
    answers = prompt_for_init(tmp_path, preset_agent=None, prompter=prompter)

    assert answers.reconfigure_toml is False
    assert answers.agent == "claude"
    # No LLM/scan selects — only the agent select happened.
    assert _kinds(prompter, "select") == ["Which agent?"]


def test_overwrite_gate_yes_runs_full_flow(tmp_path: Path) -> None:
    (tmp_path / "whygraph.toml").write_text("x = 1\n", encoding="utf-8")
    prompter = ScriptedPrompter(
        selects=["claude", "openai", DEFAULT, "off"],
        texts=[DEFAULT, DEFAULT],
        confirms=[True, True],  # overwrite? Yes ; Write? Yes
    )
    answers = prompt_for_init(tmp_path, preset_agent=None, prompter=prompter)

    assert answers.reconfigure_toml is True
    assert answers.analyze_provider == "openai"


def test_preset_agent_skips_agent_prompt(tmp_path: Path) -> None:
    prompter = ScriptedPrompter(
        selects=["openai", DEFAULT, "off"],  # no agent select
        texts=[DEFAULT, DEFAULT],
        confirms=[True],
    )
    answers = prompt_for_init(tmp_path, preset_agent="cursor", prompter=prompter)

    assert answers.agent == "cursor"
    assert "Which agent?" not in _kinds(prompter, "select")


# ---------- 9. abort paths --------------------------------------------------


def test_abort_on_ctrl_c_at_prompt(tmp_path: Path) -> None:
    prompter = ScriptedPrompter(selects=[ABORT])
    with pytest.raises(InitAborted):
        prompt_for_init(tmp_path, preset_agent=None, prompter=prompter)


def test_abort_on_declined_final_confirm(tmp_path: Path) -> None:
    prompter = ScriptedPrompter(
        selects=["claude", "openai", DEFAULT, "off"],
        texts=[DEFAULT, DEFAULT],
        confirms=[False],  # decline "Write these files?"
    )
    with pytest.raises(InitAborted):
        prompt_for_init(tmp_path, preset_agent=None, prompter=prompter)


# ---------- 9b. summary masks secrets ---------------------------------------


def test_render_summary_masks_secrets(tmp_path: Path) -> None:
    answers = InitAnswers(
        agent="claude",
        analyze_provider="anthropic",
        analyze_model="claude-opus-4-7",
        rationale_provider="anthropic",
        rationale_model="claude-opus-4-7",
        api_keys={"anthropic": "sk-secret"},
        scan_provider="github",
        scan_token="ghp-secret-token",
    )
    text = render_summary(
        answers,
        example_path=tmp_path / "whygraph.example.toml",
        user_path=tmp_path / "whygraph.toml",
        write_user=True,
    )
    # Never the raw values.
    assert "sk-secret" not in text
    assert "ghp-secret-token" not in text
    # Only status.
    assert "set (hidden)" in text


def test_render_summary_reports_env_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    answers = InitAnswers(
        analyze_provider="openai",
        analyze_model="gpt-4o",
        rationale_provider="openai",
        rationale_model="gpt-4o",
    )
    text = render_summary(
        answers,
        example_path=tmp_path / "whygraph.example.toml",
        user_path=tmp_path / "whygraph.toml",
        write_user=True,
    )
    assert "from $OPENAI_API_KEY" in text
    assert "sk-from-env" not in text


def test_summary_hook_receives_text(tmp_path: Path) -> None:
    seen: list[str] = []
    prompter = ScriptedPrompter(
        selects=["claude", "anthropic", DEFAULT, "off"],
        texts=[DEFAULT, DEFAULT],
        passwords=[""],  # blank anthropic key → env fallback
        confirms=[True],
    )
    prompt_for_init(
        tmp_path,
        preset_agent=None,
        prompter=prompter,
        on_summary=seen.append,
    )
    assert len(seen) == 1
    assert "Review" not in seen[0]  # the body only; the panel title is the command's
    assert "Analyze:" in seen[0]
