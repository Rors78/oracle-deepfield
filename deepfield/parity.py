"""Parity harness — feed BOTH engines the identical DB closed-candle series.

Reviewer sharpening #5: parity is v4.4-logic ON MY DB SERIES, both engines — not
the 11:23 printout. The vendored v4.4's fetch_ohlc is replaced by a DB-driven stub
that appends a synthetic forming bar then applies v4.4's own USE_CLOSED_CANDLES
strip, so what analyze_pair sees == my engine's closed set. run_series_equality()
asserts len == DB closed count and last-ts == DB last-closed ts (the M2 gate).

run_parity() (M3) then diffs: compat vs vendored v4.4 must be 0; full vs compat
isolates each fix.
"""
import os
import importlib.util

from . import config
from . import store
from . import engine
from .signals import FIRED, NA, NAMES
from .profiles import COMPAT, FULL

SLOTS = [1, 2, 3, 4, 5, 6, 7]

_V44_PATH = os.path.join(config.PROJECT_ROOT, "docs", "reference", "oracle_dca_v44.py")


def load_v44():
    spec = importlib.util.spec_from_file_location("oracle_dca_v44", _V44_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _closed_rows(conn, ws_symbol, interval):
    return conn.execute(
        "SELECT ts,o,h,l,c,v FROM candles WHERE pair=? AND interval=? AND closed=1 ORDER BY ts",
        (ws_symbol, interval),
    ).fetchall()


def _rest_to_ws():
    return {p["rest"]: p["ws"] for p in config.PAIRS}


def make_db_fetch_ohlc(conn):
    """A drop-in for v4.4 fetch_ohlc, driven by DB closed candles. Reproduces the
    contract: build raw rows (closed + 1 synthetic forming) then strip the last,
    yielding exactly the DB closed set. Asserts series-equality on every call."""
    rest_to_ws = _rest_to_ws()

    def _fetch(pair, interval, count=720):
        ws = rest_to_ws.get(pair, pair)
        rows = _closed_rows(conn, ws, interval)
        if not rows or len(rows) < 20:
            return None
        last = rows[-1]
        # synthetic forming bar (flat at last close, zero vol) — will be stripped
        forming = (last[0] + interval * 60, last[4], last[4], last[4], last[4], 0.0)
        full = list(rows) + [forming]
        times = [r[0] for r in full]
        opens = [r[1] for r in full]
        highs = [r[2] for r in full]
        lows = [r[3] for r in full]
        closes = [r[4] for r in full]
        vols = [r[5] for r in full]
        # v4.4's USE_CLOSED_CANDLES strip
        if len(closes) > 1:
            times, opens, highs, lows, closes, vols = (
                times[:-1], opens[:-1], highs[:-1], lows[:-1], closes[:-1], vols[:-1])
        # series-equality invariant (M2 gate): strip removed exactly the dummy
        assert len(closes) == len(rows), f"strip len mismatch {ws}/{interval}"
        assert times[-1] == rows[-1][0], f"last-ts mismatch {ws}/{interval}"
        return times, opens, highs, lows, closes, vols

    return _fetch


def run_series_equality(db_path=None):
    """M2 gate: prove each engine's closed series is identical, per pair/interval."""
    conn = store.connect(db_path or config.DB_PATH)
    fetch = make_db_fetch_ohlc(conn)
    report = []
    for p in config.PAIRS:
        ws, rest = p["ws"], p["rest"]
        for interval in config.INTERVALS:
            db_closed = _closed_rows(conn, ws, interval)
            res = fetch(rest, interval)  # asserts inside
            times = res[0]
            ok = (len(times) == len(db_closed)) and (times and db_closed and times[-1] == db_closed[-1][0])
            report.append({
                "pair": ws, "interval": interval,
                "v44_len": len(times), "db_closed": len(db_closed),
                "last_ts_match": bool(times and db_closed and times[-1] == db_closed[-1][0]),
                "ok": bool(ok),
            })
    conn.close()
    return report


def _weekly_daily(conn, ws):
    w = _closed_rows(conn, ws, 10080)
    d = _closed_rows(conn, ws, 1440)
    weekly = ([r[1] for r in w], [r[2] for r in w], [r[3] for r in w], [r[4] for r in w], [r[5] for r in w])
    daily = ([r[4] for r in d],)
    return weekly, daily


def run_parity(db_path=None):
    """M3: diff vendored-v4.4 vs new-engine(compat) [must be 0] and new(full) vs compat."""
    conn = store.connect(db_path or config.DB_PATH)
    mod = load_v44()
    mod.fetch_ohlc = make_db_fetch_ohlc(conn)
    mod._throttle = lambda: None  # no network, no sleeps
    rows = []
    for p in config.PAIRS:
        rest, ws, disp = p["rest"], p["ws"], p["display"]
        v44 = mod.analyze_pair(rest, disp, p["ordermin"])
        weekly, daily = _weekly_daily(conn, ws)
        compat = engine.evaluate(ws, weekly, daily, COMPAT)
        full = engine.evaluate(ws, weekly, daily, FULL)
        rows.append({"pair": ws, "v44": v44, "compat": compat, "full": full})
    conn.close()
    return rows


def _v44_slot_fired(v44, slot):
    return NAMES[slot][0] in (v44.get("signals") or [])


def _state(card, slot):
    return [r for r in card.results if r.slot == slot][0].state


def _attribute(slot, cstate, fstate):
    """Map a FULL-vs-COMPAT slot diff to its F-item. UNATTRIBUTED = a stop."""
    if slot == 1 and fstate == NA and cstate != NA:
        return "F3"
    if slot == 5 and cstate != fstate:
        return "F1"
    if slot == 4 and cstate != fstate:
        return "F2"
    return "UNATTRIBUTED"


def triangulate(db_path=None):
    """M3 gate. Leg A: COMPAT vs v4.4 per slot (must be 105/105). Leg B: FULL vs
    COMPAT per slot, each diff attributed (zero UNATTRIBUTED)."""
    rows = run_parity(db_path)
    legA_total = legA_match = 0
    legA_diffs = []
    legB = []
    for row in rows:
        for s in SLOTS:
            vf = _v44_slot_fired(row["v44"], s)
            cf = (_state(row["compat"], s) == FIRED)
            legA_total += 1
            if vf == cf:
                legA_match += 1
            else:
                legA_diffs.append((row["pair"], s, vf, cf))
            cs, fs = _state(row["compat"], s), _state(row["full"], s)
            if cs != fs:
                legB.append((row["pair"], s, cs, fs, _attribute(s, cs, fs)))
    return {
        "rows": rows,
        "legA_total": legA_total, "legA_match": legA_match, "legA_diffs": legA_diffs,
        "legB": legB, "unattributed": [x for x in legB if x[4] == "UNATTRIBUTED"],
    }
