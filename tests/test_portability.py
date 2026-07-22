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
