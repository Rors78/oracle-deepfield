"""Logging setup — RotatingFileHandler, 5 MB x 3, INFO default (F12). SPEC §7.

No unbounded DEBUG ledgers on a small disk.
"""
import os
import logging
from logging.handlers import RotatingFileHandler

from .config import LOG_DIR

MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 3


def setup_logging(debug=False, log_dir=None):
    log_dir = log_dir or LOG_DIR
    os.makedirs(log_dir, exist_ok=True)
    handler = RotatingFileHandler(
        os.path.join(log_dir, "deepfield.log"), maxBytes=MAX_BYTES, backupCount=BACKUPS)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    root.handlers = [h for h in root.handlers if not isinstance(h, RotatingFileHandler)]
    root.addHandler(handler)
    return handler
