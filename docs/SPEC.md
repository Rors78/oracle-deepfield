# ORACLE DEEPFIELD (v5) — Build Specification

**From:** Architect (Opus, relayed by the operator, Jeremy)
**To:** Builder (Claude Code / Opus CLI)
**Status:** Authoritative. API contracts below were verified against live Kraken and docs.kraken.com on 2026-07-03. Where this spec and Kraken reality disagree, reality wins — but post the discrepancy before adapting.

**Name rationale:** Hubble Deep Field — park on a patch of dark sky, long exposure, wait for faint signal. That is exactly this strategy: weekly/daily bottom detection for long-horizon spot accumulation. The lineage is Oracle DCA v4.x (a 4-hour REST poller). DEEPFIELD is the always-on, WebSocket-live successor.

---

## §0 — Working protocol (read first)

The operator relays messages between you and the architect. To make that loop tight:

1. **Read this entire spec before writing any code.** Your first reply is (a) the plan restated in your own words, and (b) every question you have, batched. Misreads are cheapest to catch here.
2. **Milestone-gated.** Do not start M(n+1) until M(n)'s proof is posted and acked by the architect.
3. **Proof means pasted runtime output.** Each milestone specifies its proof commands. "Done" without proof is a defect. The operator has near-perfect QA instinct and a long memory for false completion claims — do not test this.
4. **Blocked?** After 2 distinct failed attempts, stop. Post the exact error, what you tried, and your best hypothesis. Do not thrash.
5. **Commit per milestone**, message format `M<N>: <summary>`. Never commit secrets.
6. **Scope discipline.** Anything not in this spec is out of scope. Good ideas go in `docs/LATER.md` as one-liners, not code.

---

## §1 — Mission

An always-on terminal application that watches 15 Kraken USD spot pairs for cycle-bottom conditions using the 7-signal Oracle scoring system on weekly + daily candles, with:

- **Live layer:** WebSocket v2 ticker (all pairs) + live-updating daily and weekly candles.
- **Structural layer:** closed-candle signal evaluation — the strategy's discipline is preserved. Confirmed scores change only when candles close; the UI shows exactly when that will happen.
- **Provisional layer:** a second evaluation including the forming candle, clearly marked, display-only by default.
- A cinematic flight-telemetry TUI, an alert chain with a persistent cooldown ledger, and SQLite ground truth.

**This tool is signal-only. There is no order execution anywhere in this build.** It recommends; the operator places.

---

## §2 — Invariants (non-negotiable, from the operator's canon)

1. **Persistence:** all state survives restart. RAM-only state is a bug. SQLite (WAL) is ground truth.
2. **Single-writer:** exactly one task owns DB writes. Everything else reads.
3. **State over events:** scores, alerts, and displays derive from persisted candle/ticker state — never directly from the event stream. The stream is transport, not truth.
4. **Blindness check:** no formula may map *missing* data to a more favorable value than *present* data. N/A signals shrink the denominator; they never count as fired. Stale pairs degrade visibly; they never silently hold last-known-good.
5. **UI reads engine-published values only.** The UI never re-implements a formula. (This bug class cost the operator multi-hour ghost hunts. Forbidden.)
6. **Reconciliation diffs, logs loudly, then repairs. Never silently fixes.**
7. **Confirmed vs provisional:** the alert ledger fires on closed candles only. Provisional results are display-layer, marked, and alert only if `PROVISIONAL_ALERTS=True` (default False).
8. **Proof before done.**
9. **No unsolicited risk features.** Build what is specified; the operator makes his own risk calls.

---

## §3 — Environment & isolation

- **Host:** Dell Optiplex 7050 SFF, i7-7700 (4c/8t), Ubuntu. This box is shared with another running project — be a polite neighbor. Steady-state budget: **<5% of one core, <300 MB RSS** (measured at M6).
- **M0 step 1:** run `lsblk -f && df -h`, identify the shared 1 TB drive's mount point **with the operator**, then create `<mount>/oracle-deepfield/`. Hard rule: **touch nothing else on that drive.** No imports from, symlinks to, or writes into any other project directory. This build is an island.
- Own venv inside the project dir. Own git repo. `.gitignore`: `venv/`, `__pycache__/`, `*.db`, `*.db-wal`, `*.db-shm`, `logs/`, `*.log`.
- `python3 --version` must be ≥3.10. Write 3.10-compatible asyncio (no `TaskGroup` unless 3.11+ is confirmed, and then only if you prefer it).
- Terminals: **terminator** locally, **Termux → SSH** from the operator's phone. The TUI must render correctly on both (rich auto-negotiates color depth). A `--simple` plaintext mode is REQUIRED (§8).

---

## §4 — Dependencies

Operator ruling: **dependencies are fine; dogma is not.** The old Oracle file's header ("Pure Python 3 stdlib ONLY") was a phone-era constraint from Pydroid3 days. It does **not** apply to this build — do not inherit it when you read the old code for reference.

- **Required:** `websockets` (WS client), `rich` (TUI). **Dev:** `pytest`.
- REST stays stdlib `urllib` inside `asyncio.to_thread`, because the throttle/retry pattern in Appendix B is already field-proven — port it verbatim rather than rewriting working code to justify a library.
- You MAY add small, mainstream libraries where they clearly earn their keep — one-line justification in the commit message. No kitchen sink.
- Pin exact versions in `requirements.txt` at M0.

---

## §5 — Architecture

Single asyncio process. Components and data flow:

```
ws_client ───► event queue ───► ingest/writer (owns SQLite) ───► state
rest_client ──────────────────────┘                               │
                                          engine (pure fns) ◄─────┤ reads
                                          ui (rich Live)    ◄─────┤ reads engine-published state
                                          alerter           ◄── confirmed transitions
                                          reconciler        ──► REST diff → RECON log → repair
```

Modules:

- `events.py` — typed event dataclasses: `Tick`, `CandleUpdate`, `CandleClosed`, `LinkUp`, `LinkDown`, `ReconRepair`, …
- `store.py` — SQLite WAL. One writer task consumes the queue. Read helpers for everyone else.
- `engine.py` + `signals.py` — **pure functions, zero I/O.** Input: candle series (optionally + forming candle). Output: a `ScoreCard` per pair — fired signals, N/A signals (with reasons), score, denominator, required threshold, status, gap metrics, tranche recommendation. The engine publishes every number the UI displays (invariant 5).
- `ws_client.py` — see §6 for contract and ops policy.
- `rest_client.py` — Appendix B pattern, verbatim.
- `alerter.py` — §11.
- `reconciler.py` — hourly and after every reconnect: REST-fetch the last 10 candles per pair/interval, diff against DB, log a `RECON` line with before/after for any mismatch, then repair. Repair counts surface in the UI header (invariant 6).
- `ui.py` / `simple_ui.py` — §8.
- `config.py` — §10.

**Candle-close detection** (this is subtle — verified behavior): the WS ohlc feed sends **nothing** across an interval border until the next trade occurs. On low-volume pairs a weekly close can pass silently for minutes. Therefore a close is detected by EITHER (a) a new `interval_begin` arriving for that pair/interval, OR (b) the clock passing `last_interval_begin + interval` (+5 s grace), followed by a REST confirm of the closed bar. **Never hardcode the weekly anchor day** — derive all boundaries from `interval_begin` arithmetic.

---

## §6 — Data layer contracts (verified 2026-07-03 — do not re-guess)

### REST (throttled per Appendix B)

- `GET https://api.kraken.com/0/public/OHLC?pair=<REST_PAIR>&interval=<1440|10080>`
  Returns the most recent candles, **hard-capped at 720** regardless of `since`. Row: `[time, open, high, low, close, vwap, volume, count]`. **`time` is the bar OPEN (unix).** Bar close = `time + interval*60`. All freshness math uses bar CLOSE. (v4.x measured from open and permanently displayed fresh data as "2d old" in red. Bug class F5.)
- `GET https://api.kraken.com/0/public/AssetPairs?pair=…`
  Per pair: `wsname`, `ordermin`, `costmin`. Fetch at startup, cache in `pairs` table, refresh daily.

### WebSocket v2 — `wss://ws.kraken.com/v2`

- **Symbols are v2 format** ("BTC/USD"). Derive from REST `wsname` with normalization `{XBT→BTC, XDG→DOGE}`. **Live-verified today:** AssetPairs still returns `XBT/USD` and `XDG/USD`, while v2 wants `BTC/USD` and `DOGE/USD`. Every subscribe returns a per-symbol ACK with a `success` flag — treat any `success:false` as a loud failure (surface symbol + error, do not limp along silently).
- **ticker:** `{"method":"subscribe","params":{"channel":"ticker","symbol":[...]}}` — snapshot on subscribe (default true), updates on trades. Payload per symbol: `last, bid, ask, bid_qty, ask_qty, high, low, volume, vwap, change, change_pct, timestamp`. The `high/low/volume/vwap/change/change_pct` fields are **true rolling 24h** — this fixes v4.x's since-UTC-open mislabel for free. Label the UI "24h" honestly now.
- **ohlc:** `{"method":"subscribe","params":{"channel":"ohlc","symbol":[...],"interval":1440}}` and the same at `10080`. Valid interval enum includes **1440 and 10080** (verified). Snapshot on subscribe delivers recent candles; updates stream per trade with `open/high/low/close/vwap/trades/volume/interval_begin/interval`. **`interval_begin` is the canonical bar start; the `timestamp` field is deprecated — ignore it.**
- **Keepalive:** send `{"method":"ping"}` at least every 30 s (Kraken disconnects after ~60 s of inactivity). `heartbeat` channel messages count as liveness. Watchdog: no inbound message of any kind for >10 s while subscribed → link suspect; >20 s → force reconnect.
- **Reconnect policy:** 3 immediate attempts, then exponential backoff 5 s → 60 s cap with ±20% jitter. Cloudflare bans IPs at roughly **150 connection attempts per rolling 10 minutes** — the backoff must keep worst-case attempts far below that. On every (re)connect: resubscribe all channels, then trigger a **gap-heal** (REST refetch of recent candles per pair/interval). A resumed stream is never assumed gapless.
- One connection carries everything: 15 ticker + 15 ohlc@1440 + 15 ohlc@10080 = 45 subscriptions.

---

## §7 — Engine: signals and mandated fixes

Port the seven Oracle signals (Below W-EMA200 · W-RSI<40 Turning Up · W-MACD Hist Crossup · D-RSI Divergence · W-First Up Close · W-Vol Accumulation · Near 52w Low <20%) with the following fixes. Each F-item traces to an audit finding on v4.x; **write a named regression test per fix** (`test_F1_…` etc.) so the audit trail is greppable.

- **F1 — sig5 honesty.** Rename to `W-First Up Close` (it compares closes, not highs). Require `close[-1] > close[-2]` AND ≥ `DOWN_WEEKS` (default 3) **consecutive** lower closes immediately prior. (Old code accepted any 1 down week of the prior 2 and fired on ~37% of random weeks.)
- **F2 — sig4 pivot quality.** Divergence pivots need prominence ≥ `PIVOT_MIN_DEPTH` (default 1.5% vs neighbors) and ≥3 bars spacing. Pair price/RSI pivots within ±5 bars as before.
- **F3 — young listings.** A signal whose data requirement cannot be met (e.g. EMA200 with <200 weekly bars — SUI has ~165) is **N/A**: excluded from the denominator, displayed as N/A with reason. `required = max(2, round(5/7 × achievable))`. Config `STRICT_SEVEN=True` reverts to fixed 5-of-7. N/A must never inflate a score (invariant 4).
- **F4 — monthly RSI anchor.** Sample every 4th weekly close **anchored at the end**: `wc[(len(wc)-1) % 4 :: 4]`, so the latest weekly close is always the final monthly sample. (Old start-anchored slice dropped up to 3 of the newest weeks.)
- **F5 — freshness.** Per-pair `tick_age` = seconds since the last WS message touching that pair. REST candle age measured to bar CLOSE. `tick_age > STALE_SECS` (default 180) → row renders STALE and that pair's alerts are suppressed until it recovers.
- **F6 — 24h stats** come from WS ticker fields (§6), labeled honestly.
- **F7 — levels.** `LEVELS` config: named horizontal price levels per symbol, default carries `{"BTC/USD": [("62.8k", 62858), ("57.6k", 57585)]}`. Display-only, operator-edited. No magic numbers in render code.
- **F8 — tranche floor.** `base_qty = max(ordermin, ceil_to_lot(costmin / live_price))`; `qty = base_qty × conviction_mult` with conviction: `score == required → 1.0 (STARTER)`, `required+1 → 1.5`, `≥ required+2 → 2.0` (config). The old 0.5×min tier is dead — it was below exchange minimum and unplaceable. `ordermin`/`costmin` come from AssetPairs at runtime. **Live-verified today: 11 of 15 of the old hardcoded minimums were wrong** (LTC 0.1 not 0.01 · ADA 20 not 1 · DOGE 50 not 1 · ALGO 41 not 1 · AVAX 0.5 not 0.01 · DOT 3.9 · SUI 5 · UNI 1.5 · XRP 1.65 · LINK 0.55 · AAVE 0.05). Full table in Appendix C.
- **F9 — regime.** Computed from **stored** BTC candles (no duplicate fetching by construction). Slope over 4 bars: `ema[-1] - ema[-5]`. States: BULL (above & rising), BEAR (below), RECOVERY (above & falling EMA). No float-equality NEUTRAL branch.
- **F10 — alert cooldown ledger** in SQLite: per-symbol timestamp of the most recent confirmed BUY; re-alert only after `REALERT_HOURS` (default 24). These exact semantics were field-proven in v4.4 (restart wrote zero duplicate rows). `import-legacy <csv>` seeds the ledger from the operator's existing `dca_log.csv`.
- **F11 — one VERSION constant**, used everywhere including the REST User-Agent. (v4.x shipped four different version strings in one file.)
- **F12 — logging.** `RotatingFileHandler`, 5 MB × 3 backups, INFO default, `--debug` flag. No unbounded DEBUG ledgers on a small disk.
- **F13 — confirmed/provisional split** (invariant 7). The engine evaluates twice per pair: closed series → confirmed ScoreCard; closed + forming candle → provisional ScoreCard. Provisional recompute: on ohlc update events, throttled to ≤1/s per pair (the math is microseconds on 720 floats; the throttle is for hygiene, not load).

**Indicator math:** port Appendix A **verbatim** — EMA (SMA-seeded), Wilder RSI, MACD signal seeding, and SMA were hand-verified correct by the architect. Unit-test against fixed vectors; parity-gate at M3.

**Gap metrics:** keep the concept (numeric distance to each unfired signal — it is genuinely good UX). Fix the `"n<40"` label typo → `"need<40"`.

---

## §8 — UI

**rich Live**, alternate screen, render loop capped at `RENDER_HZ` (default 2). Price cells read the latest tick state; the layout does not re-render per tick.

**Aesthetic:** AMOLED black. Palette seeded from Oracle v4.4's `C` table (gold `#ffd75f`-family, cyan, green 82, red 196 on true black). Flight-telemetry discipline: **color is information, not decoration.** Monospace column alignment. Dim single-line separators. Price cells flash green/red (~300 ms tint) on tick direction.

Layout regions, top to bottom:

1. **Header:** DEEPFIELD wordmark + VERSION · local time (operator is America/Denver) + UTC · `LINK UP`/`LINK DOWN` with reconnect count · RECON repair count · uptime.
2. **Countdowns:** `D closes 07:12:44 · W closes 3d 14:07` — derived from `interval_begin` arithmetic. This line is the honest heart of "live" for a closed-candle strategy: prices tick now; structure updates on a knowable clock. Make it prominent.
3. **BTC pulse strip:** live last · true-24h Δ% · 24h H/L · distance to each configured LEVEL.
4. **Regime line:** BULL/BEAR/RECOVERY · BTC D-RSI and M-RSI · danger tag when D-RSI < `DANGER_DRSI` (default 30 — align the display tier boundaries to this same threshold; v4.x had the warning at 30 but tiers at 25/35).
5. **Main table** (15 rows): SYM · confirmed `score/denom` · provisional score (dim, `±` marked) · WRSI · DRSI · live price · 24h Δ% · tick-age · status (`BUY` / `WCH` / `---` / `STALE`; N/A notes inline).
6. **Champion card:** live entry price AND last confirmed close (both, labeled) · tranche qty per F8 with USD cost · W-support · 52w range · signal checklist including N/A rows with reasons.
7. **Closest-not-yet:** top 3 sub-threshold pairs with gap metrics.
8. **Alert tail:** last 5 ledger rows.

**Keys** (termios cbreak reader task; deliver late in M6): `q` quit · `p` pause render · `f` force reconcile · `a` test alert.

**`--simple` mode is REQUIRED:** no rich — a v4.4-style plaintext frame printed every `SIMPLE_SECS` (default 60) from the same engine-published state. This is the fallback for dumb terminals, logging, and cron.

**Budget check at M6:** `pidstat` 60 s sample — <5% of one core, <300 MB RSS.

---

## §9 — Persistence (SQLite, WAL, single writer)

```sql
candles(pair TEXT, interval INTEGER, ts INTEGER,          -- ts = bar OPEN, unix
        o REAL, h REAL, l REAL, c REAL, v REAL,
        closed INTEGER,
        PRIMARY KEY (pair, interval, ts));

pairs(rest_pair TEXT PRIMARY KEY, ws_symbol TEXT, display TEXT,
      ordermin REAL, costmin REAL, updated_ts INTEGER);

alerts(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, symbol TEXT,
       price REAL, score INTEGER, denom INTEGER, signals TEXT,
       kind TEXT);                                        -- confirmed | provisional | test

meta(key TEXT PRIMARY KEY, value TEXT);
```

Forming candle is upserted with `closed=0`; flipped to `1` on close-confirm. **Warm start:** backfill only the gap since `MAX(ts)` per pair/interval — cold start is ~30 throttled REST calls once; warm start is near-instant.

---

## §10 — Config

`config.py`, operator-edited, in the v4.4 "CONFIG BLOCK — edit these freely" ethos. Keys: `PAIRS` (Appendix C) · `MIN_RATIO` / `STRICT_SEVEN` · `REALERT_HOURS=24` · `DOWN_WEEKS=3` · `PIVOT_MIN_DEPTH=0.015` · `STALE_SECS=180` · `DANGER_DRSI=30` · `LEVELS` · `PROVISIONAL_ALERTS=False` · `SIMPLE_SECS=60` · `RENDER_HZ=2` · conviction multipliers · `MIN_CALL_GAP=0.6` / `FETCH_RETRIES=2` · Telegram via env only (`ORACLE_TG_TOKEN`, `ORACLE_TG_CHAT`) — never in files, never committed.

---

## §11 — Alerting

On a **confirmed** BUY transition that passes the F10 cooldown:

1. Ledger row (kind=`confirmed`).
2. Local sound — tiered, each tier guarded by `shutil.which` AND artifact existence: `paplay` (generated wav) → `aplay` → terminal bell. **Never trust return code alone** — v4.x's `termux-media-player` returned 0 with no sound and swallowed alerts silently. That failure class is banned.
3. `notify-send` if present.
4. Telegram `sendMessage` via stdlib urllib POST, **iff** env vars are set. Message: symbol · score/denom · live price · top signals.

`--test-alert` exercises the entire chain end-to-end and writes a ledger row with kind=`test`.

---

## §12 — CLI

`./deepfield` (or `python -m deepfield`): default = live TUI. Flags/subcommands: `--simple` · `--once` (single confirmed evaluation + one plaintext frame; for cron and tests) · `--debug` · `--test-alert` · `--reconcile` · `import-legacy <csv>` · `export-csv <path>`.

---

## §13 — Milestones and proof gates

**M0 — Scaffold.** Mount identified with operator · project dir · venv · deps pinned · git init · this spec committed as `docs/SPEC.md` · module skeletons.
*Proof:* `df -h <mount>` · `tree -L 2` · `pip freeze` · `git log --oneline`.

**M1 — REST + store.** AssetPairs → `pairs` table with wsname normalization applied · full backfill, 15 pairs × 2 intervals.
*Proof:* `sqlite3` candle counts per pair/interval (expect ~719–720 each) · newest BTC daily candle ts+close eyeballed against kraken.com · `pairs` table dump showing ordermin/costmin/ws_symbol.

**M2 — Engine + tests.** Indicators vs fixed vectors · one named regression test per F-item where testable.
*Proof:* full `pytest -v` output, green.

**M3 — Parity gate (measurement before action).** Run the engine over backfilled closed candles; compare per-pair confirmed scores/signals against v4.4 logic on identical data. Every diff must be explained by an enumerated fix (F1/F2/F3/F9). Zero unexplained diffs.
*Proof:* side-by-side table with per-diff annotations. **Do not start M4 until acked.**

**M4 — WS client.** Connect · subscribe all 45 · normalized events logged · forced-drop reconnect drill (`--test-drop` or kill the TCP connection) demonstrating reconnect → resubscribe → gap-heal RECON lines, with Cloudflare-safe backoff timing visible in the log.
*Proof:* log excerpt with ticks from all 15 symbols + the complete reconnect sequence.

**M5 — Live engine + alert path.** Provisional scores updating · simulated candle-close (injected event) → confirmed recompute → ledger row → cooldown suppression on repeat.
*Proof:* log + `sqlite3 "select * from alerts;"` + the duplicate-suppression demo.

**M6 — TUI.** Full layout live.
*Proof:* `Console.export_svg` (or `export_text`) of a live frame · `pidstat` 60 s CPU/RSS inside budget · confirmation it renders over SSH from Termux.

**M7 — Ship.** Alert chain end-to-end (incl. Telegram if env set) · `--simple` mode · README + tmux runbook (`tmux new -s deepfield`) · `import-legacy` demo against a **copy** of the operator's old `dca_log.csv`.
*Proof:* test-alert transcript · one simple-mode frame · README.

---

## §14 — Out of scope

Order execution · portfolio/PnL tracking · additional venues · web dashboard · more pairs · strategy changes beyond the F-items. Park ideas in `docs/LATER.md`.

---

## Appendix A — Verified indicator functions (port VERBATIM)

Hand-verified correct by the architect. Do not "improve" the math.

```python
import math

def safe_float(v):
    """Convert to float, return 0.0 on any failure."""
    try:
        f = float(v)
        return f if math.isfinite(f) else 0.0
    except Exception:
        return 0.0

def clean(series):
    """Return list of finite floats, replacing non-finite with 0.0."""
    out = []
    for v in series:
        f = safe_float(v)
        out.append(f if math.isfinite(f) else 0.0)
    return out

def calc_ema(series, period):
    """EMA, SMA-seeded. Same length as input; 0.0 where insufficient data."""
    s = clean(series)
    n = len(s)
    out = [0.0] * n
    if n < period:
        return out
    k = 2.0 / (period + 1)
    seed = sum(s[:period]) / period
    out[period - 1] = seed
    for i in range(period, n):
        out[i] = s[i] * k + out[i - 1] * (1.0 - k)
    return out

def calc_rsi(series, period=14):
    """Wilder RSI. Same length as input; 0.0 where insufficient data."""
    s = clean(series)
    n = len(s)
    out = [0.0] * n
    if n < period + 1:
        return out
    gains, losses = [], []
    for i in range(1, n):
        delta = s[i] - s[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    if len(gains) < period:
        return out
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    if avg_loss == 0.0:
        out[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        out[period] = 100.0 - (100.0 / (1.0 + rs))
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0.0:
            out[i + 1] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i + 1] = 100.0 - (100.0 / (1.0 + rs))
    return out

def calc_macd(series, fast=12, slow=26, signal_period=9):
    """MACD -> (macd_line, signal_line, histogram), same-length lists."""
    s = clean(series)
    n = len(s)
    e_fast = calc_ema(s, fast)
    e_slow = calc_ema(s, slow)
    macd_line = [0.0] * n
    for i in range(n):
        if e_fast[i] != 0.0 and e_slow[i] != 0.0:
            macd_line[i] = e_fast[i] - e_slow[i]
    sig_line = [0.0] * n
    start = slow - 1
    valid = [(i, macd_line[i]) for i in range(start, n) if macd_line[i] != 0.0]
    if len(valid) >= signal_period:
        seed_idx  = valid[signal_period - 1][0]
        seed_vals = [v for _, v in valid[:signal_period]]
        sig_line[seed_idx] = sum(seed_vals) / signal_period
        k = 2.0 / (signal_period + 1)
        for i in range(seed_idx + 1, n):
            if macd_line[i] != 0.0:
                sig_line[i] = macd_line[i] * k + sig_line[i - 1] * (1.0 - k)
            else:
                sig_line[i] = sig_line[i - 1]
    histogram = [macd_line[i] - sig_line[i] for i in range(n)]
    return macd_line, sig_line, histogram

def calc_sma(series, period):
    """Simple Moving Average. Same-length list."""
    s = clean(series)
    n = len(s)
    out = [0.0] * n
    for i in range(period - 1, n):
        out[i] = sum(s[i - period + 1:i + 1]) / period
    return out
```

Divergence detector: port the v4.4 `find_bullish_divergence` structure (3-bar local minima, ±5-bar price/RSI pivot pairing, price lower-low + RSI higher-low) **plus F2**: a pivot qualifies only if it is ≥ `PIVOT_MIN_DEPTH` below both neighbors' values proportionally, and consecutive accepted pivots are ≥3 bars apart.

## Appendix B — Proven REST throttle/retry (port VERBATIM, adapt names)

Field-tested in v4.4 against live Kraken (33 calls/scan, zero failures, restart-safe). Run inside `asyncio.to_thread`.

```python
import time, json, logging, urllib.request

MIN_CALL_GAP  = 0.6
FETCH_RETRIES = 2
_last_api_call = [0.0]

def _throttle():
    gap = time.time() - _last_api_call[0]
    if gap < MIN_CALL_GAP:
        time.sleep(MIN_CALL_GAP - gap)
    _last_api_call[0] = time.time()

def fetch_json(url, ua):
    """GET Kraken public endpoint with throttle + transient-aware retry.
    Returns parsed 'result' dict or None (after logging)."""
    raw = None
    for attempt in range(FETCH_RETRIES + 1):
        _throttle()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=25) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
        except Exception:
            if attempt == FETCH_RETRIES:
                logging.exception(f"fetch network error {url}")
                return None
            time.sleep(2.0 * (attempt + 1))
            continue
        errs = raw.get("error") or []
        if errs:
            transient = any(("Too many" in str(e)) or ("Unavailable" in str(e))
                            or ("EService" in str(e)) for e in errs)
            if transient and attempt < FETCH_RETRIES:
                time.sleep(3.0 * (attempt + 1))
                continue
            logging.error(f"Kraken API error {url}: {errs}")
            return None
        break
    return raw.get("result") if raw else None
```

## Appendix C — Pair table (ordermin/costmin live-verified 2026-07-03; refresh at runtime via AssetPairs — never hardcode as truth)

| REST pair | wsname (v1) | v2 symbol | display | ordermin | costmin |
|---|---|---|---|---|---|
| XXBTZUSD | XBT/USD | **BTC/USD** | BTC | 0.00005 | 0.5 |
| XETHZUSD | ETH/USD | ETH/USD | ETH | 0.001 | 0.5 |
| XXRPZUSD | XRP/USD | XRP/USD | XRP | 1.65 | 0.5 |
| SOLUSD | SOL/USD | SOL/USD | SOL | 0.06 | 0.5 |
| SUIUSD | SUI/USD | SUI/USD | SUI | 5 | 0.5 |
| XDGUSD | XDG/USD | **DOGE/USD** | DOGE | 50 | 0.5 |
| XLTCZUSD | LTC/USD | LTC/USD | LTC | 0.1 | 0.5 |
| LINKUSD | LINK/USD | LINK/USD | LINK | 0.55 | 0.5 |
| ADAUSD | ADA/USD | ADA/USD | ADA | 20 | 0.5 |
| AVAXUSD | AVAX/USD | AVAX/USD | AVAX | 0.5 | 0.5 |
| AAVEUSD | AAVE/USD | AAVE/USD | AAVE | 0.05 | 0.5 |
| UNIUSD | UNI/USD | UNI/USD | UNI | 1.5 | 0.5 |
| DOTUSD | DOT/USD | DOT/USD | DOT | 3.9 | 0.5 |
| BCHUSD | BCH/USD | BCH/USD | BCH | 0.01 | 0.5 |
| ALGOUSD | ALGO/USD | ALGO/USD | ALGO | 41 | 0.5 |

Note the two bolded rows — the v1→v2 rename traps. Build the normalization, then trust the per-symbol subscribe ACKs to verify it.

---

*End of spec. Builder: restate the plan, batch your questions, then M0.*
