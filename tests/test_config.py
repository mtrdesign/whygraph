from __future__ import annotations

from pathlib import Path

from whygraph.config import (
    DEFAULT_MODEL,
    DEFAULT_TTL_DAYS,
    Config,
    find_codegraph_db,
    find_whygraph_db,
    load_config,
)


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


def test_find_codegraph_db_walks_up(tmp_path: Path) -> None:
    db = _touch(tmp_path / ".codegraph" / "codegraph.db")
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    assert find_codegraph_db(deep) == db


def test_find_codegraph_db_returns_none_when_absent(tmp_path: Path) -> None:
    assert find_codegraph_db(tmp_path) is None


def test_find_whygraph_db_walks_up(tmp_path: Path) -> None:
    db = _touch(tmp_path / ".whygraph" / "whygraph.db")
    deep = tmp_path / "x" / "y"
    deep.mkdir(parents=True)
    assert find_whygraph_db(deep) == db


def test_load_config_defaults_to_claude_cli_backend(tmp_path: Path) -> None:
    cfg = load_config(env={}, cwd=tmp_path)
    assert cfg.rationale_backend == "claude_cli"
    assert cfg.anthropic_api_key is None
    assert cfg.model == DEFAULT_MODEL
    assert cfg.evidence_ttl_seconds == DEFAULT_TTL_DAYS * 24 * 60 * 60


def test_load_config_key_alone_does_not_switch_to_api(
    tmp_path: Path,
) -> None:
    """ANTHROPIC_API_KEY must NOT auto-promote to the api backend.

    A stray key in the environment was silently switching users to the
    direct-API billing path; we now require an explicit
    WHYGRAPH_RATIONALE_BACKEND=api opt-in.
    """
    cfg = load_config(env={"ANTHROPIC_API_KEY": "sk-test"}, cwd=tmp_path)
    assert cfg.rationale_backend == "claude_cli"
    assert cfg.anthropic_api_key == "sk-test"  # still captured for `api` use


def test_load_config_explicit_api_with_key(tmp_path: Path) -> None:
    cfg = load_config(
        env={
            "ANTHROPIC_API_KEY": "sk-test",
            "WHYGRAPH_RATIONALE_BACKEND": "api",
        },
        cwd=tmp_path,
    )
    assert cfg.rationale_backend == "api"
    assert cfg.anthropic_api_key == "sk-test"


def test_load_config_explicit_api_backend(tmp_path: Path) -> None:
    cfg = load_config(env={"WHYGRAPH_RATIONALE_BACKEND": "api"}, cwd=tmp_path)
    assert cfg.rationale_backend == "api"


def test_load_config_ttl_overridable_via_env(tmp_path: Path) -> None:
    cfg = load_config(env={"WHYGRAPH_EVIDENCE_TTL_DAYS": "2"}, cwd=tmp_path)
    assert cfg.evidence_ttl_seconds == 2 * 24 * 60 * 60


def test_load_config_invalid_ttl_falls_back_to_default(tmp_path: Path) -> None:
    cfg = load_config(env={"WHYGRAPH_EVIDENCE_TTL_DAYS": "garbage"}, cwd=tmp_path)
    assert cfg.evidence_ttl_seconds == DEFAULT_TTL_DAYS * 24 * 60 * 60


def test_load_config_zero_ttl_falls_back_to_default(tmp_path: Path) -> None:
    cfg = load_config(env={"WHYGRAPH_EVIDENCE_TTL_DAYS": "0"}, cwd=tmp_path)
    assert cfg.evidence_ttl_seconds == DEFAULT_TTL_DAYS * 24 * 60 * 60


def test_load_config_whygraph_db_env_wins(tmp_path: Path) -> None:
    explicit = tmp_path / "elsewhere" / "wg.db"
    cfg = load_config(env={"WHYGRAPH_DB": str(explicit)}, cwd=tmp_path)
    assert cfg.whygraph_db_path == explicit


def test_load_config_whygraph_db_walks_up_when_present(tmp_path: Path) -> None:
    db = _touch(tmp_path / ".whygraph" / "whygraph.db")
    deep = tmp_path / "sub"
    deep.mkdir()
    cfg = load_config(env={}, cwd=deep)
    assert cfg.whygraph_db_path == db


def test_load_config_whygraph_db_falls_back_to_default_path(tmp_path: Path) -> None:
    cfg = load_config(env={}, cwd=tmp_path)
    assert cfg.whygraph_db_path == tmp_path / ".whygraph" / "whygraph.db"
    assert not cfg.whygraph_db_path.exists()


def test_load_config_codegraph_db_env_wins(tmp_path: Path) -> None:
    explicit = tmp_path / "external" / "cg.db"
    cfg = load_config(env={"CODEGRAPH_DB": str(explicit)}, cwd=tmp_path)
    assert cfg.codegraph_db_path == explicit


def test_load_config_codegraph_db_walks_up(tmp_path: Path) -> None:
    db = _touch(tmp_path / ".codegraph" / "codegraph.db")
    deep = tmp_path / "deep"
    deep.mkdir()
    cfg = load_config(env={}, cwd=deep)
    assert cfg.codegraph_db_path == db


def test_load_config_codegraph_db_none_when_absent(tmp_path: Path) -> None:
    cfg = load_config(env={}, cwd=tmp_path)
    assert cfg.codegraph_db_path is None


def test_load_config_model_overridable(tmp_path: Path) -> None:
    cfg = load_config(env={"WHYGRAPH_MODEL": "claude-opus-4-7"}, cwd=tmp_path)
    assert cfg.model == "claude-opus-4-7"


def test_config_is_frozen(tmp_path: Path) -> None:
    cfg = load_config(env={}, cwd=tmp_path)
    assert isinstance(cfg, Config)
    try:
        cfg.model = "other"  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("Config should be frozen")
