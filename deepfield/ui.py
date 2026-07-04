"""rich Live TUI — AMOLED, flight-telemetry. SPEC §8, invariant 5.

Reads engine-published state ONLY; never re-implements a formula. Palette
mirrors v4.4's 256-color `C` table. Render loop caps at RENDER_HZ; price cells
read the latest tick state and flash on tick direction, but the layout itself
does not re-render per tick.

Note: "true black background" is a terminal-theme property, not something rich
can force without fighting the user's own terminal palette — this module
focuses on the foreground palette + color-as-information discipline, which is
achievable and portable (including over Termux SSH, no truecolor required).
"""
import time
import asyncio
import datetime
from zoneinfo import ZoneInfo

from rich.console import Console, Group
from rich.table import Table
from rich.text import Text
from rich.live import Live

from . import store
from . import engine
from . import config
from . import VERSION
from .signals import FIRED, NOT, NA

DENVER = ZoneInfo("America/Denver")

GOLD = "color(220)"
AMBER = "color(214)"
GREEN = "color(82)"
LIME = "color(118)"
CYAN = "color(51)"
RED = "color(196)"
ORANGE = "color(208)"
PURPLE = "color(141)"
GRAY = "color(240)"
SILVER = "grey70"
WHITE = "white"

DISPLAY = {p["ws"]: p["display"] for p in config.PAIRS}
PAIR_LIST = [p["ws"] for p in config.PAIRS]

STATUS_SHORT = {"BUY": "BUY", "WATCH": "WCH", "---": "---", "STALE": "STALE"}
STATUS_STYLE = {"BUY": GREEN, "WATCH": GOLD, "STALE": RED, "---": GRAY}
REGIME_STYLE = {"BULL": GREEN, "BEAR": RED, "RECOVERY": AMBER, "UNKNOWN": GRAY}


def _fmt_price(p):
    if p is None:
        return "---"
    if p >= 1000:
        return f"${p:,.0f}"
    if p >= 1:
        return f"${p:.2f}"
    return f"${p:.4f}"


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
    if d > 0:
        return f"{d}d {h:02d}:{m:02d}"
    return f"{h:02d}:{m:02d}"


# ── region 1: header ─────────────────────────────────────────────────────────

def render_header(appstate):
    now_local = datetime.datetime.now(DENVER)
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    uptime = time.time() - appstate.started_ts
    t = Text()
    t.append("DEEPFIELD ", style=f"bold {GOLD}")
    t.append(f"v{VERSION}  ", style=GOLD)
    t.append(f"{now_local.strftime('%H:%M:%S')} MT / {now_utc.strftime('%H:%M:%S')} UTC   ", style=SILVER)
    t.append("LINK UP" if appstate.link_up else "LINK DOWN", style=GREEN if appstate.link_up else RED)
    t.append(f"  (reconnects={appstate.reconnect_count})  ", style=GRAY)
    t.append(f"RECON={appstate.recon_repairs}  ", style=GRAY)
    t.append(f"uptime={_fmt_hms(uptime)}", style=GRAY)
    return t


# ── region 2: countdowns ─────────────────────────────────────────────────────

def render_countdowns(appstate):
    now = time.time()
    t = Text()
    if appstate.daily_interval_begin is not None:
        t.append(f"D closes {_fmt_hms(appstate.daily_interval_begin + 86400 - now)}", style=CYAN)
    else:
        t.append("D closes ?", style=GRAY)
    t.append("  ·  ", style=GRAY)
    if appstate.weekly_interval_begin is not None:
        t.append(f"W closes {_fmt_days_hm(appstate.weekly_interval_begin + 604800 - now)}", style=CYAN)
    else:
        t.append("W closes ?", style=GRAY)
    return t


# ── region 3: BTC pulse ──────────────────────────────────────────────────────

def render_btc_pulse(appstate):
    ps = appstate.pairs.get("BTC/USD")
    t = Text()
    if ps is None or ps.last_tick is None:
        t.append("BTC LIVE  unavailable", style=GRAY)
        return t
    tick = ps.last_tick
    chg_style = GREEN if tick.change_pct >= 0 else RED
    t.append("BTC ", style=f"bold {GOLD}")
    t.append(f"{_fmt_price(tick.last)}  ", style=WHITE)
    t.append(f"{tick.change_pct:+.1f}%", style=chg_style)
    t.append(f"   24h H:{_fmt_price(tick.high24)} L:{_fmt_price(tick.low24)}", style=SILVER)
    levels = config.LEVELS.get("BTC/USD", [])
    if levels:
        parts = [f"{label}:{(tick.last - price) / price * 100:+.1f}%" for label, price in levels]
        t.append("   " + "  ".join(parts), style=GRAY)
    return t


# ── region 4: regime ─────────────────────────────────────────────────────────

def render_regime(appstate):
    r = appstate.regime
    t = Text()
    if r is None:
        t.append("BTC regime: unknown (awaiting startup sweep / first close)", style=GRAY)
        return t
    t.append("BTC  ", style=SILVER)
    t.append(f"{r.label}  ", style=f"bold {REGIME_STYLE.get(r.label, GRAY)}")
    drsi = f"{r.daily_rsi:.0f}" if r.daily_rsi is not None else "---"
    mrsi = f"{r.monthly_rsi:.0f}" if r.monthly_rsi is not None else "---"
    t.append(f"D-RSI {drsi}  M-RSI {mrsi}", style=SILVER)
    if r.danger:
        t.append(f"   ⚠ D-RSI < {config.DANGER_DRSI} — danger zone", style=ORANGE)
    return t


# ── region 5: main table ─────────────────────────────────────────────────────

def render_main_table(appstate):
    table = Table(box=None, expand=False, pad_edge=False, show_edge=False)
    for col, justify in [("SYM", "left"), ("SCR", "right"), ("PROV", "right"), ("WRSI", "right"),
                         ("DRSI", "right"), ("PRICE", "right"), ("24hΔ", "right"),
                         ("AGE", "right"), ("ST", "center")]:
        table.add_column(col, justify=justify)

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

        scr_s = f"{card.score}/{card.denom}" if card else "---"
        prov_s = f"±{prov.score}/{prov.denom}" if prov else "···"
        wrsi_s = f"{card.weekly_rsi:.0f}" if card and card.weekly_rsi is not None else "---"
        drsi_s = f"{card.daily_rsi:.0f}" if card and card.daily_rsi is not None else "---"
        price_s = _fmt_price(tick.last if tick else (card.price if card else None))
        chg_s = f"{tick.change_pct:+.1f}%" if tick else "---"
        age_s = _fmt_age(age)

        status = "STALE" if stale else (card.status if card else "---")
        price_style = WHITE
        if ps and time.monotonic() < ps.flash_until:
            price_style = GREEN if ps.flash_color == "green" else RED

        table.add_row(
            DISPLAY.get(sym, sym), scr_s, Text(prov_s, style="dim"), wrsi_s, drsi_s,
            Text(price_s, style=price_style), chg_s, age_s,
            Text(STATUS_SHORT.get(status, status), style=STATUS_STYLE.get(status, GRAY)),
        )
    return table


# ── champion selection (RULINGS Q1) ─────────────────────────────────────────

def _pick_champion(appstate):
    candidates = []
    for sym in PAIR_LIST:
        ps = appstate.pairs.get(sym)
        card = ps.confirmed if ps else None
        if card and card.status == "BUY":
            candidates.append((sym, ps, card))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (
        -item[2].score,
        item[2].pct_above_low if item[2].pct_above_low is not None else float("inf"),
        item[0],
    ))
    return candidates[0]


# ── region 6: champion card ──────────────────────────────────────────────────

def render_champion(appstate):
    picked = _pick_champion(appstate)
    if picked is None:
        return Text("No active BUY candidates this scan.", style=GRAY)
    sym, ps, card = picked
    disp = DISPLAY.get(sym, sym)
    lines = [Text(f"★ CHAMPION: {disp}", style=f"bold {GOLD}")]

    live_price = ps.last_tick.last if ps.last_tick else None
    live_s = _fmt_price(live_price) if live_price else "--- (no tick yet)"
    lines.append(Text(f"  Live entry: {live_s}    Last confirmed close: {_fmt_price(card.price)}", style=WHITE))

    if ps.tranche:
        tr = ps.tranche
        usd = tr.qty * tr.price
        src = "live" if tr.price_is_live else "close"
        lines.append(Text(f"  Tranche: {tr.qty:g} {disp}  (~${usd:,.2f}, {tr.mult:g}x min, price:{src})", style=CYAN))

    lines.append(Text(f"  W-Support: {_fmt_price(card.low_52w)}", style=SILVER))
    if card.low_52w and card.high_52w:
        pct = f" (+{card.pct_above_low:.0f}% above low)" if card.pct_above_low is not None else ""
        lines.append(Text(f"  52w range: {_fmt_price(card.low_52w)} — {_fmt_price(card.high_52w)}{pct}", style=SILVER))

    lines.append(Text(f"  Score: {card.score}/{card.denom}  (required {card.required})", style=GOLD))
    lines.append(Text("  Signals:", style=SILVER))
    for r in card.results:
        if r.state == FIRED:
            lines.append(Text(f"    ✓ {r.name}", style=LIME))
        elif r.state == NA:
            lines.append(Text(f"    · {r.name}  (N/A: {r.reason})", style=GRAY))
        else:
            lines.append(Text(f"    □ {r.name}", style=GRAY))
    return Group(*lines)


# ── region 7: closest-not-yet ────────────────────────────────────────────────

def render_closest(appstate):
    items = []
    for sym in PAIR_LIST:
        ps = appstate.pairs.get(sym)
        card = ps.confirmed if ps else None
        if card and card.status != "BUY" and card.score >= 2:
            items.append((sym, card))
    items.sort(key=lambda x: (-x[1].score, x[0]))
    top3 = items[:3]
    if not top3:
        return Text("CLOSEST NOT YET: (none within range)", style=GRAY)
    lines = [Text("CLOSEST NOT YET", style=f"bold {PURPLE}")]
    for sym, card in top3:
        lines.append(Text(f"  {DISPLAY.get(sym, sym)}  {card.score}/{card.denom}", style=SILVER))
        shown = 0
        for r in card.results:
            if shown >= 2:
                break
            if r.state == NOT and r.slot in card.gap:
                lines.append(Text(f"    □ {r.name} → {card.gap[r.slot]}", style=GRAY))
                shown += 1
    return Group(*lines)


# ── region 8: alert tail ─────────────────────────────────────────────────────

def render_alert_tail(conn):
    rows = store.recent_alerts(conn, 5)
    lines = [Text("ALERT TAIL", style=f"bold {PURPLE}")]
    if not rows:
        lines.append(Text("  (none yet)", style=GRAY))
    for ts, symbol, price, score, denom, signals, kind in rows:
        lines.append(Text(f"  {ts[:16]}  {symbol:<8}  {_fmt_price(price)}  {score}/{denom}  [{kind}]", style=SILVER))
    return Group(*lines)


# ── frame assembly + Live loop ───────────────────────────────────────────────

def render_frame(appstate, conn):
    blank = Text("")
    return Group(
        render_header(appstate), render_countdowns(appstate), blank,
        render_btc_pulse(appstate), blank,
        render_regime(appstate), blank,
        render_main_table(appstate), blank,
        render_champion(appstate), blank,
        render_closest(appstate), blank,
        render_alert_tail(conn),
    )


def export_frame_text(appstate, conn, width=80):
    """Proof helper (M6): a static export of one live frame."""
    console = Console(width=width, record=True, color_system="256")
    console.print(render_frame(appstate, conn))
    return console.export_text()


async def run_ui(appstate, conn, hz=None):
    hz = hz or config.RENDER_HZ
    console = Console()
    with Live(render_frame(appstate, conn), console=console, screen=True, auto_refresh=False) as live:
        while True:
            live.update(render_frame(appstate, conn), refresh=True)
            await asyncio.sleep(1.0 / hz)
