"""Resolve evidence links to the subjects cited in a snapshot."""


def pr_subject(source_id, number):
    return "PR", f"{source_id}#{number}"


def pr_matches_link(kind, value, pr):
    if kind == "PR_SEARCH":
        return value.casefold() in pr["title"].casefold()
    repo, _, number = value.rpartition("#")
    url = pr["src_url"] if "src_url" in pr.keys() else ""
    repo = repo.removeprefix("https://github.com/").strip("/")
    return str(pr["number"]) == number and (not repo or f"/{repo}/pull/" in (url or ""))


def evidence_subjects(links, tasks, prs, gate_key=None):
    subjects = set()
    if gate_key:
        subjects.add(("GATE", gate_key))
        subjects.update(("TASK", t["key"]) for t in tasks if gate_key in (t["gate_key"] or "").split(";"))
    for link in links:
        kind, value = link["kind"], link["value"].strip()
        if kind in ("TASK", "UNIT"):
            subjects.add((kind, value))
        elif kind == "TASK_PREFIX":
            subjects.update(("TASK", t["key"]) for t in tasks if t["key"].startswith(value))
        elif kind in ("PR", "PR_SEARCH"):
            for pr in prs:
                if pr_matches_link(kind, value, pr):
                    subjects.add(pr_subject(pr["src_source_id"], pr["number"]))
    for pr in prs:
        if ("UNIT", pr["unit"]) in subjects or any(("TASK", key.strip()) in subjects for key in pr["task_keys"].split(";")):
            subjects.add(pr_subject(pr["src_source_id"], pr["number"]))
    return subjects
