"""Reconciler / gap-heal — diff, log loudly, then repair. SPEC §5, invariant 6.

On every (re)connect and hourly: REST-refetch the recent candles per pair/interval,
diff against the DB, log a RECON line (before/after) for any CLOSED-bar mismatch or
missing bar, then repair. Never silently fixes closed data. The forming bar (closed=0)
moves by design, so its upsert is routine (debug), not a RECON repair.
"""
import time
import logging

from . import config
from . import store

log = logging.getLogger("deepfield.recon")


def _rest_by_ws():
    return {p["ws"]: p["rest"] for p in config.PAIRS}


def gap_heal(conn, symbols, fetch_ohlc, intervals=config.INTERVALS, recent=10):
    """Refetch the last `recent` candles per symbol/interval, diff, repair.
    Returns repair count (missing or changed CLOSED bars)."""
    rest_by_ws = _rest_by_ws()
    repairs = 0
    for ws in symbols:
        rest = rest_by_ws.get(ws, ws)
        for interval in intervals:
            rows = fetch_ohlc(rest, interval)
            if not rows:
                log.warning("RECON gap-heal %s/%s: fetch failed", ws, interval)
                continue
            now = int(time.time())
            checked = updated = 0
            repairs_before = repairs
            for r in rows[-recent:]:
                ts = int(r[0])
                o, h, l, c, v = float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[6])
                closed = 1 if now >= ts + interval * 60 else 0
                checked += 1
                existing = conn.execute(
                    "SELECT o,h,l,c,v,closed FROM candles WHERE pair=? AND interval=? AND ts=?",
                    (ws, interval, ts),
                ).fetchone()
                new = (o, h, l, c, v, closed)
                if existing is None:
                    store.upsert_candle(conn, ws, interval, ts, o, h, l, c, v, closed)
                    if closed:
                        log.info("RECON %s/%s ts=%d MISSING->insert closed bar c=%.6g",
                                 ws, interval, ts, c)
                        repairs += 1
                    updated += 1
                elif tuple(existing) != new:
                    # forming bar moving is routine; a changed CLOSED bar is a real repair.
                    if existing[5] == 1 or closed == 1:
                        log.info("RECON %s/%s ts=%d CHANGED before=%s after=%s",
                                 ws, interval, ts, tuple(existing), new)
                        repairs += 1
                    store.upsert_candle(conn, ws, interval, ts, o, h, l, c, v, closed)
                    updated += 1
            conn.commit()
            # This summary ran 30x per hourly cycle at INFO, almost always the steady-state
            # "updated=0 repairs+=0" — pure noise that buried real events. Demote to debug
            # unless THIS pass actually repaired a missing/changed CLOSED bar (each such
            # repair already logs its own INFO line above; routine forming-bar 'updated'
            # churn is not a repair).
            level = log.info if repairs > repairs_before else log.debug
            level("RECON gap-heal %s/%s: checked=%d updated=%d repairs+=%d",
                  ws, interval, checked, updated, repairs)
    return repairs
