"""Makes a zip of the code to give to a friend - without your logins, data or calendar IDs.

    venv/bin/python make_share.py            # writes ~/calendar-agent-share.zip

Only files tracked by git go in, minus your own config.yaml (your friend gets config.example.yaml). Afterwards the zip is checked: no secret files, and none of the calendar / task-list IDs from your
config.yaml appear anywhere in it. Your friend then follows SETUP.md.
"""
import re
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).parent
OUT = Path.home() / "calendar-agent-share.zip"
LEAVE_OUT = ("config.yaml",)
NEVER = ("credentials", "token.json", "telegram.json", "client_secret", "state.db", "backups/", "logs/")


def your_ids():
    """The IDs in your config.yaml (calendars, task lists) that must not be in the zip."""
    text = (HERE / "config.yaml").read_text()
    return set(re.findall(r"[\w.-]{16,}@(?:group|import)\.calendar\.google\.com", text)) | \
        set(re.findall(r"tasklist: '([^']{10,})'", text))


def main():
    files = subprocess.run(["git", "ls-files"], cwd=HERE, capture_output=True, text=True, check=True).stdout.split()
    files = [f for f in files if f not in LEAVE_OUT and not f.startswith(LEAVE_OUT)]
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(HERE / f, f"calender-agent/{f}")
    problems = []
    ids = your_ids()
    with zipfile.ZipFile(OUT) as z:
        for name in z.namelist():
            if any(n in name for n in NEVER):
                problems.append(f"secret file included: {name}")
            data = z.read(name).decode("utf-8", errors="ignore")
            problems += [f"{name} contains one of your IDs" for i in ids if i in data]
    if problems:
        OUT.unlink()
        sys.exit("Not made - " + "; ".join(problems))
    print(f"Made {OUT} ({len(files)} files, checked: no logins, no data, none of your calendar IDs).")
    print("Send it to your friend; they unzip it into ~/Documents and follow SETUP.md.")


if __name__ == "__main__":
    main()
