CREATE TABLE runtime_heads (
    run_id text PRIMARY KEY,
    sequence bigint NOT NULL CHECK (sequence > 0),
    digest text NOT NULL,
    state jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE runtime_items (
    run_id text NOT NULL REFERENCES runtime_heads(run_id),
    collection text NOT NULL,
    item_key text NOT NULL,
    value jsonb NOT NULL,
    PRIMARY KEY (run_id, collection, item_key)
);

CREATE TABLE runtime_events (
    run_id text NOT NULL REFERENCES runtime_heads(run_id),
    sequence bigint NOT NULL,
    previous_digest text NOT NULL,
    digest text NOT NULL,
    delta jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, sequence)
);
