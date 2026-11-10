# Unreleased

!!! note "Not released yet"
    These changes are on `main` and ship in the next release.

## Fixes

- **A failed code index refresh now says why.** When CodeGraph failed during a scan, the project's
  Health card and the scan's run page showed every frame of CodeGraph's progress spinner, colour codes
  included, and a long first index could produce so much of it that the line saying what went wrong
  was cut off. The message now keeps only the last lines of CodeGraph's output, one line per step,
  ending with its error (for example `Failed: database disk image is malformed`).
