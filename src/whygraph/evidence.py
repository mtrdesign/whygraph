from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from whygraph.backend import SymbolNode

_SHA_HEADER = re.compile(r"^[0-9a-f]{7,64} \d+ \d+(?: \d+)?$")
_CLOSING_REF_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*#(\d{1,7})\b",
    re.IGNORECASE,
)
_HASH_REF_RE = re.compile(r"(?:^|[\s(])#(\d{1,7})\b")
_SSH_REPO_RE = re.compile(r"^git@github\.com:([^/]+/[^/]+?)(?:\.git)?$")
_HTTPS_REPO_RE = re.compile(r"^https?://github\.com/([^/]+/[^/]+?)(?:\.git)?$")


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

    def file_head_sha(self, file_path: str) -> str | None:
        out = self._git(["log", "-1", "--format=%H", "--", file_path])
        if out is None:
            return None
        sha = out.strip()
        return sha or None

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


# ---------------------------------------------------------------------------
# Bundle hash, store, service.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceRecord:
    id: int
    node_id: str
    qualified_name: str
    source: str
    ref: str | None
    payload: Any
    collected_at: int


@dataclass(frozen=True)
class BundleMeta:
    bundle_hash: str
    built_at: int
    head_at_collection: str | None


@dataclass(frozen=True)
class CollectionResult:
    evidence: list[EvidenceRecord]
    bundle_hash: str
    source: str  # "cache" | "collected"
    collected_at: int
    head_at_collection: str | None


def _stable_json(value: Any) -> str:
    # json.dumps with sort_keys recursively sorts dict keys at every depth,
    # matching v0's hand-rolled stableStringify for our payload shapes.
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def compute_bundle_hash(rows: list[EvidenceRow]) -> str:
    sorted_rows = sorted(rows, key=lambda r: (r.source, r.ref or ""))
    h = hashlib.sha256()
    for r in sorted_rows:
        h.update(r.source.encode("utf-8"))
        h.update(b"|")
        h.update((r.ref or "").encode("utf-8"))
        h.update(b"|")
        h.update(_stable_json(r.payload).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


class EvidenceStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def replace(
        self,
        node_id: str,
        qualified_name: str,
        rows: list[EvidenceRow],
        head_at_collection: str | None,
        *,
        now: int,
    ) -> str:
        bundle_hash = compute_bundle_hash(rows)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "DELETE FROM evidence WHERE node_id = ?", (node_id,)
            )
            for row in rows:
                self._conn.execute(
                    "INSERT INTO evidence "
                    "(node_id, qualified_name, source, ref, payload, collected_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        node_id,
                        qualified_name,
                        row.source,
                        row.ref,
                        json.dumps(row.payload),
                        now,
                    ),
                )
            self._conn.execute(
                "INSERT INTO evidence_bundles "
                "(node_id, bundle_hash, built_at, head_at_collection) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(node_id) DO UPDATE SET "
                "bundle_hash = excluded.bundle_hash, "
                "built_at = excluded.built_at, "
                "head_at_collection = excluded.head_at_collection",
                (node_id, bundle_hash, now, head_at_collection),
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return bundle_hash

    def bundle_meta_for(self, node_id: str) -> BundleMeta | None:
        row = self._conn.execute(
            "SELECT bundle_hash, built_at, head_at_collection "
            "FROM evidence_bundles WHERE node_id = ?",
            (node_id,),
        ).fetchone()
        if row is None:
            return None
        return BundleMeta(
            bundle_hash=row["bundle_hash"],
            built_at=int(row["built_at"]),
            head_at_collection=row["head_at_collection"],
        )

    def for_node(self, node_id: str) -> list[EvidenceRecord]:
        rows = self._conn.execute(
            "SELECT id, node_id, qualified_name, source, ref, payload, collected_at "
            "FROM evidence WHERE node_id = ? ORDER BY id",
            (node_id,),
        ).fetchall()
        records = []
        for r in rows:
            try:
                payload = json.loads(r["payload"])
            except (ValueError, TypeError):
                payload = r["payload"]
            records.append(
                EvidenceRecord(
                    id=int(r["id"]),
                    node_id=r["node_id"],
                    qualified_name=r["qualified_name"],
                    source=r["source"],
                    ref=r["ref"],
                    payload=payload,
                    collected_at=int(r["collected_at"]),
                )
            )
        return records


class EvidenceService:
    def __init__(
        self,
        store: EvidenceStore,
        git: GitEvidenceCollector,
        github: GitHubEvidenceCollector | None,
        repo_root: Path,
        ttl_seconds: int,
        *,
        now: Callable[[], int] | None = None,
        head_sha_fn: Callable[[str], str | None] | None = None,
    ) -> None:
        self._store = store
        self._git = git
        self._github = github
        self._repo_root = repo_root
        self._ttl_seconds = ttl_seconds
        self._now = now or (lambda: int(time.time()))
        self._head_sha_fn = head_sha_fn or (
            lambda file_path: git.file_head_sha(file_path)
        )

    def for_node(
        self, node: SymbolNode, *, force: bool = False
    ) -> CollectionResult:
        if not force:
            cached = self._check_cache(node)
            if cached is not None:
                return cached
        return self._collect(node)

    def _check_cache(self, node: SymbolNode) -> CollectionResult | None:
        meta = self._store.bundle_meta_for(node.id)
        if meta is None:
            return None
        age = self._now() - meta.built_at
        if age > self._ttl_seconds:
            return None
        if meta.head_at_collection is not None:
            current = self._head_sha_fn(node.file_path)
            if current is None or current != meta.head_at_collection:
                return None
        # head_at_collection is None → trust TTL alone (no git history at
        # collection time means no per-file sha to compare; recollecting on
        # every call would be wasteful).
        return CollectionResult(
            evidence=self._store.for_node(node.id),
            bundle_hash=meta.bundle_hash,
            source="cache",
            collected_at=meta.built_at,
            head_at_collection=meta.head_at_collection,
        )

    def _collect(self, node: SymbolNode) -> CollectionResult:
        git_rows = collect_git_evidence(self._git, node)
        gh_rows = (
            collect_github_evidence(self._github, git_rows)
            if self._github is not None and self._github.is_available()
            else []
        )
        rows = git_rows + gh_rows
        head = self._head_sha_fn(node.file_path)
        now = self._now()
        bundle_hash = self._store.replace(
            node.id, node.qualified_name, rows, head, now=now
        )
        return CollectionResult(
            evidence=self._store.for_node(node.id),
            bundle_hash=bundle_hash,
            source="collected",
            collected_at=now,
            head_at_collection=head,
        )
