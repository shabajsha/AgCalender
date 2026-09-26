# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A personal "calendar agent". It reads university (IIITH) emails that have been forwarded to a personal Gmail account and turns deadlines, meetings and events into Google Calendar events and Google Tasks. Everything runs locally; a local Ollama model (`gemma2:9b`) does the text extraction. Timezone is always `Asia/Kolkata`.

**Why Gmail:** the IIITH Outlook tenant blocks Microsoft Graph (it needs admin consent). `test_graph.py` is the leftover failed Graph attempt. Instead, a Power Automate flow forwards every IIITH email to Gmail, so messages arrive **from the user's own IIITH address** with an `FW:` subject prefix. The real sender and date are only in the body. Calendar invites arrive as `.ics` attachments.

## Build is phased — stop after each phase

The user wants the project built in phases and tests each one before the next starts. Finish a phase, tell the user exactly what to run and what they should see, then stop.

- **Phase 0 (done):** OAuth (`auth.py`), `config.yaml` / `config.py`, `test_gmail.py`, `list_calendars.py`.
- **Phase 1 (done):** `ingest.py`. See the pipeline notes under Architecture.
- **Phase 2 (done):** units in `systemd/` (copied to `~/.config/systemd/user/`): `calendar-ingest.timer` every 30 min, `calendar-approvals.service` always on, `calendar-digest.timer` at 07:00. `digest.py` delivers via `notifiers.CHANNELS` (desktop, telegram) and writes `logs/digest.md`.
- **Phase 3 (done):** `planner.py` + `slots.py` (pure interval math) + `ranker.py` (LLM ranks only, fallback earliest-due) + `llm.py` (shared guarded Ollama call). `calendar-planner.timer` runs `planner.py --auto` every 15 min + at login; `--auto` is a no-op before `planner.plan_after` or once `state.plans` has today. Habits in `config.yaml` `habits:` (empty by default).

Keep one file per responsibility. Update the file table in `README.md` whenever a file is added. Ask before anything destructive, including deleting the duplicate `client_secret_*.json` files or `temp/`.

## Secrets

**Never print, log, or commit** the contents of `credentials.json`, `credentials.backup.json`, `token.json`, `telegram.json`, or any `client_secret_*.json`. To inspect them, print only top-level keys, field names, or a hash. `credentials.json` is a copy of `client_secret_310871259049-huk3ttva….json` (a Desktop-app OAuth client with top-level key `installed`). All of these are in `.gitignore`.

## Commands

Always use the venv interpreter (Python 3.12):

```bash
venv/bin/pip install -r requirements.txt
venv/bin/python auth.py            # browser OAuth login; writes token.json
venv/bin/python test_gmail.py      # 5 newest messages matching gmail_query
venv/bin/python list_calendars.py  # calendar names + IDs for config.yaml
```

There is no test suite or linter. The `test_*.py` scripts are manual smoke tests that hit live Google APIs. Running one without a valid `token.json` opens a browser, which can't complete in a non-interactive session. To check code without touching the network, parse it instead:

```bash
venv/bin/python -c "import ast; [ast.parse(open(f).read(), f) for f in ['auth.py','config.py']]"
```

## Architecture

- Every script gets Google access through `auth.get_credentials()` and settings through `config.load_config()`, then calls `googleapiclient.discovery.build(...)` itself. Don't duplicate OAuth or YAML loading in new scripts.
- `auth.SCOPES` is the single scope list: `gmail.readonly`, `calendar`, `tasks`. If a new feature needs another scope, add it there. `get_credentials()` notices when the saved token lacks a scope and re-runs the browser flow; it also re-runs it when refresh fails.
- `config.yaml` holds:
  - `gmail_query`: default `label:iiith`; the fallback is `subject:FW from:<iiith address>`.
  - `timezone`.
  - `calendars.{college, planner, habits}`: Google Calendar IDs (filled in).
  - `ollama.{model, num_ctx, num_thread, keep_alive, min_free_ram_gb, timeout_s, require_gpu}`, `min_confidence`, `first_run_days`, `tasklist` (the agent's own "College" list), `approval`, `skip_senders`, `keywords`.

### Ingest pipeline (`ingest.py`)
`gmail_reader.fetch_messages` → per message: `state` checks → `ics_import.parse_ics` (if an `.ics` is attached) **or** `extractor.matches_keywords` + `extractor.extract_items` → `google_writer.create_event` / `create_task` → `state.record_item`.

- **The LLM never does date math.** It copies date/time words verbatim (`date`, `start_time`, `end_time` fields); `dates.resolve_date` / `resolve_time` turn them into values. This replaced a `start`/`end` ISO schema after gemma2:9b repeatedly resolved "next Friday" to the wrong day. The prompt gives only the received *date*, no time, because the model copied the received time into items.
- **Three dedupe layers:** Gmail message ID → content fingerprint → normalised title + start (`ingest.dedupe_key`). The fingerprint exists because each email arrives twice via two forwarders, with different forward headers (`Sent:` in IST vs UTC). `gmail_reader.split_forward_header` removes that block before hashing, and also extracts the original sender for the prompt.
- **Items are plain dicts:** `type, title, start, end, all_day, due, course, location, description, recurrence`. `start`/`end` are aware datetimes, or `date` objects when `all_day`. A deadline is stored as a 30-min event ending at `due`.
- **`--dry-run`** runs Gmail and Ollama for real but uses an in-memory copy of `state.db` (`State(dry_run=True)`). Nothing is written to Google or disk except `logs/ingest.log`.
- **Failure handling:** `LLMUnavailable` or any exception leaves the message unprocessed so the next run retries it; `last_run` only advances on an error-free run. Invalid JSON from the model counts as "no items" (at temperature 0 a retry would fail the same way).
- **Filters before the LLM:** `skip_senders` (fnmatch against `msg["from_line"]`, the raw forwarded `From:` line including the mailing list; `life@lists.iiit.ac.in` = clubs) → fingerprint → keywords. `msg["sender"]` is the real person after "On Behalf Of".
- **Resource guards in `extractor.extract_items`:** refuses when `MemAvailable` < `ollama.min_free_ram_gb`; after each call checks `ollama.ps()` `size_vram` and, if 0, unloads and sets a module flag that refuses further calls this run. Both raise `LLMUnavailable`, so the email is retried next run. Background: an OOM kill left Ollama without CUDA (fix: `sudo systemctl restart ollama`).
- **Approval flow (`approval.enabled`):** `ingest.py` does not create items. It calls `approvals.ask()`, which inserts a `pending_items` row (the item is JSON via `state.item_to_json`), then sends a Telegram card with `add:<id>` / `skip:<id>` buttons (if the send fails, the row is deleted so the next run retries). `approvals.py` is a separate long-polling process that handles taps: it checks the chat id, calls `google_writer.create_item`, and records in `created_items`. Dedupe covers both tables (`item_exists` or `pending_exists`). Only `approvals.py` may call `getUpdates`: Telegram allows one consumer at a time. `telegram_bot.TelegramError` messages never include the token.
- **Pause:** `/pause` / `/resume` / `/status` are handled in `approvals.Listener.handle_message` (owner chat only). The flag is `meta.paused_since` in `state.db`. `ingest.py` returns before touching Gmail while paused, and `last_run` doesn't advance, so resuming catches up. `/resume` starts `calendar-ingest.service` immediately. The listener now receives both `callback_query` and `message` updates.
- **Digest:** "Deadlines" = `DUE:` events on the college calendar (their *end* is the due time). A deadline counts as planned if a planner event before it has `extendedProperties.private.deadline_event_id` equal to the DUE event's id, or its title contains the deadline title. Phase 3 must set `deadline_event_id` on work blocks. Tasks from the agent's own `tasklist` are left out because they duplicate the DUE events. `build_digest` is testable offline by monkeypatching `fetch_events`, `fetch_tasks`, `get_credentials`, `build` and `State`.
- **Morning flow (`morning.py`):** `calendar-morning.timer` (every 15 min + at login) runs `--tick`. Once a day after `plan_after` it sends the check-in, then calls `finish()` on Done (listener → `systemd-run morning.py --finish`) or after `morning.wait_minutes`. `finish()` = `planner.plan_today` + `digest.build_digest(skip_planner_blocks=True)` merged into one message. State lives in `meta` keys `checkin_sent_at`, `checkin_done` and `morning_sent` (set *before* planning, to stop double runs). Plain Telegram text is a to-do only while `morning.checkin_open()` is true; otherwise `/todo`. `todos.add` stores each to-do's minutes as effort under `task:<id>`, so the planner gives it exactly that time. The old `calendar-planner.timer` / `calendar-digest.timer` are superseded (disabled on this machine).
- **Planner invariants:** never let the LLM do time math: effort, done-hours, days-left and slots are all Python. Planner events are tagged `extendedProperties.private` `{source: calendar-agent-planner, plan_date, kind: habit|work, work_key, deadline_event_id}`; only tagged events are ever deleted (`google_writer.list_blocks` filters by `privateExtendedProperty`). `work_key` is `event:<DUE event id>` or `task:<task id>`; efforts from the Telegram buttons live in `state.efforts` under the same key. Done work = past tagged blocks with that `work_key`. `DUE:` events are created `transparent` and excluded from busy time.
- **Telegram commands** run work out of process: `/check` → `systemctl --user start calendar-ingest`; `/plan` and `/clear` → `systemd-run --user` transient unit (so restarting the listener can't kill them). Button-bar labels map to commands via `approvals.LABELS`.
- **Offline checks:** `dates.py` and `extractor.validate()` are pure functions, and `ics_import.parse_ics` takes raw bytes. Test them directly with made-up input; no network or LLM needed.
