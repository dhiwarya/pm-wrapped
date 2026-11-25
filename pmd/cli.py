"""``pm-dash web | run | onboard | doctor`` (Detailed Design Part 1 §1, Part 3 §10, §12-1).

``backup`` arrives with Phase 4.
"""

from __future__ import annotations

import argparse
import stat
import sys
from pathlib import Path

from . import __version__
from .config import LOOPBACK, Config, load


def inside_git_work_tree(path: Path) -> Path | None:
    """The first parent holding a ``.git`` entry, if any (L-1, Part 1 §5-1)."""
    for parent in [path.resolve(), *path.resolve().parents]:
        if (parent / ".git").exists():
            return parent
    return None


def doctor_checks(config: Config) -> list[tuple[bool, str]]:
    results: list[tuple[bool, str]] = []
    repo = inside_git_work_tree(config.data_dir)
    results.append(
        (repo is None, f"data directory {config.data_dir} is outside any git work tree"
         + ("" if repo is None else f" (it is inside {repo})"))
    )
    results.append(
        (config.listen_host in LOOPBACK, f"listen address {config.listen_host} is loopback only (Part 3 §11-6)")
    )
    secrets = config.data_dir / "secrets"
    if secrets.exists():
        loose = [p.name for p in [secrets, *secrets.iterdir()] if stat.S_IMODE(p.stat().st_mode) & 0o077]
        results.append((not loose, "secrets are readable by their owner only" + (f" (fix: {', '.join(loose)})" if loose else "")))
    import shutil
    import subprocess

    claude = shutil.which("claude")
    if claude:
        try:
            out = subprocess.run([claude, "--help"], capture_output=True, text=True, timeout=30).stdout
            missing = [o for o in ("--restricted", "--tools", "--json-schema", "--strict-mcp-config", "--no-session-persistence", "--max-budget-usd") if o not in out]
            results.append((not missing, "Claude Code CLI has the options the model step needs" + (f" (missing: {', '.join(missing)})" if missing else "")))
        except (OSError, subprocess.TimeoutExpired) as exc:
            results.append((False, f"Claude Code CLI did not answer: {exc}"))
    else:
        results.append((True, "Claude Code CLI not found: onboarding and prose reading will be skipped, the rest works"))
    if config.codex_fallback:
        codex = shutil.which("codex")
        results.append((True, "Codex CLI available for fallback" if codex else "Codex fallback unavailable: Codex CLI is not on PATH"))
    token = config.data_dir / "secrets" / "github-token"
    results.append((True, "GitHub token present" if token.exists() else "no GitHub token: PRs, reviews and issues will not be read"))
    try:
        from .data import db

        conn = db.open_store(config.db_path)
        version = conn.execute("SELECT max(version) FROM schema_version").fetchone()[0]
        conn.close()
        results.append((True, f"database {config.db_path} opens, schema version {version}"))
    except Exception as exc:  # noqa: BLE001 - report any failure
        results.append((False, f"database {config.db_path} failed to open: {exc}"))
    return results


def cmd_doctor(config: Config) -> int:
    results = doctor_checks(config)
    for ok, text in results:
        print(("ok    " if ok else "FAIL  ") + text)
    return 0 if all(ok for ok, _ in results) else 1


def cmd_web(config: Config, config_path: Path | None = None) -> int:
    failed = [text for ok, text in doctor_checks(config) if not ok]
    if failed:
        for text in failed:
            print("FAIL  " + text, file=sys.stderr)
        print("pm-dash web refuses to start. Run `pm-dash doctor` for details.", file=sys.stderr)
        return 1
    import uvicorn

    from .web.app import create_app

    uvicorn.run(create_app(config, config_path=config_path), host=config.listen_host, port=config.port, proxy_headers=False, log_level="info")
    return 0


def cmd_run(config: Config, args) -> int:
    from .run.controller import run

    outcome = run(config, trigger=args.trigger.upper(), project_key=args.project, baseline=args.baseline)
    print(f"{outcome.result}: run {outcome.run_id or '-'} {outcome.message}".rstrip())
    for p in outcome.problems:
        print("  problem: " + p)
    return 0 if outcome.result in ("OK", "PARTIAL", "SKIPPED") else 1


def cmd_onboard(config: Config, args) -> int:
    from .onboard.agent import onboard

    outcome = onboard(config, args.project)
    print(f"{outcome.result}: onboarding {outcome.onboarding_id or '-'}, {outcome.proposals} proposals, "
          f"{outcome.dropped} dropped by the quote check. {outcome.message}".rstrip())
    return 0 if outcome.result == "OK" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pm-dash", description="Project Manager Dashboard")
    parser.add_argument("--version", action="version", version=f"pm-dash {__version__}")
    parser.add_argument("--config", type=Path, help="installation settings file (default: $PMD_CONFIG or ~/.config/pm-dash/config.toml)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("web", help="serve the dashboard on the loopback address")
    sub.add_parser("doctor", help="check the installation")
    p_run = sub.add_parser("run", help="read the sources and publish a snapshot")
    p_run.add_argument("--trigger", choices=["schedule", "hand"], default="hand")
    p_run.add_argument("--project", help="one project key (default: all active projects)")
    p_run.add_argument("--baseline", action="store_true", help="read everything, not only changes")
    p_onb = sub.add_parser("onboard", help="read a project's sources and propose its setup")
    p_onb.add_argument("--project", required=True)
    args = parser.parse_args(argv)
    config = load(args.config)
    if args.command in ("run", "onboard"):
        return {"run": cmd_run, "onboard": cmd_onboard}[args.command](config, args)
    if args.command == "web":
        import os

        return cmd_web(config, args.config or (Path(os.environ["PMD_CONFIG"]) if os.environ.get("PMD_CONFIG") else None))
    return cmd_doctor(config)


if __name__ == "__main__":
    raise SystemExit(main())
