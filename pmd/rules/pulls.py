"""Merge guidance from the PR details, reviews and checks observed on a run."""

from typing import NamedTuple


class Readiness(NamedTuple):
    label: str
    reason: str


def assess(pr, *, stale=False) -> Readiness:
    if stale:
        return Readiness("Unverified", "Refresh GitHub data before making a merge decision.")

    merge = pr["merge_status"]
    review = pr["review_state"]
    checks = pr["checks_state"]
    attention = []
    waiting = []
    missing = []
    if merge == "dirty":
        attention.append("Resolve merge conflicts.")
    elif merge == "behind":
        attention.append("Update the branch with the base branch.")
    elif merge == "blocked":
        attention.append("GitHub reports an unmet merge requirement.")
    elif merge == "has_hooks":
        missing.append("GitHub reports a merge hook that can still block the merge.")
    elif merge == "unstable":
        waiting.append("GitHub reports an unstable merge status.")
    elif merge not in ("clean", "draft"):
        missing.append("GitHub has not confirmed mergeability.")

    if pr["ci_unread"]:
        missing.append("GitHub denied access to CI results.")
    elif checks == "FAILING":
        attention.append("Fix the failing checks.")
    elif checks == "PENDING":
        waiting.append("Checks are pending or inconclusive.")
    elif checks != "PASSING":
        missing.append("No successful CI was reported.")

    if review == "CHANGES_REQUESTED":
        attention.append("Address the requested changes.")
    elif review == "PENDING":
        attention.append("A requested review is outstanding.")
    elif review != "APPROVED":
        attention.append("Get a reviewer’s approval.")
    elif not pr["approved_head"]:
        attention.append("Get approval for the latest commit.")

    reasons = " ".join(attention + waiting + missing)
    if pr["draft"] or merge == "draft":
        return Readiness("Draft", f"The author marked this PR as a draft. {reasons}".rstrip())
    if attention:
        return Readiness("Needs attention", reasons)
    if missing:
        return Readiness("Unverified", reasons)
    if waiting:
        return Readiness("Waiting", reasons)
    return Readiness("Ready to merge", "Latest commit approved, checks passed, and GitHub reports a clean merge.")
