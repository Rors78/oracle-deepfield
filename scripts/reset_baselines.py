#!/usr/bin/env python3
"""Re-arm DeepField's equity yardsticks from live exchange truth.

For the situation of 2026-08-24: the operator wound the account down by hand
(prop-plan purchase, crypto moved off to Earn) while the bot was dark, so every
stored yardstick — peak_equity $202.71, tp_baseline $265.83, flows_eb_anchor
$174.43 — described an account that no longer exists. The kill switch latched
(correctly) and the flow reconciliation refused the shift (correctly, the gap
was real: crypto left as non-USD conversions its USD-only sum cannot see).

This script performs the manual reset the kill switch asks for, using the SAME
key set the bot's own T/P cycle settle writes (executor: tp_baseline, tp_trough,
tp_cycle_flows, tp_flatten_active, peak_equity) plus the flow-poll anchor pair
(app: flows_cursor, flows_eb_anchor) so the flow-mismatch alarm stops firing
about a window that has been reconciled by hand.

REFUSES to run while a deepfield process is live: the executor caches these
yardsticks in its poll loop and would race the writes. Stop the bot first
(tmux kill-session -t deepfield), run this, then relaunch.

Usage:  ./venv/bin/python scripts/reset_baselines.py         # dry-run (prints)
        ./venv/bin/python scripts/reset_baselines.py --apply
"""
import datetime
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from deepfield import broker, config, store  # noqa: E402

KEYS = ("peak_equity", "tp_baseline", "tp_trough", "tp_cycle_flows",
        "tp_flatten_active", "flows_cursor", "flows_eb_anchor")


def bot_running():
    out = subprocess.run(["pgrep", "-af", "python.*-m deepfield"],
                         capture_output=True, text=True).stdout
    return [l for l in out.splitlines() if "reset_baselines" not in l]


def main():
    apply = "--apply" in sys.argv
    live = bot_running()
    if live:
        print("REFUSED — deepfield is running; these keys race the executor's poll loop:")
        for l in live:
            print("   ", l)
        print("Stop it first:  tmux kill-session -t deepfield")
        return 2

    tb = broker.trade_balance_full()
    eb = float(tb["eb"]) if tb and tb.get("eb") not in (None, "") else None
    if eb is None:
        print("REFUSED — could not read live eb from Kraken; refusing to guess the anchor.")
        return 2
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()

    new = {
        "peak_equity": f"{eb:.4f}",       # kill switch re-latches upward from here
        "tp_baseline": f"{eb:.4f}",       # +TP_PCT measures trading profit from here
        "tp_trough": f"{eb:.4f}",
        "tp_cycle_flows": "0.0",
        "tp_flatten_active": "0",
        "flows_cursor": f"{now:.6f}",     # ledger walk starts after the hand-reconciled window
        "flows_eb_anchor": f"{eb:.4f}",   # flow gate's yardstick = live truth
    }

    conn = store.connect(config.DB_PATH)
    print(f"live eb ${eb:.4f} · db {config.DB_PATH}\n")
    print(f"{'key':20s} {'current':>14s}   {'new':>14s}")
    for k in KEYS:
        cur = store.meta_get(conn, k, None)
        print(f"{k:20s} {str(cur):>14s}   {new[k]:>14s}")
    if not apply:
        print("\ndry-run — nothing written. Re-run with --apply.")
        return 0
    for k in KEYS:
        store.meta_set(conn, k, new[k])
    conn.commit()
    print("\nAPPLIED. Relaunch the bot; peak/baseline now re-ratchet from live equity.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
