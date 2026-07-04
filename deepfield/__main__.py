"""CLI entry: `python -m deepfield` (or ./deepfield). SPEC §12.

Default = live TUI. Flags/subcommands wired here; handlers land in later milestones.
"""
import argparse
import sys

from . import VERSION


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="deepfield", description="ORACLE DEEPFIELD — signal-only Kraken bottom monitor")
    p.add_argument("--version", action="version", version=f"deepfield {VERSION}")
    p.add_argument("--simple", action="store_true", help="plaintext mode (no rich)")
    p.add_argument("--once", action="store_true", help="single confirmed eval + one plaintext frame")
    p.add_argument("--debug", action="store_true", help="verbose logging")
    p.add_argument("--test-alert", action="store_true", help="exercise the full alert chain (kind=test)")
    p.add_argument("--reconcile", action="store_true", help="run one reconcile pass and exit")
    p.add_argument("--test-drop", action="store_true", help="M4 drill: force a WS reconnect")
    sub = p.add_subparsers(dest="cmd")
    imp = sub.add_parser("import-legacy", help="seed the cooldown ledger from dca_log.csv")
    imp.add_argument("csv")
    exp = sub.add_parser("export-csv", help="export alerts ledger to CSV")
    exp.add_argument("path")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    # TODO(M1+): dispatch to handlers as milestones land.
    print(f"deepfield {VERSION} — scaffold (M0). args={vars(args)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
