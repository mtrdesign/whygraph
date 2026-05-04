from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from whygraph.evidence.types import EvidenceRow

_CLOSING_REF_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*#(\d{1,7})\b",
    re.IGNORECASE,
)
_HASH_REF_RE = re.compile(r"(?:^|[\s(])#(\d{1,7})\b")
_SSH_REPO_RE = re.compile(r"^git@github\.com:([^/]+/[^/]+?)(?:\.git)?$")
_HTTPS_REPO_RE = re.compile(r"^https?://github\.com/([^/]+/[^/]+?)(?:\.git)?$")


@dataclass(frozen=True)
class GitHubPRPayload:
    number: int
    title: str
    body: str
    state: str
    merged: bool
    merged_at: str | None
    created_at: str | None
    author: str
    url: str
    closes_issues: list[int]


@dataclass(frozen=True)
class GitHubIssuePayload:
    number: int
    title: str
    body: str
    state: str
    created_at: str | None
    closed_at: str | None
    author: str
    url: str
    labels: list[str]


def parse_closing_refs(text: str) -> list[int]:
    found = (int(m.group(1)) for m in _CLOSING_REF_RE.finditer(text))
    return list(dict.fromkeys(found))


def parse_hash_refs(text: str) -> list[int]:
    found = (int(m.group(1)) for m in _HASH_REF_RE.finditer(text))
    return list(dict.fromkeys(found))


def parse_github_repo(url: str) -> str | None:
    url = url.strip()
    m = _SSH_REPO_RE.match(url)
    if m:
        return m.group(1)
    m = _HTTPS_REPO_RE.match(url)
    if m:
        return m.group(1)
    return None


def detect_github_repo(repo_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    return parse_github_repo(result.stdout.strip())


def _gh_available() -> bool:
    return shutil.which("gh") is not None


class GitHubEvidenceCollector:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self.repo = detect_github_repo(repo_root)
        self._available = self.repo is not None and _gh_available()
        self._prs_by_commit: dict[str, list[int]] = {}
        self._pr_details: dict[int, GitHubPRPayload | None] = {}
        self._issue_details: dict[int, GitHubIssuePayload | None] = {}

    def is_available(self) -> bool:
        return self._available

    def _gh(self, args: list[str]) -> str | None:
        try:
            result = subprocess.run(
                ["gh", *args],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            return None
        if result.returncode != 0:
            return None
        return result.stdout

    def pr_numbers_for_commit(self, sha: str) -> list[int]:
        if not self._available or not self.repo:
            return []
        cached = self._prs_by_commit.get(sha)
        if cached is not None:
            return cached
        out = self._gh(["api", f"/repos/{self.repo}/commits/{sha}/pulls"])
        numbers: list[int] = []
        if out is not None:
            try:
                parsed = json.loads(out)
                if isinstance(parsed, list):
                    merged = [
                        int(p["number"])
                        for p in parsed
                        if isinstance(p, dict)
                        and p.get("merged_at")
                        and "number" in p
                    ]
                    if merged:
                        numbers = merged
                    else:
                        numbers = [
                            int(p["number"])
                            for p in parsed
                            if isinstance(p, dict) and "number" in p
                        ]
            except (ValueError, KeyError, TypeError):
                numbers = []
        self._prs_by_commit[sha] = numbers
        return numbers

    def pr(self, number: int) -> GitHubPRPayload | None:
        if not self._available or not self.repo:
            return None
        if number in self._pr_details:
            return self._pr_details[number]
        out = self._gh(
            [
                "pr",
                "view",
                str(number),
                "--repo",
                self.repo,
                "--json",
                "number,title,body,state,mergedAt,createdAt,author,url",
            ]
        )
        if out is None:
            self._pr_details[number] = None
            return None
        try:
            raw = json.loads(out)
        except ValueError:
            self._pr_details[number] = None
            return None
        if not isinstance(raw, dict):
            self._pr_details[number] = None
            return None

        body = raw.get("body") or ""
        merged_at = raw.get("mergedAt")
        state = raw.get("state") or ""
        author_obj = raw.get("author") or {}
        payload = GitHubPRPayload(
            number=int(raw.get("number", number)),
            title=raw.get("title") or "",
            body=body,
            state=state,
            merged=merged_at is not None or state.upper() == "MERGED",
            merged_at=merged_at,
            created_at=raw.get("createdAt"),
            author=author_obj.get("login") or "" if isinstance(author_obj, dict) else "",
            url=raw.get("url") or "",
            closes_issues=parse_closing_refs(body),
        )
        self._pr_details[number] = payload
        return payload

    def issue(self, number: int) -> GitHubIssuePayload | None:
        if not self._available or not self.repo:
            return None
        if number in self._issue_details:
            return self._issue_details[number]
        out = self._gh(
            [
                "issue",
                "view",
                str(number),
                "--repo",
                self.repo,
                "--json",
                "number,title,body,state,createdAt,closedAt,author,url,labels",
            ]
        )
        if out is None:
            self._issue_details[number] = None
            return None
        try:
            raw = json.loads(out)
        except ValueError:
            self._issue_details[number] = None
            return None
        if not isinstance(raw, dict):
            self._issue_details[number] = None
            return None

        author_obj = raw.get("author") or {}
        labels_raw = raw.get("labels") or []
        labels = [
            l["name"]
            for l in labels_raw
            if isinstance(l, dict) and isinstance(l.get("name"), str)
        ]
        payload = GitHubIssuePayload(
            number=int(raw.get("number", number)),
            title=raw.get("title") or "",
            body=raw.get("body") or "",
            state=raw.get("state") or "",
            created_at=raw.get("createdAt"),
            closed_at=raw.get("closedAt"),
            author=author_obj.get("login") or "" if isinstance(author_obj, dict) else "",
            url=raw.get("url") or "",
            labels=labels,
        )
        self._issue_details[number] = payload
        return payload


def collect_github_evidence(
    github: GitHubEvidenceCollector, git_rows: list[EvidenceRow]
) -> list[EvidenceRow]:
    if not github.is_available():
        return []

    pr_numbers: dict[int, None] = {}

    for row in git_rows:
        if row.source != "git_commit" or not row.ref:
            continue
        for num in github.pr_numbers_for_commit(row.ref):
            pr_numbers[num] = None

    for row in git_rows:
        if row.source != "git_commit":
            continue
        text = (row.payload.get("subject") or "") + "\n" + (row.payload.get("body") or "")
        for num in parse_hash_refs(text):
            pr_numbers[num] = None

    rows: list[EvidenceRow] = []
    issue_numbers: dict[int, None] = {}

    for num in pr_numbers:
        pr = github.pr(num)
        if pr is None:
            continue
        rows.append(
            EvidenceRow(
                source="pr",
                ref=str(pr.number),
                payload={
                    "number": pr.number,
                    "title": pr.title,
                    "body": pr.body,
                    "state": pr.state,
                    "merged": pr.merged,
                    "merged_at": pr.merged_at,
                    "created_at": pr.created_at,
                    "author": pr.author,
                    "url": pr.url,
                    "closes_issues": pr.closes_issues,
                },
            )
        )
        for issue_num in pr.closes_issues:
            issue_numbers[issue_num] = None

    for num in issue_numbers:
        issue = github.issue(num)
        if issue is None:
            continue
        rows.append(
            EvidenceRow(
                source="issue",
                ref=str(issue.number),
                payload={
                    "number": issue.number,
                    "title": issue.title,
                    "body": issue.body,
                    "state": issue.state,
                    "created_at": issue.created_at,
                    "closed_at": issue.closed_at,
                    "author": issue.author,
                    "url": issue.url,
                    "labels": issue.labels,
                },
            )
        )

    return rows
