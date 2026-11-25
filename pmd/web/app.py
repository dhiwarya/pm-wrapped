"""The web application (Detailed Design Part 1 §2-1, §4-1; Part 2 §7).

Every changing request is a POST from PMD's own pages. Each action writes hand
tables only, then redirects back with a short message.
"""

from __future__ import annotations

import sqlite3
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
from ..data import db, hand
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
)


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


def create_app(config: Config, *, now: Callable[[], datetime] | None = None) -> FastAPI:
    app = FastAPI(title="PM Dash", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config = config
    app.state.now = now or (lambda: datetime.now(UTC))
    db.open_store(config.db_path).close()

    app.add_middleware(GuardMiddleware, config=config)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = db.connect(config.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def render(request: Request, name: str, ctx: dict, *, nav: str = "", status: int = 200) -> HTMLResponse:
        ctx = {
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
        return render(request, "today.html", present.build_today(conn, app.state.now()), nav="today")

    @app.get("/health", response_class=PlainTextResponse)
    def health():
        return "ok\nlast run: none (Phase 1 has no run)\n"

    @app.get("/projects/new", response_class=HTMLResponse)
    def new_project_view(request: Request):
        return render(request, "project_new.html", {}, nav="new")

    @app.get("/p/{key}", response_class=HTMLResponse)
    def project_view(request: Request, key: str, tab: str = "overview", conn: sqlite3.Connection = Depends(get_conn)):
        project = project_or_404(conn, key)
        if tab not in ("overview", "gates", "tasks", "holidays"):
            tab = "overview"
        ctx = present.build_project(conn, project, app.state.now())
        return render(request, "project.html", {**ctx, "tab": tab}, nav="project")

    @app.get("/settings", response_class=HTMLResponse)
    def settings_view(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        ctx = {
            "time_zone": hand.get_setting(conn, "time_zone", ""),
            "host_zone": datetime.now().astimezone().tzname(),
            "holidays": hand.list_holidays(conn),
            "people": hand.list_people(conn),
            "archived": hand.list_projects(conn, ("ARCHIVED",)),
            "today": present.full_date(present.today(conn, app.state.now())),
        }
        return render(request, "settings.html", ctx, nav="settings")

    # ------------------------------------------------------------ projects

    @app.post("/projects")
    async def create_project(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        def do(f):
            hand.create_project(conn, f.get("key"), f.get("name"), f.get("description"), f.get("scope_note"))
            key = str(f.get("key", "")).strip().upper()
            return "Project added. Now add its first gate.", f"/p/{key}?tab=gates"

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
            hand.get_gate(conn, project["id"], gid)
            hand.add_confirmation(conn, gid, f.get("value"), f.get("note"))
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

    return app
