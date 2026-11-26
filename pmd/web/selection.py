"""Apply explicitly selected review rows together, or leave the whole selection open."""

import json

from ..data import db, hand, views
from ..data.hand import InputError


def selected(form):
    try:
        items = json.loads(form.get("items", ""))
    except (ValueError, TypeError):
        items = None
    if not isinstance(items, list) or not items:
        raise InputError("Select at least one row.")
    if not all(isinstance(row, dict) and all(isinstance(v, str) for v in row.values()) for row in items):
        raise InputError("Invalid selection.")
    return items


def identifier(value):
    try:
        number = int(value)
        if not 0 < number < 2**63:
            raise ValueError
        return number
    except (TypeError, ValueError):
        raise InputError("Invalid selection.") from None


def decide(conn, project_id, onboarding_id, form):
    action = form.get("action")
    if action not in ("accept", "reject"):
        raise InputError("Choose Accept or Reject.")
    items = selected(form)
    with db.transaction(conn):
        available = {p["id"]: p for p in views.proposals_with_decisions(conn, onboarding_id)
                     if p["project_id"] == project_id and p["open"]}
        choices = []
        for item in items:
            proposal = available.pop(identifier(item.get("id")), None)
            if not proposal:
                raise InputError("A selected proposal is no longer open in this review. Check the selection and try again.")
            edits = {k[5:]: v for k, v in item.items() if k.startswith("edit_")}
            choices.append((proposal, edits))
        # A selected gate may supply the gate key for selected evidence.
        for proposal, edits in sorted(choices, key=lambda choice: choice[0]["kind"] == "EVIDENCE"):
            verb = "edit" if action == "accept" and edits else action
            try:
                hand.decide_proposal(conn, project_id, proposal["id"], verb, edits)
            except InputError as exc:
                raise InputError(f"{proposal['identity']}: {exc}") from exc
    return f"{'Accepted' if action == 'accept' else 'Rejected'} {len(items)} selected."


def attach(conn, form):
    items = selected(form)
    with db.transaction(conn):
        for row in items:
            hand.attach_name(conn, row.get("value"), row.get("kind"), row.get("person"))
    return f"Attached {len(items)} selected."


def save_health(conn, project_id, gate_id, form, *, require_current=False):
    with db.transaction(conn):
        gate = hand.get_gate(conn, project_id, gate_id)
        if gate["outcome"] or gate["closed_at"]:
            raise InputError("This gate is closed or already has an outcome.")
        proposal_id = None
        value = form.get("value")
        if form.get("proposal_id") or require_current:
            proposal_id = identifier(form.get("proposal_id"))
            proposal = views.health_proposal(conn, proposal_id, gate_id)
            snap = views.current_snapshot(conn)
            latest = conn.execute("SELECT id, value FROM proposal WHERE gate_id = ? AND snapshot_id = ?",
                                  (gate_id, snap["id"] if snap else None)).fetchone()
            if not proposal or not latest or proposal["value"] != latest["value"] or (require_current and latest["id"] != proposal_id):
                raise InputError("The gate proposal has changed. Review the current proposal on Today.")
            if require_current or form.get("confirm") == "1":
                value = proposal["value"]
                previous = hand.latest_confirmations(conn, [gate_id]).get(gate_id)
                if previous and previous["value"] == value:
                    return
        hand.add_confirmation(conn, gate_id, value, form.get("note"), proposal_id=proposal_id)


def confirm(conn, form):
    items = selected(form)
    with db.transaction(conn):
        for row in items:
            project = hand.get_project(conn, row.get("project", ""))
            if project is None:
                raise InputError("A selected project no longer exists.")
            save_health(conn, project["id"], identifier(row.get("id")), row, require_current=True)
    return f"Confirmed {len(items)} selected."


def mark_met(conn, project_id, gate_id, form):
    items = selected(form)
    with db.transaction(conn):
        gate = hand.get_gate(conn, project_id, gate_id)
        if gate["closed_at"] or gate["outcome"]:
            raise InputError("This gate is closed or already has an outcome.")
        for row in items:
            item = hand.get_item(conn, project_id, identifier(row.get("id")))
            if item["gate_id"] != gate_id or item["closed_at"] or item["hand_state"]:
                raise InputError("A selected evidence item has changed. Check the selection and try again.")
            hand.set_item_state(conn, item["id"], "MET", row.get("note"), row.get("evidence_url"))
    return f"Marked {len(items)} selected as met."
