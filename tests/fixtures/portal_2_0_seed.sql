-- Seed rows for the frozen 2.0 portal schema (portal_2_0_schema.sql).
-- Secret ciphertexts were produced by whygraph.portal.secrets.encrypt with
-- portal_2_0_secret.key - never by hand. Plaintexts: tests/conftest.py
-- LEGACY_SECRET_PLAINTEXTS. Covers every importer case of plan section 7.1:
-- two projects, a global and a project config layer, a global and a project
-- secret of the same provider, three scan runs with a gap in ids (1, 2, 4),
-- a NULL created_by, both booleans, and JSON config.
INSERT INTO alembic_version (version_num) VALUES ('c5e8a1d2b3f4');
INSERT INTO users (id, uid, display_name, username, password_hash, role, created_at) VALUES (1, '6f1c2b9e-3d4a-4c5b-8e7f-9a0b1c2d3e4f', 'Ada', NULL, NULL, 'owner', '2026-09-01T09:00:00+00:00');
INSERT INTO settings (id, mode, created_at, schema_version) VALUES (1, 'local', '2026-09-01T09:00:00+00:00', NULL);
INSERT INTO projects (id, slug, name, source, root, remote_url, initialized_at, last_scan_at, last_scanned_head, created_by, created_at) VALUES (1, 'alpha', 'Alpha', 'local', '/home/ada/code/alpha', 'git@github.com:ada/alpha.git', '2026-09-01T09:10:00+00:00', '2026-09-02T08:00:00+00:00', '3f2a1b0c9d8e7f6a5b4c3d2e1f0a9b8c7d6e5f4a', 1, '2026-09-01T09:05:00+00:00');
INSERT INTO projects (id, slug, name, source, root, remote_url, initialized_at, last_scan_at, last_scanned_head, created_by, created_at) VALUES (2, 'beta', 'beta repo', 'local', '/home/ada/code/beta', NULL, NULL, NULL, NULL, NULL, '2026-09-01T09:20:00+00:00');
INSERT INTO project_agents (project_id, agent, configured_at) VALUES (1, 'claude', '2026-09-01T09:10:00+00:00');
INSERT INTO project_agents (project_id, agent, configured_at) VALUES (1, 'codex', '2026-09-01T09:10:00+00:00');
INSERT INTO project_config (id, project_id, config, updated_at) VALUES (1, NULL, '{"llm": {"model": "anthropic/claude-sonnet-4-5", "openai": {"base_url": "https://gw.example/v1"}}, "analyze": {"max_workers": 4}}', '2026-09-01T09:30:00+00:00');
INSERT INTO project_config (id, project_id, config, updated_at) VALUES (2, 1, '{"scan": {"hooks": ["post-commit", "post-merge"], "default_branch": "main"}, "analyze": {"max_workers": null}}', '2026-09-01T09:35:00+00:00');
INSERT INTO secrets (id, project_id, kind, provider, ciphertext, hint, created_at) VALUES (1, NULL, 'llm_api_key', 'anthropic', 'gAAAAABqvm-dlF7jfzyjPEYs-fLqzF3_Vx2AL3RRwjM34Lso-6FJjtQoKNLxkqJHTtM75f5SVg9EIJoy3-f7eF2Zy1NN4ilEnrbATLzorNjEbKCYE6pEg1M=', '…a1b2', '2026-09-01T10:00:00+00:00');
INSERT INTO secrets (id, project_id, kind, provider, ciphertext, hint, created_at) VALUES (2, 1, 'llm_api_key', 'anthropic', 'gAAAAABqvm-d3NrnR_qATbFjBeytmFRBARpqoDreP9twHiuJ6XMIZWLNucvfZ6oCGVZpx3YgcmPrHtnGv6bhV-I1INI3bxa3wAMdI0ZwgSkHoKg1Sm1rMiU=', '…c3d4', '2026-09-01T10:05:00+00:00');
INSERT INTO secrets (id, project_id, kind, provider, ciphertext, hint, created_at) VALUES (3, NULL, 'github_token', NULL, 'gAAAAABqvm-dFnPqj_yxg0xP_n7Oz0UrWd2wspuKCui9dkXUva47F0u3ctEcw1020syRnRJwCRRJn849BCr4ePXlZmJvWlbP8azZtsEOOZRj9tD7IU80pSs=', '…e5f6', '2026-09-01T10:10:00+00:00');
INSERT INTO secrets (id, project_id, kind, provider, ciphertext, hint, created_at) VALUES (5, 2, 'claude_oauth_token', NULL, 'gAAAAABqvm-dyj60broLo8Yz0qiXvJXSiu2oV3H3BKZkC99TULP6QV3sjIOFnE5W5IW8b8seR7kNX8v63xI1kw6zSz7G2wa9jPOkVFTnWbW1zQIhFM0gXww=', '…g7h8', '2026-09-01T10:15:00+00:00');
INSERT INTO scan_runs (id, project_id, kind, trigger, analyze, requested_by, status, started_at, finished_at, events_path, log_path, summary) VALUES (1, 1, 'scan', 'initial', 0, 1, 'ok', '2026-09-01T09:40:00+00:00', '2026-09-01T09:41:00+00:00', 'runs/1.jsonl', 'runs/1.log', '{"commits": 12, "codegraph": "init"}');
INSERT INTO scan_runs (id, project_id, kind, trigger, analyze, requested_by, status, started_at, finished_at, events_path, log_path, summary) VALUES (2, 1, 'scan', 'manual', 1, NULL, 'failed', '2026-09-01T11:00:00+00:00', '2026-09-01T11:02:00+00:00', 'runs/2.jsonl', 'runs/2.log', '{"error": "analyze failed"}');
INSERT INTO scan_runs (id, project_id, kind, trigger, analyze, requested_by, status, started_at, finished_at, events_path, log_path, summary) VALUES (4, 2, 'scan', 'hook', 0, NULL, 'interrupted', '2026-09-02T08:00:00+00:00', NULL, 'runs/4.jsonl', 'runs/4.log', NULL);
