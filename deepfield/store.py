"""SQLite (WAL) persistence — SINGLE WRITER. SPEC §9, invariants 1 & 2.

Exactly one task/connection owns writes; everyone else uses the read helpers.
SQLite-WAL is ground truth; RAM-only state is a bug.

`candles.pair` holds the **v2 ws_symbol** (e.g. "BTC/USD"), so REST backfill (M1)
and WS live updates (M4) write the same rows. `ts` = bar OPEN (unix).
Close predicate (M1 sharpening): a bar is closed iff now >= ts + interval*60.
"""
import sqlite3
import time

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
"""


def connect(db_path):
    """Open the DB with WAL, init schema, return the connection."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
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
