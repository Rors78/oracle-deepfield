#!/usr/bin/env bash
# Desktop launch — ONE click opens everything: the live bot (TUI in this window),
# the web console, and the browser pointed at it. Attaches if the bot is already
# running (no double-instance).
cd /home/golden/oracle-deepfield

WEB_URL="http://localhost:${DEEPFIELD_WEB_PORT:-8787}"

# Ensure the web console is serving. The current bot serves it in-process; if the
# port isn't answered yet (e.g. a bot started before this feature), start a
# standalone read-only reader against the live DB. The `if` guard means we never
# double-bind the port — whoever owns it keeps it.
if ! curl -sf "$WEB_URL/api/health" >/dev/null 2>&1; then
    tmux has-session -t deepfield-web 2>/dev/null || \
        tmux new-session -d -s deepfield-web -c /home/golden/oracle-deepfield \
            "./venv/bin/python -m deepfield --web"
fi

# Open the browser once something answers on the port (background; gives up ~25s).
(
    for _ in $(seq 1 50); do
        curl -sf "$WEB_URL/api/health" >/dev/null 2>&1 && break
        sleep 0.5
    done
    xdg-open "$WEB_URL" >/dev/null 2>&1 \
        || sensible-browser "$WEB_URL" >/dev/null 2>&1 \
        || firefox "$WEB_URL" >/dev/null 2>&1
) &

# The bot TUI in this terminator window. Attach ONLY if the bot process is really
# running — a session left at the "[exited — press Enter]" prompt is a corpse, not
# a live bot, and must be replaced, not attached to. Kill any stale session first.
if pgrep -f "python -m deepfield$" >/dev/null 2>&1; then
    exec tmux attach -t deepfield
fi
tmux kill-session -t deepfield 2>/dev/null
exec tmux new-session -s deepfield -c /home/golden/oracle-deepfield \
  "DEEPFIELD_EXEC_MODE=live ./venv/bin/python -m deepfield; \
   echo; echo '[DEEPFIELD exited — press Enter to close]'; read"
