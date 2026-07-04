"""M4 forced-drop drill. SPEC §13 M4.

Connect (2 conns, 45 subs total — see ws_client.py discrepancy note) -> collect
ticks from all 15 symbols -> force a TCP drop on both -> observe reconnect ->
resubscribe -> gap-heal RECON lines with Cloudflare-safe backoff timing -> a few
post-reconnect ticks -> stop. Everything logged.
"""
import sys
import time
import asyncio
import logging

from . import config
from . import store
from . import reconciler
from . import rest_client
from . import events
from .ws_client import WSClient


def _setup_console_log():
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(name)-14s %(levelname)-7s %(message)s",
                                     datefmt="%H:%M:%S"))
    root = logging.getLogger()
    root.handlers = [h]
    root.setLevel(logging.INFO)


async def run_drill(cap_secs=150, heal_n=3):
    _setup_console_log()
    log = logging.getLogger("deepfield.drill")
    symbols = [p["ws"] for p in config.PAIRS]
    heal_subset = symbols[:heal_n]  # keep the drill fast/legible; prod heals all 15

    async def gap_heal_cb(_syms):
        # drill heals a representative subset; production reconnect heals all 15
        # (identical code path). Open a THREAD-LOCAL connection inside the worker
        # thread — sqlite3 forbids sharing a connection across threads.
        def _heal():
            c = store.connect(config.DB_PATH)
            try:
                reconciler.gap_heal(c, heal_subset, rest_client.fetch_ohlc)
            finally:
                c.close()
        await asyncio.to_thread(_heal)

    # Two connections (Kraken v2 allows one ohlc interval per symbol per conn):
    # conn A = ticker + ohlc@1440 (30 subs), conn B = ohlc@10080 (15 subs).
    queue = asyncio.Queue()
    client_a = WSClient(symbols, queue,
                        subs=[{"channel": "ticker"}, {"channel": "ohlc", "interval": 1440}],
                        on_connect=gap_heal_cb, name="A(ticker+D)")
    client_b = WSClient(symbols, queue,
                        subs=[{"channel": "ohlc", "interval": 10080}],
                        on_connect=gap_heal_cb, name="B(W)")
    clients = [client_a, client_b]
    tasks = [asyncio.ensure_future(c.run()) for c in clients]

    seen = set()
    tick_total = candle_total = 0
    dropped = False
    reconnected = False
    post_reconnect_ticks = 0
    start = time.time()

    try:
        while time.time() - start < cap_secs:
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if isinstance(ev, events.Tick):
                tick_total += 1
                if ev.symbol not in seen:
                    seen.add(ev.symbol)
                    log.info("TICK first from %-9s last=%s  (%d/15 symbols)",
                             ev.symbol, ev.last, len(seen))
                if reconnected:
                    post_reconnect_ticks += 1
            elif isinstance(ev, events.CandleUpdate):
                candle_total += 1
            elif isinstance(ev, events.CandleClosed):
                log.info("CANDLE CLOSED %s/%s interval_begin=%d", ev.symbol, ev.interval, ev.interval_begin)
            elif isinstance(ev, events.LinkUp):
                if ev.reconnect_count >= 1:
                    reconnected = True
                    log.info(">>> RECONNECTED (LINK UP #%d) — resubscribed + gap-heal follows",
                             ev.reconnect_count)

            if len(seen) == 15 and not dropped:
                log.info(">>> all 15 symbols streaming (%d ticks) — FORCING DROP on both connections (TCP kill)", tick_total)
                dropped = True
                for c in clients:
                    await c.force_drop()

            if dropped and reconnected and post_reconnect_ticks >= 5:
                log.info(">>> post-reconnect stream healthy (%d ticks) — drill complete", post_reconnect_ticks)
                break
    finally:
        for c in clients:
            await c.stop()
        for t in tasks:
            try:
                await asyncio.wait_for(t, timeout=5)
            except (asyncio.CancelledError, Exception):
                # CancelledError is BaseException, not Exception — a client's
                # run() task may exit via its own internal self-cancellation
                # during forced shutdown; don't let that crash the drill.
                t.cancel()

    acks_ok = sum(c.acks_ok for c in clients)
    acks_fail = sum(c.acks_fail for c in clients)
    reconnects = sum(c.reconnect_count for c in clients)
    log.info("SUMMARY: symbols=%d/15 ticks=%d candle_updates=%d acks_ok=%d acks_fail=%d reconnects=%d",
             len(seen), tick_total, candle_total, acks_ok, acks_fail, reconnects)
    ok = (len(seen) == 15 and dropped and reconnected and acks_fail == 0)
    log.info("DRILL RESULT: %s", "✅ PASS" if ok else "❌ INCOMPLETE")
    return ok
