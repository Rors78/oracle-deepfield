"""SQLite (WAL) persistence — SINGLE WRITER. SPEC §9, invariants 1 & 2.

Exactly one task/connection owns writes; everyone else uses the read helpers.
SQLite-WAL is ground truth; RAM-only state is a bug.

`candles.pair` holds the **v2 ws_symbol** (e.g. "BTC/USD"), so REST backfill (M1)
and WS live updates (M4) write the same rows. `ts` = bar OPEN (unix).
Close predicate (M1 sharpening): a bar is closed iff now >= ts + interval*60.
"""
import sqlite3
import time
import datetime

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
    notional REAL, margin REAL, risk_usd REAL,
    score INTEGER, required INTEGER,             -- entry conviction (rides down the ladder chain)
    txid TEXT, stop_txid TEXT, status TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS equity_history(
    ts INTEGER PRIMARY KEY,                      -- unix, sampled ~5min by the bot loop
    equity REAL                                  -- display-only (web sparkline)
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
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    # Additive migration for DBs created before the orders conviction columns
    # (existing rows -> NULL score/required -> ladder falls back to flat min).
    _ensure_columns(conn, "orders", [("score", "INTEGER"), ("required", "INTEGER")])
    conn.commit()
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
            "txid", "stop_txid", "status", "error"]
    cur = conn.execute(
        f"INSERT INTO orders({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
        [row.get(c) for c in cols],
    )
    conn.commit()
    return cur.lastrowid


def open_position_count(conn):
    """Positions this bot opened and believes are open (status='open')."""
    return conn.execute("SELECT COUNT(*) FROM orders WHERE status='open'").fetchone()[0]


def committed_position_count(conn):
    """Filled positions PLUS resting entry limits ('pending') — the count the
    MAX_OPEN_POSITIONS rail must use, since every pending limit can still fill."""
    return conn.execute(
        "SELECT COUNT(*) FROM orders WHERE status IN ('open','pending')").fetchone()[0]


def has_pending_entry(conn, symbol):
    """True if this symbol already has a resting (status='pending') entry order — its
    BUY thesis is already expressed on the book, so the boot arm needn't re-fire it."""
    return conn.execute(
        "SELECT 1 FROM orders WHERE symbol=? AND status='pending' LIMIT 1", (symbol,)
    ).fetchone() is not None


def realized_pnl_since(conn, since_iso):
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
    nothing' intent instead of exploding."""
    row = conn.execute(
        "SELECT COALESCE(SUM(CAST(json_extract(error,'$.pnl') AS REAL)),0) FROM orders "
        "WHERE status='closed' AND json_valid(error)=1 AND json_extract(error,'$.closed_ts') >= ?",
        (since_iso,),
    ).fetchone()
    return row[0] if row else 0.0


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
