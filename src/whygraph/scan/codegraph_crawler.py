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
)
from whygraph.services.codegraph.paths import CODEGRAPH_DB_RELPATH

from .crawler import Crawler


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

    Attributes
    ----------
    warning : str or None
        Message describing a swallowed :class:`CodeGraphBootstrapError`,
        for the orchestrator to surface after the crawl. ``None`` on
        success.
    """

    def __init__(
        self, progress: Progress, *, project_root: Path, image: str | None
    ) -> None:
        super().__init__("codegraph", progress, total=None)
        self._project_root = project_root
        self._image = image
        self.warning: str | None = None

    def work(self) -> None:
        db_path = self._project_root / CODEGRAPH_DB_RELPATH
        verb = "sync" if db_path.exists() else "init -i"
        self.advance(0, description=f"codegraph {verb}")

        try:
            refresh_codegraph_index(self._project_root, image=self._image, capture=True)
        except CodeGraphBootstrapError as exc:
            self.warning = f"CodeGraph refresh skipped — {exc}"
            return

        # CodeGraph reports no granular progress, so land the pulsing bar
        # on a clean "complete" once the refresh returns.
        self.set_total(1)
        self.advance(1)
        self.summary = "synced" if verb == "sync" else "indexed"
