"""Delivery channels for messages to you. Add a channel by writing a function and registering it in CHANNELS.

Each channel takes (title, text, buttons). Buttons are [(label, callback_data), ...]; channels that can't show
them ignore them. config.yaml -> digest.channels picks which ones are used.
"""
import logging
import subprocess

from telegram_bot import Telegram

log = logging.getLogger(__name__)
DESKTOP_MAX_CHARS = 900  # desktop popups get unreadable beyond this; the full text is in logs/digest.md


def fullscreen_active():
    """True if the focused window is fullscreen (a game, a video): desktop popups wait, Telegram still gets it."""
    try:
        root = subprocess.run(["xprop", "-root", "_NET_ACTIVE_WINDOW"], capture_output=True, text=True, timeout=3).stdout
        window = root.strip().split()[-1]
        if not window.startswith("0x") or int(window, 16) == 0:
            return False
        props = subprocess.run(["xprop", "-id", window, "_NET_WM_STATE"], capture_output=True, text=True, timeout=3).stdout
        return "_NET_WM_STATE_FULLSCREEN" in props
    except Exception:  # noqa: BLE001 - no X display / xprop: assume not fullscreen
        return False


def desktop(title, text, buttons=None):
    if fullscreen_active():
        log.info("a fullscreen app is open: desktop popup %r skipped (Telegram still gets it)", title)
        return
    body = text if len(text) <= DESKTOP_MAX_CHARS else text[:DESKTOP_MAX_CHARS] + "\n... (full digest in logs/digest.md)"
    subprocess.run(["notify-send", "--app-name=Calendar agent", "--expire-time=0", title, body], check=True, timeout=15)


def telegram(title, text, buttons=None):
    Telegram.from_file().send(f"{title}\n\n{text}", buttons=buttons)


CHANNELS = {"desktop": desktop, "telegram": telegram}


def deliver(title, text, channels, buttons=None):
    """Sends to every configured channel; one failing channel doesn't stop the others. Returns channels that worked."""
    delivered = []
    for name in channels:
        send = CHANNELS.get(name)
        if send is None:
            log.error("unknown delivery channel %r (known: %s)", name, ", ".join(CHANNELS))
            continue
        try:
            send(title, text, buttons)
            delivered.append(name)
        except Exception as e:
            log.error("delivery via %s failed: %s", name, e)
    return delivered
