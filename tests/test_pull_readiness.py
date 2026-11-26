import json

import httpx
import pytest

from pmd.adapters import github
from pmd.rules.pulls import assess
from pmd.sources.github import ReadOnlyGitHub, checks_state, fetch_repo


READY = {"draft": 0, "merge_status": "clean", "review_state": "APPROVED", "approved_head": 1, "checks_state": "PASSING", "ci_unread": 0}


@pytest.mark.parametrize(("changes", "label", "reason"), [
    ({}, "Ready to merge", "Latest commit approved"),
    ({"draft": 1}, "Draft", "draft"),
    ({"merge_status": "dirty"}, "Needs attention", "conflicts"),
    ({"merge_status": "behind"}, "Needs attention", "base branch"),
    ({"merge_status": "blocked"}, "Needs attention", "requirement"),
    ({"merge_status": "has_hooks"}, "Unverified", "hook"),
    ({"merge_status": "unknown"}, "Unverified", "mergeability"),
    ({"merge_status": "future-value"}, "Unverified", "mergeability"),
    ({"checks_state": "NONE"}, "Unverified", "No successful CI"),
    ({"ci_unread": 1}, "Unverified", "denied access to CI"),
    ({"checks_state": "PENDING"}, "Waiting", "pending"),
    ({"checks_state": "FAILING"}, "Needs attention", "failing checks"),
    ({"review_state": "CHANGES_REQUESTED"}, "Needs attention", "requested changes"),
    ({"review_state": "PENDING"}, "Needs attention", "requested review"),
    ({"review_state": "NONE"}, "Needs attention", "approval"),
    ({"approved_head": 0}, "Needs attention", "latest commit"),
])
def test_readiness(changes, label, reason):
    result = assess({**READY, **changes})
    assert result.label == label and reason in result.reason
    assert assess({**READY, **changes}, stale=True).label == "Unverified"


@pytest.mark.parametrize(("fields", "reviews", "label"), [
    ({"mergeable": None}, [("reviewer", "APPROVED", "head")], "Unverified"),
    ({"mergeable": False}, [("reviewer", "APPROVED", "head")], "Needs attention"),
    ({"requested_teams": [{"slug": "ops"}]}, [("reviewer", "APPROVED", "head")], "Needs attention"),
    ({}, [("author", "APPROVED", "head")], "Needs attention"),
    ({}, [("reviewer", "APPROVED", "previous")], "Needs attention"),
    ({}, [("Reviewer", "APPROVED", "head"), ("reviewer", "DISMISSED", "head")], "Needs attention"),
    ({}, [("Reviewer", "APPROVED", "head"), ("reviewer", "COMMENTED", "head")], "Ready to merge"),
])
def test_advisory_uses_current_effective_reviews(tmp_path, fields, reviews, label):
    pr = {"number": 1, "state": "open", "user": {"login": "author"}, "head": {"sha": "head"},
          "mergeable": True, "mergeable_state": "clean", **fields}
    entry = {"pr": pr, "checks": "PASSING", "reviews": [
        {"state": state, "user": {"login": login}, "commit_id": commit} for login, state, commit in reviews]}
    (tmp_path / "pulls.json").write_text(json.dumps([entry]))
    result = github.extract(tmp_path, source_id=1, project_id=1, snapshot_id=1, branch_pattern="", task_key_pattern="")
    assert assess(result.rows["pr_state"][0]).label == label


@pytest.mark.parametrize(("conclusions", "combined", "expected"), [
    (["skipped", "neutral"], None, "NONE"),
    (["success", "stale"], None, "FAILING"),
    (["success", None], None, "PENDING"),
    (["success", "new-outcome"], None, "PENDING"),
    (["success"], "failure", "FAILING"),
    (["success"], "pending", "PENDING"),
    ([], "success", "PASSING"),
])
def test_checks_use_combined_status_and_all_check_pages(conclusions, combined, expected):
    def handler(request):
        if request.url.path.endswith("/status"):
            # On large commits the first page can be green even when the combined state is red.
            return httpx.Response(200, json={"state": combined or "pending", "statuses": [{"state": "success"}] if combined else []})
        page = int(request.url.params["page"])
        runs = [{"status": "completed", "conclusion": c} for c in conclusions[page-1:page]]
        return httpx.Response(200, json={"total_count": len(conclusions), "check_runs": runs})

    gh = ReadOnlyGitHub(None, transport=httpx.MockTransport(handler))
    try:
        assert checks_state(gh, "acme/widgets", "current-sha") == expected
    finally:
        gh.close()


@pytest.mark.parametrize("branch_moved", [False, True])
def test_pending_merge_calculation_rechecks_the_same_revision(tmp_path, branch_moved):
    detail_calls = 0
    pr = {"number": 7, "head": {"sha": "initial"}, "base": {"sha": "base"}, "state": "open", "updated_at": "today"}

    def handler(request):
        nonlocal detail_calls
        path = request.url.path
        if path.endswith("/pulls") and request.url.params["state"] == "open":
            result = [pr]
        elif path.endswith("/pulls/7"):
            detail_calls += 1
            head = "newer" if branch_moved and detail_calls == 2 else "initial"
            result = {**pr, "head": {"sha": head}, "mergeable": True if detail_calls == 2 else None,
                      "mergeable_state": "clean" if detail_calls == 2 else "unknown"}
        elif path.endswith("/check-runs"):
            result = {"check_runs": [{"status": "completed", "conclusion": "success"}]}
        elif path.endswith("/status"):
            result = {"state": "pending", "statuses": []}
        else:
            result = []
        return httpx.Response(200, json=result)

    gh = ReadOnlyGitHub(None, transport=httpx.MockTransport(handler))
    try:
        fetch_repo(gh, "acme/widgets", tmp_path, since=None)
    finally:
        gh.close()
    record = json.loads((tmp_path / "pulls.json").read_text())[0]["pr"]
    assert detail_calls == 2 and record["head"]["sha"] == "initial"
    assert record["mergeable_state"] == ("unknown" if branch_moved else "clean")
