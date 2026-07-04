"""rich Live TUI — AMOLED, flight-telemetry. SPEC §8, invariant 5.

Reads engine-published state ONLY; never re-implements a formula. Alternate screen,
render cap RENDER_HZ. Eight regions: header · countdowns · BTC pulse · regime ·
15-row main table · champion card · closest-not-yet · alert tail. Countdowns from
interval_begin arithmetic. Keys (termios cbreak, late M6): q/p/f/a.

TODO(M6): layout + Live loop + key reader.
"""
