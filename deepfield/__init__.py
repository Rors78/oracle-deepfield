"""ORACLE DEEPFIELD — always-on Kraken cycle-bottom monitor. Signal-only.

Weekly + daily 7-signal bottom detection over 15 Kraken USD spot pairs.
No order execution anywhere in this build (SPEC §1). It recommends; the
operator places.
"""

# F11 — single VERSION constant, used everywhere incl. the REST User-Agent.
VERSION = "6.0.0"
USER_AGENT = f"OracleDeepfield/{VERSION}"
