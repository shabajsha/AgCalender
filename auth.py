"""Google OAuth helper: returns valid credentials, running the browser flow only when needed."""
import os
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


def _save(creds):
    TOKEN_FILE.write_text(creds.to_json())
    os.chmod(TOKEN_FILE, 0o600)


def _run_flow():
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
    return flow.run_local_server(port=0)


def get_credentials():
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

    creds = _run_flow()
    _save(creds)
    return creds


if __name__ == "__main__":
    get_credentials()
    print("Authenticated OK; token saved to token.json")
