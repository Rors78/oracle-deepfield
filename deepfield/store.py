"""SQLite (WAL) persistence — SINGLE WRITER. SPEC §9, invariants 1 & 2.

Exactly one task owns writes (consumes the event queue). Everyone else uses the
read helpers. SQLite-WAL is ground truth; RAM-only state is a bug.

TODO(M1): connection open (WAL pragma), schema init, AssetPairs upsert, backfill
warm-start (gap since MAX(ts) per pair/interval), candle upsert/close-flip, read
helpers. Writer loop lands at M5.
"""

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
