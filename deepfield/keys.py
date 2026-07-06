"""Keyboard controls — termios cbreak reader on the asyncio loop. SPEC §8 + v6.

q quit · p pause · f reconcile · a test-alert · 1/2/3 views · j/k (↑/↓) select ·
enter/space expand · ,/. scroll. Handlers mutate AppState only.

Keys are matched EXACTLY first (so a multi-byte escape sequence like ↑ = \\x1b[A
routes as one key), then per-byte (so a burst/paste still dispatches each key).
A bare ESC or an unrecognized sequence dispatches nothing harmful.

Only activates when stdin is a real TTY (never in --once/cron/tests). The
terminal is restored in stop() no matter how the app exits — a cbreak terminal
left behind is exactly the kind of operator-hostile mess this project bans.
"""
import os
import sys
import tty
import termios
import logging

log = logging.getLogger("deepfield.keys")


class KeyController:
    def __init__(self, loop, handlers):
        """handlers: dict of single-byte keys (bytes) -> zero-arg callables."""
        self.loop = loop
        self.handlers = handlers
        self._fd = None
        self._saved = None

    def start(self):
        if not sys.stdin.isatty():
            log.info("stdin is not a tty — key controls disabled")
            return False
        self._fd = sys.stdin.fileno()
        self._saved = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)
        self.loop.add_reader(self._fd, self._on_readable)
        printable = "".join(sorted(k.decode("latin-1") for k in self.handlers
                                   if len(k) == 1 and 32 <= k[0] < 127))
        log.info("key controls active: %s (+arrows)", printable)
        return True

    def _on_readable(self):
        # Read the whole chunk so an escape sequence (arrows) arrives intact in
        # one syscall rather than as three unmatchable single bytes.
        try:
            data = os.read(self._fd, 16)
        except OSError:
            return
        if not data:
            return
        exact = self.handlers.get(data)
        if exact is not None:
            self._dispatch(data, exact)
            return
        for b in data:                       # per-byte fallback (bursts / pastes)
            handler = self.handlers.get(bytes([b]))
            if handler is not None:
                self._dispatch(bytes([b]), handler)

    def _dispatch(self, key, handler):
        try:
            handler()
        except Exception:
            log.exception("key handler %r failed", key)

    def stop(self):
        if self._fd is None:
            return
        try:
            self.loop.remove_reader(self._fd)
        except Exception:
            pass
        if self._saved is not None:
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
            except Exception:
                log.exception("failed to restore terminal attributes")
        self._fd = None
