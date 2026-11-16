# Plan: U25 a route proven by red then green

**Status:** PROPOSAL - nothing implemented yet.
**Objective:** A caller narrows the notes list by note kind.
**Non-goals:** Everything else.

## Steps

1. **Route: accept `kind` on `GET /notes`**
   - **Files:** mini/routes.py:11-13, tests/test_routes.py:7
   - **Change:** In the route handler `list_notes` of `mini/routes.py`, add a keyword parameter `kind: str | None = None`. When `kind` is set, return only the notes of that kind.
   - **Boundaries:** none
   - **Constraints to preserve:** none
   - **Risks:** none
   - **Test first:** tests/test_routes.py::test_list_filters_by_kind
   - **Model:** haiku - eval fixture.
   - **Verify:** `python3 -m unittest tests.test_routes`

## Acceptance
1. The tests pass. - **Check:** `python3 -m unittest`

## Confidence: high
Eval fixture.

## Implementation log
- Baseline: 0 of 1 Verify commands pass (2026-11-16T00:00:00Z)
  - Step 1: expected (Test first)
- [x] Step 1: Route: accept `kind` on `GET /notes` (verified at 2026-11-16T00:00:00Z)
