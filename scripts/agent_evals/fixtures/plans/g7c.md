# Plan: G7c fix loop and scope

**Status:** PROPOSAL - nothing implemented yet.
**Objective:** Check the fix loop, the out-of-scope line and a later re-run of the step.
**Non-goals:** Everything else.

## Working set
1 symbols analyzed.

| Symbol | Location | Evidence |
|---|---|---|
| `RANGE_FACTOR` | mini/pricing.py:3 | 1 commit, 0 PRs |

## Steps

1. **Add a marker constant**
   - **Files:** mini/pricing.py:1-4
   - **Change:** Add the constant `GATE_C = 2` near the other module constant in `mini/pricing.py`. Append the line `# gate` to the end of `.dockerignore`.
   - **Boundaries:** none
   - **Constraints to preserve:** none
   - **Risks:** none
   - **Test first:** none
   - **Model:** sonnet - gate fixture.
   - **Verify:** `python3 -c "from mini import pricing as e; assert e.GATE_C == 3"`

## Acceptance
1. The pricing tests pass. - **Check:** `python3 -m unittest`

## Confidence: high
Gate fixture.
