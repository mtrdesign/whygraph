# Unreleased

!!! note "Not released yet"
    This page collects what is going into the next release. It moves into a numbered release page when
    that release is cut.

## Fixes

- **The Describe now estimate matches what a run spends.** It undershot the real cost by about 3x
  ([#86](https://github.com/mtrdesign/whygraph/issues/86)). It now counts merge commits by their
  changes against the first parent (it left them out, though they are described), one model call per
  chunk of a large diff plus the call that merges the chunks, and output and prompt sizes measured on a
  real run, so the cost of that run falls inside the range it shows.
- **Large merge commits get the cheap stub.** A merge touching more files than
  `[analyze].large_commit_file_count` against its first parent is no longer described in one pass;
  like any large commit, its files are described on demand.
