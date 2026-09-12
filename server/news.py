"""Headlines that move a coin's price, attached to the coin they are about.

The owner asked for news to be taken into account. What that can honestly mean here is narrow,
and saying so matters more than the feature: a headline is not a forecast, and this does not
pretend to read one. It collects what was published, works out which coin each item is ABOUT,
and hands that to the decision - which is the model's to make, with the chart in front of it.

Six public RSS feeds, no key, no account, nothing sent out but the request. Measured: all six
answer in under a second and carry 10-35 items each.

MATCHING IS THE HARD PART and it is deliberately strict. "Ripple effects across the market" is
not a story about XRP and "a solana of activity" is not about SOL. So a headline only attaches
to a coin when the coin's TICKER appears as a standalone word, or its full name does. Anything
looser turns the whole feed into noise attached to everything, which is worse than no feed:
a signal that fires on every coin cannot inform a choice between coins.
"""
from __future__ import annotations

import html
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

FEEDS = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("Decrypt", "https://decrypt.co/feed"),
    ("The Block", "https://www.theblock.co/rss.xml"),
    ("Bitcoin.com", "https://news.bitcoin.com/feed/"),
    ("CryptoSlate", "https://cryptoslate.com/feed/"),
]

# Full names for the coins whose ticker is too common a word to match on its own, and for the
# ones people write out rather than abbreviate. A ticker like "ARB", "APE", "SUI" or "OP" in a
# headline is far more often English than a coin.
NAMES = {
    "BTC": ["bitcoin"], "ETH": ["ethereum", "ether"], "SOL": ["solana"],
    "XRP": ["ripple", "xrp"], "BNB": ["binance coin", "bnb"], "ADA": ["cardano"],
    "DOGE": ["dogecoin"], "AVAX": ["avalanche"], "DOT": ["polkadot"], "LINK": ["chainlink"],
    "MATIC": ["polygon"], "POL": ["polygon"], "LTC": ["litecoin"], "BCH": ["bitcoin cash"],
    "TRX": ["tron"], "SHIB": ["shiba inu", "shiba"], "PEPE": ["pepe coin", "pepe"],
    "UNI": ["uniswap"], "AAVE": ["aave"], "ATOM": ["cosmos"], "XLM": ["stellar"],
    "NEAR": ["near protocol"], "ICP": ["internet computer"], "FIL": ["filecoin"],
    "ARB": ["arbitrum"], "OP": ["optimism"], "SUI": ["sui network", "sui blockchain"],
    "APT": ["aptos"], "TON": ["toncoin", "ton network"], "HBAR": ["hedera"],
    "INJ": ["injective"], "TIA": ["celestia"], "SEI": ["sei network"], "WLD": ["worldcoin"],
    "BONK": ["bonk"], "FLOKI": ["floki"], "KAS": ["kaspa"], "RNDR": ["render"],
    "IMX": ["immutable"], "GRT": ["the graph"], "ALGO": ["algorand"], "VET": ["vechain"],
    "ETC": ["ethereum classic"], "MNT": ["mantle"], "JUP": ["jupiter exchange"],
    "ENA": ["ethena"], "PENGU": ["pudgy penguins"], "TRUMP": ["trump coin"],
}
# A short safety net only. The real rule is CASE, below - chasing this list was a losing game:
# the first version had 21 entries and still matched "CPI data", "nearly double", "Core CPI",
# "the bill" and "shouldn't"; the second had 190 and still matched "based", "fight", "cap",
# "met" and "chip". There is no end to English words that are also tickers.
AMBIGUOUS = {"OP", "ID", "AI", "NOT", "NOW", "ACT"}


def _text(el, tag: str) -> str:
    node = el.find(tag)
    return html.unescape((node.text or "").strip()) if node is not None and node.text else ""


def fetch(timeout: float = 12.0, per_feed: int = 30) -> list[dict[str, Any]]:
    """Everything published recently, newest first. A feed that fails is skipped, not fatal."""
    items: list[dict[str, Any]] = []
    for name, url in FEEDS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 TGTrader/1.0"})
            raw = urllib.request.urlopen(req, timeout=timeout).read()
            root = ET.fromstring(raw)
        except Exception:
            continue                     # one dead feed must not cost the other five
        for it in root.iter("item"):
            title = _text(it, "title")
            if not title:
                continue
            items.append({
                "title": title[:220],
                "url": _text(it, "link")[:400],
                "source": name,
                "published": _text(it, "pubDate")[:40],
                "summary": re.sub(r"<[^>]+>", " ", _text(it, "description"))[:300],
            })
            if len(items) % per_feed == 0:
                break
    return items


# Coin names that are also ordinary English words. Capitalisation alone does not settle these,
# because English capitalises the first word of a sentence whatever it means: "Ripple effects
# across markets" and "Optimism grows among traders" both survived the case rule. For these -
# and only these - the text must ALSO carry a word that says it is about crypto.
SOFT_NAMES = {"ripple", "optimism", "stellar", "avalanche", "render", "the graph", "cosmos",
              "immutable", "polygon", "bonk", "sui network", "aptos", "sei network", "tron"}

# Deliberately narrow. "traders", "markets" and "price" are not on this list: they appear in
# every financial headline ever written and would let the whole problem back in.
CRYPTO_WORDS = ("token", "coin", "crypto", "blockchain", "ledger", "protocol", "defi", "wallet",
                "staking", "airdrop", "mainnet", "nft", "etf", "onchain", "on chain", "web3",
                "stablecoin", "validator", "testnet", "layer 2", "l2")


def _named(caps: str, low: str, name: str) -> bool:
    """Is this name written the way English writes a proper noun - and does it mean the coin?

    Two gates, and the second only for the names that are ordinary words. The trade-off is
    chosen on purpose: requiring crypto context loses some real stories (a genuine Ripple
    headline with no crypto word in it is missed) and that is the SAFER error. A false negative
    costs the bot one headline; a false positive attaches a market-wide story to one coin and
    can push a decision on it. Missing news is not the same kind of wrong as inventing it.
    """
    title = " ".join(w[:1].upper() + w[1:] for w in name.split())
    if not (f" {title} " in caps or f" {name.upper()} " in caps):
        return False
    if name.lower() in SOFT_NAMES:
        return any(w in low for w in CRYPTO_WORDS)
    return True


def coins_in(text: str, tickers: set[str]) -> list[str]:
    """Which of these coins is this headline actually about?

    Strict on purpose. A ticker only counts as a standalone word, ambiguous tickers only count
    through their full name, and a full name counts wherever it appears. The failure this
    avoids is a feed that attaches every story to every coin, which cannot inform a choice
    between coins and reads as insight.
    """
    low = " " + re.sub(r"[^a-z0-9$ ]+", " ", text.lower()) + " "
    # The case-sensitive view, for the ticker rule below.
    caps = " " + re.sub(r"[^A-Za-z0-9$ ]+", " ", text) + " "
    hit: list[str] = []
    for tk in tickers:
        base = tk.split("/")[0].upper()

        # A full NAME must be written AS A NAME - capitalised, the way English writes a proper
        # noun. "Names are unambiguous by construction" was wrong and the Windows session
        # caught it: `"OP": ["optimism"]`, `"RNDR": ["render"]`, `"XRP": ["ripple"]`,
        # `"XLM": ["stellar"]`, `"AVAX": ["avalanche"]`, `"GRT": ["the graph"]` are all
        # ordinary English words, and matching them case-insensitively let "ripple effects
        # across markets", "a stellar quarter" and "the graph shows" in through the back door -
        # past the very guard that two rounds of word-list whack-a-mole had been spent building.
        #
        # Same rule as the ticker, for the same reason: a headline that means the project writes
        # "Optimism", one that means the feeling writes "optimism", and case is the only thing
        # that separates them because the letters are identical.
        if any(_named(caps, low, n) for n in NAMES.get(base, [])):
            hit.append(tk)
            continue

        # A bare TICKER only counts when the headline wrote it as a ticker: ALL CAPS, or with a
        # dollar sign. This is what finally stopped the false matches, after two rounds of
        # word-list whack-a-mole. A headline that means the coin writes "BTC" or "$SOL"; one
        # that means the English word writes "based", "Fight", "cap" - and case is the only
        # thing that separates them, because the letters are identical.
        if base in AMBIGUOUS or len(base) < 3:
            continue
        if f" {base} " in caps or f" ${base} " in caps or f" ${base.lower()} " in low:
            hit.append(tk)
    return hit


def build(tickers: list[str], limit: int = 120) -> dict[str, Any]:
    """The feed, plus a per-coin index so the app does not have to scan every headline."""
    items = fetch()[:limit]
    universe = set(tickers)
    by_coin: dict[str, list[int]] = {}
    for i, it in enumerate(items):
        found = coins_in(it["title"] + " " + it["summary"], universe)
        it["coins"] = found
        for c in found:
            by_coin.setdefault(c, []).append(i)
    return {"items": items, "by_coin": by_coin, "at": time.time()}
