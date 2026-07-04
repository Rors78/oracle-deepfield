"""Live app wiring: warm backfill -> DB -> Ingest (startup sweep) -> dual WS ->
writer -> hourly reconciler -> UI (rich or --simple). Ties M1/M4/M5/M6 together
into the actual running application. SPEC §5/§12.
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
from .ws_client import WSClient
from .state import AppState
from .logsetup import setup_logging

log = logging.getLogger("deepfield.app")


def _make_ws_clients(symbols, queue, gap_heal_cb):
    """Two connections (§6 discrepancy, M4): Kraken v2 allows one ohlc interval
    per symbol per connection. A = ticker+ohlc@1440 (30 subs), B = ohlc@10080
    (15 subs) — 45 subscriptions total, just not on one socket."""
    client_a = WSClient(symbols, queue,
                        subs=[{"channel": "ticker"}, {"channel": "ohlc", "interval": 1440}],
                        on_connect=gap_heal_cb, name="A(ticker+D)")
    client_b = WSClient(symbols, queue,
                        subs=[{"channel": "ohlc", "interval": 10080}],
                        on_connect=gap_heal_cb, name="B(W)")
    return [client_a, client_b]


def _make_gap_heal_cb(symbols):
    async def gap_heal_cb(syms):
        def _heal():
            c = store.connect(config.DB_PATH)
            try:
                reconciler.gap_heal(c, syms, rest_client.fetch_ohlc)
            finally:
                c.close()
        await asyncio.to_thread(_heal)
    return gap_heal_cb


async def _hourly_reconciler(symbols):
    while True:
        await asyncio.sleep(3600)
        def _heal():
            c = store.connect(config.DB_PATH)
            try:
                reconciler.gap_heal(c, symbols, rest_client.fetch_ohlc)
            finally:
                c.close()
        await asyncio.to_thread(_heal)


def _startup(debug):
    """Shared by run_live and run_once: warm backfill -> DB -> startup sweep."""
    setup_logging(debug=debug)
    # log=print would flood stdout ahead of every --once/--simple frame; route
    # backfill's per-series lines through logging instead, matching everything else.
    backfill.run(full=False, log=logging.getLogger("deepfield.backfill").info)
    conn = store.connect(config.DB_PATH)
    appstate = AppState()
    ing = ingest_mod.Ingest(conn, appstate)
    ing.startup_sweep()
    log.info("startup sweep complete: %d pairs, regime=%s",
             len(config.PAIRS), appstate.regime.label if appstate.regime else "?")
    return conn, appstate, ing


async def run_live(simple=False, debug=False):
    log.info("DEEPFIELD starting (simple=%s)", simple)
    conn, appstate, ing = _startup(debug)
    symbols = [p["ws"] for p in config.PAIRS]
    queue = asyncio.Queue()
    gap_heal_cb = _make_gap_heal_cb(symbols)

    clients = _make_ws_clients(symbols, queue, gap_heal_cb)
    tasks = [asyncio.ensure_future(c.run()) for c in clients]
    tasks.append(asyncio.ensure_future(ing.run(queue)))
    tasks.append(asyncio.ensure_future(_hourly_reconciler(symbols)))
    tasks.append(asyncio.ensure_future(
        simple_ui.run_simple(appstate, conn) if simple else ui.run_ui(appstate, conn)
    ))
    await asyncio.gather(*tasks)


def run_once(debug=False):
    """--once: single confirmed evaluation + one plaintext frame (cron/tests)."""
    conn, appstate, ing = _startup(debug)
    print(simple_ui.render_frame_text(appstate, conn))
    conn.close()
