"""Scan subsystem — concurrent crawlers that populate the WhyGraph DB.

Exposes :class:`Crawler` (the threaded base class) and the concrete
crawlers — :class:`GitCrawler` for local git history,
:class:`GitHubCrawler` for GitHub pull requests and issues,
:class:`CodeGraphCrawler` which refreshes the CodeGraph index, and
:class:`AnalyzeCrawler` which describes each commit's diff with an LLM
(run after :class:`GitCrawler`). The CLI runs the source crawlers (and
CodeGraph) concurrently, then the analyzer, against the shared SQLite
database.
"""

from whygraph.scan.analyze_crawler import AnalyzeCrawler
from whygraph.scan.codegraph_crawler import CodeGraphCrawler
from whygraph.scan.crawler import Crawler
from whygraph.scan.git_crawler import GitCrawler
from whygraph.scan.github_crawler import GitHubCrawler

__all__ = [
    "AnalyzeCrawler",
    "CodeGraphCrawler",
    "Crawler",
    "GitCrawler",
    "GitHubCrawler",
]
