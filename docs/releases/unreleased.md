# Unreleased

!!! note "Not released yet"
    These changes are on `main` and ship in the next release.

## Changes

- **Every constraint on a rationale card names its source.** Each item of a card's `constraints`
  and `risks` is now an object with its `text`, a `basis` (`quoted`, `cited`, `inferred` or
  `unknown`) and the commits, PRs or issues behind it. The planning agents quote the `text` as before
  and end each quote with a tag, such as `(quoted: 1a2b3c4d)` or `(inferred)`. A constraint the model
  only inferred never becomes a Blocker, and in Claude Code a review-gate verdict on one is printed
  and does not hold the step. A card cached before this release reads `unknown` until it is generated
  again. Run **Settings > Update agent files** to pick it up, and see
  [Where a constraint comes from](../guide/planning.md#where-a-constraint-comes-from).
- **A plan can change a recorded constraint on purpose.** When the task itself asks to change a
  constraint, the step gets a **Changes on purpose** line with the quote and the reason, in place of
  a Blocker. The implementor does not guard it, and in Claude Code the review gate reports it as
  **Changed on purpose** and goes on. Run **Settings > Update agent files** to pick it up.

