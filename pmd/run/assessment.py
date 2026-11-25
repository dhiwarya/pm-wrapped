"""ST-4 and ST-5: cited prose and proposals for one project."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ..data import run as R
from ..model import cli, judge, quote, reader
from ..rules.gates import is_open_gate
from ..rules.workdays import working_days_between


@dataclass
class Source:
    id: int
    commit: str
    checkout: Path
    mirror: Path
    github: Path
    github_read: bool


def previous_read(conn, snapshot, source_id):
    return conn.execute("SELECT * FROM model_source WHERE snapshot_id = ? AND source_id = ?", (snapshot, source_id)).fetchone()


def record(conn, run_id, pid, stage, answer, kept=0, dropped=0, error=""):
    R.insert(conn, "model_read", {"run_id": run_id, "project_id": pid, "stage": stage, "provider": answer.provider,
                                 "seconds": answer.seconds, "cost": answer.cost, "kept": kept, "dropped": dropped,
                                 "error": error})
    if error:
        R.add_problem(conn, run_id=run_id, project_id=pid, stage=stage, adapter=answer.provider, reason=error)


class ModelBudget:
    def __init__(self, conn, config, invoke, baseline):
        self.config = config
        self.invoke = invoke
        self.model = R.setting(conn, "model") or None
        self.remaining_seconds = int(R.setting(conn, "model_timeout_s", "600") or 600)
        budget_key = "baseline_model_budget_usd" if baseline else "model_budget_usd"
        default = "3.00" if baseline else "2.00"
        self.remaining_cost = float(R.setting(conn, budget_key, default) or default)

    def call(self, prompt, schema, cwd):
        timeout = min(int(self.remaining_seconds), 180)
        budget = int(self.remaining_cost * 100 + 1e-7) / 100
        if timeout < 1 or budget < 0.01:
            return cli.CliResult(None, error="the run's model time or Claude budget has been used", provider="none")
        result = self.invoke(prompt, schema, config=self.config, cwd=cwd, tools="", timeout=timeout,
                             budget_usd=budget, model=self.model)
        self.remaining_seconds -= result.seconds
        self.remaining_cost -= result.cost
        return result


def _old_facts(rows, source, passages):
    changed_objects = {p.ref.get("src_url") for p in passages if p.ref.get("src_url")}
    for row in rows:
        fact = dict(row)
        for key in ("id", "snapshot_id", "project_id"):
            del fact[key]
        fact["carried_over"] = 0
        if fact["src_url"] and not source.github_read:
            continue  # the fetch stage already carried these rows over
        if fact["src_path"] and fact["src_commit"] != source.commit:
            path = source.checkout / fact["src_path"]
            if not path.is_file():
                continue
            lines = quote.find(fact["quote"], path.read_text(encoding="utf-8", errors="replace"))
            if not lines:
                continue
            fact.update(src_commit=source.commit, src_line_from=lines[0], src_line_to=lines[1])
        elif fact["src_url"] and fact["src_url"] in changed_objects:
            matching = [p for p in passages if p.ref.get("src_url") == fact["src_url"]]
            if not any(quote.find(fact["quote"], p.text) for p in matching):
                continue
        yield fact


def _identity(fact):
    return tuple(fact[k] for k in ("kind", "subject_type", "subject_key", "quote", "src_path", "src_url") if k in fact)


def read(conn, run_id, snap, prev, project, sources, work, baseline, now, budget):
    pid = project["id"]
    known = {"project": [project["key"]], "unit": sorted({r["unit"] for table in ("unit_state", "unit_stage_state") for r in R.rows(conn, table, snap, pid)}),
             "task": sorted({r["key"] for r in [*R.rows(conn, "task_state", snap, pid), *R.hand_tasks(conn, pid)]}),
             "gate": [r["key"] for r in R.gates(conn, pid)],
             "pr": [f"{r['src_source_id']}#{r['number']}" for r in R.rows(conn, "pr_state", snap, pid)],
             "issue": [f"{r['src_source_id']}#{r['number']}" for r in R.rows(conn, "issue_state", snap, pid)]}
    ok = True
    previous = [r for r in R.previous_rows(conn, "fact", prev, pid) if r["read_by"] == "MODEL"]
    for source in sources:
        marker = None if baseline else previous_read(conn, prev, source.id)
        prior_commit = marker["commit_sha"] if marker else ""
        prior_time = marker["read_at"] if marker else ""
        files = reader.file_inputs(source.mirror, source.checkout, source.id, source.commit, prior_commit)
        try:
            passages = [*files, *(reader.github_inputs(source.github, source.id, prior_time) if source.github_read else ())]
            old = list(_old_facts((r for r in previous if r["src_source_id"] == source.id), source, passages))
        except (OSError, ValueError):
            record(conn, run_id, pid, "ST-4", cli.CliResult(None, provider="none"), error="could not prepare the prose input")
            if marker:
                R.insert(conn, "model_source", {**dict(marker), "snapshot_id": snap})
            ok = False
            continue
        retained = {_identity(f): f for f in old}
        complete = True
        for batch in reader.batches(passages):
            answer = budget.call(reader.prompt({**known, "source_id": source.id}, batch), reader.SCHEMA, work)
            facts, dropped, problem = reader.checked(answer.data, batch, known) if answer.data is not None else ([], 0, answer.error or "prose reading failed")
            record(conn, run_id, pid, "ST-4", answer, len(facts), dropped, problem)
            for fact in facts:
                # Give new and carried rows the same identity, including nullable references.
                retained[_identity({**dict.fromkeys(R.SRC_COLUMNS), **fact})] = fact
            complete &= not problem
            if answer.data is None:
                break
        R.insert_many(conn, "fact", [{**f, "snapshot_id": snap, "project_id": pid} for f in retained.values()])
        if complete or marker:
            R.insert(conn, "model_source", {"snapshot_id": snap, "source_id": source.id,
                                            "commit_sha": source.commit if complete else prior_commit,
                                            "read_at": R.stamp(now) if complete and source.github_read else prior_time})
        ok &= complete
    return ok


def stated_blockers(conn, snap, previous, pid, today, holidays):
    prior = R.previous_rows(conn, "blocker", previous, pid)
    people = R.people_index(conn)
    for fact in R.rows(conn, "fact", snap, pid):
        if fact["kind"] != "blocker.stated":
            continue
        value = json.loads(fact["value"])
        kind = fact["subject_type"].upper()
        if kind not in ("TASK", "UNIT", "PR", "GATE", "PROJECT"):
            continue
        key = fact["subject_key"].split("#", 1)[-1] if kind == "PR" else fact["subject_key"]
        since = fact["fact_date"] or next((r["since"] for r in prior if r["kind"] == "STATED" and r["detail"] == value["text"]), today.isoformat())
        person = next((name for name in value["people"] if name.strip().lower() in people), next(iter(value["people"]), ""))
        R.insert(conn, "blocker", {"snapshot_id": snap, "project_id": pid, "kind": "STATED", "subject_type": kind,
                                   "subject_key": key, "detail": value["text"], "person_raw": person,
                                   "person_id": people.get(person.strip().lower()), "since": since,
                                   "age_days": working_days_between(date.fromisoformat(since), today, holidays),
                                   **{k: fact[k] for k in R.SRC_COLUMNS}})


def propose(conn, run_id, snap, project, today, holidays, unread, work, budget):
    pid = project["id"]
    open_gates = [gate for gate in R.gates(conn, pid) if is_open_gate(gate)]
    horizon = int(R.setting(conn, "gate_horizon", "2") or 2)
    if not open_gates:
        return True
    tasks = [*R.rows(conn, "task_state", snap, pid), *R.hand_tasks(conn, pid)]
    prs = R.rows(conn, "pr_state", snap, pid)
    facts = R.rows(conn, "fact", snap, pid)
    blockers = R.rows(conn, "blocker", snap, pid)
    evidence = R.evidence(conn, pid)
    links = R.evidence_links(conn, [i["id"] for i in evidence])
    states = {r["evidence_item_id"]: r["run_state"] for r in conn.execute(
        "SELECT * FROM item_state WHERE snapshot_id = ?", (snap,))}
    evidence_days = int(R.setting(conn, "evidence_risk_days", "5") or 5)
    blocker_days = int(R.setting(conn, "blocker_risk_days", "3") or 3)
    proposals, contexts, allowed = [], [], {}
    for gate in open_gates[:horizon]:
        items = [i for i in evidence if i["gate_id"] == gate["id"]]
        subjects, relevant = judge.linked(gate, [link for i in items for link in links[i["id"]]], facts, tasks, prs)
        gate_blockers = []
        for blocker in blockers:
            kind, key = blocker["subject_type"], blocker["subject_key"]
            key = f"{blocker['src_source_id']}#{key}" if kind == "PR" else key
            kind = "TASK" if kind == "HAND_TASK" else kind
            if (kind, key) in subjects or kind == "PROJECT":
                gate_blockers.append(blocker)
        proposal, context, ids = judge.context_for(gate, [{"text": i["text"], "state": i["hand_state"] or states.get(i["id"], "NOT_STARTED")} for i in items],
                                                   gate_blockers, relevant, today, holidays, unread, evidence_days, blocker_days)
        proposals.append(proposal)
        if project["prose_reading"] and proposal["floor"] in ("ON_TRACK", "AT_RISK") and any(
            f["read_by"] == "MODEL" or f["src_object"] for f in relevant
        ):
            contexts.append(context)
            allowed[gate["id"]] = ids
    ok = True
    if contexts:
        answer = budget.call(judge.prompt(contexts), judge.SCHEMA, work)
        ok = answer.data is not None and judge.checked(answer.data, proposals, allowed)
        record(conn, run_id, pid, "ST-5", answer, error="" if ok else answer.error or "some gate judgments lacked valid citations")
    R.insert_many(conn, "proposal", [{**p, "snapshot_id": snap, "reasons": json.dumps(p["reasons"])} for p in proposals])
    return ok
