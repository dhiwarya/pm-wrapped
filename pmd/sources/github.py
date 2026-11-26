"""The GitHub client: ``GET`` only, to ``api.github.com`` only (Part 3 §11-3).

There is no method that sends anything else, and the REST API is used rather than
GraphQL because GraphQL needs POST. Every request is logged by the caller's hook.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

API_HOST = "api.github.com"
API = f"https://{API_HOST}"


class GitHubError(RuntimeError):
    pass


class GitHubPermissionError(GitHubError):
    pass


class ReadOnlyGitHub:
    def __init__(self, token: str | None, *, log: Callable[[str, str, str, int | None], None] | None = None,
                 transport: httpx.BaseTransport | None = None, timeout: float = 30.0) -> None:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "pm-dash"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(base_url=API, headers=headers, timeout=timeout, transport=transport, follow_redirects=False)
        self._log = log or (lambda *a: None)

    def close(self) -> None:
        self._client.close()

    def get(self, path: str, params: dict | None = None) -> httpx.Response:
        if not path.startswith("/") or "://" in path or path.startswith("//"):
            raise ValueError("GitHub paths must be relative to the API host")
        request = self._client.build_request("GET", path, params=params)
        if request.method != "GET" or request.url.host != API_HOST:
            raise ValueError("Only GET requests to api.github.com are allowed")
        try:
            response = self._client.send(request)
        except httpx.HTTPError as exc:
            self._log("GET", API_HOST, path, None)
            raise GitHubError(f"GitHub could not be reached: {exc.__class__.__name__}") from None
        self._log("GET", API_HOST, path, response.status_code)
        if response.status_code == 401:
            raise GitHubError("GitHub refused the token (401)")
        if response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0":
            raise GitHubError("GitHub rate limit reached")
        if response.status_code == 403 and "resource not accessible" in response.text.lower():
            raise GitHubPermissionError(f"GitHub denied read access to {path}")
        if response.status_code >= 400:
            raise GitHubError(f"GitHub answered {response.status_code} for {path}")
        return response

    def json(self, path: str, params: dict | None = None):
        return self.get(path, params).json()

    def paged(self, path: str, params: dict | None = None, *, stop: Callable[[dict], bool] | None = None,
              max_pages: int | None = 10) -> list[dict]:
        items: list[dict] = []
        params = {"per_page": 100, **(params or {})}
        page = 1
        while max_pages is None or page <= max_pages:
            response = self.get(path, {**params, "page": page})
            batch = response.json()
            if not isinstance(batch, list):
                raise GitHubError(f"Unexpected list response for {path}")
            for item in batch:
                if stop and stop(item):
                    return items
                items.append(item)
            if "next" not in response.links and len(batch) < params["per_page"]:
                break
            page += 1
        return items


def checks_state(gh: ReadOnlyGitHub, repo: str, sha: str) -> str:
    path = f"/repos/{repo}/commits/{sha}"
    runs = []
    page = 1
    while True:
        response = gh.get(f"{path}/check-runs", {"filter": "latest", "per_page": 100, "page": page})
        payload = response.json()
        batch = payload.get("check_runs") if isinstance(payload, dict) else None
        if not isinstance(batch, list):
            raise GitHubError("GitHub returned incomplete check runs")
        runs.extend(batch)
        if "next" not in response.links and len(runs) >= payload.get("total_count", len(runs)):
            break
        if not batch:
            raise GitHubError("GitHub returned incomplete check runs")
        page += 1
    status = gh.json(f"{path}/status")
    if not isinstance(status, dict) or not isinstance(status.get("statuses"), list):
        raise GitHubError("GitHub returned incomplete commit statuses")
    # The combined state covers all contexts, including those beyond the first page.
    # GitHub also reports "pending" for zero contexts, even when check runs succeeded.
    has_statuses = bool(status["statuses"] or status.get("total_count", 0))
    combined = status.get("state") if has_statuses else None
    conclusions = {r.get("conclusion") for r in runs}
    if conclusions & {"failure", "timed_out", "cancelled", "action_required", "startup_failure", "stale"} or combined in ("failure", "error"):
        return "FAILING"
    if (any(r.get("status") != "completed" for r in runs) or conclusions - {"success", "skipped", "neutral"}
            or (has_statuses and combined != "success")):
        return "PENDING"
    if "success" in conclusions or combined == "success":
        return "PASSING"
    return "NONE"


def fetch_repo(gh: ReadOnlyGitHub, repo: str, dest: Path, *, since: datetime | None, closed_window_days: int = 30) -> dict:
    """Save what the run needs as JSON files under ``dest`` (Part 3 §10-2).

    Open PRs are listed every run. Closed PRs and issues are listed back to
    ``since`` (or the closed window on a baseline run). Reviews, review requests
    and checks are read for open PRs and for PRs that changed.
    """
    dest.mkdir(parents=True, exist_ok=True)
    horizon = since or (datetime.now(UTC) - timedelta(days=closed_window_days))
    horizon_iso = horizon.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    open_prs = gh.paged(f"/repos/{repo}/pulls", {"state": "open", "sort": "updated", "direction": "desc"}, max_pages=None)
    closed_prs = gh.paged(f"/repos/{repo}/pulls", {"state": "closed", "sort": "updated", "direction": "desc"},
                          stop=lambda p: p.get("updated_at", "") < horizon_iso)
    pulls = []
    unread_checks = 0
    for pr in {p["number"]: p for p in open_prs + closed_prs}.values():
        number = pr["number"]
        if pr["state"] == "open":
            # Only the individual PR endpoint reports mergeability. Use its head
            # for checks, even when the branch moved after the list request.
            pr = gh.json(f"/repos/{repo}/pulls/{number}")
        entry = {"pr": pr, "reviews": [], "requests": [], "checks": "NONE"}
        entry["reviews"] = gh.paged(f"/repos/{repo}/pulls/{number}/reviews", max_pages=None)
        if pr.get("state") == "open":
            if pr.get("requested_reviewers"):
                timeline = gh.paged(f"/repos/{repo}/issues/{number}/timeline")
                entry["requests"] = [
                    {"login": (t.get("requested_reviewer") or {}).get("login", ""), "at": t.get("created_at", "")}
                    for t in timeline if t.get("event") == "review_requested"
                ]
            head_sha = (pr.get("head") or {}).get("sha")
            if head_sha:
                try:
                    entry["checks"] = checks_state(gh, repo, head_sha)
                except GitHubPermissionError:
                    unread_checks += 1
                    entry["ci_unread"] = True
                if pr.get("mergeable") is None:
                    # The first GET may start GitHub's background merge calculation.
                    # Only reuse the checks and reviews for the version we just read.
                    latest = gh.json(f"/repos/{repo}/pulls/{number}")
                    if (latest["state"] == "closed" or
                            (latest["head"]["sha"] == head_sha and latest["base"]["sha"] == pr["base"]["sha"]
                             and latest["updated_at"] == pr["updated_at"])):
                        entry["pr"] = latest
        pulls.append(entry)
    issues = [i for i in gh.paged(f"/repos/{repo}/issues", {"state": "all", "since": horizon_iso, "sort": "updated"})
              if "pull_request" not in i]
    (dest / "pulls.json").write_text(json.dumps(pulls), encoding="utf-8")
    (dest / "issues.json").write_text(json.dumps(issues), encoding="utf-8")
    return {"pulls": len(pulls), "issues": len(issues), "unread_checks": unread_checks}


def recent_prs(gh: ReadOnlyGitHub, repo: str, limit: int = 200) -> list[dict]:
    """For onboarding: titles, authors, branches and reviewers of recent PRs."""
    prs = gh.paged(f"/repos/{repo}/pulls", {"state": "all", "sort": "updated", "direction": "desc"}, max_pages=max(1, limit // 100))
    return prs[:limit]


def read_token(path: Path) -> str | None:
    if not path.exists():
        return None
    token = path.read_text(encoding="utf-8").strip()
    return token or None
