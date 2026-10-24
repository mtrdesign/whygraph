"""The portal's scan child - ``python -m whygraph.scan.child [flags]``.

The portal runs every scan as this child process, with the repository as
``cwd`` and the project's config in ``WHYGRAPH_CONFIG_JSON``. No user types
it: it refuses to run without that variable (exit ``2``), and a full scan
also needs the config's ``whygraph_db``.

Flags (the runner passes at most the first three):

``--skip-analyze``
    Skip the LLM-descriptions phase; the descriptor probe is bypassed too.
``--no-remote``
    Skip the GitHub crawl and PR-origin recovery (offline, token-free).
``--codegraph-only``
    Refresh only the CodeGraph index (a linked project has no WhyGraph
    DB); a CodeGraph failure fails the run.
``--no-codegraph``
    Hidden test seam: skip the CodeGraph refresh of a full scan.

Progress is always JSON lines on stdout (:mod:`whygraph.scan.json_progress`),
ending in a ``result`` event; the logs go to stderr at ``INFO``, which the
runner keeps as the run log. Exit status: ``0`` ok, ``1`` a crawler (or the
config) failed, ``2`` refused.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .authors import AuthorResolver
from .codegraph_crawler import CodeGraphCrawler
from .git_crawler import GitCrawler
from .github_crawler import GitHubCrawler
from .json_progress import JsonProgress, JsonUsageSink
from .pr_origin_enricher import PROriginEnricher

if TYPE_CHECKING:
    from whygraph.services.git import Repository
    from whygraph.services.github import GitHubClient

    from .crawler import Crawler

# Named explicitly: under ``python -m`` this module's ``__name__`` is
# ``__main__``, which is outside the ``whygraph`` logger the child configures.
_log = logging.getLogger("whygraph.scan.child")

_LOGGER_NAME = "whygraph"
_HANDLER_FLAG = "_whygraph_scan_child"
"""Attribute that marks the stderr handler :func:`_configure_logging` adds."""


def main(argv: list[str] | None = None) -> int:
    """Run one portal scan and return the process exit status.

    Parameters
    ----------
    argv : list of str, optional
        The arguments after the module name; ``None`` reads ``sys.argv``.

    Returns
    -------
    int
        ``0`` when every crawler succeeded, ``1`` when one failed or the
        config is malformed, ``2`` when the child refuses to run (no
        ``WHYGRAPH_CONFIG_JSON``, or a full scan with no ``whygraph_db``).
    """
    from whygraph.core import CONFIG_JSON_ENV, get_config
    from whygraph.core.config import ConfigError

    args = _parse_args(argv)

    if not os.environ.get(CONFIG_JSON_ENV, "").strip():
        print(
            f"whygraph.scan.child: {CONFIG_JSON_ENV} is not set; this is the "
            "portal's scan child, scan from the portal instead",
            file=sys.stderr,
        )
        return 2
    try:
        config = get_config()
    except ConfigError as exc:
        print(f"whygraph.scan.child: invalid config: {exc}", file=sys.stderr)
        return 1
    if not args.codegraph_only and config.whygraph_db is None:
        print(
            "whygraph.scan.child: the config names no whygraph_db; "
            "a full scan needs one",
            file=sys.stderr,
        )
        return 2

    _log.info(
        "scan starting: mode=%s flags=%s",
        "codegraph-only" if args.codegraph_only else "full",
        " ".join(_flag_list(args)) or "(none)",
    )
    t0 = time.monotonic()
    if args.codegraph_only:
        status = _scan_codegraph_only()
    else:
        status = _scan(
            skip_analyze=args.skip_analyze,
            remote=args.remote,
            refresh_codegraph=args.refresh_codegraph,
        )
    _log.info(
        "scan finished: status=%s elapsed=%.1fs",
        "ok" if status == 0 else "failed",
        time.monotonic() - t0,
    )
    return status


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the child's flags (``argparse`` exits ``2`` on an unknown one)."""
    parser = argparse.ArgumentParser(
        prog="python -m whygraph.scan.child",
        description="The WhyGraph portal's scan child.",
    )
    parser.add_argument("--skip-analyze", dest="skip_analyze", action="store_true")
    parser.add_argument("--no-remote", dest="remote", action="store_false")
    parser.add_argument("--codegraph-only", dest="codegraph_only", action="store_true")
    parser.add_argument(
        "--no-codegraph",
        dest="refresh_codegraph",
        action="store_false",
        help=argparse.SUPPRESS,
    )
    return parser.parse_args(argv)


def _flag_list(args: argparse.Namespace) -> list[str]:
    """The flags ``args`` was given, in a fixed order, for the start log line."""
    flags = []
    if args.skip_analyze:
        flags.append("--skip-analyze")
    if not args.remote:
        flags.append("--no-remote")
    if args.codegraph_only:
        flags.append("--codegraph-only")
    if not args.refresh_codegraph:
        flags.append("--no-codegraph")
    return flags


def _configure_logging() -> None:
    """Send the ``whygraph`` logger's ``INFO`` records to stderr, once.

    Idempotent: one marked :class:`logging.StreamHandler` at most. Nothing
    is attached to the root logger, so httpx and alembic ``INFO`` lines stay
    out of the run log. Called only from the ``__main__`` block, so
    in-process :func:`main` calls never add handlers.
    """
    from whygraph.core.logger import _FILE_FORMAT

    logger = logging.getLogger(_LOGGER_NAME)
    if not any(getattr(h, _HANDLER_FLAG, False) for h in logger.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(_FILE_FORMAT))
        handler.setLevel(logging.INFO)
        setattr(handler, _HANDLER_FLAG, True)
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def _scan(*, skip_analyze: bool, remote: bool, refresh_codegraph: bool) -> int:
    """Run the full, phased scan and emit its events; return the exit status."""
    from whygraph.analyze import LlmDescriptor
    from whygraph.core import get_config
    from whygraph.core.usage import use_usage_sink
    from whygraph.db import ensure_initialized
    from whygraph.services.git import Repository
    from whygraph.services.llm import LlmError

    from . import AnalyzeCrawler

    ensure_initialized()
    config = get_config()
    repository = Repository(
        Path.cwd(),
        origin_remote=config.scan_remote,
        default_branch=config.scan_default_branch,
    )
    github_client = (
        _select_github_client(config.scan_forge, repository) if remote else None
    )

    if skip_analyze:
        # Bypass the LlmDescriptor probe entirely so a broken `[analyze]`
        # config still allows a fast scan; descriptions backfill lazily.
        descriptor = None
        analyze_skip: str | None = "--skip-analyze"
    else:
        try:
            descriptor = LlmDescriptor.from_config(config)
            analyze_skip = None
        except LlmError as exc:
            descriptor = None
            analyze_skip = str(exc)

    # Which optional phases have work - decided up front so the phases are
    # numbered against the count of phases that actually run.
    run_pr_origins = github_client is not None
    run_analyze = descriptor is not None
    # Author resolution always runs: local-only, no network, no token, so it
    # is valid under --no-remote and for the auto-rescan git hooks.
    phase_total = 1 + int(run_pr_origins) + 1 + int(run_analyze)
    phase_titles = [
        "Structural crawl",
        *(["PR-origin recovery"] if run_pr_origins else []),
        "Author identity",
        *(["LLM descriptions"] if run_analyze else []),
    ]

    phase_timings: dict[str, float] = {}
    ran: list[Crawler] = []
    scan_t0 = time.monotonic()
    # The usage sink is bound before any crawler is built, because crawler
    # threads snapshot the context at construction: each LLM call becomes a
    # `usage` event the runner writes to the ledger.
    with JsonProgress() as progress, use_usage_sink(JsonUsageSink(progress)):
        progress.start_event(phase_total, phase_titles)

        # CodeGraph refresh - a background crawler. It writes .codegraph/
        # and has no data dependency on the WhyGraph DB, so it overlaps the
        # entire crawl (started before Phase 1, joined last). Best-effort:
        # failures land on .warning, not .error.
        codegraph_crawler = (
            CodeGraphCrawler(
                progress,
                project_root=repository.root,
                image=None,
                require_lock_dir=True,
            )
            if refresh_codegraph
            else None
        )
        if codegraph_crawler is not None:
            codegraph_crawler.start()

        n = 0

        # Phase 1 - Structural crawl: git + GitHub, concurrent.
        n += 1
        progress.phase(n, "Structural crawl")
        t0 = time.monotonic()
        phase1: list[Crawler] = [GitCrawler(progress, repository=repository)]
        if github_client is not None:
            phase1.append(GitHubCrawler(progress, client=github_client))
        ran += phase1
        for c in phase1:
            c.start()
        for c in phase1:
            c.join()
        phase_timings["Structural crawl"] = time.monotonic() - t0

        # Phase 2 - PR-origin recovery: needs Phase 1's git + PR rows. Gated
        # on a resolved client, which is None under --no-remote.
        if run_pr_origins:
            n += 1
            progress.phase(n, "PR-origin recovery")
            t0 = time.monotonic()
            enricher = PROriginEnricher(
                progress,
                repository=repository,
                min_commits=config.analyze.pr_origin_min_commits,
                large_commit_file_count=config.analyze.large_commit_file_count,
            )
            ran.append(enricher)
            enricher.start()
            enricher.join()
            phase_timings["PR-origin recovery"] = time.monotonic() - t0

        # Phase 3 - Author identity: needs Phase 1's commits AND Phase 2's
        # PR-origin rows; an address can appear ONLY in on_default_branch=0
        # commits, so running before Phase 2 would miss that identity.
        n += 1
        progress.phase(n, "Author identity")
        t0 = time.monotonic()
        resolver = AuthorResolver(progress, repository=repository)
        ran.append(resolver)
        resolver.start()
        resolver.join()
        phase_timings["Author identity"] = time.monotonic() - t0

        # Phase 4 - LLM descriptions: the slow, token-heavy long pole, run
        # strictly last and alone. It describes commits reachable from the
        # current branch (reachability, not on_default_branch), so PR-origin
        # rows stay lazy because their objects are unreachable.
        if run_analyze:
            n += 1
            progress.phase(n, "LLM descriptions")
            t0 = time.monotonic()
            analyzer = AnalyzeCrawler(
                progress,
                repository=repository,
                descriptor=descriptor,
                max_workers=config.analyze.max_workers,
                large_commit_file_count=config.analyze.large_commit_file_count,
            )
            ran.append(analyzer)
            analyzer.start()
            analyzer.join()
            phase_timings["LLM descriptions"] = time.monotonic() - t0

        if codegraph_crawler is not None:
            codegraph_crawler.join()

        crawlers = list(ran)
        if codegraph_crawler is not None:
            crawlers.append(codegraph_crawler)
        progress.flush()
        progress.emit(
            _result_event(
                crawlers=crawlers,
                phase_timings=phase_timings,
                total_elapsed=time.monotonic() - scan_t0,
                analyze_skip=analyze_skip,
                codegraph=_codegraph_summary(codegraph_crawler, repository.root),
            )
        )

    failed = [c for c in crawlers if c.error is not None]
    for c in failed:
        print(f"crawler {c.name!r} failed: {c.error}", file=sys.stderr)
    return 1 if failed else 0


def _scan_codegraph_only() -> int:
    """Refresh the CodeGraph index and nothing else (``--codegraph-only``).

    Never opens or creates the WhyGraph DB. Unlike a full scan, a CodeGraph
    failure is the run's failure: exit ``1`` with the usual ``result`` event
    (``status: failed``).
    """
    from whygraph.core import _resolve_root
    from whygraph.core.usage import use_usage_sink

    root = _resolve_root()
    t0 = time.monotonic()
    with JsonProgress() as progress, use_usage_sink(JsonUsageSink(progress)):
        progress.start_event(1, ["Code index"])
        progress.phase(1, "CodeGraph")
        crawler = CodeGraphCrawler(
            progress,
            project_root=root,
            image=None,
            strict=True,
            require_lock_dir=True,
        )
        crawler.start()
        crawler.join()

        elapsed = time.monotonic() - t0
        progress.flush()
        progress.emit(
            _result_event(
                crawlers=[crawler],
                phase_timings={"CodeGraph": elapsed},
                total_elapsed=elapsed,
                analyze_skip="--codegraph-only",
                codegraph=_codegraph_summary(crawler, root),
            )
        )
    if crawler.error is not None:
        print(f"crawler {crawler.name!r} failed: {crawler.error}", file=sys.stderr)
        return 1
    return 0


def _codegraph_summary(crawler: Crawler | None, root: Path) -> dict:
    """The ``codegraph`` block of the ``result`` event (the run's ``summary.codegraph``).

    ``{"status": "skipped"}`` when the step did not run;
    ``{"status": "failed", "error": ...}`` when it failed, including a
    non-strict failure the scan itself survives (its ``warning``);
    ``{"status": "ok", "files": n}`` otherwise, ``n`` being the indexed
    file count (``None`` when it cannot be read). The portal reads it to
    tell whether the index is current after the run.
    """
    if crawler is None:
        return {"status": "skipped"}
    error = (
        crawler.error
        if crawler.error is not None
        else getattr(crawler, "warning", None)
    )
    if error:
        return {"status": "failed", "error": str(error)}
    return {"status": "ok", "files": _index_file_count(root)}


def _index_file_count(root: Path) -> int | None:
    """``SELECT count(*) FROM files`` of ``root``'s CodeGraph index, read-only."""
    import sqlite3

    from whygraph.core.safe_paths import UnsafePathError, check_inside
    from whygraph.services.codegraph.paths import CODEGRAPH_DB_RELPATH

    try:
        db = check_inside(root, CODEGRAPH_DB_RELPATH)
        if not db.is_file():
            return None
        with contextlib.closing(
            sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        ) as conn:
            return int(conn.execute("SELECT count(*) FROM files").fetchone()[0])
    except (UnsafePathError, OSError, sqlite3.Error, TypeError, ValueError):
        return None


def _result_event(
    *,
    crawlers: list[Crawler],
    phase_timings: dict[str, float],
    total_elapsed: float,
    analyze_skip: str | None,
    codegraph: dict | None = None,
) -> dict:
    """Build the closing ``result`` event.

    Overall status, elapsed time, per-phase timings and one entry per
    crawler (status, summary, error / warning), plus the CodeGraph step's
    outcome (:func:`_codegraph_summary`).
    """
    entries = []
    for c in crawlers:
        entry: dict = {
            "name": c.name,
            "status": "failed" if c.error is not None else "ok",
            "summary": c.summary,
        }
        if c.error is not None:
            entry["error"] = str(c.error)
        warning = getattr(c, "warning", None)
        if warning:
            entry["warning"] = warning
        entries.append(entry)
    return {
        "type": "result",
        "status": "failed" if any(c.error is not None for c in crawlers) else "ok",
        "elapsed_sec": round(total_elapsed, 3),
        "phase_timings": {k: round(v, 3) for k, v in phase_timings.items()},
        "crawlers": entries,
        "analyze_skipped": analyze_skip,
        "codegraph": codegraph if codegraph is not None else {"status": "skipped"},
    }


def _select_github_client(provider: str, repository: Repository) -> GitHubClient | None:
    """Resolve the GitHub client for the configured ``[scan].forge``.

    Returns ``None`` when ``provider`` is ``"off"`` (remote crawling
    disabled). For ``"github"`` and ``"auto"`` it delegates to
    :meth:`GitHubClient.for_repository`, which inspects the repository's
    remote URL and returns ``None`` if it is not a GitHub remote - so a
    misconfigured ``"github"`` or a non-GitHub ``"auto"`` both degrade to
    "no crawl" rather than erroring.

    Parameters
    ----------
    provider : str
        The validated ``[scan].forge`` value (``"off"`` / ``"github"``
        / ``"auto"``).
    repository : Repository
        The repository whose remote URL is inspected.

    Returns
    -------
    GitHubClient or None
        A configured client, or ``None`` when crawling is off or the
        remote is not a recognized GitHub URL.
    """
    if provider == "off":
        return None
    from whygraph.services.github import GitHubClient

    return GitHubClient.for_repository(repository)


if __name__ == "__main__":
    _configure_logging()
    sys.exit(main())
