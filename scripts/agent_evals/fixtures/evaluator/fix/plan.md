# Plan: O7 fix case

**Status:** PROPOSAL - nothing implemented yet.
**Objective:** Canned step whose diff is missing part of the Change.
**Non-goals:** Everything else.

## Steps

1. **Add row limits**
   - **Files:** mini/report.py:1-4
   - **Change:** Add the two constants `MIN_ROWS = 1` and `MAX_ROWS = 99` after `TITLE` in `mini/report.py`.
   - **Boundaries:** none
   - **Constraints to preserve:** none
   - **Risks:** none
   - **Test first:** none
   - **Model:** sonnet - eval fixture.
   - **Verify:** `python3 -c "from mini import report; assert report.MIN_ROWS == 1"`

## Acceptance
1. The tests pass. - **Check:** `python3 -m unittest`

## Confidence: high
Eval fixture.

## Implementation log
- [x] Step 1: Add row limits (verified at 2026-11-09T00:00:00Z)
