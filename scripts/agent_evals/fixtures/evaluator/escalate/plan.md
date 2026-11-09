# Plan: O7 escalate case

**Status:** PROPOSAL - nothing implemented yet.
**Objective:** Canned step whose Change needs a path that Files omits.
**Non-goals:** Everything else.

## Steps

1. **Add levels**
   - **Files:** mini/flags.py:1-4
   - **Change:** Add the constant `LEVELS = (1, 2)` after `FLAGS` in `mini/flags.py`. Append the line `Levels are documented here.` to `README.md`.
   - **Boundaries:** none
   - **Constraints to preserve:** none
   - **Risks:** none
   - **Test first:** none
   - **Model:** sonnet - eval fixture.
   - **Verify:** `python3 -c "from mini import flags; assert flags.LEVELS == (1, 2)"`

## Acceptance
1. The tests pass. - **Check:** `python3 -m unittest`

## Confidence: high
Eval fixture.

## Implementation log
- [x] Step 1: Add levels (verified at 2026-11-09T00:00:00Z)
  - out of scope: README.md
