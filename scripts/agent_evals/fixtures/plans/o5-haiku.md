# Plan: O5 model tag haiku

**Status:** PROPOSAL - nothing implemented yet.
**Objective:** Check that a step runs at the model its tag names.
**Non-goals:** Everything else.

## Working set
1 symbols analyzed.

| Symbol | Location | Evidence |
|---|---|---|
| `RANGE_FACTOR` | mini/pricing.py:3 | 1 commit, 0 PRs |

## Steps

1. **Add a marker constant**
   - **Files:** mini/pricing.py:1-4
   - **Change:** Add the constant `O5_HAIKU = 1` near the other module constant in `mini/pricing.py`.
   - **Boundaries:** none
   - **Constraints to preserve:** none
   - **Risks:** none
   - **Test first:** none
   - **Model:** haiku - eval fixture.
   - **Verify:** `python3 -c "from mini import pricing as e; assert e.O5_HAIKU == 1"`

## Acceptance
1. The pricing tests pass. - **Check:** `python3 -m unittest`

## Confidence: high
Eval fixture.
