"""Logging setup — UNBOUNDED append FileHandler, INFO default (F12). SPEC §7.

This is the bot's own drive (hundreds of GB free). No rotation, no cap — the log
grows until the operator prunes it by hand. Deep history is the priority.
"""
import os
import sys
import logging

from .config import LOG_DIR


def _harden_console_encoding():
    """Make the inherited console handler survive non-ASCII log records.

    Log messages carry box-drawing, arrows and em-dashes throughout. Windows
    opens stdout/stderr as cp1252, which cannot encode any of them, so the
    StreamHandler raised UnicodeEncodeError mid-emit. logging swallows that and
    prints the traceback, so it never killed the bot — it just replaced the line
    you wanted with a stack trace, on the console the operator is watching.

    Reconfigure to UTF-8 where the stream supports it; where it does not, fall
    back to backslashreplace so an unencodable glyph degrades to an escape
    rather than losing the whole record. POSIX is already UTF-8; this is a
    no-op there.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:      # not a TextIOWrapper (pytest capture, pipes)
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (ValueError, OSError):
            # Detached or already-closed stream — never let logging setup be
            # the thing that stops the bot from starting.
            pass


def setup_logging(debug=False, log_dir=None):
    log_dir = log_dir or LOG_DIR
    os.makedirs(log_dir, exist_ok=True)
    _harden_console_encoding()
    # encoding is explicit: FileHandler defaults to the platform encoding, which
    # is cp1252 on Windows and would corrupt the same glyphs in the log file.
    handler = logging.FileHandler(os.path.join(log_dir, "deepfield.log"), encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    root.handlers = [h for h in root.handlers if isinstance(h, logging.FileHandler) is False]
    root.addHandler(handler)
    return handler
