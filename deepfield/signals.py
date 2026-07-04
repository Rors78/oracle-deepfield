"""The seven Oracle signals — pure functions, zero I/O. SPEC §7 + docs/RULINGS.md.

Signals (authoritative defs in RULINGS.md, which supersedes SPEC prose):
  sig1 Below W-EMA200
  sig2 W-RSI<40 Turning Up      WRSI[-1]<40 AND WRSI[-1]>WRSI[-4]
  sig3 W-MACD Hist Crossup      state: hist[-1]>0 AND any(h<0 in hist[-8:-1])
  sig4 D-RSI Divergence         find_bullish_divergence + F2 pivot quality
  sig5 W-First Up Close (F1)    close[-1]>close[-2] AND >=DOWN_WEEKS lower prior
  sig6 W-Vol Accumulation       vol[-1]>SMA20(vol) AND (green OR hammer)
  sig7 Near 52w Low <20%

Each fix gets a named regression test (test_F1_... etc). TODO(M2).
"""
