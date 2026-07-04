# ORACLE DEEPFIELD

Always-on terminal monitor: weekly + daily 7-signal cycle-bottom detection over
15 Kraken USD spot pairs. Live WebSocket v2 layer + closed-candle structural
scoring + a marked provisional layer. **Signal-only — no order execution.**
It recommends; the operator places.

- Full build spec: [`docs/SPEC.md`](docs/SPEC.md)
- Authoritative rulings (supersede spec prose): [`docs/RULINGS.md`](docs/RULINGS.md)
- Parked ideas: [`docs/LATER.md`](docs/LATER.md)

## Setup

```
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
```

First run only — cold backfill (720 candles × 15 pairs × 2 intervals, ~20-25s,
throttled REST):

```
./venv/bin/python -m deepfield --backfill --full
```

Every subsequent start does a warm backfill automatically (only the gap since
the last stored candle — near-instant).

## Run

```
python -m deepfield            # live TUI (rich Live, alternate screen)
python -m deepfield --simple   # plaintext frame every SIMPLE_SECS (dumb terminals, logging, cron)
python -m deepfield --once     # single confirmed eval + one plaintext frame (cron/tests)
```

Ctrl-C exits cleanly from any mode.

### Running under tmux (recommended for always-on use)

```
tmux new -s deepfield
./venv/bin/python -m deepfield
```

Detach: `Ctrl-b d`. Reattach from anywhere (including Termux SSH):

```
tmux attach -t deepfield
```

List sessions: `tmux ls`. Kill the session: `tmux kill-session -t deepfield`.

Over a narrow Termux SSH window, prefer `--simple` — the rich table degrades
to lossy ellipsis truncation below ~65-70 columns; `--simple` stays fully
legible down to ~50.

### Other CLI flags

```
python -m deepfield --debug                 # verbose logging (RotatingFileHandler, logs/deepfield.log)
python -m deepfield --test-alert            # exercise the full alert chain end-to-end (kind=test)
python -m deepfield --reconcile             # one gap-heal pass over all 15 pairs, then exit
python -m deepfield --backfill [--full]     # backfill only, then exit (--full = cold/720-candle refetch)
python -m deepfield --test-drop             # M4 drill: force a WS reconnect, prove resubscribe+gap-heal
python -m deepfield import-legacy <csv>     # seed the F10 cooldown ledger from a legacy v4.x dca_log.csv
python -m deepfield export-csv <path>       # dump the alerts ledger to CSV
```

`import-legacy` maps legacy display symbols (`LTC`) to DEEPFIELD's ws_symbol
(`LTC/USD`) and localizes legacy's naive timestamps as America/Denver before
converting to UTC, so a symbol logged as BUY recently by the old scanner won't
immediately re-alert once DEEPFIELD's first confirmed evaluation runs. Safe to
skip — the only cost is a possible one-time duplicate alert within
`REALERT_HOURS` of the legacy log's last entry for that symbol.

### Environment (optional)

```
export ORACLE_TG_TOKEN=...   # Telegram bot token — enables Telegram alerts
export ORACLE_TG_CHAT=...    # Telegram chat id
```

Never committed, never read from a file — env only. Alerts work fully without
these; Telegram is simply skipped (logged, not an error) when unset.

## Dev

```
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
./venv/bin/pytest -v
```

Status: **M0–M7 complete.** Milestones + proof gates in `docs/SPEC.md §13`.
