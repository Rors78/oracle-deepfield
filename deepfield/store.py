"""SQLite (WAL) persistence — SINGLE WRITER. SPEC §9, invariants 1 & 2.

Exactly one task/connection owns writes; everyone else uses the read helpers.
SQLite-WAL is ground truth; RAM-only state is a bug.

SINGLE WRITER is now ENFORCED, not just documented. The bot runs several writer
connections in one process — the ingest thread (candles), the off-loop poll_fills
thread (orders/stops), and the gap-heal threads (backfilled candles) — and WAL
lets only ONE connection hold an open write transaction at a time; the rest used
to collide as `database is locked` (chronic; a flood during the 2026-07-13 storm).
`_WriterConn` funnels every write through one process-global RLock: it grabs the
lock on a connection's first write statement and releases it on commit/rollback/
close, so the lock spans the whole transaction and no two connections ever write
at once. Readers (WAL-concurrent) never take the lock. busy_timeout stays as a
backstop for out-of-process writers (e.g. a --reconcile CLI run alongside live).

`candles.pair` holds the **v2 ws_symbol** (e.g. "BTC/USD"), so REST backfill (M1)
and WS live updates (M4) write the same rows. `ts` = bar OPEN (unix).
Close predicate (M1 sharpening): a bar is closed iff now >= ts + interval*60.
"""
import sqlite3
import threading
import time
import datetime

# Process-global write serializer. RLock (not Lock) so a thread that somehow
# re-enters a write path can't self-deadlock; the per-connection _holds_write
# guard keeps acquire/release balanced (one net acquire per open transaction).
_WRITE_LOCK = threading.RLock()
_WRITE_VERBS = frozenset(("INSERT", "UPDATE", "DELETE", "REPLACE",
                          "CREATE", "ALTER", "DROP"))


def _is_write(sql):
    """True if the statement opens/extends a write transaction (leading verb).
    SELECT/PRAGMA/WITH-read stay lock-free so readers never serialize."""
    s = sql.lstrip()
    if not s:
        return False
    return s.split(None, 1)[0].upper() in _WRITE_VERBS


class _WriterConn(sqlite3.Connection):
    """A connection that holds _WRITE_LOCK for the duration of any write
    transaction. All writes in this codebase go through conn.execute(...) (no
    cursors, no `with conn:`, no explicit BEGIN — verified), so intercepting
    execute/commit here covers every write with no call-site changes."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._holds_write = False

    def _acquire(self):
        if not self._holds_write:
            _WRITE_LOCK.acquire()
            self._holds_write = True

    def _release(self):
        if self._holds_write:
            self._holds_write = False
            _WRITE_LOCK.release()

    def execute(self, sql, *a, **k):
        if _is_write(sql):
            self._acquire()
        return super().execute(sql, *a, **k)

    def executemany(self, sql, *a, **k):
        if _is_write(sql):
            self._acquire()
        return super().executemany(sql, *a, **k)

    def executescript(self, script):
        # A script may contain writes (schema/migrations); hold the lock across it.
        self._acquire()
        return super().executescript(script)

    def commit(self):
        try:
            super().commit()
        finally:
            self._release()

    def rollback(self):
        try:
            super().rollback()
        finally:
            self._release()

    def close(self):
        # Backstop: releases the lock if a write path errored before commit,
        # so an abandoned open transaction can never wedge every other writer.
        try:
            super().close()
        finally:
            self._release()

# §9 schema + Q2 ruling: pairs gains lot_decimals (AssetPairs). ts = bar OPEN.
SCHEMA = """
CREATE TABLE IF NOT EXISTS candles(
    pair TEXT, interval INTEGER, ts INTEGER,        -- ts = bar OPEN, unix
    o REAL, h REAL, l REAL, c REAL, v REAL,
    closed INTEGER,
    PRIMARY KEY (pair, interval, ts)
);
CREATE TABLE IF NOT EXISTS pairs(
    rest_pair TEXT PRIMARY KEY, ws_symbol TEXT, display TEXT,
    ordermin REAL, costmin REAL, lot_decimals INTEGER, updated_ts INTEGER
);
CREATE TABLE IF NOT EXISTS alerts(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, symbol TEXT,
    price REAL, score INTEGER, denom INTEGER, signals TEXT,
    kind TEXT                                       -- confirmed | provisional | test
);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS journal(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, symbol TEXT, text TEXT
);
CREATE TABLE IF NOT EXISTS orders(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, symbol TEXT, margin_pair TEXT,
    side TEXT, ordertype TEXT, mode TEXT,        -- off | paper | live | validate
    entry REAL, stop REAL, volume REAL, leverage INTEGER,
    stop_prot REAL,                              -- ratcheted PROTECTIVE stop (see below)
    notional REAL, margin REAL, risk_usd REAL,
    score INTEGER, required INTEGER,             -- entry conviction (rides down the ladder chain)
    txid TEXT, stop_txid TEXT, status TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS equity_history(
    ts INTEGER PRIMARY KEY,                      -- unix, sampled ~5min by the bot loop
    equity REAL                                  -- display-only (web sparkline)
);
CREATE TABLE IF NOT EXISTS tp_cycles(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,                            -- completion time, UTC iso
    baseline REAL NOT NULL,                      -- baseline at completion (deposit-shifted)
    settled REAL NOT NULL,                       -- post-flatten equity (= next baseline)
    flows REAL,                                  -- net external USD flow in-cycle (NULL = untracked era)
    profit REAL NOT NULL,                        -- true trading profit banked this cycle
    note TEXT
);
"""


def _ensure_columns(conn, table, coldefs):
    """Idempotent additive migration (SQLite has no ADD COLUMN IF NOT EXISTS):
    ALTER-add any column in coldefs the live table is missing, leaving existing
    rows NULL for it. coldefs: [(name, decl), ...]. New/additive only — never
    drops or retypes, so it's safe to run on every connect."""
    have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, decl in coldefs:
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def connect(db_path):
    """Open the DB with WAL, init schema, return the connection.

    busy_timeout matters from M6 on: the live writer and the gap-heal thread
    (its own connection, per M4) now contend on the same WAL file on every
    (re)connect. Without it, a concurrent write throws "database is locked"
    instead of waiting the other side out.

    30s (was 5s): the per-tick candle writer got starved past 5s during WRITE
    BURSTS — a boot reconcile re-arming dozens of stops, or a seeding pass —
    each of which serially hammers the executor's connection for 10-30s while
    also driving frequent WAL auto-checkpoints. 5s expired mid-burst and skipped
    candle writes (thousands during the 2026-07-13 flatten storm; a smaller boot
    burst every restart). 30s waits any realistic burst out; steady state never
    contends, so it never actually blocks that long. A lock still held past 30s
    is a real bug worth surfacing, not masking further.
    """
    conn = sqlite3.connect(db_path, factory=_WriterConn)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.executescript(SCHEMA)
        # Additive migration for DBs created before the orders conviction columns
        # (existing rows -> NULL score/required -> ladder falls back to flat min).
        # userref (audit 2026-07-13): our client id sent on every live AddOrder so an
        # order whose transport failed AMBIGUOUSLY (may or may not have landed) can be
        # re-identified on Kraken instead of becoming an untracked naked position.
        # close_txid (limit flatten, operator 2026-07-21): txid of the pair's resting
        # post-only T/P close order. A row carrying one is OWNED by the flatten —
        # reprotect/reconcile must not re-arm a stop beside it (stop + resting sell
        # would double the sell volume -> short on fill+trigger).
        # stop_prot (rung stop-ratchet, 2026-07-29): the PROTECTIVE stop price, once a
        # rung has proved it can reach the harvest target. Deliberately NOT the `stop`
        # column: `stop` is the chain's INVALIDATION level, which _reladder hands to
        # _place_ladder_rung as the next rung's floor and sizing denominator — pinning
        # it up to breakeven would read as "ladder floor reached" and silently stop
        # accumulating on exactly the pairs that are working. Protection paths
        # (reprotect, reconcile re-place) read COALESCE(stop_prot, stop); ladder
        # geometry keeps reading `stop`. NULL = never ratcheted.
        _ensure_columns(conn, "orders", [("score", "INTEGER"), ("required", "INTEGER"),
                                         ("userref", "INTEGER"), ("close_txid", "TEXT"),
                                         ("stop_prot", "REAL")])
        conn.commit()
    except Exception:
        conn.close()   # _WriterConn.close() frees the write lock the schema writes took
        raise
    return conn


def upsert_pair(conn, rest_pair, ws_symbol, display, ordermin, costmin, lot_decimals):
    conn.execute(
        """INSERT INTO pairs(rest_pair, ws_symbol, display, ordermin, costmin, lot_decimals, updated_ts)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(rest_pair) DO UPDATE SET
             ws_symbol=excluded.ws_symbol, display=excluded.display,
             ordermin=excluded.ordermin, costmin=excluded.costmin,
             lot_decimals=excluded.lot_decimals, updated_ts=excluded.updated_ts""",
        (rest_pair, ws_symbol, display, ordermin, costmin, lot_decimals, int(time.time())),
    )


def upsert_candle(conn, pair, interval, ts, o, h, l, c, v, closed):
    conn.execute(
        """INSERT INTO candles(pair, interval, ts, o, h, l, c, v, closed)
           VALUES(?,?,?,?,?,?,?,?,?)
           ON CONFLICT(pair, interval, ts) DO UPDATE SET
             o=excluded.o, h=excluded.h, l=excluded.l, c=excluded.c, v=excluded.v,
             closed=excluded.closed""",
        (pair, interval, ts, o, h, l, c, v, closed),
    )


def max_ts(conn, pair, interval):
    row = conn.execute(
        "SELECT MAX(ts) FROM candles WHERE pair=? AND interval=?", (pair, interval)
    ).fetchone()
    return row[0]


def max_closed_ts(conn, pair, interval):
    """MAX(ts) of a CLOSED bar for (pair, interval), or None. Seeds the per-close
    fire-dedup at boot so a restart doesn't re-fire a bar that already closed (and
    fired) before the restart."""
    row = conn.execute(
        "SELECT MAX(ts) FROM candles WHERE pair=? AND interval=? AND closed=1",
        (pair, interval),
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def candle_count(conn, pair, interval):
    row = conn.execute(
        "SELECT COUNT(*) FROM candles WHERE pair=? AND interval=?", (pair, interval)
    ).fetchone()
    return row[0]


def flip_closed(conn, pair, interval, ts):
    """Mark a bar closed (0->1) without touching its OHLCV (already kept current
    by CandleUpdate upserts). Returns rowcount — 0 means the row wasn't there yet,
    which the caller should treat as a gap for the reconciler, not a crash."""
    cur = conn.execute(
        "UPDATE candles SET closed=1 WHERE pair=? AND interval=? AND ts=? AND closed=0",
        (pair, interval, ts),
    )
    return cur.rowcount


def load_weekly_daily_closed(conn, symbol):
    """Closed-only series shaped for engine.evaluate(): weekly=(wo,wh,wl,wc,wvol),
    daily=(dc,). Invariant 3 — the engine reads persisted state, not the stream."""
    w = conn.execute(
        "SELECT o,h,l,c,v FROM candles WHERE pair=? AND interval=10080 AND closed=1 ORDER BY ts",
        (symbol,),
    ).fetchall()
    d = conn.execute(
        "SELECT c FROM candles WHERE pair=? AND interval=1440 AND closed=1 ORDER BY ts",
        (symbol,),
    ).fetchall()
    weekly = ([r[0] for r in w], [r[1] for r in w], [r[2] for r in w], [r[3] for r in w], [r[4] for r in w])
    daily = ([r[0] for r in d],)
    return weekly, daily


def get_forming(conn, symbol, interval):
    """The current forming (closed=0) bar for symbol/interval, or None."""
    row = conn.execute(
        "SELECT ts,o,h,l,c,v FROM candles WHERE pair=? AND interval=? AND closed=0",
        (symbol, interval),
    ).fetchone()
    if row is None:
        return None
    return {"ts": row[0], "o": row[1], "h": row[2], "l": row[3], "c": row[4], "v": row[5]}


def insert_alert(conn, ts_iso, symbol, price, score, denom, signals, kind):
    conn.execute(
        "INSERT INTO alerts(ts, symbol, price, score, denom, signals, kind) VALUES(?,?,?,?,?,?,?)",
        (ts_iso, symbol, price, score, denom, "|".join(signals), kind),
    )
    conn.commit()


def insert_order(conn, row):
    """row: dict of the orders columns. Returns the new order id."""
    cols = ["ts", "symbol", "margin_pair", "side", "ordertype", "mode", "entry", "stop",
            "volume", "leverage", "notional", "margin", "risk_usd", "score", "required",
            "txid", "stop_txid", "status", "error", "userref"]
    cur = conn.execute(
        f"INSERT INTO orders({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
        [row.get(c) for c in cols],
    )
    conn.commit()
    return cur.lastrowid


def open_position_count(conn, mode=None):
    """Positions this bot opened and believes are open (status='open').
    mode (audit 2026-07-13 M6): scope to one exec mode so paper rows mixed into
    this DB can never leak into live money-path decisions. None = all modes
    (legacy display callers)."""
    if mode:
        return conn.execute("SELECT COUNT(*) FROM orders WHERE status='open' AND mode=?",
                            (mode,)).fetchone()[0]
    return conn.execute("SELECT COUNT(*) FROM orders WHERE status='open'").fetchone()[0]


def committed_position_count(conn, mode=None):
    """Filled positions PLUS resting entry limits ('pending') — the count the
    MAX_OPEN_POSITIONS rail must use, since every pending limit can still fill.
    mode: scope to one exec mode (see open_position_count)."""
    if mode:
        return conn.execute(
            "SELECT COUNT(*) FROM orders WHERE status IN ('open','pending') AND mode=?",
            (mode,)).fetchone()[0]
    return conn.execute(
        "SELECT COUNT(*) FROM orders WHERE status IN ('open','pending')").fetchone()[0]


def has_pending_entry(conn, symbol, mode=None):
    """True if this symbol already has a resting (status='pending') entry order — its
    BUY thesis is already expressed on the book, so the boot arm needn't re-fire it.
    mode: scope to one exec mode (see open_position_count)."""
    if mode:
        return conn.execute(
            "SELECT 1 FROM orders WHERE symbol=? AND status='pending' AND mode=? LIMIT 1",
            (symbol, mode)).fetchone() is not None
    return conn.execute(
        "SELECT 1 FROM orders WHERE symbol=? AND status='pending' LIMIT 1", (symbol,)
    ).fetchone() is not None


def realized_pnl_since(conn, since_iso, mode=None):
    """Realized P&L for positions CLOSED since `since_iso`. Buckets by the close time
    (error.closed_ts), NOT the entry ts — the daily/weekly loss caps ask "how much did
    I lose since day0/wk0", a realization-date question (a trade entered days ago and
    stopped out today must count toward today). Rows without a recorded pnl/closed_ts
    (manual closes, unresolved) contribute nothing.

    The `error` column is polymorphic — JSON for stop-exit P&L, but plain text for
    manual/other closes (e.g. 'closed manually by operator'). json_extract RAISES
    'malformed JSON' on a non-JSON value, which crashes the whole query and every
    caller (the rails loss caps AND the v6 exec snapshot). Guard with json_valid so
    a plain-text row is simply skipped — matching the documented 'contributes
    nothing' intent instead of exploding.


    MODE-SCOPED (2026-08-05). A paper run is normally seeded by snapshotting the live
    DB, so one file holds a live P&L ledger AND live paper rows — and this feeds the
    LOSS RAILS. Unscoped, a paper book's daily cap would count real-money stop losses
    (and vice versa). Default None keeps the old unscoped behaviour for any caller
    that has not opted in.

    STOP EXITS ONLY, deliberately. This feeds the daily/weekly loss-limit rails, whose
    question is "how much did the stops take out of me" — the flatten is a harvest, not
    a loss event, and the tp_cycles ledger already books it against the baseline. Once
    the flatten started recording per-lot P&L too (2026-07-27) an unfiltered SUM would
    have silently changed what a live risk rail counts, so the kind is pinned here.
    Use realized_ledger_since() for the whole track record."""
    row = conn.execute(
        "SELECT COALESCE(SUM(CAST(json_extract(error,'$.pnl') AS REAL)),0) FROM orders "
        "WHERE status='closed' AND json_valid(error)=1 "
        "AND json_extract(error,'$.exit')='stop' AND json_extract(error,'$.closed_ts') >= ? "
        "AND (? IS NULL OR mode=?)",
        (since_iso, mode, mode),
    ).fetchone()
    return row[0] if row else 0.0


def realized_ledger_since(conn, since_iso, kind=None, mode=None):
    """The per-lot realized track record: every priced exit (stop AND tp-flatten), the
    thing win-rate / expectancy / profit-factor are computed from. `kind` filters to one
    exit type; None means all. Returns {'n','total','wins','losses','avg'} — zeros on an
    empty ledger, never a division by zero.

    Separate from realized_pnl_since() on purpose: that one is a risk rail pinned to
    stop-outs, this one is the accounting view. Same json_valid guard, so plain-text
    'error' rows (manual closes, unpriceable exits) contribute nothing."""
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(CAST(json_extract(error,'$.pnl') AS REAL)),0), "
        "       COALESCE(SUM(CAST(json_extract(error,'$.pnl') AS REAL) > 0),0) "
        "FROM orders WHERE status='closed' AND json_valid(error)=1 "
        "  AND json_extract(error,'$.pnl') IS NOT NULL "
        "  AND json_extract(error,'$.closed_ts') >= ? "
        "  AND (? IS NULL OR json_extract(error,'$.exit') = ?) "
        "  AND (? IS NULL OR mode = ?)",
        (since_iso, kind, kind, mode, mode),
    ).fetchone()
    n, total, wins = (row[0] or 0), (row[1] or 0.0), (row[2] or 0)
    return {"n": n, "total": total, "wins": wins, "losses": n - wins,
            "avg": (total / n) if n else 0.0}


def equity_snapshot(conn, equity, min_gap_s=300, keep_days=90):
    """Append a display-only equity sample (web sparkline series), at most one
    per min_gap_s; prunes samples older than keep_days. Never raises past the
    caller's display-only guard — no trading effect."""
    now = int(time.time())
    row = conn.execute("SELECT MAX(ts) FROM equity_history").fetchone()
    if row and row[0] and now - row[0] < min_gap_s:
        return
    conn.execute("INSERT OR REPLACE INTO equity_history(ts,equity) VALUES(?,?)",
                 (now, float(equity)))
    conn.execute("DELETE FROM equity_history WHERE ts < ?", (now - keep_days * 86400,))
    conn.commit()


def meta_get(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def meta_set(conn, key, value):
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, str(value)))
    conn.commit()


def get_pair_info(conn, ws_symbol):
    """F8 needs live ordermin/costmin/lot_decimals — read from the pairs table
    (populated from AssetPairs at backfill), keyed by ws_symbol here."""
    row = conn.execute(
        "SELECT ordermin, costmin, lot_decimals, display FROM pairs WHERE ws_symbol=?",
        (ws_symbol,),
    ).fetchone()
    if row is None:
        return None
    return {"ordermin": row[0], "costmin": row[1], "lot_decimals": row[2], "display": row[3]}


def recent_alerts(conn, n=5):
    """Last n ledger rows, newest first — for the UI alert tail (§8)."""
    return conn.execute(
        "SELECT ts, symbol, price, score, denom, signals, kind FROM alerts ORDER BY id DESC LIMIT ?",
        (n,),
    ).fetchall()


def tp_cycle_add(conn, ts, baseline, settled, flows, profit, note=None):
    """Record one completed T/P cycle in the ledger (operator 2026-07-21: 'a t/p
    ledger somewhere'). `profit` is TRUE trading profit: settled - baseline where
    the baseline was deposit-shifted along the way, so external money never reads
    as trading gain. flows is informational (net USD in/out during the cycle);
    NULL flows marks a backfilled pre-tracking row."""
    conn.execute(
        "INSERT INTO tp_cycles(ts, baseline, settled, flows, profit, note) VALUES(?,?,?,?,?,?)",
        (ts, baseline, settled, flows, profit, note))
    conn.commit()


def tp_cycles_list(conn, n=50):
    """Newest-first completed T/P cycles for the console ledger card."""
    return conn.execute(
        "SELECT ts, baseline, settled, flows, profit, note FROM tp_cycles "
        "ORDER BY id DESC LIMIT ?", (n,)).fetchall()


def journal(conn, kind, symbol, text):
    """Append one display-truth event to the journal (v6 SURVEY, JOURNAL view).

    DISPLAY-ONLY: the alerts table stays cooldown ground truth; this narrates the
    system for the operator's eyes. This raw writer commits and MAY raise (locked
    DB, disk). Every emit site in the money path MUST call it through a try/except
    wrapper (executor._journal / ingest._journal) so a journal failure can NEVER
    delay or drop a fill, stop, or order — the same isolation rule as the
    dispatch/alerter fix. Regression: test_journal_failure_never_blocks_fill."""
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn.execute("INSERT INTO journal(ts, kind, symbol, text) VALUES(?,?,?,?)",
                 (ts, kind, symbol, text))
    conn.commit()


def recent_journal(conn, n=200):
    """Last n journal rows, newest first — the UI JOURNAL view + FIELD 'latest'."""
    return conn.execute(
        "SELECT ts, kind, symbol, text FROM journal ORDER BY id DESC LIMIT ?", (n,),
    ).fetchall()


def last_alert_ts(conn, symbol, kind="confirmed"):
    """F10: unix seconds of the most recent alert of `kind` for symbol, or None.
    Disk (this table) is ground truth for the cooldown — survives restarts.
    kind='confirmed' is the spec's F10 ledger; 'provisional' reuses the same
    per-symbol cooldown mechanism when PROVISIONAL_ALERTS is enabled."""
    row = conn.execute(
        "SELECT ts FROM alerts WHERE symbol=? AND kind=? ORDER BY ts DESC LIMIT 1",
        (symbol, kind),
    ).fetchone()
    if row is None:
        return None
    import datetime
    dt = datetime.datetime.fromisoformat(row[0])
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()
