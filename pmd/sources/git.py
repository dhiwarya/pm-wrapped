"""The git client: ``clone --mirror`` and ``fetch`` only, plus reads (Part 3 §10-2, §11-3).

Mirrors have no work tree, so nothing can be committed. A remote is an https URL
or an absolute path to a repository on the host (F1 = A): a path is mirrored like
any remote, so the user's working copy is never read or changed.
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import tarfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

# Never exported, at any depth: agent instruction files of the tracked project must
# stay data (R-M1), and secret-looking files must not reach the model (Part 3 §11-4).
EXCLUDED_DIRS = frozenset({".claude", ".kiro", ".git", ".github", "node_modules", ".venv", "__pycache__"})
EXCLUDED_NAMES = frozenset({"CLAUDE.md", "AGENTS.md", "CLAUDE.local.md", ".mcp.json"})
SECRET_NAME = re.compile(r"(^\.env($|\.)|\.pem$|\.key$|\.p12$|\.pfx$|^id_(rsa|ed25519|ecdsa)|credentials?\.json$|secrets?\.(json|ya?ml|toml)$)", re.I)
DEFAULT_SIZE_LIMIT = 512 * 1024


class GitError(RuntimeError):
    pass


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false", "LC_ALL": "C"})
    return env


def run_git(args: list[str], *, timeout: int = 300, token_file: Path | None = None) -> bytes:
    cmd = ["git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.file.allow=always"]
    if token_file is not None:
        # The token is read by the helper at request time: it is never part of a URL,
        # a command line or a log line (Part 3 §11-2).
        helper = f"!f() {{ test \"$1\" = get && echo username=x-access-token && echo password=$(cat '{token_file}'); }}; f"
        cmd += ["-c", "credential.helper=", "-c", f"credential.helper={helper}"]
    try:
        done = subprocess.run(cmd + args, capture_output=True, timeout=timeout, env=_env(), check=False)
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0]} timed out after {timeout} s") from None
    if done.returncode != 0:
        message = done.stderr.decode("utf-8", "replace").strip().splitlines()
        raise GitError(f"git {args[0]} failed: {message[-1] if message else 'no message'}")
    return done.stdout


def is_git_repository(path: str) -> bool:
    p = Path(path)
    if not p.is_absolute() or not p.exists():
        return False
    try:
        run_git(["-C", str(p), "rev-parse", "--git-dir"], timeout=30)
        return True
    except GitError:
        return False


def github_repo_of(url: str) -> str:
    """``owner/repo`` of a github.com URL, else empty."""
    m = re.fullmatch(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?", url.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else ""


def ensure_mirror(url: str, mirror: Path, *, token_file: Path | None = None, timeout: int = 300) -> None:
    """``clone --mirror`` the first time, ``fetch --prune`` after."""
    use_token = token_file if (token_file and token_file.exists() and url.startswith("https://github.com/")) else None
    if url.startswith("/") and not is_git_repository(url):
        raise GitError(f"{url} is not a git repository. Make it a git repository first")
    if (mirror / "HEAD").exists():
        run_git(["--git-dir", str(mirror), "fetch", "--prune", "--quiet", "origin"], timeout=timeout, token_file=use_token)
        return
    mirror.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_git(["clone", "--mirror", "--quiet", url, str(mirror)], timeout=timeout, token_file=use_token)


def default_branch(mirror: Path) -> str:
    out = run_git(["--git-dir", str(mirror), "symbolic-ref", "--short", "HEAD"], timeout=30).decode().strip()
    return out.removeprefix("heads/")


def head(mirror: Path, branch: str) -> tuple[str, str]:
    """(commit, ISO commit date) of a branch."""
    out = run_git(["--git-dir", str(mirror), "log", "-1", "--format=%H%x00%cI", f"refs/heads/{branch}", "--"], timeout=30)
    sha, _, when = out.decode().strip().partition("\x00")
    if not sha:
        raise GitError(f"branch {branch} has no commits")
    return sha, when


def branch_exists(mirror: Path, branch: str) -> bool:
    try:
        run_git(["--git-dir", str(mirror), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], timeout=30)
        return True
    except GitError:
        return False


def branches(mirror: Path) -> list[str]:
    out = run_git(["--git-dir", str(mirror), "for-each-ref", "--format=%(refname:short)", "refs/heads/"], timeout=30)
    return [b.removeprefix("heads/") for b in out.decode().split()]


def last_change(mirror: Path, commit: str, path: str) -> str:
    """ISO date of the last commit that touched ``path`` up to ``commit``."""
    out = run_git(["--git-dir", str(mirror), "log", "-1", "--format=%cI", commit, "--", path], timeout=60)
    return out.decode().strip()


def tree_paths(mirror: Path, commit: str, paths: list[str]) -> list[str]:
    """The given paths that exist in the commit."""
    if not paths:
        return []
    out = run_git(["--git-dir", str(mirror), "ls-tree", "--name-only", commit, "--", *paths], timeout=60)
    return [p for p in out.decode().splitlines() if p]


@dataclass
class Export:
    root: Path
    files: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)


def _excluded(path: PurePosixPath) -> str:
    if any(part in EXCLUDED_DIRS for part in path.parts[:-1]):
        return "excluded folder"
    if path.name in EXCLUDED_NAMES:
        return "agent instruction file"
    if SECRET_NAME.search(path.name):
        return "looks like a secret"
    return ""


def export(mirror: Path, commit: str, dest: Path, paths: list[str] | None = None, *,
           size_limit: int = DEFAULT_SIZE_LIMIT, timeout: int = 300) -> Export:
    """Write the files of ``commit`` (or only ``paths``) to ``dest``, minus exclusions.

    Binary files and files over ``size_limit`` are skipped and listed.
    """
    result = Export(root=dest)
    dest.mkdir(parents=True, exist_ok=True)
    wanted = tree_paths(mirror, commit, paths) if paths is not None else None
    if wanted is not None and not wanted:
        return result
    data = run_git(["--git-dir", str(mirror), "archive", "--format=tar", commit, *(["--", *wanted] if wanted else [])], timeout=timeout)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
        for member in tar:
            if not member.isfile():
                continue
            rel = PurePosixPath(member.name)
            if rel.is_absolute() or ".." in rel.parts:
                result.skipped.append((member.name, "unsafe path"))
                continue
            reason = _excluded(rel)
            if not reason and member.size > size_limit:
                reason = "too large"
            body = b""
            if not reason:
                handle = tar.extractfile(member)
                body = handle.read() if handle else b""
                if b"\x00" in body[:8192]:
                    reason = "binary"
            if reason:
                result.skipped.append((member.name, reason))
                continue
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            result.files.append(str(rel))
    return result
