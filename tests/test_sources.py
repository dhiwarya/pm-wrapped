"""The git and GitHub clients (Part 3 §10-2, §11-3; property test PR-11)."""

import httpx
import pytest

from pmd.sources import git
from pmd.sources.github import API_HOST, GitHubError, ReadOnlyGitHub

from .fixtures import make_aidlc_repo


def test_mirror_and_export_leave_out_agent_files_secrets_and_binaries(tmp_path):
    repo = make_aidlc_repo(tmp_path / "repo")
    mirror = tmp_path / "mirrors" / "1.git"
    git.ensure_mirror(str(repo), mirror)
    assert git.default_branch(mirror) == "main"
    assert {"unit/alpha", "unit/beta", "main"} <= set(git.branches(mirror))
    sha, when = git.head(mirror, "main")
    exported = git.export(mirror, sha, tmp_path / "out")
    files = set(exported.files)
    assert "aidlc-docs/aidlc-state.md" in files and "plan/wbs.csv" in files
    skipped = dict(exported.skipped)
    assert skipped["CLAUDE.md"] == "agent instruction file"
    assert skipped[".claude/settings.json"] == "excluded folder"
    assert skipped[".env"] == "looks like a secret"
    assert skipped["assets/logo.png"] == "binary"
    assert not (tmp_path / "out" / "CLAUDE.md").exists()
    only = git.export(mirror, sha, tmp_path / "out2", ["aidlc-docs", "missing/path"])
    assert all(f.startswith("aidlc-docs/") for f in only.files)
    git.ensure_mirror(str(repo), mirror)  # second time: fetch


def test_a_folder_that_is_not_a_git_repository_is_refused(tmp_path):
    (tmp_path / "plain").mkdir()
    with pytest.raises(git.GitError, match="Make it a git repository first"):
        git.ensure_mirror(str(tmp_path / "plain"), tmp_path / "m.git")


def test_github_repo_of():
    assert git.github_repo_of("https://github.com/acme/widgets.git") == "acme/widgets"
    assert git.github_repo_of("https://github.com/acme/widgets") == "acme/widgets"
    assert git.github_repo_of("https://gitlab.com/acme/widgets") == ""


def test_github_client_sends_get_only_to_the_api_host():
    seen = []

    def handler(request: httpx.Request):
        seen.append((request.method, request.url.host, request.headers.get("authorization")))
        return httpx.Response(200, json=[])

    logged = []
    gh = ReadOnlyGitHub("t0ken", log=lambda *a: logged.append(a), transport=httpx.MockTransport(handler))
    assert gh.paged("/repos/acme/widgets/pulls") == []
    assert seen == [("GET", API_HOST, "Bearer t0ken")]
    assert logged[0][:3] == ("GET", API_HOST, "/repos/acme/widgets/pulls")
    for bad in ("https://evil.example/x", "//evil.example/x", "repos/x"):
        with pytest.raises(ValueError):
            gh.get(bad)
    assert not hasattr(gh, "post") and not hasattr(gh, "request")
    assert "t0ken" not in repr(logged)


def test_github_errors_are_reported_not_raised_raw():
    gh = ReadOnlyGitHub("bad", transport=httpx.MockTransport(lambda r: httpx.Response(401, json={})))
    with pytest.raises(GitHubError, match="refused the token"):
        gh.get("/repos/acme/widgets/pulls")
