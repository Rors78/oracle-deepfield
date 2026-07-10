"""DEEPFIELD web console — a read-only live dashboard served from the DB.

The bot is the single writer; this package only READS (sqlite ro mode) the same
tables the TUI's exec snapshot reads, plus a small `web_live` meta blob the bot
persists for broker-only values (equity/margin/tick price). No trading, no Kraken
calls from here — so it can never collide with the live process's API nonce.
"""
