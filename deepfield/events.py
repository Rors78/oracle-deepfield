"""Typed event dataclasses flowing ws_client/rest_client -> writer. SPEC §5.

The stream is transport, not truth (invariant 3): these events are consumed by
the single writer task, which persists them; scores/UI derive from that state.
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class Tick:
    symbol: str            # v2 symbol, e.g. "BTC/USD"
    last: float
    bid: float
    ask: float
    high24: float
    low24: float
    volume24: float
    vwap24: float
    change_pct: float
    ts: float              # receive time (monotonic-ish wall clock)


@dataclass(frozen=True)
class CandleUpdate:
    symbol: str
    interval: int          # 1440 | 10080
    interval_begin: int    # canonical bar start (unix); `timestamp` field ignored
    o: float
    h: float
    l: float
    c: float
    v: float


@dataclass(frozen=True)
class CandleClosed:
    symbol: str
    interval: int
    interval_begin: int     # the bar that just closed


@dataclass(frozen=True)
class LinkUp:
    reconnect_count: int
    name: str = ""          # which connection (A/B) — link status is per-conn


@dataclass(frozen=True)
class LinkDown:
    reason: str
    name: str = ""


@dataclass(frozen=True)
class ReconRepair:
    symbol: str
    interval: int
    ts: int
    field: str
    before: float
    after: float
