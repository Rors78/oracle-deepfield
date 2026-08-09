#!/usr/bin/env bash
# DEEPFIELD update — the exact sequence used for every deploy since 2026-08-08,
# as one command. Run from anywhere:
#
#   bash ~/oracle-deepfield/scripts/update.sh            # pull only
#   bash ~/oracle-deepfield/scripts/update.sh --restart  # pull + clean restart
#
# Pull-only is enough for deck/server page changes: the web server re-reads
# deck.html per request (stat-keyed cache), so the console updates immediately.
# Anything touching deepfield/*.py needs --restart to load.
#
# --restart does what the operator's keypress does: sends `q` into the deepfield
# tmux session (clean shutdown — pollers stop mid-cycle safely, RUN END logged),
# waits for the process to exit, then respawns the SAME tmux window so an
# attached terminal survives. Reconcile re-verifies every stop on boot; a rung
# that fills during the seconds of downtime is claimed by fill-recovery (the
# known-benign boot-surplus pattern).
set -euo pipefail
cd "$(dirname "$0")/.."

BEFORE=$(git rev-parse --short HEAD)
git pull --ff-only
AFTER=$(git rev-parse --short HEAD)

if [ "$BEFORE" = "$AFTER" ]; then
    echo "already at $AFTER — nothing pulled"
else
    echo "updated $BEFORE -> $AFTER"
fi

if [ "${1:-}" != "--restart" ]; then
    RUNNING=$(pgrep -f "venv/bin/python -m deepfield$" | head -1 || true)
    if [ -n "$RUNNING" ]; then
        echo "bot (pid $RUNNING) still runs the OLD code — rerun with --restart to load $AFTER"
    fi
    exit 0
fi

PID=$(pgrep -f "venv/bin/python -m deepfield$" | head -1 || true)
if [ -z "$PID" ]; then
    echo "no running bot found — start it via the desktop shortcut as usual"
    exit 1
fi
if ! tmux has-session -t deepfield 2>/dev/null; then
    echo "bot pid $PID is running but not under the 'deepfield' tmux session —"
    echo "refusing to kill it blind. Stop it yourself, then relaunch."
    exit 1
fi

echo "clean-stopping pid $PID (tmux q)..."
tmux send-keys -t deepfield q
for _ in $(seq 1 60); do
    kill -0 "$PID" 2>/dev/null || break
    sleep 1
done
if kill -0 "$PID" 2>/dev/null; then
    echo "pid $PID did not exit within 60s — NOT respawning; check the deepfield window"
    exit 1
fi
echo "clean exit · respawning..."
tmux respawn-window -k -t deepfield
sleep 3
NEW=$(pgrep -f "venv/bin/python -m deepfield$" | head -1 || true)
if [ -n "$NEW" ]; then
    echo "bot relaunched (pid $NEW) on $AFTER — check the deck in ~a minute"
else
    echo "respawn issued but no process visible yet — check the deepfield window"
fi
