"""Tests for the ``whygraph.example.toml`` scaffold helpers.

Covers :func:`default_config_text` (the bundled template),
:func:`write_example_config` / :func:`write_user_config` (the writers used
by ``whygraph init``), and :func:`render_config` (the single renderer that
feeds both — including the byte-exact golden reproduction of the shipped
template and the secret-vs-comment line handling).
"""

from __future__ import annotations

from pathlib import Path

from whygraph.core.config import (
    EXAMPLE_CONFIG_FILENAME,
    DEFAULT_ANSWERS,
    Config,
    InitAnswers,
    default_config_text,
    render_config,
    write_example_config,
    write_user_config,
)

_GOLDEN = Path(__file__).parent / "fixtures" / "default_config_golden.toml"


def test_default_config_text_is_valid_and_matches_defaults(tmp_path: Path) -> None:
    """The bundled template parses and yields the same Config as no file."""
    config = tmp_path / "whygraph.toml"
    config.write_text(default_config_text(), encoding="utf-8")

    cfg = Config.from_toml(config)

    # An unedited copy behaves exactly as if no config were present.
    assert cfg.log_level == "INFO"
    assert cfg.scan_max_workers == 2
    assert cfg.analyze.provider == "anthropic"
    assert cfg.rationale.provider == "anthropic"
    # `[logging]` is commented out in the template, so file logging is off.
    assert cfg.logging.file is None


def test_write_example_config_creates_example_file(tmp_path: Path) -> None:
    path = write_example_config(tmp_path)

    assert path == tmp_path / EXAMPLE_CONFIG_FILENAME
    assert path.read_text(encoding="utf-8") == default_config_text()
    # The real config is never written — that's the user's copy to make.
    assert not (tmp_path / "whygraph.toml").exists()


def test_write_example_config_refreshes_existing(tmp_path: Path) -> None:
    """A second call overwrites a stale example with the current template."""
    existing = tmp_path / EXAMPLE_CONFIG_FILENAME
    existing.write_text("stale = true\n", encoding="utf-8")

    path = write_example_config(tmp_path)

    assert path == existing
    assert existing.read_text(encoding="utf-8") == default_config_text()


# ---------- render_config: golden + injection + round-trip -------------------


def test_render_defaults_matches_golden_byte_for_byte() -> None:
    """The unfilled render reproduces the shipped template exactly (§9.1)."""
    golden = _GOLDEN.read_text(encoding="utf-8")
    assert render_config(DEFAULT_ANSWERS, include_tokens=False) == golden
    assert default_config_text() == golden


def test_render_injects_non_default_provider_and_model() -> None:
    """Chosen analyze/rationale provider+model appear as active lines (§9.2)."""
    answers = InitAnswers(
        analyze_provider="openai",
        analyze_model="gpt-4o-mini",
        rationale_provider="deepseek",
        rationale_model="deepseek-reasoner",
    )
    out = render_config(answers, include_tokens=False)
    assert 'provider = "openai"' in out
    assert 'model = "gpt-4o-mini"' in out
    assert 'provider = "deepseek"' in out
    assert 'model = "deepseek-reasoner"' in out


def test_render_example_leaves_secret_lines_commented() -> None:
    """include_tokens=False keeps key/token lines as commented hints (§9.2)."""
    answers = InitAnswers(
        api_keys={"anthropic": "sk-ant-real"},
        scan_provider="github",
        scan_token="ghp_real",
    )
    out = render_config(answers, include_tokens=False)
    # Secrets never leak into the example.
    assert "sk-ant-real" not in out
    assert "ghp_real" not in out
    # The hints stay commented.
    assert '# api_key = "sk-ant-..."' in out
    assert '# token = "ghp_..."' in out


def test_render_user_emits_active_secret_lines_only_when_present() -> None:
    """include_tokens=True writes active lines for supplied secrets (§9.2)."""
    answers = InitAnswers(
        api_keys={"openai": "sk-openai-real"},
        scan_provider="github",
        scan_token="ghp_real",
    )
    out = render_config(answers, include_tokens=True)
    assert 'api_key = "sk-openai-real"' in out
    assert 'token = "ghp_real"' in out
    # A provider with no supplied key keeps its commented hint.
    assert '# api_key = "sk-ant-..."' in out


def test_write_user_config_round_trips(tmp_path: Path) -> None:
    """write_user_config output parses back to the chosen values (§9.3)."""
    answers = InitAnswers(
        analyze_provider="openai",
        analyze_model="gpt-4o",
        rationale_provider="anthropic",
        rationale_model="claude-opus-4-7",
        api_keys={"openai": "sk-openai-real", "anthropic": "sk-ant-real"},
        scan_provider="github",
        scan_token="ghp_real",
        reconfigure_toml=True,
    )
    path = write_user_config(tmp_path, answers)
    assert path == tmp_path / "whygraph.toml"

    cfg = Config.from_toml(path)
    assert cfg.analyze.provider == "openai"
    assert cfg.analyze.model == "gpt-4o"
    assert cfg.rationale.provider == "anthropic"
    assert cfg.rationale.model == "claude-opus-4-7"
    assert cfg.scan_provider == "github"
    assert cfg.scan_token == "ghp_real"
    assert cfg.llm.openai.api_key == "sk-openai-real"
    assert cfg.llm.anthropic.api_key == "sk-ant-real"


def test_render_claude_cli_tag_parses(tmp_path: Path) -> None:
    """provider written hyphenated; the [llm.claude_cli] header parses (§9.4)."""
    answers = InitAnswers(
        analyze_provider="claude-cli",
        analyze_model="claude-opus-4-7",
        rationale_provider="claude-cli",
        rationale_model="claude-opus-4-7",
        reconfigure_toml=True,
    )
    out = render_config(answers, include_tokens=True)
    assert 'provider = "claude-cli"' in out
    assert "[llm.claude_cli]" in out

    path = write_user_config(tmp_path, answers)
    cfg = Config.from_toml(path)
    assert cfg.analyze.provider == "claude-cli"
    assert cfg.rationale.provider == "claude-cli"
