"""PROP SIGNAL MODE — advisory re-scorer for the operator's hand-traded Kraken
Prop evaluation. SPEC (operator, 2026-08-15):

    $5,000 sim wallet · profit target +$450 · max daily loss 3% of balance at
    00:30 UTC (resets) · max lifetime drawdown $150 from start (NEVER resets)
    · unrealized P&L and ALL fees count against both limits · breach = instant
    liquidation · fees 0.04%/side · margin funding 0.033%/day charged every 4h
    · leverage up to 5x.

This module consumes DEEPFIELD's confirmed-BUY signals AT THE SIGNAL POINT
(ingest._dispatch calls on_signal after the live order and alert are handled)
and re-scores them for the Prop envelope. It is ADVISORY ONLY:

  - It never places, sizes, or cancels a live order. The operator enters Prop
    positions BY HAND and records them via the CLI (`scripts/prop open ...`).
  - The hook is wrapped in its own try/except at the call site AND is gated on
    config.PROP_ENABLED — a prop failure must never break the alert/order writer.
  - It writes only its own files: prop_state.json (ledger) and
    logs/prop_journal.jsonl (every evaluation, passed AND rejected, with reason).

GEOMETRY HONESTY: DEEPFIELD's native geometry is TP=1.0xATR vs SL=1.5xATR —
reward:risk 0.667 by construction, for every pair at every volatility. With
PROP_MIN_RR=3.0 every native signal is REJECTED, and that is the truthful
default: the journal proves what the envelope rejects rather than inventing a
target the strategy never earned. Set config.PROP_TP_R to derive the ticket TP
as an R-multiple of the stop distance instead (operator's knob, default None).

DEDUP: boot-arm re-fires every open BUY on every restart (the operator restarts
often — three times in two hours today). Evaluations are deduped on
(symbol, UTC day) and the seen-set is PERSISTED in prop_state.json, because
per-process dedup state is the defect class that produced 103 duplicate floor
lines (2026-08-12) and 397 phantom journal rows (the RENDER phantom).

Two writers touch prop_state.json (the bot's hook thread and the CLI): every
mutation goes through an atomic tmp+os.replace write; readers tolerate a stale
read (worst case: one duplicate evaluation, deduped by the journal reader).
"""
import datetime as _dt
import json
import logging
import math
import os
import time
import urllib.parse
import urllib.request

from . import config

log = logging.getLogger(__name__)

STATE_PATH = os.path.join(config.PROJECT_ROOT, "prop_state.json")
JOURNAL_PATH = os.path.join(config.LOG_DIR, "prop_journal.jsonl")

# ── envelope constants (config-overridable, defaults per the operator's spec) ──
START_BALANCE = float(getattr(config, "PROP_START_BALANCE", 5000.0))
MDD_FLOOR = float(getattr(config, "PROP_MDD_FLOOR", 4850.0))       # lifetime, never resets
MDL_PCT = float(getattr(config, "PROP_MDL_PCT", 3.0))              # % of balance at 00:30 UTC
RISK_PER_TRADE = float(getattr(config, "PROP_RISK_PER_TRADE", 40.0))
MIN_RR = float(getattr(config, "PROP_MIN_RR", 3.0))
FEE_SIDE = float(getattr(config, "PROP_FEE_SIDE", 0.0004))         # 0.04% per side
FUNDING_DAY = float(getattr(config, "PROP_FUNDING_DAY", 0.00033))  # 0.033%/day on notional
FUNDING_CHARGES_PER_DAY = 6                                        # charged every 4h
FUNDING_MAX_FRAC = float(getattr(config, "PROP_FUNDING_MAX_FRAC", 0.05))
MAX_LEV = int(getattr(config, "PROP_MAX_LEV", 5))
PROFIT_TARGET = float(getattr(config, "PROP_PROFIT_TARGET", 450.0))
RESET_HOUR_UTC, RESET_MIN_UTC = 0, 30                              # 00:30 UTC daily


# ── state ─────────────────────────────────────────────────────────────────────

def _default_state():
    return {
        "start_balance": START_BALANCE,
        "balance": START_BALANCE,          # realized only; fees debited as incurred
        "equity": START_BALANCE,           # balance + marked unrealized (prop mark)
        "mdd_floor": MDD_FLOOR,
        "mdl_floor": round(START_BALANCE * (1 - MDL_PCT / 100.0), 2),
        "mdl_reset_iso": None,             # ISO ts of the last 00:30 UTC recompute
        "positions": [],                   # entered BY HAND via `prop open`
        "seen": {},                        # dedup: "SYM:utcday" -> eval ts (pruned)
    }


def load_state(path=STATE_PATH):
    try:
        with open(path) as f:
            st = json.load(f)
    except FileNotFoundError:
        return _default_state()
    except (json.JSONDecodeError, OSError):
        log.exception("prop_state.json unreadable — refusing to guess; fix or delete it")
        raise
    base = _default_state()
    base.update(st)
    return base


def save_state(st, path=STATE_PATH):
    """Atomic: two writers (bot hook thread, CLI) must never interleave a torn file."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def last_reset_boundary(now_utc):
    """The most recent 00:30 UTC instant at or before now_utc."""
    b = now_utc.replace(hour=RESET_HOUR_UTC, minute=RESET_MIN_UTC, second=0, microsecond=0)
    if b > now_utc:
        b -= _dt.timedelta(days=1)
    return b


def maybe_reset_day(st, now_utc=None):
    """Lazy 00:30 UTC MDL recompute — runs on every evaluation and CLI call, so
    no scheduler is needed and a missed boundary heals on the next touch.
    MDL floor = balance * (1 - 3%), from the REALIZED balance (the daily limit
    resets; unrealized then counts against it intraday via equity marks).
    Returns True when a recompute happened."""
    now_utc = now_utc or _dt.datetime.now(_dt.timezone.utc)
    boundary = last_reset_boundary(now_utc)
    prev = st.get("mdl_reset_iso")
    if prev is not None and _dt.datetime.fromisoformat(prev) >= boundary:
        return False
    st["mdl_floor"] = round(float(st["balance"]) * (1 - MDL_PCT / 100.0), 2)
    st["mdl_reset_iso"] = boundary.isoformat()
    return True


# ── fee / funding arithmetic (the numbers the selftest pins by hand) ─────────

def funding_accrued(notional, hours_held):
    """Charged every 4h: floor(hours/4) completed charges at (0.033%/6) each."""
    charges = int(hours_held // 4)
    return charges * notional * FUNDING_DAY / FUNDING_CHARGES_PER_DAY


def _round_down(x, decimals):
    q = 10 ** decimals
    return math.floor(x * q) / q


# ── the sizing engine ────────────────────────────────────────────────────────

def evaluate(symbol, entry, stop, tp, equity, mdd_floor, mdl_floor,
             risk=RISK_PER_TRADE, lot_decimals=8):
    """Pure function: signal geometry + envelope -> ticket dict or rejection dict.
    NO I/O, NO delivery — the selftest calls this directly and must never pop a
    desktop alert (the suite fired real notify-send once; never again).

    A full stop-out must cost at most `risk` dollars INCLUDING the round-trip
    commission and 24h of funding:
        per_unit_loss = (entry-stop) + fee*entry + fee*stop + funding_day*entry
        size = risk / per_unit_loss
    Reward is net of the same costs on the winning path:
        per_unit_gain = (tp-entry) - fee*entry - fee*tp - funding_day*entry
    """
    out = {"symbol": symbol, "direction": "long", "entry": entry, "stop": stop,
           "tp": tp, "ok": False, "reasons": []}
    if not (0 < stop < entry < tp):
        out["reasons"].append(f"degenerate geometry (stop {stop} / entry {entry} / tp {tp})")
        return out

    per_unit_loss = (entry - stop) + FEE_SIDE * entry + FEE_SIDE * stop + FUNDING_DAY * entry
    size = _round_down(risk / per_unit_loss, lot_decimals)
    if size <= 0:
        out["reasons"].append("size rounds to zero at this lot precision")
        return out
    notional = size * entry
    risk_dollars = size * per_unit_loss
    per_unit_gain = (tp - entry) - FEE_SIDE * entry - FEE_SIDE * tp - FUNDING_DAY * entry
    reward_dollars = size * per_unit_gain
    funding_24h = size * entry * FUNDING_DAY
    rr = (reward_dollars / risk_dollars) if risk_dollars > 0 else 0.0

    # gates, all evaluated so the journal shows every failing reason at once
    if notional > MAX_LEV * equity:
        out["reasons"].append(
            f"exceeds wallet capacity: notional ${notional:,.2f} > {MAX_LEV}x equity ${MAX_LEV * equity:,.2f}")
    if rr < MIN_RR:
        out["reasons"].append(f"reward:risk {rr:.2f} < {MIN_RR:.1f} after fees")
    if equity - risk_dollars < mdd_floor:
        out["reasons"].append(
            f"stop-out breaches MDD floor: {equity:.2f} - {risk_dollars:.2f} = "
            f"{equity - risk_dollars:.2f} < {mdd_floor:.2f}")
    if equity - risk_dollars < mdl_floor:
        out["reasons"].append(
            f"stop-out breaches today's MDL floor: {equity - risk_dollars:.2f} < {mdl_floor:.2f}")
    if reward_dollars <= 0 or funding_24h > FUNDING_MAX_FRAC * reward_dollars:
        out["reasons"].append(
            f"24h funding ${funding_24h:.2f} > {FUNDING_MAX_FRAC:.0%} of expected profit "
            f"${max(reward_dollars, 0):.2f}")

    # leverage: smallest 1..MAX_LEV keeping margin lock <= 25% of equity, else MAX_LEV
    lev = MAX_LEV
    for cand in range(1, MAX_LEV + 1):
        if notional / cand <= 0.25 * equity:
            lev = cand
            break

    open_fee = FEE_SIDE * notional
    out.update({
        "size_base": size, "notional_usd": round(notional, 2), "leverage": lev,
        "risk_usd": round(risk_dollars, 2), "reward_usd": round(reward_dollars, 2),
        "rr": round(rr, 3), "funding_24h_usd": round(funding_24h, 4),
        "mdd_room_after_fill": round(equity - open_fee - mdd_floor, 2),
        "mdl_room_after_fill": round(equity - open_fee - mdl_floor, 2),
    })
    out["ok"] = not out["reasons"]
    return out


# ── journal + delivery ───────────────────────────────────────────────────────

def journal(record, path=JOURNAL_PATH):
    record = dict(record, ts=_dt.datetime.now(_dt.timezone.utc).isoformat())
    try:
        with open(path, "a") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        log.exception("prop journal write failed (evaluation result was: %s)", record)
    return record


def _format_ticket(t):
    tag = " · [EXCLUDED on live bot]" if t.get("excluded_on_live") else ""
    return (f"PROP {t['symbol']} LONG · entry {t['entry']:g} · stop {t['stop']:g} · "
            f"tp {t['tp']:g} · size {t['size_base']:g} (${t['notional_usd']:,.2f} @ "
            f"{t['leverage']}x) · risk ${t['risk_usd']:.2f} · reward ${t['reward_usd']:.2f} · "
            f"R:R {t['rr']:.2f} · room MDD ${t['mdd_room_after_fill']:.2f} / "
            f"MDL ${t['mdl_room_after_fill']:.2f}{tag}")


def deliver(ticket):
    """Existing DEEPFIELD notification path first (sound + notify-send); ntfy.sh
    topic as fallback/addition when configured. Never raises."""
    msg = _format_ticket(ticket)
    delivered = []
    try:
        from . import alerter
        alerter.play_alert()
        alerter._notify_send(ticket["symbol"], msg)
        delivered.append("desktop")
    except Exception:
        log.exception("prop ticket desktop delivery failed")
    topic = str(getattr(config, "PROP_NTFY_TOPIC", "") or "")
    if topic:
        try:
            req = urllib.request.Request(
                "https://ntfy.sh/" + urllib.parse.quote(topic),
                data=msg.encode(), headers={"Title": f"PROP ticket {ticket['symbol']}"})
            urllib.request.urlopen(req, timeout=10)
            delivered.append("ntfy")
        except Exception:
            log.exception("prop ticket ntfy delivery failed")
    return delivered


# ── the signal hook (called from ingest._dispatch, guarded there AND here) ───

def _geometry_for(conn, symbol):
    """DEEPFIELD's proposed stop/target for the pair: the persisted vol table
    (meta['vol_table'], ATR-scaled per pair, refreshed daily)."""
    from . import store
    raw = store.meta_get(conn, "vol_table")
    if not raw:
        return None
    tbl = json.loads(raw).get("table", {})
    return tbl.get(symbol)


def on_signal(conn, symbol, price, kind="confirmed"):
    """Re-score a fired DEEPFIELD signal for the Prop envelope. Advisory only —
    every return path is a no-op for the live system."""
    if not getattr(config, "PROP_ENABLED", False) or kind != "confirmed":
        return None
    now = _dt.datetime.now(_dt.timezone.utc)
    st = load_state()
    key = f"{symbol}:{now.strftime('%Y-%m-%d')}"
    if key in st.get("seen", {}):
        return None                     # boot-arm re-fire of an already-scored signal
    geo = _geometry_for(conn, symbol)
    if not geo or not price or price <= 0:
        journal({"kind": "signal", "symbol": symbol, "ok": False,
                 "reasons": ["no vol geometry or live price for pair"]})
        return None
    sl_pct, tp_pct = float(geo["sl_pct"]), float(geo["tp_pct"])
    entry = float(price)
    stop = entry * (1 - sl_pct / 100.0)
    tp_r = getattr(config, "PROP_TP_R", None)
    tp = entry + tp_r * (entry - stop) if tp_r else entry * (1 + tp_pct / 100.0)

    maybe_reset_day(st, now)
    lot_dec = 8
    try:
        from . import store
        info = store.get_pair_info(conn, symbol)
        if info and info.get("lot_decimals") is not None:
            lot_dec = int(info["lot_decimals"])
    except Exception:
        pass
    result = evaluate(symbol, entry, stop, tp, float(st["equity"]),
                      float(st["mdd_floor"]), float(st["mdl_floor"]),
                      lot_decimals=lot_dec)
    result["kind"] = "signal"
    result["tp_source"] = f"{tp_r}R" if tp_r else "deepfield-vol-table"
    # TAG, never filter (operator 2026-08-15): the live exclusions are 10x-clamp
    # economics that do not map to the 5x prop envelope. The ticket says so; the
    # hand-trader decides.
    result["excluded_on_live"] = symbol in getattr(config, "EXCLUDED_PAIRS", ())
    journal(result)
    st.setdefault("seen", {})[key] = now.isoformat()
    cutoff = (now - _dt.timedelta(days=7)).isoformat()
    st["seen"] = {k: v for k, v in st["seen"].items() if v >= cutoff}
    save_state(st)
    if result["ok"]:
        result["delivered"] = deliver(result)
        log.info("PROP ticket %s: %s", symbol, _format_ticket(result))
    else:
        log.info("PROP reject %s: %s", symbol, "; ".join(result["reasons"]))
    return result


# ── live prices for `prop mark` (deck API first — NEVER a private Kraken call
#    from a side process; the public Ticker is the unauthenticated fallback) ──

def _live_prices(symbols):
    out = {}
    try:
        with urllib.request.urlopen("http://localhost:8787/api/state", timeout=5) as r:
            state = json.load(r)
        by_display = {p["sym"]: p.get("price") for p in state.get("pairs", [])}
        for s in symbols:
            px = by_display.get(s.split("/")[0])
            if px:
                out[s] = float(px)
    except Exception:
        pass
    missing = [s for s in symbols if s not in out]
    if missing:
        try:
            pair_q = ",".join(s.replace("/", "").replace("BTC", "XBT") for s in missing)
            with urllib.request.urlopen(
                    "https://api.kraken.com/0/public/Ticker?pair=" + pair_q, timeout=10) as r:
                res = json.load(r).get("result", {})
            for s in missing:
                alt = s.replace("/", "").replace("BTC", "XBT")
                for k, v in res.items():
                    if alt in k:
                        out[s] = float(v["c"][0])
                        break
        except Exception:
            log.exception("public ticker fallback failed for %s", missing)
    return out


def mark(st, now_utc=None, prices=None):
    """Refresh equity from live prices: balance + sum of per-position unrealized
    net of the estimated close fee and funding accrued so far (the prop firm
    counts unrealized AND fees against both limits, so we must too)."""
    now_utc = now_utc or _dt.datetime.now(_dt.timezone.utc)
    if prices is None:
        prices = _live_prices([p["pair"] for p in st["positions"]])
    unreal = 0.0
    for p in st["positions"]:
        px = prices.get(p["pair"])
        if px is None:
            continue
        p["mark"] = px
        d = 1 if p["side"] == "buy" else -1
        hours = (now_utc - _dt.datetime.fromisoformat(p["ts_open"])).total_seconds() / 3600.0
        gross = (px - p["entry"]) * p["size"] * d
        close_fee = FEE_SIDE * px * p["size"]
        fund = funding_accrued(p["entry"] * p["size"], hours)
        p["funding_accrued"] = round(fund, 6)
        unreal += gross - close_fee - fund
    st["equity"] = round(st["balance"] + unreal, 4)
    return st


# ── CLI ──────────────────────────────────────────────────────────────────────

def _cmd_status(st):
    now = _dt.datetime.now(_dt.timezone.utc)
    maybe_reset_day(st, now)
    mark(st, now)
    save_state(st)
    tgt = st["start_balance"] + PROFIT_TARGET
    print(f"PROP wallet   balance ${st['balance']:,.2f} · equity ${st['equity']:,.2f}")
    print(f"target        ${tgt:,.2f}  (${tgt - st['equity']:,.2f} to go)")
    print(f"MDD floor     ${st['mdd_floor']:,.2f}  (room ${st['equity'] - st['mdd_floor']:,.2f}, lifetime)")
    print(f"MDL floor     ${st['mdl_floor']:,.2f}  (room ${st['equity'] - st['mdl_floor']:,.2f}, "
          f"reset {st['mdl_reset_iso']})")
    if not st["positions"]:
        print("positions     none")
    for p in st["positions"]:
        mk = p.get("mark")
        upnl = ((mk - p["entry"]) * p["size"] * (1 if p["side"] == "buy" else -1)) if mk else None
        print(f"  {p['pair']:10} {p['side']} {p['size']:g} @ {p['entry']:g} · stop {p['stop']:g} · "
              f"tp {p['tp']:g} · mark {mk if mk else 'n/a'} · "
              f"uP&L {f'${upnl:+.2f}' if upnl is not None else 'n/a'} · "
              f"funding ${p.get('funding_accrued', 0):.4f}")


def _cmd_open(st, pair, side, entry, size, stop, tp):
    entry, size, stop, tp = float(entry), float(size), float(stop), float(tp)
    open_fee = FEE_SIDE * entry * size
    st["balance"] = round(st["balance"] - open_fee, 4)   # fees count against limits NOW
    st["positions"].append({
        "pair": pair.upper(), "side": side.lower(), "entry": entry, "size": size,
        "stop": stop, "tp": tp, "ts_open": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "open_fee": round(open_fee, 6), "funding_accrued": 0.0,
    })
    journal({"kind": "manual-open", "pair": pair.upper(), "side": side, "entry": entry,
             "size": size, "stop": stop, "tp": tp, "open_fee": round(open_fee, 6)})
    save_state(st)
    print(f"opened {pair} {side} {size:g} @ {entry:g} (open fee ${open_fee:.4f} debited) — "
          f"balance ${st['balance']:,.2f}")


def _cmd_close(st, pair, exit_price):
    exit_price = float(exit_price)
    pair = pair.upper()
    pos = next((p for p in st["positions"] if p["pair"] == pair), None)
    if pos is None:
        print(f"no open prop position for {pair}")
        return
    now = _dt.datetime.now(_dt.timezone.utc)
    hours = (now - _dt.datetime.fromisoformat(pos["ts_open"])).total_seconds() / 3600.0
    d = 1 if pos["side"] == "buy" else -1
    gross = (exit_price - pos["entry"]) * pos["size"] * d
    close_fee = FEE_SIDE * exit_price * pos["size"]
    fund = funding_accrued(pos["entry"] * pos["size"], hours)
    net = gross - close_fee - fund                      # open fee already debited at open
    st["balance"] = round(st["balance"] + net, 4)
    st["positions"].remove(pos)
    mark(st, now, prices={})
    save_state(st)
    journal({"kind": "manual-close", "pair": pair, "exit": exit_price, "gross": round(gross, 4),
             "close_fee": round(close_fee, 6), "funding": round(fund, 6), "net": round(net, 4),
             "balance": st["balance"]})
    print(f"closed {pair} @ {exit_price:g}: gross ${gross:+.2f} − fee ${close_fee:.4f} − "
          f"funding ${fund:.4f} = net ${net:+.2f} — balance ${st['balance']:,.2f}")


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="prop", description="DEEPFIELD prop signal mode CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p_open = sub.add_parser("open")
    for a in ("pair", "side", "entry", "size", "stop", "tp"):
        p_open.add_argument(a)
    p_close = sub.add_parser("close")
    p_close.add_argument("pair")
    p_close.add_argument("exit_price")
    sub.add_parser("mark")
    sub.add_parser("reset-day")
    args = ap.parse_args(argv)
    st = load_state()
    if args.cmd == "status":
        _cmd_status(st)
    elif args.cmd == "open":
        _cmd_open(st, args.pair, args.side, args.entry, args.size, args.stop, args.tp)
    elif args.cmd == "close":
        _cmd_close(st, args.pair, args.exit_price)
    elif args.cmd == "mark":
        mark(st)
        save_state(st)
        print(f"equity marked: ${st['equity']:,.2f} (balance ${st['balance']:,.2f})")
    elif args.cmd == "reset-day":
        did = maybe_reset_day(st)
        save_state(st)
        print(f"MDL floor {'recomputed' if did else 'already current'}: ${st['mdl_floor']:,.2f}")


if __name__ == "__main__":
    main()
