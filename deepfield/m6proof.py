"""M6 proof driver: one live run against real Kraken.

Warm backfill -> startup sweep -> dual WS + ingest writer running for real ->
wait for all 15 symbols to tick -> export a genuinely-populated frame. Prints
its own PID first so an external `pidstat` can sample this exact process.
"""
import os
import sys
import time
import asyncio
import logging

from . import config
from . import store
from . import ui
from .app import _startup, _make_ws_clients, _make_gap_heal_cb
from .ingest import Ingest


async def main(run_secs=75):
    print(f"PID={os.getpid()}", flush=True)
    conn, appstate, ing = _startup(debug=False)
    logging.getLogger().setLevel(logging.WARNING)  # keep stdout clean for the proof

    symbols = [p["ws"] for p in config.PAIRS]
    queue = asyncio.Queue()
    clients = _make_ws_clients(symbols, queue, _make_gap_heal_cb(symbols))
    tasks = [asyncio.ensure_future(c.run()) for c in clients]
    tasks.append(asyncio.ensure_future(ing.run(queue)))

    start = time.time()
    seen = set()
    while time.time() - start < run_secs:
        await asyncio.sleep(0.5)
        seen = {s for s in symbols if appstate.pairs.get(s) and appstate.pairs[s].last_tick}
        if len(seen) == 15 and time.time() - start > 10:
            break  # got everything and let a couple more updates land

    print(f"symbols with live ticks: {len(seen)}/15 after {time.time()-start:.1f}s", flush=True)
    print("=" * 78, flush=True)
    print(ui.export_frame_text(appstate, conn, width=80), flush=True)
    print("=" * 78, flush=True)
    print("PROOF FRAME EXPORTED", flush=True)

    for t in tasks:
        t.cancel()
    conn.close()


if __name__ == "__main__":
    secs = int(sys.argv[1]) if len(sys.argv) > 1 else 75
    asyncio.run(main(secs))
