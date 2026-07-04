"""rich Live TUI — AMOLED flight-telemetry. SPEC §8, invariant 5.

Reads engine-published state ONLY; never re-implements a formula.

Auto-adjusting + auto-centering: the frame re-measures the terminal every
render (tmux/window resizes picked up live), caps the content column at
CONTENT_MAX, centers it in wider terminals, and sheds table columns gracefully
at narrow widths (AGE -> 24h -> DRSI -> WRSI) instead of degrading into rich's
lossy `$44…` ellipsis truncation. Below ~50 cols, `--simple` is the tool.

Color is information, not decoration: status colors the symbol and status
cells; stale rows dim wholesale; the provisional cell turns amber only when a
provisional BUY is forming that confirmed hasn't ratified yet; D-RSI tiers
align to DANGER_DRSI per §8.
"""
import time
import asyncio
import datetime
from zoneinfo import ZoneInfo

from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.panel import Panel
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
DIM_LINE = "color(236)"
DARK_GOLD = "color(94)"
SILVER = "grey70"
WHITE = "white"

CONTENT_MAX = 96      # cap the dashboard column; center it in wider terminals
CONTENT_MIN = 42

DISPLAY = {p["ws"]: p["display"] for p in config.PAIRS}
PAIR_LIST = [p["ws"] for p in config.PAIRS]

STATUS_SHORT = {"BUY": "BUY", "WATCH": "WCH", "---": "---", "STALE": "STALE"}
STATUS_STYLE = {"BUY": f"bold {GREEN}", "WATCH": GOLD, "STALE": RED, "---": GRAY}
REGIME_STYLE = {"BULL": GREEN, "BEAR": RED, "RECOVERY": AMBER, "UNKNOWN": GRAY}
REGIME_NOTE = {"BULL": "above & rising W-EMA200", "BEAR": "below W-EMA200",
               "RECOVERY": "above, EMA flattening", "UNKNOWN": "insufficient data"}
CONVICTION_LABEL = {1.0: "STARTER 1x", 1.5: "SCALE 1.5x", 2.0: "FULL 2x"}
LINK_STYLE = {"UP": GREEN, "PARTIAL": AMBER, "DOWN": RED}


# ── formatting helpers ───────────────────────────────────────────────────────

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


def _local_short(ts_iso):
    """UTC ISO ledger timestamp -> operator-local short form."""
    try:
        dt = datetime.datetime.fromisoformat(ts_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(DENVER).strftime("%m-%d %H:%M")
    except Exception:
        return ts_iso[:16]


def _sep(w):
    return Text("─" * w, style=DIM_LINE)


def _mini_bar(fraction, cells=10):
    fraction = max(0.0, min(1.0, fraction))
    filled = round(fraction * cells)
    return "▰" * filled + "▱" * (cells - filled)


def _content_width(total):
    return max(CONTENT_MIN, min(total - 2, CONTENT_MAX))


# ── region 1: header ─────────────────────────────────────────────────────────

def render_header(appstate, w):
    now_local = datetime.datetime.now(DENVER)
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    uptime = time.time() - appstate.started_ts

    g = Table.grid(expand=True)
    g.add_column(justify="left")
    g.add_column(justify="center")
    g.add_column(justify="right")

    left = Text()
    left.append("◈ DEEPFIELD ", style=f"bold {GOLD}")
    left.append(f"v{VERSION}", style=DARK_GOLD)

    mid = Text(f"{now_local.strftime('%H:%M:%S')} MT · {now_utc.strftime('%H:%M:%S')} UTC", style=SILVER)
    right = Text(f"up {_fmt_hms(uptime)}", style=GRAY)
    g.add_row(left, mid, right)

    status = appstate.link_status()
    line2 = Text()
    line2.append(f"LINK {status}", style=f"bold {LINK_STYLE[status]}")
    if appstate.links:
        line2.append("  ", style=GRAY)
        for name in sorted(appstate.links):
            up = appstate.links[name].get("up", False)
            line2.append("●", style=GREEN if up else RED)
            line2.append(f"{name[:1]} ", style=GRAY)
    rc = appstate.total_reconnects() if appstate.links else appstate.reconnect_count
    line2.append(f"· reconnects {rc} ", style=AMBER if rc else GRAY)
    line2.append(f"· RECON {appstate.recon_repairs}", style=AMBER if appstate.recon_repairs else GRAY)
    if appstate.paused:
        line2.append("   ⏸ PAUSED", style=f"bold {AMBER}")
    return Group(g, line2)


# ── region 2: countdowns ─────────────────────────────────────────────────────

def _countdown_cell(label, begin, interval_secs, fmt, now):
    t = Text()
    t.append(f"{label} ", style=f"bold {SILVER}")
    if begin is None:
        t.append("closes ?", style=GRAY)
        return t
    left = begin + interval_secs - now
    frac = (now - begin) / interval_secs
    if left <= 0:
        t.append("closing… ", style=f"bold {AMBER}")
        t.append("(confirming)", style=GRAY)
        return t
    t.append(_mini_bar(frac) + " ", style=CYAN)
    t.append(f"{frac * 100:3.0f}%  ", style=GRAY)
    t.append("closes ", style=GRAY)
    t.append(fmt(left), style=f"bold {CYAN}")
    return t


def render_countdowns(appstate, w):
    now = time.time()
    g = Table.grid(expand=True)
    g.add_column(justify="left")
    g.add_column(justify="right")
    g.add_row(
        _countdown_cell("DAILY", appstate.daily_interval_begin, 86400, _fmt_hms, now),
        _countdown_cell("WEEKLY", appstate.weekly_interval_begin, 604800, _fmt_days_hm, now),
    )
    return g


# ── region 3: BTC pulse ──────────────────────────────────────────────────────

def render_btc_pulse(appstate, w):
    ps = appstate.pairs.get("BTC/USD")
    t = Text()
    if ps is None or ps.last_tick is None:
        t.append("BTC LIVE  unavailable", style=GRAY)
        return t
    tick = ps.last_tick
    chg_style = GREEN if tick.change_pct >= 0 else RED
    t.append("BTC ", style=f"bold {GOLD}")
    t.append(f"{_fmt_price(tick.last)}  ", style=f"bold {WHITE}")
    t.append(f"{tick.change_pct:+.1f}%", style=chg_style)
    t.append(f"   24h H {_fmt_price(tick.high24)} · L {_fmt_price(tick.low24)}", style=SILVER)
    levels = config.LEVELS.get("BTC/USD", [])
    if levels and w >= 64:
        parts = [f"{label} {(tick.last - price) / price * 100:+.1f}%" for label, price in levels]
        t.append("   " + " · ".join(parts), style=GRAY)
    return t


# ── region 4: regime ─────────────────────────────────────────────────────────

def _drsi_tier(drsi):
    """Tier boundaries aligned to DANGER_DRSI per §8 (30 / 45)."""
    if drsi is None:
        return SILVER, ""
    if drsi < config.DANGER_DRSI:
        return RED, " ⚠ knife-catch zone"
    if drsi < config.DANGER_DRSI + 15:
        return GOLD, " recovering"
    return SILVER, ""


def render_regime(appstate, w):
    r = appstate.regime
    t = Text()
    if r is None:
        t.append("BTC regime: unknown (awaiting startup sweep / first close)", style=GRAY)
        return t
    t.append("BTC ", style=SILVER)
    t.append(f"{r.label} ", style=f"bold {REGIME_STYLE.get(r.label, GRAY)}")
    if w >= 62:
        t.append(f"({REGIME_NOTE.get(r.label, '')})  ", style=GRAY)
    drsi_style, drsi_tag = _drsi_tier(r.daily_rsi)
    drsi = f"{r.daily_rsi:.0f}" if r.daily_rsi is not None else "---"
    mrsi = f"{r.monthly_rsi:.0f}" if r.monthly_rsi is not None else "---"
    t.append("D-RSI ", style=GRAY)
    t.append(drsi, style=f"bold {drsi_style}")
    if drsi_tag:
        t.append(drsi_tag, style=drsi_style)
    t.append("  M-RSI ", style=GRAY)
    t.append(mrsi, style=SILVER)
    return t


# ── region 5: main table (adaptive columns) ──────────────────────────────────

def _columns_for(w):
    cols = ["SYM", "SCR", "PROV", "WRSI", "DRSI", "PRICE", "24hΔ", "AGE", "ST"]
    if w < 76:
        cols.remove("AGE")
    if w < 68:
        cols.remove("24hΔ")
    if w < 60:
        cols.remove("DRSI")
    if w < 54:
        cols.remove("WRSI")
    return cols


def _wrsi_cell(card):
    if not card or card.weekly_rsi is None:
        return Text("---", style=GRAY)
    t = Text(f"{card.weekly_rsi:.0f}", style=SILVER)
    if card.wrsi_ref is not None:
        up = card.weekly_rsi > card.wrsi_ref
        t.append("↑" if up else "↓", style=GREEN if up else RED)
    return t


def render_main_table(appstate, w):
    cols = _columns_for(w)
    table = Table(box=None, expand=True, width=w, pad_edge=False,
                  show_edge=False, header_style=f"bold {SILVER}")
    justify = {"SYM": "left", "ST": "center"}
    for col in cols:
        table.add_column(col, justify=justify.get(col, "right"), no_wrap=True)

    def sort_key(sym):
        ps = appstate.pairs.get(sym)
        card = ps.confirmed if ps else None
        return (-(card.score if card else -1), sym)

    now_mono = time.monotonic()
    for sym in sorted(PAIR_LIST, key=sort_key):
        ps = appstate.pairs.get(sym)
        card = ps.confirmed if ps else None
        prov = ps.provisional if ps else None
        tick = ps.last_tick if ps else None
        age = ps.tick_age() if ps else float("inf")
        stale = engine.is_stale(age, config.STALE_SECS)
        status = "STALE" if stale else (card.status if card else "---")
        st_style = STATUS_STYLE.get(status, GRAY)

        # PROV cell: `~` marks the provisional/pace-adjusted evaluation (RULINGS
        # Q4 — sig6 is pace-adjusted or floored "either way"). Amber when a
        # provisional BUY is forming that confirmed hasn't ratified.
        if prov:
            prov_style = "dim"
            if prov.status == "BUY" and (not card or card.status != "BUY"):
                prov_style = f"bold {AMBER}"
            prov_cell = Text(f"~{prov.score}/{prov.denom}", style=prov_style)
        else:
            prov_cell = Text("···", style="dim")

        price_style = WHITE
        if ps and now_mono < ps.flash_until:
            price_style = f"bold {GREEN}" if ps.flash_color == "green" else f"bold {RED}"

        cells = {
            "SYM": Text(DISPLAY.get(sym, sym), style=st_style),
            "SCR": Text(f"{card.score}/{card.denom}" if card else "---",
                        style=SILVER if card else GRAY),
            "PROV": prov_cell,
            "WRSI": _wrsi_cell(card),
            "DRSI": Text(f"{card.daily_rsi:.0f}" if card and card.daily_rsi is not None else "---",
                         style=SILVER),
            "PRICE": Text(_fmt_price(tick.last if tick else (card.price if card else None)),
                          style=price_style),
            "24hΔ": Text(f"{tick.change_pct:+.1f}%" if tick else "---",
                         style=(GREEN if tick and tick.change_pct >= 0 else RED) if tick else GRAY),
            "AGE": Text(_fmt_age(age), style=GRAY),
            "ST": Text(STATUS_SHORT.get(status, status), style=st_style),
        }
        table.add_row(*[cells[c] for c in cols], style="dim" if stale else None)
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

def _score_bar(score, denom, required):
    style = GREEN if score > required else GOLD
    return Text("█" * (score * 2) + "░" * ((denom - score) * 2), style=style)


def render_champion(appstate, w):
    picked = _pick_champion(appstate)
    if picked is None:
        return Text("No active BUY candidates this scan.", style=GRAY)
    sym, ps, card = picked
    disp = DISPLAY.get(sym, sym)
    lines = []

    live_price = ps.last_tick.last if ps.last_tick else None
    entry = Text("  Entry ", style=GRAY)
    entry.append(_fmt_price(live_price) if live_price else "--- (no tick)", style=f"bold {WHITE}")
    entry.append(" live" if live_price else "", style=GREEN)
    entry.append("   ·   last close ", style=GRAY)
    entry.append(_fmt_price(card.price), style=SILVER)
    lines.append(entry)

    if ps.tranche:
        tr = ps.tranche
        t = Text("  Tranche ", style=GRAY)
        t.append(f"{tr.qty:g} {disp}", style=f"bold {CYAN}")
        t.append(f"  ≈ ${tr.qty * tr.price:,.2f}", style=CYAN)
        t.append(f" · {CONVICTION_LABEL.get(tr.mult, f'{tr.mult:g}x')}", style=GOLD)
        t.append(f" · {'live' if tr.price_is_live else 'close'} px", style=GRAY)
        lines.append(t)

    rng = Text("  W-Support ", style=GRAY)
    rng.append(_fmt_price(card.low_52w), style=SILVER)
    if card.low_52w and card.high_52w:
        rng.append("   ·   52w ", style=GRAY)
        rng.append(_fmt_price(card.low_52w), style=RED)
        rng.append(" — ", style=GRAY)
        rng.append(_fmt_price(card.high_52w), style=GREEN)
        if card.pct_above_low is not None:
            rng.append(f"  (+{card.pct_above_low:.0f}% above low)", style=GRAY)
    lines.append(rng)

    sc = Text("  Score ", style=GRAY)
    sc.append_text(_score_bar(card.score, card.denom, card.required))
    sc.append(f"  {card.score}/{card.denom}", style=f"bold {GOLD}")
    sc.append(f" · required {card.required}", style=GRAY)
    lines.append(sc)

    for r in card.results:
        row = Text("   ")
        if r.state == FIRED:
            row.append("✓ ", style=LIME)
            row.append(r.name, style=LIME)
        elif r.state == NA:
            row.append("· ", style=GRAY)
            row.append(f"{r.name}  N/A — {r.reason}", style=GRAY)
        else:
            row.append("□ ", style=GRAY)
            row.append(r.name, style=SILVER)
            gap = card.gap.get(r.slot)
            if gap:
                row.append(f"  ·  {gap}", style="dim")
        lines.append(row)

    return Panel(Group(*lines), width=w, box=box.SQUARE, border_style=DARK_GOLD,
                 title=Text(f"★ CHAMPION · {disp}", style=f"bold {GOLD}"),
                 title_align="left", padding=(0, 1))


# ── region 7: closest-not-yet ────────────────────────────────────────────────

def render_closest(appstate, w):
    items = []
    for sym in PAIR_LIST:
        ps = appstate.pairs.get(sym)
        card = ps.confirmed if ps else None
        if card and card.status != "BUY" and card.score >= 2:
            items.append((sym, card))
    items.sort(key=lambda x: (-x[1].score, x[0]))
    top3 = items[:3]
    if not top3:
        return Text("CLOSEST NOT YET · (none within range)", style=GRAY)
    lines = [Text("▸ CLOSEST NOT YET", style=f"bold {PURPLE}")]
    for sym, card in top3:
        head = Text(f"  {DISPLAY.get(sym, sym):<5}", style=f"bold {SILVER}")
        head.append(f"{card.score}/{card.denom}", style=SILVER)
        head.append(f" · needs {max(0, card.required - card.score)} more", style=GRAY)
        lines.append(head)
        shown = 0
        for r in card.results:
            if shown >= 2:
                break
            if r.state == NOT and r.slot in card.gap:
                lines.append(Text(f"     □ {r.name} → {card.gap[r.slot]}", style="dim"))
                shown += 1
    return Group(*lines)


# ── region 8: alert tail ─────────────────────────────────────────────────────

KIND_STYLE = {"confirmed": GREEN, "provisional": AMBER, "test": GRAY}


def render_alert_tail(conn, w):
    rows = store.recent_alerts(conn, 5)
    lines = [Text("▸ ALERTS", style=f"bold {PURPLE}")]
    if not rows:
        lines.append(Text("  (none yet)", style=GRAY))
    for ts, symbol, price, score, denom, signals, kind in rows:
        t = Text(f"  {_local_short(ts)} MT  ", style=GRAY)
        t.append(f"{symbol:<9}", style=SILVER)
        t.append(f"{_fmt_price(price):>10}  ", style=WHITE)
        t.append(f"{score}/{denom}  ", style=GOLD)
        t.append(kind, style=KIND_STYLE.get(kind, GRAY))
        lines.append(t)
    return Group(*lines)


# ── frame assembly + Live loop ───────────────────────────────────────────────

def render_frame(appstate, conn, total_width=100, show_keys=False):
    w = _content_width(total_width)
    parts = [
        render_header(appstate, w), _sep(w),
        render_countdowns(appstate, w), _sep(w),
        render_btc_pulse(appstate, w),
        render_regime(appstate, w), _sep(w),
        render_main_table(appstate, w), _sep(w),
        render_champion(appstate, w),
        render_closest(appstate, w), _sep(w),
        render_alert_tail(conn, w),
    ]
    if show_keys:
        hint = Table.grid(expand=True)
        hint.add_column(justify="center")
        hint.add_row(Text("q quit · p pause · f reconcile · a test-alert", style=DIM_LINE))
        parts += [_sep(w), hint]
    group = Group(*parts)
    # Auto-centering: the content column floats centered in wider terminals.
    return Align.center(group, width=w) if total_width > w else group


def export_frame_text(appstate, conn, width=80):
    """Proof helper: a static export of one live frame."""
    console = Console(width=width, record=True, color_system="256")
    console.print(render_frame(appstate, conn, total_width=width))
    return console.export_text()


async def run_ui(appstate, conn, hz=None, show_keys=False):
    hz = hz or config.RENDER_HZ
    console = Console()
    with Live(console=console, screen=True, auto_refresh=False) as live:
        while True:
            if appstate.paused:
                if appstate.pause_dirty:
                    live.update(render_frame(appstate, conn, console.width, show_keys), refresh=True)
                    appstate.pause_dirty = False
                await asyncio.sleep(0.1)
                continue
            # console.width re-measured every frame -> auto-adjusts to resizes.
            live.update(render_frame(appstate, conn, console.width, show_keys), refresh=True)
            await asyncio.sleep(1.0 / hz)
