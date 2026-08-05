"""CLI entry: `python -m deepfield` (or ./deepfield). SPEC §12.

Default = live TUI. Flags/subcommands wired here; handlers land in later milestones.
"""
import argparse
import sys
import time

from . import VERSION


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="deepfield", description="ORACLE DEEPFIELD — signal-only Kraken bottom monitor")
    p.add_argument("--version", action="version", version=f"deepfield {VERSION}")
    p.add_argument("--simple", action="store_true", help="plaintext mode (no rich)")
    p.add_argument("--once", action="store_true", help="single confirmed eval + one plaintext frame")
    p.add_argument("--debug", action="store_true", help="verbose logging")
    p.add_argument("--test-alert", action="store_true", help="exercise the full alert chain (kind=test)")
    p.add_argument("--reconcile", action="store_true", help="run one reconcile pass and exit")
    p.add_argument("--backfill", action="store_true", help="cold/warm backfill pairs+candles and exit")
    p.add_argument("--full", action="store_true", help="with --backfill: force full (cold) fetch")
    p.add_argument("--test-drop", action="store_true", help="M4 drill: force a WS reconnect")
    p.add_argument("--exec-probe", action="store_true",
                   help="send validate=true orders for all pairs — prove the live order path without executing")
    p.add_argument("--exec-status", action="store_true", help="show execution config, rails, equity, recent orders")
    p.add_argument("--trim", metavar="SYMBOL",
                   help="operator deleverage: market-close a pair's open lots (e.g. NEAR/USD). "
                        "STOP THE LIVE BOT FIRST — one process per API key.")
    p.add_argument("--lots", type=int, default=None,
                   help="with --trim: shed at most N lots (largest notional first; default: the whole line)")
    p.add_argument("--audit-paper", action="store_true",
                   help="check the simulated exchange against the ledger (read-only) and exit")
    p.add_argument("--web", action="store_true", help="serve the read-only web console (localhost dashboard)")
    p.add_argument("--port", type=int, default=None,
                   help="web console port (with --web; default: DEEPFIELD_WEB_PORT or 8787)")
    sub = p.add_subparsers(dest="cmd")
    imp = sub.add_parser("import-legacy", help="seed the cooldown ledger from dca_log.csv")
    imp.add_argument("csv")
    exp = sub.add_parser("export-csv", help="export alerts ledger to CSV")
    exp.add_argument("path")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "import-legacy":
        from . import store, alerter, config
        conn = store.connect(config.DB_PATH)
        result = alerter.import_legacy_csv(conn, args.csv)
        conn.close()
        print(f"import-legacy: {result['imported']} rows imported, {result['skipped']} skipped")
        return 0
    if args.cmd == "export-csv":
        from . import store, alerter, config
        conn = store.connect(config.DB_PATH)
        n = alerter.export_alerts_csv(conn, args.path)
        conn.close()
        print(f"export-csv: {n} rows written to {args.path}")
        return 0
    if args.backfill:
        from . import backfill
        t0 = time.time()
        summary = backfill.run(full=args.full)
        print(f"\nbackfill done in {time.time()-t0:.1f}s — {len(summary)} pair/interval series")
        return 0
    if args.reconcile:
        from . import store, reconciler, rest_client, config
        conn = store.connect(config.DB_PATH)
        symbols = [p["ws"] for p in config.PAIRS]
        repairs = reconciler.gap_heal(conn, symbols, rest_client.fetch_ohlc)
        conn.close()
        print(f"reconcile pass complete: {repairs} repairs")
        return 0
    if args.test_drop:
        import asyncio
        from . import wsdrill
        ok = asyncio.run(wsdrill.run_drill())
        return 0 if ok else 1
    if args.test_alert:
        from . import store, alerter, config
        conn = store.connect(config.DB_PATH)
        result = alerter.test_alert(conn)
        conn.close()
        print(f"test-alert fired: sound={result['sound']} notify={result['notify']} telegram={result['telegram']}")
        return 0
    if args.exec_status:
        from . import store, broker, config, executor as ex
        conn = store.connect(config.DB_PATH)
        print(f"EXEC_MODE={config.EXEC_MODE}  RISK_PCT={config.RISK_PCT}  keys={'present' if broker.keys_present() else 'MISSING'}")
        e = ex.Executor(conn)
        equity = config.PAPER_PORTFOLIO_USD if config.EXEC_MODE != "live" else broker.trade_balance()
        ok, reason = e.rails_ok(equity)
        print(f"equity(for sizing)=${equity}  rails={'OK' if ok else 'BLOCKED: ' + reason}")
        print(f"open positions (this bot)={store.open_position_count(conn)}  HALT file={'YES' if __import__('os').path.exists(config.HALT_FILE) else 'no'}")
        print("recent orders:")
        for r in conn.execute("SELECT ts,symbol,mode,side,volume,leverage,entry,stop,status FROM orders ORDER BY id DESC LIMIT 8"):
            print("  ", r)
        conn.close()
        return 0
    if args.trim:
        import os
        from . import store, broker, config, executor as ex
        if os.popen("pgrep -f 'python -m deepfield$'").read().strip():
            print("REFUSING: the live bot is running. Kraken's nonce is per API key and the "
                  "rate counter is per ACCOUNT — a second process throttles the bot blind.\n"
                  "Stop it first:  tmux kill-session -t deepfield")
            return 1
        if config.EXEC_MODE != "live" or not broker.keys_present():
            print(f"REFUSING: EXEC_MODE={config.EXEC_MODE} keys={'present' if broker.keys_present() else 'MISSING'}")
            return 1
        sym = args.trim if "/" in args.trim else f"{args.trim.upper()}/USD"
        conn = store.connect(config.DB_PATH)
        res = ex.Executor(conn).trim_pair(sym, max_lots=args.lots)
        conn.close()
        print(f"trim {sym}: closed={res['closed']} failed={res['failed']} "
              f"bids_canceled={res['bids_canceled']}")
        if res["ml_before"] is not None and res["ml_after"] is not None:
            print(f"  margin level {res['ml_before']:.0f}% -> {res['ml_after']:.0f}%")
        return 1 if res["failed"] else 0
    if args.audit_paper:
        from . import paper_broker, config
        findings = paper_broker.audit()
        for name, ok, detail in findings:
            print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not ok else ""))
        bad = [f for f in findings if not f[1]]
        print(f"\n{config.DB_PATH}\n"
              + ("all invariants hold" if not bad else f"{len(bad)} INVARIANT(S) VIOLATED"))
        return 1 if bad else 0
    if args.web:
        from .web import server
        from . import config
        # honor DEEPFIELD_WEB_PORT (via config.WEB_PORT) when --port is unset, so the
        # standalone console and the in-process console resolve the same port.
        server.serve(port=args.port if args.port is not None else config.WEB_PORT)
        return 0
    if args.exec_probe:
        from . import app
        app.run_exec_probe()
        return 0
    if args.once:
        from . import app
        app.run_once(debug=args.debug)
        return 0
    # Default: live TUI (or --simple).
    import asyncio
    from . import app
    try:
        asyncio.run(app.run_live(simple=args.simple, debug=args.debug))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
