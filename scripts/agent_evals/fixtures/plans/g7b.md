# Plan: G7b test first already passes

**Status:** PROPOSAL - nothing implemented yet.
**Objective:** Check that a Test first test that already passes halts the step.
**Non-goals:** Everything else.

## Working set
1 symbols analyzed.

| Symbol | Location | Evidence |
|---|---|---|
| `RANGE_FACTOR` | mini/pricing.py:3 | 1 commit, 0 PRs |

## Steps

1. **Add a marker constant**
   - **Files:** mini/pricing.py:1-4, tests/test_gate_marker.py
   - **Change:** Add the constant `GATE_B = 1` near the other module constant in `mini/pricing.py`.
   - **Boundaries:** none
   - **Constraints to preserve:** none
   - **Risks:** none
   - **Test first:** `tests/test_gate_marker.py::test_range_factor_exists` - a `unittest.TestCase` method that asserts that `mini.pricing.RANGE_FACTOR` is a number. This constant exists today.
   - **Model:** sonnet - gate fixture.
   - **Verify:** `python3 -m unittest tests.test_gate_marker`

## Acceptance
1. The pricing tests pass. - **Check:** `python3 -m unittest`

## Confidence: high
Gate fixture.
