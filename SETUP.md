# Setting up your own calendar agent

This is a personal assistant that reads your university emails, asks you on Telegram before adding deadlines and
events to your Google Calendar / Tasks, suggests free time for your work and to-dos, and checks in each morning.
Everything runs on your own laptop; nothing is shared with the person who gave you this.

You need: **Ubuntu (or another Linux with systemd)**, Python 3.12, about 5 GB of free disk, a Google account, and
Telegram on your phone. An NVIDIA GPU is **not** needed (see step 2). Setup takes about 45 minutes.

## 1. Put the folder in place

Unzip it so the code is in `~/Documents/calender-agent` (the background services expect exactly that path,
spelled "calender"). Then:

```bash
cd ~/Documents/calender-agent
python3 -m venv venv
venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cp config.example.yaml config.yaml
venv/bin/python -m pytest -q          # should end with all tests passing (nothing touches your accounts)
```

## 2. The local AI model (Ollama)

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull gemma3:4b                   # ~3.3 GB; runs fine on a CPU
venv/bin/python eval_extractor.py gemma3:4b    # 13 test emails: shows accuracy and time per email
```

- **No NVIDIA GPU:** keep what `config.example.yaml` already says: `model: gemma3:4b` and `require_gpu: false`.
  Each email that needs the model takes roughly 20-60 seconds on a laptop CPU; only a few emails a day pass the
  filter, so that's fine. If it's too slow or hot, try `gemma2:2b` (and check it with `eval_extractor.py`).
- **With an NVIDIA GPU (6 GB or more):** `ollama pull gemma2:9b`, then set `model: gemma2:9b` and `require_gpu: true`.

## 3. Your university mail into Gmail

The agent reads mail from Gmail. Forward your university (Outlook) mail to your Gmail - for example with a
Power Automate flow ("When a new email arrives -> Forward an email" to your Gmail address) - and in Gmail make a
filter that puts the label **`iiith`** on those forwarded emails (e.g. "from: your university address" -> Apply label
`iiith`). If you use another label, change `gmail_query` in `config.yaml`.

## 4. Your own Google access (about 15 minutes, free)

1. Open https://console.cloud.google.com, create a project (any name).
2. **APIs & Services -> Library:** enable **Gmail API**, **Google Calendar API** and **Google Tasks API**.
3. **Google Auth Platform -> Branding:** app name (anything), your email as support and developer contact. Save.
4. **Audience:** user type **External**; under **Test users** add your own Gmail address.
5. **Clients -> Create client -> Desktop app.** Download the JSON and save it in the folder as **`credentials.json`**.
6. Log in once:
   ```bash
   venv/bin/python auth.py
   ```
   A browser opens: pick your account, "Google hasn't verified this app" -> **Continue**, allow Gmail (read-only),
   Calendar and Tasks.

While the app is in "Testing", Google makes you log in again every 7 days (you'll get a Telegram message; run
`venv/bin/python auth.py --new`). To stop that, publish the app. Google wants a home page and a privacy policy link:
1. Make a free public GitHub repository, upload the two files in `site/` (`index.html`, `privacy.html`), and turn
   on **Settings -> Pages** (branch `main`, folder `/`). Your pages are at `https://<you>.github.io/<repo>/`.
2. **Branding:** home page `https://<you>.github.io/<repo>/`, privacy policy `.../privacy.html`, authorized domain
   `<you>.github.io`. Don't upload a logo (that forces a Google review). Save.
3. **Audience -> Publish app** (don't submit for verification), then `venv/bin/python auth.py --new` once.

## 5. Your calendars and task lists

In Google Calendar create three calendars: **College**, **Planner**, **Habits**. In Google Tasks create two lists:
**College** and **DAILY TASKS**. Then:

```bash
venv/bin/python list_calendars.py
```

Copy the IDs into `config.yaml`: `calendars.college / planner / habits`, `tasklist` (College list) and
`morning.todo_tasklist` (DAILY TASKS). Under `calendar_watch.calendars` add any calendars you subscribe to (a
timetable, Moodle, contest calendars) with `ask`, so the bot asks before copying their events.

## 6. Your own Telegram bot

1. In Telegram, message **@BotFather**, send `/newbot`, give it a name. It replies with a token.
2. Run `venv/bin/python telegram_setup.py`, paste the token, then send any message to your new bot when asked.

## 7. Try it, then turn it on

```bash
venv/bin/python ingest.py --dry-run --since 3    # shows what it would ask you about; changes nothing
venv/bin/python planner.py --print               # a preview of how it would plan today

cp systemd/*.service systemd/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now calendar-approvals.service calendar-web.service calendar-ingest.timer calendar-morning.timer
venv/bin/python status.py                        # should end with "All good."
```

Send `/start` to your bot for the button bar. README.md explains everything it can do.

## Never share these files (they are your logins and data)

`credentials.json`, `token.json`, `telegram.json`, `state.db`, `backups/`, `logs/`. They are in `.gitignore`.
