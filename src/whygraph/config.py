from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping

RationaleBackend = Literal["claude_cli", "api"]

CODEGRAPH_DIR = ".codegraph"
CODEGRAPH_DB_FILE = "codegraph.db"
WHYGRAPH_DIR = ".whygraph"
WHYGRAPH_DB_FILE = "whygraph.db"

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_TTL_DAYS = 14


@dataclass(frozen=True)
class Config:
    repo_root: Path
    whygraph_db_path: Path
    codegraph_db_path: Path | None
    anthropic_api_key: str | None
    model: str
    evidence_ttl_seconds: int
    rationale_backend: RationaleBackend


def _walk_up(start: Path, subdir: str, filename: str) -> Path | None:
    current = start.resolve()
    while True:
        candidate = current / subdir / filename
        if candidate.is_file():
            return candidate
        if current.parent == current:
            return None
        current = current.parent


def find_codegraph_db(start: Path) -> Path | None:
    return _walk_up(start, CODEGRAPH_DIR, CODEGRAPH_DB_FILE)


def find_whygraph_db(start: Path) -> Path | None:
    return _walk_up(start, WHYGRAPH_DIR, WHYGRAPH_DB_FILE)


def load_config(
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> Config:
    e = env if env is not None else os.environ
    repo_root = (cwd if cwd is not None else Path.cwd()).resolve()

    ttl_days_raw = e.get("WHYGRAPH_EVIDENCE_TTL_DAYS", "")
    try:
        ttl_days = int(ttl_days_raw)
    except ValueError:
        ttl_days = DEFAULT_TTL_DAYS
    if ttl_days <= 0:
        ttl_days = DEFAULT_TTL_DAYS

    whygraph_db_env = e.get("WHYGRAPH_DB")
    if whygraph_db_env:
        whygraph_db_path = Path(whygraph_db_env)
    else:
        whygraph_db_path = (
            find_whygraph_db(repo_root)
            or repo_root / WHYGRAPH_DIR / WHYGRAPH_DB_FILE
        )

    codegraph_db_env = e.get("CODEGRAPH_DB")
    if codegraph_db_env:
        codegraph_db_path: Path | None = Path(codegraph_db_env)
    else:
        codegraph_db_path = find_codegraph_db(repo_root)

    api_key = e.get("ANTHROPIC_API_KEY") or None
    backend_env = e.get("WHYGRAPH_RATIONALE_BACKEND")
    if backend_env in ("api", "claude_cli"):
        backend: RationaleBackend = backend_env  # type: ignore[assignment]
    elif api_key:
        backend = "api"
    else:
        backend = "claude_cli"

    return Config(
        repo_root=repo_root,
        whygraph_db_path=whygraph_db_path,
        codegraph_db_path=codegraph_db_path,
        anthropic_api_key=api_key,
        model=e.get("WHYGRAPH_MODEL") or DEFAULT_MODEL,
        evidence_ttl_seconds=ttl_days * 24 * 60 * 60,
        rationale_backend=backend,
    )
