#!/usr/bin/env bash
# ── DEEPFIELD driver ──────────────────────────────────────────────────────────
# Launch the rich TUI in a throwaway tmux session sized like the operator's real
# screen (229x54 -> the wide two-column layout), wait for the dashboard to warm
# up (cold backfill from Kraken can take ~20-40s), capture the frame — the TUI's
# "screenshot" — then quit cleanly and tear the session down.
#
#   smoke.sh [paper|off|live]   default: paper  (safe — never places real orders)
#
# Notes:
#  * off/paper never touch the exchange with orders. `live` places REAL orders on
#    confirmed BUYs — do not use for a smoke test.
#  * SQLite is WAL + busy_timeout=5000, so this can run briefly alongside a live
#    instance, but don't leave two full writers up long-term.
set -u
MODE="${1:-paper}"
ROOT="/home/golden/oracle-deepfield"
SKILL_DIR="$ROOT/.claude/skills/run-deepfield"
SESSION="deepfield-smoke-$$"
OUT="$SKILL_DIR/last-frame.txt"

cd "$ROOT" || { echo "no $ROOT"; exit 1; }
[ -x venv/bin/python ] || { echo "venv missing — run: python3 -m venv venv && venv/bin/pip install -r requirements.txt"; exit 1; }

tmux kill-session -t "$SESSION" 2>/dev/null
# -x/-y size the detached session so the wide layout (>=150 cols) renders.
tmux new-session -d -s "$SESSION" -x 229 -y 54 -c "$ROOT" \
  "DEEPFIELD_EXEC_MODE=$MODE ./venv/bin/python -m deepfield"
echo "[smoke] launched $SESSION (EXEC_MODE=$MODE) — warming up (backfill)..."

ready=0
for _ in $(seq 1 30); do
  sleep 3
  if tmux capture-pane -t "$SESSION" -p 2>/dev/null | grep -qE "DEEPFIELD v|Traceback"; then
    ready=1; break
  fi
done
# The frame renders instantly, but WS ticks take a few seconds to flow — let them
# arrive so the captured frame shows live prices (not STALE / "no tick").
[ "$ready" = 1 ] && sleep 18

tmux capture-pane -t "$SESSION" -p > "$OUT" 2>/dev/null
if [ "$ready" = 1 ]; then
  echo "[smoke] dashboard rendered — frame captured to $OUT"
else
  echo "[smoke] WARNING: dashboard did not render in 90s — captured whatever is there:"
fi
sed -n '1,8p' "$OUT"

tmux send-keys -t "$SESSION" q 2>/dev/null   # 'q' = clean quit
sleep 2
tmux kill-session -t "$SESSION" 2>/dev/null
echo "[smoke] session torn down. Full frame: $OUT"
[ "$ready" = 1 ] || exit 1
