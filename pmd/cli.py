"""``pm-dash web | doctor`` (Detailed Design Part 1 §1, Part 3 §12-1).

``run`` arrives with Phase 2 and ``backup`` with Phase 4.
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


def cmd_web(config: Config) -> int:
    failed = [text for ok, text in doctor_checks(config) if not ok]
    if failed:
        for text in failed:
            print("FAIL  " + text, file=sys.stderr)
        print("pm-dash web refuses to start. Run `pm-dash doctor` for details.", file=sys.stderr)
        return 1
    import uvicorn

    from .web.app import create_app

    uvicorn.run(create_app(config), host=config.listen_host, port=config.port, proxy_headers=False, log_level="info")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pm-dash", description="Project Manager Dashboard")
    parser.add_argument("--version", action="version", version=f"pm-dash {__version__}")
    parser.add_argument("--config", type=Path, help="installation settings file (default: $PMD_CONFIG or ~/.config/pm-dash/config.toml)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("web", help="serve the dashboard on the loopback address")
    sub.add_parser("doctor", help="check the installation")
    args = parser.parse_args(argv)
    config = load(args.config)
    return {"web": cmd_web, "doctor": cmd_doctor}[args.command](config)


if __name__ == "__main__":
    raise SystemExit(main())
