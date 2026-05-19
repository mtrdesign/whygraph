"""Scan subsystem — concurrent crawlers that populate the WhyGraph DB.

Currently exposes only :class:`Crawler`, the orchestration primitive.
Concrete crawlers (git history, GitHub PRs/issues, …) land in follow-up
modules.
"""

from whygraph.scan.crawler import Crawler
from whygraph.scan.git_crawler import GitCrawler

__all__ = ["Crawler", "GitCrawler"]
