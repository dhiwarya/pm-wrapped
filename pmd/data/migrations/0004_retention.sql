-- A daily digest is self-contained and outlives the snapshot it was made from.
ALTER TABLE digest RENAME TO digest_published;
CREATE TABLE digest (
  id INTEGER PRIMARY KEY,
  snapshot_id INTEGER UNIQUE REFERENCES snapshot(id) ON DELETE SET NULL,
  day TEXT NOT NULL UNIQUE,
  text TEXT NOT NULL,
  cards TEXT NOT NULL,
  changes TEXT NOT NULL DEFAULT '[]',
  projects TEXT NOT NULL DEFAULT '[]'
);
INSERT INTO digest (id, snapshot_id, day, text, cards, changes)
  SELECT d.id, d.snapshot_id, d.day, d.text, d.cards, d.changes FROM digest_published d
  JOIN snapshot s ON s.id = d.snapshot_id WHERE s.published_at IS NOT NULL;
DROP TABLE digest_published;

CREATE INDEX hand_change_log_object ON hand_change_log(table_name, row_id);
