# Unreleased

!!! note "Not released yet"
    These changes are on `main` and ship in the next release.

## What's new

- **A rationale card no longer waits on the scan to describe its commits.** When some commits behind a
  card have no description yet, `whygraph_rationale_brief` and the Explorer's **Generate rationale**
  describe up to `[analyze].max_workers` of them in parallel (within 20 seconds), generate the card,
  and finish the rest in the background. Such a card comes back with `partial: true`, `describing` and
  `completing`, and the Rationale tab shows a note with a **Complete** button. A completion runs at
  most once per card (10-minute cooldown) and counts as a second card generation. `[analyze].max_workers`
  now defaults to 4.

## Fixes

- **A failed code index refresh now says why.** When CodeGraph failed during a scan, the project's
  Health card and the scan's run page showed every frame of CodeGraph's progress spinner, colour codes
  included, and a long first index could produce so much of it that the line saying what went wrong
  was cut off. The message now keeps only the last lines of CodeGraph's output, one line per step,
  ending with its error (for example `Failed: database disk image is malformed`).
