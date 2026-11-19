# WhyGraph (unreleased)

Changes that are on `main` and not in a release yet.

## Fixes

- **One scrollbar.** A long page, such as the admin Configuration page with its sign-up allowlist, no
  longer shows a second scrollbar and blank space below the page.
- **Settings remember their section.** Picking a section in the list beside a settings page (project
  and organization settings, and the admin Configuration page) puts it in the address
  (`?section=<id>`), so a refresh opens the page at that section. The admin Configuration page now
  takes `?section=` too.
- **Clear filters.** Every filter row has a **Clear filters** link while any filter is set: the
  Projects page, a project's Scans, Usage's Calls, the organization's Audit page, and the admin
  Organizations, Projects, Users and Audit lists (and an organization's Audit tab). It puts the search,
  every select, the sort and the dates back to their defaults; Usage keeps its date range.
