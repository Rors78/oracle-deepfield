#!/usr/bin/env bash
# Manual desktop launch. Attaches if already running (no double-instance);
# otherwise starts the live bot in tmux so closing the window leaves it running.
cd /home/golden/oracle-deepfield
if tmux has-session -t deepfield 2>/dev/null; then
    exec tmux attach -t deepfield
fi
exec tmux new-session -s deepfield -c /home/golden/oracle-deepfield \
  "DEEPFIELD_EXEC_MODE=live ./venv/bin/python -m deepfield; \
   echo; echo '[DEEPFIELD exited — press Enter to close]'; read"
