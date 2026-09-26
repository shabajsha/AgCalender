import json

import pytest

import alerts
import auth
from conftest import FakeTelegram


class ExpiredCreds:
    valid, expired, refresh_token = False, True, "r"

    def has_scopes(self, scopes):
        return True

    def refresh(self, request):
        raise auth.RefreshError("invalid_grant")


def test_no_browser_without_a_terminal(monkeypatch, tmp_path):
    token = tmp_path / "token.json"
    token.write_text("{}")
    monkeypatch.setattr(auth, "TOKEN_FILE", token)
    monkeypatch.setattr(auth.Credentials, "from_authorized_user_file", staticmethod(lambda *a: ExpiredCreds()))

    def no_flow(*a, **k):
        raise AssertionError("must not start the browser login in a service")
    monkeypatch.setattr(auth.InstalledAppFlow, "from_client_secrets_file", staticmethod(no_flow))
    with pytest.raises(auth.AuthExpired):
        auth.get_credentials(interactive=False)


def test_token_is_saved_privately(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "TOKEN_FILE", tmp_path / "token.json")
    auth._save(type("C", (), {"to_json": lambda self: json.dumps({"x": 1})})())
    assert (tmp_path / "token.json").stat().st_mode & 0o077 == 0
    assert not (tmp_path / "token.json.tmp").exists()


def test_alerts_are_rate_limited(monkeypatch, db):
    fake = FakeTelegram()
    monkeypatch.setattr(alerts.Telegram, "from_file", staticmethod(lambda: fake))
    assert alerts.alert("gpu", "Ollama lost the GPU", db)
    assert not alerts.alert("gpu", "Ollama lost the GPU", db)     # same problem again soon: quiet
    alerts.resolved("gpu", db)
    assert alerts.alert("gpu", "Ollama lost the GPU", db)         # fixed, then broke again: tells you
    assert len(fake.sent) == 2


def test_guard_turns_expired_login_into_one_alert(monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "alert", lambda key, text, state=None: sent.append(key))

    @alerts.guard("demo")
    def main():
        raise auth.AuthExpired("expired")

    with pytest.raises(SystemExit):
        main()
    assert sent == ["auth"]
