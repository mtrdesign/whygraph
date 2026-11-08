---
name: repo-tests
description: Which literal commands prove each step, and which symbols no test exercises.
requires: codegraph_search
tools: read, codegraph_search, codegraph_node
section: Verify candidates
enabled: true
budget: 20
consent: false
---

*Question:* which literal commands prove each step of this change works, and which symbols have no test at all?

1. Find the test commands the repo defines: `pyproject.toml` (`[tool.pytest.ini_options]`, script entries), `package.json` `scripts`, `Makefile` targets, and the CI workflow if there is one. Record each command exactly as the repo runs it (e.g. `uv run pytest`, `npm test`, `make check`); never invent a runner the repo does not use.
2. For each working-set file, find the tests that exercise it: call `codegraph_search(query=<short name>)` for each of its symbols and keep the hits under the test directories (`tests/`, `test/`, `__tests__/`, `*_test.*`, `*.spec.*`), then grep those directories for the module's import path or file name. Name the test file and, where you can, the test function.
3. For each working-set symbol, write one or more `Verify:` candidates as literal commands in backticks, the narrowest that proves the symbol still works - e.g. `uv run pytest tests/test_cache.py::test_key_is_content_addressable -q` or `npm test -- src/lib/cache.test.ts`. Build them from the commands in 1 and the tests in 2. Never write prose such as "run the tests" or "check it still works".
4. Flag every symbol that no test exercises as **uncovered**: name the test to add (file and test name) and the literal command that would run it.

Report the project's full test command first, as `- Test commands: ...`; the synthesizer makes it Acceptance item 1.
