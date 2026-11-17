-- Forward-only migration. Dropping the schema would delete production history.

CREATE TABLE triggers (
    id text PRIMARY KEY,
    row_order bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    config jsonb NOT NULL,
    state text NOT NULL CHECK (state IN ('running', 'pausing', 'paused', 'completed', 'failed')),
    reason text,
    cursor text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX production_one_active_trigger
    ON triggers ((1)) WHERE state IN ('running', 'pausing');

CREATE TABLE entities (
    trigger_id text NOT NULL REFERENCES triggers(id),
    identity text NOT NULL,
    run_id text NOT NULL UNIQUE,
    row_order bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    name text NOT NULL,
    entity_type text NOT NULL,
    state text NOT NULL CHECK (state IN ('running', 'paused', 'completed', 'failed', 'stopped')),
    quota integer NOT NULL CHECK (quota > 0),
    error text,
    progress jsonb,
    PRIMARY KEY (trigger_id, identity)
);

CREATE INDEX production_entities_identity ON entities (identity, row_order DESC);
CREATE INDEX production_entities_trigger_order ON entities (trigger_id, row_order DESC);

CREATE TABLE questions (
    id text PRIMARY KEY,
    trigger_id text NOT NULL REFERENCES triggers(id),
    run_id text NOT NULL,
    row_order bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    payload jsonb NOT NULL,
    counted boolean NOT NULL
);

-- Single-entity releases may carry a run ID without a production entity row.
CREATE INDEX production_questions_run ON questions (run_id);
CREATE INDEX production_questions_trigger_order ON questions (trigger_id, row_order DESC);
