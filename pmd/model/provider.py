"""Model routing. Codex receives input inline with its tool surfaces disabled."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from ..config import Config
from . import cli


def _strict(schema: dict) -> dict:
    """Adapt the shared schema to Codex's strict structured output format."""
    if schema.get("type") == "object":
        required = set(schema.get("required", ()))
        properties = {key: _strict(value) for key, value in schema["properties"].items()}
        for key in properties.keys() - required:
            properties[key] = {"anyOf": [properties[key], {"type": "null"}]}
        return {**schema, "properties": properties, "required": list(properties), "additionalProperties": False}
    if schema.get("type") == "array":
        return {**schema, "items": _strict(schema["items"])}
    return schema


def _omit_nulls(value):
    if isinstance(value, dict):
        return {key: _omit_nulls(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_omit_nulls(item) for item in value]
    return value


def codex(prompt: str, schema: dict, *, timeout: int, model: str | None = None, effort: str | None = None) -> cli.CliResult:
    started = time.monotonic()
    data = None
    error = ""
    binary = shutil.which("codex")
    if binary is None:
        return cli.CliResult(None, error="the Codex CLI is not on PATH", provider="codex")
    with tempfile.TemporaryDirectory(prefix="pmd-codex-") as dirname:
        root = Path(dirname)
        schema_path = root / "schema.json"
        output = root / "answer.json"
        schema_path.write_text(json.dumps(_strict(schema)), encoding="utf-8")
        args = [binary, "-a", "never", "exec", "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules",
                "--ephemeral", "--sandbox", "read-only", "-c", 'web_search="disabled"',
                "-c", "sandbox_workspace_write.network_access=false", "-c", 'project_doc_max_bytes=0',
                "--disable", "shell_tool", "--disable", "unified_exec", "--disable", "apps", "--disable", "hooks",
                "--disable", "multi_agent", "--disable", "multi_agent_v2", "--disable", "skill_search",
                "--output-schema", str(schema_path), "--output-last-message", str(output), "-"]
        if model:
            args[-1:-1] = ["--model", model]
        if effort:
            args[-1:-1] = ["-c", f"model_reasoning_effort={json.dumps(effort)}"]
        keep = ("HOME", "PATH", "LANG", "LC_ALL", "TMPDIR", "USER", "SHELL", "CODEX_HOME", "OPENAI_API_KEY")
        env = {name: os.environ[name] for name in keep if name in os.environ}
        try:
            done = subprocess.run(args, input=prompt, text=True, capture_output=True, cwd=root, env=env, timeout=timeout)
            if done.returncode:
                error = f"the Codex CLI exited with status {done.returncode}"
            else:
                answer = json.loads(output.read_text(encoding="utf-8"))
                if isinstance(answer, dict):
                    data = _omit_nulls(answer)
                else:
                    error = "the Codex CLI returned an invalid answer"
        except subprocess.TimeoutExpired:
            error = f"the Codex CLI timed out after {timeout} s"
        except (OSError, ValueError):
            error = "could not read a structured answer from the Codex CLI"
    return cli.CliResult(data, seconds=time.monotonic() - started, error=error, provider="codex")


def call(prompt: str, schema: dict, *, config: Config, cwd: Path, timeout: int, budget_usd: float,
         model: str | None = None, tools: str = "", fallback_prompt: Callable[[], str] | None = None) -> cli.CliResult:
    result = cli.call(prompt, schema, cwd=cwd, tools=tools, timeout=timeout, budget_usd=budget_usd,
                      model=config.claude_model or model, effort=config.claude_effort,
                      token_file=config.data_dir / "secrets" / "claude-token")
    remaining = int(timeout - result.seconds)
    if not (result.limited and config.codex_fallback and remaining > 0):
        return result
    fallback = codex(fallback_prompt() if fallback_prompt else prompt, schema,
                     timeout=remaining, model=config.codex_model, effort=config.codex_effort)
    return replace(fallback, seconds=result.seconds + fallback.seconds, cost=result.cost)
