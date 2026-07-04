"""--simple plaintext mode (REQUIRED). SPEC §8.

No rich — a v4.4-style plaintext frame printed every SIMPLE_SECS from the same
engine-published state (invariant 5: never re-implements a formula). Narrow-
width safe (~50-60 cols) for dumb terminals, logging, and cron — this is the
fallback ui.py's rich Table can't be, over a truly minimal SSH session.
"""
import time
import shutil
import asyncio
import datetime

from . import store
from . import engine
from . import config
from . import VERSION
from .signals import FIRED, NOT, NA

DISPLAY = {p["ws"]: p["display"] for p in config.PAIRS}
PAIR_LIST = [p["ws"] for p in config.PAIRS]

STATUS_SHORT = {"BUY": "BUY", "WATCH": "WCH", "---": "---", "STALE": "STALE"}


def _width():
    try:
        return min(shutil.get_terminal_size().columns, 60)
    except Exception:
        return 50


def _fmt_price(p):
    if p is None:
        return "---"
    if p >= 1000:
        return f"${p:,.0f}"
    if p >= 1:
        return f"${p:.2f}"
    return f"${p:.4f}"


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


def render_frame_text(appstate, conn):
    W = _width()
    sep = "-" * W
    now = time.time()
    lines = []
    lines.append(f"DEEPFIELD v{VERSION}   {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("LINK UP" if appstate.link_up else "LINK DOWN")
    lines.append(sep)

    if appstate.daily_interval_begin is not None and appstate.weekly_interval_begin is not None:
        d_left = _fmt_hms(appstate.daily_interval_begin + 86400 - now)
        w_left = _fmt_days_hm(appstate.weekly_interval_begin + 604800 - now)
        lines.append(f"D closes {d_left}  W closes {w_left}")
    else:
        lines.append("D closes ?  W closes ?")

    btc = appstate.pairs.get("BTC/USD")
    if btc and btc.last_tick:
        t = btc.last_tick
        lines.append(f"BTC {_fmt_price(t.last)} {t.change_pct:+.1f}%  24h H:{_fmt_price(t.high24)} L:{_fmt_price(t.low24)}")
    else:
        lines.append("BTC LIVE unavailable")
    lines.append(sep)

    r = appstate.regime
    if r:
        drsi = f"{r.daily_rsi:.0f}" if r.daily_rsi is not None else "---"
        mrsi = f"{r.monthly_rsi:.0f}" if r.monthly_rsi is not None else "---"
        danger = "  [!] DANGER" if r.danger else ""
        lines.append(f"BTC {r.label}  D-RSI {drsi}  M-RSI {mrsi}{danger}")
    else:
        lines.append("BTC regime: unknown")
    lines.append(sep)

    lines.append(f"{'SYM':<5}{'SCR':>5}{'PROV':>6}{'WRSI':>6}{'DRSI':>6}{'PRICE':>10}  ST")
    lines.append(sep)

    def sort_key(sym):
        ps = appstate.pairs.get(sym)
        card = ps.confirmed if ps else None
        return (-(card.score if card else -1), sym)

    for sym in sorted(PAIR_LIST, key=sort_key):
        ps = appstate.pairs.get(sym)
        card = ps.confirmed if ps else None
        prov = ps.provisional if ps else None
        tick = ps.last_tick if ps else None
        age = ps.tick_age() if ps else float("inf")
        stale = engine.is_stale(age, config.STALE_SECS)
        status = "STALE" if stale else (card.status if card else "---")

        scr_s = f"{card.score}/{card.denom}" if card else "---"
        prov_s = f"±{prov.score}" if prov else "·"
        wrsi_s = f"{card.weekly_rsi:.0f}" if card and card.weekly_rsi is not None else "---"
        drsi_s = f"{card.daily_rsi:.0f}" if card and card.daily_rsi is not None else "---"
        price_s = _fmt_price(tick.last if tick else (card.price if card else None))
        lines.append(f"{DISPLAY.get(sym, sym):<5}{scr_s:>5}{prov_s:>6}{wrsi_s:>6}{drsi_s:>6}{price_s:>10}  {STATUS_SHORT.get(status, status)}")
    lines.append(sep)

    champion = None
    for sym in PAIR_LIST:
        ps = appstate.pairs.get(sym)
        card = ps.confirmed if ps else None
        if card and card.status == "BUY":
            if champion is None or card.score > champion[1].score:
                champion = (sym, card, ps)
    if champion:
        sym, card, ps = champion
        disp = DISPLAY.get(sym, sym)
        lines.append(f"CHAMPION: {disp}  {card.score}/{card.denom}")
        live_price = ps.last_tick.last if ps.last_tick else None
        lines.append(f"  live entry {_fmt_price(live_price)}  last close {_fmt_price(card.price)}")
        if ps.tranche:
            lines.append(f"  tranche {ps.tranche.qty:g} {disp} (~${ps.tranche.qty * ps.tranche.price:,.2f})")
        for res in card.results:
            if res.state == FIRED:
                lines.append(f"    [x] {res.name}")
            elif res.state == NA:
                lines.append(f"    [~] {res.name} (N/A: {res.reason})")
    else:
        lines.append("No active BUY candidates this scan.")
    lines.append(sep)

    rows = store.recent_alerts(conn, 5)
    lines.append("ALERT TAIL")
    if not rows:
        lines.append("  (none yet)")
    for ts, symbol, price, score, denom, signals, kind in rows:
        lines.append(f"  {ts[:16]} {symbol:<8} {_fmt_price(price)} {score}/{denom} [{kind}]")
    lines.append(sep)

    return "\n".join(lines)


async def run_simple(appstate, conn, secs=None):
    secs = secs or config.SIMPLE_SECS
    while True:
        print(render_frame_text(appstate, conn))
        print()
        await asyncio.sleep(secs)
