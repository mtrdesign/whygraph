-- The portal DB schema WhyGraph 2.0.0 ships (portal head c5e8a1d2b3f4), frozen
-- from `sqlite3 portal.db .schema` after `alembic upgrade head` on an
-- empty file. Never edit: the 2.1 importer reads exactly this shape.
CREATE TABLE alembic_version (
	version_num VARCHAR(32) NOT NULL, 
	CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);
CREATE TABLE settings (
	id INTEGER NOT NULL, 
	mode TEXT NOT NULL, 
	created_at TEXT NOT NULL, 
	schema_version TEXT, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_settings_mode CHECK (mode IN ('local', 'production')), 
	CONSTRAINT ck_settings_singleton CHECK (id = 1)
);
CREATE TABLE users (
	id INTEGER NOT NULL, 
	uid TEXT NOT NULL, 
	display_name TEXT NOT NULL, 
	username TEXT, 
	password_hash TEXT, 
	role TEXT NOT NULL, 
	created_at TEXT NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_users_uid UNIQUE (uid)
);
CREATE TABLE projects (
	id INTEGER NOT NULL, 
	slug TEXT NOT NULL, 
	name TEXT NOT NULL, 
	source TEXT NOT NULL, 
	root TEXT NOT NULL, 
	remote_url TEXT, 
	initialized_at TEXT, 
	last_scan_at TEXT, 
	last_scanned_head TEXT, 
	created_by INTEGER, 
	created_at TEXT NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_projects_source CHECK (source IN ('local', 'github')), 
	CONSTRAINT fk_projects_created_by FOREIGN KEY(created_by) REFERENCES users (id) ON DELETE SET NULL, 
	CONSTRAINT uq_projects_root UNIQUE (root), 
	CONSTRAINT uq_projects_slug UNIQUE (slug)
);
CREATE TABLE project_agents (
	project_id INTEGER NOT NULL, 
	agent TEXT NOT NULL, 
	configured_at TEXT NOT NULL, 
	CONSTRAINT pk_project_agents PRIMARY KEY (project_id, agent), 
	CONSTRAINT ck_project_agents_agent CHECK (agent IN ('claude', 'cursor', 'vscode', 'codex')), 
	CONSTRAINT fk_project_agents_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE
);
CREATE TABLE project_config (
	id INTEGER NOT NULL, 
	project_id INTEGER, 
	config JSON NOT NULL, 
	updated_at TEXT NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT fk_project_config_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX uq_project_config_project ON project_config (project_id) WHERE project_id IS NOT NULL;
CREATE UNIQUE INDEX uq_project_config_global ON project_config ((1)) WHERE project_id IS NULL;
CREATE TABLE scan_runs (
	id INTEGER NOT NULL, 
	project_id INTEGER NOT NULL, 
	kind TEXT NOT NULL, 
	"trigger" TEXT NOT NULL, 
	"analyze" BOOLEAN NOT NULL, 
	requested_by INTEGER, 
	status TEXT NOT NULL, 
	started_at TEXT, 
	finished_at TEXT, 
	events_path TEXT, 
	log_path TEXT, 
	summary TEXT, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_scan_runs_kind CHECK (kind IN ('scan', 'sync')), 
	CONSTRAINT ck_scan_runs_status CHECK (status IN ('queued', 'running', 'ok', 'failed', 'interrupted', 'cancelled')), 
	CONSTRAINT ck_scan_runs_trigger CHECK (trigger IN ('initial', 'manual', 'describe', 'hook', 'poll', 'sync')), 
	CONSTRAINT fk_scan_runs_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE, 
	CONSTRAINT fk_scan_runs_requested_by FOREIGN KEY(requested_by) REFERENCES users (id) ON DELETE SET NULL
);
CREATE INDEX ix_scan_runs_project_id ON scan_runs (project_id);
CREATE TABLE IF NOT EXISTS "secrets" (
	id INTEGER NOT NULL, 
	project_id INTEGER, 
	kind TEXT NOT NULL, 
	provider TEXT, 
	ciphertext TEXT NOT NULL, 
	hint TEXT NOT NULL, 
	created_at TEXT NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_secrets_provider CHECK ((kind = 'llm_api_key') = (provider IS NOT NULL)), 
	CONSTRAINT fk_secrets_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE, 
	CONSTRAINT ck_secrets_kind CHECK (kind IN ('llm_api_key', 'github_token', 'claude_oauth_token'))
);
CREATE UNIQUE INDEX uq_secrets_project ON secrets (project_id, kind, coalesce(provider, '')) WHERE project_id IS NOT NULL;
CREATE UNIQUE INDEX uq_secrets_global ON secrets (kind, coalesce(provider, '')) WHERE project_id IS NULL;
