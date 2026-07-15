"""Logging setup — UNBOUNDED append FileHandler, INFO default (F12). SPEC §7.

This is the bot's own drive (hundreds of GB free). No rotation, no cap — the log
grows until the operator prunes it by hand. Deep history is the priority.
"""
import os
import logging

from .config import LOG_DIR


def setup_logging(debug=False, log_dir=None):
    log_dir = log_dir or LOG_DIR
    os.makedirs(log_dir, exist_ok=True)
    handler = logging.FileHandler(os.path.join(log_dir, "deepfield.log"))
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    root.handlers = [h for h in root.handlers if isinstance(h, logging.FileHandler) is False]
    root.addHandler(handler)
    return handler
