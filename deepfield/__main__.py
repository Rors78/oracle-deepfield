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
    sub = p.add_subparsers(dest="cmd")
    imp = sub.add_parser("import-legacy", help="seed the cooldown ledger from dca_log.csv")
    imp.add_argument("csv")
    exp = sub.add_parser("export-csv", help="export alerts ledger to CSV")
    exp.add_argument("path")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.backfill:
        from . import backfill
        t0 = time.time()
        summary = backfill.run(full=args.full)
        print(f"\nbackfill done in {time.time()-t0:.1f}s — {len(summary)} pair/interval series")
        return 0
    if args.test_drop:
        import asyncio
        from . import wsdrill
        ok = asyncio.run(wsdrill.run_drill())
        return 0 if ok else 1
    # TODO(M1+): dispatch remaining handlers as milestones land.
    print(f"deepfield {VERSION} — scaffold (M0). args={vars(args)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
