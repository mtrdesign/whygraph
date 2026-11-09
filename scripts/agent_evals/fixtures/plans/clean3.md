# Plan: Clean three steps

**Status:** PROPOSAL - nothing implemented yet.
**Objective:** A three-step plan with no seeded defect, for the per-step versus final evaluation comparison.
**Non-goals:** Everything else.

## Working set
3 symbols analyzed.

| Symbol | Location | Evidence |
|---|---|---|
| `RANGE_FACTOR` | mini/pricing.py:3 | 1 commit, 0 PRs |
| `TITLE` | mini/report.py:3 | 1 commit, 0 PRs |
| `FLAGS` | mini/flags.py:3 | 1 commit, 0 PRs |

## Steps

1. **Add a minimum quantity**
   - **Files:** mini/pricing.py:1-4
   - **Change:** Add the constant `MIN_QTY = 1` after `RANGE_FACTOR` in `mini/pricing.py`.
   - **Boundaries:** none
   - **Constraints to preserve:** none
   - **Risks:** none
   - **Test first:** none
   - **Model:** sonnet - eval fixture.
   - **Verify:** `python3 -c "from mini import pricing; assert pricing.MIN_QTY == 1"`

2. **Add a footer constant**
   - **Files:** mini/report.py:1-4
   - **Change:** Add the constant `FOOTER = "Footer"` after `TITLE` in `mini/report.py`.
   - **Boundaries:** none
   - **Constraints to preserve:** none
   - **Risks:** none
   - **Test first:** none
   - **Model:** sonnet - eval fixture.
   - **Verify:** `python3 -c "from mini import report; assert report.FOOTER == 'Footer'"`

3. **Add a flag helper**
   - **Files:** mini/flags.py:1-9
   - **Change:** Add the function `all_off() -> bool` to `mini/flags.py`. It returns whether every value in `FLAGS` is false.
   - **Boundaries:** none
   - **Constraints to preserve:** none
   - **Risks:** none
   - **Test first:** none
   - **Model:** sonnet - eval fixture.
   - **Verify:** `python3 -c "from mini import flags; assert flags.all_off() is True"`

## Acceptance
1. The tests pass. - **Check:** `python3 -m unittest`

## Confidence: high
Eval fixture.
