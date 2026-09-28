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
| `ingest.py` | Main job: new emails -> calendar events / tasks. Flags `--dry-run`, `--since DAYS`, `--message ID` (read one skipped email anyway). |
| `gmail_reader.py` | Fetches matching emails; strips the forward header and mailing-list footer; pulls `.ics` attachments; fingerprints emails. |
| `ics_import.py` | Turns `.ics` invites into events (no LLM). Skips cancellations, replies ("Accepted: ...") and changed single occurrences; keeps the invite UID. |
| `extractor.py` | Keyword pre-filter, the LLM prompt, and validation of what the model returns. |
| `dates.py` | Turns date/time words ("next Friday", "12th Oct", "11:59 PM") into real dates. All date math lives here, not in the LLM. |
| `google_writer.py` | Creates Google Calendar events and Google Tasks. |
| `state.py` | SQLite `state.db`: processed (and skipped) emails, created items, cards, watched calendar events and their occurrence copies, settings flags. Backed up daily to `backups/` (7 kept). |
| `approvals.py` | Keep running: acts on your Telegram Add / Skip taps, creates approved items, expires old ones, offers to block senders you always skip. |
| `telegram_bot.py` | Minimal Telegram Bot API client (send with buttons, edit, long-poll). |
| `telegram_setup.py` | One-time: asks for your bot token (hidden), finds your chat, writes `telegram.json`. |
| `morning.py` | Morning routine: check-in question on Telegram, then plan + one combined morning message. `--tick` (timer), `--finish`, `--start` (test). |
| `todos.py` | Parses to-dos you send ("Lab report 2h") and adds/undoes them in DAILY TASKS, due today. |
| `planner.py` | Works out today's work (deadlines, exam prep, to-dos, dated tasks) and your free time. Default: send free slots to pick from (`--suggest-new` for a new to-do only); `--print` previews an automatic plan; `--place` / `--auto` place blocks without asking (old behaviour); `--clear [--date]`. |
| `actions.py` | Checked plan changes shared by the bot, typed commands and the web page: move / book / skip a block, block out busy time, finish or drop a deadline. A new time must be free. |
| `nlcommands.py` | Change your plan by typing it ("move SDET study to 7pm"); rules first, the local model for other phrasings; always confirmed. |
| `habits.py` | Habits from Telegram (/habits): guided set-up, pause/delete, streaks; offered as free slots on their days. |
| `deadlines.py` | /deadlines: Done / Effort / Date / Not doing for each upcoming deadline. |
| `changes.py` | An email or invite that moves or cancels something you have: "Changed?" / "Cancelled?" cards instead of duplicates. |
| `daytimes.py` | One day's own wake-up / bedtime ("just woke up", "sleeping at 2am", "up at 9 tomorrow"), laid over the usual times by `config.load_config()`. |
| `settings.py` | Settings you can change from Telegram (/settings) or the web page, validated; stored in state.db over config.yaml. |
| `web/` | The web page (`web/app.py`, Flask, 127.0.0.1:8765): drag your blocks on a day timeline, plus tasks, deadlines, to-dos, habits, settings. `calendar-web.service`. |
| `slotpicker.py` | You choose when: one Telegram message per task with free slots as buttons, booking, "Did you finish?" after each slot, one reminder, the evening check, `/exams`. |
| `slots.py` | Free-time arithmetic (subtract busy time, sleep, gaps; place blocks). Pure Python, no LLM. |
| `ranker.py` | Asks the model only to *order* the open work; falls back to earliest-due-first. |
| `llm.py` | The single guarded Ollama call (RAM check, GPU check, timeout) used by `extractor.py` and `ranker.py`. |
| `digest.py` | Morning digest: today's events, deadlines in the next 7 days, deadlines with no Planner work block, your own tasks due, items waiting for Add / Skip. `--print` to preview. |
| `notifiers.py` | Pluggable delivery (`desktop` = notify-send, `telegram`). Add a function to `CHANNELS` for a new channel. |
| `systemd/` | User units: `calendar-ingest.timer` (every 30 min), `calendar-approvals.service` (always on), `calendar-web.service` (the web page), `calendar-morning.timer` (check-in, plan, morning message). `calendar-digest.timer` / `calendar-planner.timer` are the older separate versions, now disabled. |
| `calwatch.py` | Watches your other calendars (timetable, contests, Outlook, Moodle): Track / Ignore cards, copies tracked events into College and keeps them in step with the original. |
| `alerts.py` | Tells you on Telegram (else desktop) when something needs you: expired Google login, Ollama lost the GPU, repeated mail-check errors, an email given up on. Once per problem per 6 h. |
| `logsetup.py` | One rotating log file per script in `logs/` (1 MB, 3 kept). |
| `gtasks.py` | Paged Google Tasks helpers shared by the planner and the digest. |
| `tests/` | pytest suite (no network: a fixture blocks Telegram, Google (httplib2), Ollama (httpx), systemd and the real login). `test_fixes.py` / `test_calfixes.py` hold one regression test per bug from the 26 Sep audit. Run `venv/bin/python -m pytest`. |
| `eval_extractor.py` | Benchmarks Ollama models on 13 made-up emails with known answers: accuracy, speed, GPU fit, CPU temperature. `python eval_extractor.py gemma2:9b gemma3:4b` |
| `status.py` | Is it running? Services, timers, last runs, login, GPU, recent problems, web page - one look. |
| `list_calendars.py` | Prints your calendars' names and IDs, for filling in `config.yaml`. |
| `test_gmail.py` | Prints the 5 newest emails matching `gmail_query`, to check the query works. |
| `requirements.txt` | Python dependencies. |
| `credentials.json` | Google OAuth client (Desktop app). Secret. |
| `token.json` | Your saved Google login, created by `auth.py`. Secret. |
| `logs/ingest.log` | Log of every ingest run. |
| `telegram.json` | Bot token + chat id, created by `telegram_setup.py`. Secret. |
| `logs/approvals.log` | Log of your Add / Skip decisions. |
| `logs/digest.md` | The latest digest. |
| `backups/` | Daily copies of `state.db` (made by the first morning tick of each day; 7 kept). Gitignored. |

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
4. **Subject on `skip_subjects`** (machine notices such as "Presentation shared with you") -> skipped.
5. **Has an `.ics` invite** -> imported directly as an event.
6. **Pre-filter** (saves GPU time): the email goes to the model only if it has a strong word from `keywords`
   anywhere (plurals and hyphen/space variants count: "Exams", "Mid-sem", "Quizzes"), or a `weak_keywords` word
   with a concrete day in the same sentence ("fill the feedback form by Friday"), or "till / until / by / before
   <day>" ("active till 28th September"). Everything else is skipped but remembered: **`/skipped`** lists those
   emails with **Read** buttons that run the model on one anyway.
7. Otherwise **gemma2:9b** lists the items, copying date/time words exactly as written; `dates.py`
   turns them into dates. Items that are in the past, over a year away, have no specific day, or have
   confidence below `min_confidence` are dropped.

Deadlines become a 30-minute `DUE: ...` event ending at the due time on the college calendar **plus**
a Google Task. Meetings and events become college-calendar events. An item with the same title and
start as one already created is never created again. Every event created by the agent is tagged
(`extendedProperties.private.source = calendar-agent`) and links back to the email.

Weekday names resolve to the **nearest upcoming** day: "next Friday" written on a Wednesday means the
Friday two days later, not the one after (an early reminder is safer than a missed deadline). Times are read only
from real clock times ("11:59 PM", "1700 hrs", "2-4 PM", "10 AM to 12 PM"); anything vaguer ("at 5", "EOD") means
the end of the day for a deadline, never midnight. A submission window is due at its end.

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

- The prompt tells the model the email is untrusted data; at most 5 items per email (and per invite) are kept;
  titles are stripped of links, line breaks and control characters. The model's output never runs as code and
  Gmail access is read-only. The agent adds events and tasks only after you tap Add; it deletes only its own
  planner blocks, the to-dos you Undo, and an event whose task failed to save.
- `min_free_ram_gb`: if less RAM is free, LLM calls are postponed to the next run (on 26 Sep a full swap plus the
  browser led the kernel to OOM-kill Ollama).
- `require_gpu`: if the GPU isn't available to Ollama (power-saver mode, or Ollama restarted without CUDA after an
  OOM kill), the model would run on the CPU: slow, hot, 6 GB of RAM. The agent unloads it, alerts you once, and
  makes **no model calls for 6 hours**: emails wait (they're read later, nothing is lost) and plans use due-date
  order. It checks whether the model is already loaded on the CPU before running anything. After fixing it
  (`sudo systemctl restart ollama`, or leaving power-saver mode), tap **Check mail now** to try the GPU again.
- A model answer that takes longer than `timeout_s` counts against that email (given up after 3 tries, with an
  alert) instead of costing 5 minutes of GPU on every run. Answers are capped at 700 tokens.
- **Being offline isn't an error.** A check that can't reach Google or Telegram (Wi-Fi not back after wake-up) logs
  one line and tries again at the next run. You're only told if there's been no connection for 6 hours.

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
| `/calendars` | Lists your calendars; tap one to cycle ask / copy / show / ignore. |
| `/review` | The calendar review right now: what's new, changed or cancelled on your calendars. |
| `/check`, **Check mail now** | Runs a mail check immediately instead of waiting for the next 30-minute run. |
| `/plan`, **Plan rest of today** | Sends each task that still needs time today, with free slots to pick from. Nothing is booked until you tap a time. |
| `/exams` | Exams in the next 2 weeks and how much preparation each gets; tap to change (or none). |
| `/today`, **Today** | Today's events, your blocks with how they went, tasks still without a time. |
| `/deadlines`, **Deadlines** | Each upcoming deadline with Done / Effort / Date (+1 day, +2 days, +1 week, or type one) / Not doing. |
| `/habits`, **Habits** | Your habits with streaks; New habit walks you through name, length, days and time of day. |
| `/settings`, **Settings** | Work hours, sleep, daily limit, block length, morning time, reminders, heads-up, evening check, exam prep... |
| *typing a change* | "move SDET study to 7pm", "leetcode not today", "busy 2-5pm", "make midsem prep 10 hours", "add gym at 6pm for 1h", "swap SMAI and SDET A2", "push leetcode by 30 min". Always asks Yes / Cancel first. |
| `/clear` | Removes today's planner-made blocks that haven't started (also the **Clear today's plan** button). Blocks already worked stay: later plans count them as done. |
| `/pause` | Stops reading mail. Your other calendars are still checked, buttons on existing cards still work, and the digest still arrives, noting the pause. |
| `/skipped` | Emails the pre-filter skipped (no deadline words), newest first, with **Read** buttons to run the model on one anyway. |
| `/resume` | Starts reading again and checks right away, including all mail that arrived while paused. |
| `/status` | Health: mail reading on/paused, last mail and calendar checks, today's morning, Google login, GPU, emails waiting for the model, cards waiting, skipped emails, problems in the last 24 h, last backup. |

If a tap or command fails (Google unreachable, login expired), the bot says so instead of leaving the card
unchanged. The button bar at the bottom of the chat appears after `/start` or `/help`. A command sent while the laptop was
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
   gets its share (a deadline at 09:00 or midnight leaves no time on its own day, so that day isn't counted). A
   to-do shorter than `min_block_minutes` gets a block of its own length ("Call bank 15m" -> 15 min). Blocks that
   have started count in full. The **model only ranks** these (it sees days-left and hours computed in Python); if it's
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

## You choose when (slot picking)

After the check-in (or on **Plan rest of today**) each task that needs time today gets its own message:

```
Study SDET MidSem
2 h to do today (due Sun 27 Sep 23:59). Pick a time for the first 1 h 30 min:
[11:00-12:30] [14:00-15:30] [19:00-20:30]
[More times] [Not today]
```

- The slots are real free time (all your calendars, gaps, sleep), spread over morning / afternoon / evening, and
  never after the task is due. Tapping one books it on the Planner calendar; the other messages update so that time
  isn't offered twice. **Nothing is booked until you tap.** *Not today* moves a to-do to tomorrow.
- A task you haven't given a time gets **one reminder** `morning.remind_after_minutes` (2 h) later, with fresh slots.
- When a booked slot ends: **"Did you finish ...?" Done / Partly / Not done.** Done ticks the to-do off in Google Tasks
  once its time is all done; Partly or Not done offers new slots. Your answers count (done 100%, partly 50%, not
  done 0), so work that didn't happen is planned again. No questions during the sleep window; they come after it.
- At `morning.evening_check` (21:30): the day's summary (done, not done, not answered, no time picked) with
  **Move to tomorrow** for unfinished to-dos.
- **Exams** (midsem, endsem, quiz, exam, test, viva on the College calendar, from email, the timetable or Moodle) get
  preparation time before them: `planner.exam_prep_hours` (quiz 2 h, midsem 8 h, endsem 12 h, other 6 h), spread over
  the days left. Change one with `/exams` or the buttons on its card. The morning message lists exams coming up.
- `/todo` after the morning sends free slots for the new to-do straight away.
- The daily limit (`max_work_hours_per_day`) is a warning, never a silent drop.
- **Moving or deleting a block in the Google Calendar app is followed.** The bot re-reads its blocks every 5 minutes
  (and right before a heads-up, a "Did you finish?", `/today`, a typed change or the web page), so it uses the time a
  block has now. A deleted block gets no more questions; a block you answered *Not done* and then moved to a later
  time counts as booked again.
- Ask "what's scheduled today?" (or tomorrow, or a weekday) for that day's events and blocks.

## Typing changes, habits, deadlines, settings

- **Type what you want changed.** Common phrasings are understood by fixed rules (this works with the GPU off); for
  anything else the local model says what you meant, but never works out times. The task is found by its title,
  times are worked out in Python, and the new time must be free. You always get **Yes / Cancel** first; if the time
  isn't free you get the nearest free times as buttons. Only the agent's own blocks are ever moved.
- **Heads-up** `morning.heads_up_minutes` (5) before each booked block: *Next at 15:00: ...* with **Start**,
  **Push 30 min** (moved if that's free, else other times offered) and **Skip** (removed; planned again later).
- **Habits** (`/habits` -> New habit): name, length, days, time of day. On its days a habit gets free slots inside
  its window like any task (on the Habits calendar); *Did you finish?* afterwards keeps a **streak**.
- **Deadlines** (`/deadlines`): Done ticks the task off and removes its upcoming work; Date moves the DUE event and
  task; Not doing deletes them after you confirm. After tapping **Add** on an email card you can **Undo** for 10 min.
- **Emails that move or cancel something you already have** ("SDET midsem postponed to Friday", "quiz cancelled") ask
  *Changed? Was ... Now ...* (Move it / Add as new / Ignore) or *Cancelled?* (Remove it / Keep it) instead of adding a
  duplicate. Titles must match closely and numbers exactly ("Assignment 2" is never "Assignment 3"). Updated or
  cancelled invites are matched by their UID.
- **Settings** (`/settings`, or the web page): each change is checked and stored in `state.db`; every script uses it
  from its next run. Reset goes back to `config.yaml`.

## Woke up late? Sleeping late?

Your usual times are in `/settings` (Sleep, Morning starts at, Work hours). For **one day**, just tell the bot:

| You type | What happens |
|---|---|
| `just woke up` / `woke up at 10` | Today starts then: the morning check-in comes now (or, if it already ran without you, fresh free slots from now). No pings before that time. |
| `up at 9 tomorrow` | Tomorrow's check-in waits until 9, and nothing pings you before. |
| `sleeping at 2am` / `bed at 11 tonight` / `early night` | Tonight's bedtime: free slots run until 30 min before it, and no heads-ups or questions after it. |

The evening check also has **Bed 23:30 / 00:30 / 01:30 / 02:30** and **Up 07:00 ... 10:00** buttons. Each reply has
**Use the usual time** to undo it, and the next day goes back to your usual times by itself. Bare hours are read
sensibly: "up at 9" is morning, "bed at 11" is evening, "bed at 1" is after midnight.

## Web page

`calendar-web.service` serves **http://localhost:8765** on the laptop: a day timeline (your events in grey, work in
blue, habits in green; **drag a block to move it, drag its bottom edge to resize**; a time that isn't free snaps back
with the nearest free times), "Did you finish?", tasks that need a time (tap a slot), to-dos, deadlines, habits,
settings, and Check mail / Plan rest of today / Pause.

Safety: it only listens on 127.0.0.1; requests need an allowed Host/Origin (localhost + `web.allowed_hosts`) and the
page's CSRF token; with `web.tailscale_user` set only your Tailscale login gets in.

**From your phone (once, needs sudo):**
1. Install Tailscale on the laptop (`curl -fsSL https://tailscale.com/install.sh | sh`) and run `sudo tailscale up`.
2. Install the Tailscale app on your phone and sign in with the same account.
3. On the laptop: `sudo tailscale serve --bg 8765`. It prints `https://<laptop>.<tailnet>.ts.net`.
4. Put that name in `config.yaml` -> `web.allowed_hosts` (and your login in `web.tailscale_user`), then
   `systemctl --user restart calendar-web`. Open the https address on your phone.
Never use `tailscale funnel`: that would put the page on the public internet. The page only works while the laptop
is on.

## Morning check-in

The first time the laptop is on after `planner.plan_after` (06:45), the bot asks **"What do you want to get done
today?"**. Reply one to-do per line, optionally with a time (`Lab report 2h`, `Call bank 15m`, `Revise OS 1h30m`;
no time = `todo_default_minutes`). Each reply is added to **DAILY TASKS** due today, with its time stored as effort,
and confirmed with **Undo**. Tap **Done** (or **Nothing today**). After `morning.wait_minutes` (45) without an
answer it goes ahead anyway. Then **one morning message** arrives (today's events, your tasks for today, exams,
deadlines and tasks due), followed by one message per task with free slots to pick from (see above). This replaces
the separate 07:00 digest.

- Plain messages count as to-dos only while the check-in is open; at any other time use `/todo`. Typing
  "done" or "nothing" works like the buttons. The buttons are dated, so an old one never starts a new day.
- The day counts as done only when the message has reached **Telegram**. If it couldn't (no Wi-Fi yet), the same
  message is retried every 15 minutes (the desktop popup isn't repeated). If planning fails, the message still comes,
  with the reason in place of the plan.
- If the laptop is first on after `morning.latest_checkin` (18:00), there's no "Good morning" question: it just
  plans what's left of the day.
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

## Is it running?

- **On the laptop:** `venv/bin/python status.py` - services and timers, last mail and calendar check, today's
  morning, Google login, GPU, emails waiting, problems in the last 24 h, web page. Ends with "All good." or points at
  the lines marked `!!`.
- **On your phone:** send `/status` to the bot (the same health, plus Pause / Resume).
- **By hand:** `systemctl --user status calendar-approvals calendar-web`, `systemctl --user list-timers 'calendar-*'`,
  `journalctl --user -u calendar-ingest -n 30`, and the files in `logs/`.
- If something breaks you get a Telegram alert (desktop popup if Telegram can't be reached), at most once per problem
  every 6 hours. Being offline is not an alert unless it lasts 6 hours.

## While you're gaming

- **The model never fights your game for the GPU.** Before loading it, the agent checks the GPU (`nvidia-smi`): if
  other programs use more than `ollama.gpu_busy_vram_mb` (1.5 GB) of GPU memory, or the GPU is more than
  `gpu_busy_percent` (60%) busy, the model isn't loaded. The email just waits for a later mail check (every 30 min) -
  no alert, no error. `/status` shows how many are waiting. Low RAM is treated the same way.
- Everything else is light: the mail and calendar checks every 30 minutes take a few seconds of CPU at low priority
  (`Nice=10`), and the Telegram bot and web page sleep until something happens.
- **No desktop popups over a fullscreen window** (game or video); Telegram still gets everything, on your phone.
- Optional, so Ollama can never take many CPU cores even when it does run: `sudo systemctl edit ollama` and add
  `[Service]` / `Nice=15` / `CPUQuota=400%`.

## Development

```bash
venv/bin/pip install -r requirements.txt -r requirements-dev.txt
venv/bin/python -m pytest            # ~260 tests, a few seconds, never touches your real accounts
git log --oneline                    # history; each phase is one or more commits
```

If the Google login ever expires (you'll get a Telegram alert), run `venv/bin/python auth.py` in a terminal.
Background jobs never open a browser themselves; they alert and wait.

## Your other calendars

With every mail check the agent also looks at your other Google calendars (`calendar_watch` in `config.yaml`):

| Policy | What happens to a new event |
|---|---|
| `ask` | A Telegram card: **Track** copies it into College, **Ignore** drops it. Repeating events are asked about once per series; a course's two weekly slots are one question; more than 5 new at once become one summary card (Track all / Ignore all / One by one). Until you answer, the event still blocks planning time. |
| `copy` | Copied into College without asking (the **Always track calendar** button). |
| `show` | Counted and shown in the digest, never copied (your own calendar, holidays). |
| `ignore` | Ignored completely (the **Never ask this calendar** button). |

- **Daily review** (`daily_review: true`): the calendars are still checked every 30 minutes, but what's found
  waits for **one message a day**, sent just before the morning check-in (or when the laptop first comes on).
  It has numbered **New** / **Changed** / **Cancelled** / **Copied automatically** sections, with buttons per
  item and **Track all new / Ignore all new**. Items update in place as you tap them; anything undecided
  carries over to the next day's review. Events starting within `urgent_hours` (24) are asked about right
  away instead. `/review` sends the review now.
- **Changes are asked about again.** A tracked copy follows the original immediately (moved: the copy moves;
  cancelled: the copy is removed), and the review asks *Still want it? Keep / Remove*. A tracked **repeating**
  event is copied occurrence by occurrence for the next `days_ahead` days, so a single cancelled or moved class is
  reflected exactly; one moved class isn't reported as a change to the whole series.
- **A feed hiccup can't delete anything.** An event has to be missing on two checks in a row before its copy is
  removed, and if it comes back it's restored (tracked again). Copies you tracked keep following their original
  even after you switch that calendar to *Never ask*, *show* or *ignore*. An event you ignored
  that changes asks *Track now / Keep ignoring*. A deadline made from a calendar event moves with it.
- Events that look like deadlines (e.g. Moodle's "Assignment 2 is due") also get **It's a deadline**, which makes a DUE event and task, with effort buttons.
- An event that already came in as an email invite isn't asked about again.
- `/calendars` shows every calendar's policy and changes it with a tap.
- **Nothing is final by accident.** Calendar-wide buttons (Always track calendar, Never ask this calendar) ask
  "Yes / Cancel" first, and afterwards show **Undo**. Track all, Ignore all and It's a deadline also get **Undo**;
  a single Track or Ignore has Untrack / Track instead. Undo removes anything the tap created (copies, the
  deadline and its task) and brings the cards back, but leaves alone anything you decided differently since.
  Tapping a button twice never makes a second copy or deadline, and Ignore on a card only affects that card.

The Moodle calendar's name in Google Calendar is its export URL, which contains a private token. The agent only
ever shows `courses.iiit.ac.in`, never the URL.
