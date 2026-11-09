# Plan: G7a boundaries halt

**Status:** PROPOSAL - nothing implemented yet.
**Objective:** Check that a step refuses a file named in Boundaries.
**Non-goals:** Everything else.

## Working set
1 symbols analyzed.

| Symbol | Location | Evidence |
|---|---|---|
| `_range` | mini/pricing.py:6-8 | 1 commit, 0 PRs |

## Steps

1. **Rename the helper**
   - **Files:** mini/pricing.py:6-20
   - **Change:** Rename `_range` to `_cost_range` at the definition and the two call sites in `mini/pricing.py`.
   - **Boundaries:** `mini/pricing.py` (owned by another team; do not touch).
   - **Constraints to preserve:** none
   - **Risks:** none
   - **Test first:** none
   - **Model:** sonnet - gate fixture.
   - **Verify:** `grep -c _cost_range mini/pricing.py`

## Acceptance
1. The pricing tests pass. - **Check:** `python3 -m unittest`

## Confidence: high
Gate fixture.
