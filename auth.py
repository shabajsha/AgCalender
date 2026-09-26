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
    """Atomic write, readable only by you from the moment the file exists."""
    tmp = TOKEN_FILE.with_name(TOKEN_FILE.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(creds.to_json())
    os.replace(tmp, TOKEN_FILE)


def _is_interactive():
    return sys.stdin is not None and sys.stdin.isatty()


def get_credentials(interactive=None):
    """interactive=None means "only if run from a terminal". Services and timers never open a browser:
    they raise AuthExpired instead (an old version waited forever for a login nobody could see)."""
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
        # A token granted for fewer scopes (e.g. older version of this app) must be redone.
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


if __name__ == "__main__":
    get_credentials(interactive=True)
    print("Authenticated OK; token saved to token.json")
