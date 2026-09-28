"""Is the calendar agent running? One look, on the laptop (no Telegram needed):

    venv/bin/python status.py

Shows the background services and timers, when mail and your calendars were last checked, today's morning
routine, the Google login, the GPU, emails waiting for the model, problems in the last 24 hours and the web page.
Read-only: it changes nothing.
"""
import subprocess
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import llm
from config import load_config
from state import State

SERVICES = [("calendar-approvals.service", "Telegram bot (taps, commands, heads-up)"),
            ("calendar-web.service", "web page on http://localhost:8765")]
TIMERS = [("calendar-ingest.timer", "mail + calendar check, every 30 min"),
          ("calendar-morning.timer", "morning routine, done-checks, evening check, every 15 min")]


def _systemctl(*args):
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return "?"


def main():
    cfg = load_config()
    tz = ZoneInfo(cfg["timezone"])
    now = datetime.now(tz)
    state = State()
    ok = True

    def stamp(iso):
        return datetime.fromisoformat(iso).astimezone(tz).strftime("%a %d %b %H:%M") if iso else "never"

    print(f"Calendar agent - {now:%a %d %b %H:%M}\n")
    print("Background")
    for unit, what in SERVICES:
        active = _systemctl("is-active", unit)
        ok &= active == "active"
        print(f"  {'OK ' if active == 'active' else '!! '} {what}: {active}")
    for unit, what in TIMERS:
        active = _systemctl("is-active", unit)
        nxt = _systemctl("show", unit, "-p", "NextElapseUSecRealtime", "--value")
        ok &= active == "active"
        next_at = nxt.split()[2][:5] if len(nxt.split()) >= 3 else ""  # "Sun 2026-09-27 16:15:00 IST" -> "16:15"
        print(f"  {'OK ' if active == 'active' else '!! '} {what}: {active}" + (f", next at {next_at}" if next_at else ""))

    last = state.get_last_run()
    mail_age = now - last.astimezone(tz) if last else None
    print("\nLast runs")
    print(f"  {'OK ' if mail_age and mail_age < timedelta(hours=2) else '!! '} mail check: "
          f"{last.astimezone(tz):%a %d %b %H:%M}" if last else "  !!  mail check: never")
    print(f"      calendar check: {stamp(state.get_meta('last_calendar_scan'))}")
    print(f"      morning routine: {'done today' if state.get_meta('morning_sent') == now.date().isoformat() else 'not yet today'}")
    print(f"      state.db backup: {stamp(state.get_meta('last_backup'))}")

    print("\nHealth")
    print("  " + ("!!  Google login EXPIRED: run venv/bin/python auth.py" if state.get_meta("alert:auth") else "OK  Google login"))
    lost = llm.gpu_lost_since(state)
    busy = llm.gpu_busy(cfg["ollama"])
    if not cfg["ollama"].get("require_gpu", True):
        print(f"  OK  model {cfg['ollama']['model']} runs on the CPU (require_gpu: false)")
    elif lost:
        print(f"  !!  GPU unavailable to Ollama since {lost.astimezone(tz):%H:%M} (send /check after fixing)")
    elif busy:
        print(f"  --  GPU busy right now ({busy}): emails wait until it's free")
    else:
        print("  OK  GPU free for the model")
    waiting = int(state.get_meta("llm_waiting") or 0)
    if waiting:
        print(f"  --  {waiting} email(s) waiting for the model")
    if state.paused_since():
        print(f"  --  mail reading PAUSED since {state.paused_since().astimezone(tz):%a %H:%M} (/resume in Telegram)")
    problems = [(k[len("alert:"):], datetime.fromisoformat(v)) for k, v in
                state.db.execute("SELECT key, value FROM meta WHERE key LIKE 'alert:%' AND value != ''")]
    recent = [(k, t) for k, t in problems if now - t.astimezone(tz) < timedelta(days=1)]
    print("  " + ("OK  no problems in the last 24 h" if not recent else
                  "!!  problems in the last 24 h: " + ", ".join(f"{k} ({t.astimezone(tz):%H:%M})" for k, t in recent)))
    try:
        with urllib.request.urlopen("http://127.0.0.1:8765/", timeout=5) as resp:
            web_ok = resp.status == 200
    except OSError:
        web_ok = False
    print("  " + ("OK  web page answers" if web_ok else "!!  web page not answering"))

    print("\nLogs: logs/ingest.log, logs/approvals.log, logs/morning.log, logs/planner.log"
          "\nLive: journalctl --user -u calendar-approvals -f")
    print("\nAll good." if ok and not recent and web_ok else "\nSomething needs a look (lines marked !!).")


if __name__ == "__main__":
    main()
