#!/usr/bin/env bash
# Persistent runner: restarts the bot on CRASH (non-zero exit); a clean quit
# (q / Ctrl-C -> exit 0) stops the loop. Reboot survival is handled by systemd.
cd /home/golden/oracle-deepfield
export DEEPFIELD_EXEC_MODE="${DEEPFIELD_EXEC_MODE:-live}"
until ./venv/bin/python -m deepfield; do
  code=$?
  echo "[deepfield-loop] exited $code — restarting in 5s ($(date))" >&2
  sleep 5
done
echo "[deepfield-loop] clean exit — staying down"
