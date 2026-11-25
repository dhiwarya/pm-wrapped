"""The web application (Detailed Design Part 1 §2-1, §4-1; Part 2 §7).

Every changing request is a POST from PMD's own pages. Each action writes hand
tables only, then redirects back with a short message.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import __version__
from ..config import Config
from ..data import db, hand, views
from ..data.hand import InputError
from . import present
from .guard import GuardMiddleware

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals.update(
    STATUS=present.STATUS,
    OUTCOME=present.OUTCOME,
    ITEM_STATE=present.ITEM_STATE,
    COLOURS=hand.COLOURS,
    COLOUR_TILE=present.COLOUR_TILE,
    COLOUR_WORD=present.COLOUR_WORD,
    TASK_STATUSES=hand.TASK_STATUSES,
    TASK_STATUS_WORD=present.TASK_STATUS_WORD,
    plural=present.plural,
    short_date=present.short_date,
    version=__version__,
    RUN_RESULT=present.RUN_RESULT,
    BLOCKER_WORD=present.BLOCKER_WORD,
    KIND_WORD=present.KIND_WORD,
    LINK_WORD=present.LINK_WORD,
    SOURCE_ROLES=hand.SOURCE_ROLES,
    json_pretty=present.json_pretty,
)
templates.env.filters["fromjson"] = json.loads


def default_spawner(config: Config, config_path: Path | None) -> Callable[[list[str]], None]:
    """Start ``pm-dash run`` or ``pm-dash onboard`` as its own process and return at once (Part 1 §2-1)."""

    def spawn(args: list[str]) -> None:
        logs = config.data_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        cmd = [sys.executable, "-m", "pmd.cli", *(["--config", str(config_path)] if config_path else []), *args]
        with open(logs / f"{args[0]}.log", "ab") as log:
            subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True, cwd=config.data_dir)

    return spawn


def safe_back(value: object, default: str) -> str:
    """Only a path on this site may be a redirect target (no open redirect)."""
    text = str(value or "")
    if not text.startswith("/") or text.startswith("//") or "\\" in text or "\n" in text or "\r" in text:
        return default
    return text


def with_query(url: str, **params: str) -> str:
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k not in ("msg", "err", *params)]
    query += [(k, v) for k, v in params.items() if v]
    return urlunsplit(("", "", parts.path, urlencode(query), parts.fragment))


def redirect(url: str, *, msg: str = "", err: str = "") -> RedirectResponse:
    return RedirectResponse(with_query(url, msg=msg, err=err), status_code=303)


def create_app(config: Config, *, now: Callable[[], datetime] | None = None, config_path: Path | None = None,
               spawn: Callable[[list[str]], None] | None = None) -> FastAPI:
    app = FastAPI(title="PM Dash", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config = config
    app.state.now = now or (lambda: datetime.now(UTC))
    app.state.spawn = spawn or default_spawner(config, config_path)
    db.open_store(config.db_path).close()

    app.add_middleware(GuardMiddleware, config=config)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
        conn = db.connect(config.db_path)
        try:
            if request.method in ("GET", "HEAD"):
                conn.execute("BEGIN")
            yield conn
        finally:
            conn.close()

    def render(request: Request, name: str, ctx: dict, *, nav: str = "", status: int = 200,
               conn: sqlite3.Connection | None = None) -> HTMLResponse:
        own = conn is None
        conn = conn or db.connect(config.db_path)
        try:
            head = present.header(conn, app.state.now())
        finally:
            if own:
                conn.close()
        ctx = {
            "head": head,
            "nav": nav,
            "msg": request.query_params.get("msg", "")[:300],
            "err": request.query_params.get("err", "")[:300],
            **ctx,
        }
        return templates.TemplateResponse(request, name, ctx, status_code=status)

    def project_or_404(conn: sqlite3.Connection, key: str) -> sqlite3.Row:
        project = hand.get_project(conn, key)
        if project is None:
            raise StarletteHTTPException(status_code=404)
        return project

    async def act(request: Request, default_back: str, fn: Callable[[dict], str | tuple[str, str]]):
        form = dict(await request.form())
        back = safe_back(form.get("back"), default_back)
        try:
            result = fn(form)
        except InputError as exc:
            return redirect(back, err=str(exc))
        except sqlite3.IntegrityError:
            return redirect(back, err="That change conflicts with data already stored. Nothing was saved.")
        msg, target = (result, back) if isinstance(result, str) else result
        return redirect(target, msg=msg)

    @app.exception_handler(StarletteHTTPException)
    async def not_found(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404:
            return render(request, "notfound.html", {}, status=404)
        return PlainTextResponse(str(exc.detail), status_code=exc.status_code)

    # ------------------------------------------------------------ views

    @app.get("/", response_class=HTMLResponse)
    def today_view(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        return render(request, "today.html", present.build_today(conn, app.state.now()), nav="today", conn=conn)

    @app.get("/changes", response_class=HTMLResponse)
    @app.get("/changes/{day}", response_class=HTMLResponse)
    def changes_view(request: Request, day: str | None = None, conn: sqlite3.Connection = Depends(get_conn)):
        digest = views.digest(conn, day)
        if day and digest is None:
            raise StarletteHTTPException(status_code=404)
        snapshot = views.current_snapshot(conn)
        snapshot_id = snapshot["id"] if snapshot else None
        repos = present.repos_of(conn)
        names = {p["id"]: p["name"] for p in conn.execute("SELECT id, name FROM project")}
        rows = json.loads(digest["changes"]) if day else [dict(row) for row in views.changes(conn, snapshot_id)]
        changes = [dict(row, project=names.get(row["project_id"]) or "Dashboard", project_group=row["project_id"] or 0,
                        src=present.src_link(json.loads(row.get("source", "{}")), repos)) for row in rows]
        return render(request, "changes.html", {"digest": digest, "days": views.digest_days(conn), "changes": changes,
                                                  "historical": bool(day)}, nav="changes", conn=conn)

    @app.get("/story", response_class=HTMLResponse)
    @app.get("/story/{day}", response_class=HTMLResponse)
    def story_view(request: Request, day: str | None = None, slide: int = 0, conn: sqlite3.Connection = Depends(get_conn)):
        digest = views.digest(conn, day)
        if digest is None:
            if not day:
                return RedirectResponse("/changes", status_code=303)
            raise StarletteHTTPException(status_code=404)
        cards = json.loads(digest["cards"])
        decision_ids = [card["gate_id"] for card in cards if card["kind"] == "DECISION"]
        confs = hand.latest_confirmations(conn, decision_ids)
        snap = views.current_snapshot(conn)
        latest = views.health_proposals(conn, snap["id"] if snap else None)
        for card in cards:
            if card["kind"] != "DECISION":
                continue
            gid = card["gate_id"]
            gate = conn.execute("SELECT outcome, closed_at FROM gate WHERE id = ?", (gid,)).fetchone()
            conf = confs.get(gid)
            proposal = latest.get(gid)
            card["live_confirmation"] = present.STATUS[conf["value"]]["word"] if conf else ""
            card["can_confirm"] = bool(gate and not gate["outcome"] and gate["closed_at"] is None and proposal
                                       and proposal["value"] == card["value"] and (not conf or conf["value"] != card["value"]))
        return render(request, "story.html", {"digest": digest, "cards": cards, "slide": max(0, min(slide, len(cards) - 1))},
                      nav="today", conn=conn)

    @app.get("/facts/{fid}", response_class=HTMLResponse)
    def fact_view(request: Request, fid: int, conn: sqlite3.Connection = Depends(get_conn)):
        fact = views.fact(conn, fid)
        if fact is None:
            raise StarletteHTTPException(status_code=404)
        return render(request, "fact.html", {"fact": fact, "value": json.loads(fact["value"]),
                                              "src": present.src_link(fact, present.repos_of(conn))}, conn=conn)

    @app.get("/health", response_class=PlainTextResponse)
    def health(conn: sqlite3.Connection = Depends(get_conn)):
        last = views.last_run(conn)
        snap = views.current_snapshot(conn)
        return (f"ok\nlast run: {last['result'] + ' ' + last['started_at'] if last else 'none'}\n"
                f"snapshot: {snap['created_at'] if snap else 'none'}\n")

    @app.get("/projects/new", response_class=HTMLResponse)
    def new_project_view(request: Request):
        return render(request, "project_new.html", {}, nav="new")

    @app.get("/p/{key}", response_class=HTMLResponse)
    def project_view(request: Request, key: str, tab: str = "overview", conn: sqlite3.Connection = Depends(get_conn)):
        project = project_or_404(conn, key)
        if tab not in ("overview", "gates", "tasks", "sources", "reading", "holidays"):
            tab = "overview"
        ctx = present.build_project(conn, project, app.state.now())
        return render(request, "project.html", {**ctx, "tab": tab}, nav="project", conn=conn)

    @app.get("/settings", response_class=HTMLResponse)
    def settings_view(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        ctx = {
            "time_zone": hand.get_setting(conn, "time_zone", ""),
            "host_zone": datetime.now().astimezone().tzname(),
            "holidays": hand.list_holidays(conn),
            "people": hand.list_people(conn),
            "archived": hand.list_projects(conn, ("ARCHIVED",)),
            "today": present.full_date(present.today(conn, app.state.now())),
            "run_time": hand.get_setting(conn, "run_time", "07:00"),
            "review_wait_days": hand.get_setting(conn, "review_wait_days", "2"),
            "stale_input_days": hand.get_setting(conn, "stale_input_days", "3"),
            "owner_person_id": hand.get_setting(conn, "owner_person_id", ""),
            "evidence_risk_days": hand.get_setting(conn, "evidence_risk_days", "5"),
            "blocker_risk_days": hand.get_setting(conn, "blocker_risk_days", "3"),
            "model_timeout_s": hand.get_setting(conn, "model_timeout_s", "600"),
            "model_budget_usd": hand.get_setting(conn, "model_budget_usd", "2.00"),
            "baseline_model_budget_usd": hand.get_setting(conn, "baseline_model_budget_usd", "3.00"),
            "unmatched": views.unmatched_names(conn, (views.current_snapshot(conn) or {"id": None})["id"]),
            "aliases": conn.execute("SELECT a.*, p.display_name FROM person_alias a JOIN person p ON p.id = a.person_id ORDER BY p.display_name, a.kind").fetchall(),
        }
        return render(request, "settings.html", ctx, nav="settings", conn=conn)

    # ------------------------------------------------------------ projects

    @app.post("/projects")
    async def create_project(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        def do(f):
            locations = [line.strip() for line in str(f.get("sources", "")).splitlines() if line.strip()]
            if not locations:
                hand.create_project(conn, f.get("key"), f.get("name"), f.get("description"), f.get("scope_note"))
                key = str(f.get("key", "")).strip().upper()
                return "Project added. Now add its first gate.", f"/p/{key}?tab=gates"
            # With sources: the onboarding agent proposes the setup (Design §3-4).
            from ..data.db import transaction

            with transaction(conn):
                first = hand.suggest_source_key(hand._source_location(locations[0])[0])
                name = str(f.get("name") or "").strip() or first
                key = str(f.get("key") or "").strip().upper() or hand.unique_project_key(conn, first)
                pid = hand.create_project(conn, key, name, f.get("description"), f.get("scope_note"))
                for loc in locations:
                    hand.add_source(conn, pid, loc)
            app.state.spawn(["onboard", "--project", key])
            return "Project added. The onboarding agent is reading its sources.", f"/p/{key}/onboarding?after=0"

        return await act(request, "/projects/new", do)

    @app.post("/p/{key}/edit")
    async def edit_project(request: Request, key: str, conn: sqlite3.Connection = Depends(get_conn)):
        project = project_or_404(conn, key)

        def do(f):
            hand.update_project(conn, project["id"], f.get("name"), f.get("description"), f.get("scope_note"))
            return "Project saved."

        return await act(request, f"/p/{key}", do)

    @app.post("/p/{key}/state")
    async def project_state(request: Request, key: str, conn: sqlite3.Connection = Depends(get_conn)):
        project = project_or_404(conn, key)

        def do(f):
            state = f.get("state")
            hand.set_project_state(conn, project["id"], state)
            return {"ACTIVE": "Project is active.", "PAUSED": "Project paused.", "ARCHIVED": "Project archived."}[
                str(state)
            ]

        return await act(request, f"/p/{key}", do)

    @app.post("/p/{key}/colour")
    async def project_colour(request: Request, key: str, conn: sqlite3.Connection = Depends(get_conn)):
        project = project_or_404(conn, key)

        def do(f):
            hand.set_project_colour(conn, project["id"], f.get("colour"))
            return "Colour saved."

        return await act(request, f"/p/{key}", do)

    @app.post("/p/{key}/delete")
    async def project_delete(request: Request, key: str, conn: sqlite3.Connection = Depends(get_conn)):
        project = project_or_404(conn, key)

        def do(f):
            hand.delete_project(conn, project["id"], f.get("confirm_key"))
            return f"Project {key} deleted.", "/"

        return await act(request, f"/p/{key}", do)

    # ------------------------------------------------------------ gates

    @app.post("/p/{key}/gates")
    async def gate_create(request: Request, key: str, conn: sqlite3.Connection = Depends(get_conn)):
        project = project_or_404(conn, key)

        def do(f):
            gid = hand.create_gate(conn, project["id"], f.get("key"), f.get("name"), f.get("date"), f.get("description"))
            return "Gate added. Add the evidence that must be shown to pass it.", f"/p/{key}?tab=gates#g-{gid}"

        return await act(request, f"/p/{key}?tab=gates", do)

    @app.post("/p/{key}/gates/{gid}")
    async def gate_edit(request: Request, key: str, gid: int, conn: sqlite3.Connection = Depends(get_conn)):
        project = project_or_404(conn, key)

        def do(f):
            hand.get_gate(conn, project["id"], gid)
            hand.update_gate(conn, gid, f.get("name"), f.get("date"), f.get("description"))
            return "Gate saved."

        return await act(request, f"/p/{key}?tab=gates#g-{gid}", do)

    @app.post("/p/{key}/gates/{gid}/close")
    async def gate_close(request: Request, key: str, gid: int, conn: sqlite3.Connection = Depends(get_conn)):
        project = project_or_404(conn, key)

        def do(f):
            hand.get_gate(conn, project["id"], gid)
            hand.close_gate(conn, gid)
            return "Gate closed. It is kept in the record and its key is not reused."

        return await act(request, f"/p/{key}?tab=gates", do)

    @app.post("/p/{key}/gates/{gid}/outcome")
    async def gate_outcome(request: Request, key: str, gid: int, conn: sqlite3.Connection = Depends(get_conn)):
        project = project_or_404(conn, key)

        def do(f):
            hand.get_gate(conn, project["id"], gid)
            hand.record_outcome(
                conn, gid, f.get("outcome", ""), f.get("outcome_note"), f.get("open_scope"),
                f.get("open_owner"), f.get("close_date"),
            )
            return "Outcome recorded." if f.get("outcome") else "Outcome cleared."

        return await act(request, f"/p/{key}?tab=gates#g-{gid}", do)

    @app.post("/p/{key}/gates/{gid}/health")
    async def gate_health(request: Request, key: str, gid: int, conn: sqlite3.Connection = Depends(get_conn)):
        project = project_or_404(conn, key)

        def do(f):
            with db.transaction(conn):
                gate = hand.get_gate(conn, project["id"], gid)
                if gate["outcome"] or gate["closed_at"]:
                    raise InputError("This gate is closed or already has an outcome.")
                proposal_id = None
                value = f.get("value")
                if f.get("proposal_id"):
                    try:
                        proposal_id = int(f["proposal_id"])
                    except ValueError:
                        raise InputError("Invalid gate proposal.") from None
                    proposal = views.health_proposal(conn, proposal_id, gid)
                    snap = views.current_snapshot(conn)
                    latest = views.health_proposals(conn, snap["id"] if snap else None).get(gid)
                    if not proposal or not latest or proposal["value"] != latest["value"]:
                        raise InputError("The gate proposal has changed. Review the current proposal on Today.")
                    if f.get("confirm") == "1":
                        value = proposal["value"]
                hand.add_confirmation(conn, gid, value, f.get("note"), proposal_id=proposal_id)
            return "Health saved as your confirmed value."

        return await act(request, f"/p/{key}?tab=gates#g-{gid}", do)

    # ------------------------------------------------------------ evidence items

    @app.post("/p/{key}/gates/{gid}/items")
    async def item_create(request: Request, key: str, gid: int, conn: sqlite3.Connection = Depends(get_conn)):
        project = project_or_404(conn, key)

        def do(f):
            hand.get_gate(conn, project["id"], gid)
            hand.create_item(conn, gid, f.get("text"))
            return "Evidence item added."

        return await act(request, f"/p/{key}?tab=gates#g-{gid}", do)

    def item_action(path: str, fn: Callable[[sqlite3.Connection, int, dict], str]):
        async def handler(request: Request, key: str, iid: int, conn: sqlite3.Connection = Depends(get_conn)):
            project = project_or_404(conn, key)

            def do(f):
                item = hand.get_item(conn, project["id"], iid)
                return fn(conn, item["id"], f), f"/p/{key}?tab=gates#g-{item['gate_id']}"

            return await act(request, f"/p/{key}?tab=gates", do)

        app.post(f"/p/{{key}}/items/{{iid}}/{path}")(handler)

    def _item_text(conn, iid, f):
        hand.update_item_text(conn, iid, f.get("text"))
        return "Evidence item saved."

    def _item_state(conn, iid, f):
        state = f.get("state", "")
        hand.set_item_state(conn, iid, state, f.get("note"), f.get("evidence_url"))
        return {"MET": "Marked met.", "WAIVED": "Item waived.", "": "Item reopened."}.get(str(state), "Saved.")

    def _item_remove(conn, iid, f):
        how = hand.remove_item(conn, iid)
        return "Met item closed and kept in the record." if how == "closed" else "Evidence item removed."

    def _item_move(conn, iid, f):
        hand.move_item(conn, iid, f.get("direction"))
        return "Order saved."

    item_action("edit", _item_text)
    item_action("state", _item_state)
    item_action("remove", _item_remove)
    item_action("move", _item_move)

    # ------------------------------------------------------------ tasks

    task_fields = ("title", "owner", "gate_key", "start", "finish", "status", "depends_on", "note")

    @app.post("/p/{key}/tasks")
    async def task_create(request: Request, key: str, conn: sqlite3.Connection = Depends(get_conn)):
        project = project_or_404(conn, key)

        def do(f):
            hand.create_task(conn, project["id"], f.get("key"), **{k: f.get(k) for k in task_fields})
            return "Task added."

        return await act(request, f"/p/{key}?tab=tasks", do)

    @app.post("/p/{key}/tasks/{tid}")
    async def task_edit(request: Request, key: str, tid: int, conn: sqlite3.Connection = Depends(get_conn)):
        project = project_or_404(conn, key)

        def do(f):
            hand.get_task(conn, project["id"], tid)
            hand.update_task(conn, project["id"], tid, **{k: f.get(k) for k in task_fields})
            return "Task saved."

        return await act(request, f"/p/{key}?tab=tasks", do)

    @app.post("/p/{key}/tasks/{tid}/remove")
    async def task_remove(request: Request, key: str, tid: int, conn: sqlite3.Connection = Depends(get_conn)):
        project = project_or_404(conn, key)

        def do(f):
            hand.get_task(conn, project["id"], tid)
            hand.delete_task(conn, project["id"], tid)
            return "Task removed."

        return await act(request, f"/p/{key}?tab=tasks", do)

    # ------------------------------------------------------------ holidays and settings

    @app.post("/p/{key}/holidays")
    async def project_holiday_add(request: Request, key: str, conn: sqlite3.Connection = Depends(get_conn)):
        project = project_or_404(conn, key)

        def do(f):
            hand.add_holiday(conn, f.get("date"), f.get("label"), project["id"])
            return "Holiday added."

        return await act(request, f"/p/{key}?tab=holidays", do)

    @app.post("/p/{key}/holidays/{hid}/remove")
    async def project_holiday_remove(request: Request, key: str, hid: int, conn: sqlite3.Connection = Depends(get_conn)):
        project = project_or_404(conn, key)

        def do(f):
            hand.remove_holiday(conn, hid, project["id"])
            return "Holiday removed."

        return await act(request, f"/p/{key}?tab=holidays", do)

    @app.post("/settings/time-zone")
    async def set_time_zone(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        def do(f):
            name = str(f.get("time_zone", "")).strip()
            if name:
                try:
                    ZoneInfo(name)
                except (ZoneInfoNotFoundError, ValueError):
                    raise InputError(f"{name} is not a time zone name. Use a name like Asia/Jakarta.") from None
            hand.set_setting(conn, "time_zone", name)
            return "Time zone saved." if name else "Time zone cleared: the host's time zone is used."

        return await act(request, "/settings", do)

    @app.post("/settings/holidays")
    async def holiday_add(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        def do(f):
            hand.add_holiday(conn, f.get("date"), f.get("label"))
            return "Holiday added for all projects."

        return await act(request, "/settings", do)

    @app.post("/settings/holidays/{hid}/remove")
    async def holiday_remove(request: Request, hid: int, conn: sqlite3.Connection = Depends(get_conn)):
        def do(f):
            hand.remove_holiday(conn, hid)
            return "Holiday removed."

        return await act(request, "/settings", do)

    @app.post("/settings/people")
    async def person_add(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        def do(f):
            hand.add_person(conn, f.get("name"))
            return "Person added."

        return await act(request, "/settings", do)

    @app.post("/settings/people/{pid}")
    async def person_rename(request: Request, pid: int, conn: sqlite3.Connection = Depends(get_conn)):
        def do(f):
            hand.rename_person(conn, pid, f.get("name"))
            return "Name saved."

        return await act(request, "/settings", do)

    # ------------------------------------------------------------ Phase 2: run, views of the snapshot

    def pick_project(conn: sqlite3.Connection, key: str | None) -> sqlite3.Row | None:
        if key:
            return hand.get_project(conn, key)
        projects = hand.list_projects(conn)
        return projects[0] if projects else None

    @app.post("/run")
    async def run_now(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        def do(f):
            if views.running(conn):
                raise InputError("A run or onboarding is in progress. Wait for it to end.")
            key = str(f.get("project") or "").strip()
            if key and hand.get_project(conn, key) is None:
                raise InputError(f"There is no project {key}.")
            app.state.spawn(["run", "--trigger", "hand", *(["--project", key] if key else [])])
            return "Run started. This page refreshes until it ends.", "/runs"

        return await act(request, "/runs", do)

    @app.get("/runs", response_class=HTMLResponse)
    def runs_view(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        tz = present.zone(conn)
        now_ = app.state.now()
        items = views.runs(conn)
        ids = [r["id"] for r in items]
        srcs, probs = views.run_sources(conn, ids), views.run_problems(conn, ids)
        rows = []
        for r in items:
            took = ""
            if r["ended_at"]:
                secs = int((present.local_stamp(r["ended_at"], tz) - present.local_stamp(r["started_at"], tz)).total_seconds())
                took = f"{secs // 60} min {secs % 60:02d} s" if secs >= 60 else f"{secs} s"
            rows.append({"row": r, "when": present.when_text(r["started_at"], tz, now_), "took": took, "sources": srcs[r["id"]],
                         "problems": probs[r["id"]], "counts": views.run_counts(conn, r["id"]), "look": present.RUN_RESULT[r["result"]],
                         "model_reads": views.model_reads(conn, r["id"])})
        projects = hand.list_projects(conn)
        return render(request, "runs.html", {"rows": rows, "projects": projects}, nav="runs", conn=conn)

    @app.get("/units", response_class=HTMLResponse)
    def units_view(request: Request, project: str | None = None, conn: sqlite3.Connection = Depends(get_conn)):
        candidates = [p for p in hand.list_projects(conn) if views.facts(conn, (views.current_snapshot(conn) or {"id": None})["id"], p["id"], "unit.exists")]
        chosen = hand.get_project(conn, project) if project else (candidates[0] if candidates else None)
        ctx = present.build_units(conn, chosen) if chosen else {"project": None, "rows": []}
        return render(request, "units.html", {**ctx, "candidates": candidates}, nav="units", conn=conn)

    @app.get("/timeline", response_class=HTMLResponse)
    def timeline_view(request: Request, project: str | None = None, gate: str = "", owner: str = "", status: str = "",
                      late: str = "", view: str = "chart", conn: sqlite3.Connection = Depends(get_conn)):
        chosen = pick_project(conn, project)
        if chosen is None:
            return render(request, "timeline.html", {"project": None, "projects": []}, nav="timeline", conn=conn)
        day = present.today(conn, app.state.now())
        tasks = present.timeline_tasks(conn, chosen, day)
        owners = sorted({t["owner"] for t in tasks if t["owner"]})
        shown = [t for t in tasks if (not gate or t["gate"] == gate) and (not owner or t["owner"] == owner)
                 and (not status or t["status"] == status) and (not late or t["late"])]
        gates = hand.list_gates(conn, chosen["id"])
        ctx = {"project": chosen, "projects": hand.list_projects(conn), "tasks": shown, "all_count": len(tasks), "gates": gates,
               "owners": owners, "f": {"gate": gate, "owner": owner, "status": status, "late": late, "view": view},
               "svg": present.timeline_svg(shown, [g for g in gates if not gate or g["key"] == gate], day), "repos": present.repos_of(conn),
               "src_link": present.src_link}
        return render(request, "timeline.html", ctx, nav="timeline", conn=conn)

    # ------------------------------------------------------------ sources and reading settings

    @app.post("/p/{key}/sources")
    async def source_add(request: Request, key: str, conn: sqlite3.Connection = Depends(get_conn)):
        project = project_or_404(conn, key)

        def do(f):
            hand.add_source(conn, project["id"], f.get("location"), key=f.get("key"), role=f.get("role") or "DOCS", branches=f.get("branches"))
            return "Source added. Use \"Look for new setup\" to let the agent read it."

        return await act(request, f"/p/{key}?tab=sources", do)

    @app.post("/p/{key}/sources/{sid}")
    async def source_edit(request: Request, key: str, sid: int, conn: sqlite3.Connection = Depends(get_conn)):
        project = project_or_404(conn, key)

        def do(f):
            hand.get_source(conn, project["id"], sid)
            hand.update_source(conn, project["id"], sid, role=f.get("role"), branches=f.get("branches"), enabled=f.get("enabled", "0"))
            return "Source saved."

        return await act(request, f"/p/{key}?tab=sources", do)

    @app.post("/p/{key}/sources/{sid}/remove")
    async def source_remove(request: Request, key: str, sid: int, conn: sqlite3.Connection = Depends(get_conn)):
        project = project_or_404(conn, key)

        def do(f):
            hand.get_source(conn, project["id"], sid)
            hand.remove_source(conn, project["id"], sid)
            return "Source removed. Its mirror stays in the data directory until you delete it."

        return await act(request, f"/p/{key}?tab=sources", do)

    @app.post("/p/{key}/reading")
    async def reading_save(request: Request, key: str, conn: sqlite3.Connection = Depends(get_conn)):
        project = project_or_404(conn, key)

        def do(f):
            hand.set_reading(conn, project["id"], wbs_source=f.get("wbs_source"), wbs_path=f.get("wbs_path"), wbs_mapping=f.get("wbs_mapping"),
                             dashboard_source=f.get("dashboard_source"), dashboard_path=f.get("dashboard_path"),
                             branch_pattern=f.get("branch_pattern"), task_key_pattern=f.get("task_key_pattern"),
                             prose_reading=f.get("prose_reading", "0"))
            return "Reading settings saved. They apply from the next run."

        return await act(request, f"/p/{key}?tab=reading", do)

    # ------------------------------------------------------------ evidence links and names

    @app.post("/p/{key}/items/{iid}/links")
    async def link_add(request: Request, key: str, iid: int, conn: sqlite3.Connection = Depends(get_conn)):
        project = project_or_404(conn, key)

        def do(f):
            item = hand.get_item(conn, project["id"], iid)
            hand.add_link(conn, item["id"], f.get("kind"), f.get("value"))
            return "Link added. The next run follows it.", f"/p/{key}?tab=gates#g-{item['gate_id']}"

        return await act(request, f"/p/{key}?tab=gates", do)

    @app.post("/p/{key}/links/{lid}/remove")
    async def link_remove(request: Request, key: str, lid: int, conn: sqlite3.Connection = Depends(get_conn)):
        project = project_or_404(conn, key)

        def do(f):
            hand.remove_link(conn, project["id"], lid)
            return "Link removed."

        return await act(request, f"/p/{key}?tab=gates", do)

    @app.post("/settings/names")
    async def name_attach(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        def do(f):
            hand.attach_name(conn, f.get("value"), f.get("kind"), f.get("person"))
            return "Name attached. The next run matches it."

        return await act(request, "/settings", do)

    @app.post("/settings/run")
    async def run_settings(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        def do(f):
            import re as _re

            run_time = str(f.get("run_time", "07:00")).strip()
            if not _re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", run_time):
                raise InputError("Run time must be like 07:00.")
            values = {name: str(f.get(name, hand.get_setting(conn, name, default))).strip() for name, default in (
                ("review_wait_days", "2"), ("stale_input_days", "3"), ("evidence_risk_days", "5"), ("blocker_risk_days", "3"))}
            if not all(value.isascii() and value.isdigit() and len(value) <= 2 and 0 < int(value) < 60 for value in values.values()):
                raise InputError("The thresholds are whole numbers of working days, 1 to 59.")
            owner = str(f.get("owner_person_id", hand.get_setting(conn, "owner_person_id", "")))
            if owner and owner not in {str(person["id"]) for person in hand.list_people(conn)}:
                raise InputError("Choose a person from the dashboard.")
            timeout = str(f.get("model_timeout_s", hand.get_setting(conn, "model_timeout_s", "600"))).strip()
            if not _re.fullmatch(r"[0-9]{1,4}", timeout) or not 1 <= int(timeout) <= 1800:
                raise InputError("Enter a model time limit from 1 to 1800 seconds.")
            budgets = {name: str(f.get(name, hand.get_setting(conn, name, default))).strip() for name, default in (
                ("model_budget_usd", "2.00"), ("baseline_model_budget_usd", "3.00"))}
            if not all(_re.fullmatch(r"[0-9]{1,3}(\.[0-9]{1,2})?", value) and 0.01 <= float(value) <= 100 for value in budgets.values()):
                raise InputError("Enter Claude budgets from $0.01 to $100.00.")
            with db.transaction(conn):
                for name, value in {**values, **budgets, "model_timeout_s": timeout, "run_time": run_time, "owner_person_id": owner}.items():
                    hand.set_setting(conn, name, value)
            return "Run settings saved. The timer itself is set where PM Dash is installed."

        return await act(request, "/settings", do)

    # ------------------------------------------------------------ Phase 2b: onboarding (Design §3-4, V-9)

    @app.post("/p/{key}/onboard")
    async def onboard_start(request: Request, key: str, conn: sqlite3.Connection = Depends(get_conn)):
        project = project_or_404(conn, key)

        def do(f):
            if not [s for s in hand.list_sources(conn, project["id"]) if s["enabled"]]:
                raise InputError("Add a source first: a GitHub URL or a git repository on this machine.")
            if views.running(conn):
                raise InputError("A run or onboarding is in progress. Wait for it to end.")
            latest = views.onboarding(conn, project["id"])
            app.state.spawn(["onboard", "--project", key])
            return "The onboarding agent is reading the sources.", f"/p/{key}/onboarding?after={latest['id'] if latest else 0}"

        return await act(request, f"/p/{key}?tab=sources", do)

    @app.get("/p/{key}/onboarding", response_class=HTMLResponse)
    @app.get("/p/{key}/onboarding/{oid}", response_class=HTMLResponse)
    def onboarding_view(request: Request, key: str, oid: int | None = None, after: int | None = None,
                        conn: sqlite3.Connection = Depends(get_conn)):
        project = project_or_404(conn, key)
        ob = views.onboarding(conn, project["id"], oid)
        if after is not None and oid is None and (ob is None or ob["id"] <= after):
            # The new onboarding process has not written its row yet (Part 1 §2-1).
            return render(request, "onboarding.html", {"project": project, "onboarding": None, "starting": after,
                                                       "tile": present.COLOUR_TILE[project["colour"]]}, nav="project", conn=conn)
        if ob is None:
            return render(request, "onboarding.html", {"project": project, "onboarding": None, "tile": present.COLOUR_TILE[project["colour"]]},
                          nav="project", conn=conn)
        return render(request, "onboarding.html", present.build_review(conn, project, ob, app.state.now()), nav="project", conn=conn)

    @app.post("/p/{key}/proposals/{pid}")
    async def proposal_decide(request: Request, key: str, pid: int, conn: sqlite3.Connection = Depends(get_conn)):
        project = project_or_404(conn, key)

        def do(f):
            edits = {k[5:]: v for k, v in f.items() if k.startswith("edit_")}
            return hand.decide_proposal(conn, project["id"], pid, f.get("action"), edits)

        return await act(request, f"/p/{key}/onboarding", do)

    @app.post("/p/{key}/onboarding/{oid}/accept-kind")
    async def proposal_accept_kind(request: Request, key: str, oid: int, conn: sqlite3.Connection = Depends(get_conn)):
        project = project_or_404(conn, key)

        def do(f):
            kind = str(f.get("kind", ""))
            if kind in ("GATE", "EVIDENCE", "PROJECT"):
                raise InputError("Gates, evidence and the project are accepted one by one (R-O4).")
            from ..data.db import transaction

            n = 0
            with transaction(conn):
                for p in views.proposals_with_decisions(conn, oid):
                    if p["kind"] == kind and p["open"] and p["read_by"] == "PROGRAM" and p["project_id"] == project["id"]:
                        hand.decide_proposal(conn, project["id"], p["id"], "accept")
                        n += 1
            return f"Accepted {n} {present.KIND_WORD.get(kind, kind).lower()}."

        return await act(request, f"/p/{key}/onboarding/{oid}", do)

    return app
