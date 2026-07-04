"""Live app wiring: warm backfill -> DB -> Ingest (startup sweep) -> dual WS ->
writer -> clock-close watchdog -> hourly reconciler -> UI (rich or --simple)
-> keys (q/p/f/a). SPEC §5/§8/§12.
"""
import asyncio
import logging

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
            if ex is None:
                equity = None
            elif mode == "live":
                equity = await asyncio.to_thread(broker.trade_balance)
                if equity:
                    ex._update_peak(equity)      # DB write, back on the loop
            else:
                equity = config.PAPER_PORTFOLIO_USD
            rails_ok, reason = ex.rails_ok(equity) if ex else (True, "")
            positions = [
                {"symbol": r[0], "entry": r[1], "stop": r[2], "volume": r[3],
                 "leverage": r[4], "margin": r[5], "mode": r[6]}
                for r in conn.execute(
                    "SELECT symbol,entry,stop,volume,leverage,margin,mode FROM orders "
                    "WHERE status='open' ORDER BY id DESC")
            ]
            appstate.exec = {
                "mode": mode, "equity": equity, "open_count": len(positions),
                "positions": positions, "rails_ok": rails_ok, "rails_reason": reason,
                "halt": os.path.exists(config.HALT_FILE), "updated": _t.time(),
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
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("exec state refresh failed (will retry)")
        await asyncio.sleep(interval)


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
    setup_logging(debug=debug)
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
    log.info("startup sweep complete: %d pairs, regime=%s",
             len(config.PAIRS), appstate.regime.label if appstate.regime else "?")
    return conn, appstate, ing


async def run_live(simple=False, debug=False):
    log.info("DEEPFIELD starting (simple=%s)", simple)
    conn, appstate, ing = _startup(debug, announce=not simple)
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

    keys = KeyController(asyncio.get_running_loop(), {
        b"q": on_quit, b"p": on_pause, b"f": on_force_reconcile, b"a": on_test_alert,
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
