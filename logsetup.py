"""Shared logging setup: a rotating file per script in logs/, plus the console."""
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path(__file__).parent / "logs"
NOISY = ("googleapiclient", "urllib3", "httpx")


def setup(name):
    """logs/<name>.log, rotated at 1 MB (3 old files kept)."""
    LOG_DIR.mkdir(exist_ok=True)
    file_handler = RotatingFileHandler(LOG_DIR / f"{name}.log", maxBytes=1_000_000, backupCount=3)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers[:] = [file_handler, console]
    for noisy in NOISY:
        logging.getLogger(noisy).setLevel(logging.WARNING)
