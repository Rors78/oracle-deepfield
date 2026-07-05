#!/usr/bin/env bash
# Attach to the live dashboard, creating it if it isn't running.
tmux attach -t deepfield 2>/dev/null || \
  tmux new-session -s deepfield "/home/golden/oracle-deepfield/scripts/deepfield-loop.sh"
