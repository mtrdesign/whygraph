# Plan: G9 old format

## Working set
1 symbols analyzed.

| Symbol | Location | Evidence |
|---|---|---|
| `RANGE_FACTOR` | mini/pricing.py:3 | 1 commit, 0 PRs |

## Blockers
None.

## Steps

1. **Add a marker constant**
   - **Files:** mini/pricing.py:1-4
   - **Change:** Add the constant `GATE_OLD = 1` near the other module constant.
   - **Constraints to preserve:** none
   - **Risks:** none
   - **Verify:** `python3 -c "from mini import pricing as e; assert e.GATE_OLD == 1"`

## Risks called out across the change
None.

## Confidence: high
Gate fixture in the format from before plan 44.
