# Calendar agent

Reads IIITH emails forwarded to Gmail and turns deadlines, meetings and events into
Google Calendar events and Google Tasks. Runs locally; uses Ollama (gemma2:9b) for extraction.

**Never share or commit `credentials.json`, `credentials.backup.json` or `token.json`.**
They are listed in `.gitignore`.

## Files

| File | Purpose |
|---|---|
| `auth.py` | `get_credentials()`: loads/refreshes `token.json`, runs the browser login only when needed. Run it directly to log in. |
| `config.py` | `load_config()`: reads `config.yaml`. |
| `config.yaml` | Gmail query, timezone, calendar IDs (college / planner / habits), Ollama model, keyword list, confidence threshold. |
| `ingest.py` | Main job: new emails -> calendar events / tasks. Flags `--dry-run`, `--since DAYS`. |
| `gmail_reader.py` | Fetches matching emails; strips the forward header and mailing-list footer; pulls `.ics` attachments; fingerprints emails. |
| `ics_import.py` | Turns `.ics` invites into events (no LLM). Skips cancellations. |
| `extractor.py` | Keyword pre-filter, the LLM prompt, and validation of what the model returns. |
| `dates.py` | Turns date/time words ("next Friday", "12th Oct", "11:59 PM") into real dates. All date math lives here, not in the LLM. |
| `google_writer.py` | Creates Google Calendar events and Google Tasks. |
| `state.py` | SQLite `state.db`: processed emails, created items, last run time. |
| `approvals.py` | Keep running: acts on your Telegram Add / Skip taps, creates approved items, expires old ones, offers to block senders you always skip. |
| `telegram_bot.py` | Minimal Telegram Bot API client (send with buttons, edit, long-poll). |
| `telegram_setup.py` | One-time: asks for your bot token (hidden), finds your chat, writes `telegram.json`. |
| `morning.py` | Morning routine: check-in question on Telegram, then plan + one combined morning message. `--tick` (timer), `--finish`, `--start` (test). |
| `todos.py` | Parses to-dos you send ("Lab report 2h") and adds/undoes them in DAILY TASKS, due today. |
| `planner.py` | Plans the rest of today: habits first, then work blocks for deadlines and dated tasks. `--auto` (timer), `--print`, `--clear [--date]`. |
| `slots.py` | Free-time arithmetic (subtract busy time, sleep, gaps; place blocks). Pure Python, no LLM. |
| `ranker.py` | Asks the model only to *order* the open work; falls back to earliest-due-first. |
| `llm.py` | The single guarded Ollama call (RAM check, GPU check, timeout) used by `extractor.py` and `ranker.py`. |
| `digest.py` | Morning digest: today's events, deadlines in the next 7 days, deadlines with no Planner work block, your own tasks due, items waiting for Add / Skip. `--print` to preview. |
| `notifiers.py` | Pluggable delivery (`desktop` = notify-send, `telegram`). Add a function to `CHANNELS` for a new channel. |
| `systemd/` | User units: `calendar-ingest.timer` (every 30 min), `calendar-approvals.service` (always on), `calendar-morning.timer` (check-in, plan, morning message). `calendar-digest.timer` / `calendar-planner.timer` are the older separate versions, now disabled. |
| `eval_extractor.py` | Benchmarks Ollama models on 13 made-up emails with known answers: accuracy, speed, GPU fit, CPU temperature. `python eval_extractor.py gemma2:9b gemma3:4b` |
| `list_calendars.py` | Prints your calendars' names and IDs, for filling in `config.yaml`. |
| `test_gmail.py` | Prints the 5 newest emails matching `gmail_query`, to check the query works. |
| `requirements.txt` | Python dependencies. |
| `credentials.json` | Google OAuth client (Desktop app). Secret. |
| `token.json` | Your saved Google login, created by `auth.py`. Secret. |
| `logs/ingest.log` | Log of every ingest run. |
| `telegram.json` | Bot token + chat id, created by `telegram_setup.py`. Secret. |
| `logs/approvals.log` | Log of your Add / Skip decisions. |
| `logs/digest.md` | The latest digest. |

## Setup (Phase 0)

```bash
cd ~/Documents/calender-agent
source venv/bin/activate
pip install -r requirements.txt
python auth.py            # browser login; click through the "unverified app" warning
python test_gmail.py      # should list recent "FW: ..." subjects
python list_calendars.py  # copy IDs into config.yaml
```

To re-login from scratch (e.g. after changing scopes), delete `token.json` and run `python auth.py`.

## Ingestion (Phase 1)

```bash
python ingest.py --dry-run --since 7   # show what would be created from the last 7 days; writes nothing
python ingest.py --since 7             # actually create them
python ingest.py                       # normal run: emails since the last run (first run: 3 days)
```

How an email is handled:

1. **Already processed** (by Gmail message ID) -> skipped.
2. **Sender on `skip_senders`** (matched against the forwarded `From:` line, so `life@lists.iiit.ac.in`
   catches every club/fest email) -> skipped.
3. **Duplicate forward** -> skipped. Each IIITH email currently arrives twice with different forward
   headers; after removing the header the copies have the same fingerprint.
4. **Has an `.ics` invite** -> imported directly as an event.
5. **No keyword** from `config.yaml` in subject/body -> skipped (no LLM call).
6. Otherwise **gemma2:9b** lists the items, copying date/time words exactly as written; `dates.py`
   turns them into dates. Items that are in the past, over a year away, have no specific day, or have
   confidence below `min_confidence` are dropped.

Deadlines become a 30-minute `DUE: ...` event ending at the due time on the college calendar **plus**
a Google Task. Meetings and events become college-calendar events. An item with the same title and
start as one already created is never created again. Every event created by the agent is tagged
(`extendedProperties.private.source = calendar-agent`) and links back to the email.

Weekday names resolve to the **nearest upcoming** day: "next Friday" written on a Wednesday means the
Friday two days later, not the one after (an early reminder is safer than a missed deadline).

If Ollama isn't running, the affected emails are retried on the next run. To start over, delete
`state.db` (events already created in Google stay).

## Model and heat

`config.yaml` → `ollama:` sets the model and how hard it runs. `num_ctx: 2048` matters most: at the default 4096,
gemma2:9b (6.3 GB) doesn't fit in the 6 GB GPU and about a third of it runs on the CPU. `num_thread: 4` limits the
CPU share, and `keep_alive: 30s` unloads the model soon after a run.

Benchmark on 26 Sep 2026 (13 test emails): gemma2:9b 13/13, 5.3 s per email; gemma3:4b 13/13 plus 1 invented item,
1.7 s, but on real mail it filed registration deadlines as plain events (so no Task); qwen2.5:3b 9/13 with 6 invented
items. gemma2:9b stays the default. Rerun `eval_extractor.py` before switching models.

Optional, to stop Ollama competing with your own work (`sudo systemctl edit ollama`):

```ini
[Service]
Nice=15
CPUQuota=400%
```

## Safety and resource guards

- The prompt tells the model the email is untrusted data; at most 5 items per email are kept; titles are stripped of
  links, line breaks and control characters. The model's output never runs as code, Gmail access is read-only, and
  the agent only ever *adds* events and tasks.
- `min_free_ram_gb`: if less RAM is free, LLM calls are postponed to the next run (on 26 Sep a full swap plus the
  browser led the kernel to OOM-kill Ollama).
- `require_gpu`: after an OOM kill Ollama restarted without detecting the NVIDIA GPU and ran the model fully on the
  CPU (6 GB RAM). If that happens the agent unloads the model, logs `sudo systemctl restart ollama`, and stops calling
  the model for that run.

## Approving items on your phone (Telegram)

With `approval.enabled: true` nothing is added until you say so. Each new item arrives in Telegram as a card with
**Add** and **Skip**. Add creates the College-calendar event (plus a task in the **College** task list for
deadlines). Skip records your choice. Items you don't answer expire once their date passes. After
`learn_after_skips` skips and no adds from the same sender, the bot offers **Always skip**. Blocked senders are
stored in `state.db` (`sender_prefs`) and skipped before the LLM runs.

One-time setup:

1. In Telegram, message **@BotFather** -> `/newbot` -> choose a name -> copy the token.
2. `python telegram_setup.py` -> paste the token (hidden) -> press **Start** on your new bot. You get a test message.
3. `python approvals.py` in a terminal and leave it running (Phase 2 makes it a service).
4. `python ingest.py` -> cards appear on your phone.

Only the item title, date, sender address and email subject go to Telegram, never the email body.

## Running automatically (Phase 2)

Install and start (stop any `python approvals.py` you started by hand first: Telegram allows only one listener):

```bash
mkdir -p ~/.config/systemd/user
cp ~/Documents/calender-agent/systemd/calendar-* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now calendar-approvals.service calendar-ingest.timer calendar-digest.timer
```

Check on it:

```bash
systemctl --user status calendar-approvals          # should say "active (running)"
systemctl --user list-timers 'calendar-*'           # next mail check and next digest
journalctl --user -u calendar-ingest -n 30          # output of the last mail checks
systemctl --user start calendar-ingest              # run a mail check right now
systemctl --user start calendar-digest              # send a digest right now
```

The timers use `Persistent=true`: a check or digest missed while the laptop was off or asleep runs as soon as it's
back. Services run while you're logged in. After editing a unit in `systemd/`, copy it again and `daemon-reload`.
To stop everything: `systemctl --user disable --now calendar-approvals.service calendar-ingest.timer calendar-digest.timer`.

## Telegram commands

Send these to the bot (they're also in its menu button). Only your own chat is obeyed.

| Command / button | What it does |
|---|---|
| `/todo Lab report 2h` | Adds to-dos (one per line, optional time; default 30 min) to DAILY TASKS, due today, with an Undo button. |
| `/check`, **Check mail now** | Runs a mail check immediately instead of waiting for the next 30-minute run. |
| `/plan`, **Plan rest of today** | Re-plans from now: habits, then work blocks (replaces today's not-yet-started blocks). |
| `/clear` | Removes today's planner-made blocks (also the **Clear today's plan** button under each plan). |
| `/pause` | Stops reading mail. Buttons on existing cards still work, and the digest still arrives, noting the pause. |
| `/resume` | Starts reading again and checks right away, including all mail that arrived while paused. |
| `/status` | Paused or on, time of the last mail check, number of cards waiting for you. |

The button bar at the bottom of the chat appears after `/start` or `/help`. A command sent while the laptop was
off waits in Telegram and runs when it's back; the bot says it was delayed.

The pause is stored in `state.db` (`meta.paused_since`), so it survives restarts. After changing `approvals.py`,
run `systemctl --user restart calendar-approvals`.

## Planner (Phase 3)

Runs **once a day, the first time the laptop is on after `plan_after` (06:45)**, whether that's boot, wake from sleep
or login (the timer checks every 15 min, and it's a no-op once today is planned). If that's later than 07:15 the
summary says the laptop was off. `/plan` re-plans the rest of the day at any time.

1. Not-yet-started planner blocks from an earlier plan today are removed (finished or ongoing ones stay).
2. **Free time** = now until midnight, minus every timed event on your calendars (with `gap_minutes` around each)
   and the `sleep` window. All-day events, "free" events and `DUE:` markers don't block time.
3. **Habits** from `config.yaml` go first, each inside its own window, on its days.
4. **Work**: every deadline (`DUE:` event) and every dated task on your own task lists gets an effort. That's the
   hours you picked on its Telegram card (2-30 h), else `default_effort_hours` (3) or `task_effort_hours` (1). Work
   already done in earlier planner blocks is subtracted, and the rest is spread evenly over the days left, so today
   gets its share. The **model only ranks** these (it sees days-left and hours computed in Python); if it's
   unavailable, the order is earliest due first. Blocks (max `block_minutes`, min `min_block_minutes`) are placed
   earliest-first inside `work_window`, up to `max_work_hours_per_day`.
5. Summary to Telegram (with **Clear today's plan**) and the desktop. An automatic run with nothing to place stays
   quiet.

Every planner event carries `extendedProperties.private.source = calendar-agent-planner` and `plan_date`, so only
its own blocks are ever moved or deleted. Wipe any day with `python planner.py --clear --date YYYY-MM-DD`.

Install the timer (once):

```bash
cp ~/Documents/calender-agent/systemd/calendar-planner.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now calendar-planner.timer
```

## Morning check-in

The first time the laptop is on after `planner.plan_after` (06:45), the bot asks **"What do you want to get done
today?"**. Reply one to-do per line, optionally with a time (`Lab report 2h`, `Call bank 15m`, `Revise OS 1h30m`;
no time = `todo_default_minutes`). Each reply is added to **DAILY TASKS** due today, with its time stored as effort,
and confirmed with **Undo**. Tap **Done** (or **Nothing today**). After `morning.wait_minutes` (45) without an
answer it goes ahead anyway. Then the day is planned and **one morning message** arrives: today's events, your plan,
deadlines, and tasks. This replaces the separate 07:00 digest.

- Plain messages count as to-dos only while the check-in is open; at any other time use `/todo`.
- Telegram keeps messages for 24 h, so `/todo` works while the laptop is off and is added when it's back.
- Adding tasks straight in the Google Tasks app (due today) also works; the planner reads them, at `task_effort_hours` each.
- Test the morning now with `python morning.py --start`. `--finish` skips straight to plan + message.

Timers (replacing `calendar-planner.timer` and `calendar-digest.timer`):

```bash
cp ~/Documents/calender-agent/systemd/calendar-morning.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user disable --now calendar-planner.timer calendar-digest.timer
systemctl --user enable --now calendar-morning.timer
```
