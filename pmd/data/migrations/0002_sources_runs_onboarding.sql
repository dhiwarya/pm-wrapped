-- Phase 2 and 2b: sources, run tables, onboarding (Detailed Design Part 2 §6-2, §6-3, §6-7).

-- ---------------------------------------------------------------- hand tables

-- git_url is an https:// URL or an absolute path to a git repository on the host (F1 = A).
CREATE TABLE source (
  id          INTEGER PRIMARY KEY,
  project_id  INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
  key         TEXT NOT NULL CHECK (length(key) BETWEEN 1 AND 40),
  role        TEXT NOT NULL DEFAULT 'DOCS' CHECK (role IN ('DOCS', 'CODE', 'OTHER')),
  git_url     TEXT NOT NULL CHECK (git_url GLOB 'https://*' OR git_url GLOB '/*'),
  github_repo TEXT NOT NULL DEFAULT '',
  branches    TEXT NOT NULL DEFAULT '',
  enabled     INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
  UNIQUE (project_id, key)
);

CREATE TABLE person_alias (
  id        INTEGER PRIMARY KEY,
  person_id INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
  kind      TEXT NOT NULL CHECK (kind IN ('GITHUB', 'GIT', 'DOC', 'WBS')),
  value     TEXT NOT NULL COLLATE NOCASE CHECK (length(trim(value)) > 0),
  UNIQUE (kind, value)
);

CREATE TABLE evidence_link (
  id               INTEGER PRIMARY KEY,
  evidence_item_id INTEGER NOT NULL REFERENCES evidence_item(id) ON DELETE CASCADE,
  kind             TEXT NOT NULL CHECK (kind IN ('TASK', 'TASK_PREFIX', 'UNIT', 'PR', 'PR_SEARCH')),
  value            TEXT NOT NULL CHECK (length(trim(value)) > 0)
);

ALTER TABLE project_setting ADD COLUMN wbs_source_id INTEGER REFERENCES source(id) ON DELETE SET NULL;
ALTER TABLE project_setting ADD COLUMN wbs_path TEXT NOT NULL DEFAULT '';
ALTER TABLE project_setting ADD COLUMN wbs_mapping TEXT NOT NULL DEFAULT '';
ALTER TABLE project_setting ADD COLUMN dashboard_source_id INTEGER REFERENCES source(id) ON DELETE SET NULL;
ALTER TABLE project_setting ADD COLUMN dashboard_json_path TEXT NOT NULL DEFAULT '';
ALTER TABLE project_setting ADD COLUMN branch_unit_pattern TEXT NOT NULL DEFAULT '';
ALTER TABLE project_setting ADD COLUMN task_key_pattern TEXT NOT NULL DEFAULT '';
ALTER TABLE project_setting ADD COLUMN prose_reading INTEGER NOT NULL DEFAULT 1;

-- ---------------------------------------------------------------- run tables

CREATE TABLE run (
  id            INTEGER PRIMARY KEY,
  trigger       TEXT NOT NULL CHECK (trigger IN ('SCHEDULE', 'HAND')),
  scope         TEXT NOT NULL DEFAULT '',
  baseline      INTEGER NOT NULL DEFAULT 0,
  started_at    TEXT NOT NULL,
  ended_at      TEXT,
  result        TEXT NOT NULL DEFAULT 'RUNNING' CHECK (result IN ('RUNNING', 'OK', 'PARTIAL', 'FAILED', 'SKIPPED')),
  error         TEXT NOT NULL DEFAULT '',
  model_seconds REAL NOT NULL DEFAULT 0,
  model_cost    REAL NOT NULL DEFAULT 0
);

CREATE TABLE onboarding (
  id            INTEGER PRIMARY KEY,
  project_id    INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
  started_at    TEXT NOT NULL,
  ended_at      TEXT,
  result        TEXT NOT NULL DEFAULT 'RUNNING' CHECK (result IN ('RUNNING', 'OK', 'FAILED')),
  error         TEXT NOT NULL DEFAULT '',
  progress      TEXT NOT NULL DEFAULT '',
  prose_read    INTEGER NOT NULL DEFAULT 0,
  dropped       INTEGER NOT NULL DEFAULT 0,
  model_seconds REAL NOT NULL DEFAULT 0,
  model_cost    REAL NOT NULL DEFAULT 0
);

CREATE TABLE run_source (
  id            INTEGER PRIMARY KEY,
  run_id        INTEGER REFERENCES run(id) ON DELETE CASCADE,
  onboarding_id INTEGER REFERENCES onboarding(id) ON DELETE CASCADE,
  source_id     INTEGER NOT NULL,
  branch        TEXT NOT NULL DEFAULT '',
  commit_sha    TEXT NOT NULL DEFAULT '',
  commit_date   TEXT NOT NULL DEFAULT '',
  ok            INTEGER NOT NULL,
  error         TEXT NOT NULL DEFAULT '',
  CHECK ((run_id IS NULL) <> (onboarding_id IS NULL))
);

CREATE TABLE run_problem (
  id            INTEGER PRIMARY KEY,
  run_id        INTEGER REFERENCES run(id) ON DELETE CASCADE,
  onboarding_id INTEGER REFERENCES onboarding(id) ON DELETE CASCADE,
  project_id    INTEGER,
  stage         TEXT NOT NULL,
  adapter       TEXT NOT NULL DEFAULT '',
  path          TEXT NOT NULL DEFAULT '',
  line          INTEGER,
  reason        TEXT NOT NULL
);

CREATE TABLE request_log (
  id            INTEGER PRIMARY KEY,
  run_id        INTEGER REFERENCES run(id) ON DELETE CASCADE,
  onboarding_id INTEGER REFERENCES onboarding(id) ON DELETE CASCADE,
  method        TEXT NOT NULL,
  host          TEXT NOT NULL,
  path          TEXT NOT NULL,
  status        INTEGER,
  at            TEXT NOT NULL
);

CREATE TABLE snapshot (
  id           INTEGER PRIMARY KEY,
  run_id       INTEGER NOT NULL REFERENCES run(id),
  created_at   TEXT NOT NULL,
  published_at TEXT
);

CREATE TABLE current_snapshot (
  only        INTEGER PRIMARY KEY CHECK (only = 1),
  snapshot_id INTEGER NOT NULL REFERENCES snapshot(id)
);

-- Every row of a typed table carries a source reference (Part 2 §6-4, R-F1).
CREATE TABLE unit_state (
  id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL, project_id INTEGER NOT NULL,
  unit TEXT NOT NULL, lead_raw TEXT NOT NULL DEFAULT '', lead_person_id INTEGER,
  branch TEXT NOT NULL DEFAULT '', stage TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '',
  blocked TEXT NOT NULL DEFAULT '', carried_over INTEGER NOT NULL DEFAULT 0,
  src_source_id INTEGER, src_commit TEXT, src_path TEXT, src_line_from INTEGER, src_line_to INTEGER,
  src_object TEXT, src_number INTEGER, src_url TEXT,
  CHECK (src_path IS NOT NULL OR src_url IS NOT NULL)
);

CREATE TABLE unit_stage_state (
  id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL, project_id INTEGER NOT NULL,
  unit TEXT NOT NULL, stage TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('NONE', 'ARTIFACT_PRESENT', 'APPROVED')),
  read_by TEXT NOT NULL DEFAULT 'PROGRAM' CHECK (read_by IN ('PROGRAM', 'MODEL')),
  carried_over INTEGER NOT NULL DEFAULT 0,
  src_source_id INTEGER, src_commit TEXT, src_path TEXT, src_line_from INTEGER, src_line_to INTEGER,
  src_object TEXT, src_number INTEGER, src_url TEXT,
  CHECK (src_path IS NOT NULL OR src_url IS NOT NULL)
);

CREATE TABLE task_state (
  id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL, project_id INTEGER NOT NULL,
  key TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', owner_raw TEXT NOT NULL DEFAULT '', owner_person_id INTEGER,
  gate_key TEXT NOT NULL DEFAULT '', start TEXT NOT NULL DEFAULT '', finish TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '', remaining TEXT NOT NULL DEFAULT '', depends_on TEXT NOT NULL DEFAULT '',
  carried_over INTEGER NOT NULL DEFAULT 0,
  src_source_id INTEGER, src_commit TEXT, src_path TEXT, src_line_from INTEGER, src_line_to INTEGER,
  src_object TEXT, src_number INTEGER, src_url TEXT,
  CHECK (src_path IS NOT NULL OR src_url IS NOT NULL)
);

CREATE TABLE pr_state (
  id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL, project_id INTEGER NOT NULL, source_id INTEGER NOT NULL,
  number INTEGER NOT NULL, title TEXT NOT NULL DEFAULT '', author_raw TEXT NOT NULL DEFAULT '', author_person_id INTEGER,
  state TEXT NOT NULL CHECK (state IN ('OPEN', 'MERGED', 'CLOSED')), draft INTEGER NOT NULL DEFAULT 0,
  base TEXT NOT NULL DEFAULT '', head TEXT NOT NULL DEFAULT '', unit TEXT NOT NULL DEFAULT '', task_keys TEXT NOT NULL DEFAULT '',
  review_state TEXT NOT NULL DEFAULT 'NONE' CHECK (review_state IN ('NONE', 'PENDING', 'APPROVED', 'CHANGES_REQUESTED')),
  checks_state TEXT NOT NULL DEFAULT 'NONE' CHECK (checks_state IN ('NONE', 'PENDING', 'PASSING', 'FAILING')),
  created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '', merged_at TEXT NOT NULL DEFAULT '',
  carried_over INTEGER NOT NULL DEFAULT 0,
  src_source_id INTEGER, src_commit TEXT, src_path TEXT, src_line_from INTEGER, src_line_to INTEGER,
  src_object TEXT, src_number INTEGER, src_url TEXT,
  CHECK (src_path IS NOT NULL OR src_url IS NOT NULL)
);

CREATE TABLE review_state (
  id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL, project_id INTEGER NOT NULL, source_id INTEGER NOT NULL,
  pr_number INTEGER NOT NULL, reviewer_raw TEXT NOT NULL, reviewer_person_id INTEGER,
  state TEXT NOT NULL CHECK (state IN ('PENDING', 'APPROVED', 'CHANGES_REQUESTED', 'COMMENTED', 'DISMISSED')),
  requested_at TEXT NOT NULL DEFAULT '', submitted_at TEXT NOT NULL DEFAULT '',
  carried_over INTEGER NOT NULL DEFAULT 0,
  src_source_id INTEGER, src_commit TEXT, src_path TEXT, src_line_from INTEGER, src_line_to INTEGER,
  src_object TEXT, src_number INTEGER, src_url TEXT,
  CHECK (src_path IS NOT NULL OR src_url IS NOT NULL)
);

CREATE TABLE issue_state (
  id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL, project_id INTEGER NOT NULL, source_id INTEGER NOT NULL,
  number INTEGER NOT NULL, title TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT '',
  assignees TEXT NOT NULL DEFAULT '', labels TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '',
  carried_over INTEGER NOT NULL DEFAULT 0,
  src_source_id INTEGER, src_commit TEXT, src_path TEXT, src_line_from INTEGER, src_line_to INTEGER,
  src_object TEXT, src_number INTEGER, src_url TEXT,
  CHECK (src_path IS NOT NULL OR src_url IS NOT NULL)
);

CREATE TABLE fact (
  id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL, project_id INTEGER NOT NULL,
  subject_type TEXT NOT NULL, subject_key TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL,
  value TEXT NOT NULL DEFAULT '{}', fact_date TEXT NOT NULL DEFAULT '',
  read_by TEXT NOT NULL DEFAULT 'PROGRAM' CHECK (read_by IN ('PROGRAM', 'MODEL')), quote TEXT NOT NULL DEFAULT '',
  carried_over INTEGER NOT NULL DEFAULT 0,
  src_source_id INTEGER, src_commit TEXT, src_path TEXT, src_line_from INTEGER, src_line_to INTEGER,
  src_object TEXT, src_number INTEGER, src_url TEXT,
  CHECK (src_path IS NOT NULL OR src_url IS NOT NULL),
  CHECK (read_by = 'PROGRAM' OR length(quote) > 0)
);

CREATE TABLE blocker (
  id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL, project_id INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('REVIEW_WAITING', 'CHANGES_REQUESTED', 'CHECK_FAILING', 'TASK_LATE',
                                     'DEPENDENCY_LATE', 'UNIT_BLOCKED', 'STATED')),
  subject_type TEXT NOT NULL, subject_key TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
  since TEXT NOT NULL, age_days INTEGER NOT NULL DEFAULT 0, person_id INTEGER, person_raw TEXT NOT NULL DEFAULT '',
  src_source_id INTEGER, src_commit TEXT, src_path TEXT, src_line_from INTEGER, src_line_to INTEGER,
  src_object TEXT, src_number INTEGER, src_url TEXT,
  CHECK (src_path IS NOT NULL OR src_url IS NOT NULL OR subject_type = 'HAND_TASK')
);

CREATE TABLE item_state (
  id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL, evidence_item_id INTEGER NOT NULL,
  run_state TEXT NOT NULL CHECK (run_state IN ('NOT_STARTED', 'IN_PROGRESS', 'CANDIDATE')),
  reason TEXT NOT NULL DEFAULT ''
);

CREATE TABLE unmatched_name (
  id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL, project_id INTEGER NOT NULL,
  kind TEXT NOT NULL, value TEXT NOT NULL, seen_in TEXT NOT NULL DEFAULT ''
);

CREATE TABLE onboarding_proposal (
  id INTEGER PRIMARY KEY, onboarding_id INTEGER NOT NULL REFERENCES onboarding(id) ON DELETE CASCADE,
  project_id INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('PROJECT', 'GATE', 'EVIDENCE', 'WBS', 'DASHBOARD_FILE', 'PATTERN', 'PERSON', 'HOLIDAY')),
  identity TEXT NOT NULL, payload TEXT NOT NULL,
  read_by TEXT NOT NULL CHECK (read_by IN ('PROGRAM', 'MODEL')), quote TEXT NOT NULL DEFAULT '',
  date_origin TEXT NOT NULL DEFAULT '' CHECK (date_origin IN ('', 'WBS', 'PLAN_TABLE', 'PROSE')),
  src_source_id INTEGER, src_commit TEXT, src_path TEXT, src_line_from INTEGER, src_line_to INTEGER,
  src_object TEXT, src_number INTEGER, src_url TEXT,
  CHECK (src_path IS NOT NULL OR src_url IS NOT NULL),
  CHECK (read_by = 'PROGRAM' OR length(quote) > 0)
);

-- The user's answer to a proposal (hand data). UNDONE undoes a rejection (Design §6-10).
CREATE TABLE onboarding_decision (
  id             INTEGER PRIMARY KEY,
  proposal_id    INTEGER NOT NULL REFERENCES onboarding_proposal(id) ON DELETE CASCADE,
  decision       TEXT NOT NULL CHECK (decision IN ('ACCEPTED', 'EDITED', 'REJECTED', 'UNDONE')),
  edited_payload TEXT NOT NULL DEFAULT '',
  created_rows   TEXT NOT NULL DEFAULT '',
  at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX unit_state_snap ON unit_state(snapshot_id, project_id);
CREATE INDEX unit_stage_state_snap ON unit_stage_state(snapshot_id, project_id);
CREATE INDEX task_state_snap ON task_state(snapshot_id, project_id);
CREATE INDEX pr_state_snap ON pr_state(snapshot_id, project_id);
CREATE INDEX review_state_snap ON review_state(snapshot_id, project_id);
CREATE INDEX issue_state_snap ON issue_state(snapshot_id, project_id);
CREATE INDEX fact_snap ON fact(snapshot_id, project_id, kind);
CREATE INDEX blocker_snap ON blocker(snapshot_id, project_id);
CREATE INDEX item_state_snap ON item_state(snapshot_id);
CREATE INDEX proposal_onboarding ON onboarding_proposal(onboarding_id);
CREATE INDEX decision_proposal ON onboarding_decision(proposal_id);
