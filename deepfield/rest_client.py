"""Kraken REST — stdlib urllib inside asyncio.to_thread. SPEC §6, Appendix B.

Port the field-proven throttle/retry (Appendix B) VERBATIM, adapting names.
Endpoints: OHLC (cap 720, `time`=bar OPEN, freshness to bar CLOSE per F5) and
AssetPairs (wsname, ordermin, costmin, lot_decimals). User-Agent = USER_AGENT (F11).

TODO(M1): fetch_json (Appendix B), fetch_ohlc, fetch_assetpairs.
"""
