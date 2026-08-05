"""Cross-platform import surface.

deepfield/keys.py needs POSIX terminal control (tty/termios) for the TUI's key
handling, and app.py imports it at MODULE level. So a bare `import termios` did not
degrade the keyboard on Windows — it made `import deepfield.app` raise
ModuleNotFoundError, which took down `python -m deepfield` entirely before anything
started. Found 2026-07-22 when the repo was cloned to a Windows 11 machine.

Everything else in the package is stdlib-portable, so this one import is the whole
difference between "runs on Windows" and "does not start on Windows". These tests
simulate a platform with no POSIX terminal modules and assert the package still
loads and the controller reports itself unavailable rather than raising.
"""
import builtins
import importlib
import sys

import pytest

# Blocked on Windows. fcntl is included because it is the usual companion import
# that sneaks in with terminal handling.
POSIX_ONLY = {"termios", "tty", "fcntl"}

# The modules that must survive, i.e. everything on the path from `python -m deepfield`
# to a running bot, plus the read-only console.
MUST_IMPORT = [
    "deepfield",
    "deepfield.__main__",
    "deepfield.app",
    "deepfield.config",
    "deepfield.keys",
    "deepfield.ui",
    "deepfield.simple_ui",
    "deepfield.ingest",
    "deepfield.executor",
    "deepfield.broker",
    "deepfield.store",
    "deepfield.engine",
    "deepfield.signals",
    "deepfield.indicators",
    "deepfield.defense",
    "deepfield.alerter",
    "deepfield.ws_client",
    "deepfield.web.server",
]


@pytest.fixture
def no_posix_terminal(monkeypatch):
    """Make the POSIX-only terminal modules unimportable, and drop anything already
    imported that depends on them so the import actually re-runs under the block."""
    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.split(".")[0] in POSIX_ONLY:
            raise ModuleNotFoundError(f"No module named '{name}'")
        return real_import(name, *args, **kwargs)

    for mod in list(sys.modules):
        if mod.split(".")[0] in POSIX_ONLY or mod.startswith("deepfield"):
            monkeypatch.delitem(sys.modules, mod, raising=False)
    monkeypatch.setattr(builtins, "__import__", blocked)
    return blocked


@pytest.mark.parametrize("modname", MUST_IMPORT)
def test_imports_without_posix_terminal_modules(no_posix_terminal, modname):
    """The regression guard: any new module-level `import termios`/`tty`/`fcntl`
    anywhere on this path breaks Windows, and fails here instead."""
    importlib.import_module(modname)


def test_key_controller_reports_unavailable_instead_of_raising(no_posix_terminal):
    """app.py treats a False from start() as "no key controls" — the same path as a
    non-TTY stdin — so returning False keeps the dashboard running. Raising would not."""
    keys = importlib.import_module("deepfield.keys")
    assert keys.termios is None and keys.tty is None
    kc = keys.KeyController(loop=None, handlers={})
    assert kc.start() is False
    kc.stop()          # must be a clean no-op, not an AttributeError on None


def test_key_controller_still_works_where_posix_is_available(monkeypatch):
    """Guard the guard: the fallback must not have disabled key controls on Linux,
    where the operator actually runs this. Import-level check only — starting a real
    cbreak reader needs a TTY the suite does not have.

    Re-imports from scratch: a preceding blocked-import test leaves a deepfield.keys
    in sys.modules whose termios is None, and reusing that would make this pass or
    fail on test ORDER rather than on the code."""
    if sys.platform.startswith("win"):
        pytest.skip("no POSIX terminal control on this platform")
    for mod in list(sys.modules):
        if mod.startswith("deepfield"):
            monkeypatch.delitem(sys.modules, mod, raising=False)
    keys = importlib.import_module("deepfield.keys")
    assert keys.termios is not None and keys.tty is not None


# ── Console encoding ─────────────────────────────────────────────────────────
# Log messages carry box-drawing, arrows and em-dashes throughout (1765 chars
# across 30+ codepoints that cp1252 cannot encode). Windows opens stdout as
# cp1252, so StreamHandler.emit raised UnicodeEncodeError and logging replaced
# the intended line with a stack trace on the operator's console. Never fatal —
# logging swallows handler errors — which is exactly why it survived unnoticed.
# Found 2026-08-03 on the same Windows 11 machine as the termios bug above.

UNENCODABLE = "web console \u2192 http://127.0.0.1:8787 \u2500\u2550 \u274c"


def _emit_to_cp1252_stream(harden):
    """Emit a record carrying cp1252-hostile glyphs to a simulated Windows console.

    Returns (console_text, raised_traceback). Uses a private logger with
    propagate=False so the root handlers the suite installs cannot absorb or
    duplicate the record, and so this cannot pass or fail on test ORDER.

    logging reports a failing handler by printing to the real sys.stderr rather
    than by raising, so the failure signal must be captured THERE — reading only
    the handler's own stream shows an empty buffer and looks like success.
    """
    import contextlib
    import io
    import logging

    buf = io.BytesIO()
    stream = io.TextIOWrapper(buf, encoding="cp1252", newline="")
    if harden:
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (ValueError, OSError):
            pass
    handler = logging.StreamHandler(stream)
    log = logging.getLogger("deepfield.test.console.%s" % harden)
    log.handlers = [handler]
    log.propagate = False
    log.setLevel(logging.INFO)
    complaints = io.StringIO()
    with contextlib.redirect_stderr(complaints):
        log.info(UNENCODABLE)
        handler.flush()
        stream.flush()
    text = buf.getvalue().decode("utf-8", errors="replace")
    noise = complaints.getvalue()
    return text, ("Traceback" in noise or "UnicodeEncodeError" in noise)


def test_cp1252_console_swallows_the_record_without_the_guard():
    """Here is the bug: an unhardened cp1252 console loses the line to a traceback."""
    text, raised = _emit_to_cp1252_stream(harden=False)
    assert raised, "expected UnicodeEncodeError on a raw cp1252 stream, got: %r" % text[:200]


def test_hardened_console_renders_the_record_intact():
    """Here is the guard: reconfigured to UTF-8, the real message survives."""
    text, raised = _emit_to_cp1252_stream(harden=True)
    assert not raised, "console still raised: %r" % text[:200]
    assert "web console \u2192 http://127.0.0.1:8787" in text


def test_harden_is_a_noop_on_streams_that_cannot_reconfigure(monkeypatch):
    """pytest capture and pipes replace stdout with objects that have no
    reconfigure(). Logging setup must not be the thing that stops the bot."""
    import io

    from deepfield import logsetup

    monkeypatch.setattr(sys, "stdout", io.StringIO(), raising=False)
    monkeypatch.setattr(sys, "stderr", io.StringIO(), raising=False)
    logsetup._harden_console_encoding()      # must not raise


def test_file_handler_is_utf8_not_platform_default(tmp_path):
    """The log FILE had the same defect: FileHandler with no encoding= inherits
    cp1252 on Windows, corrupting the same glyphs it is meant to preserve."""
    import logging

    from deepfield import logsetup

    handler = logsetup.setup_logging(log_dir=str(tmp_path))
    try:
        logging.getLogger().info(UNENCODABLE)
        handler.flush()
        written = (tmp_path / "deepfield.log").read_text(encoding="utf-8")
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()
    assert (handler.encoding or "").lower().replace("-", "") == "utf8"
    assert "\u2192" in written and "\u2500" in written
