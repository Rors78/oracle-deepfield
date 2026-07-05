---
name: run-deepfield
description: Run, launch, build, smoke-test, or screenshot ORACLE DEEPFIELD — the always-on Kraken cycle-bottom monitor + leveraged-execution TUI. Use when asked to start the bot, capture its dashboard, drive it headless, or verify it runs.
---

# Run ORACLE DEEPFIELD

DEEPFIELD is a Python (3.12) **terminal dashboard** (`rich` Live TUI) that streams
15 Kraken USD pairs over websockets, scores each for a cycle bottom, and (when
armed) places leveraged margin orders. It has no window — it draws to the
terminal — so the agent path drives it inside **tmux** and captures the frame as
text. All paths below are relative to the unit root `/home/golden/oracle-deepfield`.

## Prerequisites

`tmux` (3.x) and `python3-venv`. On a clean box:
```bash
sudo apt-get install -y tmux python3-venv
```
(Both were already present here — tmux 3.4, Python 3.12.3.)

## Build

```bash
cd /home/golden/oracle-deepfield
python3 -m venv venv
./venv/bin/pip install -r requirements.txt      # websockets, rich, pytest
```

No Kraken keys are needed to run the **dashboard** — it uses the public WS feed.
Keys (`~/.deepfield_keys`, falling back to `~/.hydra_keys`; two lines: key,
secret) are only read for live equity, `live` execution, and `--exec-probe`.

## Run — agent path (driver)

The driver launches the full TUI in a throwaway tmux session sized to the wide
layout, waits for websocket ticks to flow, captures the frame, and quits clean:

```bash
.claude/skills/run-deepfield/smoke.sh paper      # off | paper | live  (default: paper)
```
It writes the captured dashboard to `.claude/skills/run-deepfield/last-frame.txt`
and prints the header. **Only ever use `off` or `paper` to smoke-test** — `live`
places REAL orders on confirmed BUYs. A good frame shows live prices, fresh `AGE`
(e.g. `10s`), and no `STALE`.

Fastest non-interactive check (no WS, no tmux — backfill + one eval + one
plaintext frame, then exits 0):
```bash
DEEPFIELD_EXEC_MODE=off ./venv/bin/python -m deepfield --once
```

Inspect execution config / rails / recent orders (read-only, safe alongside a
running instance):
```bash
./venv/bin/python -m deepfield --exec-status
```

Prove the live *order path* against real Kraken without executing (sends
`validate=true` orders for all pairs — needs keys):
```bash
DEEPFIELD_EXEC_MODE=off ./venv/bin/python -m deepfield --exec-probe
```

## Run — human path

```bash
DEEPFIELD_EXEC_MODE=paper ./venv/bin/python -m deepfield        # full TUI; --simple for plaintext
```
Keys inside the TUI: `q` quit · `p` pause · `f` reconcile · `a` test-alert.
Detach without stopping it under tmux with `Ctrl+b d`. Headless, `python -m
deepfield` alone still runs — but you can't see it without a terminal, hence the
driver.

`EXEC_MODE`: `off` (default, no orders) · `paper` (simulated fills) · `live`
(REAL leveraged orders). Halt all entries anytime: `touch deepfield.HALT_ENTRIES`
(the dashboard shows `⛔ HALTED`); resume by deleting it.

## Test

```bash
./venv/bin/python -m pytest -q        # 64 tests, ~12s
```

## Gotchas (battle scars from actually running it)

- **Wide layout needs ≥150 cols.** The two-column dashboard only renders at
  width ≥150; the driver forces `tmux new-session -x 229 -y 54`. A narrower
  terminal silently falls back to a stacked single column. `--simple` is the
  any-size plaintext fallback.
- **WS ticks lag the first frame by ~10-15s.** The frame renders instantly at
  `up 00:00:02` but every row is `STALE` / `AGE ---` until ticks arrive — capture
  too early and the "screenshot" looks dead. The driver sleeps 18s after render
  before capturing for exactly this reason.
- **`--once` shows `LINK DOWN` and all `STALE` — that's correct.** It does a
  backfill + single confirmed eval + one plaintext frame with no live WS; it's a
  logic/render smoke, not a live view.
- **Single SQLite writer.** The DB is WAL + `busy_timeout=5000`, so a brief
  second instance is tolerated, but don't leave **two** full `live`/`paper`
  writers up — they contend. To look at a running live bot, use `--exec-status`
  or `--once` (readers), not a second full instance.
- **`deepfield.db` path is fixed** (`config.DB_PATH` = project root, not
  env-overridable). The driver reads/writes the real DB; it's WAL-safe but shared
  with any live instance.
- **HALT file is sticky.** If `deepfield.HALT_ENTRIES` exists, rails block all
  entries and the champion card reads `⛔ HALTED` — this also makes execution
  tests look "blocked." Delete it to arm. (`tests/conftest.py` isolates it so the
  suite isn't poisoned by a live HALT file.)

## Troubleshooting

- **All rows `STALE`, `AGE ---`, `BTC LIVE unavailable`** → captured before ticks
  flowed. Wait ~15s (the driver does) or check the WS: `LINK UP ●A ●B` in the
  header means connected.
- **Dashboard cramped / single column** → terminal < 150 cols. Use the driver
  (forces 229) or widen the window; or run `--simple`.
- **`database is locked` in logs** → a second full writer is running. Stop one;
  use a reader mode (`--exec-status`, `--once`) alongside a live instance.
- **`rails=BLOCKED: HALT file present`** → `rm deepfield.HALT_ENTRIES`.
- **`keys=MISSING` on `--exec-status`/`--exec-probe`** → put key+secret (two
  lines) in `~/.deepfield_keys`. Not needed for `off`/`paper` dashboard runs.
