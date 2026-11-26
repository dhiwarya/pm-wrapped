"""Calls the Claude Code CLI headless, with read tools only (Part 3 §10-5, §11-4).

``--restricted`` removes every tool that runs commands or code and WebFetch,
ignores user, project and local settings files, and confines the file tools to the
working directory. ``--tools`` then names the whole tool set. Checked against
Claude Code 2.1.321 on 2026-11-25.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CliResult:
    data: dict | None
    cost: float = 0.0
    seconds: float = 0.0
    error: str = ""
    limited: bool = False
    provider: str = "claude"


LIMIT = re.compile(
    r"(?:you(?:'|’| ha)ve hit (?:your|the) (?:usage |session |weekly |daily )?limit|"
    r"usage[_ ]limit|rate[_ -]limit|quota exceeded|exceeded (?:your |the )?(?:quota|budget)|"
    r"budget (?:limit |has been )?(?:reached|exceeded)|error_max_budget_usd|\b429\b)",
    re.IGNORECASE,
)


def build_command(prompt: str, schema: dict, *, tools: str, model: str | None, budget_usd: float,
                  effort: str | None = None, binary: str = "claude") -> list[str]:
    cmd = [
        binary, "-p", prompt,
        "--restricted",
        "--tools", tools,
        "--strict-mcp-config",
        "--no-session-persistence",
        "--output-format", "json",
        "--json-schema", json.dumps(schema, separators=(",", ":")),
        "--max-budget-usd", f"{budget_usd:.2f}",
    ]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]
    return cmd


def _env(token_file: Path | None) -> dict[str, str]:
    # Hand the CLI nothing but what it needs: no GitHub token, no PMD settings.
    keep = ("HOME", "PATH", "LANG", "LC_ALL", "TMPDIR", "USER", "SHELL", "TERM")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR"):
        if k in os.environ:
            env[k] = os.environ[k]
    if token_file is not None and token_file.exists():
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token_file.read_text(encoding="utf-8").strip()
    return env


def call(prompt: str, schema: dict, *, cwd: Path, tools: str = "Read,Glob,Grep", model: str | None = None,
         effort: str | None = None, budget_usd: float = 1.0, timeout: int = 600, token_file: Path | None = None,
         binary: str = "claude") -> CliResult:
    if shutil.which(binary) is None:
        return CliResult(None, error=f"{binary} is not installed or not on PATH")
    started = time.monotonic()
    try:
        done = subprocess.run(build_command(prompt, schema, tools=tools, model=model, effort=effort, budget_usd=budget_usd, binary=binary),
                              cwd=cwd, capture_output=True, text=True, timeout=timeout, env=_env(token_file), check=False)
    except subprocess.TimeoutExpired:
        return CliResult(None, seconds=time.monotonic() - started, error=f"the model call timed out after {timeout} s")
    except OSError:
        return CliResult(None, error="could not start the Claude CLI")
    seconds = time.monotonic() - started
    try:
        envelope = json.loads(done.stdout)
    except json.JSONDecodeError:
        limited = bool(LIMIT.search(done.stderr) or LIMIT.search(done.stdout))
        return CliResult(None, seconds=seconds, error="Claude usage limit reached" if limited else
                         f"the Claude CLI gave no JSON (exit {done.returncode})", limited=limited)
    if not isinstance(envelope, dict):
        return CliResult(None, seconds=seconds, error="the Claude CLI returned an invalid envelope")
    try:
        cost = float(envelope.get("total_cost_usd") or 0)
    except (ValueError, TypeError):
        cost = 0.0
    if done.returncode or envelope.get("is_error") or envelope.get("subtype") != "success" or not envelope.get("structured_output"):
        details = " ".join(str(envelope.get(k, "")) for k in ("subtype", "result", "errors", "error", "stop_reason"))
        limited = bool(LIMIT.search(details) or LIMIT.search(done.stderr))
        if limited or done.returncode or envelope.get("is_error") or envelope.get("subtype") != "success":
            return CliResult(None, cost=cost, seconds=seconds,
                             error="Claude usage limit reached" if limited else "the Claude model call failed", limited=limited)
    data = envelope.get("structured_output")
    if not isinstance(data, dict):
        return CliResult(None, cost=cost, seconds=seconds, error="the answer did not match the output form")
    return CliResult(data, cost=cost, seconds=seconds)
