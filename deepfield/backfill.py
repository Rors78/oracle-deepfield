"""Cold/warm backfill: AssetPairs -> pairs table, OHLC -> candles. SPEC §6/§9, M1.

Cold start = full fetch (~30 throttled OHLC calls). Warm start = fetch only the
gap since MAX(ts) per pair/interval. Runs the sync REST helpers directly; the live
app wraps these in asyncio.to_thread.
"""
import time

from . import config
from . import store
from . import rest_client


def _ws_from_wsname(wsname, fallback):
    """Derive v2 symbol from v1 wsname via NORMALIZE (XBT->BTC, XDG->DOGE)."""
    if not wsname or "/" not in wsname:
        return fallback
    base, quote = wsname.split("/", 1)
    base = config.NORMALIZE.get(base, base)
    return f"{base}/{quote}"


def _index_assetpairs(ap):
    """Map both canonical key and altname -> entry for robust lookup."""
    idx = {}
    for key, entry in (ap or {}).items():
        idx[key] = entry
        alt = entry.get("altname")
        if alt:
            idx.setdefault(alt, entry)
    return idx


def run(db_path=None, full=True, log=print):
    db_path = db_path or config.DB_PATH
    conn = store.connect(db_path)
    rest_pairs = [p["rest"] for p in config.PAIRS]

    # 1) AssetPairs — ONE comma-joined call (§6 sharpening).
    ap = rest_client.fetch_assetpairs(rest_pairs)
    if ap is None:
        raise RuntimeError("AssetPairs fetch failed (see log)")
    idx = _index_assetpairs(ap)

    ws_by_rest = {}
    for p in config.PAIRS:
        rest = p["rest"]
        entry = idx.get(rest)
        if entry is None:
            log(f"WARN: {rest} not in AssetPairs response; using config fallbacks")
            ws = p["ws"]
            store.upsert_pair(conn, rest, ws, p["display"], p["ordermin"], p["costmin"], None)
        else:
            ws = _ws_from_wsname(entry.get("wsname"), p["ws"])
            ordermin = float(entry.get("ordermin", p["ordermin"]))
            costmin = float(entry.get("costmin", p["costmin"]))
            lot_dec = entry.get("lot_decimals")
            lot_dec = int(lot_dec) if lot_dec is not None else None
            store.upsert_pair(conn, rest, ws, p["display"], ordermin, costmin, lot_dec)
        ws_by_rest[rest] = ws
    conn.commit()

    # 2) OHLC backfill — per pair/interval.
    summary = {}
    for p in config.PAIRS:
        rest = p["rest"]
        ws = ws_by_rest[rest]
        for interval in config.INTERVALS:
            since = None if full else store.max_ts(conn, ws, interval)
            rows = rest_client.fetch_ohlc(rest, interval, since)
            if rows is None:
                log(f"WARN: OHLC fetch failed {rest} {interval}")
                summary[(ws, interval)] = store.candle_count(conn, ws, interval)
                continue
            now = int(time.time())
            for r in rows:
                ts = int(r[0])
                closed = 1 if now >= ts + interval * 60 else 0
                store.upsert_candle(
                    conn, ws, interval, ts,
                    float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[6]),
                    closed,
                )
            conn.commit()
            n = store.candle_count(conn, ws, interval)
            summary[(ws, interval)] = n
            log(f"{ws:10s} {interval:5d}  rows={len(rows):4d}  stored_total={n}")

    conn.close()
    return summary
