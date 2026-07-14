#!/usr/bin/env bash
# Regenerate docs/DEEPFIELD_AUDIT_BUNDLE.md — a single self-contained file
# (design rationale + verbatim money-path source) to hand to an external auditor.
#
# Run any time the executor / config / call-path changes, so the bundle never
# goes stale:  ./scripts/build-audit-bundle.sh
set -euo pipefail

# Repo root = parent of this script's dir, so it works from any cwd.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DOC="docs/AUDIT_ORIENTATION.md"          # Part 1 (instructions/rationale)
OUT="docs/DEEPFIELD_AUDIT_BUNDLE.md"     # the combined deliverable

# Money-path source, in reading order. Primary audit target first.
FILES=(
  deepfield/executor.py   # target: sizing, placement, fill/stop state machine
  deepfield/config.py     # every knob the executor reads (rails, sizing, stops)
  deepfield/broker.py     # Kraken private API: nonce, signing, retry, idempotency
  deepfield/ingest.py     # confirmed-BUY -> order hook + one-shot startup arm
  deepfield/app.py        # poll_fills / verify_open_stops wiring
  deepfield/signals.py    # what makes a BUY (the trigger)
  deepfield/store.py      # sizing/pnl/position-count helpers
)

[ -f "$DOC" ] || { echo "ERROR: missing $DOC (Part 1 source)" >&2; exit 1; }
for f in "${FILES[@]}"; do
  [ -f "$f" ] || { echo "ERROR: missing source file $f" >&2; exit 1; }
done

{
  printf '# ORACLE DEEPFIELD — Audit Bundle (single file)\n\n'
  printf '> **How to use this file:** read Part 1 (design rationale + what to scrutinize),\n'
  printf '> then audit the source in Part 2. Everything the review needs is in this one\n'
  printf '> file — the live leveraged-execution path and every config knob it reads.\n'
  printf '> Generated %s from the running repo.\n\n' "$(date -u '+%Y-%m-%d %H:%M UTC')"
  printf -- '---\n\n'
  printf '# PART 1 — DESIGN RATIONALE & AUDITOR INSTRUCTIONS\n\n'
  cat "$DOC"
  printf '\n\n---\n\n'
  printf '# PART 2 — SOURCE UNDER AUDIT\n\n'
  printf 'Full, verbatim source of the money path. Primary target: `executor.py`.\n'
  printf 'Supporting files are included so every symbol referenced above resolves.\n\n'
  for f in "${FILES[@]}"; do
    printf '## `%s` (%s lines)\n\n' "$f" "$(wc -l < "$f")"
    printf '```python\n'
    cat "$f"
    printf '```\n\n'
  done
} > "$OUT"

echo "wrote $OUT — $(wc -l < "$OUT") lines, $(du -h "$OUT" | cut -f1)"
