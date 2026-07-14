"""DEEPFIELD v6 · THE SURVEY — rich Live TUI. Three full-screen views.

A long exposure of dark sky: idle pairs render compressed, watch pairs brighter,
confirmed BUYs and open positions in full detail. Detail is EARNED, so the frame
can never overflow. One spatial language — the year-band: every pair drawn on its
52-week range, so "buy near the yearly low" is visible geometrically.

Three laws (SPEC identity):
1. Resolution follows relevance.  2. One spatial language: the year-band.
3. Color is a legend, not decoration — every data CATEGORY owns one hue,
   used for that category everywhere (see PALETTE).

Renderers are PURE AppState readers (invariant 5): they read the exec snapshot
(app._exec_state_refresh) and never touch sqlite/broker. The `conn` argument is
kept only for signature compatibility; nothing in the render path uses it.
"""
import time
import asyncio
import datetime
import json
from zoneinfo import ZoneInfo

from rich.console import Console, Group
from rich.table import Table
from rich.text import Text
from rich.live import Live

from . import engine
from . import config
from . import VERSION

DENVER = ZoneInfo("America/Denver")

# ── PALETTE — the category legend (truecolor hex; §2) ────────────────────────
PRICE = "#E7ECF0"      # steel  — all prices, measured values
TIME = "#6FB3D2"       # sky    — clocks, countdowns, ages, TTLs, timestamps
QTY = "#A98FD6"        # violet — volume, fill/pos counts, margin, notional, capacity
THESIS = "#C9A85C"     # brass  — scores, BUY, detections, fill dots, ✓
GAIN = "#57B98A"       # green  — positive P&L / 24hΔ, up cursor
LOSS = "#D45D5D"       # red    — negative P&L / 24hΔ, down cursor
RISK = "#E0483E"       # alarm  — stops, proximity, HALT, MISMATCH, UNPROTECTED
HEALTH = "#4FBFA3"     # teal   — link, recon, coherence, live fills, rails ok
ATTN = "#E09B4C"       # amberorange — STALE, pending/○ bids, EXPIRE, warnings
INK = "#6B747C"        # graphite — labels, units, chrome (LABELS ONLY, never data)
FAINT = "#3E454C"      # idle rows, band tracks, separators
IND = "#8FD1C6"        # soft cyan — W/D-RSI values + trend arrows
SEL_BG = "on #141920"  # selected strip background

DISPLAY = {p["ws"]: p["display"] for p in config.PAIRS}
PAIR_LIST = [p["ws"] for p in config.PAIRS]

KIND_TAG_STYLE = {   # JOURNAL / latest tag hues by category
    "detect": THESIS, "fill": HEALTH, "order": PRICE, "stop": f"dim {RISK}",
    "recon": HEALTH, "expire": ATTN, "sys": INK,
}


# ── formatting helpers ───────────────────────────────────────────────────────

def _fmt_price(p):
    if p is None:
        return "---"
    if p >= 1000:
        return f"${p:,.0f}"
    if p >= 1:
        return f"${p:.2f}"
    return f"${p:.4f}"


def _fmt_usd(v):
    if v is None:
        return "---"
    if round(v, 2) == 0:            # zero carries no sign (honesty fix)
        return "$0.00"
    return f"+${v:,.2f}" if v > 0 else f"-${abs(v):,.2f}"


def _fmt_age(secs):
    if secs is None or secs == float("inf"):
        return "---"
    if secs < 60:
        return f"{secs:.0f}s"
    if secs < 3600:
        return f"{secs / 60:.0f}m"
    return f"{secs / 3600:.1f}h"


def _fmt_hms(secs):
    secs = max(0, int(secs))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _fmt_days_hm(secs):
    secs = max(0, int(secs))
    d, rem = divmod(secs, 86400)
    h, rem2 = divmod(rem, 3600)
    m, _ = divmod(rem2, 60)
    return f"{d}d {h:02d}:{m:02d}" if d > 0 else f"{h:02d}:{m:02d}"


def _local_short(ts_iso):
    try:
        dt = datetime.datetime.fromisoformat(ts_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(DENVER).strftime("%m-%d %H:%M")
    except Exception:
        return (ts_iso or "")[:16]


def _local_hm(ts_iso):
    try:
        dt = datetime.datetime.fromisoformat(ts_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(DENVER).strftime("%H:%M")
    except Exception:
        return "--:--"


def _local_hms(ts_iso):
    try:
        dt = datetime.datetime.fromisoformat(ts_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(DENVER).strftime("%H:%M:%S")
    except Exception:
        return "--:--:--"


def _mini_bar(fraction, cells=6):
    fraction = max(0.0, min(1.0, fraction))
    filled = round(fraction * cells)
    return "▰" * filled + "▱" * (cells - filled)


def _dots(score, denom):
    """Score as filled/hollow dots — brass fired, faint not (THESIS)."""
    score = max(0, int(score or 0))
    denom = max(score, int(denom or 0))
    t = Text()
    t.append("●" * score, style=THESIS)
    t.append("○" * (denom - score), style=FAINT)
    return t


def _sep(w):
    return Text("─" * max(1, w), style=FAINT)


# ── the year-band — the signature (§ FIELD) ──────────────────────────────────

def _band_col(price, lo, hi, band_w):
    """Linear price→column over [lo, hi]. The load-bearing math (acceptance #2):
    a fill at the 52w low lands in column 0; at the 52w high, the last column.
    Out-of-range clamps to the ends; a degenerate range returns None."""
    if price is None or lo is None or hi is None or hi <= lo or band_w <= 0:
        return None
    frac = (price - lo) / (hi - lo)
    frac = 0.0 if frac < 0 else (1.0 if frac > 1 else frac)
    return int(round(frac * (band_w - 1)))


def _proximity(now_price, stop):
    """True when price sits within 3% above the stop — OR anywhere BELOW it.
    No lower bound: price under the stop means the stop should have FIRED and
    didn't (the true emergency); it must never demote back to a calm state.
    Promotes to the fault tier and paints the band segment red."""
    if not (now_price and stop and stop > 0):
        return False
    r = (now_price - stop) / stop
    return r < 0.03


def _render_band(lo, hi, band_w, *, fills=(), pendings=(), stop=None,
                 now_price=None, avg_entry=None, faint=False):
    """A Text of exactly band_w cells. Layers, later overwrites earlier:
    track ─ FAINT → proximity segment RISK → stop ┊ RISK → pending ○ ATTN →
    fill • THESIS → cursor ▼ (GAIN≥ø-entry / LOSS below / steel no-pos)."""
    cells = [["─", FAINT] for _ in range(max(0, band_w))]
    if not cells or lo is None or hi is None or hi <= lo:
        return Text("─" * max(1, band_w), style=FAINT)

    def col(p):
        return _band_col(p, lo, hi, band_w)

    cur_col = col(now_price)
    stop_col = col(stop)
    if _proximity(now_price, stop) and stop_col is not None and cur_col is not None:
        for i in range(min(stop_col, cur_col), max(stop_col, cur_col) + 1):
            cells[i] = ["─", RISK]
    if stop_col is not None:
        cells[stop_col] = ["┊", RISK]
    for p in pendings:
        c = col(p)
        if c is not None:
            cells[c] = ["○", ATTN]
    for p in fills:
        c = col(p)
        if c is not None:
            cells[c] = ["•", THESIS]
    if cur_col is not None:
        if avg_entry is None:
            cur_style = PRICE
        elif now_price >= avg_entry:
            cur_style = GAIN
        else:
            cur_style = LOSS
        cells[cur_col] = ["▼", cur_style]

    t = Text()
    for ch, style in cells:
        t.append(ch, style=("dim " + style) if faint else style)
    return t


# ── derived pair meta (pure) ─────────────────────────────────────────────────

def _actionable(ps, now):
    """A BUY the bot would act on right now: confirmed BUY, not stale, not on
    cooldown. Preserved from v5 — still the single most important derived state."""
    card = ps.confirmed if ps else None
    if not card or card.status != "BUY":
        return False
    if engine.is_stale(ps.tick_age(now), config.STALE_SECS):
        return False
    return ps.cooldown_until <= now


def _pair_view(appstate, sym, now):
    """Everything a strip/band needs for one pair, read from AppState only."""
    ps = appstate.pairs.get(sym)
    card = ps.confirmed if ps else None
    tick = ps.last_tick if ps else None
    price = tick.last if tick else (card.price if card else None)
    age = ps.tick_age(now) if ps else float("inf")
    stale = engine.is_stale(age, config.STALE_SECS)
    bp = appstate.exec.get("by_pair", {}).get(sym, {})
    fills = bp.get("fills", [])
    pendings = bp.get("pendings", [])
    vol_sum = bp.get("vol_sum", 0.0)
    avg_entry = bp.get("avg_entry")
    stop = bp.get("stop")
    has_fills = bool(fills)
    is_buy = bool(card and card.status == "BUY" and not stale)
    prox = has_fills and _proximity(price, stop)
    pnl = None
    if has_fills and price is not None:
        pnl = sum((price - (f["entry"] or 0.0)) * (f["vol"] or 0.0) for f in fills)
    return {
        "ps": ps, "card": card, "tick": tick, "price": price, "age": age,
        "stale": stale, "fills": fills, "pendings": pendings, "vol_sum": vol_sum,
        "avg_entry": avg_entry, "stop": stop, "has_fills": has_fills,
        "is_buy": is_buy, "prox": prox, "pnl": pnl,
        "lo": card.low_52w if card else None, "hi": card.high_52w if card else None,
    }


def _tier(v):
    """Attention tier (§ FIELD sort): 0 fault (proximity, OR stale data feeding a
    live position) · 1 BUY · 2 position w/o BUY · 3 watch · 4 idle. Stale WITHOUT a
    position stays where its score puts it — old data only matters when money rides
    on it."""
    card = v["card"]
    if v["prox"]:
        return 0
    if v["stale"] and v["has_fills"]:
        return 0
    if v["is_buy"]:
        return 1
    if v["has_fills"]:
        return 2
    if card and (card.status == "WATCH" or card.score >= 3):
        return 3
    return 4


def _attention_sorted(appstate, now):
    """Ordered [(sym, tier, view)] — faults, then BUYs (score desc), positions,
    watch (score desc), idle (alpha)."""
    rows = []
    for sym in PAIR_LIST:
        v = _pair_view(appstate, sym, now)
        rows.append((sym, _tier(v), v))

    def key(item):
        sym, tier, v = item
        score = v["card"].score if v["card"] else -1
        return (tier, -score, sym)
    return sorted(rows, key=key)


def _resolution(tier, v):
    """active (2 rows: data+band) · watch (1 row bare band) · idle (1 faint)."""
    if tier in (0, 1, 2) or v["has_fills"] or v["is_buy"]:
        return "active"
    if tier == 3:
        return "watch"
    return "idle"


# ── VIEW 1 · FIELD — header ──────────────────────────────────────────────────

def render_header(appstate, w):
    now_local = datetime.datetime.now(DENVER)
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    uptime = time.time() - appstate.started_ts

    top = Table.grid(expand=True)
    top.add_column(justify="left")
    top.add_column(justify="right")
    left = Text()
    left.append("DEEPFIELD  ", style=f"bold {THESIS}")
    left.append(f"survey · v{VERSION}", style=INK)
    right = Text()
    right.append(now_local.strftime("%a %b %d").lower() + " · ", style=INK)
    right.append(now_local.strftime("%H:%M:%S"), style=TIME)
    right.append(" mt · ", style=INK)
    right.append(now_utc.strftime("%H:%M"), style=TIME)
    right.append(" utc · up ", style=INK)
    right.append(_fmt_hms(uptime), style=TIME)
    top.add_row(left, right)

    return Group(top, render_exec_line(appstate), _btc_regime_line(appstate), _sep(w))


def render_exec_line(appstate):
    """Header status line: link · recon · stops · exec · equity · day P&L · pos ·
    bids · halt. (Name kept from v5 for the dashboard test hook.)"""
    ex = appstate.exec
    t = Text()
    t.append("link ", style=INK)
    if appstate.links:
        for name in sorted(appstate.links):
            up = appstate.links[name].get("up", False)
            t.append("●", style=HEALTH if up else RISK)
    else:
        t.append("··", style=INK)
    # LIVE stop coverage — open rows carrying a resting stop txid, refreshed every
    # 15s. This is the safety-reading number; a fill landing between reconciles must
    # not read as a fault, so it tracks the live book, not the boot stamp.
    stot, scov = ex.get("stops_total"), ex.get("stops_covered")
    if stot is not None:
        t.append(" · ", style=INK)
        t.append(f"stops {scov}/{stot}", style=HEALTH if scov >= stot else f"bold {RISK}")
    # recon = the DEEPER boot-time invariant check (Kraken volume reconcile), shown
    # separately as its own stamp so it isn't confused with live coverage.
    t.append(" · recon ", style=INK)
    rec = _parse_recon(ex.get("last_recon"))
    if rec is None:
        t.append("?", style=INK)
    elif rec.get("all_ok"):
        t.append("ok ", style=HEALTH)
        # date + time — after multi-day uptime a bare "03:14" reads as today's
        t.append(_local_short(rec.get("ts")), style=TIME)
    else:
        t.append("▢ MISMATCH", style=f"bold {RISK}")
    mode = ex.get("mode", "off")
    t.append(" · ", style=INK)
    if mode == "off":
        t.append("exec off", style=INK)
        t.append(" · signal-only", style=INK)
        return t
    t.append("exec ", style=INK)
    t.append(mode.upper(), style=f"bold {THESIS}")
    eq = ex.get("equity")
    if eq is not None:
        t.append(" · equity ", style=INK)
        t.append(f"${eq:,.2f}", style=PRICE)
    # 'open' = unrealized mark-to-market of live positions (lifetime, not today);
    # 'day'  = realized P&L closed since day0 (only moves on a CLOSE -> $0 until a stop);
    # 'swing' = live mark-to-market MOVE since UTC midnight (how the book did today).
    upnl = _open_pnl(appstate)
    rpnl = ex.get("realized_day", 0.0) or 0.0
    t.append(" · open ", style=INK)
    t.append(_fmt_usd(upnl), style=GAIN if upnl >= 0 else LOSS)
    t.append(" · day ", style=INK)
    t.append(_fmt_usd(rpnl), style=GAIN if rpnl >= 0 else LOSS)
    swing = ex.get("swing_day")
    if swing is not None:
        t.append(" · swing ", style=INK)
        t.append(_fmt_usd(swing), style=GAIN if swing >= 0 else LOSS)
    t.append(" · ", style=INK)
    t.append(f"{ex.get('open_count', 0)} pos", style=QTY)
    t.append(" · ", style=INK)
    t.append(f"{len(ex.get('pending', []))} bids", style=QTY)
    t.append(" · halt ", style=INK)
    if ex.get("halt"):
        t.append("● HALTED", style=f"bold {RISK}")
    elif not ex.get("rails_ok", True):
        t.append(f"● {ex.get('rails_reason', 'blocked')}", style=f"bold {RISK}")
    else:
        t.append("—", style=INK)
    return t


def _btc_regime_line(appstate):
    now = time.time()
    t = Text()
    t.append_text(_countdown("DAILY", appstate.daily_interval_begin, 86400, _fmt_hms, now))
    t.append("    ")
    t.append_text(_countdown("WEEKLY", appstate.weekly_interval_begin, 604800, _fmt_days_hm, now))
    t.append("    ")
    ps = appstate.pairs.get("BTC/USD")
    if ps and ps.last_tick:
        tick = ps.last_tick
        t.append("BTC ", style=INK)
        t.append(_fmt_price(tick.last), style=PRICE)
        t.append(f" {tick.change_pct:+.1f}%", style=GAIN if tick.change_pct >= 0 else LOSS)
    r = appstate.regime
    if r is not None:
        t.append(" · ", style=INK)
        t.append((r.label or "?").lower(), style=INK)
        note = _REGIME_NOTE.get(r.label)
        if note:
            t.append(f" · {note}", style=INK)
        if r.daily_rsi is not None:
            t.append(" · d-rsi ", style=INK)
            t.append(f"{r.daily_rsi:.0f}", style=IND)
    return t


_REGIME_NOTE = {"BULL": "above & rising w-ema200", "BEAR": "below w-ema200",
                "RECOVERY": "above, ema flattening", "NEUTRAL": "ema flat"}


def _countdown(label, begin, interval_secs, fmt, now):
    t = Text()
    t.append(f"{label} ", style=INK)
    if begin is None:
        t.append("closes ?", style=INK)
        return t
    left = begin + interval_secs - now
    frac = (now - begin) / interval_secs
    if left <= 0:
        t.append("closing…", style=ATTN)
        return t
    t.append(_mini_bar(frac) + " ", style=TIME)
    t.append(f"{frac * 100:3.0f}% ", style=INK)
    t.append(fmt(left), style=TIME)
    return t


def _parse_recon(raw):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _open_pnl(appstate):
    total = 0.0
    for p in appstate.exec.get("positions", []):
        ps = appstate.pairs.get(p["symbol"])
        tick = ps.last_tick if ps else None
        card = ps.confirmed if ps else None
        # SAME price selection as _pair_view: fall back to the last daily close
        # (card.price) when there's no live tick, so this header total reconciles
        # with the sum of the per-pair FIELD/LEDGER/BOOK rows (which do the same)
        cur = tick.last if tick else (card.price if card else None)
        e_, v_ = p.get("entry"), p.get("volume")
        if None not in (cur, e_, v_):
            total += (cur - e_) * v_
    return total


# ── VIEW 1 · FIELD — strips + accordion ──────────────────────────────────────

def _strip_state(v, now):
    ps, card = v["ps"], v["card"]
    if v["prox"]:
        stop, price = v["stop"], v["price"]
        if stop and price is not None and price < stop:
            # price UNDER the stop = the stop should have fired — loudest state
            return "▼BELOW STOP", f"bold {RISK}"
        return "NEAR-STOP", RISK
    if v["stale"]:
        return "STALE", ATTN
    if v["is_buy"]:
        if ps and ps.cooldown_until > now:
            return "BUY⏳", ATTN
        return "BUY", THESIS
    if v["has_fills"]:                 # money on the table outranks a faded thesis
        return "HOLD", HEALTH
    if card and card.status == "WATCH":
        return "WCH", TIME
    return "idle", FAINT


_STRIP_COLS = ((7, "left"), (9, "left"), (10, "right"), (8, "right"),
               (11, "right"), (10, "right"), (12, "left"))


def _band_bounds(v):
    """Band scale follows the tier (resolution follows relevance, on the x-axis).
    An ACTIVE pair zooms to its TRADE ZONE, anchored to the fills / resting bids /
    cursor — NOT the stop — so the entry ladder breathes across the full width
    instead of being squeezed against the stop→cheapest-fill buffer. The stop
    normally sits BELOW the zone and is drawn as an off-scale ◄ marker in the
    label; when price NEARS the stop (proximity) the zone extends down to keep the
    stop — and its red alarm segment — on-scale, exactly when it matters. Watch/idle
    keep the YEAR scale (cursor-hugging-the-yearly-low IS their information).
    Returns (lo, hi, year_note)."""
    if v["has_fills"] and v["stop"]:
        price, stop = v["price"], v["stop"]
        refs = [f["entry"] for f in v["fills"] if f["entry"]]
        refs += [p["price"] for p in v["pendings"] if p.get("price")]
        if price:
            refs.append(price)
        lo = min(refs) * 0.99
        hi = max(refs) * 1.01
        if _proximity(price, stop):
            lo = min(lo, stop * 0.995)          # pull the stop on-scale near danger
        if hi <= lo:
            hi = lo * 1.02
        card = v["card"]
        note = (_pct_low_note(card.pct_above_low)
                if (card and card.pct_above_low is not None) else None)
        return lo, hi, note
    return v["lo"], v["hi"], None


def _pct_low_note(pct):
    """One canonical '% above 52w low' label — used identically on active bands
    and watch rows so the frame speaks one dialect."""
    return f"+{pct:.0f}% ↑52w low"


def _field_strip(appstate, sym, tier, v, w, selected, now):
    res = _resolution(tier, v)
    disp = DISPLAY.get(sym, sym)
    st_label, st_style = _strip_state(v, now)

    if res == "idle":
        band = _render_band(v["lo"], v["hi"], max(20, w - 22),
                            now_price=v["price"], faint=True)
        line = Text(f"  {disp:<5} ", style=FAINT)
        line.append_text(band)
        line.append(f"  {st_label}", style=FAINT)
        if selected:
            line.stylize(SEL_BG)
        return [line]

    if res == "watch":
        # ONE row (spec): sym + score + price + a bare YEAR-scale band (track+cursor)
        card = v["card"]
        pct = (" " + _pct_low_note(card.pct_above_low)
               if (card and card.pct_above_low is not None) else "")
        band_w = max(20, w - 46 - len(pct))
        band = _render_band(v["lo"], v["hi"], band_w, now_price=v["price"])
        line = Text(f"  {disp:<5} ", style=TIME)
        if card:
            line.append_text(_dots(card.score, card.denom))
        line.append(f" {_fmt_price(v['price']):>10}  ", style=PRICE)
        line.append_text(band)
        line.append(f" {_fmt_price(v['hi']):<9}", style=INK)
        if pct:
            line.append(pct, style=INK)          # restore the thesis number in 1-row form
        if selected:
            line.stylize(SEL_BG)
        return [line]

    # active: 2 rows (data + trade-zone band)
    data = Table.grid(expand=False, padding=(0, 1, 0, 0))
    for wdt, jst in _STRIP_COLS:
        data.add_column(width=wdt, justify=jst, no_wrap=True)
    sym_style = f"bold {THESIS}" if v["is_buy"] else PRICE
    card = v["card"]
    chg = v["tick"].change_pct if v["tick"] else None
    pos_cell = (Text(f"{len(v['fills'])}f·{v['vol_sum']:g}", style=QTY)
                if v["has_fills"] else Text("—", style=INK))
    pnl_cell = (Text(_fmt_usd(v["pnl"]), style=GAIN if v["pnl"] >= 0 else LOSS)
                if v["pnl"] is not None else Text("—", style=INK))
    data.add_row(
        Text(f"  {disp}", style=sym_style),
        _dots(card.score, card.denom) if card else Text("—", style=INK),
        Text(_fmt_price(v["price"]), style=PRICE),
        (Text(f"{chg:+.1f}%", style=GAIN if chg >= 0 else LOSS)
         if chg is not None else Text("—", style=INK)),
        pos_cell, pnl_cell,
        Text(st_label, style=st_style),
    )
    rows = [data]

    blo, bhi, note = _band_bounds(v)
    stop, price = v["stop"], v["price"]
    # Label the TRUE stop (safety-relevant). It normally sits BELOW the zone, so mark
    # it off-scale with ◄ and don't plot a (clamped, misleading) tick; when proximity
    # pulls it on-scale, the ┊ tick + red alarm segment render inside the band.
    stop_on = stop is not None and blo <= stop <= bhi
    lbl = Text("stop ", style=INK)
    lbl.append(_fmt_price(stop) if stop else "—", style=RISK if stop else INK)
    if stop and not stop_on:
        lbl.append(" ◄", style=RISK)
    rt = Text()
    if stop and price:
        rt.append(f"  {(stop / price - 1) * 100:+.1f}% stop", style=INK)
    if note:
        rt.append(f" · {note}", style=INK)
    band_w = max(20, w - 6 - len(lbl.plain) - len(rt.plain))
    band = _render_band(blo, bhi, band_w,
                        fills=[f["entry"] for f in v["fills"]],
                        pendings=[p["price"] for p in v["pendings"]],
                        stop=stop if stop_on else None,
                        now_price=price, avg_entry=v["avg_entry"])
    bline = Text("     ")
    bline.append_text(lbl)
    bline.append(" ")
    bline.append_text(band)
    bline.append_text(rt)
    rows.append(bline)

    if selected:
        for r in rows:
            if isinstance(r, Text):
                r.stylize(SEL_BG)
    return rows


def _expansion(appstate, sym, v, w):
    """THESIS (left) + LEDGER (right) block under the focused strip."""
    grid = Table.grid(expand=True, padding=(0, 2, 0, 0))
    grid.add_column(width=max(30, w * 42 // 100), vertical="top")
    grid.add_column(vertical="top")
    grid.add_row(_thesis_block(appstate, sym, v), _ledger_block(appstate, sym, v))
    return grid


def _expansion_rows(appstate, v):
    """Actual rendered height of the accordion = max(THESIS col, LEDGER col), so
    the FIELD budget reserves the true cost (not a flat guess) and the height law
    stays airtight — the loop drops an idle pair before the keybar could clip."""
    card = v["card"]
    thesis = 1 + (((len(card.results) + 1) // 2) if (card and card.results) else 0) + 1
    nfills = len(v["fills"])
    scroll = max(0, appstate.ledger_scroll)
    shown = min(10, max(0, nfills - scroll))
    ledger = (2 + shown + min(3, len(v["pendings"]))
              + (1 if nfills > scroll + 10 else 0) + (1 if nfills else 0))
    return max(thesis, ledger)


def _thesis_block(appstate, sym, v):
    from .signals import FIRED
    card = v["card"]
    lines = []
    head = Text("  THESIS", style=INK)
    if card:
        head.append(f"  ·  {card.score}/{card.denom} · required {card.required}", style=INK)
    lines.append(head)
    if card and card.results:
        names = [(r.name, r.state == FIRED) for r in card.results]
        for i in range(0, len(names), 2):
            row = Text("   ")
            for name, fired in names[i:i + 2]:
                row.append("✓ " if fired else "· ", style=THESIS if fired else FAINT)
                row.append(f"{name:<22}", style=PRICE if fired else INK)
            lines.append(row)
    note = Text("   ", style=INK)
    if v["stop"] is not None:
        note.append(f"stop {_fmt_price(v['stop'])}", style=RISK)
        note.append(" · support-invalidation", style=INK)
    ps = v["ps"]
    if ps and ps.exec_plan:
        pl = ps.exec_plan
        note.append("  · next ", style=INK)
        note.append(f"{pl.get('volume', 0):g}", style=QTY)
        note.append(" @ ", style=INK)
        note.append(_fmt_price(pl.get("entry")), style=PRICE)
    lines.append(note)
    return Group(*lines)


_LEDGER_COLS = ((4, "right"), (9, "right"), (11, "right"), (11, "right"),
                (11, "right"), (10, "right"), (7, "left"))


def _ledger_block(appstate, sym, v):
    lines = [Text(f"  LEDGER · {DISPLAY.get(sym, sym)}", style=INK)]
    fills = list(reversed(v["fills"]))          # newest first
    price = v["price"]
    scroll = max(0, appstate.ledger_scroll)
    window = fills[scroll:scroll + 10]
    hdr = Table.grid(expand=True, padding=(0, 1, 0, 0))
    for wdt, jst in _LEDGER_COLS:
        hdr.add_column(width=wdt, justify=jst, no_wrap=True)
    hdr.add_row(*[Text(h, style=INK) for h in
                  ("#", "vol", "entry", "now", "uP&L", "stop", "state")])
    lines.append(hdr)
    for idx, f in enumerate(window, start=scroll + 1):
        upnl = ((price - (f["entry"] or 0.0)) * (f["vol"] or 0.0)) if price is not None else None
        tbl = Table.grid(expand=True, padding=(0, 1, 0, 0))
        for wdt, jst in _LEDGER_COLS:
            tbl.add_column(width=wdt, justify=jst, no_wrap=True)
        tbl.add_row(
            Text(str(idx), style=INK),
            Text(f"{f['vol']:g}", style=QTY),
            Text(_fmt_price(f["entry"]), style=PRICE),
            Text(_fmt_price(price), style=PRICE),
            (Text(_fmt_usd(upnl), style=GAIN if upnl >= 0 else LOSS)
             if upnl is not None else Text("—", style=INK)),
            Text(_fmt_price(f["stop"]), style=RISK),
            Text("live", style=HEALTH),
        )
        lines.append(tbl)
    for p in v["pendings"][:3]:
        pl = Text("   ○ ", style=ATTN)
        pl.append(f"{p['vol']:g}", style=QTY)
        pl.append(" @ ", style=INK)
        pl.append(_fmt_price(p["price"]), style=PRICE)
        pl.append("  resting post-only", style=ATTN)
        lines.append(pl)
    if len(fills) > scroll + 10:
        lines.append(Text(f"   ({len(fills) - scroll - 10} more)  ,/. scroll", style=INK))
    if v["has_fills"]:
        sig = Text("   Σ ", style=INK)
        sig.append(f"{len(fills)}f·{v['vol_sum']:g}", style=f"bold {QTY}")
        sig.append("  ø ", style=INK)
        sig.append(_fmt_price(v["avg_entry"]), style=PRICE)
        sig.append("  uP&L ", style=INK)
        sig.append(_fmt_usd(v["pnl"]), style=f"bold {GAIN if (v['pnl'] or 0) >= 0 else LOSS}")
        if v["stop"] and v["avg_entry"]:
            away = (v["stop"] / v["avg_entry"] - 1) * 100
            sig.append(f"  stop {away:+.1f}%", style=RISK)
        lines.append(sig)
    return Group(*lines)


def render_field(appstate, w, height):
    now = time.time()
    ordered = _attention_sorted(appstate, now)
    if appstate.focus_symbol not in [s for s, _, _ in ordered]:
        appstate.focus_symbol = ordered[0][0] if ordered else None

    header = render_header(appstate, w)          # 4 rows (title/exec/btc/sep)
    # Fixed chrome = header(4) + sep(1) + margin(1) + keybar(1) = 7. The rest splits
    # between strips and a journal tail that ABSORBS whatever the strips don't use —
    # dead space becomes live narrative (15 lines quiet, shrinks to 1 on a busy field).
    avail = max(4, height - 7)
    body, used = [], 0
    for sym, tier, v in ordered:
        selected = (sym == appstate.focus_symbol)
        strip = _field_strip(appstate, sym, tier, v, w, selected, now)
        cost = len(strip)
        exp_block = None
        if sym == appstate.expanded_symbol:
            exp_block = _expansion(appstate, sym, v, w)
            cost += _expansion_rows(appstate, v)
        if used + cost > avail - 1 and body:     # always leave >=1 row for the journal
            break
        body.extend(strip)
        if exp_block is not None:
            body.append(exp_block)
        used += cost
    jlines = max(1, avail - used)
    return Group(header, Group(*body), _sep(w), _margin_line(appstate, w),
                 _journal_tail_block(appstate, jlines), _keybar())


def _margin_line(appstate, w):
    ex = appstate.exec
    used = ex.get("margin_used")
    free = ex.get("free_margin")
    bal = ex.get("balance") or {}
    try:
        lvl = float(bal.get("ml")) if bal.get("ml") else None
    except (TypeError, ValueError):
        lvl = None
    m = Text("margin ", style=INK)
    if used is not None and free is not None and (used + free) > 0:
        frac = used / (used + free)
        m.append("▕" + _mini_bar(frac, 10) + "▏ ", style=QTY)
        m.append(f"${used:,.2f}", style=QTY)
        m.append(" used · ", style=INK)
        m.append(f"${free:,.2f}", style=QTY)
        m.append(" free", style=INK)
    else:
        m.append("—", style=INK)
    if lvl is not None:
        lvl_style = HEALTH if lvl > 500 else (ATTN if lvl >= 150 else RISK)
        m.append("  · lvl ", style=INK)
        m.append(f"{lvl:,.0f}%", style=lvl_style)
    cap = ex.get("capacity")
    if cap is not None:
        m.append("    capacity ≈ ", style=INK)
        m.append(f"{cap}", style=QTY)
        m.append(" min-fills", style=INK)
    return m


def _journal_tail_block(appstate, n):
    """The last n journal events, newest first — the 'latest' line grown into a
    live narrative that fills whatever height the strips leave (Fix 3). Padded to
    EXACTLY n rows so the keybar pins to a fixed floor while history is short."""
    n = max(1, n)
    tail = appstate.exec.get("journal_tail", [])
    lines = []
    if not tail:
        lines.append(Text("latest — (quiet)", style=INK))
    for ts, kind, symbol, text in tail[:n]:
        row = Text(_local_hms(ts) + " ", style=TIME)   # HH:MM:SS — ordering is fact, not faith
        row.append(f"{(kind or ''):<7}", style=KIND_TAG_STYLE.get(kind, INK))
        row.append(f"{symbol + ' ' if symbol else ''}", style=PRICE)
        row.append(text or "", style=FAINT)
        lines.append(row)
    while len(lines) < n:                               # pad so the floor is fixed
        lines.append(Text(""))
    return Group(*lines)


def _keybar():
    return Text("1 field · 2 book · 3 journal · j/k select · enter expand · ,/. scroll · "
                "q quit · p pause · f reconcile", style=FAINT)


# ── VIEW 2 · BOOK ────────────────────────────────────────────────────────────

def render_book(appstate, w, height):
    now = time.time()
    rec = _parse_recon(appstate.exec.get("last_recon"))
    per_pair = rec.get("per_pair", {}) if rec else {}
    by_pair = appstate.exec.get("by_pair", {})
    head = [Text("BOOK  ", style=f"bold {INK}"),
            Text("open positions & resting orders, grouped per pair", style=INK), _sep(w)]
    syms = [s for s in PAIR_LIST
            if by_pair.get(s, {}).get("fills") or by_pair.get(s, {}).get("pendings")]
    if not syms:
        head.append(Text("  (no open positions or resting orders)", style=INK))
        return Group(*head)
    lines = []                               # body rows — windowed by book_scroll
    for sym in syms:
        v = _pair_view(appstate, sym, now)
        ph = Text("▍ ", style=f"dim {THESIS}")          # per-pair header (NOT the outer `head`)
        ph.append(f"{DISPLAY.get(sym, sym)}", style=f"bold {PRICE}")
        ph.append(f"  {len(v['fills'])}f", style=QTY)
        ph.append(f" · {v['vol_sum']:g} vol", style=QTY)
        ph.append("  · ø ", style=INK)
        ph.append(_fmt_price(v["avg_entry"]), style=PRICE)
        ph.append("  · uP&L ", style=INK)
        ph.append(_fmt_usd(v["pnl"]), style=GAIN if (v["pnl"] or 0) >= 0 else LOSS)
        ph.append("  · stop ", style=INK)
        ph.append(_fmt_price(v["stop"]), style=RISK)
        if v["stop"] and v["avg_entry"]:
            ph.append(f" {(v['stop'] / v['avg_entry'] - 1) * 100:+.1f}%", style=RISK)
        pp = per_pair.get(sym)
        if pp is not None:
            coherent = pp.get("stops", 0) >= pp.get("rows", 0)
            ph.append("   ", style=INK)
            ph.append("▣ coherent" if coherent else "▢ MISMATCH",
                      style=HEALTH if coherent else f"bold {RISK}")
            # this is the BOOT-TIME Kraken volume reconcile, not live coverage —
            # stamp it with the recon time so it can't be misread as current
            # protection (live per-fill coverage is the 'live'/no-stop tag below)
            if rec and rec.get("ts"):
                ph.append(f" @recon {_local_short(rec.get('ts'))}", style=TIME)
        lines.append(ph)
        price = v["price"]
        for idx, f in enumerate(reversed(v["fills"]), start=1):
            upnl = ((price - (f["entry"] or 0.0)) * (f["vol"] or 0.0)) if price is not None else None
            row = Text(f"    #{idx:<3}", style=INK)
            row.append(f"{f['vol']:g}@{f['lev']}x ", style=QTY)
            row.append(f"{_fmt_price(f['entry']):>10}", style=PRICE)
            row.append(f" now {_fmt_price(price):>10}", style=PRICE)
            row.append(f"  {_fmt_usd(upnl):>9}", style=GAIN if (upnl or 0) >= 0 else LOSS)
            row.append(f"  stop {_fmt_price(f['stop'])}", style=RISK)
            # 'live' == a resting stop order actually protects this fill. Gate on the
            # stop TXID, not the mere stop price, so an unprotected fill (stop price
            # recorded but no resting order) can't render as fine.
            if f.get("stop_txid"):
                row.append("  live", style=HEALTH)
            else:
                row.append("  ⚠ no-stop", style=f"bold {RISK}")
            lines.append(row)
        for p in v["pendings"]:
            row = Text("    ○   ", style=ATTN)
            row.append(f"{p['vol']:g}", style=QTY)
            row.append(" @ ", style=INK)
            row.append(_fmt_price(p["price"]), style=PRICE)
            row.append("  resting post-only", style=ATTN)
            lines.append(row)
        lines.append(Text(""))
    # window the body by book_scroll (,/. keys); clamp so you can't scroll to void
    visible = max(6, height - len(head) - 1)
    scroll = min(max(0, appstate.book_scroll), max(0, len(lines) - visible))
    window = lines[scroll:scroll + visible]
    if len(lines) > scroll + visible:
        window.append(Text(f"  ({len(lines) - scroll - visible} more)  ,/. scroll", style=INK))
    if scroll > 0:
        window.insert(0, Text(f"  (↑ {scroll} above)", style=INK))
    return Group(*head, *window)


# ── VIEW 3 · JOURNAL ─────────────────────────────────────────────────────────

def render_journal(appstate, w, height):
    tail = appstate.exec.get("journal_tail", [])
    lines = [Text("JOURNAL  ", style=f"bold {INK}"),
             Text("the system, narrating itself — newest first", style=INK), _sep(w)]
    if not tail:
        lines.append(Text("  (no events yet)", style=INK))
        return Group(*lines)
    scroll = max(0, appstate.journal_scroll)
    window = tail[scroll:scroll + max(6, height - 6)]
    last_day = None
    for ts, kind, symbol, text in window:
        try:
            dt = datetime.datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            day = dt.astimezone(DENVER).strftime("%A · %B %d").upper()
        except Exception:
            day = None
        if day and day != last_day:
            lines.append(Text(f"  {day}", style=INK))
            last_day = day
        row = Text("  ")
        row.append(_local_hms(ts) + "  ", style=TIME)
        row.append(f"{(kind or '').upper():<8}", style=KIND_TAG_STYLE.get(kind, INK))
        if symbol:
            row.append(symbol + " ", style=PRICE)
        row.append(text or "", style=FAINT)
        lines.append(row)
    if len(tail) > scroll + len(window):
        lines.append(Text(f"  ({len(tail) - scroll - len(window)} older)  ,/. scroll", style=INK))
    return Group(*lines)


# ── navigation (pure AppState mutators — the KeyController handlers call these) ─

def nav_view(appstate, n):
    """1/2/3 — switch view, keep focus, collapse expansion (§4)."""
    appstate.view = n
    appstate.expanded_symbol = None
    appstate.ledger_scroll = 0


def nav_select(appstate, delta):
    """j/k/↑/↓ — move focus through the attention-sorted order, clamped."""
    order = [s for s, _, _ in _attention_sorted(appstate, time.time())]
    if not order:
        return
    cur = appstate.focus_symbol
    idx = order.index(cur) if cur in order else 0
    appstate.focus_symbol = order[max(0, min(len(order) - 1, idx + delta))]


def nav_expand(appstate):
    """enter/space — toggle the ONE expanded pair (the focused one)."""
    appstate.expanded_symbol = (None if appstate.expanded_symbol == appstate.focus_symbol
                                else appstate.focus_symbol)
    appstate.ledger_scroll = 0


def nav_scroll(appstate, delta):
    """,/. — scroll the active scrollable for the current view."""
    if appstate.view == 2:
        appstate.book_scroll = max(0, appstate.book_scroll + delta)
    elif appstate.view == 3:
        appstate.journal_scroll = max(0, appstate.journal_scroll + delta)
    else:
        appstate.ledger_scroll = max(0, appstate.ledger_scroll + delta)


# ── frame dispatch + Live loop ───────────────────────────────────────────────

def render_frame(appstate, conn=None, total_width=120, height=54, show_keys=False):
    w = min(total_width - 1, 236)
    view = getattr(appstate, "view", 1)
    if view == 2:
        return render_book(appstate, w, height)
    if view == 3:
        return render_journal(appstate, w, height)
    return render_field(appstate, w, height)


def export_frame_text(appstate, conn=None, width=120, height=54, styles=False):
    """Proof helper: a static export of one live frame (read-only). Renders to an
    in-memory buffer (never echoes to stdout) and returns the INTRINSIC height of
    the frame — no screen padding — so acceptance #1 can grep the true row count.
    styles=True keeps the ANSI for a visual dump."""
    import io
    console = Console(width=width, record=True, color_system="truecolor",
                      file=io.StringIO())
    console.print(render_frame(appstate, conn, total_width=width, height=height))
    return console.export_text(styles=styles)


async def run_ui(appstate, conn=None, hz=None, show_keys=False):
    hz = hz or config.RENDER_HZ
    console = Console()
    key_evt = getattr(appstate, "_key_evt", None)   # set by run_live for instant redraw
    with Live(console=console, screen=True, auto_refresh=False) as live:
        while True:
            if appstate.paused:
                if appstate.pause_dirty:
                    live.update(render_frame(appstate, conn, console.width, console.height, show_keys),
                                refresh=True)
                    appstate.pause_dirty = False
                await asyncio.sleep(0.1)
                continue
            live.update(render_frame(appstate, conn, console.width, console.height, show_keys),
                        refresh=True)
            # Wake immediately on a keypress (view/select/expand/scroll), else tick
            # at the render cap so clocks/countdowns/prices still advance.
            if key_evt is not None:
                try:
                    await asyncio.wait_for(key_evt.wait(), timeout=1.0 / hz)
                except asyncio.TimeoutError:
                    pass
                key_evt.clear()
            else:
                await asyncio.sleep(1.0 / hz)
