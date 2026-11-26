"""The ``github`` adapter: saved API answers to PR, review and issue rows (Part 2 §8-2)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import Result, object_ref

REVIEW_STATES = {"APPROVED": "APPROVED", "CHANGES_REQUESTED": "CHANGES_REQUESTED", "COMMENTED": "COMMENTED", "DISMISSED": "DISMISSED"}


def branch_pattern_regex(pattern: str) -> list[re.Pattern]:
    """``unit/<unit>; <unit>/<topic>`` to regular expressions with a ``unit`` group."""
    out = []
    for part in (p.strip() for p in pattern.split(";")):
        if not part:
            continue
        rx = re.escape(part).replace(re.escape("<unit>"), r"(?P<unit>[A-Za-z0-9._-]+)").replace(re.escape("<topic>"), r".+")
        out.append(re.compile(rf"^{rx}$"))
    return out


def unit_of_branch(branch: str, patterns: list[re.Pattern]) -> str:
    for rx in patterns:
        m = rx.match(branch)
        if m and m.groupdict().get("unit"):
            return m.group("unit")
    return ""


def extract(folder: Path, *, source_id: int, project_id: int, snapshot_id: int, branch_pattern: str, task_key_pattern: str) -> Result:
    result = Result()
    pulls_file, issues_file = folder / "pulls.json", folder / "issues.json"
    if not pulls_file.is_file():
        return result
    patterns = branch_pattern_regex(branch_pattern)
    try:
        task_rx = re.compile(task_key_pattern) if task_key_pattern else None
    except re.error:
        task_rx = None
        result.problem("github", "", "the task-key pattern is not a valid regular expression")
    base = {"snapshot_id": snapshot_id, "project_id": project_id, "source_id": source_id}
    for entry in json.loads(pulls_file.read_text(encoding="utf-8")):
        pr = entry["pr"]
        number = pr["number"]
        url = pr.get("html_url", "")
        state = "OPEN" if pr.get("state") == "open" else ("MERGED" if pr.get("merged_at") else "CLOSED")
        head = (pr.get("head") or {}).get("ref", "")
        head_sha = (pr.get("head") or {}).get("sha") or ""
        merge_status = pr.get("mergeable_state") or "unknown"
        if pr.get("mergeable") is False:
            merge_status = "dirty"
        elif merge_status == "clean" and pr.get("mergeable") is not True:
            merge_status = "unknown"
        text = f"{pr.get('title', '')}\n{pr.get('body') or ''}"
        keys = sorted(set(task_rx.findall(text))) if task_rx else []
        keys = [k if isinstance(k, str) else k[0] for k in keys]
        unit = unit_of_branch(head, patterns)
        # The newest review of each reviewer counts; requested reviewers with no review are PENDING.
        newest: dict[str, dict] = {}
        for review in sorted(entry.get("reviews", []), key=lambda r: r.get("submitted_at") or ""):
            login = ((review.get("user") or {}).get("login") or "").lower()
            if login and review.get("state") in REVIEW_STATES:
                if review["state"] == "COMMENTED" and login in newest:
                    continue
                newest[login] = review
        requested = {u["login"].lower(): "" for u in pr.get("requested_reviewers") or [] if u.get("login")}
        for req in entry.get("requests", []):
            login = (req.get("login") or "").lower()
            if login in requested:
                requested[login] = req.get("at", "")
        overall = "NONE"
        if any(r["state"] == "CHANGES_REQUESTED" for r in newest.values()):
            overall = "CHANGES_REQUESTED"
        elif requested or pr.get("requested_teams"):
            overall = "PENDING"
        elif any(r["state"] == "APPROVED" for r in newest.values()):
            overall = "APPROVED"
        author = (pr.get("user") or {}).get("login") or ""
        approved_head = bool(head_sha and any(r["state"] == "APPROVED" and r.get("commit_id") == head_sha and login != author.lower()
                                             for login, r in newest.items()))
        result.add("pr_state", {**base, "number": number, "title": pr.get("title", "")[:300], "author_raw": author,
                                "head_sha": head_sha, "approved_head": int(approved_head), "merge_status": merge_status,
                                "ci_unread": int(bool(entry.get("ci_unread"))),
                                "state": state, "draft": int(bool(pr.get("draft"))), "base": (pr.get("base") or {}).get("ref", ""),
                                "head": head, "unit": unit, "task_keys": ";".join(keys), "review_state": overall,
                                "checks_state": entry.get("checks", "NONE") if state == "OPEN" else "NONE",
                                "created_at": pr.get("created_at", "") or "", "updated_at": pr.get("updated_at", "") or "",
                                "merged_at": pr.get("merged_at") or "", **object_ref(source_id, "PR", number, url)})
        for login, review in newest.items():
            if login in requested:
                continue  # re-requested after the review: pending again
            result.add("review_state", {**base, "pr_number": number, "reviewer_raw": login, "state": review["state"],
                                        "submitted_at": review.get("submitted_at") or "",
                                        **object_ref(source_id, "REVIEW", number, review.get("html_url") or url)})
        for login, at in requested.items():
            if login:
                result.add("review_state", {**base, "pr_number": number, "reviewer_raw": login, "state": "PENDING",
                                            "requested_at": at, **object_ref(source_id, "PR", number, url)})
    if issues_file.is_file():
        for issue in json.loads(issues_file.read_text(encoding="utf-8")):
            result.add("issue_state", {**base, "number": issue["number"], "title": issue.get("title", "")[:300], "state": issue.get("state", ""),
                                       "assignees": ";".join(a.get("login", "") for a in issue.get("assignees") or []),
                                       "labels": ";".join(lbl.get("name", "") for lbl in issue.get("labels") or []),
                                       "updated_at": issue.get("updated_at", "") or "",
                                       **object_ref(source_id, "ISSUE", issue["number"], issue.get("html_url", ""))})
    return result
