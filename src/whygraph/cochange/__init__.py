"""Co-change + volatility signals for rationale enrichment.

These are git-derived metrics computed at rationale-generation time:

    types     — `CoChangeNeighbor`, `CoChangeReport`, `VolatilityReport`.
    git       — raw `git log` / `git show` operations + porcelain parsing.
    service   — `CoChangeService` (with per-commit SQLite cache) and
                `VolatilityService`. Also exposes `cochange_fingerprint` /
                `volatility_fingerprint` for cache invalidation.

Co-change answers "which files historically change in the same commits as
this one" — surfaces coupling that's invisible to the static call graph
(schemas, GraphQL definitions, generated configs co-modified with code).

Volatility answers "is this code stable or actively churning" — calibrates
the rationale's tone (a single 1-day-old commit is thin history; many
commits across many authors signals an in-flight design).

Import from the specific submodule rather than re-exporting through the
package — keeps call sites explicit about where each piece lives.
"""
