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
import json
import time
import sqlite3
import datetime
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import VERSION, config
from .. import store, engine
from ..profiles import FULL

HERE = os.path.dirname(os.path.abspath(__file__))
CONSOLE_HTML = os.path.join(HERE, "console.html")
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

def _ladder(conn):
    by = {}
    for oid, sym, vol, lev, entry, stop, txid in conn.execute(
            "SELECT id,symbol,volume,leverage,entry,stop,stop_txid FROM orders "
            "WHERE status='open' ORDER BY symbol,id"):
        d = by.setdefault(sym, {"fills": [], "pendings": [], "stopped": 0})
        d["fills"].append({"vol": vol, "lev": lev, "entry": entry, "stop": stop})
        if txid:
            d["stopped"] += 1
    for sym, entry, vol in conn.execute(
            "SELECT symbol,entry,volume FROM orders WHERE status='pending' ORDER BY symbol,id"):
        d = by.setdefault(sym, {"fills": [], "pendings": [], "stopped": 0})
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
    prices = live.get("prices", {})
    changes = live.get("chg", {})
    ladder = _ladder(conn)

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
        rungs = vols = bids = None
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
            fills = len(lad["fills"])
            size = round(lad["vol_sum"], 8)
            avg = lad["avg"]; stop = lad["stop"]
            if price is not None:
                pnl = sum((price - (f["entry"] or 0)) * (f["vol"] or 0) for f in lad["fills"])
                open_pnl += pnl

        # tier + status: positioned → BUY-signal → watch → idle
        if fills:
            tier, status, stStyle = "active", "BUY", "buy"
            near = stop and price and (price - stop) / stop < 0.03
            if near:
                status, stStyle = "NEAR-STOP", "near"
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
            "price": price, "chg": chg, "score": score, "denom": denom, "req": req,
            "sig": sig, "naReason": na, "lo": lo52, "hi": hi52, "pctLow": pct_low,
            "fills": fills, "size": size, "pnl": pnl, "avg": avg, "stop": stop,
            "stops": (lad["stopped"] if lad else 0),   # open fills with a resting stop_txid
            "bid": bid, "bids": bids, "rungs": rungs, "vols": vols,
            "lev": (lad["fills"][0]["lev"] if (lad and lad["fills"]) else 10),
            "cardStatus": (card.status if card else "---"),
        })

    # health
    now_utc = datetime.datetime.now(UTC)
    day0 = now_utc.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    wk0 = (now_utc - datetime.timedelta(days=now_utc.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0).isoformat()
    try:
        rday = store.realized_pnl_since(conn, day0)
        rweek = store.realized_pnl_since(conn, wk0)
    except Exception:
        rday = rweek = 0.0
    scov = conn.execute(
        "SELECT COUNT(*),COALESCE(SUM(CASE WHEN stop_txid IS NOT NULL AND stop_txid<>'' "
        "THEN 1 ELSE 0 END),0) FROM orders WHERE status='open'").fetchone()
    bid_count = conn.execute("SELECT COUNT(*) FROM orders WHERE status='pending'").fetchone()[0]
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
    # exec mode: the web process doesn't inherit the bot's env var, so prefer the
    # bot's persisted value, then infer from what the live orders were placed under.
    mode = live.get("mode") if live.get("_fresh") else None
    if not mode:
        row = conn.execute("SELECT mode FROM orders WHERE status IN('open','pending') "
                           "ORDER BY id DESC LIMIT 1").fetchone()
        mode = row[0] if row and row[0] else config.EXEC_MODE

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
        "peak": peak, "open_pnl": round(open_pnl, 2),
        "realized_day": round(rday, 2), "realized_week": round(rweek, 2),
        "swing_day": swing_day,
        "pos_count": scov[0], "bid_count": bid_count,
        "stops_covered": scov[1], "stops_total": scov[0],
        "margin_used": live.get("margin_used") if live.get("_fresh") else None,
        "free_margin": live.get("free_margin") if live.get("_fresh") else None,
        "margin_level": live.get("margin_level") if live.get("_fresh") else None,
        "capacity": live.get("capacity") if live.get("_fresh") else None,
        "recon_ok": recon.get("ok"), "recon_time": recon.get("time"),
        "now_mt": now_local.strftime("%H:%M:%S"), "now_utc": now_utc.strftime("%H:%M"),
        "day": now_local.strftime("%a %b %d").lower(),
    }
    regime = {
        "daily_pct": dp, "daily_left_s": dl, "weekly_pct": wp, "weekly_left_s": wl,
        "btc_price": (btc["price"] if btc else None), "btc_chg": (btc["chg"] if btc else None),
        "label": reg.get("label"), "note": reg.get("note"), "daily_rsi": reg.get("drsi"),
    }
    return {"health": health, "regime": regime, "pairs": pairs,
            "journal": _journal(conn), "v": VERSION}


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
            t = dt.astimezone(DENVER).strftime("%H:%M")
        return {"ok": bool(d.get("all_ok", True)), "time": t}
    except Exception:
        return {}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def build_pair(sym_display):
    ws = next((s for s in PAIR_LIST if DISPLAY[s] == sym_display), None)
    if ws is None:
        return None
    conn = _ro_conn()
    try:
        tss, closes = _daily_closes(conn, ws, 365)
    finally:
        conn.close()
    if not closes:
        return {"closes": [], "start_ts": None}
    return {"closes": [round(c, 6) for c in closes], "start_ts": tss[0]}


# ── HTTP handler ─────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        try:
            if path in ("/", "/index.html"):
                with open(CONSOLE_HTML, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif path == "/api/state":
                self._json(build_state())
            elif path.startswith("/api/pair/"):
                sym = path[len("/api/pair/"):]
                data = build_pair(sym)
                if data is None:
                    self._json({"error": "unknown pair"}, 404)
                else:
                    self._json(data)
            elif path == "/api/health":
                self._json({"ok": True, "v": VERSION})
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
