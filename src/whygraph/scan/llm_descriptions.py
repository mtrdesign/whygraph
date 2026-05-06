"""LLM-written commit-pair diff descriptions via the `claude` CLI."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from rich.progress import Progress, TaskID

from whygraph import llm_subprocess
from whygraph.llm_subprocess import LlmError, claude_cli_available
from whygraph.scan import db as db_module
from whygraph.scan import git as git_module

__all__ = [
    "DEFAULT_MODEL",
    "DEFAULT_MAX_DIFF_CHARS",
    "DEFAULT_TIMEOUT_SEC",
    "DEFAULT_MAX_WORKERS",
    "LlmConfig",
    "LlmError",
    "claude_cli_available",
    "commits_to_describe",
    "describe_pair",
    "get_pair_diff",
    "run_phase",
]

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_MAX_DIFF_CHARS = 50_000
DEFAULT_TIMEOUT_SEC = 120
DEFAULT_MAX_WORKERS = 4


@dataclass(frozen=True)
class LlmConfig:
    model: str = DEFAULT_MODEL
    max_diff_chars: int = DEFAULT_MAX_DIFF_CHARS
    timeout_sec: int = DEFAULT_TIMEOUT_SEC
    max_workers: int = DEFAULT_MAX_WORKERS
    # If None, ANTHROPIC_API_KEY is stripped from the subprocess env so
    # `claude` falls through to subscription billing. If set, the value is
    # exported to the subprocess as ANTHROPIC_API_KEY (API billing).
    anthropic_api_key: str | None = None


_PROMPT_TEMPLATE = """\
You produce a dense, unambiguous code-change description that another LLM will read later as context for downstream tasks (rationale generation, code review, change attribution).

Audience is an LLM, not a human. Optimize for token efficiency and exact reference.

Rules:
- Verbatim identifiers. Reproduce file paths, function/class/method/variable/flag/constant names exactly as in the diff. No paraphrasing or pluralizing ("get_user" stays "get_user").
- Concrete deltas only. Use before→after pairs and signatures: `renamed foo→bar in src/x.py`; `signature compute(x) → compute(x, scale=1.0)`; `removed import json from src/y.py`; `added field User.email: str (nullable)`. State numeric facts where present (counts, defaults, line counts).
- No hedging ("seems", "may", "appears"), no judgment ("better", "cleaner", "improves"), no invented rationale.
- Describe ONLY what the diff contains. Ignore any commit message, PR title, or issue link text that may appear inside the diff (e.g. in changelog edits) — do not parrot it.
- Be token-efficient while staying unambiguously readable to a later LLM consumer. Do not pad. Single block. No preamble, no trailing remarks.

Diff:
{diff}

Output only the description.
"""


def get_pair_diff(repo_root: Path, sha_a: str, sha_b: str) -> str:
    return git_module._run_git(repo_root, ["diff", sha_a, sha_b])


def commits_to_describe(
    db: db_module.Database,
    repo_root: Path,
    branch: str,
) -> list[tuple[str, str]]:
    shas = list(git_module.walk_first_parent(repo_root, branch))
    if len(shas) < 2:
        return []
    pairs = list(zip(shas[:-1], shas[1:], strict=True))
    candidate_shas = [sha for sha, _ in pairs]
    needs = db.commits_without_llm_description(candidate_shas)
    return [(sha, next_sha) for sha, next_sha in pairs if sha in needs]


def describe_pair(diff: str, config: LlmConfig) -> str:
    if len(diff) > config.max_diff_chars:
        omitted = len(diff) - config.max_diff_chars
        diff = diff[: config.max_diff_chars] + f"\n[truncated: {omitted} chars omitted]"
    prompt = _PROMPT_TEMPLATE.format(diff=diff)
    return llm_subprocess.invoke_claude(
        prompt,
        model=config.model,
        timeout_sec=config.timeout_sec,
        anthropic_api_key=config.anthropic_api_key,
    )


def _process_pair(
    repo_root: Path,
    config: LlmConfig,
    sha: str,
    next_sha: str,
) -> tuple[str, str | None, str | None]:
    """Worker body. Returns (sha, description_or_None, error_or_None)."""
    try:
        diff = get_pair_diff(repo_root, sha, next_sha)
        description = describe_pair(diff, config)
    except (git_module.GitError, LlmError) as exc:
        return sha, None, str(exc)
    return sha, description, None


def run_phase(
    db_path: Path,
    repo_root: Path,
    branch: str,
    config: LlmConfig,
    progress: Progress,
    task_id: TaskID,
) -> str:
    with db_module.Database(db_path) as db:
        pairs = commits_to_describe(db, repo_root, branch)

    total = len(pairs)
    progress.update(task_id, total=total or 1, completed=0, description="llm")
    if total == 0:
        progress.update(task_id, completed=1)
        return "0 to describe (all filled or no pairs)"

    workers = max(1, config.max_workers)
    described = 0
    failed = 0
    with (
        ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="whygraph-llm"
        ) as ex,
        db_module.Database(db_path) as db,
    ):
        futures = [
            ex.submit(_process_pair, repo_root, config, sha, next_sha)
            for sha, next_sha in pairs
        ]
        for fut in as_completed(futures):
            sha, description, error = fut.result()
            if error is not None:
                failed += 1
                progress.console.log(
                    f"[yellow][llm] {sha[:7]}: {error}[/yellow]"
                )
            else:
                assert description is not None
                db.set_llm_description(sha, description, config.model)
                described += 1
            progress.advance(task_id)

    summary = f"{described} described"
    if failed:
        summary += f", {failed} failed (will retry on next scan)"
    return summary
