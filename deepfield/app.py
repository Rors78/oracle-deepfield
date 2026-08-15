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
from . import vol as volatility
from . import paper_broker
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
        # Was hardcoded 'live' back when only the live branch reached here. Paper now
        # rides the same path against the simulated exchange, and the mode is what
        # every order query filters on — hardcoding it would file paper fills as live.
        e.mode = config.EXEC_MODE
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


def _anchor_flows_eb(c, broker):
    """Record eb (equivalent balance) at the flow cursor. eb is the reconciliation
    yardstick because it EXCLUDES open-position unrealized P&L (e = tb + n), so on
    a USD-collateral account it moves only with external flows, realized P&L and
    fees — market drift on the open book cannot fake or mask a flow. Guarded: an
    anchor miss only means the next window's gate falls back to allow-with-log."""
    try:
        tb = broker.trade_balance_full()
        eb = float(tb["eb"]) if tb and tb.get("eb") not in (None, "") else None
        if eb is not None:
            store.meta_set(c, "flows_eb_anchor", str(round(eb, 4)))
    except Exception:
        log.exception("flows eb anchor failed (reconciliation gate degrades to allow)")


def _reconcile_external_flow(c, broker, cursor, net):
    """The gate that would have caught the -$72.11 (operator order 2026-08-15).

    That number was a triple-counted $24: the typed Ledgers walk passed filter
    values Kraken ignores, re-summing the whole window per type. The hand-check
    that caught it live — ledger-claimed flow vs what the account actually lost —
    is now permanent: reconciled = eb_now - eb_anchor - realized P&L over the
    same window. REFUSE the shift when |ledger - reconciled| > max($2, 5% of the
    flow); log both numbers, page the operator, and leave every switch yardstick
    (peak/baseline/trough) and the cursor untouched.

    Tolerance notes: rollover fees (~$0.03/hr) bias reconciled slightly negative
    and are folded into the $2 floor rather than window-matched — the rollover
    poll's cursor does not align with this one. Missing anchor or failed eb read
    degrades to ALLOW with a loud line (the gate must not brick legitimate flow
    accounting on its own telemetry gap); the anchor re-arms on every advance."""
    try:
        anchor_raw = store.meta_get(c, "flows_eb_anchor", None)
        anchor = float(anchor_raw) if anchor_raw not in (None, "") else None
    except (TypeError, ValueError):
        anchor = None
    eb_now = None
    try:
        tb = broker.trade_balance_full()
        eb_now = float(tb["eb"]) if tb and tb.get("eb") not in (None, "") else None
    except Exception:
        pass
    if anchor is None or eb_now is None:
        log.warning("external-flow reconciliation UNAVAILABLE (eb anchor=%s, eb now=%s) "
                    "— allowing the $%+.2f shift unreconciled this once", anchor_raw, eb_now, net)
        return True
    cursor_iso = datetime.datetime.fromtimestamp(
        float(cursor), datetime.timezone.utc).isoformat()
    try:
        realized = float(store.realized_pnl_since(c, cursor_iso, config.EXEC_MODE) or 0.0)
    except Exception:
        log.exception("realized-P&L read failed in flow reconciliation — treating as 0")
        realized = 0.0
    reconciled = eb_now - anchor - realized
    tol = max(2.0, 0.05 * abs(net))
    if abs(net - reconciled) > tol:
        msg = (f"ledger claims ${net:+.2f} external flow but the account moved "
               f"${reconciled:+.2f} over the same window (eb {anchor:.2f} -> {eb_now:.2f}, "
               f"realized ${realized:+.2f}) — gap ${abs(net - reconciled):.2f} > "
               f"tol ${tol:.2f}. SHIFT REFUSED; peak/baseline/trough untouched.")
        log.error("external-flow RECONCILIATION FAILED: %s", msg)
        try:
            alerter.fire_safety("flow-mismatch", "*", msg)
        except Exception:
            log.exception("flow-mismatch alert failed (refusal already logged)")
        return False
    log.info("external flow $%+.2f reconciled against account delta $%+.2f "
             "(tol $%.2f) — shift allowed", net, reconciled, tol)
    return True


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
        _anchor_flows_eb(c, broker)
        log.info("external-flow accounting anchored at %s — deposits/withdrawals "
                 "shift the T/P baseline from here on",
                 now.isoformat(timespec="seconds"))
        return
    r = broker.external_flows_since(cursor)
    if r is None:                        # API failure: keep the cursor, retry next poll
        return
    net, count, complete = r
    if not complete:
        log.warning("external-flow window truncated at the page ceiling — net flow is "
                    "a partial sum; the missed remainder shifts nothing (baseline may "
                    "lag until the operator reconciles)")
    if count == 0 or abs(net) < 0.01:
        store.meta_set(c, "flows_cursor", now.timestamp())
        _anchor_flows_eb(c, broker)
        return
    if not _reconcile_external_flow(c, broker, cursor, net):
        # REFUSED (operator decision 2026-08-15, after the -$72.11 incident): the
        # cursor and eb anchor deliberately do NOT advance, so the window stays
        # open for adjudication and the next poll re-checks — a transient
        # mismatch heals itself, a real one keeps refusing (alert throttled).
        return
    store.meta_set(c, "flows_cursor", now.timestamp())
    _anchor_flows_eb(c, broker)
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
        # THROUGH executor.tp_target — never `new * (1 + TP_PCT)` inline. That
        # exact re-spelling is the 39%-off deck bug of 2026-08-05 (the docstring on
        # tp_target tells the story), and this narration line was the last caller
        # still holding a private copy (2026-08-07 audit, money-path finding 6):
        # whenever the trough ratchet was active it overstated the target it
        # claimed to report. Log-only, but a log that states a number the executor
        # will not act on is how operators learn to distrust the log.
        from . import executor as _ex      # local import, as elsewhere in this file
        try:
            tro = float(store.meta_get(c, "tp_trough", new) or new)
        except (TypeError, ValueError):
            tro = new
        target_note = f"${_ex.tp_target(new, min(tro, new)):.2f}"
        cur = c.execute("UPDATE meta SET value=? WHERE key='tp_baseline' AND value=?",
                        (str(round(new, 4)), raw_baseline))
    if cur.rowcount == 0:
        c.commit()
        log.warning("external flow $%+.2f NOT applied — tp_baseline changed underneath "
                    "(T/P cycle completed concurrently; its post-flatten baseline already "
                    "contains the flow)", net)
        return
    c.commit()
    # The trough ratchet (executor _check_take_profit, 2026-07-24) measures the
    # same trading-equity dollars as the baseline — shift it by the same net, with
    # the same CAS-skip semantics: if the executor ratcheted concurrently, the
    # trough it wrote came from a post-flow equity read and already contains this
    # flow. A withdrawal must shift the trough or the equity dip it causes would
    # be double-counted (once here on the baseline, once by the ratchet).
    raw_trough = store.meta_get(c, "tp_trough", None)
    trough = 0.0
    try:
        trough = float(raw_trough or 0)
    except (TypeError, ValueError):
        pass
    if trough > 0:
        newt = trough + net
        tcur = c.execute("UPDATE meta SET value=? WHERE key='tp_trough' AND value=?",
                         (("0.0" if new <= 0 or newt <= 0 else str(round(newt, 4))),
                          raw_trough))
        c.commit()
        if tcur.rowcount == 0:
            log.info("T/P trough shift $%+.2f skipped — ratcheted underneath "
                     "(post-flow equity already contains it)", net)
    # Kill-switch peak rides the same ledger truth (rails re-arm 2026-07-30): a
    # deposit is not profit and a withdrawal is not a drawdown — an unshifted
    # peak would either mask a real drawdown (deposit-inflated peak already bit
    # once: the 07-16 $40 deposit helped set the old 223.79) or trip the switch
    # on money the operator moved out. Same CAS-skip semantics as the baseline:
    # if the executor reset/ratcheted the peak underneath, that write came from
    # a post-flow equity read and already contains this flow. Floor 0 — a big
    # withdrawal clears it and _update_peak re-seeds from live equity.
    raw_peak = store.meta_get(c, "peak_equity", None)
    peak = 0.0
    try:
        peak = float(raw_peak or 0)
    except (TypeError, ValueError):
        pass
    if peak > 0:
        newp = peak + net
        pcur = c.execute("UPDATE meta SET value=? WHERE key='peak_equity' AND value=?",
                         (("0.0" if newp <= 0 else str(round(newp, 4))), raw_peak))
        c.commit()
        if pcur.rowcount == 0:
            log.info("kill-switch peak shift $%+.2f skipped — peak changed underneath "
                     "(post-flow equity already contains it)", net)
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
    # Scoped to the running book: this weights the STRESS basket, a safety reading.
    # With both ledgers in one file (paper is seeded from a live snapshot) an
    # unscoped sum weights a simulated basket by real positions, and the survivability
    # verdict then describes a book that is not being traded.
    m = _book_mode(config.EXEC_MODE)
    rows = conn.execute(
        "SELECT symbol, SUM(volume * entry) FROM orders WHERE status='open' "
        "AND volume > 0 AND entry > 0" + (" AND mode=?" if m else "") +
        " GROUP BY symbol", (m,) if m else ()).fetchall()
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


def _refresh_vol_table_threaded():
    """Off-loop (own conn): recompute the per-pair ATR distance table (operator ruling
    2026-08-06 §d).

    Reads ONLY stored daily candles — no exchange call, so this can never spend the
    per-account rate limit the live trading loop depends on. Runs on its own thread and
    its own cadence rather than inline, because it walks every roster pair and the exec
    loop turns over in ~8s.

    Persisted with a timestamp so the deck and the TUI show the SAME numbers the money
    path resolves against, rather than each recomputing and drifting. Open positions are
    unaffected: their distances were frozen onto the row at fill."""
    try:
        conn = store.connect(config.DB_PATH)
    except Exception:
        log.exception("vol refresh: cannot open DB (table unchanged)")
        return
    try:
        table = volatility.build_table(conn)
        volatility.save_table(conn, table)
        atr_n = sum(1 for d in table.values() if d.get("source") == "atr")
        clamped = [s for s, d in table.items() if d.get("liq_clamped")]
        floored = [s for s, d in table.items() if d.get("tp_floored")]
        log.info("VOL table refreshed: %d pairs (%d from ATR, %d fallback)%s%s",
                 len(table), atr_n, len(table) - atr_n,
                 ("; liq-clamped SL: " + ",".join(sorted(clamped))) if clamped else "",
                 ("; TP at floor: " + ",".join(sorted(floored))) if floored else "")
    except Exception:
        log.exception("vol refresh failed (previous table stands)")
    finally:
        try:
            conn.close()
        except Exception:
            pass


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
            store.meta_set(c, "stress_state", json.dumps(
                {"flat": True, "updated": time.time()}))
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
            # Freshness stamp so the L_eff growth gate (_stack_margin_ok) can
            # fail open on a stale blob instead of trusting a dead poll forever.
            "updated": time.time(),
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
    # Scoped like every other aggregate in the snapshot — the per-pair ledger the
    # operator reads must be the book the executor is trading.
    m = _book_mode(config.EXEC_MODE)
    mc, ma = (" AND mode=?", (m,)) if m else ("", ())
    for oid, sym, ts, vol, lev, entry, stop, stop_txid in conn.execute(
            "SELECT id, symbol, ts, volume, leverage, entry, stop, stop_txid FROM orders "
            "WHERE status='open'" + mc + " ORDER BY symbol, id", ma):
        d = by_pair.setdefault(sym, {"fills": [], "pendings": [], "vol_sum": 0.0,
                                     "avg_entry": None, "upnl": None, "stop": None})
        d["fills"].append({"id": oid, "ts": ts, "vol": vol, "lev": lev,
                           "entry": entry, "stop": stop, "stop_txid": stop_txid})
    for sym, price, vol, ts in conn.execute(
            "SELECT symbol, entry, volume, ts FROM orders "
            "WHERE status='pending'" + mc + " ORDER BY symbol, id", ma):
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
    # Soon after boot, then daily. The table must exist before the first entry resolves
    # its distances — vol.distances() computes on demand if it is missing, but that puts
    # a per-pair candle walk on the exec loop instead of off it.
    vol_next = _t.monotonic() + 15
    while True:
        try:
            mode = config.EXEC_MODE
            balance = None
            margin_used = free_margin = None
            if ex is None:
                equity = None
            # Paper rides the SAME branch as live — but only with a simulated
            # exchange attached. If attach ever failed, fall through to the inert
            # branch rather than aiming these private calls at the real account.
            elif mode == "live" or (mode == "paper" and paper_broker.attached()):
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
            # Volatility table (ruling §d). Outside the live-only branch above: it is
            # pure local candle math, so paper resolves the same distances live does.
            vsecs = float(getattr(config, "VOL_REFRESH_SECS", 0) or 0)
            if vsecs > 0 and _t.monotonic() >= vol_next:
                vol_next = _t.monotonic() + vsecs
                await asyncio.to_thread(_refresh_vol_table_threaded)
            rails_detail = ex.rails_detail(equity) if ex else None
            # The growth gates (respend pacing / regime / stack) refuse seeds and
            # rungs while every rail is green — computed by the executor, shipped
            # verbatim, never re-derived downstream (2026-08-07 audit, deck R1:
            # the deck read "clear to buy" through an empty respend bucket).
            growth_detail = ex.growth_detail() if ex else None
            rails_detail = _kill_switch_flow_recheck(conn, ex, equity, rails_detail)
            rails_ok, reason = ((rails_detail["ok"], rails_detail["reason"])
                                if rails_detail else (True, ""))
            rails_block_since = _track_rails_block(conn, rails_ok, reason)
            positions, pending = _book_rows(conn, mode)
            # v6 SURVEY read-only plumbing: per-pair ledger, journal tail, realized
            # day/week P&L (F6 boundaries, verbatim from rails_ok), min-fill capacity.
            now = datetime.datetime.now(datetime.timezone.utc)
            day0 = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            wk0 = (now - datetime.timedelta(days=now.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0).isoformat()

            def _realized(since):        # display-only — never let it blank the snapshot
                try:
                    # MODE-SCOPED, like rails_ok and the web console. This was the one
                    # caller of three that the 2026-08-05 mode-scoping missed, while
                    # the comment above still claimed "verbatim from rails_ok" — it was
                    # not. A paper run is seeded from a live snapshot, so one DB holds
                    # both ledgers; unscoped, the TUI's realized day/week showed REAL
                    # money losses against a simulated book.
                    #
                    # The 'off'->None mapping is _book_mode's — call it, don't
                    # re-spell it. This closure held its own inline copy of the
                    # rule while the test that claimed to pin it asserted on a
                    # third copy (a constant expression in the test body), so a
                    # revert here to bare `mode` was invisible to the suite
                    # (2026-08-07 audit, test-forge finding 6). One rule, one
                    # implementation; the test now pins _book_mode and, through
                    # it, this caller.
                    return store.realized_pnl_since(conn, since, _book_mode(mode))
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
            scov = _stop_coverage(conn, mode)         # same book as everything above
            # Defense buffer engine (Wave 1): price-space liq telemetry +
            # edge-triggered escalation. Needs a TradeBalance for the notional `v` —
            # which the paper simulator now supplies, so the gate is the BALANCE, not
            # the mode. It was `mode == "live"` from when only live reached here, and
            # that left the Defense card's liq buffer / call buffer / effective
            # leverage reading "—" for the entire paper session. Display/alert only,
            # never gates or sizes an order; paper alerts are forced quiet in the
            # alerter, so a simulated book cannot page anyone.
            defense_blob = None
            if balance:
                defense_blob = _run_defense(conn, balance, equity, margin_used, _t.time())
                # Ratio-space watch alongside the price-space tiers above: the two
                # measure different failure modes and can disagree (see docstring).
                _check_margin_level(conn, balance)
                # Did equity move because the book moved, or because money changed
                # POCKETS? The kill switch cannot tell the difference (2026-08-05).
                _check_collateral_shift(conn, balance)
            appstate.exec = {
                "mode": mode, "equity": equity, "open_count": len(positions),
                "positions": positions, "pending": pending,
                "rails_ok": rails_ok, "rails_reason": reason,
                "rails_detail": rails_detail, "rails_block_since": rails_block_since,
                "growth_detail": growth_detail,
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


_ml_worst_bucket = None   # worst 5-point margin-level bucket paged since the last recovery


def _book_mode(mode):
    """WHICH BOOK does an aggregate mean? A paper run is seeded from a live DB
    snapshot, so one file legitimately holds a live P&L ledger AND live paper rows.

    Returns the mode to filter on, or None for "every row" — which is what 'off'
    means here. No order row is ever written with mode='off', so scoping to the
    literal would blank a real book that the operator is inspecting with execution
    disabled: a regression dressed as a fix. The web console reaches the same answer
    by a different road (server._active_mode falls through 'off' to the last working
    order)."""
    return mode if mode in ("live", "paper") else None


def _book_rows(conn, mode):
    """Open positions and resting entry bids for ONE book — (positions, pending).

    These were unscoped while rails_ok counted committed positions mode-scoped, so
    the executor and the operator's screen were describing different books. Harmless
    with a single book in the file; it is paper, where both ledgers coexist, that the
    split quietly invalidated."""
    m = _book_mode(mode)
    clause = " AND mode=?" if m else ""
    args = (m,) if m else ()
    positions = [
        {"symbol": r[0], "entry": r[1], "stop": r[2], "volume": r[3],
         "leverage": r[4], "margin": r[5], "mode": r[6]}
        for r in conn.execute(
            "SELECT symbol,entry,stop,volume,leverage,margin,mode FROM orders "
            "WHERE status='open'" + clause + " ORDER BY id DESC", args)
    ]
    pending = [
        {"symbol": r[0], "entry": r[1], "volume": r[2], "leverage": r[3]}
        for r in conn.execute(
            "SELECT symbol,entry,volume,leverage FROM orders "
            "WHERE status='pending'" + clause + " ORDER BY id DESC", args)
    ]
    return positions, pending


def _stop_coverage(conn, mode):
    """(open lots, lots carrying a resting protective stop) for ONE book. The live
    safety reading behind "7/7 stops resting" — it tracks the book right now rather
    than the boot recon stamp, so it must not count another book's stops."""
    m = _book_mode(mode)
    clause = " AND mode=?" if m else ""
    args = (m,) if m else ()
    return conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(CASE WHEN stop_txid IS NOT NULL "
        "AND stop_txid<>'' THEN 1 ELSE 0 END),0) FROM orders WHERE status='open'"
        + clause, args).fetchone()


def _kill_switch_flow_recheck(conn, ex, equity, rails_detail):
    """Pre-latch flow check (operator decision 2026-08-15). When the kill switch
    is about to NEWLY latch a block, run one immediate external-flow poll before
    trusting the drawdown — a ledger-explained equity drop (deposit, withdrawal,
    transfer, spend) is money changing pockets, not losses, and must never trip
    the switch. The 2026-08-15 incident: a $20 eval-fee purchase between hourly
    polls read as pure drawdown for 42+ minutes because the switch evaluated
    every cycle while the flow accountant slept.

    Fires ONLY on the transition into a new block (rails_block_since empty) and
    only for the kill-switch reason — a standing block never re-polls, so a real
    drawdown costs exactly one extra Ledgers walk. If the poll shifts the peak,
    the fresh verdict simply never latches; if it doesn't, the original verdict
    stands. NEVER raises into the poll loop."""
    try:
        if (rails_detail is None or rails_detail.get("ok")
                or "KILL SWITCH" not in str(rails_detail.get("reason", ""))
                or (store.meta_get(conn, "rails_block_since", None) or "")):
            return rails_detail
        log.warning("kill switch would newly latch (%s) — immediate external-flow "
                    "poll before trusting the drawdown", rails_detail.get("reason"))
        from . import broker as _broker
        _poll_external_flows(conn, _broker,
                             datetime.datetime.now(datetime.timezone.utc))
        fresh = ex.rails_detail(equity)
        if fresh.get("ok"):
            log.warning("kill-switch trip EXPLAINED by external flow — peak shifted, "
                        "block not latched")
        return fresh
    except Exception:
        log.exception("pre-latch flow recheck failed — latching on the original verdict")
        return rails_detail


def _track_rails_block(conn, rails_ok, reason):
    """Put a CLOCK on a blocking rail, and announce the moment the bot goes inert.

    2026-08-05: two consecutive boots ran with the kill switch down. Every entry and
    every ladder rung was refused for the whole of both runs, and the only trace was
    an INFO line per symbol per cycle — indistinguishable, in a busy log, from the
    bot working normally. The operator restarted twice into a frozen bot.

    A standing block is a chronic STATE and re-paging it on a timer is the exact
    habit SAFETY_ALERT_QUIET_KINDS exists to break. But CROSSING INTO inert is an
    event, and nothing announced it. So this fires once, only after the block has
    stood for RAILS_INERT_ALERT_MINS — short blocks are ordinary (MAX_OPEN breathes
    as rungs fill and close) and a one-cycle block is not news. The ordinary
    per-kind throttle governs everything after.

    The clock is persisted in `meta`, not held in memory, so it survives the
    restarts — which matters more than usual here, because restarting is precisely
    what the operator does when the bot looks wrong. An in-memory clock would have
    reset on both of those boots and never reached the threshold.

    Returns the ISO instant the current block began, or None when rails are clear.
    NEVER raises into the poll loop."""
    try:
        if rails_ok:
            if store.meta_get(conn, "rails_block_since", None):
                store.meta_set(conn, "rails_block_since", "")
                store.meta_set(conn, "rails_block_alerted", "")
                log.warning("RAILS CLEAR — entries and ladder rungs live again")
            return None

        since = store.meta_get(conn, "rails_block_since", None) or ""
        now = datetime.datetime.now(datetime.timezone.utc)
        if not since:
            store.meta_set(conn, "rails_block_since", now.isoformat())
            log.warning("RAILS BLOCKING — no new entries, no ladder rungs: %s", reason)
            return now.isoformat()

        try:
            started = datetime.datetime.fromisoformat(since)
            if started.tzinfo is None:
                started = started.replace(tzinfo=datetime.timezone.utc)
            mins = (now - started).total_seconds() / 60.0
        except (TypeError, ValueError):
            # Unparseable stamp: re-anchor rather than alerting off a bad clock.
            store.meta_set(conn, "rails_block_since", now.isoformat())
            return now.isoformat()

        if (mins >= float(getattr(config, "RAILS_INERT_ALERT_MINS", 30))
                and not store.meta_get(conn, "rails_block_alerted", None)):
            store.meta_set(conn, "rails_block_alerted", now.isoformat())
            hrs = mins / 60.0
            span = f"{mins:.0f} min" if mins < 90 else f"{hrs:.1f}h"
            alerter.fire_safety(
                "rails-inert", "—",
                f"bot has bought NOTHING for {span} — {reason}")
            log.warning("RAILS INERT %s — %s", span, reason)
        return since
    except Exception:
        log.exception("rails block tracking failed (non-fatal)")
        return None


def _check_collateral_shift(conn, balance):
    """Name a PHANTOM DRAWDOWN: equity falling because money changed pockets.

    2026-08-05, the incident behind this. Kraken reported eb $225.30 (the whole
    account) against tb $173.83 (the margin-collateral subset). Equity is derived
    from tb, so it read $173.87 — a $51.47 fall with no trade, no loss and no
    ledger flow. The kill switch measures equity against peak and cannot tell those
    apart, so it fired, and the bot bought nothing for two entire boots.

    The existing flow-shift (external_flows_since) does not cover this. It walks
    Ledgers for `deposit` and `withdrawal` ONLY, and a collateral-composition change
    is neither: the money never left the account, it merely stopped being accepted
    as margin. So peak_equity is never shifted and the drop reads as pure drawdown.

    REPORTS, DOES NOT CORRECT — deliberately. Auto-shifting peak by the collateral
    delta would be wrong and dangerous: tb also moves when a non-USD collateral
    holding is REVALUED, and that is a genuine drawdown. Silently absorbing it would
    disarm the kill switch exactly when it should fire. So this makes the operator's
    invisible problem visible and leaves the decision (clear peak_equity or not)
    with him.

    Quiet by design: a standing gap is a chronic STATE. Only a CHANGE in the gap is
    news, and only one worth more than a dollar. Never raises into the poll loop."""
    try:
        def _f(k):
            try:
                v = balance.get(k) if balance else None
                return float(v) if v not in (None, "") else None
            except (TypeError, ValueError, AttributeError):
                return None
        eb, tb = _f("eb"), _f("tb")
        if eb is None or tb is None:
            return None
        gap = eb - tb
        prev = store.meta_get(conn, "collateral_gap", None)
        try:
            prev = float(prev) if prev not in (None, "") else None
        except (TypeError, ValueError):
            prev = None
        store.meta_set(conn, "collateral_gap", round(gap, 4))
        if prev is None or abs(gap - prev) < 1.0:
            return round(gap, 2)

        delta = gap - prev
        if delta > 0:
            # Money left the collateral pool: equity drops, the book did nothing.
            msg = (f"${delta:,.2f} left MARGIN COLLATERAL — equity falls by that much "
                   f"with no trade and no loss. Account ${eb:,.2f}, collateral "
                   f"${tb:,.2f}. The kill switch reads this as drawdown.")
        else:
            msg = (f"${abs(delta):,.2f} returned to margin collateral — equity rises "
                   f"with no trading gain. Account ${eb:,.2f}, collateral ${tb:,.2f}.")
        log.warning("COLLATERAL SHIFT: %s", msg)
        try:
            store.journal(conn, "collateral-shift", "—", msg)
        except Exception:
            pass                      # journaling is a nicety; the log line is the record
        alerter.fire_safety("collateral-shift", "—", msg, loud=False)
        return round(gap, 2)
    except Exception:
        log.exception("collateral shift check failed (non-fatal)")
        return None


def _check_margin_level(conn, balance):
    """Page when KRAKEN'S OWN margin level approaches the exchange's seizure band.

    Not a duplicate of the price-space defense tiers. Those answer "how far can the
    basket fall before liquidation"; this answers "how close is the account to a
    margin call right now". At a mixed per-pair leverage the two diverge widely —
    on 2026-07-22 the book sat at ml 132% with a healthy 21.9% liq buffer reading
    NOMINAL, because used margin is a large fraction of equity while the price
    distance to liquidation stays long. Kraken calls at ml<=80% and force-liquidates
    from 40%, bypassing every stop and invisibly to the ledger, so the ratio needs
    its own watch and cannot be inferred from the buffer.

    On the threshold: MARGIN_LEVEL_ALERT_PCT was written by audit 2026-07-13 #2 at
    150 and then never read by any code. 150 was the twin of an all-pairs-10:1 book;
    the current 2-5x mix floors near ml 130 in ordinary running (p05 136, p50 161
    over the last 3000 logged samples), so wiring 150 verbatim would have paged
    continuously and trained the operator to ignore the channel — the same failure
    the edge-triggered stack-pause narration was introduced to stop. 120 sits under
    the observed operating floor and over the 2026-07-16 near-margin-call band
    (106-115), so it stays quiet in normal running and fires on a real approach.

    FAILS OPEN: an unknown/garbage margin level never pages. Throttle key carries a
    5-point bucket so a worsening slide keeps paging instead of being swallowed by
    the per-kind window (same trick as the defense buffer key). Returns the level
    for the caller's telemetry, or None. Never raises into the loop."""
    try:
        floor = float(getattr(config, "MARGIN_LEVEL_ALERT_PCT", 0) or 0)
        try:
            lvl = float(balance["ml"]) if (balance and balance.get("ml")) else None
        except (TypeError, ValueError, KeyError):
            lvl = None
        if lvl is None or lvl != lvl or lvl <= 0:
            return None                      # unknown -> never page on a bad read
        global _ml_worst_bucket
        if floor <= 0:                       # 0 disables
            return lvl
        if lvl >= floor:
            # Re-arm only on a REAL recovery, not on a touch of the floor. A level
            # hovering at the boundary (110 <-> 120 overnight on 07-23) would otherwise
            # re-arm on every wobble up and re-page on every wobble down.
            rearm = floor * float(getattr(config, "MARGIN_LEVEL_REARM_RATIO", 1.15) or 1.0)
            if lvl >= rearm:
                _ml_worst_bucket = None
            return lvl
        bucket = int(lvl // 5) * 5           # a new key every 5 points down
        # Only page on a WORSENING slide. Previously any bucket got its own throttle
        # window, so a level oscillating across a boundary (110 <-> 115 all night on
        # 2026-07-23) alternated two keys and paged at ~half the intended interval —
        # the same condition, twice as loud. Ratcheting on the worst bucket seen means
        # a wobble is silent and a genuine deterioration still pages immediately.
        if _ml_worst_bucket is not None and bucket >= _ml_worst_bucket:
            return lvl
        _ml_worst_bucket = bucket
        text = (f"margin level {lvl:.0f}% below alert floor {floor:.0f}% — "
                f"Kraken margin-calls at 80%, force-liquidates from 40%")
        _journal_safe(conn, "defense", "*", text)
        # Under the floor is a dashboard fact; at the seizure band it is a wake-the-
        # operator fact. Only the latter makes noise (config.MARGIN_LEVEL_LOUD_PCT).
        loud_pct = float(getattr(config, "MARGIN_LEVEL_LOUD_PCT", 0) or 0)
        alerter.fire_safety("margin-level", f"ml{bucket}", text,
                            loud=(loud_pct > 0 and lvl < loud_pct))
        return lvl
    except Exception:
        log.exception("margin-level watch failed (alert/display only — trade path unaffected)")
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
    # Effective T/P base is min(baseline, trough) — the trough ratchet keeps the
    # target reachable after a drawdown while the baseline stays the ledger's
    # profit yardstick (executor._check_take_profit).
    tptr = _mg("tp_trough")
    # The TARGET is not min(baseline,trough) * (1 + TP_PCT). It was computed that way here for
    # two weeks while the executor also applied the 07-27 baseline floor, and on
    # 2026-08-05 the two disagreed by 39% — the deck showed $208.54 against $223.30
    # of equity, reading as a flatten 6.6% overdue, while the executor's real target
    # was $289.83 and the book was 23% below it, holding. Call the executor's own
    # definition so a console number cannot mean something different from the rail.
    from . import executor as _ex          # local import, as elsewhere in this file
    tp_tgt = _ex.tp_target(tpb, tptr if tptr else tpb) if tpb else None
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
    # Kraken reports TWO balances and they are not the same money. `eb` is the
    # equivalent balance — every currency in the account, i.e. the figure the operator
    # sees on Kraken's balances page. `tb`/`e` is the TRADE balance: only what counts
    # as margin collateral, which is what sizing, the rails and the margin level must
    # use. Persisting only `e` meant the deck could never reconcile with Kraken: on
    # 2026-08-05 it read $173.90 against an account holding $225.30, a $51.47 gap the
    # console had no way to explain. Carry `eb` so the deck can name it.
    def _bal(k):
        try:
            v = float(balance[k]) if balance else None
            return round(v, 2) if v is not None else None
        except (TypeError, ValueError, KeyError):
            return None

    blob = {
        "equity": equity, "margin_used": margin_used, "free_margin": free_margin,
        "balance_total": _bal("eb"),          # all currencies — matches Kraken's UI
        "balance_trade": _bal("tb"),          # the margin-collateral subset
        "margin_level": round(lvl) if lvl else None,
        "capacity": appstate.exec.get("capacity"),
        "prices": prices, "chg": chg, "links": links,
        "mode": config.EXEC_MODE, "started": appstate.started_ts, "updated": _t.time(),
        "tp_baseline": round(tpb, 2) if tpb else None,
        "tp_trough": round(tptr, 2) if tptr else None,
        "tp_target": round(tp_tgt, 2) if tp_tgt else None,
        "fees_day": _mg("fees_day"), "fees_total": _mg("fees_total"),
        "fees_epoch": _mg("fees_epoch"),
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
        # Per-rail headroom + the clock on a standing block. The deck could always
        # have read rails_ok from appstate; it never did, and a bot frozen on the
        # kill switch for two boots looked perfectly healthy (2026-08-05).
        "rails": appstate.exec.get("rails_detail"),
        "rails_block_since": appstate.exec.get("rails_block_since"),
        # Growth gates (respend pacing / regime / stack) — the refusals that keep
        # the book from growing while the rails read CLEAR. Executor-computed;
        # this is transport, not derivation.
        "growth": appstate.exec.get("growth_detail"),
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


def _code_sha():
    """Short commit of the code actually running, or None. Best-effort and never
    raises — this is a log banner, not a rail."""
    try:
        import subprocess
        r = subprocess.run(["git", "-C", config.PROJECT_ROOT, "rev-parse", "--short", "HEAD"],
                           capture_output=True, timeout=3, text=True)
        return r.stdout.strip() or None if r.returncode == 0 else None
    except Exception:
        return None


def _run_banner():
    """Mark the start of a run, with the parameters that decide what it means.

    The log is append-only and NEVER rotated (operator's standing rule), so one
    file holds every restart. Without a boundary you cannot scope a grep to a run —
    counting 'PAPER FILL' across a restart silently mixes two different books, and
    with the code changing under it you cannot tell which build produced which
    lines. One WARNING line per boot buys both, and costs nothing between them."""
    bits = [f"exec={config.EXEC_MODE}", f"pid={os.getpid()}"]
    sha = _code_sha()
    if sha:
        bits.append(f"code={sha}")
    if config.EXEC_MODE != "live":
        # The SEEDED cash, not the config constant. PAPER_PORTFOLIO_USD seeds the
        # simulated book exactly ONCE (paper_broker line ~154: only when `cash` is
        # unset), so on every restart against an existing book the constant and the
        # actual money diverge — this banner announced "paper_equity=$1000" over a
        # $199.67 book on 2026-08-05. A boot line whose job is to identify the run
        # must not state a number the run does not have. Falls back to the constant
        # for a genuinely fresh book, and never blocks the banner on a DB read.
        seeded = None
        try:
            row = store.connect(config.DB_PATH).execute(
                "SELECT value FROM paper_state WHERE key='cash'").fetchone()
            seeded = float(row[0]) if row and row[0] is not None else None
        except Exception:
            seeded = None
        bits.append(f"paper_cash=${seeded:,.2f}" if seeded is not None
                    else f"paper_seed=${config.PAPER_PORTFOLIO_USD:g} (fresh book)")
    bits += [f"size_mult={config.SIZE_MULT:g}",
             f"rails={'on' if config.RAILS_ENABLED else 'OFF'}",
             f"db={config.DB_PATH}"]
    log.warning("═══ RUN START · deepfield v%s · %s", VERSION, " · ".join(bits))


def _startup(debug, announce=False):
    """Shared by run_live and run_once: warm backfill -> DB -> startup sweep."""
    from . import broker
    setup_logging(debug=debug)
    _run_banner()                          # first line of the run, before any work
    broker.setup_raw_log(config.LOG_DIR)   # RAW order req/resp -> its own audit file
    # Paper mode runs the FULL live code path against a simulated exchange. Attach it
    # before anything can place an order, so there is no window in which paper-mode
    # traffic could reach the real account.
    if config.EXEC_MODE == "paper":
        paper_broker.attach()
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
    if (config.EXEC_MODE == "live" or paper_broker.attached()) and ing.executor is not None:
        try:
            kr = broker.open_positions()
            # Scoped: this is compared against Kraken's OWN open positions, which
            # only ever reflect one book. Counting both ledgers manufactures a drift
            # warning out of nothing — and this line is a safety reading.
            ours = store.open_position_count(conn, mode=_book_mode(config.EXEC_MODE))
            # kr is None on an API failure — guard the len() so a transient blip in this
            # cosmetic log line can't raise and skip verify_open_stops() below (which has
            # its own None-handling and MUST run to re-place any missing/orphaned stops).
            # Kraken splits ONE order's fill across multiple position RECORDS
            # (venue partials — an ETH dust split held 3 records on 1 order,
            # 2026-07-30), so len(kr) counts records, not positions; the ledger
            # counts orders. Compare orders to orders, and name the record count
            # only when it differs, so this line stops reading as a mismatch.
            if kr is None:
                kr_txt = "unavailable"
            else:
                refs = {p.get("ordertxid") for p in kr.values()
                        if isinstance(p, dict) and p.get("ordertxid")} if isinstance(kr, dict) else set()
                n_orders = len(refs) if refs else len(kr)
                kr_txt = (str(n_orders) if n_orders == len(kr)
                          else f"{n_orders} ({len(kr)} records — venue fill splits)")
            log.info("startup position check: ledger open=%d · Kraken open positions=%s",
                     ours, kr_txt)
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
        # Pairs with the RUN START banner so a grep can bracket exactly one run.
        log.warning("═══ RUN END · pid=%s · clean shutdown", os.getpid())
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
