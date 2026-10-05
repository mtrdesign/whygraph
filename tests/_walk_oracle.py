"""The pre-M2e walk-past, kept as a test oracle (plan §0.1 #10, §5.3).

A verbatim copy of ``whygraph.mcp.evidence._walk_past_boring`` as it was
before evidence-from-hunks: every hop re-blames the **working tree** with
the accumulated ignore set, and a ``GitError`` ends the walk. The only
adaptations: it calls the DB helper through the module (so a test's
monkeypatch of it applies to both walks), and it accepts the new
``blamer`` keyword so it can stand in for the new walk unchanged.

``tests/test_evidence_from_hunks.py`` runs ``evidence_from_hunks`` once
with the new walk and once with this one patched in, and compares the
two bundles. Delete this module (and the equivalence suite's oracle half)
once the M2e PR has merged.
"""

from __future__ import annotations

from whygraph.mcp import evidence as _evidence
from whygraph.mcp.evidence import _MAX_BORING_HOPS
from whygraph.mcp.targets import Target
from whygraph.services.git import BlameHunk, GitError, Repository


def walk_past_boring_oracle(
    repo: Repository,
    target: Target,
    *,
    initial_hunks: tuple[BlameHunk, ...],
    blamer: object = None,
) -> tuple[list[BlameHunk], set[str]]:
    """Re-run blame with refactor-heavy commits ignored, up to a cap.

    Returns the set of hunks that *appeared only after* a boring commit
    was ignored, along with the set of boring SHAs we ended up walking
    past. Used by :func:`collect_evidence` to tag those hunks
    ``source="blame-walked"``.
    """
    seen_shas = {h.sha for h in initial_hunks if not h.is_uncommitted}
    boring_shas = _evidence._boring_shas_in(seen_shas)
    if not boring_shas:
        return [], set()

    walked: list[BlameHunk] = []
    ignored = set(boring_shas)
    for _ in range(_MAX_BORING_HOPS):
        try:
            hunks = repo.blame(
                target.path,
                target.line_start,
                target.line_end,
                ignore_revs=tuple(sorted(ignored)),
            )
        except GitError:
            # Walk-past is best-effort: if git refuses the call (e.g.
            # an ignored SHA can't be resolved), bail out cleanly and
            # keep whatever we already have.
            break
        new_walked = [
            h for h in hunks if not h.is_uncommitted and h.sha not in seen_shas
        ]
        walked.extend(new_walked)
        seen_shas.update(h.sha for h in new_walked)
        new_boring = _evidence._boring_shas_in({h.sha for h in new_walked}) - ignored
        if not new_boring:
            break
        ignored.update(new_boring)
    return walked, ignored
