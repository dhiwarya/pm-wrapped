CREATE TABLE proposal (
  id INTEGER PRIMARY KEY,
  snapshot_id INTEGER NOT NULL REFERENCES snapshot(id) ON DELETE CASCADE,
  gate_id INTEGER NOT NULL REFERENCES gate(id) ON DELETE CASCADE,
  value TEXT NOT NULL CHECK (value IN ('ON_TRACK', 'AT_RISK', 'LATE', 'UNKNOWN')),
  floor TEXT NOT NULL CHECK (floor IN ('ON_TRACK', 'AT_RISK', 'LATE', 'UNKNOWN')),
  floor_rule TEXT NOT NULL,
  produced_by TEXT NOT NULL CHECK (produced_by IN ('RULES', 'MODEL')),
  reasons TEXT NOT NULL,
  UNIQUE (snapshot_id, gate_id)
);

CREATE TABLE change (
  id INTEGER PRIMARY KEY,
  snapshot_id INTEGER NOT NULL REFERENCES snapshot(id) ON DELETE CASCADE,
  project_id INTEGER REFERENCES project(id) ON DELETE CASCADE,
  grp TEXT NOT NULL,
  subject_type TEXT NOT NULL,
  subject_key TEXT NOT NULL,
  kind TEXT NOT NULL,
  change TEXT NOT NULL CHECK (change IN ('ADDED', 'REMOVED', 'CHANGED')),
  old TEXT, new TEXT,
  source TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX change_snapshot ON change(snapshot_id, project_id);

CREATE TABLE digest (
  id INTEGER PRIMARY KEY,
  snapshot_id INTEGER NOT NULL UNIQUE REFERENCES snapshot(id) ON DELETE CASCADE,
  day TEXT NOT NULL UNIQUE,
  text TEXT NOT NULL,
  cards TEXT NOT NULL,
  changes TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE model_read (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
  project_id INTEGER REFERENCES project(id) ON DELETE CASCADE,
  stage TEXT NOT NULL CHECK (stage IN ('ST-4', 'ST-5')),
  provider TEXT NOT NULL,
  seconds REAL NOT NULL,
  cost REAL NOT NULL,
  kept INTEGER NOT NULL DEFAULT 0,
  dropped INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX model_read_run ON model_read(run_id);

ALTER TABLE onboarding ADD COLUMN used_codex INTEGER NOT NULL DEFAULT 0;

CREATE TABLE model_source (
  snapshot_id INTEGER NOT NULL REFERENCES snapshot(id) ON DELETE CASCADE,
  source_id INTEGER NOT NULL REFERENCES source(id) ON DELETE CASCADE,
  commit_sha TEXT NOT NULL,
  read_at TEXT NOT NULL,
  PRIMARY KEY (snapshot_id, source_id)
);
