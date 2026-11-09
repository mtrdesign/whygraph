# Plan: O7 pass case

**Status:** PROPOSAL - nothing implemented yet.
**Objective:** Canned step whose diff does exactly what the plan says.
**Non-goals:** Everything else.

## Steps

1. **Add a tax rate**
   - **Files:** mini/pricing.py:1-4
   - **Change:** Add the constant `TAX_RATE = 0.2` after `RANGE_FACTOR` in `mini/pricing.py`.
   - **Boundaries:** none
   - **Constraints to preserve:** none
   - **Risks:** none
   - **Test first:** none
   - **Model:** sonnet - eval fixture.
   - **Verify:** `python3 -c "from mini import pricing; assert pricing.TAX_RATE == 0.2"`

## Acceptance
1. The tests pass. - **Check:** `python3 -m unittest`

## Confidence: high
Eval fixture.

## Implementation log
- [x] Step 1: Add a tax rate (verified at 2026-11-09T00:00:00Z)
