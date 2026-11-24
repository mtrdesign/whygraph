# Plan: Add a filter dropdown to the notes list

**Status:** PROPOSAL - nothing implemented yet; review the steps, then run the implement command on this file.
**Objective:** A member narrows the Notes page list by note kind with a dropdown, and the choice survives a reload as `?kind=`.
**Non-goals:** The four status tabs (`NOTE_FILTERS`) stay as they are. The Explorer Notes tab (`src/playground/src/components/NotesTab.tsx`), the Chat `find_notes` tool and the MCP surface get no new filter. No paging, sorting or search box changes.
**Read first:** src/playground/src/pages/NotesPage.tsx:32-112 (the list call and the tabs); src/playground/src/components/portal/ScanHistory.tsx:38-70 (the native-select `Filter` to copy); src/whygraph/mcp/notes.py:2051-2137 (the domain filters).

## Working set
5 symbols analyzed (0 truncated from impact set).

| Symbol | Location | Evidence |
|---|---|---|
| `list_notes` (domain) | src/whygraph/mcp/notes.py:2051-2165 | 3 commits, 1 PR (partial) |
| `list_notes` (route) | src/whygraph/portal/notes_routes.py:241-282 | 2 commits, 1 PR |
| `projectApi.notes` / `NoteFilters` | src/playground/src/api.ts:2224-2229, 2324-2332 | no card fetched |
| `NotesSearch` / `validateNotesSearch` | src/playground/src/lib/routeSearch.ts:115-129 | 6 commits, 3 PRs (partial) |
| `NotesPage` / `listCall` | src/playground/src/pages/NotesPage.tsx:32-150 | 5 commits, 3 PRs (partial) |

No change needed in src/whygraph/chat/tools.py:1104 and :1118 (`_project_wide_note_count`, `_find_notes`): they call the domain `list_notes` with keyword arguments, and the new `kind` keyword defaults to `None`. No change needed in src/playground/src/router.tsx:749-752: the notes route already uses `validateNotesSearch`.

## UI profile
- **Start:** `make dev-local` → http://localhost:5174 · **Fixture mode:** none · **Browser tests:** Playwright in src/playground/e2e/tests · **Auth:** none
- **Stack:** React 19 with TanStack Router, shadcn on Base UI, Tailwind 4, theme by class dark · **Conventions:** none

## Setup
- `uv sync` - installs the Python environment
- `npm --prefix src/playground ci` - installs the frontend packages
- Format: `uv run ruff format`

## Steps

1. **Domain: filter `list_notes` by kind**
   - **Files:** src/whygraph/mcp/notes.py:2051-2137, tests/test_mcp_notes.py:1052
   - **Change:** Add a keyword argument `kind: str | None = None` to `list_notes` in src/whygraph/mcp/notes.py. Raise `_invalid` when `kind` is not in `NOTE_KINDS`. Append `col(ProjectNote.kind) == kind` to `conditions`. Copy the `anchor_kind` check at src/whygraph/mcp/notes.py:2112-2115. Document `kind` in the docstring `Parameters` section beside `status, anchor_kind`.
   - **Boundaries:** src/whygraph/chat/tools.py (callers stay unchanged); `_refuse_linked()` stays the first call.
   - **Constraints to preserve:** "`whygraph_propose_note` must call `_refuse_linked()` first: linked projects have no WhyGraph DB, so note calls return `note_linked` with no DB access." (cited: 3c3f8020)
   - **Risks:** "Relaxing the `_refuse_linked()` guard would make the note tools touch a database that linked projects do not have; PR #90 notes \"Linked projects get no notes in this PR (they have no WhyGraph DB)\"." (quoted: 3c3f8020)
   - **Test first:** tests/test_mcp_notes.py::test_list_notes_filters_by_kind
   - **Model:** sonnet - copies the `anchor_kind` filter beside it.
   - **Verify:** `uv run pytest -n0 tests/test_mcp_notes.py -q -k list_notes`

2. **Route: accept `kind` on `GET /notes`**
   - **Files:** src/whygraph/portal/notes_routes.py:241-282, tests/test_portal_notes.py:213
   - **Change:** Add a query parameter `kind: str | None = None` to the route `list_notes` in src/whygraph/portal/notes_routes.py. Pass `kind=kind` to `notes.list_notes`. Add `kind` to the docstring line `status, anchor_kind, path, area, q`.
   - **Boundaries:** the write router and `/summary` stay unchanged; `_mapped()` keeps mapping `note_invalid` to 400.
   - **Constraints to preserve:** "Domain `WhyGraphError` codes must map through `_STATUS`; `ServeError` passes through and unmapped codes re-raise bare rather than being converted." (cited: e8e6090d); "Read routes are mounted under `PROJECT_READ` and write routes under `PROJECT_CHAT`." (cited: e8e6090d)
   - **Risks:** "Linked projects get no notes in this PR because they have no WhyGraph DB; changing the mount or guard can break the intended refusal behavior." (quoted: PR #90)
   - **Test first:** tests/test_portal_notes.py::test_list_filters_by_kind
   - **Model:** haiku - one passed-through query parameter.
   - **Verify:** `uv run pytest -n0 tests/test_portal_notes.py -q -k list_filters`

3. **Client: carry `kind` in the API call and the URL**
   - **Files:** src/playground/src/api.ts:2224-2229, src/playground/src/api.ts:2324-2332, src/playground/src/lib/routeSearch.ts:115-129
   - **Change:** Add `kind?: NoteKind` to `NoteFilters` in src/playground/src/api.ts. Set the `kind` param in `projectApi.notes` when `f.kind` is set. Add `NOTE_KIND_FILTERS` beside `NOTE_FILTERS` in src/playground/src/lib/routeSearch.ts, with the five `NoteKind` values. Add `kind?: NoteKind` to `NotesSearch`. Return `kind: oneOf(NOTE_KIND_FILTERS, search.kind)` from `validateNotesSearch`.
   - **Boundaries:** `NOTE_FILTERS` keeps its four values and order.
   - **Constraints to preserve:** "Every validator must return all of its keys explicitly, even when the value is undefined; a route match's search is the raw URL search overlaid with the validator's output, so an omitted key lets the unvalidated raw value through." (cited: a00c9c50); "Invalid or absent values must resolve to undefined via `oneOf`/`positiveInt`, not by throwing or substituting a default." (cited: e8e6090d); "`filter` is restricted to exactly the four Notes-page tabs, because the NotesPage maps each filter value to a distinct list call (`review`→`status=proposed`, `confirmed`, `attention`→`attention=true`, `retired`→`status=retired`)." (cited: e8e6090d); "Keep each route's constant array next to its validator so the UI and the validator read from one source rather than duplicating allowed values." (cited: a00c9c50)
   - **Risks:** "A validator that builds its result conditionally (omitting a key) silently lets the unvalidated raw URL value through — the file header calls this out as the easy-to-break, load-bearing invariant." (cited: a00c9c50); "Changing `NOTE_FILTERS` values or order breaks the NotesPage's filter→request mapping and its `\"review\"` default, and the review queue stops excluding confirmed notes." (cited: 53a3173a)
   - **Test first:** none
   - **Model:** sonnet - copies the `oneOf` validators of the same file.
   - **Verify:** `npm --prefix src/playground run typecheck`

4. **Notes page: add the Kind dropdown**
   - **Files:** src/playground/src/pages/NotesPage.tsx:32-112, src/playground/src/test/notes.test.tsx:104, src/playground/src/test/notes.test.tsx:197
   - **Change:** Read `kind` from `search` in `NotesPage`. Pass `kind` to `listCall` and spread it into each call. Add `kind` to the `list` query key after `filter`. Render a labelled native select "Kind" beside the `Tabs`. Copy `Filter` from src/playground/src/components/portal/ScanHistory.tsx:38-70, with `nativeSelect` from src/playground/src/components/portal/Field.tsx. Offer "Any" and each `KIND_LABEL` entry from src/playground/src/lib/notes.ts:13. Set `data-testid="filter-kind"`. Keep `kind` when a tab changes and on "Show the list". Show "No <kind label> notes in this list." when `kind` is set and the list is empty. Teach the fake portal in src/playground/src/test/notes.test.tsx:104 to honour `kind`. Add the test "filters the list by kind" under `describe("Notes page")`.
   - **Boundaries:** `ProjectPurposeCard` mount gate and placement; the linked-note section; `rejectCount` counting; src/playground/src/components/NotesTab.tsx.
   - **Constraints to preserve:** "The card must never mount for linked projects; the purpose routes are refused there, so the predicate is `project.data.source !== \"platform\" && project.data.initialized` and mounting must stay conditional on it." (cited: 58e1281d); "The project metadata query stays a separate `useQuery` keyed `projectKey(slug, \"project\")`; it is not merged into the notes `useInfiniteQuery`." (cited: 58e1281d); "Card placement is fixed: after the header `<div>` and before the `search.id !== undefined` linked-note section." (cited: 58e1281d)
   - **Changes on purpose:** "The existing notes query, filter handling and linked-note rendering must stay untouched by the purpose-card change." (cited: 58e1281d) - the task itself changes the notes query key and filter handling; the purpose card stays untouched.
   - **Risks:** "Editing the notes page around the new card risks disturbing the filter/tab behaviour, where UI labels differ from wire values and `filter`/`id` live in URL search." (cited: e8e6090d)
   - **Test first:** src/playground/src/test/notes.test.tsx::filters the list by kind
   - **Model:** sonnet - copies the `Filter` select of `ScanHistory`.
   - **Routes:** `/p/demo/notes`
   - **Expected UI:** Default: the "Kind" dropdown shows "Any" beside the tabs, and the list is unfiltered. Filtered: only notes of the chosen kind show, on every tab. Empty: the page shows "No Constraint notes in this list." Loading: the list skeleton shows under the dropdown. Error: the list `ErrorState` shows, and the dropdown stays usable. Phone width: the dropdown wraps below the tabs at full width. Dark: the dropdown uses the dark theme tokens.
   - **Verify:** `npm --prefix src/playground test -- src/test/notes.test.tsx`

5. **Docs: describe the Kind filter**
   - **Files:** docs/guide/notes.md:132-135
   - **Change:** Add one sentence after the four filters in docs/guide/notes.md. Name the **Kind** dropdown and its `?kind=` link form.
   - **Boundaries:** mkdocs.yml (no nav change).
   - **Constraints to preserve:** none recorded
   - **Risks:** none recorded
   - **Test first:** none
   - **Model:** haiku - one sentence of prose.
   - **Verify:** `make docs-build`

## Risks called out across the change
- "A validator that builds its result conditionally (omitting a key) silently lets the unvalidated raw URL value through — the file header calls this out as the easy-to-break, load-bearing invariant." (cited: a00c9c50)
- "Editing the notes page around the new card risks disturbing the filter/tab behaviour, where UI labels differ from wire values and `filter`/`id` live in URL search." (cited: e8e6090d)
- "Relaxing the `_refuse_linked()` guard would make the note tools touch a database that linked projects do not have; PR #90 notes \"Linked projects get no notes in this PR (they have no WhyGraph DB)\"." (quoted: 3c3f8020)
- The page pages server-side with `before_id`, so a client-side filter would show short pages. Mitigation: filter on the server (steps 1 and 2).

## Acceptance
1. The test files of the steps' Verify commands pass. - **Check:** `uv run pytest -n0 tests/test_mcp_notes.py tests/test_portal_notes.py -q`
2. The Kind dropdown narrows the notes list and keeps the status tab. - **Check:** `npm --prefix src/playground test -- src/test/notes.test.tsx`
3. The API rejects an unknown kind with 400 `note_invalid`. - **Check:** `uv run pytest -n0 tests/test_portal_notes.py -q -k kind`
4. The frontend type-checks with the new `kind` search key. - **Check:** `npm --prefix src/playground run typecheck`
5. The Python lint passes. - **Check:** `uv run ruff check src/ tests/ scripts/`
6. The docs build passes. - **Check:** `make docs-build`
7. The screenshots show the dropdown on each UI step. - **Check:** manual: open .whygraph/plans/plan57-baseline/ui/index.html and compare before and after for each UI step
8. The full test suite passes (CI). - **Check:** manual: uv run pytest

## Resolved decisions
| Q | Question | Answer |
|---|---|---|
| Q1 | What should the dropdown filter by? | default: Note kind (decision, constraint, gotcha, convention, purpose) |
| Q2 | Which states should `/p/demo/notes` have? | default: As proposed: default, filtered, empty, loading, error, phone width, dark |

## Open questions
- **Q3.** Should the dropdown offer "Project purpose"? **Options:** Yes, all five kinds / No, the four proposable kinds only **Default:** Yes, all five kinds **Plan-shaping:** no - the purpose note shows in the list today.

## Confidence: medium
4 of 5 working-set nodes have cards with 2 to 6 commits; 3 cards are partial, and the domain card describes the propose surface more than `list_notes`. Task wording leaves the filter dimension open (Q1). Dry run: item 1 did not finish in 540 s; item 1 now runs the steps' test files and item 8 is the full suite for CI.

## Implementation log

- Baseline: 2 of 5 Verify commands pass (2026-11-16T18:09:00Z)
  - Step 1: expected (Test first)
- [x] Step 1: Domain: filter `list_notes` by kind (verified at 2026-11-16T18:11:22Z)
- [x] Step 2: Route: accept `kind` on `GET /notes` (verified at 2026-11-16T18:13:14Z)
- [x] Step 3: Client: carry `kind` in the API call and the URL (verified at 2026-11-16T18:14:33Z)
- [x] Step 4: Notes page: add the Kind dropdown (verified at 2026-11-16T18:16:06Z)
- [x] Step 5: Docs: describe the Kind filter (verified at 2026-11-16T18:17:32Z)
- Acceptance: passed (2026-11-16T18:20:14Z)
- Polish: applied 2 of 2 findings (2026-11-16T18:21:44Z)
  - F1: applied
  - F2: applied
