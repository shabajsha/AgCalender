"""The web page: a day timeline where you drag your blocks to move them (or drag the bottom edge to resize), plus
tasks that need a time, "Did you finish?", deadlines, to-dos, habits, settings and controls.

    venv/bin/python web/app.py        # normally the calendar-web systemd service; open http://localhost:8765

Security: it listens on 127.0.0.1 only. From your phone, use Tailscale: `sudo tailscale serve --bg 8765` (your own
devices only - never `tailscale funnel`, which would put it on the internet). Requests must come with an allowed
Host/Origin (localhost, plus `web.allowed_hosts` - your *.ts.net name), every change needs the page's CSRF token
(new each start), and with `web.tailscale_user` set, only that Tailscale login gets in.
Every change goes through the same checks as the bot (actions.py): a new time must be free.
"""
import secrets
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from flask import Flask, abort, jsonify, render_template, request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import actions  # noqa: E402
import calwatch  # noqa: E402
import deadlines  # noqa: E402
import google_writer  # noqa: E402
import habits  # noqa: E402
import planner  # noqa: E402
import settings  # noqa: E402
import slotpicker  # noqa: E402
import todos  # noqa: E402
from config import load_config  # noqa: E402
from state import State  # noqa: E402

PORT = 8765
DAY_START_H = 6


def _services():
    from googleapiclient.discovery import build

    from auth import get_credentials
    creds = get_credentials(interactive=False)
    return build("calendar", "v3", credentials=creds), build("tasks", "v1", credentials=creds)


def create_app(services=_services, state_factory=State, config=load_config):
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["CSRF"] = secrets.token_urlsafe(32)
    google = {}

    def ctx():
        """cfg, state, calendar, tasks and a listener-like object for the helpers shared with the bot."""
        import auth
        if not google or google.get("token") != auth.token_stamp():  # first request, or you logged in again
            google["token"] = auth.token_stamp()
            google["cal"], google["tasks"] = services()
        cfg = config()
        tz = ZoneInfo(cfg["timezone"])
        state = state_factory()
        lis = SimpleNamespace(cfg=cfg, state=state, calendar=google["cal"], tasks=google["tasks"], tg=None)
        return SimpleNamespace(cfg=cfg, state=state, cal=google["cal"], tasks=google["tasks"], tz=tz,
                               now=datetime.now(tz), lis=lis)

    @app.before_request
    def guard():
        cfg = config()
        web = cfg.get("web", {}) or {}
        allowed = {f"localhost:{PORT}", f"127.0.0.1:{PORT}", "localhost", "127.0.0.1"} | set(web.get("allowed_hosts") or [])
        if request.host not in allowed:
            abort(403, "unknown host")  # stops DNS-rebinding tricks from other web pages
        user = web.get("tailscale_user")
        local = request.host.split(":")[0] in ("localhost", "127.0.0.1")
        if user and not local and request.headers.get("Tailscale-User-Login") != user:
            abort(403, "not your Tailscale login")  # via Tailscale the login must be yours (tagged devices have none)
        if request.method != "GET":
            origin = request.headers.get("Origin")
            if origin and origin.split("://", 1)[-1] not in allowed:
                abort(403, "cross-site request")
        if (request.method != "GET" or request.path.startswith("/api/")) and \
                request.headers.get("X-CSRF-Token") != app.config["CSRF"]:
            abort(403, "missing or wrong CSRF token")  # also for /api/state: another site can't make it read your calendars

    @app.after_request
    def headers(resp):
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = ("default-src 'self'; style-src 'self'; script-src 'self'; "
                                                   "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/")
    def index():
        return render_template("index.html", csrf=app.config["CSRF"])

    @app.get("/api/state")
    def api_state():
        c = ctx()
        day = date.fromisoformat(request.args.get("day") or c.now.date().isoformat())
        return jsonify(page_state(c, day))

    def body():
        return request.get_json(silent=True) or {}

    def when(value, c):
        t = datetime.fromisoformat(value)
        return t.astimezone(c.tz) if t.tzinfo else t.replace(tzinfo=c.tz)

    @app.post("/api/blocks/<int:block_id>/move")
    def move(block_id):
        c = ctx()
        block = c.state.block(block_id) or abort(404)
        ok, message, alternatives = actions.move_block(c.cfg, c.state, c.cal, block, when(body()["start"], c),
                                                       when(body()["end"], c), c.now)
        return jsonify(message=message, alternatives=[[s.isoformat(), e.isoformat()] for s, e in alternatives]), (200 if ok else 409)

    @app.post("/api/blocks/<int:block_id>/answer")
    def answer(block_id):
        c = ctx()
        block = c.state.block(block_id) or abort(404)
        status = {"done": "done", "partly": "partly", "notdone": "notdone"}.get(body().get("answer")) or abort(400)
        if block["status"] not in ("booked", "asked"):
            return jsonify(message="Already answered."), 409
        c.state.block_set(block_id, status=status)
        ticked = status == "done" and slotpicker._maybe_complete(c.lis, block)
        return jsonify(message=f"{block['title']}: {status.replace('notdone', 'not done')}." + (" Ticked off." if ticked else ""))

    @app.post("/api/blocks/<int:block_id>/skip")
    def skip(block_id):
        c = ctx()
        block = c.state.block(block_id) or abort(404)
        if not actions.skip_block(c.cfg, c.state, c.cal, block):
            return jsonify(message=f"{block['title']} was already {actions._answered(block)}."), 409
        return jsonify(message=f"Skipped {block['title']}.")

    @app.post("/api/items/<int:item_id>/book")
    def book(item_id):
        c = ctx()
        item = c.state.plan_item(item_id) or abort(404)
        start = when(body()["start"], c)
        ok, message, alternatives = actions.book(c.cfg, c.state, c.cal, item, start,
                                                 start + timedelta(minutes=int(body()["minutes"])), c.now)
        return jsonify(message=message, alternatives=[[s.isoformat(), e.isoformat()] for s, e in alternatives]), (200 if ok else 409)

    @app.post("/api/items/<int:item_id>/not-today")
    def not_today(item_id):
        c = ctx()
        item = c.state.plan_item(item_id) or abort(404)
        c.state.plan_item_set(item_id, status="skipped")
        moved = slotpicker._move_task(c.lis, item, c.now.date() + timedelta(days=1))
        return jsonify(message=f"{item['title']}: not today." + (" Moved to tomorrow." if moved else ""))

    @app.post("/api/todos")
    def add_todos():
        c = ctx()
        mc = c.cfg["morning"]
        parsed = todos.parse(body().get("text", ""), mc["todo_default_minutes"])
        if not parsed:
            return jsonify(message="No to-do found. One per line, e.g. 'Lab report 2h'."), 400
        _, added = todos.add(c.tasks, mc["todo_tasklist"], c.state, c.now.date(), parsed)
        slotpicker.prepare(c.cfg, c.state, c.now, c.cal, c.tasks, rank=False)
        return jsonify(message="Added: " + ", ".join(f"{t} ({todos.fmt_minutes(m)})" for t, m in added))

    @app.post("/api/deadlines/<event_id>/<op>")
    def deadline(event_id, op):
        c = ctx()
        ev, title = deadlines._title(c.cal, c.cfg, event_id)
        if ev is None:
            abort(404)
        if op == "done":
            actions.finish_deadline(c.cfg, c.state, c.cal, c.tasks, event_id, c.now)
            return jsonify(message=f"Done: {title}.")
        if op == "effort":
            hours = float(body().get("hours", 0))
            if not 0 <= hours <= 200:
                abort(400)
            c.state.set_effort(f"event:{event_id}", hours)
            return jsonify(message=f"{title}: {hours:g} h of work.")
        if op == "move":
            due = deadlines._due(ev, c.tz) + timedelta(days=int(body().get("days", 1)))
            google_writer.move_deadline(c.cal, c.tasks, c.cfg, event_id, c.state.task_for_event(event_id), due)
            return jsonify(message=f"{title}: now due {due:%a %d %b %H:%M}.")
        abort(404)

    @app.post("/api/habits")
    def new_habit():
        c = ctx()
        b = body()
        name, minutes, days = str(b.get("name", "")).strip()[:60], int(b.get("minutes", 0)), b.get("days") or []
        start, end = str(b.get("window_start", "")), str(b.get("window_end", ""))
        valid_days = [d for d in habits.DAY_KEYS if d in days]
        try:
            ok = name and 5 <= minutes <= 600 and valid_days and datetime.strptime(start, "%H:%M") < datetime.strptime(end, "%H:%M")
        except ValueError:
            ok = False
        if not ok:
            return jsonify(message="Need a name, 5-600 minutes, at least one day and a time window like 17:00-21:00."), 400
        c.state.habit_add(name, minutes, ",".join(valid_days), start, end)
        return jsonify(message=f"Saved: {name}.")

    @app.post("/api/habits/<int:habit_id>/<op>")
    def habit_op(habit_id, op):
        c = ctx()
        habit = c.state.habit(habit_id) or abort(404)
        if op == "toggle":
            c.state.habit_set(habit_id, active=0 if habit["active"] else 1)
            return jsonify(message=f"{habit['name']}: {'paused' if habit['active'] else 'resumed'}.")
        if op == "delete":
            c.state.habit_delete(habit_id)
            return jsonify(message=f"Deleted {habit['name']}.")
        abort(404)

    @app.post("/api/settings/<int:index>")
    def change_setting(index):
        c = ctx()
        if index >= len(settings.SETTINGS):
            abort(404)
        s = settings.SETTINGS[index]
        if body().get("reset"):
            c.state.clear_setting(s.key)
            return jsonify(message=f"{s.label}: back to config.yaml.")
        try:
            value = settings.parse(s, str(body().get("value", ""))) if s.kind not in ("bool", "channels") else \
                settings.normalise(s, s.choices[int(body().get("choice", 0))])
            settings.set_value(c.state, s, value)
        except (ValueError, IndexError) as e:
            return jsonify(message=f"{s.label}: {e}"), 400
        return jsonify(message=f"{s.label}: {settings.show(s, value)}.")

    @app.post("/api/actions/<op>")
    def control(op):
        import approvals
        c = ctx()
        if op == "check":
            ok = approvals.Listener._start_ingest_now()
            return jsonify(message="Checking mail now." if ok else "Couldn't start the mail check.")
        if op == "plan":
            ok = approvals.Listener._run_planner()
            return jsonify(message="Free slots for the rest of today are on their way to Telegram." if ok is True
                           else "Already planning." if ok == "busy" else "Couldn't start the planner.")
        if op in ("pause", "resume"):
            c.state.set_paused(op == "pause")
            if op == "resume":
                approvals.Listener._start_ingest_now()
            return jsonify(message="Mail reading paused." if op == "pause" else "Mail reading resumed.")
        abort(404)

    @app.errorhandler(403)
    def forbidden(e):
        return jsonify(message=str(e.description)), 403

    return app


def _iso(value, tz):
    return value.astimezone(tz).isoformat() if isinstance(value, datetime) else value.isoformat()


def events_on(c, day):
    """The day's events on your calendars (read-only on the page); the agent's own blocks come from state."""
    tz, out = c.tz, []
    start = datetime.combine(day, datetime.min.time(), tz)
    statuses, own = c.state.watch_statuses(), {b["event_id"] for b in c.state.blocks()}
    for cal_entry in calwatch.load_calendars(c.cal, c.cfg, c.state):
        if not cal_entry["selected"] or cal_entry["policy"] == "ignore":
            continue
        for ev in google_writer.list_events(c.cal, cal_entry["id"], start, start + timedelta(days=1)):
            if ev["id"] in own or not calwatch.shown_today(cal_entry["policy"], statuses.get((cal_entry["id"], calwatch.event_key(ev)))):
                continue
            s = ev["start"].get("dateTime") or ev["start"].get("date")
            e = ev["end"].get("dateTime") or ev["end"].get("date")
            if "T" in s:  # some feeds give UTC ("...Z"): show everything in your timezone
                s, e = datetime.fromisoformat(s).astimezone(tz).isoformat(), datetime.fromisoformat(e).astimezone(tz).isoformat()
            out.append({"title": ev.get("summary", "(no title)"), "start": s, "end": e, "all_day": "T" not in s,
                        "calendar": cal_entry["label"],
                        "planner": ev.get("extendedProperties", {}).get("private", {}).get("source") == google_writer.PLANNER_TAG})
    return out


def page_state(c, day):
    tz, today = c.tz, c.now.date()
    actions.sync_if_stale(c.cfg, c.state, c.cal, c.now)  # blocks you moved in the Calendar app show where they are now
    blocks = [{"id": b["id"], "title": b["title"], "start": _iso(datetime.fromisoformat(b["start"]), tz),
               "end": _iso(datetime.fromisoformat(b["end"]), tz), "status": b["status"],
               "kind": "habit" if (b["work_key"] or "").startswith("habit:") else "busy" if b["status"] == "busy" else "work"}
              for b in c.state.blocks() if datetime.fromisoformat(b["start"]).astimezone(tz).date() == day
              and b["status"] != "cleared"]
    waiting = []
    if day == today:
        _, free, _ = slotpicker.prepare(c.cfg, c.state, c.now, c.cal, c.tasks, rank=False)
        for row in c.state.plan_items(today, statuses=["open"]):
            if row["minutes"] <= 0:
                continue
            chunk, opts = slotpicker._pick(free, row, c.cfg["planner"], c.now)
            waiting.append({"id": row["id"], "title": row["title"], "minutes": row["minutes"], "kind": row["kind"],
                            "due": row["due"], "chunk": chunk, "options": [[s.isoformat(), e.isoformat()] for s, e in opts]})
    dls = [{"id": ev["id"], "title": ev["summary"][len(google_writer.DUE_PREFIX):], "due": _iso(deadlines._due(ev, tz), tz),
            "effort": planner._effort(c.state, f"event:{ev['id']}", c.cfg["planner"]["default_effort_hours"])}
           for ev in deadlines.upcoming(c.cfg, c.cal, c.now)]
    hab = [{"id": h["id"], "name": h["name"], "describe": habits.describe(h), "active": bool(h["active"]),
            "streak": habits.streak(c.state, h, today)} for h in c.state.habits()]
    sets = [{"index": i, "label": s.label, "kind": s.kind, "value": settings.show(s, settings.current(c.cfg, s.key)),
             "choices": [settings.show(s, settings.normalise(s, ch)) for ch in s.choices]} for i, s in enumerate(settings.SETTINGS)]
    paused = c.state.paused_since()
    return {"day": day.isoformat(), "today": today.isoformat(), "now": c.now.isoformat(),
            "window": c.cfg["planner"]["work_window"], "sleep": c.cfg["planner"]["sleep"], "day_start_hour": DAY_START_H,
            "events": events_on(c, day), "blocks": blocks, "waiting": waiting, "deadlines": dls, "habits": hab,
            "settings": sets, "paused": bool(paused),
            "asked": [b for b in blocks if b["status"] in ("asked",) or (b["status"] == "booked" and b["end"] < c.now.isoformat())]}


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=PORT, threaded=False)
