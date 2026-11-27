CREATE TABLE project_person (
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    person_id INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
    PRIMARY KEY (project_id, person_id)
);

ALTER TABLE task_state ADD COLUMN wbs_gates TEXT;
