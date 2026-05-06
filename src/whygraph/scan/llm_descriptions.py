"""LLM-written commit-pair diff descriptions via the `claude` CLI."""

from __future__ import annotations

import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from rich.progress import Progress, TaskID

from whygraph.scan import db as db_module
from whygraph.scan import git as git_module

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_MAX_DIFF_CHARS = 50_000
DEFAULT_TIMEOUT_SEC = 120
DEFAULT_MAX_WORKERS = 4

# Flags passed to every `claude --print` invocation. Trims the agent
# runtime of work the description prompt doesn't need: MCP servers, tool
# init, slash command/skill discovery, on-disk session persistence.
# Cuts cold start ~40-50% in this repo's measurements.
_LEAN_FLAGS: tuple[str, ...] = (
    "--strict-mcp-config",
    "--mcp-config",
    '{"mcpServers":{}}',
    "--tools",
    "",
    "--disable-slash-commands",
    "--no-session-persistence",
)


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


class LlmError(RuntimeError):
    pass


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


def claude_cli_available() -> bool:
    return shutil.which("claude") is not None


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
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    if config.anthropic_api_key:
        env["ANTHROPIC_API_KEY"] = config.anthropic_api_key
    try:
        result = subprocess.run(
            ["claude", "--print", "--model", config.model, *_LEAN_FLAGS],
            input=prompt,
            text=True,
            capture_output=True,
            check=False,
            timeout=config.timeout_sec,
            env=env,
        )
    except FileNotFoundError as exc:
        raise LlmError("claude CLI is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise LlmError(f"claude timed out after {config.timeout_sec}s") from exc
    if result.returncode != 0:
        stderr = (result.stderr or "").strip() or (result.stdout or "").strip()
        raise LlmError(f"claude exited {result.returncode}: {stderr}")
    text = (result.stdout or "").strip()
    if not text:
        raise LlmError("claude returned empty output")
    return text


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
