"""Pages, actions and the guard (Design §6, §9; Part 2 §7; Part 3 §11-6)."""

import re

from pmd.config import Config
from pmd.data import hand
from pmd.web import guard
from pmd.web.app import safe_back, with_query

from .conftest import ORIGIN, post


def add_project(client, key="ALPHA", name="Alpha launch"):
    return post(client, "/projects", {"key": key, "name": name, "scope_note": "Ship the beta."})


# ------------------------------------------------------------------ guard

def test_get_pages_carry_security_headers(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"


def test_post_without_own_header_is_refused(client):
    r = client.post("/projects", data={"key": "ALPHA", "name": "x"}, headers={"Origin": ORIGIN})
    assert r.status_code == 403


def test_post_from_another_origin_is_refused(client):
    r = client.post("/projects", data={"key": "ALPHA", "name": "x"},
                    headers={"Origin": "https://evil.example", "X-PMD-Request": "1"})
    assert r.status_code == 403
    r = client.post("/projects", data={"key": "ALPHA", "name": "x"}, headers={"X-PMD-Request": "1"})
    assert r.status_code == 403  # no Origin at all


def test_unknown_host_is_refused(client):
    assert client.get("/", headers={"Host": "pmd.evil.example"}).status_code == 400


def test_tailscale_identity():
    cfg = Config(allowed_login="owner@example.com", tailnet_name="laptop.tail1234.ts.net")
    h = {"host": "laptop.tail1234.ts.net"}
    assert guard.check(cfg, "GET", h) is None
    assert guard.check(cfg, "GET", {**h, "tailscale-user-login": "Owner@Example.com"}) is None
    assert guard.check(cfg, "GET", {**h, "tailscale-user-login": "someone@else.com"})[0] == 403
    assert guard.check(cfg, "GET", {"host": "127.0.0.1:8787"}) is None
    assert guard.check(cfg, "GET", {"host": "[::1]:8787"})[0] == 400  # ::1 not in allowed names
    post_ok = {**h, "origin": "https://laptop.tail1234.ts.net", "x-pmd-request": "1"}
    assert guard.check(cfg, "POST", post_ok) is None


def test_redirect_targets_stay_on_site():
    assert safe_back("//evil.example/x", "/") == "/"
    assert safe_back("https://evil.example", "/") == "/"
    assert safe_back("/\\evil", "/") == "/"
    assert safe_back("/p/A?tab=gates#g-1", "/") == "/p/A?tab=gates#g-1"
    assert with_query("/p/A?tab=gates&msg=old#g-1", msg="New") == "/p/A?tab=gates&msg=New#g-1"


# ------------------------------------------------------------------ pages and actions

def test_empty_today(client):
    r = client.get("/")
    assert "0 projects." in r.text and "Add your first project." in r.text
    assert "Nothing needs your call." in r.text


def test_project_flow_shows_days_left_on_today(client, conn):
    r = add_project(client)
    assert r.status_code == 200 and "Project added" in r.text
    assert str(r.url).startswith(f"{ORIGIN}/p/ALPHA?tab=gates")

    r = post(client, "/p/ALPHA/gates", {"key": "G1", "name": "Beta to pilot users", "date": "2026-11-27"})
    assert "Gate added" in r.text
    gid = hand.list_gates(conn, hand.get_project(conn, "ALPHA")["id"])[0]["id"]

    today = client.get("/").text
    assert "1 project." in today
    assert 'data-count="3"' in today and "working days to<br>Beta to pilot users" in today
    assert "You have not set a value yet." in today

    post(client, f"/p/ALPHA/gates/{gid}/items", {"text": "Pilot users can sign in"})
    post(client, f"/p/ALPHA/gates/{gid}/items", {"text": "Support runbook written"})
    iid = hand.list_items(conn, [gid])[gid][0]["id"]
    r = post(client, f"/p/ALPHA/items/{iid}/state", {"state": "MET", "evidence_url": "https://example.com/x"})
    assert "Marked met." in r.text
    assert "1 of 2 met" in client.get("/").text

    r = post(client, f"/p/ALPHA/gates/{gid}/health", {"value": "AT_RISK", "note": "pilot list late", "back": "/"})
    assert str(r.url).startswith(f"{ORIGIN}/?") or str(r.url) == f"{ORIGIN}/"
    today = client.get("/").text
    assert "t-butter" in today and "At risk" in today and "pilot list late" in today


def test_status_is_never_colour_alone(client, conn):
    add_project(client)
    post(client, "/p/ALPHA/gates", {"key": "G1", "name": "Beta", "date": "2026-11-27"})
    html = client.get("/").text + client.get("/p/ALPHA?tab=gates").text
    for sticker in re.findall(r'<span class="sticker[^"]*">(.*?)</span>', html, re.S):
        icon, word = re.match(r'<i aria-hidden="true">(.*?)</i>(.*)', sticker.strip(), re.S).groups()
        assert icon.strip() and word.strip(), sticker


def test_past_gate_shows_days_past(client):
    add_project(client)
    post(client, "/p/ALPHA/gates", {"key": "G0", "name": "Kickoff", "date": "2026-11-20"})
    today = client.get("/").text
    assert 'data-count="2"' in today and "working days past" in today and "record the outcome" in today


def test_input_error_comes_back_as_message(client):
    add_project(client)
    r = add_project(client)
    assert r.status_code == 200 and "exists already" in r.text
    r = post(client, "/p/ALPHA/gates", {"key": "G1", "name": "Beta", "date": "soon"})
    assert "must be a date" in r.text


def test_tasks_tab_shows_late_tasks(client):
    add_project(client)
    post(client, "/p/ALPHA/gates", {"key": "G1", "name": "Beta", "date": "2026-12-04"})
    r = post(client, "/p/ALPHA/tasks", {"key": "1.023", "title": "Sign-in library", "owner": "Rani",
                                         "gate_key": "G1", "finish": "2026-11-23", "status": "IN_PROGRESS"})
    assert "Task added." in r.text and "1d late" in r.text and "1 late" in r.text


def test_archive_hides_project_and_settings_restores_it(client):
    add_project(client)
    post(client, "/p/ALPHA/state", {"state": "ARCHIVED"})
    assert "Alpha launch" not in client.get("/").text
    settings = client.get("/settings").text
    assert "Alpha launch" in settings
    post(client, "/p/ALPHA/state", {"state": "ACTIVE", "back": "/settings"})
    assert "Alpha launch" in client.get("/").text


def test_delete_project_needs_key(client):
    add_project(client)
    r = post(client, "/p/ALPHA/delete", {"confirm_key": "nope"})
    assert "Type the project key ALPHA" in r.text
    r = post(client, "/p/ALPHA/delete", {"confirm_key": "ALPHA"})
    assert "Project ALPHA deleted." in r.text
    assert client.get("/p/ALPHA").status_code == 404


def test_cannot_touch_another_projects_gate(client, conn):
    add_project(client, "AA", "First")
    add_project(client, "BB", "Second")
    post(client, "/p/AA/gates", {"key": "G1", "name": "Beta", "date": "2026-12-04"})
    gid = hand.list_gates(conn, hand.get_project(conn, "AA")["id"])[0]["id"]
    r = post(client, f"/p/BB/gates/{gid}/health", {"value": "LATE"})
    assert "no longer exists" in r.text
    assert hand.latest_confirmations(conn, [gid]) == {}


def test_time_zone_setting(client):
    r = post(client, "/settings/time-zone", {"time_zone": "Mars/Olympus"})
    assert "is not a time zone name" in r.text
    r = post(client, "/settings/time-zone", {"time_zone": "Asia/Jakarta"})
    assert "Time zone saved." in r.text and "Tuesday 24 November 2026" in r.text


def test_static_font_is_served_locally(client):
    css = client.get("/static/pmd.css").text
    assert "fonts/bricolage-grotesque-latin-wght.woff2" in css and "googleapis" not in css
    assert client.get("/static/fonts/bricolage-grotesque-latin-wght.woff2").status_code == 200
    assert "googleapis" not in client.get("/").text


def test_not_found_page(client):
    r = client.get("/p/NOPE")
    assert r.status_code == 404 and "Nothing here." in r.text
