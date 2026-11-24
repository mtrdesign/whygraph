# Plan: P26 a farewell helper

**Status:** PROPOSAL - nothing implemented yet.
**Objective:** Add a farewell helper that the committed test exercises.
**Non-goals:** Everything else.

## Working set
1 symbols analyzed.

| Symbol | Location | Evidence |
|---|---|---|
| `farewell` | test/farewell.test.js:4 | 1 commit, 0 PRs |

## Steps

1. **Add the farewell helper**
   - **Files:** src/farewell.js (new)
   - **Change:** Create `src/farewell.js` with `export function farewell(name)`, which returns `Goodbye, ${name}!`.
   - **Boundaries:** none
   - **Constraints to preserve:** none
   - **Risks:** none
   - **Test first:** test/farewell.test.js::farewell says goodbye
   - **Model:** sonnet - eval fixture.
   - **Verify:** `node --test test/farewell.test.js`

## Acceptance
1. The farewell test passes. - **Check:** `node --test test/farewell.test.js`

## Confidence: high
Eval fixture.
