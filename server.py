#!/usr/bin/env python3
"""
CASCADE local data proxy — pure stdlib, no pip install required.

Serves the static terminal (index.html) AND a small JSON API that proxies two
free, no-key market-data sources so the browser can reach them without CORS or
API-key headaches:

  * Quotes   — Yahoo Finance "spark" batch endpoint (near-real-time last price
               + previous close for many symbols in one call).
  * History  — Yahoo Finance "chart" endpoint (daily bars, for the real
               sparkline on the selected instrument).
  * Correlation — pairwise Pearson correlation of daily returns, computed
               server-side from the same history endpoint above.
  * Lookup   — Yahoo Finance search/autocomplete endpoint. Resolves any
               ticker's name/sector/industry so the client can synthesize
               it into the model on the fly, beyond the curated universe.
  * Options  — CBOE delayed option chains (cdn.cboe.com). Full chain with
               volume / open interest / IV / greeks. ~15-min delayed. This is
               "most ACTIVE" (by traded volume), not "most PURCHASED" — free
               feeds do not classify buy vs sell trade side.
  * Snipe    — 0DTE deep-ITM screening board (SPY/QQQ/IWM) built on the same
               CBOE chain data: filters to same-day-expiry, in-the-money
               contracts with a live bid/ask and scores each one's spread,
               moneyness/delta tier, and a 1-contract execution model
               (breakeven, P&L at a few spot-move scenarios). Screening tool
               only — it never places or simulates placing an order.

Run:  python server.py                (defaults to port 8474)
      python server.py --port 9000

Then open http://localhost:8474  (the launch.json "cascade" config does this).

NOTE: data is delayed and for personal / educational use. Not investment advice.
"""

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def is_within_dir(base, target):
    """True if target is base itself or a path strictly inside it.

    A plain `target.startswith(base)` also matches sibling directories whose
    name happens to share base's prefix (e.g. base=".../cascade" would match
    ".../cascade_evil/secret.txt") since it never checks for a separator
    boundary. Compare against `base + os.sep` to close that gap.
    """
    return target == base or target.startswith(base + os.sep)


def has_hidden_component(base, target):
    """True if any path segment of `target`, relative to `base`, starts with a dot.

    Extracted out of Handler._send_file so it's a plain function testable
    without spinning up a real HTTP request -- BASE_DIR (a git checkout) also
    contains .git/, and other tooling drops dotfiles like .env next to
    server.py, none of which should be servable just because they happen to
    live under the static root.
    """
    rel = os.path.relpath(target, base)
    if rel == os.curdir:  # target IS base -- relpath returns "." here, not a real component
        return False
    return any(part.startswith(".") for part in rel.split(os.sep))


# Underlyings scanned for the "most active options" board. Kept to the genuinely
# option-liquid names (the real leaders) so a cold scan stays responsive.
OPTION_SCAN = [
    "SPY", "QQQ", "IWM", "NVDA", "TSLA", "AAPL", "AMZN", "MSFT", "META", "AMD",
    "GOOGL", "MU", "JPM", "BAC", "XOM", "UNH", "LLY", "BA", "WMT", "GE", "AVGO",
    "COIN", "PLTR", "SMCI",
]

# Underlyings scanned for the "Snipe" 0DTE deep-ITM board. Deliberately a
# small, dedicated list rather than reusing OPTION_SCAN: SPY/QQQ/IWM are the
# names that reliably list same-day expiries every trading day. SPX was
# evaluated and deliberately left out -- CBOE's delayed-quotes endpoint 403s
# on plain "SPX" and requires the undocumented "_SPX" alias to resolve, that
# alias reports its underlying as "^SPX" (not "SPX", which would need its own
# Yahoo-quote symbol mapping for spot price), and a live probe of that chain
# found zero same-day-expiry contracts despite being fetched on a Friday
# (SPX's own M/W/F 0DTE cadence), so plumbing it in couldn't even be verified
# end-to-end. Add it later if CBOE's index-option support turns out sturdier.
SNIPE_SCAN = ["SPY", "QQQ", "IWM"]

# Hard cap on how many symbols a single API request may fan out to, so a
# crafted ?symbols=... list can't trigger an unbounded burst of outbound
# requests to Yahoo / CBOE (resource-exhaustion / amplification guard).
MAX_API_SYMBOLS = 60

# ---------------------------------------------------------------------------
# tiny TTL cache
# ---------------------------------------------------------------------------
_cache = {}
_cache_lock = threading.Lock()


def cache_get(key):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
    return None


def cache_put(key, value, ttl):
    now = time.time()
    with _cache_lock:
        _cache[key] = (now + ttl, value)
        # Opportunistic purge: cache_get only checks expiry lazily on read,
        # so without this a long-running server accumulates every expired
        # entry forever (worst case: get_lookup caching every ticker a user
        # has ever searched). Piggyback the sweep on writes rather than
        # running a timer thread.
        expired = [k for k, (exp, _) in _cache.items() if exp <= now]
        for k in expired:
            del _cache[k]


def fetch(url, timeout=15):
    """Fetch raw bytes from a URL with a user agent header.

    Args:
        url (str): The URL to fetch.
        timeout (int): Request timeout in seconds. Defaults to 15.

    Returns:
        bytes: The raw response body.

    Raises:
        urllib.error.URLError: If the request fails.
    """
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# Yahoo spark throttles rapid successive calls from one IP (returns 400), so
# serialize all Yahoo requests behind a minimum inter-call gap, with one retry.
_yahoo_lock = threading.Lock()
_yahoo_last = [0.0]


def fetch_yahoo(url, timeout=15, gap=0.6):
    """Fetch from Yahoo Finance with rate-limiting and automatic retry.

    Yahoo throttles rapid requests with 400 errors, so this serializes calls
    with a minimum inter-call gap, measured from completion (not start).

    Args:
        url (str): The Yahoo Finance URL to fetch.
        timeout (int): Request timeout in seconds. Defaults to 15.
        gap (float): Minimum seconds between calls. Defaults to 0.6.

    Returns:
        bytes: The raw response body.

    Raises:
        urllib.error.HTTPError: If the request fails after retry.
    """
    # Serialize Yahoo calls with a real gap measured from the previous call's
    # COMPLETION (fetches take ~0.5s, so a start-time gap would never actually
    # space them out — back-to-back requests are what trip the 400).
    for attempt in (0, 1):
        with _yahoo_lock:
            wait = gap - (time.time() - _yahoo_last[0])
            if wait > 0:
                time.sleep(wait)
        try:
            data = fetch(url, timeout=timeout)
            with _yahoo_lock:
                _yahoo_last[0] = time.time()
            return data
        except urllib.error.HTTPError as e:
            with _yahoo_lock:
                _yahoo_last[0] = time.time()
            if e.code == 400 and attempt == 0:
                time.sleep(1.2)   # cool down, then retry once
                continue
            raise


# ---------------------------------------------------------------------------
# quotes  (Yahoo spark, batched)
# ---------------------------------------------------------------------------
def _spark_chunk(chunk):
    """Fetch one <=20-symbol spark batch (Yahoo 400s on larger batches)."""
    enc = ",".join(urllib.parse.quote(s) for s in chunk)
    url = (f"https://query1.finance.yahoo.com/v8/finance/spark?symbols={enc}"
           f"&range=1d&interval=1d")
    rows = {}
    data = json.loads(fetch_yahoo(url))
    for sym, d in data.items():
        if not isinstance(d, dict):
            continue
        closes = [c for c in (d.get("close") or []) if c is not None]
        prev = d.get("chartPreviousClose") or d.get("previousClose")
        price = closes[-1] if closes else None
        if price is None or not prev:
            continue
        rows[sym] = {"price": round(price, 4),
                     "prevClose": round(prev, 4),
                     "change": round(price / prev - 1, 6)}
    return rows


def get_quotes(symbols):
    """Fetch latest quotes for multiple symbols from Yahoo Finance.

    Args:
        symbols (list): List of stock symbols (e.g., ['AAPL', 'MSFT']).

    Returns:
        dict: Map {symbol: {price, prevClose, change}} or {_error: message} on failure.
    """
    symbols = symbols[:MAX_API_SYMBOLS]  # bound outbound fan-out per request
    # Sort for the cache key so ?symbols=AAPL,MSFT and ?symbols=MSFT,AAPL share
    # one entry (the result is a symbol-keyed map, so order is irrelevant).
    key = "q:" + ",".join(sorted(symbols))
    cached = cache_get(key)
    if cached is not None:
        return cached
    # Fetch chunks SEQUENTIALLY: Yahoo spark rate-limits concurrent bursts (a
    # parallel fan-out 400s even though each chunk is fine on its own).
    chunks = [symbols[i:i + 15] for i in range(0, len(symbols), 15)]
    out, errs = {}, []
    for c in chunks:
        try:
            out.update(_spark_chunk(c))
        except Exception as e:  # noqa: BLE001 — one bad chunk shouldn't sink the rest
            errs.append(str(e))
    if not out and errs:
        out["_error"] = errs[0]
    cache_put(key, out, ttl=20)
    return out


# ---------------------------------------------------------------------------
# history  (Yahoo chart, daily bars)
# ---------------------------------------------------------------------------
def get_history(symbol, rng="6mo"):
    """Fetch daily closing prices for a symbol over a time range.

    Args:
        symbol (str): Stock symbol.
        rng (str): Time range (e.g., '6mo', '1y', '5y'). Defaults to '6mo'.

    Returns:
        dict: {t: [timestamps], c: [closes]} or {_error: message} on failure.
    """
    key = f"h:{symbol}:{rng}"
    cached = cache_get(key)
    if cached is not None:
        return cached
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(symbol)}?range={rng}&interval=1d")
    out = {"t": [], "c": []}
    try:
        data = json.loads(fetch_yahoo(url))
        res = data["chart"]["result"][0]
        ts = res.get("timestamp") or []
        closes = res["indicators"]["quote"][0].get("close") or []
        for t, c in zip(ts, closes):
            if c is not None:
                out["t"].append(t)
                out["c"].append(round(c, 4))
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
        out["_error"] = f"Failed to parse history data: {e}"
    except (urllib.error.URLError, IOError) as e:
        out["_error"] = f"Network error fetching history: {e}"
    cache_put(key, out, ttl=900)
    return out


# ---------------------------------------------------------------------------
# correlation  (pairwise Pearson from Yahoo daily history, date-aligned)
# ---------------------------------------------------------------------------
def _returns_by_t(hist):
    """{'t':[...], 'c':[...]} -> {timestamp: daily return}, skipping gaps."""
    t, c = hist.get("t") or [], hist.get("c") or []
    return {t[k]: c[k] / c[k - 1] - 1 for k in range(1, len(t)) if c[k - 1]}


def _pearson(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / (sxx * syy) ** 0.5


def get_correlation(symbols, rng="6mo"):
    """Compute pairwise Pearson correlation of daily returns.

    Args:
        symbols (list): List of stock symbols to correlate.
        rng (str): Time range for historical data. Defaults to '6mo'.

    Returns:
        dict: {symbols, pairs: [[sym1, sym2, r], ...], missing, range, asOf}.
    """
    symbols = symbols[:MAX_API_SYMBOLS]  # bound history fan-out and O(n^2) pairing
    key = "corr:" + ",".join(sorted(symbols)) + f":{rng}"
    cached = cache_get(key)
    if cached is not None:
        return cached
    # Sequential, like get_quotes — reuses get_history's own Yahoo pacing
    # (fetch_yahoo) and its 900s cache, so a repeat sync within 15min of a
    # quote/chart fetch for the same symbol costs nothing extra.
    returns_by_symbol, missing = {}, []
    for sym in symbols:
        r = _returns_by_t(get_history(sym, rng))
        if len(r) < 20:
            missing.append(sym)
        returns_by_symbol[sym] = r
    # Pairwise date-intersection: a gappy or newly-listed symbol only
    # degrades its own edges, not the whole matrix's common window.
    pairs = []
    for i in range(len(symbols)):
        for j in range(i + 1, len(symbols)):
            si, sj = returns_by_symbol[symbols[i]], returns_by_symbol[symbols[j]]
            common = sorted(set(si) & set(sj))
            if len(common) < 20:
                continue
            r = _pearson([si[t] for t in common], [sj[t] for t in common])
            if r is not None:
                pairs.append([symbols[i], symbols[j], round(r, 4)])
    out = {"symbols": symbols, "pairs": pairs, "missing": missing,
           "range": rng, "asOf": int(time.time())}
    cache_put(key, out, ttl=900)
    return out


# ---------------------------------------------------------------------------
# lookup  (Yahoo's public search/autocomplete — no crumb/auth required, unlike
# the v7 quote and quoteSummary endpoints which now 401 without a session.
# Used by the client to resolve a ticker typed in search that isn't in the
# curated 38-name universe, so it can synthesize a new node for any real
# Nasdaq/NYSE symbol instead of being limited to the built-in list.)
# ---------------------------------------------------------------------------
def get_lookup(symbol):
    """Search for equity matches by partial symbol/name from Yahoo Finance.

    Args:
        symbol (str): Partial symbol or company name to search.

    Returns:
        dict: {symbol, matches: [{symbol, name, sector, industry, exchange}, ...]}
            or {_error: message}.
    """
    key = f"lu:{symbol.upper()}"
    cached = cache_get(key)
    if cached is not None:
        return cached
    url = (f"https://query1.finance.yahoo.com/v1/finance/search?q={urllib.parse.quote(symbol)}"
           f"&quotesCount=8&newsCount=0")
    out = {"symbol": symbol, "matches": []}
    try:
        data = json.loads(fetch_yahoo(url))
        for quote in data.get("quotes", []):
            if quote.get("quoteType") != "EQUITY":
                continue
            out["matches"].append({
                "symbol": quote.get("symbol"),
                "name": quote.get("longname") or quote.get("shortname"),
                "sector": quote.get("sector"),
                "industry": quote.get("industry"),
                "exchange": quote.get("exchDisp"),
            })
    except json.JSONDecodeError as e:
        out["_error"] = f"Failed to parse search results: {e}"
    except (urllib.error.URLError, IOError) as e:
        out["_error"] = f"Network error fetching lookup: {e}"
    cache_put(key, out, ttl=3600)
    return out


# ---------------------------------------------------------------------------
# options  (CBOE delayed chains -> most active by volume)
# ---------------------------------------------------------------------------
_OCC = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


def _parse_occ_symbol(occ_str):
    """Parse OCC symbol string into components.

    OCC format: ROOTYYMMDDCSTRIKE8 (e.g., AAPL240119C00150000)
    - ROOT: stock root symbol
    - YYMMDD: expiry date
    - C/P: call or put
    - STRIKE8: strike price * 1000 (8 digits)

    Args:
        occ_str (str): OCC option symbol.

    Returns:
        tuple: (root, expiry_date, call_or_put, strike) or None if invalid.
    """
    m = _OCC.match(occ_str)
    if not m:
        return None
    root, ymd, cp, strike8 = m.groups()
    try:
        exp = dt.datetime.strptime(ymd, "%y%m%d").date()
        strike = int(strike8) / 1000.0
        return (root, exp, cp, strike)
    except ValueError:
        return None


def _parse_chain(symbol, raw):
    """Return (contracts, call_vol, put_vol) for one underlying's CBOE chain."""
    contracts = []
    call_vol = put_vol = 0.0
    data = json.loads(raw)
    body = data.get("data") or {}
    today = dt.date.today()  # one call per chain, not once per contract (chains run 100s of rows)
    for opt in body.get("options", []):
        vol = opt.get("volume") or 0
        if not vol:
            continue
        parsed = _parse_occ_symbol(opt.get("option", ""))
        if not parsed:
            continue
        root, exp, cp, strike = parsed
        dte = (exp - today).days
        if cp == "C":
            call_vol += vol
        else:
            put_vol += vol
        contracts.append({
            "underlying": symbol,
            "type": cp,
            "strike": strike,
            "expiry": exp.isoformat(),
            "dte": dte,
            "volume": int(vol),
            "oi": int(opt.get("open_interest") or 0),
            "last": opt.get("last_trade_price"),
            "iv": round((opt.get("iv") or 0) * 100, 1),
            "bid": opt.get("bid"),
            "ask": opt.get("ask"),
            "delta": opt.get("delta"),  # signed: negative for puts, kept as-is (not abs'd)
            "theta": opt.get("theta"),
        })
    return contracts, call_vol, put_vol


def _cboe_chain_url(symbol):
    # Every other Yahoo-facing URL builder in this file quotes its symbol
    # (see _spark_chunk, get_history, get_lookup) — this one didn't, and
    # `symbol` is client-controlled via /api/options/active?symbols=....
    # safe="" also escapes "/", closing off path-segment injection
    # (e.g. symbol="../other/path") that the default quote() would allow.
    return (f"https://cdn.cboe.com/api/global/delayed_quotes/options/"
            f"{urllib.parse.quote(symbol, safe='')}.json")


def _fetch_chain(symbol):
    key = f"o:{symbol}"
    cached = cache_get(key)
    if cached is not None:
        return cached
    url = _cboe_chain_url(symbol)
    result = None
    try:
        raw = fetch(url, timeout=20)
        # capture the feed's own timestamp for the "as of" line
        ts = json.loads(raw).get("timestamp")
        contracts, call_vol, put_vol = _parse_chain(symbol, raw)
        result = {"contracts": contracts, "call_vol": call_vol, "put_vol": put_vol, "ts": ts}
    except (urllib.error.URLError, IOError, json.JSONDecodeError, KeyError, ValueError):
        # Dead symbol or parsing error shouldn't kill the board; return empty result
        result = {"contracts": [], "call_vol": 0.0, "put_vol": 0.0, "ts": None}
    cache_put(key, result, ttl=300)
    return result


def get_active_options(symbols, top=25):
    """Fetch most-active option contracts across underlyings (CBOE, ~15-min delayed).

    Args:
        symbols (list): List of stock symbols to scan. Defaults to OPTION_SCAN if empty.
        top (int): Number of top contracts to return, sorted by volume. Defaults to 25.

    Returns:
        dict: {as_of, delayed, market_pcr, total_call, total_put, underlyings, contracts, note}.
    """
    symbols = symbols[:MAX_API_SYMBOLS]  # bound outbound CBOE fan-out per request
    # A negative top is not "no limit" or an error -- Python's `list[:top]`
    # silently interprets it as "drop the last |top| items", which for
    # top=-1 returns nearly the whole board while the caller asked for "the
    # most active contracts" and got something unrelated to that count back.
    # Clamp to a real count so the contract is "at most `top` results," full stop.
    top = max(top, 0)
    key = "oa:" + ",".join(sorted(symbols)) + f":{top}"
    cached = cache_get(key)
    if cached is not None:
        return cached
    all_contracts = []
    underlying_stats = {}
    as_of = None
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        for sym, res in zip(symbols, executor.map(_fetch_chain, symbols)):
            all_contracts.extend(res["contracts"])
            tot = res["call_vol"] + res["put_vol"]
            if tot > 0:
                underlying_stats[sym] = {
                    "underlying": sym,
                    "callVol": int(res["call_vol"]),
                    "putVol": int(res["put_vol"]),
                    "totalVol": int(tot),
                    "pcr": round(res["put_vol"] / res["call_vol"], 2) if res["call_vol"] else None,
                }
            as_of = as_of or res["ts"]
    all_contracts.sort(key=lambda c: c["volume"], reverse=True)
    board = sorted(underlying_stats.values(), key=lambda u: u["totalVol"], reverse=True)
    total_call = sum(u["callVol"] for u in board)
    total_put = sum(u["putVol"] for u in board)
    out = {
        "as_of": as_of,
        "delayed": True,
        "note": "Most ACTIVE by traded volume (CBOE, ~15-min delayed). Not buy/sell classified.",
        "market_pcr": round(total_put / max(total_call, 0.0001), 2) if total_call else None,
        "total_call": total_call,
        "total_put": total_put,
        "underlyings": board,
        "contracts": all_contracts[:top],
    }
    cache_put(key, out, ttl=120)
    return out


def _itm_scan_tier(spread_pct, delta, moneyness_pct):
    """Classify one 0DTE deep-ITM contract into the Snipe board's A/B/C tier.

    C: spread_pct > 20% -- the bid/ask gap alone can eat the whole edge on a
       small test trade, regardless of how deep ITM the contract is.
    A: everything else that's either high-confidence by delta (>=0.85, i.e.
       priced by the market as very likely to expire ITM) OR far enough ITM
       by raw moneyness (>=0.8%) to use as a fallback probability proxy.
       The fallback exists because an earlier web-scrape prototype of this
       same strategy sometimes reported delta==0 on very-deep-ITM contracts
       (a site artifact, not a real 0 delta) -- CBOE's own feed shouldn't
       have that problem, but the belt-and-suspenders check costs nothing.
    B: everything remaining (tradeable spread, but neither signal clears
       the tier-A bar).
    """
    if spread_pct is None or spread_pct > 0.20:
        return "C"
    if abs(delta or 0) >= 0.85 or moneyness_pct >= 0.008:
        return "A"
    return "B"


def get_itm_scan(symbols=None, down_pct=-0.005, flat_pct=0.0, up_pct=0.005, top=20):
    """Scan SNIPE_SCAN underlyings for 0DTE deep-ITM "sniping" candidates.

    For each same-day-expiry, in-the-money contract with a real two-sided
    market, compute the CBOE-delayed screening metrics (spread, moneyness,
    tier) and a simple 1-contract execution model (entry at ask, breakeven,
    P&L at three spot-move scenarios). This is a screening/analysis tool --
    it never places or simulates placing an order.

    Args:
        symbols (list): Underlyings to scan. Defaults to SNIPE_SCAN.
        down_pct, flat_pct, up_pct (float): Spot-move scenarios to price P&L
            at, e.g. -0.005 = -0.5%. Exposed as params so the UI can later
            let a user tune them; defaults model a quiet 0DTE session.
        top (int): Max contracts to return across all symbols, ranked
            tier-A-first then by spread ascending. Defaults to 20.

    Returns:
        dict: {as_of, delayed, note, contracts}.
    """
    if symbols is None:
        symbols = SNIPE_SCAN
    symbols = symbols[:MAX_API_SYMBOLS]  # bound outbound fan-out per request
    top = max(top, 0)  # see get_active_options' identical guard: top=-1 must mean 0, not "all but last"
    key = ("itm:" + ",".join(sorted(symbols)) +
           f":{top}:{down_pct}:{flat_pct}:{up_pct}")
    cached = cache_get(key)
    if cached is not None:
        return cached

    quotes = get_quotes(symbols)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        chains = dict(zip(symbols, executor.map(_fetch_chain, symbols)))

    scenarios = (("down", down_pct), ("flat", flat_pct), ("up", up_pct))
    all_contracts = []
    as_of = None
    for sym in symbols:
        res = chains.get(sym) or {}
        as_of = as_of or res.get("ts")
        spot = (quotes.get(sym) or {}).get("price")
        if not spot:
            continue  # can't score moneyness/breakeven/P&L without a spot price
        for c in res.get("contracts", []):
            if c.get("dte") != 0:
                continue
            strike, typ = c.get("strike"), c.get("type")
            is_itm = (typ == "C" and strike < spot) or (typ == "P" and strike > spot)
            if not is_itm:
                continue
            bid, ask = c.get("bid"), c.get("ask")
            if bid is None or ask is None or bid <= 0 or ask <= 0:
                continue  # spread math needs a real two-sided market

            mid = (bid + ask) / 2
            spread_pct = (ask - bid) / mid if mid else None
            moneyness_pct = ((spot - strike) / spot if typ == "C"
                              else (strike - spot) / spot)
            delta = c.get("delta")
            tier = _itm_scan_tier(spread_pct, delta, moneyness_pct)

            contract_cost = ask * 100
            if typ == "C":
                breakeven = strike + ask
                breakeven_cushion_pct = (spot - breakeven) / spot
            else:
                breakeven = strike - ask
                breakeven_cushion_pct = (breakeven - spot) / spot

            scenario_out = {}
            for name, move_pct in scenarios:
                scenario_price = spot * (1 + move_pct)
                intrinsic = (max(0.0, scenario_price - strike) if typ == "C"
                             else max(0.0, strike - scenario_price))
                pnl_dollars = (intrinsic - ask) * 100
                scenario_out[name] = {
                    "move_pct": move_pct,
                    "scenario_price": round(scenario_price, 2),
                    "pnl_dollars": round(pnl_dollars, 2),
                    "pnl_pct": round(pnl_dollars / contract_cost, 4),
                }

            all_contracts.append({
                "underlying": c.get("underlying"),
                "type": typ,
                "strike": strike,
                "expiry": c.get("expiry"),
                "dte": c.get("dte"),
                "bid": bid,
                "ask": ask,
                "volume": c.get("volume"),
                "oi": c.get("oi"),
                "iv": c.get("iv"),
                "delta": delta,
                "spot": spot,
                "mid": round(mid, 4),
                "spread_pct": round(spread_pct, 4) if spread_pct is not None else None,
                "moneyness_pct": round(moneyness_pct, 4),
                "tier": tier,
                "contract_cost": round(contract_cost, 2),
                "max_loss": round(contract_cost, 2),  # 1 long contract: max loss == entry cost
                "breakeven": round(breakeven, 4),
                "breakeven_cushion_pct": round(breakeven_cushion_pct, 4),
                "scenarios": scenario_out,
            })

    # Tier A first, then tightest spread first within/across tiers.
    all_contracts.sort(key=lambda c: (c["tier"] != "A", c["spread_pct"]))
    out = {
        "as_of": as_of,
        "delayed": True,
        "note": ("CBOE delayed ~15min. Screening tool, not a trade recommendation. "
                 "No trades are placed automatically."),
        "contracts": all_contracts[:top],
    }
    cache_put(key, out, ttl=90)
    return out


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_json(self, obj, status=200):
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_file(self, path):
        full = os.path.normpath(os.path.join(BASE_DIR, path.lstrip("/")))
        # BASE_DIR is the project directory itself (server.py's own folder), which
        # for a git checkout also contains .git/ -- the containment check below
        # only proves a path stays under BASE_DIR, not that it's meant to be
        # public. Without this, GET /.git/config (or /.git/HEAD, /.env, etc.)
        # passes containment and isfile cleanly and would hand back the repo's
        # full history / any dotfile secrets over plain HTTP.
        if has_hidden_component(BASE_DIR, full):
            self.send_error(404)
            return
        if not is_within_dir(BASE_DIR, full) or not os.path.isfile(full):
            self.send_error(404)
            return
        ctype = ("text/html" if full.endswith(".html")
                 else "application/javascript" if full.endswith(".js")
                 else "text/css" if full.endswith(".css")
                 else "application/octet-stream")
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _qparam(query, name, default=""):
        """Return the first value of a urllib.parse.parse_qs() query param, or `default`.

        Every query param in this handler comes back from parse_qs() as a
        list (it supports repeated keys, which none of these endpoints use),
        so every call site was repeating the same `query.get(name, [default])[0]`
        shape. Centralizing it here means that shape is defined once instead
        of six times, and a future param that DOES need repeated-key handling
        has one obvious place to special-case.
        """
        return query.get(name, [default])[0]

    @staticmethod
    def _qsymbols(query, default=()):
        """Parse a comma-separated `symbols` query param into a clean list.

        Filters out empty entries so a trailing comma or an empty param
        value (?symbols=) doesn't produce a spurious "" ticker.
        """
        raw = Handler._qparam(query, "symbols")
        syms = [s for s in raw.split(",") if s]
        return syms if syms else list(default)

    def _api_quotes(self, query):
        syms = self._qsymbols(query)
        return get_quotes(syms) if syms else {}

    def _api_history(self, query):
        sym = self._qparam(query, "symbol")
        rng = self._qparam(query, "range", "6mo")
        return get_history(sym, rng) if sym else {"_error": "no symbol"}

    def _api_correlation(self, query):
        syms = self._qsymbols(query)
        rng = self._qparam(query, "range", "6mo")
        return (get_correlation(syms, rng) if len(syms) >= 2
                else {"_error": "need >=2 symbols"})

    def _api_lookup(self, query):
        sym = self._qparam(query, "symbol")
        return get_lookup(sym) if sym else {"_error": "no symbol"}

    def _api_options_active(self, query):
        syms = self._qsymbols(query, default=OPTION_SCAN)
        top = int(self._qparam(query, "top", "25"))
        return get_active_options(syms, top)

    def _api_options_itm_scan(self, query):
        syms = self._qsymbols(query, default=SNIPE_SCAN)
        top = int(self._qparam(query, "top", "20"))
        down = float(self._qparam(query, "down", "-0.005"))
        flat = float(self._qparam(query, "flat", "0.0"))
        up = float(self._qparam(query, "up", "0.005"))
        return get_itm_scan(syms, down, flat, up, top)

    # path -> (self, query) -> response-dict. Keeps do_GET itself pure routing:
    # "which endpoint is this" is separated from "how is its response computed",
    # so a new endpoint adds one table row and one small method instead of
    # another branch in a growing if/elif chain that used to mix both concerns.
    _API_ROUTES = {
        "/api/quotes": _api_quotes,
        "/api/history": _api_history,
        "/api/correlation": _api_correlation,
        "/api/lookup": _api_lookup,
        "/api/options/active": _api_options_active,
        "/api/options/itm-scan": _api_options_itm_scan,
    }

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed_url.query)
        try:
            handler = self._API_ROUTES.get(parsed_url.path)
            if handler is not None:
                self._send_json(handler(self, query))
            elif parsed_url.path in ("/", ""):
                self._send_file("index.html")
            else:
                self._send_file(parsed_url.path)
        except BrokenPipeError:
            pass
        except ValueError as e:
            self._send_json({"_error": f"Invalid parameter: {e}"}, status=400)
        except (FileNotFoundError, OSError) as e:
            self._send_json({"_error": f"File error: {e}"}, status=500)
        except Exception as e:
            # Catch remaining errors to prevent server crash; log to stderr
            print(f"[server error] Unhandled exception: {e}", file=sys.stderr)
            self._send_json({"_error": "Internal server error"}, status=500)

    def log_message(self, fmt, *args):  # quieter console
        # args[0] isn't always the request line — send_error() logs via
        # log_error("code %d, message %s", code, message), so args[0] can be
        # an int. A bare `"/api/" in args[0]` then raises TypeError, which
        # turns every 404 into a crashed, confusing 500.
        if args and isinstance(args[0], str) and "/api/" in args[0]:
            return
        super().log_message(fmt, *args)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8474)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    try:
        srv = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as e:
        # The common case here is "Address already in use" (another cascade
        # instance, or anything else, already holds the port) -- binding
        # happens in the constructor, so an unguarded call surfaces as a raw
        # traceback pointing at socket internals instead of telling the user
        # what to actually do about it.
        print(f"Could not start server on {args.host}:{args.port}: {e}", file=sys.stderr)
        print(f"  Is another instance already running? "
              f"Try: python server.py --port {args.port + 1}", file=sys.stderr)
        sys.exit(1)
    print(f"CASCADE proxy on http://{args.host}:{args.port}  (Ctrl+C to stop)")
    print(f"  static : {BASE_DIR}")
    print(f"  quotes : Yahoo spark   |  options : CBOE delayed  |  data is DELAYED")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
