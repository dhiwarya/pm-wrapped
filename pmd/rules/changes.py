"""The same four counts appear on Today and in the digest."""

import json


def highlights(changes):
    groups = {"PRs merged": [], "New notes": [], "Tasks completed": [], "Newly late": []}
    for row in changes:
        new = json.loads(row["new"]) if row["new"] is not None else None
        old = json.loads(row["old"]) if row["old"] is not None else None
        if row["grp"] == "pr_state" and new and new["state"] == "MERGED" and (not old or old["state"] != "MERGED"):
            groups["PRs merged"].append(row)
        elif row["kind"] == "note" and row["change"] == "ADDED":
            groups["New notes"].append(row)
        elif row["grp"] == "task_state" and new and new["status"] == "DONE" and (not old or old["status"] != "DONE"):
            groups["Tasks completed"].append(row)
        elif row["grp"] == "hand" and row["subject_type"] == "hand_task" and row["kind"] == "status" and new == "DONE" and old != "DONE":
            groups["Tasks completed"].append(row)
        elif row["kind"] == "TASK_LATE" and row["change"] == "ADDED":
            groups["Newly late"].append(row)
    return groups
