"""Scan subsystem — phased crawlers that populate the WhyGraph DB.

Exposes :class:`Crawler` (the threaded base class) and the concrete
crawlers — :class:`GitCrawler` for local git history,
:class:`GitHubCrawler` for GitHub pull requests and issues,
:class:`CodeGraphCrawler` which refreshes the CodeGraph index,
:class:`AnalyzeCrawler` which describes each commit's diff with an LLM,
and :class:`PROriginEnricher` which recovers a squash-merged PR's original
commits.

The CLI drives them in three ordered phases against the shared SQLite
database: **Phase 1** runs :class:`GitCrawler` and :class:`GitHubCrawler`
concurrently; **Phase 2** runs :class:`PROriginEnricher` (which needs the
git + PR rows Phase 1 persisted); **Phase 3** runs :class:`AnalyzeCrawler`
last and alone (the slow, token-heavy LLM pass). :class:`CodeGraphCrawler`
is a best-effort background task started before Phase 1 and joined after
Phase 3 — it has no data dependency on the DB, so it spans the whole scan.
"""

from whygraph.scan.analyze_crawler import AnalyzeCrawler
from whygraph.scan.codegraph_crawler import CodeGraphCrawler
from whygraph.scan.crawler import Crawler
from whygraph.scan.git_crawler import GitCrawler
from whygraph.scan.github_crawler import GitHubCrawler
from whygraph.scan.pr_origin_enricher import PROriginEnricher

__all__ = [
    "AnalyzeCrawler",
    "CodeGraphCrawler",
    "Crawler",
    "GitCrawler",
    "GitHubCrawler",
    "PROriginEnricher",
]
