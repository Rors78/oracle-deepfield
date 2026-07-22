"""ORACLE DEEPFIELD — always-on Kraken cycle-bottom monitor and execution engine.

Weekly + daily 7-signal bottom detection over 29 Kraken margin pairs, scored on
closed daily/weekly candles (SIGNAL_INTERVALS) out of the 15m/1h/1d/1w streams
it ingests. Orders are LIVE leveraged margin orders on the `:BTNL` book when
DEEPFIELD_EXEC_MODE=live — post-only limit entries, with a real stop-loss
resting on the exchange behind every fill so the book stays protected even if
this process dies. EXEC_MODE is fail-closed: anything but an exact known mode
string becomes 'off'.

This supersedes SPEC §1's signal-only contract, which described the M0-M7 build
before execution landed. The scorecard is no longer the primary order trigger
either: continuous laddering and per-pair seeding place most orders without
consulting it, and the score's live role is conviction sizing (see the
SEED_PAIRS / LADDER_CONTINUOUS notes in config.py). Leverage is governed by
MARGIN_LEVEL_STACK_FLOOR_PCT, not by the size multiplier.
"""

# F11 — single VERSION constant, used everywhere incl. the REST User-Agent.
VERSION = "6.0.0"
USER_AGENT = f"OracleDeepfield/{VERSION}"
