from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from whygraph.backend import SymbolNode
from whygraph.evidence.git import GitEvidenceCollector, collect_git_evidence
from whygraph.evidence.github import (
    GitHubEvidenceCollector,
    collect_github_evidence,
)
from whygraph.evidence.store import EvidenceStore
from whygraph.evidence.types import CollectionResult


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
