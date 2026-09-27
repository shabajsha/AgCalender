"""Loads config.yaml from the project folder, with the settings you changed from Telegram or the web page
(state.db, settings table; see settings.py) laid over it."""
from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def _set(cfg, dotted, value):
    *parents, last = dotted.split(".")
    node = cfg
    for part in parents:
        node = node.setdefault(part, {})
    node[last] = value


def load_config(overrides=True):
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    if overrides:
        from state import read_settings  # here: state imports nothing from config, but keep config light
        for key, value in read_settings().items():
            _set(cfg, key, value)
    return cfg
