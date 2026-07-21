"""Live app wiring: warm backfill -> DB -> Ingest (startup sweep) -> dual WS ->
writer -> clock-close watchdog -> hourly reconciler -> UI (rich or --simple)
-> keys (q/p/f/a). SPEC §5/§8/§12.
"""
import asyncio
import json
import logging
import os
import datetime
import math
import statistics
import time

from . import VERSION
from . import config
from . import store
from . import backfill
from . import reconciler
from . import rest_client
from . import ingest as ingest_mod
from . import ui
from . import simple_ui
from . import alerter
from . import defense
from .ws_client import WSClient
from .state import AppState
from .keys import KeyController
from .logsetup import setup_logging

log = logging.getLogger("deepfield.app")


def _make_gap_heal_cb(intervals):
    """Reconnect gap-heal scoped to the intervals that connection actually
    carries — conn A owns 1440, conn B owns 10080. Healing both from both
    doubled the REST load on every reconnect for no coverage gain."""
    def _heal():
        c = store.connect(config.DB_PATH)
        try:
            symbols = [p["ws"] for p in config.PAIRS]
            return reconciler.gap_heal(c, symbols, rest_client.fetch_ohlc, intervals=intervals)
        finally:
            c.close()

    async def gap_heal_cb(_syms):
        await asyncio.to_thread(_heal)
    return gap_heal_cb


_IV_LABEL = {15: "15m", 60: "1h", 240: "4h", 1440: "D", 10080: "W"}


def _make_ws_clients(symbols, queue):
    """One connection PER OHLC INTERVAL (§6 discrepancy, M4): Kraken v2 allows only
    one ohlc interval per symbol per connection — a second ohlc@interval subscribe
    on the same socket ACKs success:false for every symbol. Ticker rides along on
    the first connection (verified: ticker + one ohlc interval coexist fine).

    Generalized 2026-07-19 from the hardcoded A/B pair so config.INTERVALS drives
    the topology: N intervals -> N connections, each gap-healing only the interval
    it carries. Reconnect backoff stays far under Cloudflare's ~150 attempts/10min
    even at 4 sockets."""
    # Ticker rides the DAILY socket, not merely the first one. It is the live-price
    # feed the executor, stop math and P/L read, so it belongs on the quietest,
    # most stable connection — pinning it to a fast interval would couple execution
    # pricing to the noisiest, most reconnect-prone socket. 1440 carried it before
    # this generalization; keep it there.
    ticker_on = 1440 if 1440 in config.INTERVALS else config.INTERVALS[0]
    clients = []
    for i, interval in enumerate(config.INTERVALS):
        subs = [{"channel": "ohlc", "interval": interval}]
        label = _IV_LABEL.get(interval, str(interval))
        if interval == ticker_on:
            subs.insert(0, {"channel": "ticker"})
            label = f"ticker+{label}"
        clients.append(WSClient(
            symbols, queue, subs=subs,
            on_connect=_make_gap_heal_cb((interval,)),
            name=f"{chr(ord('A') + i)}({label})"))
    return clients


def _heal_all():
    """Full-scope heal (both intervals) — hourly pass and the 'f' key."""
    c = store.connect(config.DB_PATH)
    try:
        symbols = [p["ws"] for p in config.PAIRS]
        return reconciler.gap_heal(c, symbols, rest_client.fetch_ohlc)
    finally:
        c.close()


def _poll_fills_threaded():
    """Off-loop (own conn): promote filled entry limits to positions and rest
    their stops. Blocking Kraken I/O, so never on the event loop."""
    from . import executor as executor_mod
    c = store.connect(config.DB_PATH)
    try:
        e = executor_mod.Executor(c)
        e.mode = "live"
        e.poll_fills()
    except Exception:
        log.exception("poll_fills failed")
    finally:
        c.close()


def _poll_rollover_fees_threaded():
    """Off-loop (own conn): accumulate margin rollover fees into meta for display
    (audit 2026-07-13 #2 'rollover-fee accounting' — the config knob + broker reader
    existed but were never wired to a caller, so the drag was invisible; wired
    2026-07-15). Two bounded Ledgers reads:
      - fees_day: exact re-sum since UTC midnight (a day's rollovers stay well under
        the broker's 1000-entry page cap), so it resets naturally each UTC day.
      - fees_total: running sum SINCE ACCOUNTING BEGAN (fees_epoch), not all-time.
        Each poll sums only the window [fees_cursor, now] and banks it:
        fees_banked += window, cursor -> the poll instant. So every steady-state
        read is just the last hour (~a handful of entries, one page) — never a
        re-walk of history — and because the cursor lands on an arbitrary poll
        second while rollovers post only on 4h UTC marks, the bank neither drops
        nor double-counts a boundary entry.

    The first poll ANCHORS the cursor at now instead of seeding from history
    (fix 2026-07-19). It used to walk back to 0, which was never survivable: Kraken
    held 6011 rollover entries (~121 pages) against a rate limit shared with the
    bot's own private traffic, so the seed failed every hour, the cursor was never
    banked, and the next poll re-walked from 0 — fees_total read None from the day
    this was wired until the fix. The cost is that rollover paid before the epoch is
    not counted; fees_day remains exact for the current UTC day either way.

    Blocking Kraken I/O + display-only writes; never gates an order, never raises
    into the loop. Also hosts the external-flow poll (same thread, same cadence,
    same Ledgers pacing budget) — see _poll_external_flows."""
    from . import broker
    import datetime as _dt
    c = store.connect(config.DB_PATH)
    try:
        now = _dt.datetime.now(_dt.timezone.utc)
        try:
            _poll_fees(c, broker, now)
        except Exception:
            log.exception("rollover fee poll failed (display only)")
        try:
            _poll_external_flows(c, broker, now)
        except Exception:
            log.exception("external-flow poll failed (baseline shift skipped)")
    finally:
        c.close()


def _poll_fees(c, broker, now):
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    day = broker.rollover_fees_since(midnight)
    if day is not None:                          # None == API failure: keep last value
        store.meta_set(c, "fees_day", round(day[0], 4))

    def _mf(key):
        try:
            return float(store.meta_get(c, key, 0) or 0)
        except (TypeError, ValueError):
            return 0.0
    banked, cursor = _mf("fees_banked"), _mf("fees_cursor")
    if not cursor:
        # First poll ever: ANCHOR here rather than walking history back to 0.
        # That walk was the bug (fix 2026-07-19) — Kraken holds 6011 rollover
        # entries, ~121 pages, against a rate limit shared with the bot's own
        # private traffic. It failed every hour, never banked a cursor, and so
        # re-walked from 0 the next hour forever; fees_total was None the whole
        # time. Anchoring makes the total mean "rollover paid since accounting
        # began" (fees_epoch records when) and costs exactly one page from here
        # on. fees_day is unaffected and still exact for the current UTC day.
        store.meta_set(c, "fees_total", 0.0)
        store.meta_set(c, "fees_banked", 0.0)
        store.meta_set(c, "fees_cursor", now.timestamp())
        store.meta_set(c, "fees_epoch", now.timestamp())
        log.info("rollover accounting anchored at %s — fees_total accrues from now "
                 "(prior history not summed: %d pages exceeds the shared rate limit)",
                 now.isoformat(timespec="seconds"), 121)
        return
    win = broker.rollover_fees_since(cursor)
    if win is not None:                          # None == API failure: bank nothing, retry
        new_total = round(banked + win[0], 4)
        store.meta_set(c, "fees_total", new_total)
        store.meta_set(c, "fees_banked", new_total)
        store.meta_set(c, "fees_cursor", now.timestamp())
        if not win[3]:
            # Truncated: we summed a floor, not the true window. Advance anyway —
            # holding the cursor back is exactly the deadlock this fix removes.
            log.warning("rollover window truncated at the page ceiling — fees_total "
                        "under-counts this window; cursor advanced to stay converged")


def _poll_external_flows(c, broker, now):
    """Deposit/withdrawal awareness for the equity T/P (operator 2026-07-21).

    The +TP_PCT trigger measures equity against meta['tp_baseline'], but equity
    also moves when money walks in or out of the account: the 2026-07-19 $20
    deposit shrank the effective profit target by $20 and the next flatten fired
    at ~+9% real trading gain instead of +20%. Poll Ledgers for deposit/withdrawal
    entries past a cursor and SHIFT the baseline by the net USD flow, so the
    target always measures TRADING profit only. The same net is accumulated into
    meta['tp_cycle_flows'] so the completed cycle's ledger row can show how much
    external money moved during the cycle.

    Cursor discipline is rollover's (fix 2026-07-19): the first poll ANCHORS at
    now — correct here beyond rate safety, because the current baseline was armed
    against equity that already contained every historical flow. The baseline
    write is a guarded compare-and-swap on the exact string we read: if the
    executor completed a T/P cycle in between (its new baseline = post-flatten
    equity, which already CONTAINS the deposit), applying our shift on top would
    double-count it — CAS failure skips the shift, correctly. A withdrawal that
    would push the baseline to zero or below clears it to 0 instead; the executor
    re-arms at its next live equity read (its baseline<=0 path). Never gates an
    order, never raises into the loop."""
    def _mf(key):
        try:
            return float(store.meta_get(c, key, 0) or 0)
        except (TypeError, ValueError):
            return 0.0
    cursor = _mf("flows_cursor")
    if not cursor:
        store.meta_set(c, "flows_cursor", now.timestamp())
        log.info("external-flow accounting anchored at %s — deposits/withdrawals "
                 "shift the T/P baseline from here on",
                 now.isoformat(timespec="seconds"))
        return
    r = broker.external_flows_since(cursor)
    if r is None:                        # API failure: keep the cursor, retry next poll
        return
    net, count, complete = r
    store.meta_set(c, "flows_cursor", now.timestamp())
    if not complete:
        log.warning("external-flow window truncated at the page ceiling — net flow is "
                    "a partial sum; the missed remainder shifts nothing (baseline may "
                    "lag until the operator reconciles)")
    if count == 0 or abs(net) < 0.01:
        return
    raw_baseline = store.meta_get(c, "tp_baseline", None)
    baseline = 0.0
    try:
        baseline = float(raw_baseline or 0)
    except (TypeError, ValueError):
        pass
    if baseline <= 0:
        # Not armed — the executor will arm against post-flow equity anyway.
        log.info("external flow $%+.2f seen with no armed T/P baseline — nothing to shift", net)
        return
    new = baseline + net
    if new <= 0:
        target_note = "cleared (executor re-arms at live equity)"
        cur = c.execute("UPDATE meta SET value=? WHERE key='tp_baseline' AND value=?",
                        ("0.0", raw_baseline))
    else:
        target_note = f"${new * (1 + config.TP_PCT):.2f}"
        cur = c.execute("UPDATE meta SET value=? WHERE key='tp_baseline' AND value=?",
                        (str(round(new, 4)), raw_baseline))
    if cur.rowcount == 0:
        c.commit()
        log.warning("external flow $%+.2f NOT applied — tp_baseline changed underneath "
                    "(T/P cycle completed concurrently; its post-flatten baseline already "
                    "contains the flow)", net)
        return
    c.commit()
    store.meta_set(c, "tp_cycle_flows", round(_mf("tp_cycle_flows") + net, 4))
    log.warning("T/P BASELINE SHIFTED by external flow $%+.2f (%d ledger entr%s): "
                "$%.2f -> $%.2f, target %s — trading-profit-only trigger",
                net, count, "y" if count == 1 else "ies", baseline, max(new, 0.0),
                target_note)
    try:
        store.journal(c, "tp", "*",
                      f"external flow ${net:+.2f}: baseline ${baseline:.2f} -> "
                      f"${max(new, 0.0):.2f}, target {target_note}")
    except Exception:
        log.exception("journal write failed (baseline shift already applied)")


def _stress_weights(conn):
    """[(symbol, notional)] for the OPEN book, notional at last known entry price.

    Only the PROPORTIONS matter (basket_index normalises), so entry-priced notional
    is sufficient and avoids needing a live tick for every symbol. Deliberately
    approximate: DB volume can lag Kraken's by vol_closed, so a symbol's share may
    be slightly off. That biases the basket weighting, never the absolute buffer —
    e/m/v for the actual buffer numbers come from live TradeBalance, not from here."""
    rows = conn.execute(
        "SELECT symbol, SUM(volume * entry) FROM orders WHERE status='open' "
        "AND volume > 0 AND entry > 0 GROUP BY symbol").fetchall()
    return [(r[0], float(r[1])) for r in rows if r[1] and r[1] > 0]


def _aligned_series(conn, symbols, interval, limit, field="c"):
    """[(symbol, [prices oldest->newest])] truncated to a COMMON bar count.

    Alignment matters: basket math over unaligned series invents moves that never
    happened. Bars are taken newest-first then reversed, and every series is cut to
    the shortest so all of them cover the same span."""
    out = []
    for s in symbols:
        rows = conn.execute(
            f"SELECT {field} FROM candles WHERE pair=? AND interval=? AND closed=1 "
            "ORDER BY ts DESC LIMIT ?", (s, interval, limit)).fetchall()
        p = [r[0] for r in reversed(rows) if r[0] and r[0] > 0]
        if len(p) > 1:
            out.append((s, p))
    if not out:
        return []
    n = min(len(p) for _, p in out)
    return [(s, p[-n:]) for s, p in out]


def _poll_stress_threaded():
    """Off-loop (own conn): intraday liq-buffer stress telemetry.

    buffer_liq_pct is denominated in ADVERSE BASKET MOVE, so this measures the moves
    that actually happen and reports what they would do to the CURRENT book:

      - intraday_dd: worst basket drawdown over the last 24h of 15m LOWS. Daily bars
        structurally cannot show this — a -3% daily close may have wicked -9% and
        recovered, and the point-in-time buffer poll never saw it. This is the
        near-miss the operator otherwise learns about from the exchange app.
      - ref_move_1d/5d: worst 1- and 5-day basket moves over ~2y of daily bars, the
        historical reference the ceiling is judged against.
      - lev_ceiling: eff-leverage ceiling that survives ref_move_1d, from live m/v.

    TELEMETRY ONLY: writes meta and journals/alerts on a breach. It never places,
    cancels or sizes an order — the reverse gear stays the only actuator. Blocking
    DB reads; never raises into the loop."""
    from . import broker
    c = store.connect(config.DB_PATH)
    try:
        weights = _stress_weights(c)
        if not weights:
            store.meta_set(c, "stress_state", json.dumps({"flat": True}))
            return
        syms = [s for s, _ in weights]
        wmap = dict(weights)

        intr = _aligned_series(c, syms, 15, int(config.STRESS_INTRADAY_LOOKBACK_BARS), field="l")
        intraday_dd = defense.basket_drawdown([(wmap[s], p) for s, p in intr]) if intr else 0.0

        daily = _aligned_series(c, syms, 1440, int(config.STRESS_REFERENCE_DAYS), field="c")
        idx = defense.basket_index([(wmap[s], p) for s, p in daily]) if daily else []
        refs = {h: defense.worst_move(idx, h) for h in config.STRESS_REFERENCE_HORIZONS}

        bal = broker.trade_balance_full()
        if not bal:
            return                                   # no live e/m/v: stay silent
        e = broker.equity(bal)
        m, v = float(bal.get("m") or 0), float(bal.get("v") or 0)
        ref1 = refs.get(1) or 0.0
        out = {
            "intraday_dd_pct": round(intraday_dd * 100, 3),
            "intraday_bars": len(intr[0][1]) if intr else 0,
            "ref_move_pct": {str(h): round(r * 100, 3) for h, r in refs.items()},
            "buffer_now_pct": None, "buffer_under_intraday_pct": None,
            "lev_now": None, "lev_ceiling": None,
        }
        cur = defense.compute(e, m, v)
        if cur:
            out["buffer_now_pct"] = _round_or_none(cur.get("buffer_liq_pct"))
            out["lev_now"] = _round_or_none(cur.get("eff_leverage"), 3)
        out["buffer_under_intraday_pct"] = _round_or_none(
            defense.stress_buffer(e, m, v, intraday_dd))
        ceiling = defense.max_survivable_leverage(m, v, ref1)
        out["lev_ceiling"] = _round_or_none(ceiling, 3)
        store.meta_set(c, "stress_state", json.dumps(out))

        lev, ratio = out["lev_now"], float(config.STRESS_LEVERAGE_ALERT_RATIO or 1.0)
        if (lev is not None and ceiling is not None and ref1 > 0
                and not math.isinf(ceiling) and lev > ceiling * ratio):
            text = (f"STRESS leverage {lev:.2f}x over the {ceiling:.2f}x ceiling that "
                    f"survives the worst 1-day basket move on record ({ref1*100:.1f}%) "
                    f"— buffer {out['buffer_now_pct']}%")
            _journal_safe(c, "defense", "*", text)
            alerter.fire_safety("liq-risk", f"lev{lev:.1f}", text)
    except Exception:
        log.exception("stress telemetry poll failed (display only)")
    finally:
        c.close()


def _round_or_none(x, nd=3):
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(f):
        return None
    return None if math.isinf(f) else round(f, nd)


def _sys_journal(conn, text):
    """Isolated 'sys' journal emit for lifecycle events — never raises out."""
    try:
        store.journal(conn, "sys", "", text)
    except Exception:
        log.exception("sys journal emit failed — unaffected")


def _build_by_pair(conn, appstate):
    """v6 SURVEY: per-pair ledger snapshot for the FIELD bands + BOOK view.
    Pure DB read of the open/pending order rows, keyed by symbol. uP&L here is a
    snapshot convenience (last known tick); the FIELD LEDGER recomputes per-fill
    uP&L live at render from ps.last_tick (renderers own the live math)."""
    by_pair = {}
    for oid, sym, ts, vol, lev, entry, stop, stop_txid in conn.execute(
            "SELECT id, symbol, ts, volume, leverage, entry, stop, stop_txid FROM orders "
            "WHERE status='open' ORDER BY symbol, id"):
        d = by_pair.setdefault(sym, {"fills": [], "pendings": [], "vol_sum": 0.0,
                                     "avg_entry": None, "upnl": None, "stop": None})
        d["fills"].append({"id": oid, "ts": ts, "vol": vol, "lev": lev,
                           "entry": entry, "stop": stop, "stop_txid": stop_txid})
    for sym, price, vol, ts in conn.execute(
            "SELECT symbol, entry, volume, ts FROM orders "
            "WHERE status='pending' ORDER BY symbol, id"):
        d = by_pair.setdefault(sym, {"fills": [], "pendings": [], "vol_sum": 0.0,
                                     "avg_entry": None, "upnl": None, "stop": None})
        d["pendings"].append({"price": price, "vol": vol, "ts": ts})
    for sym, d in by_pair.items():
        fills = d["fills"]
        vsum = sum((f["vol"] or 0.0) for f in fills)
        d["vol_sum"] = vsum
        if vsum > 0:
            num = sum((f["vol"] or 0.0) * (f["entry"] or 0.0) for f in fills)
            d["avg_entry"] = num / vsum
        stops = [f["stop"] for f in fills if f["stop"] is not None]
        d["stop"] = max(stops) if stops else None   # tightest protective floor for the stack
        ps = appstate.pairs.get(sym)
        cur = ps.last_tick.last if (ps and ps.last_tick) else None
        if cur is not None and vsum > 0:
            d["upnl"] = sum((cur - (f["entry"] or 0.0)) * (f["vol"] or 0.0) for f in fills)
    return by_pair


def _snapshot_capacity(conn, appstate, free_margin):
    """Room to keep buying: free margin ÷ the typical fill's margin, in min-fills.
    Median margin of the last 10 LIVE fills; fallback to the mean of the current
    per-pair exec_plan margins. None when neither is available."""
    if not free_margin or free_margin <= 0:
        return None
    rows = conn.execute(
        "SELECT margin FROM orders WHERE mode='live' AND status IN('open','closed') "
        "AND margin IS NOT NULL ORDER BY id DESC LIMIT 10").fetchall()
    margins = [float(r[0]) for r in rows if r[0] is not None]
    if not margins:
        margins = [p.exec_plan["margin"] for p in appstate.pairs.values()
                   if p.exec_plan and p.exec_plan.get("margin")]
        if margins:
            margins = [sum(margins) / len(margins)]
    if not margins:
        return None
    typical = statistics.median(margins)
    return int(free_margin / typical) if typical > 0 else None


async def _exec_state_refresh(appstate, conn, ing, interval=15):
    """Publish the execution snapshot (equity/positions/rails) + per-BUY cooldown
    and dry-run order plan into AppState, so the UI stays a pure reader. Equity is
    the only slow bit (a Kraken call in live) — isolated to a worker thread; every
    DB touch stays on the loop (single-writer safe)."""
    import os
    import time as _t
    from . import broker
    ex = ing.executor
    # monotonic gate for the rollover-fee poll; first run held ~2min past boot so its
    # one-time history walk doesn't contend with the boot reconcile on the private API.
    fees_next = _t.monotonic() + 120
    kr_pos = {}   # last-good Kraken per-pair open-P/L truth (display-only; survives blips)
    krpos_next = _t.monotonic()   # throttle gate for the per-pair OpenPositions(docalcs) poll
    # Intraday stress telemetry: held past boot like the fee poll so its multi-pair
    # candle scan doesn't contend with the boot reconcile.
    stress_next = _t.monotonic() + 90
    while True:
        try:
            mode = config.EXEC_MODE
            balance = None
            margin_used = free_margin = None
            if ex is None:
                equity = None
            elif mode == "live":
                await asyncio.to_thread(_poll_fills_threaded)   # filled limits -> positions + stops
                balance = await asyncio.to_thread(broker.trade_balance_full)

                def _bf(key):   # each field independent — a missing m/mf must not null equity
                    try:
                        return float(balance[key]) if balance else None
                    except (TypeError, ValueError, KeyError):
                        return None
                # equity via the SHARED extractor (e->eb->tb) so the dashboard, rails,
                # peak, and the order path can never disagree on the sizing denominator.
                equity = broker.equity(balance)
                margin_used, free_margin = _bf("m"), _bf("mf")
                if equity:
                    ex._update_peak(equity)      # DB write, back on the loop
                # Rollover fee drag (audit 2026-07-13 #2, wired 2026-07-15): poll the
                # Ledgers API on its own cadence so held-position financing is VISIBLE
                # next to P&L instead of mimicking market losses. Off-loop (own conn),
                # display-only, gated so it never rides the 15s equity cadence.
                rsecs = float(getattr(config, "ROLLOVER_POLL_SECS", 0) or 0)
                if rsecs > 0 and _t.monotonic() >= fees_next:
                    fees_next = _t.monotonic() + rsecs
                    await asyncio.to_thread(_poll_rollover_fees_threaded)
                # Per-pair open-P/L ground truth: Kraken's own mark-to-market per open lot,
                # so the dashboard's PER-PAIR pnl/avg show what Kraken shows instead of a
                # ledger recompute off the single collapsed entry row. THROTTLED on its own
                # cadence (not the ~8s exec loop) — this is an extra OpenPositions(docalcs)
                # call and the account is rate-limit-strained (the hourly Ledgers walk
                # already burns the counter); a display value does not need 8s granularity.
                # The HEADER total is unaffected — it rides TradeBalance `n`, fetched free
                # above every cycle. Best-effort; a blip keeps the last-good map.
                ksecs = float(getattr(config, "KRPOS_POLL_SECS", 45) or 0)
                if ksecs > 0 and _t.monotonic() >= krpos_next:
                    krpos_next = _t.monotonic() + ksecs
                    try:
                        pc = await asyncio.to_thread(broker.open_positions_calc)
                        if pc is not None:
                            kr_pos = _agg_kraken_positions(pc)
                    except Exception:
                        log.exception("open_positions_calc failed (display value only)")
                # Intraday liq-buffer stress (2026-07-19). Off-loop, own conn, own
                # cadence; reads stored candles + one TradeBalance. Telemetry only.
                ssecs = float(getattr(config, "STRESS_POLL_SECS", 0) or 0)
                if (getattr(config, "STRESS_ENABLED", False) and ssecs > 0
                        and _t.monotonic() >= stress_next):
                    stress_next = _t.monotonic() + ssecs
                    await asyncio.to_thread(_poll_stress_threaded)
            else:
                equity = config.PAPER_PORTFOLIO_USD
                free_margin, margin_used = equity, 0.0
            rails_ok, reason = ex.rails_ok(equity) if ex else (True, "")
            positions = [
                {"symbol": r[0], "entry": r[1], "stop": r[2], "volume": r[3],
                 "leverage": r[4], "margin": r[5], "mode": r[6]}
                for r in conn.execute(
                    "SELECT symbol,entry,stop,volume,leverage,margin,mode FROM orders "
                    "WHERE status='open' ORDER BY id DESC")
            ]
            pending = [
                {"symbol": r[0], "entry": r[1], "volume": r[2], "leverage": r[3]}
                for r in conn.execute(
                    "SELECT symbol,entry,volume,leverage FROM orders "
                    "WHERE status='pending' ORDER BY id DESC")
            ]
            # v6 SURVEY read-only plumbing: per-pair ledger, journal tail, realized
            # day/week P&L (F6 boundaries, verbatim from rails_ok), min-fill capacity.
            now = datetime.datetime.now(datetime.timezone.utc)
            day0 = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            wk0 = (now - datetime.timedelta(days=now.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0).isoformat()

            def _realized(since):        # display-only — never let it blank the snapshot
                try:
                    return store.realized_pnl_since(conn, since)
                except Exception:
                    log.exception("realized_pnl_since failed (display value only)")
                    return 0.0

            def _day_swing(eq):
                """Daily book SWING = equity now − equity at the first read of the UTC day.
                Complements 'day' (realized): realized only moves when something CLOSES (for
                a long-only, stops-only book that's $0 until a stop -> a loss), so the swing
                is the live mark-to-market move that actually tracks how the book did today.
                Baseline is persisted in meta, so it survives the day's restarts (best-effort:
                if the bot was down over midnight the baseline is the first read after).
                Display-only; None when equity is unknown."""
                if eq is None:
                    return None
                try:
                    today = now.date().isoformat()
                    d, _, base = (store.meta_get(conn, "day_open_equity") or "").partition("|")
                    if d != today or not base:
                        store.meta_set(conn, "day_open_equity", f"{today}|{eq}")
                        return 0.0
                    return eq - float(base)
                except Exception:
                    log.exception("day swing calc failed (display value only)")
                    return None
            # live stop coverage: open rows carrying a resting stop txid (header
            # safety-reading number — tracks the live book, not the boot recon stamp)
            scov = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(CASE WHEN stop_txid IS NOT NULL "
                "AND stop_txid<>'' THEN 1 ELSE 0 END),0) FROM orders WHERE status='open'"
            ).fetchone()
            # Defense buffer engine (Wave 1): price-space liq telemetry +
            # edge-triggered escalation. Live only (needs the real TradeBalance
            # notional `v`); display/alert only, never gates or sizes an order.
            defense_blob = None
            if mode == "live" and balance:
                defense_blob = _run_defense(conn, balance, equity, margin_used, _t.time())
            appstate.exec = {
                "mode": mode, "equity": equity, "open_count": len(positions),
                "positions": positions, "pending": pending,
                "rails_ok": rails_ok, "rails_reason": reason,
                "halt": os.path.exists(config.HALT_FILE), "updated": _t.time(),
                "balance": balance, "margin_used": margin_used, "free_margin": free_margin,
                "by_pair": _build_by_pair(conn, appstate),
                "journal_tail": store.recent_journal(conn, 200),
                "realized_day": _realized(day0),
                "realized_week": _realized(wk0),
                "swing_day": _day_swing(equity),   # live mark-to-market move since UTC midnight

                "capacity": _snapshot_capacity(conn, appstate, free_margin),
                "last_recon": store.meta_get(conn, "last_recon"),
                "stops_total": scov[0], "stops_covered": scov[1],
                "defense": defense_blob,
                # Kraken ground-truth open P/L (display-only): total from TradeBalance `n`
                # (unrealized net of open positions), per-pair from OpenPositions(docalcs).
                "open_pnl": _bf("n") if mode == "live" else None,
                "kr_pos": kr_pos,
            }
            for sym, ps in list(appstate.pairs.items()):
                card = ps.confirmed
                if card and card.status == "BUY":
                    last = store.last_alert_ts(conn, sym, "confirmed")
                    ps.cooldown_until = (last + config.REALERT_HOURS * 3600) if last else 0.0
                    price = ps.last_tick.last if ps.last_tick else card.price
                    ps.exec_plan = ex.plan(sym, price, card, equity) if (ex and equity) else None
                else:
                    ps.cooldown_until = 0.0
                    ps.exec_plan = None
            # v6 web console: persist the broker-only values (equity/margin/tick
            # price/links) the read-only web server can't get from the DB. Pure
            # display persistence — no trading effect. Never blanks the snapshot.
            try:
                _persist_web_live(conn, appstate, equity, margin_used, free_margin, balance)
            except Exception:
                log.exception("web_live persist failed (display value only)")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("exec state refresh failed (will retry)")
        await asyncio.sleep(interval)


def _journal_safe(conn, kind, symbol, text):
    """Isolated journal emit for the display path (same rule as executor._journal):
    a journal failure (locked DB, disk) must never break the exec-refresh loop."""
    try:
        store.journal(conn, kind, symbol, text)
    except Exception:
        log.exception("defense journal emit failed (display value only)")


def _emit_defense_event(conn, ev, computed, tier):
    """Render one escalation event to journal (kind='defense') + operator alert.
    Severity keyed on the tier being ENTERED: CRITICAL -> a distinct 'liq-risk'
    page (throttle key carries the buffer level, so a worsening crash keeps
    paging instead of being swallowed by the 30-min per-kind throttle); CAUTION
    -> a 'defense' notify; recovery to NOMINAL / boot baseline -> journal only
    (no beep on good news). Never raises into the caller."""
    line = defense.format_line(computed, tier)
    b = ev.get("buffer")
    bkey = f"{b:.0f}%buf" if isinstance(b, (int, float)) else "*"
    if ev.get("kind") == "recritical":
        text = f"DEFENSE CRITICAL worsening — {line}"
        _journal_safe(conn, "defense", "*", text)
        alerter.fire_safety("liq-risk", bkey, text)
        return
    frm, to = ev.get("from"), ev.get("to")
    text = f"DEFENSE {frm or 'init'}→{to} — {line}"
    _journal_safe(conn, "defense", "*", text)
    if to == defense.TIER_CRITICAL:
        alerter.fire_safety("liq-risk", bkey, text)
    elif to == defense.TIER_CAUTION:
        alerter.fire_safety("defense", "*", text)
    # to NOMINAL (recovery / boot baseline) and UNKNOWN: journal only, no alert.


def _run_defense(conn, balance, equity, margin_used, now):
    """Compute the price-space liq buffer from the live TradeBalance, run the
    edge-triggered escalation state machine (deepfield.defense), persist its
    state, and journal + alert ONLY on tier transitions (and >=2% further decay
    within CRITICAL). Returns the computed dict (buffer_call/liq_pct,
    eff_leverage, tier) for the UI + web blob, or None on failure. TELEMETRY
    ONLY — never places, cancels, or sizes an order; never raises into the loop."""
    try:
        try:
            v = float(balance.get("v")) if balance else 0.0
        except (TypeError, ValueError, AttributeError):
            v = 0.0
        computed = defense.compute(equity, margin_used, v)
        nominal = float(getattr(config, "DEFENSE_BUFFER_NOMINAL_PCT", 12.0) or 12.0)
        critical = float(getattr(config, "DEFENSE_BUFFER_CRITICAL_PCT", 6.0) or 6.0)
        blq = computed.get("buffer_liq_pct") if computed else None
        tier = defense.tier_for(blq, nominal, critical)
        try:
            prev = json.loads(store.meta_get(conn, "defense_state") or "{}")
        except Exception:
            prev = {}
        new_state, events = defense.step(prev, computed, tier, now)
        try:
            store.meta_set(conn, "defense_state", json.dumps(new_state))
        except Exception:
            log.exception("defense_state persist failed (display value only)")
        for ev in events:
            _emit_defense_event(conn, ev, computed, tier)
        out = dict(computed) if computed else {}
        out["tier"] = tier
        return out
    except Exception:
        log.exception("defense engine failed (display value only — trade path unaffected)")
        return None


_KRAKEN_PAIR_TO_SYM = {v: k for k, v in config.MARGIN_PAIR.items()}


def _agg_kraken_positions(positions_calc):
    """Aggregate an OpenPositions(docalcs) map into per-symbol GROUND TRUTH for the
    dashboard: {sym: {"net": unrealized$, "vol": open_volume, "avg": blended_cost}}.

    Kraken's per-lot `net`/`value` already reflect only the OPEN remainder (vol_closed
    excluded) and are net of fees/rollover — so summing `net` across a pair's lots is
    exactly what Kraken reports, and always beats recomputing (blob_price − db_entry)×vol
    off the ledger's single collapsed `entry` row. Returns {} on None/empty (caller keeps
    the DB-derived fallback). Display-only — never sizes or gates an order."""
    if not positions_calc:
        return {}
    agg = {}
    for p in positions_calc.values():
        sym = _KRAKEN_PAIR_TO_SYM.get(p.get("pair"))
        if not sym:
            continue
        try:
            vol = float(p.get("vol", 0)) - float(p.get("vol_closed", 0) or 0)
            net = float(p.get("net", 0))
            value = float(p.get("value", 0) or 0)
        except (TypeError, ValueError):
            continue
        a = agg.setdefault(sym, {"net": 0.0, "vol": 0.0, "value": 0.0})
        a["net"] += net
        a["vol"] += vol
        a["value"] += value
    out = {}
    for sym, a in agg.items():
        # blended cost basis of the OPEN remainder: cost = value − net (both from Kraken),
        # so avg = cost / open_vol. None when vol rounds to 0 (fully-closed straggler).
        avg = round((a["value"] - a["net"]) / a["vol"], 8) if a["vol"] else None
        out[sym] = {"net": round(a["net"], 2), "vol": round(a["vol"], 8), "avg": avg}
    return out


def _persist_web_live(conn, appstate, equity, margin_used, free_margin, balance):
    """Write the read-only `web_live` meta blob for deepfield.web.server. Broker-
    only fields (equity/margin/level/links/tick prices) that a DB reader can't see."""
    import json as _json
    import time as _t
    prices, chg = {}, {}
    for sym, ps in appstate.pairs.items():
        if ps.last_tick:
            prices[sym] = ps.last_tick.last
            cp = getattr(ps.last_tick, "change_pct", None)
            if cp is not None:
                chg[sym] = round(cp, 1)
    lvl = None
    try:
        lvl = float(balance["ml"]) if (balance and balance.get("ml")) else None
    except (TypeError, ValueError, KeyError):
        lvl = None
    links = ([bool(appstate.links[n].get("up")) for n in sorted(appstate.links)]
             if appstate.links else None)

    # T/P cycle + rollover fee drag: web/server.py and console.html already read these
    # blob keys (their fee/T-P row renders blank without them). tp values from meta,
    # fees populated by _poll_rollover_fees_threaded. Best-effort — never blank the blob.
    def _mg(key):
        try:
            v = store.meta_get(conn, key)
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def _mg_json(key):
        try:
            v = store.meta_get(conn, key)
            return json.loads(v) if v else None
        except Exception:
            return None
    tpb = _mg("tp_baseline")
    # Defense telemetry (Wave 1) for the web dashboard's future health band. inf
    # (flat book) -> None so the blob stays STRICT JSON (JS JSON.parse rejects
    # Infinity); a JS reader treats None as "no positions / no risk".
    dfn = appstate.exec.get("defense") or {}

    def _finite(x):
        try:
            x = float(x)
            return round(x, 2) if x == x and abs(x) != float("inf") else None
        except (TypeError, ValueError):
            return None
    blob = {
        "equity": equity, "margin_used": margin_used, "free_margin": free_margin,
        "margin_level": round(lvl) if lvl else None,
        "capacity": appstate.exec.get("capacity"),
        "prices": prices, "chg": chg, "links": links,
        "mode": config.EXEC_MODE, "started": appstate.started_ts, "updated": _t.time(),
        "tp_baseline": round(tpb, 2) if tpb else None,
        "tp_target": round(tpb * (1 + config.TP_PCT), 2) if tpb else None,
        "fees_day": _mg("fees_day"), "fees_total": _mg("fees_total"),
        "buffer_liq_pct": _finite(dfn.get("buffer_liq_pct")),
        "buffer_call_pct": _finite(dfn.get("buffer_call_pct")),
        "eff_leverage": _finite(dfn.get("eff_leverage")),
        "defense_tier": dfn.get("tier"),
        # Intraday stress telemetry (_poll_stress_threaded). Read from meta rather
        # than recomputed here so the blob build stays a cheap pure read.
        "stress": _mg_json("stress_state"),
        # Kraken ground-truth open P/L (see _agg_kraken_positions): total header number +
        # per-pair map so the dashboard shows what Kraken shows, not a ledger recompute.
        "open_pnl": _finite(appstate.exec.get("open_pnl")),
        "kr_pos": appstate.exec.get("kr_pos") or {},
    }
    store.meta_set(conn, "web_live", _json.dumps(blob))
    if equity is not None:
        store.equity_snapshot(conn, equity)          # sparkline series, 5-min sampled


async def _hourly_reconciler(ing):
    while True:
        await asyncio.sleep(3600)
        try:
            repairs = await asyncio.to_thread(_heal_all)
            if repairs:
                # Repaired closed bars change the truth the cards were computed
                # from — republish. Quiet sweep (no alerts) by design.
                log.info("hourly reconcile made %d repairs — resweeping confirmed cards", repairs)
                ing.startup_sweep()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("hourly reconcile pass failed (will retry next hour)")


def _startup(debug, announce=False):
    """Shared by run_live and run_once: warm backfill -> DB -> startup sweep."""
    from . import broker
    setup_logging(debug=debug)
    broker.setup_raw_log(config.LOG_DIR)   # RAW order req/resp -> its own audit file
    if announce:
        print("DEEPFIELD warming up — backfilling candle gap (throttled REST)...", flush=True)
    # log=print would flood stdout ahead of every --once/--simple frame; route
    # backfill's per-series lines through logging instead, matching everything else.
    backfill.run(full=False, log=logging.getLogger("deepfield.backfill").info)
    conn = store.connect(config.DB_PATH)
    appstate = AppState()
    ing = ingest_mod.Ingest(conn, appstate)
    if announce:
        print("DEEPFIELD sweeping confirmed scores...", flush=True)
    ing.startup_sweep()
    # Persistence: on a live restart, surface any drift between our open-orders
    # ledger and what Kraken actually shows (a stop may have filled while down).
    if config.EXEC_MODE == "live" and ing.executor is not None:
        try:
            kr = broker.open_positions()
            ours = store.open_position_count(conn)
            # kr is None on an API failure — guard the len() so a transient blip in this
            # cosmetic log line can't raise and skip verify_open_stops() below (which has
            # its own None-handling and MUST run to re-place any missing/orphaned stops).
            log.info("startup position check: ledger open=%d · Kraken open positions=%s",
                     ours, len(kr) if kr is not None else "unavailable")
            ing.executor.verify_open_stops()   # re-place any missing protective stops
        except Exception:
            log.exception("startup position/stop check failed")
    log.info("startup sweep complete: %d pairs, regime=%s",
             len(config.PAIRS), appstate.regime.label if appstate.regime else "?")
    return conn, appstate, ing


def _start_web_console():
    """Serve the read-only web console in a daemon thread so one launch (the desktop
    icon) brings up TUI + web together. Fully isolated: its own ro DB connections, a
    guarded loop — it can never delay or crash the bot. Best-effort; a busy port just
    logs and moves on."""
    import threading

    def _run():
        try:
            from .web import server as web_server
            web_server.serve(port=config.WEB_PORT, quiet=True)
        except OSError as e:
            log.warning("web console not started (port %d in use?): %s", config.WEB_PORT, e)
        except Exception:
            log.exception("web console thread crashed (bot unaffected)")

    threading.Thread(target=_run, name="web-console", daemon=True).start()
    log.info("web console → http://127.0.0.1:%d", config.WEB_PORT)


async def run_live(simple=False, debug=False):
    log.info("DEEPFIELD starting (simple=%s)", simple)
    conn, appstate, ing = _startup(debug, announce=not simple)
    if config.WEB_ENABLED:
        try:
            _start_web_console()
        except Exception:
            log.exception("web console launch failed (bot continues)")
    _sys_journal(conn, f"process start — survey v{VERSION} · exec {config.EXEC_MODE}")
    symbols = [p["ws"] for p in config.PAIRS]
    queue = asyncio.Queue()

    clients = _make_ws_clients(symbols, queue)
    stop = asyncio.Event()
    heal_running = {"flag": False}

    def on_quit():
        log.info("key: q — shutting down")
        stop.set()

    def on_pause():
        appstate.paused = not appstate.paused
        appstate.pause_dirty = True
        log.info("key: p — render %s", "paused" if appstate.paused else "resumed")

    def on_force_reconcile():
        if heal_running["flag"]:
            log.info("key: f — reconcile already running, ignored")
            return
        log.info("key: f — forcing full reconcile")

        async def _run():
            heal_running["flag"] = True
            try:
                repairs = await asyncio.to_thread(_heal_all)
                log.info("forced reconcile complete: %d repairs", repairs)
                if repairs:
                    ing.startup_sweep()
            finally:
                heal_running["flag"] = False
        asyncio.ensure_future(_run())

    def on_test_alert():
        log.info("key: a — test alert")

        def _fire():
            c = store.connect(config.DB_PATH)  # thread-local conn, never the writer's
            try:
                alerter.test_alert(c)
            finally:
                c.close()
        asyncio.ensure_future(asyncio.to_thread(_fire))

    # ── v6 SURVEY view controls — mutate AppState only, then wake the renderer ──
    appstate._key_evt = asyncio.Event()   # run_ui waits on this for instant redraw

    def _wake():
        appstate.pause_dirty = True        # so a keypress redraws even while paused
        appstate._key_evt.set()

    def on_view(n):
        return lambda: (ui.nav_view(appstate, n), _wake())

    def on_select(delta):
        return lambda: (ui.nav_select(appstate, delta), _wake())

    def on_expand():
        ui.nav_expand(appstate)
        _wake()

    def on_scroll(delta):
        return lambda: (ui.nav_scroll(appstate, delta), _wake())

    keys = KeyController(asyncio.get_running_loop(), {
        b"q": on_quit, b"p": on_pause, b"f": on_force_reconcile, b"a": on_test_alert,
        b"1": on_view(1), b"2": on_view(2), b"3": on_view(3),
        b"j": on_select(1), b"k": on_select(-1),
        b"\x1b[B": on_select(1), b"\x1b[A": on_select(-1),   # ↓ / ↑
        b"\r": on_expand, b"\n": on_expand, b" ": on_expand,
        b",": on_scroll(-1), b".": on_scroll(1),
    })
    keys_active = keys.start() if not simple else False

    tasks = [asyncio.ensure_future(c.run()) for c in clients]
    tasks.append(asyncio.ensure_future(ing.run(queue)))
    tasks.append(asyncio.ensure_future(ing.clock_close_watchdog(rest_client.fetch_ohlc)))
    tasks.append(asyncio.ensure_future(_hourly_reconciler(ing)))
    tasks.append(asyncio.ensure_future(_exec_state_refresh(appstate, conn, ing)))
    tasks.append(asyncio.ensure_future(
        simple_ui.run_simple(appstate, conn) if simple
        else ui.run_ui(appstate, conn, show_keys=keys_active)
    ))

    stop_task = asyncio.ensure_future(stop.wait())
    try:
        done, _pending = await asyncio.wait([stop_task, *tasks],
                                            return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            if t is not stop_task and t.exception() is not None:
                log.error("task died: %r", t.exception())
    finally:
        keys.stop()
        stop_task.cancel()
        for c in clients:
            await c.stop()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        _sys_journal(conn, "process stop — clean shutdown")
        conn.close()
        log.info("DEEPFIELD stopped cleanly")


def run_once(debug=False):
    """--once: single confirmed evaluation + one plaintext frame (cron/tests)."""
    conn, appstate, ing = _startup(debug)
    print(simple_ui.render_frame_text(appstate, conn))
    conn.close()


def run_exec_probe(debug=False):
    """--exec-probe: send validate=true orders for EVERY config.PAIRS pair against
    real Kraken — proves pair name, leverage, precision, and minimums are accepted
    WITHOUT executing. The proof gate before EXEC_MODE goes live, and the ROSTER
    AUTHORITY (operator dispatch 2026-07-19): a pair only trades if it validates.

    Per pair: price = confirmed card when present, else the newest DB close (new
    pairs have no card at probe time — backfill above guarantees candles). On an
    Invalid-price reject the probe COARSENS config.MARGIN_TICK_DECIMALS one digit
    and retries (the :BTNL book is coarser than spot for some pairs — CRV/SHIB
    precedent); on Unknown-asset-pair it marks the pair DROP (no :BTNL book on
    this account). Everything is journaled machine-readably to
    logs/exec_probe_journal.json for scripts/gen_roster.py --probe to consume."""
    from . import broker, executor
    setup_logging(debug=debug)
    broker.setup_raw_log(config.LOG_DIR)
    if not broker.keys_present():
        print(f"NO KEYS — put your Kraken key/secret (2 lines) in {broker.KEYFILES[0]} first.")
        print("Use a DEDICATED API key for DEEPFIELD (nonce is per-key; sharing with hydra collides).")
        return
    backfill.run(full=False, log=logging.getLogger("deepfield.backfill").info)
    conn = store.connect(config.DB_PATH)
    appstate = AppState()
    ing = ingest_mod.Ingest(conn, appstate)
    ing.startup_sweep()
    ex = executor.Executor(conn)
    ex.mode = "validate"
    journal = {"probed": [], "dropped": [], "ticks": {}}
    print(f"VALIDATE PROBE — real Kraken order-check on {len(config.PAIRS)} pairs, nothing executes:\n")
    for p in config.PAIRS:
        sym = p["ws"]
        ps = appstate.pair(sym)
        card = ps.confirmed
        price = card.price if card else None
        if not price:
            row = conn.execute(
                "SELECT c FROM candles WHERE pair=? AND closed=1 ORDER BY ts DESC LIMIT 1",
                (sym,)).fetchone()
            price = row[0] if row else None
        if not price or price <= 0:
            print(f"  {sym:13s} ❌ NOPRICE — no card and no DB close (backfill failed?)")
            journal["dropped"].append({"pair": sym, "why": "no price after backfill"})
            continue
        # Retry loop: coarsen tick decimals on precision rejects, floor 0.
        start_tick = config.MARGIN_TICK_DECIMALS.get(sym, 2)
        verdict = None
        while True:
            oid = ex.place_entry(sym, price, card)
            row = conn.execute(
                "SELECT status, entry, stop, volume, leverage, error FROM orders WHERE id=?",
                (oid,)).fetchone() if oid else None
            if row is None:
                verdict = ("reject", "no order row (see log)")
                break
            st, entry, stop, vol, lev, err = row
            if st == "validated":
                tick = config.MARGIN_TICK_DECIMALS.get(sym, 2)
                note = f" (tick {start_tick}->{tick})" if tick != start_tick else ""
                print(f"  {sym:13s} ✅ validated vol={vol:g} x{lev} @ {entry} stop={stop}{note}")
                journal["probed"].append({
                    "pair": sym, "lev": lev, "tick": tick, "volume": vol,
                    "entry": entry, "stop": stop, "notional": vol * entry,
                    "margin": vol * entry / lev})
                journal["ticks"][sym] = tick
                verdict = ("ok", None)
                break
            es = err or ""
            tick = config.MARGIN_TICK_DECIMALS.get(sym, 2)
            if ("Invalid price" in es or "decimal" in es.lower()) and tick > 0:
                config.MARGIN_TICK_DECIMALS[sym] = tick - 1
                print(f"  {sym:13s} …  price precision reject at {tick} decimals — retrying at {tick - 1}")
                continue
            if "Unknown asset pair" in es:
                verdict = ("drop", es)
            else:
                verdict = ("reject", es)
            break
        if verdict[0] in ("drop", "reject"):
            mark = "🗑" if verdict[0] == "drop" else "❌"
            print(f"  {sym:13s} {mark} {verdict[0].upper()}  {verdict[1]}")
            journal["dropped"].append({"pair": sym, "why": verdict[1]})
        time.sleep(0.3)   # be gentle on the AddOrder counter — probe is 130+ calls
    jpath = os.path.join(config.LOG_DIR, "exec_probe_journal.json")
    with open(jpath, "w") as f:
        json.dump(journal, f, indent=1)
    ok, dr = len(journal["probed"]), len(journal["dropped"])
    print(f"\n{ok} validated · {dr} dropped/rejected · journal -> {jpath}")
    conn.close()
