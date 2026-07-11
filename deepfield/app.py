"""Live app wiring: warm backfill -> DB -> Ingest (startup sweep) -> dual WS ->
writer -> clock-close watchdog -> hourly reconciler -> UI (rich or --simple)
-> keys (q/p/f/a). SPEC §5/§8/§12.
"""
import asyncio
import logging
import datetime
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


def _make_ws_clients(symbols, queue):
    """Two connections (§6 discrepancy, M4): Kraken v2 allows one ohlc interval
    per symbol per connection. A = ticker+ohlc@1440 (30 subs), B = ohlc@10080
    (15 subs) — 45 subscriptions total, just not on one socket."""
    client_a = WSClient(symbols, queue,
                        subs=[{"channel": "ticker"}, {"channel": "ohlc", "interval": 1440}],
                        on_connect=_make_gap_heal_cb((1440,)), name="A(ticker+D)")
    client_b = WSClient(symbols, queue,
                        subs=[{"channel": "ohlc", "interval": 10080}],
                        on_connect=_make_gap_heal_cb((10080,)), name="B(W)")
    return [client_a, client_b]


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
        e.poll_harvest_oco()   # FORK A: poll-cadence OCO — cancel the sibling the instant a stop/harvest fills
    except Exception:
        log.exception("poll_fills failed")
    finally:
        c.close()


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
            # live stop coverage: open rows carrying a resting stop txid (header
            # safety-reading number — tracks the live book, not the boot recon stamp)
            scov = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(CASE WHEN stop_txid IS NOT NULL "
                "AND stop_txid<>'' THEN 1 ELSE 0 END),0) FROM orders WHERE status='open'"
            ).fetchone()
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
                "capacity": _snapshot_capacity(conn, appstate, free_margin),
                "last_recon": store.meta_get(conn, "last_recon"),
                "stops_total": scov[0], "stops_covered": scov[1],
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
    blob = {
        "equity": equity, "margin_used": margin_used, "free_margin": free_margin,
        "margin_level": round(lvl) if lvl else None,
        "capacity": appstate.exec.get("capacity"),
        "prices": prices, "chg": chg, "links": links,
        "mode": config.EXEC_MODE, "started": appstate.started_ts, "updated": _t.time(),
    }
    store.meta_set(conn, "web_live", _json.dumps(blob))


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
            ing.executor._reconcile_harvests() # FORK A: place/retrofit target-sells (budgeted to live open vol)
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
    """--exec-probe: send validate=true orders for all 15 pairs against real
    Kraken — proves pair name, leverage, precision, and minimums are accepted
    WITHOUT executing. The proof gate before EXEC_MODE goes live."""
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
    print("VALIDATE PROBE — real Kraken order-check, nothing executes:\n")
    for p in config.PAIRS:
        sym = p["ws"]
        ps = appstate.pair(sym)
        card = ps.confirmed
        price = card.price if card else None
        if not price:
            print(f"  {sym:9s} skip (no price)")
            continue
        oid = ex.place_entry(sym, price, card)
        row = conn.execute("SELECT status, entry, stop, volume, leverage, error FROM orders WHERE id=?",
                           (oid,)).fetchone() if oid else None
        if row:
            st, entry, stop, vol, lev, err = row
            mark = "✅" if st == "validated" else "❌"
            print(f"  {sym:9s} {mark} {st:9s} vol={vol:g} x{lev} @ {entry} stop={stop}" + (f"  {err}" if err else ""))
        else:
            print(f"  {sym:9s} ❌ no order row")
    conn.close()
