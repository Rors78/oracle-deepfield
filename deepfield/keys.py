"""Keyboard controls — termios cbreak reader on the asyncio loop. SPEC §8.

q quit · p pause render · f force reconcile · a test alert.

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
        log.info("key controls active: %s", b"".join(sorted(self.handlers)).decode())
        return True

    def _on_readable(self):
        try:
            ch = os.read(self._fd, 1)
        except OSError:
            return
        handler = self.handlers.get(ch)
        if handler is None:
            return
        try:
            handler()
        except Exception:
            log.exception("key handler %r failed", ch)

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
