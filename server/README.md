# The server-side market sweep

Reading a chart costs one request per coin. The market is ~390 active USDT pairs on bybit and
~300 clear a small account's liquidity floor, so a full sweep is ~300 requests — about a minute
of solid traffic, every sweep, on a home connection that drops. That is why the app only ever
looked at the most liquid 40, which is ten percent of the market.

This box sweeps all of them every ten minutes and publishes one file. The app makes ONE request.

    feed.py      the sweep: tickers -> charts -> indicators -> which rules fired
    news.py      six public RSS feeds, matched to the coins they are actually about
    *.service    oneshot unit, MemoryMax=500M, CPUQuota=60%, Nice=15, IO idle
    *.timer      every 10 minutes

Published at `/var/lib/tgtrader-dl/market.json`, served by the existing `tgtrader-dl` service on
port 40002 — no new port, no firewall rule, and nothing in the Caddyfile touched.

Measured on this box: 304 coins in 63s, 7.3s of CPU, **112 MB peak** against the 500 MB cap,
162 KB published. It shares the machine with the live SMM panel, MariaDB, two Redis instances
and a VPN in 4 GB, which is what the caps are for.

**It never places an order and never sees an API key.** It publishes facts — prices,
indicators, which rules fired, headlines — and every decision stays on the owner's machine.

    systemctl status tgtrader-feed.timer
    journalctl -u tgtrader-feed.service -n 40
