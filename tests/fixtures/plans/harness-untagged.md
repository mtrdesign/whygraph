# Plan: Add a per-project `[rationale].max_cards` limit, enforced by `whygraph_rationale_brief`

**Status:** PROPOSAL - nothing implemented yet; review the steps, then run the implement command on this file.
**Objective:** A project can set `[rationale].max_cards`, and `whygraph_rationale_brief` then refuses to generate a new card once `rationale_cache` holds that many, while cache hits and `cached_only` calls keep working.
**Non-goals:** The limit does not apply to `POST /api/v1/projects/{slug}/rationale` or to a linked project. The portal Overview route, the playground Overview page and the `GenerationNotPermitted` class stay unchanged. This plan adds no Settings form field (see Q3).

## Resolved decisions

| # | Question | Decision |
|---|---|---|
| Q1 | Can a repo's `whygraph.toml` set `max_cards` on import? | default: PUT only, not importable |
| Q2 | Should a call at the limit that overwrites an existing card be refused? | default: Allow the overwrite |
| Q3 | Should the project Settings form get a `max_cards` field? | default: No field, set through the config API |

## Working set
10 symbols analyzed (about 70 truncated from impact set, mostly tests).

| Symbol | Location | Evidence |
|---|---|---|
| `RationaleConfig` | src/whygraph/core/config.py:414-471 | 20 commits, 9 PRs |
| `_build_rationale_config` | src/whygraph/core/config.py:978-983 | 20 commits, 13 PRs |
| `store_cached` | src/whygraph/mcp/rationale_cache.py:307-348 | 4 commits, 4 PRs |
| `GenerationNotPermitted` | src/whygraph/mcp/rationale.py:199-246 | 20 commits, 13 PRs |
| `rationale_card` | src/whygraph/mcp/rationale.py:360-516 | 20 commits, 15 PRs |
| `linked_rationale` | src/whygraph/mcp/rationale.py:263-301 | 16 commits, 14 PRs |
| `whygraph_rationale_brief` | src/whygraph/mcp/rationale.py:575-631 | 19 commits, 14 PRs |
| `post_rationale` | src/whygraph/portal/v1_routes.py:503-539 | 8 commits, 5 PRs |
| `ToolRegistry::_get_rationale` | src/whygraph/chat/tools.py:1279-1328 | 10 commits, 6 PRs |
| `rationale_generate` | src/whygraph/serve/routes.py:204-212 | 10 commits, 7 PRs |

## Prior art
- PR #61 (src/whygraph/mcp/rationale.py, src/whygraph/portal/policy.py) - a cache-miss refusal goes before `before_generate`, so nothing is charged.
- PR #63 (src/whygraph/mcp/rationale.py) - every rationale refusal carries a stable `code=`, which the SPA registry must list.
- PR #59 (src/whygraph/mcp/rationale.py) - the first cache-miss refusal; it refuses on a miss and keeps serving hits.
- PR #58 (src/whygraph/mcp/rationale.py, src/whygraph/portal/policy.py) - linked projects branch away before the local cache, and `rationale_card` takes caller-specific limits as keywords.
- PR #67 (src/whygraph/mcp/rationale.py, src/whygraph/mcp/rationale_cache.py) - `cached_only` returns before every gate, and count-style helpers belong in the cache module.
- PR #65 (src/whygraph/mcp/rationale.py) - `force_refresh` still passes every gate.
- PR #77 and PR #78 (src/whygraph/core/config.py) - range checks live in `Config` validation, reject bools and raise `ConfigError`.
- Commit e4ea859 (src/whygraph/portal/policy.py) - portal-level value validation exists, but a `Config` range check is enough here.
- Commit 7a37d7e (no PR) - a precedent for scoping one key out of a single allowlist.

## Steps

1. **Add the `max_cards` config key**
   - **Files:** src/whygraph/core/config.py:414-471, src/whygraph/core/config.py:978-983, src/whygraph/core/config.py:1398-1419, tests/test_core_config_rationale.py:136-168
   - **Change:** Add `max_cards: int = 0` to `RationaleConfig`, documented in its NumPy docstring. Define `0` as no limit. Parse the key in `_build_rationale_config` the way its sibling fields are parsed. Add `("rationale.max_cards", self.rationale.max_cards)` to the `is_agent_limit` tuple at config.py:1406-1413. Keep the range check out of `_build_rationale_config`. Add `test_rationale_max_cards_default`, `test_rationale_max_cards_parsed` and `test_rationale_max_cards_out_of_range_raises`, modelled on tests/test_core_config_rationale.py:136-168.
   - **Boundaries:** Keep `max_cards` out of the agent-limit group in `ORG_ONLY_KEYS`. Do not touch src/whygraph/portal/overview_routes.py (t2), src/playground/src/api.ts, src/playground/src/pages/ProjectHome.tsx, src/playground/src/lib/overview.ts or src/playground/src/test/projectsOverview.test.tsx (t3).
   - **Constraints to preserve:** "`agent_generations_per_hour` and `agent_generations_per_member_per_hour` must pass `is_agent_limit`: a real int (not bool or float) from 0 to 10000. A violation raises `ConfigError`." / "Range and type validation of values such as `agent_generations_per_member_per_hour` (0–10000, bools rejected) belongs to `Config` validation, not to this builder."
   - **Risks:** "Adding a field to `RationaleConfig` without updating this builder and the `Config` validation (or the portal allowlists) would leave it silently unloaded or unvalidated." / "The defaults, field names and docs must stay in sync with the docs, the SPA org-defaults form and the golden or example config. The evidence names the docs and SPA form as consumers."
   - **Test first:** none
   - **Model:** haiku - a mechanical edit that copies the agent-limit fields.
   - **Verify:** `uv run pytest tests/test_core_config_rationale.py -q`

2. **Add the stored-card count**
   - **Files:** src/whygraph/mcp/rationale_cache.py:307-348, tests/test_mcp_rationale_cache.py
   - **Change:** Add public `count_cached() -> int` beside `store_cached` in rationale_cache.py. Count every `RationaleCache` row through `get_session`, across all providers and models. Leave `store_cached` and the lookup functions unchanged. Assert in the test that two stored cards count as 2. Assert that upserting an existing key leaves the count at 2.
   - **Boundaries:** Task t2 reads this helper from src/whygraph/mcp/rationale_cache.py, so keep the name `count_cached` and add only this function. Do not touch src/whygraph/portal/overview_routes.py (t2), src/playground/src/api.ts, src/playground/src/pages/ProjectHome.tsx, src/playground/src/lib/overview.ts or src/playground/src/test/projectsOverview.test.tsx (t3).
   - **Constraints to preserve:** "Changing the signature, the return value or the stored row shape affects the response's cached_at field and the cached_only paths (lookup_exact, lookup_overlapping, CachedCard) that read these rows."
   - **Risks:** none recorded
   - **Test first:** `tests/test_mcp_rationale_cache.py::test_count_cached_counts_stored_rows`
   - **Model:** haiku - one small query function, fully specified.
   - **Verify:** `uv run pytest tests/test_mcp_rationale_cache.py -k count_cached -q`

3. **Refuse a new card at the limit**
   - **Files:** src/whygraph/mcp/rationale.py:360-516, src/whygraph/mcp/rationale.py:575-631, src/playground/src/lib/api-error-codes.json, src/playground/src/lib/apiErrors.ts:321, tests/test_mcp_rationale.py
   - **Change:** Add keyword `max_cards: int = 0` to `rationale_card`. Insert the gate between the `usage_blocked` check (rationale.py:475) and `before_generate` (rationale.py:477). Refuse when `max_cards > 0`, `count_cached() >= max_cards` and `lookup_exact(target, provider, pinned_model)` is `None`. Allow an overwrite of an existing key at the limit, because `store_cached` upserts and the count does not grow (see Q2). Raise `WhyGraphError(message, code="rationale_card_limit")` directly. Use `WhyGraphError` because tests/test_error_codes.py scans its `code=` keyword, and it answers 400. Keep "not found" out of the message, because that text turns the status into 404. Leave `GenerationNotPermitted` unchanged, because `whygraph_error_handler` and `_mapped_errors` branch on its `reason`. Pass `max_cards=get_config().rationale.max_cards` from `whygraph_rationale_brief`, after the `ctx.remote` branch at rationale.py:618-619. Add `rationale_card_limit` in sorted position to api-error-codes.json. Add its message to `MESSAGES` in apiErrors.ts. Accept that concurrent misses can overshoot the limit by a few cards, and add no lock. Add `test_max_cards_still_serves_cache_hits`, `test_max_cards_cached_only_unaffected` and `test_max_cards_force_refresh_on_existing_key_allowed`. Assert in the refusal test that no LLM client is built.
   - **Boundaries:** External contracts: `rationale_card` keeps its existing behaviour for callers that omit `max_cards`. No change is needed in src/whygraph/mcp/errors.py because the refusal is a plain `WhyGraphError`. Do not touch src/whygraph/portal/overview_routes.py (t2), src/playground/src/api.ts, src/playground/src/pages/ProjectHome.tsx, src/playground/src/lib/overview.ts or src/playground/src/test/projectsOverview.test.tsx (t3).
   - **Constraints to preserve:** "It must be raised only on a cache miss, before `before_generate` and before any LLM client is built, so nothing is charged. Cache hits must still be served." / "With `cached_only` and no `force_refresh`, the function must never generate, backfill descriptions, store a card or call the LLM; `generated` is always false. `force_refresh` overrides `cached_only`." / "`force_refresh` skips the cache lookup but must still pass `before_generate`, `allow_description` and the `usage_blocked` budget gate, and it still stores the result (`test_force_refresh_still_honours_the_generation_gate`)."
   - **Risks:** "The gate order (cached_only, then cache lookup, then `before_generate`/`allow_description`/`usage_blocked`, then backfill, then generate and store) encodes the no-spend guarantees for Viewers and budget caps. Reordering it can spend money or return a 403 where a cached card should be served." / "The serve handler (`whygraph_error_handler`) and `v1_routes._mapped_errors` branch on `reason == BUDGET_EXCEEDED`. Changing the reason values or attribute names would silently turn a budget `403` with `scope` into the wrong status." / "The SPA and agents rely on the code and scope strings. `tests/test_error_codes.py` requires every backend code to have an SPA registry entry, so renaming a code breaks that test and the UI messages." / "Shadowing the FastMCP `ctx` with the project context (or reordering `_client_channel`) breaks progress reporting or linked routing."
   - **Test first:** `tests/test_mcp_rationale.py::test_max_cards_refuses_new_generation_when_full`
   - **Model:** opus - new gate design in the shared no-spend path.
   - **Verify:** `uv run pytest tests/test_mcp_rationale.py -k max_cards -q`, `uv run pytest tests/test_mcp_rationale.py -q`, `uv run pytest tests/test_error_codes.py -q`, `npm --prefix src/playground test`

4. **Pin that a linked project ignores the limit**
   - **Files:** src/whygraph/mcp/rationale.py:263-301, tests/test_mcp_linked.py
   - **Change:** Leave `linked_rationale` unchanged. Add a test with a linked context and `max_cards` set. Assert that `count_cached` is never called and the platform's card is returned.
   - **Boundaries:** Do not open a local database on this path. Do not touch src/whygraph/portal/overview_routes.py or src/whygraph/mcp/rationale_cache.py beyond step 2 (t2), src/playground/src/api.ts, src/playground/src/pages/ProjectHome.tsx, src/playground/src/lib/overview.ts or src/playground/src/test/projectsOverview.test.tsx (t3).
   - **Constraints to preserve:** "Never read the local rationale cache, never call an LLM, and never open a local WhyGraph DB for a linked project; the engine guard in db/engines refuses a bound context that has remote set." / "Linked (platform) projects ignore `cached_only`, because the platform serves their cards."
   - **Risks:** "Any path that opens a local DB or session for a linked project would break the engine guard and could leave a stray whygraph.db beside the checkout."
   - **Test first:** `tests/test_mcp_linked.py::test_linked_rationale_ignores_max_cards`
   - **Model:** haiku - one test against unchanged code.
   - **Verify:** `uv run pytest tests/test_mcp_linked.py -q`

5. **Pin that the platform route is exempt**
   - **Files:** src/whygraph/portal/v1_routes.py:503-539, tests/test_portal_v1.py
   - **Change:** Leave `post_rationale` unchanged, so it omits `max_cards` and `rationale_card` applies no limit. Keep the route exempt because the org card budget already bounds it. Keep the route exempt because its error codes are pinned by contract fixtures. Add `test_rationale_max_cards_does_not_apply_on_platform` to tests/test_portal_v1.py.
   - **Boundaries:** External contract: the `/api/v1` error envelope and tests/test_api_v1_contract.py fixtures. Do not touch src/whygraph/portal/overview_routes.py (t2), src/playground/src/api.ts, src/playground/src/pages/ProjectHome.tsx, src/playground/src/lib/overview.ts or src/playground/src/test/projectsOverview.test.tsx (t3).
   - **Constraints to preserve:** "A cache miss must spend the org's card budget only after confirming a rationale key is configured, so nothing is charged for a card that cannot be generated. A cache hit is served either way."
   - **Risks:** "Changing the error codes or envelope breaks the recorded contract fixtures (`test_api_v1_contract.py`) and the local client's `FakePlatform` replay."
   - **Test first:** none
   - **Model:** sonnet - one test modelled on the generation-limit tests at tests/test_portal_v1.py:682-800.
   - **Verify:** `uv run pytest tests/test_portal_v1.py tests/test_api_v1_contract.py -q`

6. **Pin that Chat and the Explorer inherit the limit**
   - **Files:** src/whygraph/chat/tools.py:1279-1328, src/whygraph/serve/routes.py:204-212, tests/test_chat_tools.py, tests/test_serve_phase2.py
   - **Change:** Leave `_get_rationale` and `rationale_generate` unchanged, because both call `whygraph_rationale_brief` and so inherit the limit. Let the refusal in `_get_rationale` follow the existing `WhyGraphError` path that a viewer refusal takes. Add `test_get_rationale_reports_max_cards_refusal` to tests/test_chat_tools.py. Assert there that the tool reports the refusal as a tool error with code `rationale_card_limit`. Add `test_rationale_generate_maps_max_cards_refusal` to tests/test_serve_phase2.py. Assert there that the route answers 400 with code `rationale_card_limit`.
   - **Boundaries:** Do not touch src/whygraph/portal/overview_routes.py (t2), src/playground/src/api.ts, src/playground/src/pages/ProjectHome.tsx, src/playground/src/lib/overview.ts or src/playground/src/test/projectsOverview.test.tsx (t3).
   - **Constraints to preserve:** "Generation must go through `whygraph_rationale_brief` unchanged, so the audited generate-and-cache flow and cache rows cannot drift."
   - **Risks:** none recorded
   - **Test first:** none
   - **Model:** sonnet - two tests that copy existing refusal tests.
   - **Verify:** `uv run pytest tests/test_chat_tools.py -q`, `uv run pytest tests/test_serve_phase2.py -q`

7. **Make the key writable by PUT and not by import**
   - **Files:** src/whygraph/portal/policy.py:83-92, src/whygraph/portal/policy.py:104-137, tests/test_portal_v1.py:928, tests/test_portal_v1.py:1091
   - **Change:** Exclude `max_cards` from `IMPORT_ALLOWLIST["rationale"]` at policy.py:107. Add `"rationale": {**IMPORT_ALLOWLIST["rationale"], "max_cards": True}` to `PUT_ALLOWLIST`. Leave `ORG_ONLY_KEYS` unchanged. Note that `PRODUCTION_PUT_ALLOWLIST` and `DEFAULTS_ALLOWLIST` derive from `PUT_ALLOWLIST`, so they need no edit. Note that `LINKED_PUT_ALLOWLIST` needs no edit, so a linked project cannot write the key. Add `test_max_cards_settable_per_project`, `test_max_cards_not_in_org_only_keys` and `test_max_cards_not_importable_from_repo`.
   - **Boundaries:** External contract: the four allowlists in src/whygraph/portal/policy.py. Do not touch src/whygraph/portal/overview_routes.py (t2), src/playground/src/api.ts, src/playground/src/pages/ProjectHome.tsx, src/playground/src/lib/overview.ts or src/playground/src/test/projectsOverview.test.tsx (t3).
   - **Constraints to preserve:** "Both agent limits are org-layer-only. They stay in `ORG_ONLY_KEYS`, out of the import allowlist and the project PUT allowlists, and are readable only from the org layer row, never from a merged project config. A repo is untrusted input."
   - **Risks:** "Moving any of the agent limits into the project allowlist or the import allowlist would let an untrusted repo raise its own spend ceiling. A test, `test_agent_limits_not_importable_from_repo`, guards this."
   - **Test first:** `tests/test_portal_v1.py::test_max_cards_settable_per_project`
   - **Model:** sonnet - a small allowlist edit with tests that copy neighbours.
   - **Verify:** `uv run pytest tests/test_portal_v1.py -k max_cards -q`

8. **Document the key and the refusal**
   - **Files:** docs/reference/configuration.md:44, docs/reference/configuration.md:93-100, docs/reference/configuration.md:159, docs/reference/mcp.md:287, whygraph.example.toml:45-53
   - **Change:** Add a commented `# max_cards = 0` line under `[rationale]` in configuration.md. Add the same line to whygraph.example.toml. Describe `0` as no limit and the range as `0` to `10000`. State that the count covers every stored card, across providers and models. State that the key is settable per project, is not imported from a repo, and does not apply to linked projects. Add the import exception at configuration.md:44. Add a `rationale_card_limit` row beside the `generation_not_permitted` row at mcp.md:287. State there that cache hits, `cached_only` and overwrites of an existing card still work. State there that the limit covers the MCP tool, Chat and the Explorer but not `POST /api/v1/projects/{slug}/rationale`. Use plain hyphens, never em dashes.
   - **Boundaries:** Do not edit the Overview docs under docs/portal/, which t2 and t3 own. Do not touch src/whygraph/portal/overview_routes.py or src/whygraph/mcp/rationale_cache.py (t2), src/playground/src/api.ts, src/playground/src/pages/ProjectHome.tsx, src/playground/src/lib/overview.ts or src/playground/src/test/projectsOverview.test.tsx (t3).
   - **Constraints to preserve:** none recorded
   - **Risks:** "The defaults, field names and docs must stay in sync with the docs, the SPA org-defaults form and the golden or example config. The evidence names the docs and SPA form as consumers."
   - **Test first:** none
   - **Model:** haiku - mechanical docs edits, fully specified.
   - **Verify:** `make docs-build`

## Risks called out across the change
- "The gate order (cached_only, then cache lookup, then `before_generate`/`allow_description`/`usage_blocked`, then backfill, then generate and store) encodes the no-spend guarantees for Viewers and budget caps. Reordering it can spend money or return a 403 where a cached card should be served."
- "The serve handler (`whygraph_error_handler`) and `v1_routes._mapped_errors` branch on `reason == BUDGET_EXCEEDED`. Changing the reason values or attribute names would silently turn a budget `403` with `scope` into the wrong status."
- "The SPA and agents rely on the code and scope strings. `tests/test_error_codes.py` requires every backend code to have an SPA registry entry, so renaming a code breaks that test and the UI messages."
- "Any path that opens a local DB or session for a linked project would break the engine guard and could leave a stray whygraph.db beside the checkout."
- "Changing the error codes or envelope breaks the recorded contract fixtures (`test_api_v1_contract.py`) and the local client's `FakePlatform` replay."
- "Moving any of the agent limits into the project allowlist or the import allowlist would let an untrusted repo raise its own spend ceiling. A test, `test_agent_limits_not_importable_from_repo`, guards this."
- "Adding a field to `RationaleConfig` without updating this builder and the `Config` validation (or the portal allowlists) would leave it silently unloaded or unvalidated."
- "Shadowing the FastMCP `ctx` with the project context (or reordering `_client_channel`) breaks progress reporting or linked routing."
- "Changing the signature, the return value or the stored row shape affects the response's cached_at field and the cached_only paths (lookup_exact, lookup_overlapping, CachedCard) that read these rows."
- "The defaults, field names and docs must stay in sync with the docs, the SPA org-defaults form and the golden or example config. The evidence names the docs and SPA form as consumers."
- Uncovered by any existing test: the new count helper, the `max_cards` refusal in `rationale_card`, the linked-path case, and the portal policy for `max_cards`.

## Acceptance
1. The full test suite passes. - **Check:** `uv run pytest`
2. A call that would generate a new card at the limit is refused, while cache hits and `cached_only` calls keep working. - **Check:** `uv run pytest tests/test_mcp_rationale.py -k max_cards -q`
3. `[rationale].max_cards` parses, defaults to `0`, and rejects out-of-range values. - **Check:** `uv run pytest tests/test_core_config_rationale.py -q`
4. The key is writable per project, absent from `ORG_ONLY_KEYS`, and not importable from a repo. - **Check:** `uv run pytest tests/test_portal_v1.py -k max_cards -q`
5. A linked project and `POST /api/v1/projects/{slug}/rationale` ignore the limit. - **Check:** `uv run pytest tests/test_mcp_linked.py tests/test_portal_v1.py -k max_cards -q`
6. The docs describe the key and the refusal, and the strict build passes. - **Check:** `make docs-build`

## Confidence: medium
Every report is medium, and a spot-check confirmed the gate position, allowlists, error scan and error handler, but no history precedent exists for a count cap, route-level config PUT tests were not located, and tests/test_api_v1_contract.py, tests/test_chat_tools.py and tests/test_serve_phase2.py were not read.
