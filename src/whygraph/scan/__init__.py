"""Scan subsystem — concurrent crawlers that populate the WhyGraph DB.

Exposes :class:`Crawler` (the threaded base class) and the concrete
crawlers — :class:`GitCrawler` for local git history and
:class:`GitHubCrawler` for GitHub pull requests and issues. The CLI
runs all instantiated crawlers concurrently against the shared SQLite
database.
"""

from whygraph.scan.crawler import Crawler
from whygraph.scan.git_crawler import GitCrawler
from whygraph.scan.github_crawler import GitHubCrawler

__all__ = ["Crawler", "GitCrawler", "GitHubCrawler"]
