"""Google OAuth helper: returns valid credentials, running the browser flow only when someone is there to use it."""
import os
import sys
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/tasks",
]

HERE = Path(__file__).parent
CREDENTIALS_FILE = HERE / "credentials.json"  # OAuth client (Desktop app)
TOKEN_FILE = HERE / "token.json"              # your saved login; created on first run
LOGIN_TIMEOUT_S = 300


class AuthExpired(Exception):
    """The saved login can't be used or refreshed, and there's no terminal to log in again from."""


def _save(creds):
    """Atomic write, readable only by you from the moment the file exists. The temp file is per process: at wake-up
    two jobs refresh the login at once, and a shared temp name made one of them crash (28 Sep, 09:35)."""
    tmp = TOKEN_FILE.with_name(f"{TOKEN_FILE.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(creds.to_json())
    os.replace(tmp, TOKEN_FILE)


def token_stamp():
    """When token.json last changed (None if missing). The long-running bot and web page compare this to notice a
    new login: they keep the credentials they loaded in memory, and the old ones stay revoked after `auth.py`."""
    try:
        return TOKEN_FILE.stat().st_mtime_ns
    except OSError:
        return None


def _is_interactive():
    return sys.stdin is not None and sys.stdin.isatty()


def get_credentials(interactive=None):
    """interactive=None means "only if run from a terminal". Services and timers never open a browser:
    they raise AuthExpired instead (an old version waited forever for a login nobody could see)."""
    creds = None
    if TOKEN_FILE.exists():
        # Loaded with the scopes it was granted (passing SCOPES here would overwrite them, so the check below
        # could never notice a token granted for fewer scopes, e.g. by an older version of this app).
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE))
        if not creds.has_scopes(SCOPES):
            creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save(creds)
            return creds
        except RefreshError:
            pass  # revoked or expired refresh token -> log in again

    if interactive is None:
        interactive = _is_interactive()
    if not interactive:
        raise AuthExpired("Google login missing, expired or revoked; run `python auth.py` in a terminal")
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
    creds = flow.run_local_server(port=0, timeout_seconds=LOGIN_TIMEOUT_S)
    _save(creds)
    return creds


def new_login():
    """A fresh browser login even if the saved one still works (e.g. after publishing the app, so the new token
    doesn't expire after 7 days). The old token.json is only replaced once the new login succeeded."""
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
    creds = flow.run_local_server(port=0, timeout_seconds=LOGIN_TIMEOUT_S)
    _save(creds)
    return creds


if __name__ == "__main__":
    if "--new" in sys.argv[1:]:
        new_login()
        print("New login saved to token.json. The bot and web page pick it up within a minute.")
    else:
        get_credentials(interactive=True)
        print("Authenticated OK; token saved to token.json. The bot and web page pick it up within a minute.")
