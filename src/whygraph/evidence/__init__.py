"""Evidence collection: git + GitHub provenance for code symbols.

Submodules:
    types     — shared dataclasses (`EvidenceRow`, `EvidenceRecord`,
                `BundleMeta`, `CollectionResult`) and `compute_bundle_hash`.
    store     — `EvidenceStore`: SQLite persistence layer.
    service   — `EvidenceService`: orchestrates collection, caching,
                and HEAD-sha staleness.
    git       — git collector (blame + commit info).
    github    — GitHub collector (PRs + issues, with squash-merge fallback).

Import from the specific submodule rather than re-exporting through the
package — keeps call sites explicit about where each piece lives.
"""
