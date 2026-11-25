"""Calls the Claude Code CLI headless, with read tools only (Part 3 §10-5, §11-4).

``--restricted`` removes every tool that runs commands or code and WebFetch,
ignores user, project and local settings files, and confines the file tools to the
working directory. ``--tools`` then names the whole tool set. Checked against
Claude Code 2.1.321 on 2026-11-25.
"""

from __future__ import annotations

import json
import os
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


def build_command(prompt: str, schema: dict, *, tools: str, model: str | None, budget_usd: float, binary: str = "claude") -> list[str]:
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
         budget_usd: float = 1.0, timeout: int = 600, token_file: Path | None = None, binary: str = "claude") -> CliResult:
    if shutil.which(binary) is None:
        return CliResult(None, error=f"{binary} is not installed or not on PATH")
    started = time.monotonic()
    try:
        done = subprocess.run(build_command(prompt, schema, tools=tools, model=model, budget_usd=budget_usd, binary=binary),
                              cwd=cwd, capture_output=True, text=True, timeout=timeout, env=_env(token_file), check=False)
    except subprocess.TimeoutExpired:
        return CliResult(None, seconds=time.monotonic() - started, error=f"the model call timed out after {timeout} s")
    seconds = time.monotonic() - started
    try:
        envelope = json.loads(done.stdout)
    except json.JSONDecodeError:
        tail = (done.stderr or done.stdout).strip().splitlines()
        return CliResult(None, seconds=seconds, error=f"the CLI gave no JSON (exit {done.returncode}): {tail[-1] if tail else ''}"[:300])
    cost = float(envelope.get("total_cost_usd") or 0)
    if envelope.get("is_error") or envelope.get("subtype") != "success":
        return CliResult(None, cost=cost, seconds=seconds, error=f"the model call failed: {envelope.get('subtype')}")
    data = envelope.get("structured_output")
    if not isinstance(data, dict):
        return CliResult(None, cost=cost, seconds=seconds, error="the answer did not match the output form")
    return CliResult(data, cost=cost, seconds=seconds)
