-- Hand tables of Phase 1 (Detailed Design Part 2 §6-2).
-- Hand tables are written by the web process only. Change-log triggers are created
-- by pmd.data.db.ensure_change_log_triggers after every migration.

CREATE TABLE project (
  id          INTEGER PRIMARY KEY,
  key         TEXT NOT NULL UNIQUE
              CHECK (length(key) BETWEEN 2 AND 12 AND key NOT GLOB '*[^A-Z0-9-]*'),
  name        TEXT NOT NULL CHECK (length(trim(name)) > 0),
  description TEXT NOT NULL DEFAULT '',
  scope_note  TEXT NOT NULL DEFAULT '',
  state       TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (state IN ('ACTIVE', 'PAUSED', 'ARCHIVED')),
  created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE project_setting (
  project_id INTEGER PRIMARY KEY REFERENCES project(id) ON DELETE CASCADE,
  colour     TEXT NOT NULL CHECK (colour IN ('VIOLET', 'BLUE', 'TANGERINE', 'PINK', 'LIME'))
);

CREATE TABLE person (
  id           INTEGER PRIMARY KEY,
  display_name TEXT NOT NULL UNIQUE COLLATE NOCASE CHECK (length(trim(display_name)) > 0)
);

-- A gate is never deleted, only closed, so a gate key is never reused (R-H4).
CREATE TABLE gate (
  id           INTEGER PRIMARY KEY,
  project_id   INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
  key          TEXT NOT NULL CHECK (length(key) BETWEEN 1 AND 16),
  name         TEXT NOT NULL CHECK (length(trim(name)) > 0),
  date         TEXT NOT NULL CHECK (date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  description  TEXT NOT NULL DEFAULT '',
  outcome      TEXT NOT NULL DEFAULT '' CHECK (outcome IN ('', 'PASS', 'CONDITIONAL_PASS', 'REWORK')),
  outcome_note TEXT NOT NULL DEFAULT '',
  open_scope   TEXT NOT NULL DEFAULT '',
  open_owner   TEXT NOT NULL DEFAULT '',
  close_date   TEXT NOT NULL DEFAULT '',
  closed_at    TEXT,
  UNIQUE (project_id, key),
  CHECK (outcome <> 'CONDITIONAL_PASS' OR (
    length(trim(open_scope)) > 0 AND length(trim(open_owner)) > 0
    AND close_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'))
);
CREATE INDEX gate_project ON gate(project_id);

-- A MET item is closed, never deleted (Part 2 §7-2).
CREATE TABLE evidence_item (
  id           INTEGER PRIMARY KEY,
  gate_id      INTEGER NOT NULL REFERENCES gate(id) ON DELETE CASCADE,
  text         TEXT NOT NULL CHECK (length(trim(text)) > 0),
  hand_state   TEXT NOT NULL DEFAULT '' CHECK (hand_state IN ('', 'MET', 'WAIVED')),
  evidence_url TEXT NOT NULL DEFAULT '' CHECK (evidence_url = '' OR evidence_url GLOB 'https://*'),
  note         TEXT NOT NULL DEFAULT '',
  sort         INTEGER NOT NULL DEFAULT 0,
  closed_at    TEXT,
  CHECK (hand_state <> 'WAIVED' OR length(trim(note)) > 0)
);
CREATE INDEX evidence_item_gate ON evidence_item(gate_id);

CREATE TABLE hand_task (
  id              INTEGER PRIMARY KEY,
  project_id      INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
  key             TEXT NOT NULL CHECK (length(key) BETWEEN 1 AND 24),
  title           TEXT NOT NULL CHECK (length(trim(title)) > 0),
  owner_person_id INTEGER REFERENCES person(id) ON DELETE SET NULL,
  gate_key        TEXT NOT NULL DEFAULT '',
  start           TEXT NOT NULL DEFAULT '',
  finish          TEXT NOT NULL DEFAULT '',
  status          TEXT NOT NULL DEFAULT 'NOT_STARTED'
                  CHECK (status IN ('NOT_STARTED', 'IN_PROGRESS', 'DONE', 'BLOCKED', 'DEFERRED')),
  depends_on      TEXT NOT NULL DEFAULT '',
  note            TEXT NOT NULL DEFAULT '',
  UNIQUE (project_id, key)
);
CREATE INDEX hand_task_project ON hand_task(project_id);

-- Append only (R-C1). The newest row per gate is the confirmed value.
CREATE TABLE confirmation (
  id                   INTEGER PRIMARY KEY,
  gate_id              INTEGER NOT NULL REFERENCES gate(id) ON DELETE CASCADE,
  value                TEXT NOT NULL CHECK (value IN ('ON_TRACK', 'AT_RISK', 'LATE')),
  note                 TEXT NOT NULL DEFAULT '',
  answered_proposal_id INTEGER,
  created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX confirmation_gate ON confirmation(gate_id);
CREATE TRIGGER confirmation_append_only BEFORE UPDATE ON confirmation
BEGIN
  SELECT RAISE(ABORT, 'confirmation rows are append only');
END;

CREATE TABLE holiday (
  id         INTEGER PRIMARY KEY,
  date       TEXT NOT NULL CHECK (date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  label      TEXT NOT NULL DEFAULT '',
  project_id INTEGER REFERENCES project(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX holiday_unique ON holiday(date, ifnull(project_id, 0));

CREATE TABLE setting (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE hand_change_log (
  id         INTEGER PRIMARY KEY,
  table_name TEXT NOT NULL,
  row_id     INTEGER NOT NULL,
  field      TEXT NOT NULL,
  old,
  new,
  at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TRIGGER hand_change_log_no_update BEFORE UPDATE ON hand_change_log
BEGIN
  SELECT RAISE(ABORT, 'hand_change_log is append only');
END;
CREATE TRIGGER hand_change_log_no_delete BEFORE DELETE ON hand_change_log
BEGIN
  SELECT RAISE(ABORT, 'hand_change_log is append only');
END;
