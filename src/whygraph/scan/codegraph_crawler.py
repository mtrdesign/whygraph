"""CodeGraphCrawler — refresh the CodeGraph index alongside the crawl.

Runs ``codegraph init -i`` (first index) or ``codegraph sync -q``
(incremental) as one more :class:`Crawler` thread, so the index builds
concurrently with the git / GitHub / analyze crawlers instead of blocking
before them. CodeGraph writes ``.codegraph/`` and has no data dependency
on the WhyGraph DB, so it can safely overlap the entire scan.

The refresh is **best-effort**: only the MCP rationale / evidence tools
read the index, not the crawl. A :class:`CodeGraphBootstrapError` (tool
missing, non-zero exit) is therefore swallowed into :attr:`warning`
rather than failing the scan; any other exception propagates into the
base class's :attr:`Crawler.error` and surfaces as a real failure.

Subprocess output is captured (``capture=True``) rather than streamed so
it cannot corrupt the shared :class:`rich.progress.Progress` display; the
captured tail is folded into :attr:`warning` on failure.
"""

from __future__ import annotations

from pathlib import Path

from rich.progress import Progress

from whygraph.services.codegraph import (
    CodeGraphBootstrapError,
    refresh_codegraph_index,
    writer_lock,
)
from whygraph.services.codegraph.writer_lock import lock_dir
from whygraph.services.codegraph.paths import CODEGRAPH_DB_RELPATH

from .crawler import Crawler

WRITER_LOCK_WAIT_SEC: float = 660.0
"""How long the refresh waits for another CodeGraph writer on the same repository."""


class CodeGraphCrawler(Crawler):
    """Refresh ``<project_root>/.codegraph/codegraph.db`` concurrently.

    Drives an indeterminate (pulsing) progress task — CodeGraph reports no
    granular progress — and completes it cleanly on success. CodeGraph
    bootstrap failures are recorded on :attr:`warning` rather than
    :attr:`Crawler.error`, preserving the best-effort contract.

    Parameters
    ----------
    progress : rich.progress.Progress
        Shared Progress instance owned by the orchestrator.
    project_root : Path
        Repository root whose ``.codegraph/`` index is refreshed.
    image : str or None
        Docker image override for the CodeGraph fallback path; ``None``
        uses the pinned default. Ignored when a local ``codegraph`` binary
        is found.
    strict : bool, optional
        Record a :class:`CodeGraphBootstrapError` on :attr:`Crawler.error`
        (failing the scan) instead of :attr:`warning`. Used by
        the scan child's ``--codegraph-only``, where the index is the whole job.
    require_lock_dir : bool, optional
        A portal-managed child: refuse to run CodeGraph unless the writer
        lock directory is configured (``WHYGRAPH_CODEGRAPH_LOCK_DIR``), so
        it never writes the index unlocked. Default ``False`` (tests).

    Attributes
    ----------
    warning : str or None
        Message describing a swallowed :class:`CodeGraphBootstrapError`,
        for the orchestrator to surface after the crawl. ``None`` on
        success.
    """

    def __init__(
        self,
        progress: Progress,
        *,
        project_root: Path,
        image: str | None,
        strict: bool = False,
        require_lock_dir: bool = False,
    ) -> None:
        super().__init__("codegraph", progress, total=None)
        self._project_root = project_root
        self._image = image
        self._strict = strict
        self._require_lock_dir = require_lock_dir
        self.warning: str | None = None

    def work(self) -> None:
        db_path = self._project_root / CODEGRAPH_DB_RELPATH
        verb = "sync" if db_path.exists() else "init -i"
        self.advance(0, description=f"codegraph {verb}")

        try:
            if self._require_lock_dir and lock_dir() is None:
                raise CodeGraphBootstrapError("writer lock directory not configured")
            with writer_lock(self._project_root, wait=WRITER_LOCK_WAIT_SEC) as held:
                if not held:
                    raise CodeGraphBootstrapError(
                        "another CodeGraph writer held the lock for"
                        f" {WRITER_LOCK_WAIT_SEC:g} s"
                    )
                refresh_codegraph_index(
                    self._project_root, image=self._image, capture=True
                )
        except CodeGraphBootstrapError as exc:
            if self._strict:
                raise
            self.warning = f"CodeGraph refresh skipped — {exc}"
            return

        # CodeGraph reports no granular progress, so land the pulsing bar
        # on a clean "complete" once the refresh returns.
        self.set_total(1)
        self.advance(1)
        self.summary = "synced" if verb == "sync" else "indexed"
