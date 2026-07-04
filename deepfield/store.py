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
