from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from whygraph.backend import SymbolNode

_SHA_HEADER = re.compile(r"^[0-9a-f]{7,64} \d+ \d+(?: \d+)?$")


@dataclass(frozen=True)
class GitBlameEntry:
    commit: str
    author: str
    author_email: str
    author_time: int
    summary: str
    line_count: int


@dataclass(frozen=True)
class GitCommitInfo:
    sha: str
    author: str
    author_email: str
    author_time: int
    committer: str
    committer_email: str
    committer_time: int
    parents: tuple[str, ...]
    subject: str
    body: str


@dataclass(frozen=True)
class EvidenceRow:
    source: str
    ref: str | None
    payload: dict[str, Any] = field(default_factory=dict)


def _parse_line_porcelain(stdout: str) -> list[GitBlameEntry]:
    entries: dict[str, dict[str, Any]] = {}
    lines = stdout.split("\n")
    i = 0
    n = len(lines)
    while i < n:
        header = lines[i]
        i += 1
        if not header or not _SHA_HEADER.match(header):
            continue
        sha = header.split(" ", 1)[0]

        author = ""
        author_email = ""
        author_time = 0
        summary = ""
        while i < n and not lines[i].startswith("\t"):
            line = lines[i]
            i += 1
            if line.startswith("author "):
                author = line[7:]
            elif line.startswith("author-mail "):
                raw = line[12:]
                if raw.startswith("<"):
                    raw = raw[1:]
                if raw.endswith(">"):
                    raw = raw[:-1]
                author_email = raw
            elif line.startswith("author-time "):
                try:
                    author_time = int(line[12:])
                except ValueError:
                    author_time = 0
            elif line.startswith("summary "):
                summary = line[8:]
        if i < n and lines[i].startswith("\t"):
            i += 1

        existing = entries.get(sha)
        if existing is not None:
            existing["line_count"] += 1
        else:
            entries[sha] = {
                "commit": sha,
                "author": author,
                "author_email": author_email,
                "author_time": author_time,
                "summary": summary,
                "line_count": 1,
            }

    blames = [GitBlameEntry(**e) for e in entries.values()]
    blames.sort(key=lambda b: b.line_count, reverse=True)
    return blames


class GitEvidenceCollector:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self._commit_cache: dict[str, GitCommitInfo] = {}

    def _git(self, args: list[str]) -> str | None:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            return None
        if result.returncode != 0:
            return None
        return result.stdout

    def blame_line_range(
        self, file_path: str, start_line: int, end_line: int
    ) -> list[GitBlameEntry]:
        if end_line < start_line:
            return []
        stdout = self._git(
            [
                "blame",
                "--line-porcelain",
                "-L",
                f"{start_line},{end_line}",
                "--",
                file_path,
            ]
        )
        if stdout is None:
            return []
        return _parse_line_porcelain(stdout)

    def commit_info(self, sha: str) -> GitCommitInfo | None:
        cached = self._commit_cache.get(sha)
        if cached is not None:
            return cached

        meta = self._git(
            [
                "log",
                "-1",
                "--format=%H%n%an%n%ae%n%at%n%cn%n%ce%n%ct%n%P%n%s",
                sha,
            ]
        )
        if meta is None:
            return None
        body = self._git(["log", "-1", "--format=%B", sha]) or ""

        parts = meta.rstrip("\n").split("\n")
        if len(parts) < 9:
            return None

        try:
            author_time = int(parts[3])
        except ValueError:
            author_time = 0
        try:
            committer_time = int(parts[6])
        except ValueError:
            committer_time = 0

        info = GitCommitInfo(
            sha=parts[0],
            author=parts[1],
            author_email=parts[2],
            author_time=author_time,
            committer=parts[4],
            committer_email=parts[5],
            committer_time=committer_time,
            parents=tuple(p for p in parts[7].split(" ") if p) if parts[7] else (),
            subject="\n".join(parts[8:]),
            body=body.rstrip("\n"),
        )
        self._commit_cache[sha] = info
        return info


def collect_git_evidence(
    git: GitEvidenceCollector, node: SymbolNode
) -> list[EvidenceRow]:
    blame = git.blame_line_range(node.file_path, node.start_line, node.end_line)
    if not blame:
        return []

    line_total = node.end_line - node.start_line + 1
    rows: list[EvidenceRow] = []

    for b in blame:
        rows.append(
            EvidenceRow(
                source="git_blame",
                ref=b.commit,
                payload={
                    "author": b.author,
                    "author_email": b.author_email,
                    "author_time": b.author_time,
                    "summary": b.summary,
                    "line_count": b.line_count,
                    "line_total": line_total,
                },
            )
        )

    seen_shas: set[str] = set()
    for b in blame:
        if b.commit in seen_shas:
            continue
        seen_shas.add(b.commit)
        info = git.commit_info(b.commit)
        if info is None:
            continue
        rows.append(
            EvidenceRow(
                source="git_commit",
                ref=info.sha,
                payload={
                    "subject": info.subject,
                    "body": info.body,
                    "author": info.author,
                    "author_email": info.author_email,
                    "author_time": info.author_time,
                    "committer": info.committer,
                    "committer_email": info.committer_email,
                    "committer_time": info.committer_time,
                    "parents": list(info.parents),
                },
            )
        )

    return rows
