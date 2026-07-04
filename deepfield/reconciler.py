"""Reconciler — diff, log loudly, then repair. SPEC §5, invariant 6.

Hourly and after every reconnect: REST-fetch last 10 candles per pair/interval,
diff against DB, log a RECON line with before/after for any mismatch, then repair.
Never silently fixes. Repair counts surface in the UI header.

TODO(M4/M5): reconcile loop + gap-heal hook + RECON logging.
"""
