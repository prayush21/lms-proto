CREATE ROLE app_user LOGIN PASSWORD 'app' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE tiers (
    id smallserial PRIMARY KEY,
    name text NOT NULL UNIQUE,
    max_concurrent_attempts integer NOT NULL CHECK (max_concurrent_attempts > 0)
);

CREATE TABLE tenants (
    id uuid PRIMARY KEY,
    slug text NOT NULL UNIQUE,
    name text NOT NULL,
    tier_id smallint NOT NULL REFERENCES tiers(id),
    is_platform boolean NOT NULL DEFAULT false
);

CREATE TABLE users (
    id uuid PRIMARY KEY,
    email text NOT NULL UNIQUE,
    display_name text NOT NULL
);

CREATE TABLE memberships (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    user_id uuid NOT NULL REFERENCES users(id),
    role text NOT NULL CHECK (role IN ('student', 'author', 'proctor', 'admin')),
    PRIMARY KEY (tenant_id, user_id)
);

CREATE TABLE questions (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    id uuid NOT NULL,
    current_version_id uuid,
    created_by uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, created_by) REFERENCES memberships(tenant_id, user_id)
);

CREATE TABLE question_versions (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL,
    question_id uuid NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    subject text NOT NULL,
    topic text NOT NULL,
    subtopic text NOT NULL DEFAULT '',
    difficulty text NOT NULL CHECK (difficulty IN ('easy', 'medium', 'hard')),
    stem text NOT NULL,
    options jsonb NOT NULL CHECK (jsonb_typeof(options) = 'array'),
    correct_option_id text NOT NULL,
    explanation text NOT NULL DEFAULT '',
    created_by uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, question_id, id),
    UNIQUE (tenant_id, question_id, version),
    FOREIGN KEY (tenant_id, question_id) REFERENCES questions(tenant_id, id),
    FOREIGN KEY (tenant_id, created_by) REFERENCES memberships(tenant_id, user_id)
);

ALTER TABLE questions ADD CONSTRAINT questions_current_version_fk
    FOREIGN KEY (tenant_id, id, current_version_id)
        REFERENCES question_versions(tenant_id, question_id, id);

CREATE TABLE blueprints (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    id uuid NOT NULL,
    name text NOT NULL,
    created_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, created_by) REFERENCES memberships(tenant_id, user_id)
);

CREATE TABLE blueprint_sections (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL,
    blueprint_id uuid NOT NULL,
    position integer NOT NULL CHECK (position >= 0),
    name text NOT NULL,
    time_limit_seconds integer NOT NULL CHECK (time_limit_seconds > 0),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, blueprint_id, position),
    FOREIGN KEY (tenant_id, blueprint_id) REFERENCES blueprints(tenant_id, id)
);

CREATE TABLE blueprint_items (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL,
    section_id uuid NOT NULL,
    position integer NOT NULL CHECK (position >= 0),
    question_owner_tenant_id uuid NOT NULL,
    question_version_id uuid NOT NULL,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, section_id, position),
    FOREIGN KEY (tenant_id, section_id) REFERENCES blueprint_sections(tenant_id, id),
    FOREIGN KEY (question_owner_tenant_id, question_version_id)
        REFERENCES question_versions(tenant_id, id)
);

CREATE TABLE attempts (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    kind text NOT NULL CHECK (kind IN ('mock', 'quiz')),
    status text NOT NULL CHECK (status IN ('in_progress', 'submitted')),
    blueprint_id uuid,
    quiz_spec jsonb,
    started_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    submitted_at timestamptz,
    submission_trigger text CHECK (submission_trigger IN ('student', 'deadline', 'proctor')),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, user_id) REFERENCES memberships(tenant_id, user_id),
    FOREIGN KEY (tenant_id, blueprint_id) REFERENCES blueprints(tenant_id, id)
);
CREATE INDEX attempts_active_idx ON attempts (tenant_id, status, expires_at);

CREATE TABLE attempt_sections (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL,
    attempt_id uuid NOT NULL,
    position integer NOT NULL,
    name text NOT NULL,
    time_limit_seconds integer NOT NULL,
    starts_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, attempt_id, id),
    UNIQUE (tenant_id, attempt_id, position),
    FOREIGN KEY (tenant_id, attempt_id) REFERENCES attempts(tenant_id, id)
);

CREATE TABLE attempt_items (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL,
    attempt_id uuid NOT NULL,
    attempt_section_id uuid NOT NULL,
    position integer NOT NULL,
    question_owner_tenant_id uuid NOT NULL,
    question_version_id uuid NOT NULL,
    shuffle_seed bigint NOT NULL,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, attempt_id, id),
    UNIQUE (tenant_id, attempt_section_id, position),
    FOREIGN KEY (tenant_id, attempt_id) REFERENCES attempts(tenant_id, id),
    FOREIGN KEY (tenant_id, attempt_id, attempt_section_id)
        REFERENCES attempt_sections(tenant_id, attempt_id, id),
    FOREIGN KEY (question_owner_tenant_id, question_version_id)
        REFERENCES question_versions(tenant_id, id)
);

CREATE TABLE attempt_answers (
    tenant_id uuid NOT NULL,
    attempt_id uuid NOT NULL,
    attempt_item_id uuid NOT NULL,
    selected_option_id text NOT NULL,
    client_revision bigint NOT NULL CHECK (client_revision >= 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, attempt_id, attempt_item_id),
    FOREIGN KEY (tenant_id, attempt_id) REFERENCES attempts(tenant_id, id),
    FOREIGN KEY (tenant_id, attempt_id, attempt_item_id)
        REFERENCES attempt_items(tenant_id, attempt_id, id)
);

CREATE TABLE submissions (
    tenant_id uuid NOT NULL,
    attempt_id uuid NOT NULL,
    trigger text NOT NULL CHECK (trigger IN ('student', 'deadline', 'proctor')),
    submitted_at timestamptz NOT NULL,
    score_status text NOT NULL DEFAULT 'queued' CHECK (score_status IN ('queued', 'complete', 'failed')),
    PRIMARY KEY (tenant_id, attempt_id),
    FOREIGN KEY (tenant_id, attempt_id) REFERENCES attempts(tenant_id, id)
);

CREATE TABLE scores (
    tenant_id uuid NOT NULL,
    attempt_id uuid NOT NULL,
    correct_count integer NOT NULL,
    question_count integer NOT NULL,
    percentage numeric(5,2) NOT NULL,
    breakdown jsonb NOT NULL,
    scored_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, attempt_id),
    FOREIGN KEY (tenant_id, attempt_id) REFERENCES attempts(tenant_id, id)
);

CREATE TABLE reports (
    tenant_id uuid NOT NULL,
    attempt_id uuid NOT NULL,
    overall jsonb NOT NULL,
    per_subject jsonb NOT NULL,
    per_topic jsonb NOT NULL,
    percentile numeric(5,2) NOT NULL,
    generated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, attempt_id),
    FOREIGN KEY (tenant_id, attempt_id) REFERENCES attempts(tenant_id, id)
);

CREATE TABLE outbox (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    id uuid NOT NULL,
    event_type text NOT NULL CHECK (event_type IN ('score.requested', 'report.requested')),
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    publish_attempts integer NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, id)
);
CREATE INDEX outbox_pending_idx ON outbox (tenant_id, created_at) WHERE published_at IS NULL;

CREATE TABLE processed_jobs (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    job_id uuid NOT NULL,
    job_type text NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, job_id, job_type)
);

CREATE FUNCTION current_tenant_id() RETURNS uuid LANGUAGE sql STABLE AS $$
    SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid
$$;

-- The platform tenant is a reserved catalog owner. It is readable, never writable,
-- through a customer transaction. The rejected alternative was nullable tenant IDs,
-- which make composite ownership constraints much weaker.
CREATE FUNCTION platform_tenant_id() RETURNS uuid LANGUAGE sql IMMUTABLE AS $$
    SELECT '00000000-0000-0000-0000-000000000001'::uuid
$$;

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'memberships','blueprints','blueprint_sections','blueprint_items','attempts',
    'attempt_sections','attempt_items','attempt_answers','submissions','scores',
    'reports','outbox','processed_jobs'
  ] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format(
      'CREATE POLICY tenant_isolation ON %I USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id())', t
    );
  END LOOP;
END $$;

ALTER TABLE questions ENABLE ROW LEVEL SECURITY;
ALTER TABLE questions FORCE ROW LEVEL SECURITY;
CREATE POLICY question_read ON questions FOR SELECT
  USING (tenant_id IN (current_tenant_id(), platform_tenant_id()));
CREATE POLICY question_write ON questions FOR ALL
  USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());

ALTER TABLE question_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE question_versions FORCE ROW LEVEL SECURITY;
CREATE POLICY version_read ON question_versions FOR SELECT
  USING (tenant_id IN (current_tenant_id(), platform_tenant_id()));
CREATE POLICY version_write ON question_versions FOR ALL
  USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());

GRANT USAGE ON SCHEMA public TO app_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_user;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_user;
GRANT EXECUTE ON FUNCTION current_tenant_id(), platform_tenant_id() TO app_user;
REVOKE INSERT, UPDATE, DELETE ON tiers, tenants, users, memberships FROM app_user;
-- Versions are append-only for the application role. Editing means inserting a
-- new row and moving questions.current_version_id.
REVOKE UPDATE, DELETE ON question_versions FROM app_user;
