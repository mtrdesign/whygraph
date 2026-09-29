"""Tests for config v2: ``from_dict``, ``normalize_v2`` / ``merge_v2``, and
``Config.model_for``.

The load-bearing property is back-compat: a 1.x ``whygraph.toml`` must
resolve to the same ``(provider, model, timeout)`` per task - and the same
rationale-cache key - as it did before v2, while the v2 keys
(``[llm].model``, ``[scan].forge``, ``[analyze].max_workers``) take over.
"""

from __future__ import annotations

import json
import logging
import tomllib
from pathlib import Path

import pytest

from whygraph.core import config as config_mod
from whygraph.core.config import (
    CHAT_PROVIDERS,
    KNOWN_PROVIDERS,
    AnalyzeConfig,
    Config,
    ConfigError,
    LlmConfig,
    ModelChoice,
    default_config_text,
    merge_v2,
    normalize_v2,
)


@pytest.fixture(autouse=True)
def _fresh_deprecations():
    """Each test sees the once-per-process deprecation log afresh."""
    config_mod._reset_deprecation_warnings()
    yield
    config_mod._reset_deprecation_warnings()


def _write(path: Path, body: str) -> Path:
    path.write_text(body)
    return path


def _from(body: str, base: Path) -> Config:
    return Config.from_dict(tomllib.loads(body), base)


def _deprecations(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if "deprecated" in r.getMessage()]


# A 1.x file exercising every legacy spelling, and its hand-written v2
# translation. The analyze task-level timeout moves to [llm.openai] - no
# other task uses openai, so the resolved timeouts stay equal.
_OLD = """
log_level = "DEBUG"
whygraph_db = ".whygraph/custom.db"

[scan]
max_workers = 4
provider = "github"
remote = "upstream"

[analyze]
provider = "openai"
timeout_sec = 30

[rationale]
provider = "openrouter"
model = "openai/gpt-4o"

[chat]
provider = "deepseek"

[llm.openai]
model = "gpt-4o-mini"

[llm.deepseek]
model = "deepseek-reasoner"

[llm.openrouter]
timeout_sec = 90

[llm.claude-cli]
timeout_sec = 300
"""

_V2 = """
log_level = "DEBUG"
whygraph_db = ".whygraph/custom.db"

[scan]
forge = "github"
remote = "upstream"

[analyze]
max_workers = 4
model = "openai/gpt-4o-mini"

[rationale]
model = "openrouter/openai/gpt-4o"

[llm]
model = "deepseek/deepseek-reasoner"

[llm.openai]
timeout_sec = 30

[llm.openrouter]
timeout_sec = 90

[llm.claude_cli]
timeout_sec = 300
"""


# ---- from_toml == from_dict(normalize_v2(load(...))) ----------------------


@pytest.mark.parametrize("body", [_OLD, _V2], ids=["v1", "v2"])
def test_from_toml_equals_from_dict_of_normalized(tmp_path: Path, body: str) -> None:
    path = _write(tmp_path / "whygraph.toml", body)
    with path.open("rb") as f:
        raw = tomllib.load(f)

    normalized, _ = normalize_v2(raw, tmp_path)

    assert Config.from_toml(path) == Config.from_dict(normalized, tmp_path)


def test_old_fixture_and_v2_translation_resolve_identically(tmp_path: Path) -> None:
    old = _from(_OLD, tmp_path)
    new = _from(_V2, tmp_path)

    expected = {
        "analyze": ("openai", "gpt-4o-mini", 30),
        "rationale": ("openrouter", "openai/gpt-4o", 90),
        "chat": ("deepseek", "deepseek-reasoner", 60),
    }
    for task, triple in expected.items():
        for cfg in (old, new):
            assert (*cfg.model_for(task), cfg.timeout_for(task)) == triple, task
    assert old.cache_identity("rationale") == new.cache_identity("rationale")
    assert old.scan_forge == new.scan_forge == "github"
    assert old.analyze.max_workers == new.analyze.max_workers == 4
    assert old.whygraph_db == new.whygraph_db == tmp_path / ".whygraph/custom.db"
    assert old.llm.claude_cli.timeout_sec == new.llm.claude_cli.timeout_sec == 300


def test_defaults_resolve_to_the_1x_pairs() -> None:
    cfg = Config.defaults()

    assert cfg.model_for("analyze") == ModelChoice("anthropic", "claude-opus-4-7")
    assert cfg.model_for("rationale") == ("anthropic", "claude-opus-4-7")
    assert cfg.model_for("chat") == ("anthropic", "claude-opus-4-7")
    assert cfg.timeout_for("analyze") == 60
    assert cfg.cache_identity("rationale") == ("anthropic", None)


def test_model_for_rejects_an_unknown_task() -> None:
    with pytest.raises(ValueError, match="unknown task"):
        Config.defaults().model_for("describe")


# ---- precedence -----------------------------------------------------------


def test_legacy_openrouter_provider_keeps_slashed_model_whole(tmp_path: Path) -> None:
    cfg = _from(
        '[rationale]\nprovider = "openrouter"\nmodel = "openai/gpt-4o"\n', tmp_path
    )

    assert cfg.model_for("rationale") == ("openrouter", "openai/gpt-4o")
    assert cfg.cache_identity("rationale") == ("openrouter", "openai/gpt-4o")


def test_llm_model_splits_on_the_first_slash(tmp_path: Path) -> None:
    cfg = _from('[llm]\nmodel = "openrouter/openrouter/auto"\n', tmp_path)

    for task in ("analyze", "rationale", "chat"):
        assert cfg.model_for(task) == ("openrouter", "openrouter/auto")


def test_task_model_with_known_prefix_splits_when_no_provider(tmp_path: Path) -> None:
    cfg = _from('[analyze]\nmodel = "deepseek/deepseek-chat"\n', tmp_path)

    assert cfg.model_for("analyze") == ("deepseek", "deepseek-chat")
    # The other tasks are untouched.
    assert cfg.model_for("rationale") == ("anthropic", "claude-opus-4-7")


def test_task_model_with_unknown_prefix_is_not_split(tmp_path: Path) -> None:
    cfg = _from('[analyze]\nmodel = "acme/model-x"\n', tmp_path)

    assert cfg.model_for("analyze") == ("anthropic", "acme/model-x")


def test_task_model_without_prefix_inherits_llm_provider(tmp_path: Path) -> None:
    cfg = _from(
        '[llm]\nmodel = "openai/gpt-4o"\n[analyze]\nmodel = "gpt-4o-mini"\n', tmp_path
    )

    assert cfg.model_for("analyze") == ("openai", "gpt-4o-mini")
    assert cfg.model_for("rationale") == ("openai", "gpt-4o")


def test_task_provider_beats_llm_model_provider(tmp_path: Path) -> None:
    """[llm].model only supplies the model when its provider is the one used."""
    cfg = _from(
        '[llm]\nmodel = "openai/gpt-4o"\n[rationale]\nprovider = "deepseek"\n', tmp_path
    )

    assert cfg.model_for("rationale") == ("deepseek", "deepseek-chat")


def test_llm_model_beats_deprecated_provider_table_model(tmp_path: Path) -> None:
    cfg = _from(
        '[llm]\nmodel = "anthropic/claude-sonnet-4-5"\n'
        '[llm.anthropic]\nmodel = "claude-haiku-4-5"\n',
        tmp_path,
    )

    assert cfg.model_for("analyze") == ("anthropic", "claude-sonnet-4-5")


def test_claude_cli_spellings_canonicalize(tmp_path: Path) -> None:
    cfg = _from('[llm]\nmodel = "claude_cli/claude-opus-4-7"\n', tmp_path)

    assert cfg.model_for("analyze") == ("claude-cli", "claude-opus-4-7")
    assert cfg.timeout_for("analyze") == 120


def test_llm_model_must_be_provider_slash_model(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="llm.model"):
        _from('[llm]\nmodel = "gpt-4o"\n', tmp_path)


def test_empty_model_strings_normalize_to_none(tmp_path: Path) -> None:
    cfg = _from('[llm]\nmodel = ""\n[chat]\nmodel = "  "\n', tmp_path)

    assert cfg.llm.model is None
    assert cfg.chat.model is None
    assert cfg.model_for("chat") == ("anthropic", "claude-opus-4-7")


def test_task_timeout_still_wins_and_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="whygraph.core.config"):
        cfg = _from("[analyze]\ntimeout_sec = 15\n", tmp_path)

    assert cfg.timeout_for("analyze") == 15
    assert cfg.timeout_for("rationale") == 60
    assert any("[analyze].timeout_sec" in m for m in _deprecations(caplog))


# ---- chat -----------------------------------------------------------------


def test_chat_refuses_an_explicit_non_chat_provider(tmp_path: Path) -> None:
    for body in ('[chat]\nprovider = "ollama"\n', '[chat]\nmodel = "claude-cli/x"\n'):
        cfg = _from(body, tmp_path)
        with pytest.raises(ConfigError, match="not a chat provider"):
            cfg.model_for("chat")


def test_chat_falls_back_when_non_chat_provider_is_inherited(tmp_path: Path) -> None:
    cfg = _from('[llm]\nmodel = "ollama/llama3"\n', tmp_path)

    assert cfg.model_for("analyze") == ("ollama", "llama3")
    assert cfg.model_for("chat") == ("anthropic", "claude-opus-4-7")


def test_chat_for_an_explicit_provider(tmp_path: Path) -> None:
    cfg = _from(
        '[chat]\nprovider = "deepseek"\nmodel = "deepseek-reasoner"\n', tmp_path
    )

    # The task's own model belongs to its own provider only.
    assert cfg.model_for("chat", provider="deepseek") == (
        "deepseek",
        "deepseek-reasoner",
    )
    assert cfg.model_for("chat", provider="openai") == ("openai", "gpt-4o")
    with pytest.raises(ConfigError, match="not a chat provider"):
        cfg.model_for("chat", provider="ollama")


def test_core_provider_lists_mirror_services() -> None:
    from whygraph.services.llm import CHAT_PROVIDERS as SERVICE_CHAT
    from whygraph.services.llm import LlmClientFactory

    assert set(KNOWN_PROVIDERS) == set(LlmClientFactory.BUILTIN_PROVIDERS)
    assert tuple(CHAT_PROVIDERS) == tuple(SERVICE_CHAT)


# ---- rationale-cache identity ---------------------------------------------


def test_cache_identity_follows_llm_model(tmp_path: Path) -> None:
    unpinned = Config.defaults().cache_identity("rationale")
    pinned = _from('[llm]\nmodel = "anthropic/claude-opus-4-7"\n', tmp_path)
    changed = _from('[llm]\nmodel = "anthropic/claude-sonnet-4-5"\n', tmp_path)

    assert unpinned == ("anthropic", None)
    assert pinned.cache_identity("rationale") == ("anthropic", "claude-opus-4-7")
    assert changed.cache_identity("rationale") == ("anthropic", "claude-sonnet-4-5")


def test_cache_identity_ignores_a_deprecated_provider_table_pin(tmp_path: Path) -> None:
    """1.x keyed a `[llm.<provider>].model`-only file under "default"."""
    cfg = _from('[llm.anthropic]\nmodel = "claude-sonnet-4-5"\n', tmp_path)

    assert cfg.model_for("rationale") == ("anthropic", "claude-sonnet-4-5")
    assert cfg.cache_identity("rationale") == ("anthropic", None)


def test_cache_identity_matches_1x_for_task_pins(tmp_path: Path) -> None:
    cfg = _from(
        '[rationale]\nprovider = "claude-cli"\n[analyze]\nmodel = "claude-haiku-4-5"\n',
        tmp_path,
    )

    assert cfg.cache_identity("rationale") == ("claude-cli", None)
    assert cfg.cache_identity("analyze") == ("anthropic", "claude-haiku-4-5")


# ---- normalize_v2 / merge_v2 ----------------------------------------------


def test_normalize_maps_aliases_and_reports_them(tmp_path: Path) -> None:
    raw = {"scan": {"provider": "github", "max_workers": 3}}

    normalized, warnings = normalize_v2(raw, tmp_path)

    assert normalized == {"scan": {"forge": "github"}, "analyze": {"max_workers": 3}}
    assert len(warnings) == 2
    # Pure: the input is untouched.
    assert raw == {"scan": {"provider": "github", "max_workers": 3}}


def test_new_key_wins_over_alias_in_one_layer(tmp_path: Path) -> None:
    raw = {
        "scan": {"provider": "github", "forge": "auto", "max_workers": 3},
        "analyze": {"max_workers": 5},
    }

    normalized, warnings = normalize_v2(raw, tmp_path)

    assert normalized["scan"] == {"forge": "auto"}
    assert normalized["analyze"] == {"max_workers": 5}
    assert all("ignored" in w for w in warnings) and len(warnings) == 2


def test_normalize_resolves_paths_against_base(tmp_path: Path) -> None:
    raw = {
        "whygraph_db": "db/w.db",
        "codegraph_db": "/abs/c.db",
        "logging": {"file": "logs/x.log"},
        "llm": {"claude-cli": {"config_dir": "profile"}},
    }

    normalized, _ = normalize_v2(raw, tmp_path)

    assert normalized["whygraph_db"] == str((tmp_path / "db/w.db").resolve())
    assert normalized["codegraph_db"] == "/abs/c.db"
    assert normalized["logging"]["file"] == str((tmp_path / "logs/x.log").resolve())
    assert normalized["llm"] == {
        "claude_cli": {"config_dir": str((tmp_path / "profile").resolve())}
    }


def test_merge_rules(tmp_path: Path) -> None:
    lower, _ = normalize_v2(
        {
            "log_level": "DEBUG",
            "llm": {"model": "openai/gpt-4o", "openai": {"timeout_sec": 30}},
            "scan": {"hooks": ["post-commit", "post-merge"], "remote": "upstream"},
            "analyze": {"max_diff_chars": 10},
        },
        tmp_path,
    )
    upper, _ = normalize_v2(
        {
            "llm": {"openai": {"base_url": "http://gw"}},
            "scan": {"hooks": ["post-checkout"]},
            "analyze": {"max_diff_chars": None},
            "log_level": None,
        },
        tmp_path,
    )

    merged = merge_v2(lower, upper)
    cfg = Config.from_dict(merged, tmp_path)

    # Dicts merge key by key; an absent key inherits.
    assert cfg.llm.model == "openai/gpt-4o"
    assert cfg.llm.openai.timeout_sec == 30
    assert cfg.llm.openai.base_url == "http://gw"
    assert cfg.scan_remote == "upstream"
    # Lists replace, never concatenate.
    assert cfg.scan_hooks == ("post-checkout",)
    # null resets to the default.
    assert cfg.analyze.max_diff_chars == AnalyzeConfig().max_diff_chars
    assert cfg.log_level == "INFO"
    # Inputs are not mutated.
    assert lower["scan"]["hooks"] == ["post-commit", "post-merge"]


def test_aliases_on_two_layers_resolve_to_new_key_with_one_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    layers = [
        {"scan": {"provider": "github", "max_workers": 3}},
        {"scan": {"provider": "auto", "max_workers": 6}},
    ]

    with caplog.at_level(logging.WARNING, logger="whygraph.core.config"):
        normalized = []
        for layer in layers:
            data, warnings = normalize_v2(layer, tmp_path)
            config_mod._warn_deprecated(warnings)
            normalized.append(data)
        merged = merge_v2(*normalized)
        cfg = Config.from_dict(merged, tmp_path)

    assert merged == {"scan": {"forge": "auto"}, "analyze": {"max_workers": 6}}
    assert cfg.scan_forge == "auto"
    assert cfg.analyze.max_workers == 6
    messages = _deprecations(caplog)
    assert sum("[scan].provider" in m for m in messages) == 1
    assert sum("[scan].max_workers" in m for m in messages) == 1


def test_alias_on_one_layer_and_new_key_on_another(tmp_path: Path) -> None:
    """Per-layer normalization: the higher layer wins, under the new key."""
    lower, _ = normalize_v2({"analyze": {"max_workers": 8}}, tmp_path)
    upper, _ = normalize_v2({"scan": {"max_workers": 2}}, tmp_path)

    assert Config.from_dict(merge_v2(lower, upper), tmp_path).analyze.max_workers == 2
    assert Config.from_dict(merge_v2(upper, lower), tmp_path).analyze.max_workers == 8


def test_merged_dict_survives_a_json_round_trip(tmp_path: Path) -> None:
    """What a child scan receives as WHYGRAPH_CONFIG_JSON builds the same Config."""
    normalized, _ = normalize_v2(tomllib.loads(_OLD), tmp_path)
    shipped = json.loads(json.dumps(merge_v2(normalized)))

    elsewhere = tmp_path / "other-cwd"
    elsewhere.mkdir()
    assert Config.from_dict(shipped, elsewhere) == Config.from_dict(
        normalized, tmp_path
    )


def test_from_dict_does_not_mutate_its_input(tmp_path: Path) -> None:
    raw = tomllib.loads(_OLD)
    snapshot = json.loads(json.dumps(raw))

    first = Config.from_dict(raw, tmp_path)
    second = Config.from_dict(raw, tmp_path)

    assert raw == snapshot
    assert first == second


# ---- deprecation warnings -------------------------------------------------


def test_deprecations_warn_once_per_process(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _write(tmp_path / "whygraph.toml", _OLD)

    with caplog.at_level(logging.WARNING, logger="whygraph.core.config"):
        Config.from_toml(path)
        Config.from_toml(path)

    messages = _deprecations(caplog)
    assert len(messages) == len(set(messages))
    assert {m.split(" ")[0] for m in messages} == {
        "[scan].provider",
        "[scan].max_workers",
        "[analyze].timeout_sec",
        "[llm.openai].model",
        "[llm.deepseek].model",
    }


def test_v2_file_and_bundled_template_emit_no_deprecation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="whygraph.core.config"):
        _from(_V2, tmp_path)
        _from(default_config_text(), tmp_path)

    assert _deprecations(caplog) == []


# ---- the LLM factories honour [llm].model ---------------------------------


def test_client_factory_default_model_follows_llm_model() -> None:
    from whygraph.services.llm import LlmClientFactory

    factory = LlmClientFactory(config=LlmConfig(model="openai/gpt-4o-mini"))

    assert factory.make("openai", client=object()).model == "gpt-4o-mini"
    assert factory.make("deepseek", client=object()).model == "deepseek-chat"
    assert factory.make("openai", model="gpt-4o", client=object()).model == "gpt-4o"


def test_chat_client_default_model_follows_llm_model() -> None:
    from whygraph.services.llm import make_chat_client

    llm = LlmConfig(model="openrouter/openrouter/auto")

    assert make_chat_client("openrouter", config=llm, client=object()).model == (
        "openrouter/auto"
    )
    assert make_chat_client("openai", config=llm, client=object()).model == "gpt-4o"


def test_scan_panel_label_reads_model_for(tmp_path: Path) -> None:
    from whygraph.cli.commands.scan import _analyze_model_label

    assert _analyze_model_label(Config.defaults()) == "anthropic · claude-opus-4-7"
    cfg = _from('[llm]\nmodel = "deepseek/deepseek-reasoner"\n', tmp_path)
    assert _analyze_model_label(cfg) == "deepseek · deepseek-reasoner"
