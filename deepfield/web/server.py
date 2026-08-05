"""DEEPFIELD web console server — stdlib only (no Flask), read-only.

Serves the redesigned operator console and a JSON API computed live from the DB:
  GET /                 the console (static HTML/CSS/JS)
  GET /api/state        health + regime + 15-pair field + journal  (polled ~4s)
  GET /api/pair/<SYM>   daily 52-week close series for the detail chart

Everything is derived from `deepfield.db` on each request (with a tiny cache):
signals via engine.evaluate over real candles, the ladder from the `orders` table,
journal from `journal`, charts from `candles`, realized P&L + peak equity from the
DB. Broker-only values (live equity/margin/tick price) come from the `web_live`
meta blob the bot persists; if it's stale/absent the dashboard still shows real
positions, P&L (marked from last close), charts and journal — nothing is faked.
"""
import os
import gzip
import hashlib
import json
import time
import sqlite3
import datetime
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import VERSION, config
from .. import store, engine
from ..profiles import FULL

# Below roughly one network payload, gzip's header and CPU cost more than it saves.
_MIN_GZIP_BYTES = 1400

# path -> ((mtime_ns, size), body, etag). Keyed on the stat so a live edit is
# picked up without a restart, which is how deck.html is actually developed.
_static_cache = {}
_static_lock = threading.Lock()

HERE = os.path.dirname(os.path.abspath(__file__))
CONSOLE_HTML = os.path.join(HERE, "console.html")   # v7 flight deck (kept at /v7)
DECK_HTML = os.path.join(HERE, "deck.html")         # v8 observatory deck (default)
UTC = datetime.timezone.utc
DENVER = None
try:
    from zoneinfo import ZoneInfo
    DENVER = ZoneInfo("America/Denver")
except Exception:                                    # pragma: no cover
    DENVER = UTC

DISPLAY = {p["ws"]: p["display"] for p in config.PAIRS}
PAIR_LIST = [p["ws"] for p in config.PAIRS]


# ── read-only DB access (never interferes with the live writer) ──────────────

def _ro_conn():
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True, timeout=5)
    conn.execute("PRAGMA busy_timeout=4000")
    return conn


# ── candle loading ───────────────────────────────────────────────────────────

def _series(conn, pair, interval, cols):
    return conn.execute(
        f"SELECT {cols} FROM candles WHERE pair=? AND interval=? AND closed=1 ORDER BY ts",
        (pair, interval)).fetchall()


def _sig(v, digits=8):
    """Round to significant digits, not decimal places. Decimal-place rounding
    (round(x, 6)) quantizes micro-price pairs onto the 1e-06 grid — SHIB at
    4.7e-06 collapsed to a flat 5e-06 line and PEPE vanished entirely."""
    return float(f"{v:.{digits}g}") if v is not None else None


def _spark(conn, pair, hours=24, max_pts=48):
    """15m closes for the book-row sparkline, downsampled. Display-only."""
    since = int(time.time()) - hours * 3600
    rows = conn.execute(
        "SELECT c FROM candles WHERE pair=? AND interval=15 AND ts>=? ORDER BY ts",
        (pair, since)).fetchall()
    if len(rows) > max_pts:
        step = len(rows) / max_pts
        rows = [rows[int(i * step)] for i in range(max_pts)] + [rows[-1]]
    return [_sig(r[0], 6) for r in rows]


def _daily_closes(conn, pair, limit=365):
    rows = conn.execute(
        "SELECT ts,c FROM candles WHERE pair=? AND interval=1440 ORDER BY ts DESC LIMIT ?",
        (pair, limit)).fetchall()
    rows = rows[::-1]
    return [r[0] for r in rows], [r[1] for r in rows]     # ts_list, close_list


def _card(conn, pair):
    """The real engine card for a pair (signals/score/denom/required/status)."""
    w = _series(conn, pair, 10080, "o,h,l,c,v")
    d = _series(conn, pair, 1440, "c")
    if not w or not d:
        return None
    wo = [r[0] for r in w]; wh = [r[1] for r in w]; wl = [r[2] for r in w]
    wc = [r[3] for r in w]; wv = [r[4] for r in w]; dc = [r[0] for r in d]
    return engine.evaluate(pair, (wo, wh, wl, wc, wv), (dc,), FULL)


# ── live broker values the bot persists (equity/margin/tick price) ───────────

def _web_live(conn):
    raw = store.meta_get(conn, "web_live")
    if not raw:
        return {}
    try:
        d = json.loads(raw)
    except Exception:
        return {}
    # stale after 90s (bot refreshes every 15s) — used to flag equity as live/derived
    d["_fresh"] = (time.time() - d.get("updated", 0)) < 90
    return d


# ── the ladder, straight from the orders table (same rows the TUI reads) ─────


def _active_mode(conn, live=None):
    """Which book is this console showing?

    A paper run is normally seeded by snapshotting the LIVE db (for its candle
    history), so one file legitimately holds a live P&L ledger AND live paper rows.
    Every aggregate below therefore has to say which one it means: on 2026-08-05 the
    deck showed a $200 simulated book a harvest total of +$2.96 and a cycle ledger of
    +$51.93 — every cent of it real-money history from a different book.

    Prefer the bot's own persisted value, then infer from the most recent working
    order, then the env. Same ladder the display badge already used; it is simply
    resolved once now and applied to the queries too."""
    live = live if live is not None else _web_live(conn)
    mode = live.get("mode") if live.get("_fresh") else None
    if not mode:
        row = conn.execute("SELECT mode FROM orders WHERE status IN('open','pending') "
                           "ORDER BY id DESC LIMIT 1").fetchone()
        mode = (row[0] if row and row[0] else None) or config.EXEC_MODE
    return mode


def _ladder(conn, mode=None):
    by = {}
    # one shape for every symbol, whether it entered here via a fill or via a bare
    # resting bid — a pair whose last fill closed still has bids, and a half-built
    # dict 500s the whole deck on the first counter the caller reads.
    blank = lambda: {"fills": [], "pendings": [], "stopped": 0, "harvesting": 0}
    # COALESCE(stop_prot, stop): stop distance is a RISK reading — it must show the
    # level a stop would actually rest at, which for a rung that proved the harvest
    # target is its ratcheted breakeven, not the chain invalidation level below it.
    for oid, sym, vol, lev, entry, stop, txid, ctxid in conn.execute(
            "SELECT id,symbol,volume,leverage,entry,COALESCE(stop_prot,stop),"
            "stop_txid,close_txid FROM orders "
            "WHERE status='open' AND (? IS NULL OR mode=?) ORDER BY symbol,id", (mode, mode)):
        d = by.setdefault(sym, blank())
        # close_txid on an open row = mid-harvest (tp-rung) or mid-flatten: the
        # stop is OFF because a resting sell owns the exit — a deliberate state
        # the UI must show as harvest, never as "unprotected".
        d["fills"].append({"vol": vol, "lev": lev, "entry": entry, "stop": stop,
                           "stop_txid": txid or "", "harv": bool(ctxid)})
        if txid:
            d["stopped"] += 1
        elif ctxid:
            d["harvesting"] += 1
    for sym, entry, vol in conn.execute(
            "SELECT symbol,entry,volume FROM orders WHERE status='pending' "
            "AND (? IS NULL OR mode=?) ORDER BY symbol,id", (mode, mode)):
        d = by.setdefault(sym, blank())
        d["pendings"].append({"price": entry, "vol": vol})   # every resting bid is real state
    for sym, d in by.items():
        # resting bids nearest-first (highest price fills first in a falling market)
        # — surface the whole stack, not just the lowest-id row, so the UI agrees
        # with the bid_count health figure that already counts them all
        d["pendings"].sort(key=lambda p: (p["price"] is None, -(p["price"] or 0.0)))
        d["pending"] = d["pendings"][0] if d["pendings"] else None
        fills = d["fills"]
        vsum = sum(f["vol"] or 0.0 for f in fills)
        d["vol_sum"] = vsum
        d["avg"] = (sum((f["vol"] or 0) * (f["entry"] or 0) for f in fills) / vsum) if vsum else None
        stops = [f["stop"] for f in fills if f["stop"] is not None]
        d["stop"] = max(stops) if stops else None
    return by


# ── clocks / countdowns ──────────────────────────────────────────────────────

def _bar_countdown(conn, interval_min):
    span = interval_min * 60
    row = conn.execute("SELECT MAX(ts) FROM candles WHERE interval=?", (interval_min,)).fetchone()
    if not row or row[0] is None:
        return 0, span
    now = time.time()
    open_ts = row[0]
    left = max(0, open_ts + span - now)
    pct = max(0, min(100, (now - open_ts) / span * 100))
    return int(left), int(pct)


# ── state assembly (cached briefly to avoid recompute on every poll) ─────────

_cache = {"t": 0, "state": None}
_lock = threading.Lock()


def build_state():
    with _lock:
        if _cache["state"] is not None and (time.time() - _cache["t"]) < 2.5:
            return _cache["state"]
    conn = _ro_conn()
    try:
        st = _assemble(conn)
    finally:
        conn.close()
    with _lock:
        _cache["t"] = time.time()
        _cache["state"] = st
    return st


def _assemble(conn):
    live = _web_live(conn)
    now = time.time()
    updated = live.get("updated")
    data_age = (now - updated) if updated else None
    fresh = bool(live.get("_fresh"))
    # W1: prices/chg get the SAME freshness gate equity/margin already have.
    # A stale blob (asyncio loop wedged, 12h-incident class) must not serve
    # frozen prices as current — fall back to the last daily close instead.
    prices = live.get("prices", {}) if fresh else {}
    changes = live.get("chg", {}) if fresh else {}
    # W7: per-pair tick ages (new blob key — absent in old blobs, degrade silently).
    # Ages were captured at blob-write time; add the blob's own age on top.
    tick_ages = live.get("tick_ages") or {}
    mode = _active_mode(conn, live)
    ladder = _ladder(conn, mode)
    # Kraken ground-truth open P/L (written by the bot loop): per-pair `net`/`avg` marked
    # by Kraken itself. Freshness-gated like prices — a stale blob falls back to the
    # ledger recompute below. DISPLAY[sym]-keyed by our own sym, same as PAIR_LIST.
    kr_pos = (live.get("kr_pos") or {}) if fresh else {}

    pairs = []
    open_pnl = 0.0
    for sym in PAIR_LIST:
        disp = DISPLAY[sym]
        card = _card(conn, sym)
        tss, closes = _daily_closes(conn, sym, 365)
        last_close = closes[-1] if closes else (card.price if card else None)
        price = prices.get(sym) or last_close
        chg = changes.get(sym)
        if chg is None and len(closes) >= 2 and closes[-2]:
            chg = round((closes[-1] / closes[-2] - 1) * 100, 1)
        # 52-week bounds + %-above-low come from the engine card (what the TUI shows,
        # weekly-close based); fall back to the daily close range if no card.
        if card and card.low_52w:
            lo52, hi52 = card.low_52w, card.high_52w
        else:
            lo52 = min(closes) if closes else None
            hi52 = max(closes) if closes else None

        lad = ladder.get(sym)
        pnl = None
        rungs = vols = bids = fstops = prot = None
        avg = stop = bid = None
        fills = 0
        size = 0.0
        if lad and lad["pendings"]:
            # up to 3 resting bids, nearest-first, each with its REAL per-order vol
            bids = [{"price": p["price"], "vol": p["vol"]} for p in lad["pendings"][:3]]
            bid = lad["pending"]["price"] if lad["pending"] else None
        if lad and lad["fills"]:
            rungs = [f["entry"] for f in lad["fills"]]
            vols = [f["vol"] for f in lad["fills"]]      # real per-fill vols (unequal under conviction sizing)
            fstops = [f["stop"] for f in lad["fills"]]   # W4: per-fill stops (they DIVERGE across chains)
            prot = [bool(f["stop_txid"]) for f in lad["fills"]]  # W3: per-fill protection truth
            fills = len(lad["fills"])
            size = round(lad["vol_sum"], 8)
            avg = lad["avg"]; stop = lad["stop"]
            if price is not None:
                pnl = sum((price - (f["entry"] or 0)) * (f["vol"] or 0) for f in lad["fills"])
                open_pnl += pnl
            # Prefer Kraken's own mark: `net` is the true unrealized (blended cost basis,
            # net of fees/rollover) — the ledger recompute above uses one collapsed entry
            # row and a tick price, so it drifts. Only override when the pair is present.
            krp = kr_pos.get(sym)
            if krp is not None:
                if krp.get("net") is not None:
                    pnl = krp["net"]
                if krp.get("avg"):
                    avg = krp["avg"]

        # W7: per-pair tick staleness (only when the blob carries tick_ages)
        t_age = tick_ages.get(sym)
        if t_age is not None and updated:
            t_age = t_age + max(0.0, now - updated)
        pair_stale = t_age is not None and t_age > config.STALE_SECS

        # tier + status (TUI semantics, ui._strip_state): positioned pairs read
        # BELOW-STOP → NEAR-STOP → STALE (fault) → BUY (only when the signal is
        # actually live) → HOLD. W6: held inventory is not a live signal.
        fault = False
        if fills:
            tier = "active"
            below = stop and price and price < stop
            near = stop and price and (price - stop) / stop < 0.03
            if below:
                # price under the stop = the stop should have FIRED — loudest state
                status, stStyle, fault = "BELOW-STOP", "near", True
            elif near:
                status, stStyle, fault = "NEAR-STOP", "near", True
            elif pair_stale:
                # stale data feeding a live position — fault tier (ui._tier)
                status, stStyle, fault = "STALE", "stale", True
            elif card and card.status == "BUY":
                status, stStyle = "BUY", "buy"
            else:
                status, stStyle = "HOLD", "hold"
        elif pair_stale:
            # stale WITHOUT a position stays where its score puts it (ui._tier)
            tier = "watch" if card and card.status in ("BUY", "WATCH") else "idle"
            status, stStyle = "STALE", "stale"
        elif card and card.status == "BUY":
            tier, status, stStyle = "buy", "BUY", "buy"
        elif card and card.status == "WATCH":
            tier, status, stStyle = "watch", "WCH", "wch"
        else:
            tier, status, stStyle = "idle", "idle", "idle"

        sig, na = [], {}
        score = denom = 0
        req = 5
        if card:
            st_map = {engine.FIRED: 1, engine.NOT: 0, engine.NA: -1}
            sig = [st_map[r.state] for r in card.results]
            na = {i: r.reason for i, r in enumerate(card.results) if r.state == engine.NA}
            score, denom, req = card.score, card.denom, card.required
        if card and card.pct_above_low is not None:
            pct_low = max(0, round(card.pct_above_low))
        elif price and lo52 and lo52 > 0:
            pct_low = max(0, round((price / lo52 - 1) * 100))
        else:
            pct_low = None

        pairs.append({
            "sym": disp, "tier": tier, "status": status, "stStyle": stStyle,
            "spark": _spark(conn, sym),
            "price": price, "chg": chg, "score": score, "denom": denom, "req": req,
            "sig": sig, "naReason": na, "lo": lo52, "hi": hi52, "pctLow": pct_low,
            "fills": fills, "size": size, "pnl": pnl, "avg": avg, "stop": stop,
            "stops": (lad["stopped"] if lad else 0),   # open fills with a resting stop_txid
            "harv": (lad["harvesting"] if lad else 0),  # fills mid-harvest (sell resting, stop off)
            "bid": bid, "bids": bids, "rungs": rungs, "vols": vols,
            "fstops": fstops, "prot": prot,            # per-fill stop / protection (W3/W4)
            "fault": fault,
            # W12: no hardcoded 10 — a 5x pair's default must be ITS max leverage
            "lev": (lad["fills"][0]["lev"] if (lad and lad["fills"])
                    else config.PER_PAIR_LEVERAGE.get(sym, 10)),
            "cardStatus": (card.status if card else "---"),
        })

    # health
    now_utc = datetime.datetime.now(UTC)
    day0 = now_utc.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    wk0 = (now_utc - datetime.timedelta(days=now_utc.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0).isoformat()
    try:
        rday = store.realized_pnl_since(conn, day0, mode)
        rweek = store.realized_pnl_since(conn, wk0, mode)
    except Exception:
        rday = rweek = 0.0
    scov = conn.execute(
        "SELECT COUNT(*),COALESCE(SUM(CASE WHEN stop_txid IS NOT NULL AND stop_txid<>'' "
        "THEN 1 ELSE 0 END),0),COALESCE(SUM(CASE WHEN (stop_txid IS NULL OR stop_txid='') "
        "AND close_txid IS NOT NULL THEN 1 ELSE 0 END),0) "
        "FROM orders WHERE status='open' AND (? IS NULL OR mode=?)", (mode, mode)).fetchone()
    bid_count = conn.execute("SELECT COUNT(*) FROM orders WHERE status='pending' "
                             "AND (? IS NULL OR mode=?)", (mode, mode)).fetchone()[0]
    peak = _num(store.meta_get(conn, "peak_equity"))
    equity = live.get("equity") if live.get("_fresh") else None
    # daily SWING = equity now − equity at the day's first read (baseline written by the
    # bot loop; read-only here). Complements realized 'day', which is $0 until a close.
    swing_day = None
    if equity is not None:
        dd, _, base = (store.meta_get(conn, "day_open_equity") or "").partition("|")
        if base and dd == day0[:10]:
            try:
                swing_day = round(equity - float(base), 2)
            except (TypeError, ValueError):
                swing_day = None
    recon = _recon(store.meta_get(conn, "last_recon"))
    # W10: open rows on symbols no longer in config.PAIRS render nowhere — surface them
    ghost = sorted(s for s, d in ladder.items() if d["fills"] and s not in DISPLAY)
    # exec mode: the web process doesn't inherit the bot's env var, so prefer the
    # bot's persisted value, then infer from what the live orders were placed under.

    now_local = datetime.datetime.now(DENVER)
    dl, dp = _bar_countdown(conn, 1440)
    wl, wp = _bar_countdown(conn, 10080)
    btc = next((p for p in pairs if p["sym"] == "BTC"), None)
    reg = _regime(conn)

    health = {
        "mode": mode, "halt": os.path.exists(config.HALT_FILE),
        "links": live.get("links", [True, True]) if live.get("_fresh") else None,
        "uptime_s": int(time.time() - live["started"]) if live.get("started") else None,
        "equity": equity, "equity_live": bool(live.get("_fresh") and equity is not None),
        # header open P/L: Kraken's TradeBalance `n` (net unrealized) when the blob is
        # fresh — the one number that always agrees with Kraken — else the ledger sum.
        "peak": peak,
        "open_pnl": (round(live["open_pnl"], 2)
                     if fresh and live.get("open_pnl") is not None
                     else round(open_pnl, 2)),
        "realized_day": round(rday, 2), "realized_week": round(rweek, 2),
        "swing_day": swing_day,
        "pos_count": scov[0], "bid_count": bid_count,
        "stops_covered": scov[1], "stops_total": scov[0],
        "harvesting": scov[2],                         # lots whose exit is a resting harvest sell
        "margin_used": live.get("margin_used") if live.get("_fresh") else None,
        "free_margin": live.get("free_margin") if live.get("_fresh") else None,
        "margin_level": live.get("margin_level") if live.get("_fresh") else None,
        "capacity": live.get("capacity") if live.get("_fresh") else None,
        "equity_series": _equity_series(conn),
        "recon_ok": recon.get("ok"), "recon_time": recon.get("time"),
        "recon_per_pair": recon.get("per_pair"),       # W5: which pair(s) mismatch
        "ghost_pairs": ghost,                          # W10
        # T/P cycle + fee drag (new blob keys — absent in old blobs, degrade to null)
        "tp_baseline": live.get("tp_baseline"), "tp_target": live.get("tp_target"),
        "tp_trough": live.get("tp_trough"),
        "fees_day": live.get("fees_day"), "fees_total": live.get("fees_total"),
        "fees_epoch": live.get("fees_epoch"),   # unix ts rollover accounting anchored (may be absent in old blobs)
        # Liq-buffer telemetry. Wave 1 has published these into the blob since
        # a5e2d39 but this allowlist never forwarded them, so the console showed
        # nothing — the operator's only price-space risk read was the TUI, the
        # journal and alerts. Gated on _fresh like the other equity-derived
        # numbers: a stale buffer is worse than a blank one. `stress` carries its
        # own poll and is passed through as-is.
        "buffer_liq_pct": live.get("buffer_liq_pct") if live.get("_fresh") else None,
        "buffer_call_pct": live.get("buffer_call_pct") if live.get("_fresh") else None,
        "eff_leverage": live.get("eff_leverage") if live.get("_fresh") else None,
        "defense_tier": live.get("defense_tier") if live.get("_fresh") else None,
        "stress": live.get("stress"),
        "now_mt": now_local.strftime("%H:%M:%S"), "now_utc": now_utc.strftime("%H:%M"),
        "day": now_local.strftime("%a %b %d").lower(),
    }
    regime = {
        "daily_pct": dp, "daily_left_s": dl, "weekly_pct": wp, "weekly_left_s": wl,
        "btc_price": (btc["price"] if btc else None), "btc_chg": (btc["chg"] if btc else None),
        "label": reg.get("label"), "note": reg.get("note"), "daily_rsi": reg.get("drsi"),
    }
    return {"health": health, "regime": regime, "pairs": pairs,
            "journal": _journal(conn), "tp_cycles": _tp_cycles(conn),
            "rung_harvest": _rung_harvest(conn, day0, mode), "v": VERSION,
            # W1: the client must KNOW when market data is frozen. data_age_s is
            # always sent (null only when no blob has ever been written).
            "data_stale": not fresh,
            "data_age_s": (round(data_age, 1) if data_age is not None else None)}


def _regime(conn):
    bw = _series(conn, "BTC/USD", 10080, "c")
    bd = _series(conn, "BTC/USD", 1440, "c")
    if not bw or not bd:
        return {}
    try:
        r = engine.regime([x[0] for x in bw], [x[0] for x in bd], FULL)
    except Exception:
        return {}
    notes = {"BULL": "above & rising w-ema200", "BEAR": "below w-ema200",
             "RECOVERY": "above, ema flattening", "NEUTRAL": "ema flat"}
    return {"label": (r.label or "?").lower(), "note": notes.get(r.label, ""),
            "drsi": round(r.daily_rsi) if r.daily_rsi else None}


def _tp_cycles(conn, n=30):
    """Completed T/P cycles for the ledger card (newest first). `profit` is TRUE
    trading profit (deposit-shifted baseline); flows null = pre-tracking backfill.
    Table appears with the first post-upgrade bot start — absent is fine."""
    try:
        rows = store.tp_cycles_list(conn, n)
    except sqlite3.OperationalError:
        return []
    out = []
    for ts, baseline, settled, flows, profit, note in rows:
        try:
            dt = datetime.datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            label = dt.astimezone(DENVER).strftime("%b %d · %H:%M")
        except Exception:
            label = (ts or "")[:16]
        out.append({"ts": ts, "label": label,
                    "baseline": round(baseline, 2), "settled": round(settled, 2),
                    "flows": (round(flows, 2) if flows is not None else None),
                    "profit": round(profit, 2), "note": note})
    return out


def _rung_harvest(conn, day0_iso, mode=None, n=6):
    """Per-rung harvest aggregates + the most recent banks (exit='tp-rung',
    operator 2026-07-29 'build it at 4%'). KEPT SEPARATE from the tp_cycles
    ledger on purpose: a cycle's profit is settled-minus-baseline, so rung banks
    inside a completed cycle are already inside that number — summing the two
    ledgers would double-count. `pct` is the price-space gain on the rung's own
    basis (pnl / entry*vol), the number the 4% target is set in."""
    try:
        total = store.realized_ledger_since(conn, "1970-01-01", kind="tp-rung", mode=mode)
        today = store.realized_ledger_since(conn, day0_iso, kind="tp-rung", mode=mode)
        recent = []
        for sym, pnl, cts, entry, vol in conn.execute(
                "SELECT symbol, CAST(json_extract(error,'$.pnl') AS REAL), "
                "json_extract(error,'$.closed_ts'), entry, volume FROM orders "
                "WHERE status='closed' AND json_valid(error)=1 "
                "AND json_extract(error,'$.exit')='tp-rung' AND (? IS NULL OR mode=?) "
                "ORDER BY json_extract(error,'$.closed_ts') DESC LIMIT ?", (mode, mode, n)):
            try:
                dt = datetime.datetime.fromisoformat(cts)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                label = dt.astimezone(DENVER).strftime("%b %d · %H:%M")
            except Exception:
                label = (cts or "")[:16]
            basis = (entry or 0) * (vol or 0)
            recent.append({"sym": (sym or "").replace("/USD", ""), "pnl": round(pnl or 0, 4),
                           "label": label,
                           "pct": (round(pnl / basis * 100, 1) if pnl is not None and basis > 0 else None)})
        # full bank history (unix ts) for the cumulative skim curve — capped at the
        # most recent 400 banks so the payload stays bounded as the ledger grows
        series = []
        for cts, pnl, sym in conn.execute(
                "SELECT json_extract(error,'$.closed_ts'), "
                "CAST(json_extract(error,'$.pnl') AS REAL), symbol FROM orders "
                "WHERE status='closed' AND json_valid(error)=1 "
                "AND json_extract(error,'$.exit')='tp-rung' AND (? IS NULL OR mode=?) "
                "ORDER BY json_extract(error,'$.closed_ts') DESC LIMIT 400", (mode, mode)):
            try:
                dt = datetime.datetime.fromisoformat(cts)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                series.append([int(dt.timestamp()), round(pnl or 0, 4),
                               (sym or "").replace("/USD", "")])
            except Exception:
                continue
        series.reverse()                                 # oldest-first for plotting
        by_sym = [{"sym": (s or "").replace("/USD", ""), "total": round(t or 0, 4), "n": n}
                  for s, t, n in conn.execute(
                      "SELECT symbol, SUM(CAST(json_extract(error,'$.pnl') AS REAL)), "
                      "COUNT(*) FROM orders WHERE status='closed' AND json_valid(error)=1 "
                      "AND json_extract(error,'$.exit')='tp-rung' AND (? IS NULL OR mode=?) "
                      "GROUP BY symbol ORDER BY 2 DESC LIMIT 6", (mode, mode))]
        return {"total": round(total["total"], 4), "n": total["n"], "wins": total["wins"],
                "today": round(today["total"], 4), "today_n": today["n"], "recent": recent,
                "series": series, "by_sym": by_sym}
    except Exception:
        # display-only block — a bad row must never take down /api/state
        return {"total": 0.0, "n": 0, "wins": 0, "today": 0.0, "today_n": 0,
                "recent": [], "series": [], "by_sym": []}


def _journal(conn, n=60):
    rows = store.recent_journal(conn, n)
    out = []
    for ts, kind, sym, text in rows:
        try:
            dt = datetime.datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            loc = dt.astimezone(DENVER)
            hms = loc.strftime("%H:%M:%S")
            day = loc.strftime("%A · %B %d")
        except Exception:
            hms, day = (ts or "")[11:19], ""
        out.append([hms, kind or "", (sym or ""), text or "", day])
    return out


def _equity_series(conn, hours=24, max_pts=180):
    """Last `hours` of persisted equity samples, downsampled for the sparkline.
    Table appears with the first post-upgrade bot start — absent is fine."""
    try:
        since = int(time.time()) - hours * 3600
        rows = conn.execute(
            "SELECT ts,equity FROM equity_history WHERE ts>=? ORDER BY ts",
            (since,)).fetchall()
    except sqlite3.OperationalError:
        return []
    if len(rows) > max_pts:
        step = len(rows) / max_pts
        rows = [rows[int(i * step)] for i in range(max_pts)] + [rows[-1]]
    return [[r[0], round(r[1], 4)] for r in rows]


def _recon(raw):
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        ts = d.get("ts")
        t = ""
        if ts:
            dt = datetime.datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            # W5: date + time — after multi-day uptime a bare "03:14" lies
            t = dt.astimezone(DENVER).strftime("%m-%d %H:%M")
        return {"ok": bool(d.get("all_ok", True)), "time": t,
                "per_pair": d.get("per_pair")}
    except Exception:
        return {}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# intraday detail intervals: minutes → row cap (payload-bounded, plot-sized)
PAIR_IVS = {15: 240, 60: 520, 1440: 366}


def build_pair(sym_display, iv=1440):
    ws = next((s for s in PAIR_LIST if DISPLAY[s] == sym_display), None)
    if ws is None:
        return None
    iv = iv if iv in PAIR_IVS else 1440
    conn = _ro_conn()
    try:
        # full OHLCV (candles + volume), newest-last; includes the forming bar
        rows = conn.execute(
            "SELECT ts,o,h,l,c,v FROM candles WHERE pair=? AND interval=? "
            "ORDER BY ts DESC LIMIT ?", (ws, iv, PAIR_IVS[iv])).fetchall()[::-1]
        days = [[r[0], _sig(r[1]), _sig(r[2]), _sig(r[3]),
                 _sig(r[4]), round(r[5], 4)] for r in rows]
        fills, pendings = [], []
        for (oid, ts, entry, stop, stop_txid, ctxid, vol, lev, notional, margin,
             score, req, status) in conn.execute(
                "SELECT id,ts,entry,COALESCE(stop_prot,stop),stop_txid,close_txid,"
                "volume,leverage,notional,margin,"
                "score,required,status FROM orders WHERE symbol=? "
                "AND status IN ('open','pending') ORDER BY id", (ws,)):
            # W3: per-fill stop AND stop_txid — protection is a per-fill truth,
            # never a hardcode ("live" only when a resting stop order exists).
            # harv: a resting harvest/flatten sell owns this fill's exit (stop off
            # BY DESIGN) — the detail sheet shows it brass, not danger-red.
            row = {"id": oid, "ts": ts, "entry": entry, "stop": stop,
                   "stop_txid": stop_txid or "", "harv": bool(ctxid),
                   "vol": vol, "lev": lev, "notional": notional,
                   "margin": margin, "score": score, "req": req}
            (fills if status == "open" else pendings).append(row)
    finally:
        conn.close()
    if not days:
        return {"closes": [], "start_ts": None, "iv": iv}
    return {"closes": [d[4] for d in days], "start_ts": days[0][0], "iv": iv,
            "days": days, "fills": fills, "pendings": pendings}


def build_equity(hours):
    """Equity samples for a chart window; hours=0 means the whole retained
    history (store prunes at 90 days). Read-only, display-only."""
    hours = max(0, min(24 * 120, hours))
    conn = _ro_conn()
    try:
        return _equity_series(conn, hours=hours or 24 * 120, max_pts=300)
    finally:
        conn.close()


def build_health():
    """W2: a REAL health verdict for external watchdogs (desktop script keys on it).
    ok is true ONLY when: the web_live blob is <120s old AND every link is up AND
    the DB answered. Never raises — on any failure it reports ok:false + error."""
    out = {"ok": False, "v": VERSION, "data_age_s": None, "links": None,
           "last_equity_sample_age_s": None, "db_ok": False, "ghost_pairs": []}
    try:
        conn = _ro_conn()
        try:
            live = _web_live(conn)
            updated = live.get("updated")
            age = (time.time() - updated) if updated else None
            out["data_age_s"] = round(age, 1) if age is not None else None
            out["links"] = live.get("links")
            try:
                row = conn.execute("SELECT MAX(ts) FROM equity_history").fetchone()
                if row and row[0]:
                    out["last_equity_sample_age_s"] = round(time.time() - row[0], 1)
            except sqlite3.OperationalError:
                pass                                     # table appears post-upgrade
            out["ghost_pairs"] = sorted(
                s for (s,) in conn.execute(
                    "SELECT DISTINCT symbol FROM orders WHERE status='open'")
                if s not in DISPLAY)
            out["db_ok"] = True                          # the queries answered
        finally:
            conn.close()
        links = out["links"]
        out["ok"] = bool(out["db_ok"]
                         and age is not None and age < 120
                         and links and all(links))
    except Exception as e:                               # never-500 contract
        out["error"] = str(e)
    return out


# ── HTTP handler ─────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, ctype, cache="no-store", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        # The state blob is ~37KB of highly repetitive JSON fetched every 4s by every
        # open console, and the deck itself is ~98KB — both compress about 4x. That
        # matters most for the phone on the LAN relay, which is a pure byte-pump and
        # forwards the encoding untouched. Costs ~1ms of CPU in the bot's process
        # against ~29KB saved per poll, per client.
        gz = False
        if (len(body) >= _MIN_GZIP_BYTES
                and "gzip" in (self.headers.get("Accept-Encoding") or "").lower()):
            try:
                body, gz = gzip.compress(body, 6), True
            except Exception:
                gz = False                      # never fail a response over compression
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if gz:
            self.send_header("Content-Encoding", "gzip")
        # Correct even though we send no-store: an intermediary that ignores that
        # must still key any cache on the encoding.
        self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or ()):
            self.send_header(k, v)
        self.send_header("Cache-Control", cache)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json; charset=utf-8")

    def _send_static(self, path_on_disk, ctype):
        """Serve a console page with revalidation.

        The deck is ~98KB (31KB gzipped) and was sent in full on every load, with
        Cache-Control: no-store forbidding the browser from even keeping a copy to
        revalidate against. It changes only when the file does, so hash it and let
        an unchanged reload cost 0 bytes — this is the page reloaded after every
        bot restart, and on a phone over wifi. no-cache (not no-store) is the
        correct directive: keep it, but always check with us first.

        The body is cached in-process keyed on (mtime, size), so a reload does not
        re-read 98KB from disk either. A file edited in place is picked up because
        its mtime moves — deck.html is edited live during development."""
        try:
            st = os.stat(path_on_disk)
            key = (st.st_mtime_ns, st.st_size)
            with _static_lock:
                hit = _static_cache.get(path_on_disk)
                if not hit or hit[0] != key:
                    with open(path_on_disk, "rb") as f:
                        body = f.read()
                    hit = (key, body, '"%s"' % hashlib.sha1(body).hexdigest()[:16])
                    _static_cache[path_on_disk] = hit
            _key, body, etag = hit
        except OSError:
            self._send(404, "not found", "text/plain")
            return
        if (self.headers.get("If-None-Match") or "").strip() == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        self._send(200, body, ctype, cache="no-cache", extra=(("ETag", etag),))

    def do_GET(self):
        path, _, query = self.path.partition("?")
        qs = {}
        for part in query.split("&"):
            k, _, v = part.partition("=")
            if k:
                qs[k] = v
        try:
            if path in ("/", "/index.html"):
                self._send_static(DECK_HTML, "text/html; charset=utf-8")
            elif path == "/v7":
                self._send_static(CONSOLE_HTML, "text/html; charset=utf-8")
            elif path == "/api/state":
                self._json(build_state())
            elif path.startswith("/api/pair/"):
                sym = path[len("/api/pair/"):]
                try:
                    iv = int(qs.get("iv", "1440"))
                except ValueError:
                    iv = 1440
                data = build_pair(sym, iv)
                if data is None:
                    self._json({"error": "unknown pair"}, 404)
                else:
                    self._json(data)
            elif path == "/api/equity":
                try:
                    hours = int(qs.get("hours", "24"))
                except ValueError:
                    hours = 24
                self._json(build_equity(hours))
            elif path == "/api/health":
                self._json(build_health())
            else:
                self._send(404, "not found", "text/plain")
        except BrokenPipeError:
            pass
        except Exception as e:                           # never 500 the whole page
            try:
                self._json({"error": str(e)}, 500)
            except Exception:
                pass

    def log_message(self, *a):
        pass                                             # quiet


def serve(host="127.0.0.1", port=8787, quiet=False):
    """Blocking serve loop. quiet=True suppresses stdout (used when embedded in the
    bot process, whose TUI owns the terminal)."""
    httpd = ThreadingHTTPServer((host, port), Handler)
    if not quiet:
        print(f"DEEPFIELD web console — http://{host}:{port}  (read-only · Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        if not quiet:
            print("\nweb console stopped")
    finally:
        httpd.server_close()
