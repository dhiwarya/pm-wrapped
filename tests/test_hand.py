"""Hand tables (Design §4, Part 2 §6-2)."""

import sqlite3
from datetime import date

import pytest

from pmd.data import hand
from pmd.data.hand import InputError


def project(conn, key="ALPHA", name="Alpha launch"):
    pid = hand.create_project(conn, key, name)
    return pid


def log_rows(conn, table):
    return conn.execute("SELECT * FROM hand_change_log WHERE table_name = ? ORDER BY id", (table,)).fetchall()


def test_colours_follow_a_fixed_order_and_survive_deletes(conn):
    keys = ["AA", "BB", "CC"]
    for k in keys:
        project(conn, k, k)
    colours = [hand.get_project(conn, k)["colour"] for k in keys]
    assert colours == ["VIOLET", "BLUE", "TANGERINE"]
    hand.delete_project(conn, hand.get_project(conn, "BB")["id"], "BB")
    project(conn, "DD", "DD")
    assert hand.get_project(conn, "DD")["colour"] == "PINK"  # not BLUE again
    assert hand.get_project(conn, "CC")["colour"] == "TANGERINE"


@pytest.mark.parametrize("key", ["", "A", "lower-ok", "WAY-TOO-LONG-KEY", "-X", "A B"])
def test_project_key_rules(conn, key):
    if key == "lower-ok":
        project(conn, key, "Lower")  # uppercased on the way in
        assert hand.get_project(conn, "LOWER-OK")
        return
    with pytest.raises(InputError):
        project(conn, key, "Bad")


def test_duplicate_project_key(conn):
    project(conn)
    with pytest.raises(InputError, match="exists already"):
        project(conn)


def test_gate_key_is_never_reused_after_close(conn):
    pid = project(conn)
    gid = hand.create_gate(conn, pid, "G1", "Step 1", "2026-12-04")
    hand.close_gate(conn, gid)
    assert hand.list_gates(conn, pid) == []
    with pytest.raises(InputError, match="never reused"):
        hand.create_gate(conn, pid, "G1", "Again", "2026-12-11")


def test_gate_date_must_be_a_date(conn):
    pid = project(conn)
    with pytest.raises(InputError, match="date"):
        hand.create_gate(conn, pid, "G1", "Step 1", "next friday")


def test_conditional_pass_needs_scope_owner_and_date(conn):
    pid = project(conn)
    gid = hand.create_gate(conn, pid, "G1", "Step 1", "2026-12-04")
    with pytest.raises(InputError):
        hand.record_outcome(conn, gid, "CONDITIONAL_PASS", "", "", "Rani", "2026-12-11")
    hand.record_outcome(conn, gid, "CONDITIONAL_PASS", "ok", "Load test", "Rani", "2026-12-11")
    assert hand.get_gate(conn, pid, gid)["outcome"] == "CONDITIONAL_PASS"
    hand.record_outcome(conn, gid, "PASS")
    row = hand.get_gate(conn, pid, gid)
    assert row["outcome"] == "PASS" and row["open_scope"] == ""


def test_waive_needs_a_note_and_link_must_be_https(conn):
    pid = project(conn)
    gid = hand.create_gate(conn, pid, "G1", "Step 1", "2026-12-04")
    iid = hand.create_item(conn, gid, "Demo recorded")
    with pytest.raises(InputError, match="note"):
        hand.set_item_state(conn, iid, "WAIVED")
    with pytest.raises(InputError, match="https"):
        hand.set_item_state(conn, iid, "MET", url="http://example.com")
    with pytest.raises(InputError):
        hand.set_item_state(conn, iid, "CANDIDATE")  # run state, never set by hand (R-E1)
    hand.set_item_state(conn, iid, "MET", url="https://example.com/demo")
    assert hand.list_items(conn, [gid])[gid][0]["hand_state"] == "MET"


def test_met_item_is_closed_not_deleted(conn):
    pid = project(conn)
    gid = hand.create_gate(conn, pid, "G1", "Step 1", "2026-12-04")
    met = hand.create_item(conn, gid, "Done thing")
    other = hand.create_item(conn, gid, "Other thing")
    hand.set_item_state(conn, met, "MET")
    assert hand.remove_item(conn, met) == "closed"
    assert hand.remove_item(conn, other) == "deleted"
    assert conn.execute("SELECT count(*) FROM evidence_item").fetchone()[0] == 1
    assert hand.list_items(conn, [gid])[gid] == []


def test_move_item(conn):
    pid = project(conn)
    gid = hand.create_gate(conn, pid, "G1", "Step 1", "2026-12-04")
    a, b, c = (hand.create_item(conn, gid, t) for t in "abc")
    hand.move_item(conn, c, "up")
    assert [i["text"] for i in hand.list_items(conn, [gid])[gid]] == ["a", "c", "b"]
    hand.move_item(conn, a, "up")  # already first: no change
    assert [i["text"] for i in hand.list_items(conn, [gid])[gid]] == ["a", "c", "b"]


def test_confirmations_are_append_only(conn):
    pid = project(conn)
    gid = hand.create_gate(conn, pid, "G1", "Step 1", "2026-12-04")
    first = hand.add_confirmation(conn, gid, "ON_TRACK")
    hand.add_confirmation(conn, gid, "AT_RISK", "review waiting")
    assert hand.latest_confirmations(conn, [gid])[gid]["value"] == "AT_RISK"
    assert len(hand.confirmation_history(conn, gid)) == 2
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE confirmation SET value = 'LATE' WHERE id = ?", (first,))
    with pytest.raises(InputError):
        hand.add_confirmation(conn, gid, "UNKNOWN")


def test_every_hand_change_is_logged(conn):
    pid = project(conn)
    hand.update_project(conn, pid, "Alpha relaunch", "", "")
    rows = log_rows(conn, "project")
    assert rows[0]["field"] == "*" and rows[0]["new"] is not None
    assert [(r["field"], r["old"], r["new"]) for r in rows[1:]] == [("name", "Alpha launch", "Alpha relaunch")]
    hand.delete_project(conn, pid, "ALPHA")
    assert log_rows(conn, "project")[-1]["new"] is None
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM hand_change_log")


def test_tasks(conn):
    pid = project(conn)
    hand.create_gate(conn, pid, "G1", "Step 1", "2026-12-04")
    tid = hand.create_task(conn, pid, "1.023", title="Library", owner="Rani", gate_key="G1",
                           start="2026-11-16", finish="2026-11-23", status="IN_PROGRESS", depends_on="1.020, 1.021")
    row = hand.list_tasks(conn, pid)[0]
    assert row["owner"] == "Rani" and row["depends_on"] == "1.020; 1.021"
    with pytest.raises(InputError, match="no open gate"):
        hand.update_task(conn, pid, tid, title="Library", gate_key="G9")
    with pytest.raises(InputError, match="before start"):
        hand.update_task(conn, pid, tid, title="Library", start="2026-11-20", finish="2026-11-19")
    with pytest.raises(InputError, match="used already"):
        hand.create_task(conn, pid, "1.023", title="Again")
    hand.create_task(conn, pid, "1.024", title="Other", owner="rani")  # same person, any case
    assert len(hand.list_people(conn)) == 1


def test_holidays(conn):
    pid = project(conn)
    hand.add_holiday(conn, "2026-12-25", "Christmas Day")
    hand.add_holiday(conn, "2026-11-26", "Offsite", pid)
    assert hand.holiday_dates(conn, pid) == {date(2026, 12, 25), date(2026, 11, 26)}
    with pytest.raises(InputError, match="already"):
        hand.add_holiday(conn, "2026-12-25", "again")
    hand.add_holiday(conn, "2026-12-25", "project copy", pid)  # project and global lists are separate


def test_delete_project_needs_its_key(conn):
    pid = project(conn)
    with pytest.raises(InputError, match="Type the project key"):
        hand.delete_project(conn, pid, "WRONG")
    hand.delete_project(conn, pid, "alpha")
    assert hand.get_project(conn, "ALPHA") is None
