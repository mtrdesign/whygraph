# Unreleased

!!! note "Not released yet"
    These changes are on `main` and ship in the next release.

## Fixes

- **DeepSeek answers no longer show raw tool-call markup in Chat.** DeepSeek models sometimes write a
  tool call as text (`<｜｜DSML｜｜ invoke ...>`) instead of calling the tool. Chat used to show that
  markup as the answer. It now runs the call the markup describes and keeps the markup out of the
  answer. This also covers DeepSeek models served through OpenRouter. Markup that cannot be read is
  still shown, as before.
