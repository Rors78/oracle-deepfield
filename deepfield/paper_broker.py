"""Simulated Kraken private API — the exchange half of paper mode.

Paper mode used to short-circuit inside the executor: `_place_entry` wrote the
order row straight to status='open' with a synthetic PAPER-* txid and a
PAPER-STOP-* stop that could never trigger. Everything downstream of a fill
(poll_fills, the protective-stop reconcile, continuous laddering, chain seeding,
the +4% rung harvest, the stop-ratchet, T/P, reverse gear) is gated live-only, so
none of it ever ran and the paper book could only ever grow.

This module supplies the missing counterparty. Every private Kraken endpoint the
bot uses reaches the network through exactly ONE function — broker.private() — so
attaching a simulated exchange is a single rebind: broker.private -> _dispatch().
broker's own response handling stays in the path and therefore stays exercised
(open_orders unwrapping 'open', equity()'s e->eb->tb ladder, CancelOrderBatch's
indexed-bracket encoding, the Ledgers pagination walk).

Prices come from the WS-fed `candles` table (the forming 15m bar), so fills track
the real market with NO extra REST load and no private traffic whatsoever — the
account rate limit, which a competition bot may be using, is never touched.

NO-LOOK-AHEAD RULE. A resting order may only be filled by price action that
happened AFTER it was placed. The current price (forming-bar close) is 'now' and
is always usable; a bar's high/low EXTREMES are usable only once the bar OPENED
after the order was placed. Without this, an order placed mid-bar would instantly
fill against a wick that had already printed — the classic look-ahead bug, which
here would fabricate fills the live bot could never have won.

LONG-ONLY INVARIANT. A sell fills only against long volume actually held; any
excess is dropped (loudly) rather than opening a short. A resting orphan stop
whose position already closed therefore cannot invent a short position — it just
goes nowhere until the executor's reconcile cancels it, which is the behavior the
orphan-stop guard exists to produce.

FULL FILLS ARE DELIBERATE — do not "fix" this into partial fills. poll_fills has
careful partial-fill handling ("partial N while resting — canceling remainder")
which never runs in paper, and that absence looks like a fidelity gap. It is not.
Every DEEPFIELD entry floors up to the EXCHANGE MINIMUM before SIZE_MULT, so a
rung is a ~$4-9 order against a book with orders of magnitude more depth at any
touched level. Such an order does not partially fill in practice; modeling one
that does would make the simulator LESS faithful to this strategy, not more, and
would understate accumulation for a reason that has nothing to do with the market.
A partial CLOSE is a different thing and is supported — a sell smaller than a lot
advances vol_closed and leaves the remainder open (see _close_longs).
"""
import datetime
import json
import logging
import random
import sqlite3
import threading
import time

from . import config

log = logging.getLogger(__name__)

_LOCK = threading.RLock()
_conn = None
_real_private = None
_attached = False

_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_orders(
    txid       TEXT PRIMARY KEY,
    userref    TEXT,
    pair       TEXT,        -- verbatim as sent, e.g. 'XBTUSD:BTNL'
    symbol     TEXT,        -- our candle key, e.g. 'BTC/USD'
    type       TEXT,        -- buy | sell
    ordertype  TEXT,        -- limit | stop-loss | market
    price      REAL,        -- limit price, or stop trigger
    volume     REAL,
    leverage   REAL,
    oflags     TEXT,
    status     TEXT,        -- open | closed | canceled | expired
    vol_exec   REAL DEFAULT 0,
    fill_price REAL,
    cost       REAL DEFAULT 0,
    fee        REAL DEFAULT 0,
    opentm     REAL,
    closetm    REAL,
    reason     TEXT
);
CREATE TABLE IF NOT EXISTS paper_positions(
    postxid    TEXT PRIMARY KEY,
    ordertxid  TEXT,
    pair       TEXT,
    symbol     TEXT,
    type       TEXT,        -- always 'buy' (long only)
    vol        REAL,
    vol_closed REAL DEFAULT 0,
    price      REAL,        -- entry
    cost       REAL,
    fee        REAL DEFAULT 0,
    margin     REAL,
    leverage   REAL,
    opentm     REAL
);
CREATE TABLE IF NOT EXISTS paper_ledger(
    lid    TEXT PRIMARY KEY,
    refid  TEXT,
    time   REAL,
    type   TEXT,
    asset  TEXT,
    amount REAL,
    fee    REAL,
    balance REAL
);
CREATE TABLE IF NOT EXISTS paper_state(key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS ix_paper_orders_status ON paper_orders(status);
CREATE INDEX IF NOT EXISTS ix_paper_pos_pair ON paper_positions(pair);
"""

_ALNUM = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def _txid(prefix="O"):
    """Kraken-shaped id (OXXXXX-XXXXX-XXXXXX) so nothing downstream can quietly
    depend on paper ids looking different from live ones."""
    r = lambda n: "".join(random.choice(_ALNUM) for _ in range(n))   # noqa: E731
    return f"{prefix}{r(5)}-{r(5)}-{r(6)}"


# ── attach / detach ──────────────────────────────────────────────────────────

def attached():
    """True when a simulated exchange is bound to broker.private(). The executor
    arms paper mode ONLY when this is true — paper with no simulator keeps the
    legacy annotate-only behavior the unit tests pin down."""
    return _attached


def attach(db_path=None):
    """Bind the simulator to broker.private(). Idempotent.

    REFUSES in live mode. Executor._armed() is true for live regardless of this
    module, and broker.private() would by then be the simulator — so a live-mode
    bot with a simulator attached would place orders into a fantasy book while
    every gate, rail and reconcile believed they were real, and the operator's
    console would show a book that does not exist. Nothing calls attach() from a
    live path today; this makes that state impossible by construction rather than
    by convention. off/validate are permitted (the unit tests attach there)."""
    global _conn, _real_private, _attached
    with _LOCK:
        if _attached:
            return
        if getattr(config, "EXEC_MODE", "") == "live":
            raise RuntimeError(
                "refusing to attach the paper exchange in LIVE mode — a live bot "
                "trading against a simulated book would look identical to a real one")
        from . import broker
        _conn = sqlite3.connect(db_path or config.DB_PATH, check_same_thread=False,
                                timeout=30.0)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA busy_timeout=30000")
        _conn.executescript(_SCHEMA)
        if _state_get("cash") is None:
            _state_set("cash", float(config.PAPER_PORTFOLIO_USD))
            _state_set("deposits", float(config.PAPER_PORTFOLIO_USD))
            _reanchor_fee_accounting()
        _conn.commit()
        _real_private = broker.private
        broker.private = _dispatch
        _attached = True
        log.warning("PAPER EXCHANGE attached — broker.private() is simulated; "
                    "no private Kraken endpoint will be contacted. cash=$%.2f",
                    _cash())


def _reanchor_fee_accounting():
    """A simulated exchange has NO history before it existed.

    The normal way to seed a paper run is to snapshot the live DB, because that
    carries hundreds of thousands of candles and saves re-backfilling the whole
    roster over the shared public API. But the snapshot also carries `meta`, and the
    rollover-fee accounting there is anchored to when LIVE accounting began. Left
    alone the console reports "carry since jul 19" for a ledger that started
    minutes ago — claiming weeks the simulation never lived through, and anchoring
    every net-skim figure to a window that does not exist.

    Re-anchor the whole fee/flow cursor set to now, once, when the exchange is first
    seeded. Only ever runs on a FRESH simulated book (guarded by the cash seed), so a
    restart never rewrites accounting the paper run has legitimately accumulated."""
    # `meta` belongs to the bot, not the simulator. Attaching to a bare DB (unit
    # tests, a scratch file) means there is no accounting to re-anchor — skip rather
    # than assume the bot's schema exists.
    if not _conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
                         ).fetchone():
        return
    now = time.time()
    keys = (("fees_epoch", now), ("fees_cursor", now), ("flows_cursor", now),
            ("fees_banked", 0.0), ("fees_total", 0.0), ("fees_day", 0.0))
    # Back the old values up before overwriting. Paper is meant to run from a
    # worktree, where PROJECT_ROOT — and therefore DB_PATH — is its own; but running
    # EXEC_MODE=paper from the MAIN checkout would point this at the live ledger,
    # where these keys are real rollover accounting going back months. Zeroing that
    # is not recoverable from the API (the Ledgers walk that built it cannot be
    # repeated — see broker.rollover_fees_since). Recoverable beats gone.
    for k, _v in keys:
        prev = _conn.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
        if prev is not None:
            _conn.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                          "ON CONFLICT(key) DO NOTHING", (f"prepaper_{k}", prev[0]))
    for k, v in keys:
        _conn.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))
    log.info("PAPER: fee accounting re-anchored to now — a simulated exchange has "
             "no carry history before it existed")


def detach():
    """Restore the real broker.private (tests; never used by the running bot)."""
    global _conn, _attached
    with _LOCK:
        if not _attached:
            return
        from . import broker
        broker.private = _real_private
        try:
            _conn.close()
        except Exception:
            pass
        _conn, _attached = None, False


# ── small state helpers ──────────────────────────────────────────────────────

def _state_get(key, default=None):
    r = _conn.execute("SELECT value FROM paper_state WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def _state_set(key, value):
    _conn.execute("INSERT INTO paper_state(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, str(value)))


def _cash():
    try:
        return float(_state_get("cash", config.PAPER_PORTFOLIO_USD))
    except (TypeError, ValueError):
        return float(config.PAPER_PORTFOLIO_USD)


def _credit(amount, kind, asset="ZUSD", fee=0.0, refid=None):
    """Move cash and record a ledger entry (the Ledgers endpoint reads these)."""
    bal = _cash() + float(amount) - float(fee)
    _state_set("cash", bal)
    _conn.execute("INSERT INTO paper_ledger(lid,refid,time,type,asset,amount,fee,balance) "
                  "VALUES(?,?,?,?,?,?,?,?)",
                  (_txid("L"), refid or "", time.time(), kind, asset,
                   float(amount), float(fee), bal))


_SYM_BY_MPAIR = None


def _symbol_for(pair):
    """'XBTUSD:BTNL' -> 'BTC/USD'. Built once from config.MARGIN_PAIR."""
    global _SYM_BY_MPAIR
    if _SYM_BY_MPAIR is None:
        _SYM_BY_MPAIR = {v: k for k, v in config.MARGIN_PAIR.items()}
        # tolerate a pair sent without the :BTNL routing suffix
        for mp, sym in list(_SYM_BY_MPAIR.items()):
            _SYM_BY_MPAIR.setdefault(str(mp).split(":")[0], sym)
    p = str(pair or "")
    return _SYM_BY_MPAIR.get(p) or _SYM_BY_MPAIR.get(p.split(":")[0])


def _bar(symbol, cache):
    """Freshest 15m bar (ts, o, h, l, c) for a symbol, or None. The forming bar is
    updated continuously by the WS feed, so `c` is the live last price."""
    if symbol in cache:
        return cache[symbol]
    row = _conn.execute(
        "SELECT ts,o,h,l,c FROM candles WHERE pair=? AND interval=15 "
        "ORDER BY ts DESC LIMIT 1", (symbol,)).fetchone()
    cache[symbol] = row
    return row


def _last_price(symbol, cache):
    b = _bar(symbol, cache)
    try:
        return float(b[4]) if b else None
    except (TypeError, ValueError):
        return None


# ── the matching engine ──────────────────────────────────────────────────────

def _settle():
    """Advance the simulated exchange to the current market. Runs at the top of
    every dispatch, so whatever the bot asks about is already up to date.

    Stops are evaluated BEFORE limits: within one bar the true sequence is
    unknowable, and taking the stop first is the outcome that costs the book most
    — a simulator should not flatter the strategy."""
    cache = {}
    rows = _conn.execute(
        "SELECT txid,symbol,type,ordertype,price,volume,leverage,opentm,pair "
        "FROM paper_orders WHERE status='open'").fetchall()
    stops = [r for r in rows if "stop" in str(r[3]).lower()]
    limits = [r for r in rows if "stop" not in str(r[3]).lower()]
    for r in stops + limits:
        try:
            _settle_one(r, cache)
        except Exception:
            log.exception("paper settle failed for %s — leaving it resting", r[0])
    _accrue_rollover(cache)
    _conn.commit()


def _settle_one(row, cache):
    txid, symbol, otype, ordertype, price, volume, leverage, opentm, pair = row
    bar = _bar(symbol, cache)
    if not bar:
        return                                    # no market data yet — cannot settle
    bar_ts, _o, hi, lo, last = (float(x) for x in bar)
    price = float(price or 0)
    # No look-ahead: a bar's extremes count only if the bar OPENED after we rested.
    usable_range = bar_ts >= float(opentm or 0)
    is_stop = "stop" in str(ordertype).lower()

    if is_stop:                                   # protective stop-loss (sell, market)
        triggered = last <= price or (usable_range and lo <= price)
        if not triggered:
            return
        # A market sell fills at whatever is there: at the trigger in an orderly
        # move, or straight through it on a gap-down. Then normal slippage.
        ref = min(price, last)
        fill = ref * (1.0 - config.PAPER_SLIPPAGE_PCT)
        _fill(txid, fill, float(volume), taker=True, cache=cache, reason="stop triggered")
        return

    if otype == "buy":
        hit = last <= price or (usable_range and lo <= price)
    else:
        hit = last >= price or (usable_range and hi >= price)
    if hit:
        # A resting maker limit fills AT its price — that is the whole point of
        # post-only, and the executor's cost basis assumes exactly this.
        _fill(txid, price, float(volume), taker=False, cache=cache, reason="limit filled")


def _fill(txid, fill_price, volume, taker, cache, reason):
    """Execute a resting order in full and settle its position side-effects."""
    o = _conn.execute("SELECT symbol,type,pair,leverage FROM paper_orders WHERE txid=?",
                      (txid,)).fetchone()
    if not o:
        return
    symbol, otype, pair, leverage = o
    fee_pct = config.PAPER_FEE_TAKER_PCT if taker else config.PAPER_FEE_MAKER_PCT

    if otype == "sell":
        # LONG-ONLY: a sell can only retire long volume we actually hold.
        available = _long_open_vol(pair)
        volume = min(volume, available)
        if volume <= 0:
            log.warning("PAPER %s: sell %s has no long volume left to close — "
                        "dropping (never opens a short)", symbol, txid)
            _conn.execute("UPDATE paper_orders SET status='canceled', closetm=?, "
                          "reason='no long volume to close' WHERE txid=?",
                          (time.time(), txid))
            return

    gross = volume * fill_price
    fee = gross * fee_pct
    _conn.execute("UPDATE paper_orders SET status='closed', vol_exec=?, fill_price=?, "
                  "cost=?, fee=?, closetm=?, reason=? WHERE txid=?",
                  (volume, fill_price, gross, fee, time.time(), reason, txid))

    if otype == "buy":
        # Margin open: cash pays the fee, the notional is financed. Unrealized P&L
        # rides in TradeBalance 'n' until the lot is closed.
        _conn.execute(
            "INSERT INTO paper_positions(postxid,ordertxid,pair,symbol,type,vol,"
            "vol_closed,price,cost,fee,margin,leverage,opentm) "
            "VALUES(?,?,?,?,'buy',?,0,?,?,?,?,?,?)",
            (_txid("P"), txid, pair, symbol, volume, fill_price, gross, fee,
             gross / max(1.0, float(leverage or 1)), float(leverage or 1), time.time()))
        _credit(0.0, "margin", fee=fee, refid=txid)
        log.info("PAPER FILL %s: bought %.8g @ %.8g (fee $%.4f) — %s",
                 symbol, volume, fill_price, fee, reason)
    else:
        realized = _close_longs(pair, volume, fill_price)
        _credit(realized, "margin", fee=fee, refid=txid)
        log.info("PAPER FILL %s: sold %.8g @ %.8g — realized $%.4f (fee $%.4f) — %s",
                 symbol, volume, fill_price, realized - fee, fee, reason)


def _long_open_vol(pair):
    r = _conn.execute("SELECT COALESCE(SUM(vol - vol_closed),0) FROM paper_positions "
                      "WHERE pair=?", (pair,)).fetchone()
    return max(0.0, float(r[0] or 0))


def _close_longs(pair, volume, fill_price):
    """Retire `volume` of long exposure FIFO; returns gross realized P&L."""
    remaining, realized = float(volume), 0.0
    for postxid, vol, vol_closed, entry in _conn.execute(
            "SELECT postxid,vol,vol_closed,price FROM paper_positions "
            "WHERE pair=? AND vol > vol_closed ORDER BY opentm ASC", (pair,)).fetchall():
        if remaining <= 0:
            break
        take = min(remaining, float(vol) - float(vol_closed))
        realized += take * (fill_price - float(entry))
        remaining -= take
        _conn.execute("UPDATE paper_positions SET vol_closed=vol_closed+? WHERE postxid=?",
                      (take, postxid))
    # Fully-retired lots leave the position book, exactly as Kraken drops them.
    _conn.execute("DELETE FROM paper_positions WHERE vol - vol_closed <= 1e-12")
    return realized


def _accrue_rollover(cache):
    """Kraken charges margin rollover every 4h on open notional. Modeling it keeps
    paper from reading rosier than live, where financing (~35%/yr on notional) is
    the single largest cost the strategy carries."""
    every = float(config.PAPER_ROLLOVER_SECS or 0)
    if every <= 0:
        return
    try:
        nxt = float(_state_get("rollover_next", 0) or 0)
    except (TypeError, ValueError):
        nxt = 0.0
    now = time.time()
    if nxt <= 0:
        _state_set("rollover_next", now + every)
        return
    if now < nxt:
        return
    _state_set("rollover_next", now + every)
    total = 0.0
    for pair, vol_open, entry in _conn.execute(
            "SELECT pair, vol - vol_closed, price FROM paper_positions "
            "WHERE vol > vol_closed").fetchall():
        total += float(vol_open) * float(entry) * config.PAPER_ROLLOVER_PCT
    if total > 0:
        _credit(0.0, "rollover", fee=total)
        log.info("PAPER ROLLOVER: charged $%.4f financing on open notional", total)


# ── Kraken-shaped views ──────────────────────────────────────────────────────

def _order_view(r):
    """A paper_orders row rendered in Kraken's QueryOrders/OpenOrders shape."""
    (txid, userref, pair, _symbol, otype, ordertype, price, volume, leverage,
     oflags, status, vol_exec, fill_price, cost, fee, opentm, closetm, reason) = r
    d = {
        "refid": None,
        "userref": int(userref) if str(userref or "").isdigit() else userref,
        "status": status,
        "opentm": opentm,
        "vol": f"{float(volume):.10f}",
        "vol_exec": f"{float(vol_exec or 0):.10f}",
        "cost": f"{float(cost or 0):.10f}",
        "fee": f"{float(fee or 0):.10f}",
        "price": f"{float(fill_price or 0):.10f}",
        "oflags": oflags or "",
        "misc": "",
        "descr": {
            "pair": pair,
            "type": otype,
            "ordertype": ordertype,
            "price": f"{float(price or 0):.10f}",
            "leverage": f"{int(float(leverage or 1))}:1",
            "order": (f"{otype} {float(volume):.8g} {pair} @ {ordertype} "
                      f"{float(price or 0):.8g}"),
        },
    }
    if closetm:
        d["closetm"] = closetm
    if reason and status in ("canceled", "expired"):
        d["reason"] = reason
    return d


_ORDER_COLS = ("txid,userref,pair,symbol,type,ordertype,price,volume,leverage,"
               "oflags,status,vol_exec,fill_price,cost,fee,opentm,closetm,reason")


def _positions_view(docalcs, cache):
    out = {}
    for (postxid, ordertxid, pair, symbol, vol, vol_closed, price, cost, fee,
         margin, leverage, opentm) in _conn.execute(
            "SELECT postxid,ordertxid,pair,symbol,vol,vol_closed,price,cost,fee,"
            "margin,leverage,opentm FROM paper_positions").fetchall():
        vol_open = float(vol) - float(vol_closed)
        p = {
            "ordertxid": ordertxid, "posstatus": "open", "pair": pair,
            "time": opentm, "type": "buy", "ordertype": "limit",
            "cost": f"{float(cost):.10f}", "fee": f"{float(fee):.10f}",
            "vol": f"{float(vol):.10f}", "vol_closed": f"{float(vol_closed):.10f}",
            "margin": f"{float(margin):.10f}", "terms": "0.0200% per 4 hours",
            "rollovertm": str(int(opentm + 14400)), "misc": "", "oflags": "",
        }
        if docalcs:
            last = _last_price(symbol, cache)
            if last is not None:
                value = vol_open * last
                p["value"] = f"{value:.10f}"
                p["net"] = f"{value - vol_open * float(price):.10f}"
            else:                                  # no mark — report flat, never guess
                p["value"], p["net"] = "0.0", "0.0"
        out[postxid] = p
    return out


def _trade_balance(cache):
    cost_basis = value = margin = 0.0
    for symbol, vol, vol_closed, price, lev in _conn.execute(
            "SELECT symbol,vol,vol_closed,price,leverage FROM paper_positions").fetchall():
        vol_open = float(vol) - float(vol_closed)
        if vol_open <= 0:
            continue
        entry = float(price)
        last = _last_price(symbol, cache)
        cost_basis += vol_open * entry
        value += vol_open * (last if last is not None else entry)
        margin += vol_open * entry / max(1.0, float(lev or 1))
    cash = _cash()
    unrealized = value - cost_basis
    equity = cash + unrealized
    free = equity - margin
    ml = (equity / margin * 100.0) if margin > 0 else 0.0
    return {
        "eb": f"{cash:.10f}",          # equity balance (cash)
        "tb": f"{cash:.10f}",          # trade balance
        "m": f"{margin:.10f}",         # margin used
        "n": f"{unrealized:.10f}",     # unrealized net P&L
        "c": f"{cost_basis:.10f}",     # cost basis of open positions
        "v": f"{value:.10f}",          # current value of open positions
        "e": f"{equity:.10f}",         # equity
        "mf": f"{free:.10f}",          # free margin
        "ml": f"{ml:.10f}",            # margin level %
    }


# ── endpoint dispatch ────────────────────────────────────────────────────────

def _dispatch(endpoint, params=None, idempotent=True, meta=None):
    """Stands in for broker.private(). Always answers definitively — a simulator
    has no ambiguous transport, so the userref-recovery path simply never arms."""
    if meta is not None:
        meta["definite"] = True
    p = dict(params or {})
    with _LOCK:
        if not _attached:
            log.error("paper dispatch called while detached — refusing %s", endpoint)
            return None
        try:
            _settle()
            return _handle(endpoint, p, meta)
        except Exception:
            log.exception("paper exchange failed on %s — returning None (caller "
                          "treats it as an API failure and converges next cycle)",
                          endpoint)
            return None


def _handle(endpoint, p, meta):
    cache = {}
    if endpoint == "/0/private/AddOrder":
        return _add_order(p, meta, cache)

    if endpoint == "/0/private/TradeBalance":
        return _trade_balance(cache)

    if endpoint == "/0/private/OpenPositions":
        return _positions_view(str(p.get("docalcs", "")).lower() == "true", cache)

    if endpoint == "/0/private/OpenOrders":
        rows = _conn.execute(f"SELECT {_ORDER_COLS} FROM paper_orders WHERE status='open'").fetchall()
        ref = str(p.get("userref", "") or "")
        if ref:
            rows = [r for r in rows if str(r[1] or "") == ref]
        return {"open": {r[0]: _order_view(r) for r in rows}}

    if endpoint == "/0/private/ClosedOrders":
        rows = _conn.execute(
            f"SELECT {_ORDER_COLS} FROM paper_orders WHERE status!='open' "
            "ORDER BY closetm DESC LIMIT 50").fetchall()
        ref = str(p.get("userref", "") or "")
        if ref:
            rows = [r for r in rows if str(r[1] or "") == ref]
        return {"closed": {r[0]: _order_view(r) for r in rows}}

    if endpoint == "/0/private/QueryOrders":
        ids = [t for t in str(p.get("txid", "")).split(",") if t]
        if not ids:
            return {}
        q = ",".join("?" * len(ids))
        rows = _conn.execute(
            f"SELECT {_ORDER_COLS} FROM paper_orders WHERE txid IN ({q})", ids).fetchall()
        return {r[0]: _order_view(r) for r in rows}

    if endpoint == "/0/private/CancelOrder":
        txid = p.get("txid")
        n = _conn.execute("UPDATE paper_orders SET status='canceled', closetm=?, "
                          "reason='canceled by user' WHERE txid=? AND status='open'",
                          (time.time(), txid)).rowcount
        _conn.commit()
        return {"count": n}

    if endpoint == "/0/private/CancelOrderBatch":
        ids = [v for k, v in p.items() if k.startswith("orders[")]
        n = 0
        for t in ids:
            n += _conn.execute("UPDATE paper_orders SET status='canceled', closetm=?, "
                               "reason='canceled by user' WHERE txid=? AND status='open'",
                               (time.time(), t)).rowcount
        _conn.commit()
        return {"count": n}

    if endpoint == "/0/private/Ledgers":
        want = str(p.get("type", "all") or "all")
        try:
            start = float(p.get("start", 0) or 0)
            ofs = int(p.get("ofs", 0) or 0)
        except (TypeError, ValueError):
            start, ofs = 0.0, 0
        sql = "SELECT lid,refid,time,type,asset,amount,fee,balance FROM paper_ledger WHERE time>=?"
        args = [start]
        if want and want != "all":
            sql += " AND type=?"
            args.append(want)
        sql += " ORDER BY time ASC LIMIT 50 OFFSET ?"
        args.append(ofs)
        rows = _conn.execute(sql, args).fetchall()
        return {"ledger": {r[0]: {"refid": r[1], "time": r[2], "type": r[3],
                                  "asset": r[4], "amount": f"{r[5]:.10f}",
                                  "fee": f"{r[6]:.10f}", "balance": f"{r[7]:.10f}"}
                           for r in rows},
                "count": len(rows)}

    log.error("paper exchange has no handler for %s — refusing (nothing was sent "
              "to Kraken)", endpoint)
    if meta is not None:
        meta["error"] = f"paper: unsupported endpoint {endpoint}"
    return None


def _add_order(p, meta, cache):
    pair = p.get("pair")
    symbol = _symbol_for(pair)
    if not symbol:
        if meta is not None:
            meta["error"] = f"EOrder:Unknown asset pair ({pair})"
        log.error("PAPER AddOrder: unknown pair %s", pair)
        return None
    otype = str(p.get("type", "buy")).lower()
    ordertype = str(p.get("ordertype", "limit")).lower()
    try:
        volume = float(p.get("volume", 0) or 0)
        price = float(p.get("price", 0) or 0)
    except (TypeError, ValueError):
        if meta is not None:
            meta["error"] = "EGeneral:Invalid arguments:volume/price"
        return None
    if volume <= 0:
        if meta is not None:
            meta["error"] = "EOrder:Invalid volume"
        return None

    last = _last_price(symbol, cache)
    oflags = str(p.get("oflags", "") or "")
    # Post-only rejects anything that would cross and take liquidity — the very
    # behavior the ladder's below-market clamp and the harvest's abort floor exist
    # to respect. Simulating the reject keeps those guards honest.
    if "post" in oflags and last is not None and ordertype == "limit":
        # STRICT inequality. The simulator has one price where a real book has a
        # bid/ask pair, and resting AT the touch is the maker's normal position —
        # the rung harvest deliberately prices its sell at min(bid+tick, ask), i.e.
        # ON the ask. Treating equality as crossing rejected exactly those sells, so
        # the +4% engine could never fire in paper and the book would only ever grow:
        # the same blind spot the simulator was built to remove.
        crosses = (otype == "buy" and price > last) or (otype == "sell" and price < last)
        if crosses:
            if meta is not None:
                meta["error"] = "EOrder:Post only order"
            log.info("PAPER AddOrder %s: post-only %s @ %.8g would cross last %.8g — rejected",
                     symbol, otype, price, last)
            return None

    if str(p.get("validate", "")).lower() == "true":
        return {"descr": {"order": f"{otype} {volume:.8g} {pair} @ {ordertype} {price:.8g}"}}

    txid = _txid()
    _conn.execute(
        "INSERT INTO paper_orders(txid,userref,pair,symbol,type,ordertype,price,volume,"
        "leverage,oflags,status,opentm) VALUES(?,?,?,?,?,?,?,?,?,?,'open',?)",
        (txid, str(p.get("userref", "") or ""), pair, symbol, otype, ordertype, price,
         volume, float(p.get("leverage", 1) or 1), oflags, time.time()))
    _conn.commit()
    log.info("PAPER AddOrder %s: %s %.8g @ %s %.8g -> %s",
             symbol, otype, volume, ordertype, price, txid)
    return {"txid": [txid], "descr": {"order": f"{otype} {volume:.8g} {pair} @ "
                                               f"{ordertype} {price:.8g}"}}


# ── operator-facing summary ──────────────────────────────────────────────────

def summary():
    """One-line state of the simulated exchange (used by --paper-status)."""
    with _LOCK:
        if not _attached:
            return "paper exchange: not attached"
        _settle()
        cache = {}
        tb = _trade_balance(cache)
        resting = _conn.execute("SELECT COUNT(*) FROM paper_orders WHERE status='open'").fetchone()[0]
        lots = _conn.execute("SELECT COUNT(*) FROM paper_positions WHERE vol>vol_closed").fetchone()[0]
        fills = _conn.execute("SELECT COUNT(*) FROM paper_orders WHERE status='closed'").fetchone()[0]
        fees = _conn.execute("SELECT COALESCE(SUM(fee),0) FROM paper_ledger").fetchone()[0]
        return (f"paper exchange: equity ${float(tb['e']):.2f} · cash ${_cash():.2f} · "
                f"ml {float(tb['ml']):.0f}% · {lots} lots · {resting} resting · "
                f"{fills} fills · ${float(fees or 0):.2f} fees")
