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
  * Snipe Log — forward paper-trading track record for the Snipe board's
               top-scored pick each day: a daemon thread snapshots it ~30min
               before the close (snipe_log.json), settled against the
               underlying's real closing price the next time the log is read
               on a later day. Not a backtest (no historical intraday options
               data exists on this free feed) and never places a real trade
               — pure simulated bookkeeping for studying the strategy's
               honest, forward-only hit rate.
  * Risk flags — every ITM board penalizes and labels three signals the
               raw scores are blind to: IV rich relative to the underlying's
               REALIZED vol (not "IV Rank" — this feed has no IV history to
               rank against), an earnings report landing before expiry
               (Nasdaq's keyless calendar; Yahoo's earnings endpoints now
               401), and thin open interest / volume. Flags reduce the score
               and travel with it rather than filtering the contract out, so
               a flagged row stays visible with a legible reason.
  * Spreads  — vertical debit spreads (long ITM leg + short leg further
               out), as a separate board because a defined-risk/capped-gain
               payoff isn't rankable against a naked long's. Priced at the
               worst realistic fill (buy the ask, sell the bid), so net
               debit is an upper bound and max gain a lower bound.
  * Snipe Weekly — same idea as Snipe, but for ~7-day-out ITM contracts
               instead of same-day. A week-long hold has a different risk
               shape than 0DTE (real theta bleed, weekend gap risk, lower
               attainable delta), so it filters to a DTE window (not exactly
               7 — not every name lands a Friday expiry precisely there),
               derives each contract's own expected move from its IV
               (spot * iv * sqrt(dte/365)) instead of a fixed spot-move
               scenario, and scores on probability + profit magnitude +
               spread + theta exposure — the Snipe board's score explicitly
               excludes raw payoff by design, which is wrong for "biggest
               profit" questions. Same disclaimer: screening tool only,
               never places or simulates placing an order.

Run:  python server.py                (defaults to port 8474)
      python server.py --port 9000

Then open http://localhost:8474  (the launch.json "cascade" config does this).

NOTE: data is delayed and for personal / educational use. Not investment advice.
"""

import argparse
import concurrent.futures
import datetime as dt
import json
import math
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

# Default +/- tolerance, in calendar days, around a target DTE (see
# get_itm_scan_weekly / get_debit_spreads). 3, not 2, because [target-3,
# target+3] spans exactly 7 calendar days and weekly option expiries are 7
# days apart -- so any 7-day span contains exactly one Friday, which makes 3
# the smallest window that GUARANTEES a hit for names carrying only Friday
# weeklies. At 2 the window is [5,9] and misses them outright: SMCI, WMT and
# COIN were all verified to list expiries at 3/10/17 DTE with nothing in
# between, so a ~7-DTE scan of any of them returned zero candidates. Going
# wider than 3 buys no additional coverage on the names checked (4 caught
# nothing 3 didn't) and only mixes more disparate horizons into one board,
# so this stops at the principled minimum rather than padding for safety.
# SPY/QQQ/IWM list near-daily expiries and are unaffected either way.
_DEFAULT_DTE_WINDOW = 3

# Forward paper-trading log for the Snipe board's top-scored pick each day --
# see snapshot_snipe_pick()/resolve_snipe_log() below. Lives next to server.py
# (not under a data/ subdir) to match this project's flat, no-build-step layout.
SNIPE_LOG_PATH = os.path.join(BASE_DIR, "snipe_log.json")

# US Eastern time helpers, used to compute "30 minutes before the close"
# (15:30 America/New_York) for the Snipe Log's daily snapshot scheduler, and
# to map a closing-price timestamp back to the calendar trading day it
# belongs to.
#
# zoneinfo.ZoneInfo("America/New_York") would normally do this, but zoneinfo
# depends on an IANA tz database that it expects the OS to supply -- Windows
# doesn't ship one, so without the (non-stdlib) `tzdata` PyPI package as a
# fallback, zoneinfo raises ModuleNotFoundError on a stock Windows Python
# install (verified: that's exactly what happens on this project's own dev
# machine). Hand-rolling the US DST rule, which has been stable since 2007
# (2nd Sunday in March -> 1st Sunday in November, 2am local), keeps this
# genuinely dependency-free instead of quietly requiring `pip install
# tzdata` on the platform this app actually runs on.
def _nth_sunday(year, month, n):
    """The date of the n-th Sunday of `month`/`year` (n=1 -> first Sunday)."""
    first_of_month = dt.date(year, month, 1)
    first_sunday = first_of_month + dt.timedelta(days=(6 - first_of_month.weekday()) % 7)
    return first_sunday + dt.timedelta(weeks=n - 1)


def _ny_utcoffset_hours(date_obj):
    """US Eastern's UTC offset in hours (-4 EDT / -5 EST) for calendar
    `date_obj`, per the US DST rule in effect since 2007."""
    dst_start = _nth_sunday(date_obj.year, 3, 2)   # 2nd Sunday in March
    dst_end = _nth_sunday(date_obj.year, 11, 1)    # 1st Sunday in November
    return -4 if dst_start <= date_obj < dst_end else -5


def _now_et():
    """Current time as a tz-aware datetime in US Eastern (EDT/EST, DST-correct)."""
    now_utc = dt.datetime.now(dt.timezone.utc)
    offset = _ny_utcoffset_hours(now_utc.date())
    return now_utc.astimezone(dt.timezone(dt.timedelta(hours=offset)))


def _today_et():
    """Today's date in US Eastern -- the calendar the market actually runs on.

    Use this, never dt.date.today(), for anything that decides WHICH TRADING
    DAY something belongs to: contract DTE, a log entry's date, whether an
    entry is far enough in the past to settle. dt.date.today() is the
    machine's local date, and the two disagree for a window every night whose
    width is the offset between local time and ET -- so on a host outside
    Eastern, an option's DTE or a logged pick's date can silently land on the
    wrong trading day. That produces a subtly wrong track record rather than
    an error, which is the worst way for this to fail.
    """
    return _now_et().date()


def _et_date_from_ts(ts):
    """Unix timestamp -> the US-Eastern calendar date (YYYY-MM-DD) it falls
    on. Used to match a Yahoo daily-bar timestamp back to the trading day
    it represents."""
    utc_dt = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)
    offset = _ny_utcoffset_hours(utc_dt.date())
    return utc_dt.astimezone(dt.timezone(dt.timedelta(hours=offset))).date().isoformat()


# Hard cap on how many symbols a single API request may fan out to, so a
# crafted ?symbols=... list can't trigger an unbounded burst of outbound
# requests to Yahoo / CBOE (resource-exhaustion / amplification guard).
MAX_API_SYMBOLS = 60

# Same guard, for the OTHER axis a request can scale along. The DTE window
# feeds earnings_map(), which fetches one calendar day per day of horizon --
# so ?target_dte=100000 would otherwise attempt ~100k outbound requests to
# Nasdaq through a thread pool. These bound it at values well past any real
# screening horizon (CBOE chains run ~2y out, but nothing here is designed to
# scan more than a couple of months).
MAX_TARGET_DTE = 180
MAX_DTE_WINDOW = 30

# Separate, tighter cap on how far ahead the earnings calendar is consulted.
# Not just a cost guard: past ~6 weeks the flag stops carrying information,
# because essentially every contract that far out spans an earnings report,
# so "earnings before expiry" would fire on everything and mean nothing.
MAX_EARNINGS_LOOKAHEAD_DAYS = 45


def _clamp_int(value, lo, hi):
    return max(lo, min(hi, value))

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


def _parse_chain(symbol, raw, require_volume=True):
    """Return (contracts, call_vol, put_vol) for one underlying's CBOE chain.

    Args:
        require_volume (bool): drop contracts that haven't traded today.
            True for every trading board -- an untraded contract has no
            meaningful market to screen. False for open-interest analysis
            (see get_gamma_exposure), where untraded strikes are precisely
            the point: measured on SPY's live chain, zero-volume rows hold
            17% of all open interest, so filtering them would silently
            discard a sixth of the positioning being measured.
    """
    contracts = []
    call_vol = put_vol = 0.0
    data = json.loads(raw)
    body = data.get("data") or {}
    # ET, not local: dte decides which trading day a contract expires on.
    # One call per chain, not once per contract (chains run 100s of rows).
    today = _today_et()
    for opt in body.get("options", []):
        vol = opt.get("volume") or 0
        if require_volume and not vol:
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
            "gamma": opt.get("gamma"),
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


def _fetch_chain(symbol, require_volume=True):
    """Fetch and parse one underlying's chain, cached per (symbol, filter).

    The two variants are cached under different keys deliberately: the
    traded-only view is what every board wants and is far smaller, while the
    full view (untraded strikes included) is only needed for open-interest
    analysis. Sharing one cache entry would either bloat every board's
    working set or silently give the OI analysis a filtered chain.
    """
    key = f"o:{symbol}" if require_volume else f"oall:{symbol}"
    cached = cache_get(key)
    if cached is not None:
        return cached
    url = _cboe_chain_url(symbol)
    result = None
    try:
        raw = fetch(url, timeout=20)
        # capture the feed's own timestamp for the "as of" line
        ts = json.loads(raw).get("timestamp")
        contracts, call_vol, put_vol = _parse_chain(symbol, raw, require_volume)
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


# Snipe Score weights/ranges -- named here so they're one place to retune
# instead of magic numbers buried in the scoring formula.
_SCORE_PROB_WEIGHT = 0.40     # how much "will this finish ITM" (delta) counts
_SCORE_BANKED_WEIGHT = 0.35   # how much "already profitable if flat" counts
_SCORE_COST_WEIGHT = 0.25     # how much "tight spread" counts
_SCORE_DELTA_FLOOR = 0.70     # |delta| at/below this scores 0 on probability
_SCORE_DELTA_CEIL = 1.00      # |delta| at/above this scores 1 on probability
_SCORE_FLAT_PNL_FLOOR = -0.10  # flat P&L% at/below this scores 0 on "banked"
_SCORE_FLAT_PNL_CEIL = 0.05    # flat P&L% at/above this scores 1 on "banked"
_SCORE_SPREAD_CEIL = 0.20      # spread% at/above this scores 0 on cost


def _clamp01(x):
    return max(0.0, min(1.0, x))


def _itm_scan_score(delta, spread_pct, flat_pnl_pct):
    """0-100 "Snipe Score" -- ranks contracts by how likely they are to hand
    back a small, repeatable profit, NOT by raw dollar payoff or moneyness
    alone. The strategy's own goal is "high probability of cashing even a
    small profit," so the score is built from exactly those three signals:

      - probability (40%): |delta|, the market's own estimate of finishing
        ITM, scaled from 0.70 (score 0) to 1.00 (score 1). This is what makes
        wins repeatable instead of a coin flip.
      - banked profit (35%): the FLAT-scenario P&L% (spot doesn't move at
        all). This is the single best "cashing even a small profit" signal --
        it answers "am I already at or near breakeven without betting on any
        move," scaled from -10% (score 0) to +5% (score 1).
      - cost efficiency (25%): spread_pct, inverted and scaled 0%->1,
        20%->0. A wide bid/ask is paid TWICE (entry and exit) and can turn a
        contract that looks profitable on paper into a net loser in practice.

    Each sub-score is clamped to [0,1] before weighting, so one extreme input
    (e.g. a very negative flat P&L on a thin, expensive contract) can't drag
    the total negative or let a single factor swamp the other two.
    """
    prob = _clamp01((abs(delta or 0) - _SCORE_DELTA_FLOOR) /
                     (_SCORE_DELTA_CEIL - _SCORE_DELTA_FLOOR))
    banked = _clamp01((flat_pnl_pct - _SCORE_FLAT_PNL_FLOOR) /
                       (_SCORE_FLAT_PNL_CEIL - _SCORE_FLAT_PNL_FLOOR))
    cost = _clamp01(1 - (spread_pct if spread_pct is not None else 1.0) / _SCORE_SPREAD_CEIL)
    return round(100 * (_SCORE_PROB_WEIGHT * prob +
                         _SCORE_BANKED_WEIGHT * banked +
                         _SCORE_COST_WEIGHT * cost), 1)


# ---------------------------------------------------------------------------
# Risk flags -- shared by every ITM board.
#
# Each flag is a named, visible reason a contract is riskier than its raw
# score suggests. Flags PENALIZE the score and are returned alongside it
# rather than filtering the contract out: a flagged contract stays on the
# board where it can be seen and judged, and the reason it dropped is
# legible instead of the board silently going quiet. Same design as the
# existing A/B/C tier -- surface the judgement, don't hide the row.
# ---------------------------------------------------------------------------
_RISK_PENALTIES = {
    "IV_RICH": 12,     # option priced well above the underlying's realized vol
    "EARNINGS": 25,    # an earnings report lands before expiry (IV-crush risk)
    "THIN_OI": 10,     # too few open contracts to trust the quoted market
    "THIN_VOL": 8,     # barely traded today -- the quote may be stale
}

# IV/realized-vol ratio at/above which a contract is flagged IV_RICH. 1.0
# would flag nearly everything (index options almost always carry a variance
# risk premium -- implied normally sits above realized); 1.5 targets the
# genuinely expensive tail.
_IV_RICH_RATIO = 1.5
_MIN_OI = 50               # below this, THIN_OI
_MIN_VOLUME = 10           # below this, THIN_VOL
_REALIZED_VOL_DAYS = 30    # trailing sessions used for the realized-vol estimate


def realized_vol(symbol, lookback_days=_REALIZED_VOL_DAYS):
    """Annualized realized volatility (in percent, to match CBOE's IV units)
    from `lookback_days` of trailing daily closes, or None if there isn't
    enough history.

    Reuses get_history(symbol, "6mo") -- the exact call the correlation
    feature already makes and caches -- so this adds no extra network round
    trip in the common case.

    NOTE ON WHAT THIS IS NOT: this is realized (historical) vol, not an "IV
    Rank" or "IV percentile". Those require a history of *implied* vol, and
    the CBOE delayed-quotes feed gives only a current IV snapshot -- there is
    no IV history to rank against on this data source. Comparing today's IV
    to realized vol answers a real but different question ("is this option
    expensive relative to how much the underlying has actually been moving?"),
    and naming it accurately matters because IV Rank is what a trader would
    normally expect from a label like that.
    """
    hist = get_history(symbol, "6mo")
    closes = hist.get("c") or []
    window = closes[-(lookback_days + 1):]
    if len(window) < 3:  # need at least 2 returns for a standard deviation
        return None
    rets = [math.log(window[i] / window[i - 1])
            for i in range(1, len(window)) if window[i - 1] > 0 and window[i] > 0]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)  # sample variance
    return math.sqrt(var) * math.sqrt(252) * 100


def _nasdaq_earnings_for_date(date_iso):
    """{SYMBOL: date_iso} for every company reporting on one calendar date,
    from Nasdaq's public earnings calendar (no API key).

    Yahoo is not usable for this: both quoteSummary/calendarEvents and the
    v7 quote endpoint now 401 without crumb/cookie auth, while the v8 chart
    endpoint this app already uses carries no earnings field at all. Nasdaq's
    calendar is date-indexed rather than symbol-indexed, hence this shape.

    Never raises -- an unavailable calendar degrades to "no earnings known",
    which suppresses the flag rather than taking down the whole scan.
    """
    key = f"earn:{date_iso}"
    cached = cache_get(key)
    if cached is not None:
        return cached
    out = {}
    url = f"https://api.nasdaq.com/api/calendar/earnings?date={urllib.parse.quote(date_iso)}"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        for row in ((data.get("data") or {}).get("rows") or []):
            sym = (row.get("symbol") or "").strip().upper()
            if sym:
                out[sym] = date_iso
    except (urllib.error.URLError, IOError, json.JSONDecodeError,
            KeyError, TypeError, ValueError):
        pass  # see docstring: unknown, not fatal
    cache_put(key, out, ttl=43200)  # 12h -- an earnings calendar doesn't move intraday
    return out


def earnings_map(days_ahead):
    """{SYMBOL: 'YYYY-MM-DD'} of the soonest earnings date within the next
    `days_ahead` calendar days, built once per scan and shared by every
    symbol in it.

    Bounded deliberately: the calendar is queried per date, so the number of
    outbound requests equals the horizon being scanned (1 for a 0DTE board,
    <=11 for a ~7-DTE one), fetched in parallel and cached 12h. A per-symbol
    lookahead would instead multiply that by the size of the scan universe.

    `days_ahead` is clamped to MAX_EARNINGS_LOOKAHEAD_DAYS here as well as at
    the callers -- this function is the thing that actually turns a number
    into N outbound requests, so it owns the last word on that bound rather
    than trusting every present and future caller to have clamped first.
    """
    if days_ahead < 0:
        return {}
    days_ahead = min(days_ahead, MAX_EARNINGS_LOOKAHEAD_DAYS)
    today = _today_et()
    dates = [(today + dt.timedelta(days=i)).isoformat()
             for i in range(days_ahead + 1)]
    merged = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        for day in executor.map(_nasdaq_earnings_for_date, dates):
            for sym, date_iso in day.items():
                # dates are walked in ascending order, so the first hit wins
                merged.setdefault(sym, date_iso)
    return merged


def _earnings_flag(earnings_date_iso, expiry_iso):
    """EARNINGS flag when a report lands on or before the contract expires.

    The heaviest penalty on the board, and the one most worth surfacing: an
    earnings print inside the holding period changes the distribution
    entirely, and the IV crush immediately after it can lose money on a
    contract whose direction was right.
    """
    if not earnings_date_iso or not expiry_iso:
        return None
    try:
        earn = dt.date.fromisoformat(earnings_date_iso)
        expiry = dt.date.fromisoformat(expiry_iso)
    except (ValueError, TypeError):
        return None
    if earn > expiry:
        return None
    return {"code": "EARNINGS", "penalty": _RISK_PENALTIES["EARNINGS"],
            "label": f"earnings {earnings_date_iso} before expiry"}


def _liquidity_flags(volume, oi):
    """THIN_OI / THIN_VOL flags for one contract's traded interest.

    A tight bid/ask on a contract nobody holds or trades is not the same
    thing as a tight bid/ask on a liquid one -- the quote can be stale or
    unfillable at any real size, which the spread alone never reveals.
    """
    flags = []
    if oi is not None and oi < _MIN_OI:
        flags.append({"code": "THIN_OI", "penalty": _RISK_PENALTIES["THIN_OI"],
                      "label": f"thin open interest ({int(oi)})"})
    if volume is not None and volume < _MIN_VOLUME:
        flags.append({"code": "THIN_VOL", "penalty": _RISK_PENALTIES["THIN_VOL"],
                      "label": f"barely traded today ({int(volume)})"})
    return flags


def _iv_flag(iv, rvol):
    """IV_RICH flag when this contract's IV sits well above the underlying's
    realized vol. Returns (flag_or_None, iv_rv_ratio_or_None)."""
    if not iv or not rvol:
        return None, None
    ratio = iv / rvol
    if ratio < _IV_RICH_RATIO:
        return None, round(ratio, 2)
    return ({"code": "IV_RICH", "penalty": _RISK_PENALTIES["IV_RICH"],
             "label": f"IV {ratio:.1f}x realized vol"}, round(ratio, 2))


def _build_risk_flags(volume, oi, iv, rvol, earnings_date_iso, expiry_iso):
    """Every risk flag for one contract, in one place.

    The single-leg scan core and the spreads board both need the same three
    checks, and previously each assembled them by hand -- so a new flag type
    meant editing two call sites and a third board would have made three.
    They differ only in what they feed in (spreads pass the WEAKER leg's
    liquidity and the LONG leg's IV), which stays the caller's decision.

    Returns:
        (flags, iv_rv_ratio): the ratio comes back even when it doesn't
        trip the flag, since the boards display it either way.
    """
    flags = _liquidity_flags(volume, oi)
    iv_flag, iv_rv_ratio = _iv_flag(iv, rvol)
    if iv_flag:
        flags.append(iv_flag)
    earnings_flag = _earnings_flag(earnings_date_iso, expiry_iso)
    if earnings_flag:
        flags.append(earnings_flag)
    return flags, iv_rv_ratio


def _apply_risk_flags(raw_score, flags):
    """Subtract every flag's penalty from `raw_score`, floored at 0.

    Returned alongside the raw score (never in place of it) so the board can
    show both what the contract scored on its own merits and what the risk
    signals took off -- a penalty you can't see is indistinguishable from a
    scoring bug.
    """
    return round(max(0.0, raw_score - sum(f["penalty"] for f in flags)), 1)


def _fetch_chains_and_quotes(symbols):
    """The single network fan-out every options board starts from: spot
    quotes for all symbols, plus each symbol's full CBOE chain in parallel.

    Shared by the ITM scan core and the debit-spread board -- the latter
    can't reuse _scan_itm_candidates directly because it needs the OTM side
    of the chain too (that's where its short legs come from).
    """
    quotes = get_quotes(symbols)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        chains = dict(zip(symbols, executor.map(_fetch_chain, symbols)))
    return quotes, chains


def _scan_itm_candidates(symbols, dte_lo, dte_hi):
    """Shared scan core behind the single-leg ITM boards (0DTE Snipe, Snipe
    Weekly).

    Does the one network fan-out (quotes + full chains, in parallel), filters
    to in-the-money contracts whose DTE falls in [dte_lo, dte_hi] and which
    have a real two-sided market, and computes the per-contract math that is
    genuinely identical across boards: mid/spread, moneyness, intrinsic and
    extrinsic value, 1-contract cost, breakeven and its cushion.

    Also attaches the shared risk flags (IV richness vs realized vol, thin
    open interest, thin volume, earnings before expiry) so every board
    penalizes the same signals identically instead of each one re-deriving
    them. Boards apply the penalty to their own raw score via
    _apply_risk_flags.

    Deliberately stops short of scoring and scenarios -- those are where the
    boards actually differ (the 0DTE score rewards banked flat-scenario
    profit; the weekly score rewards profit magnitude at an IV-implied move),
    and collapsing them into one formula would silently re-tune both boards'
    calibration. This helper exists to kill the duplicated *plumbing*, not to
    pretend the strategies are the same.

    Args:
        symbols (list): Underlyings to scan (already caller-clamped).
        dte_lo, dte_hi (int): Inclusive DTE window. Pass 0, 0 for 0DTE.

    Returns:
        (as_of, candidates): `candidates` is a list of (raw_contract, spot,
        base) triples, where `base` holds the shared metrics above.
    """
    quotes, chains = _fetch_chains_and_quotes(symbols)
    # Once per scan, not once per symbol -- see earnings_map's docstring.
    earnings = earnings_map(dte_hi)

    candidates = []
    as_of = None
    for sym in symbols:
        res = chains.get(sym) or {}
        as_of = as_of or res.get("ts")
        spot = (quotes.get(sym) or {}).get("price")
        if not spot:
            continue  # can't score moneyness/breakeven/P&L without a spot price
        # Per-symbol, not per-contract: a chain runs to hundreds of rows and
        # these depend only on the underlying.
        rvol = realized_vol(sym)
        earnings_date_iso = earnings.get(sym.upper())
        for c in res.get("contracts", []):
            dte = c.get("dte")
            if dte is None or not (dte_lo <= dte <= dte_hi):
                continue
            strike, typ = c.get("strike"), c.get("type")
            is_itm = (typ == "C" and strike < spot) or (typ == "P" and strike > spot)
            if not is_itm:
                continue
            bid, ask = c.get("bid"), c.get("ask")
            if bid is None or ask is None or bid <= 0 or ask <= 0:
                continue  # spread math needs a real two-sided market

            mid = (bid + ask) / 2
            intrinsic = max(0.0, spot - strike) if typ == "C" else max(0.0, strike - spot)
            if typ == "C":
                breakeven = strike + ask
                breakeven_cushion_pct = (spot - breakeven) / spot
            else:
                breakeven = strike - ask
                breakeven_cushion_pct = (breakeven - spot) / spot

            flags, iv_rv_ratio = _build_risk_flags(
                c.get("volume"), c.get("oi"), c.get("iv"), rvol,
                earnings_date_iso, c.get("expiry"))

            candidates.append((c, spot, {
                "mid": mid,
                "spread_pct": (ask - bid) / mid if mid else None,
                "moneyness_pct": ((spot - strike) / spot if typ == "C"
                                   else (strike - spot) / spot),
                "intrinsic": intrinsic,
                "extrinsic": max(0.0, ask - intrinsic),
                "extrinsic_ratio": (max(0.0, ask - intrinsic) / ask) if ask else None,
                "contract_cost": ask * 100,
                "breakeven": breakeven,
                "breakeven_cushion_pct": breakeven_cushion_pct,
                "realized_vol": round(rvol, 2) if rvol else None,
                "iv_rv_ratio": iv_rv_ratio,
                "earnings_date": earnings_date_iso,
                "flags": flags,
            }))
    return as_of, candidates


def get_itm_scan(symbols=None, down_pct=-0.005, flat_pct=0.0, up_pct=0.005, top=20):
    """Scan SNIPE_SCAN underlyings for 0DTE deep-ITM "sniping" candidates.

    For each same-day-expiry, in-the-money contract with a real two-sided
    market, compute the CBOE-delayed screening metrics (spread, moneyness,
    tier) and a simple 1-contract execution model (entry at ask, breakeven,
    P&L at three spot-move scenarios). This is a screening/analysis tool --
    it never places or simulates placing an order.

    Shares its fetch/filter/execution-model plumbing with the weekly board
    via _scan_itm_candidates(); keeps its own scoring, which deliberately
    rewards "already profitable if the underlying doesn't move" rather than
    raw payoff (see _itm_scan_score).

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

    scenarios = (("down", down_pct), ("flat", flat_pct), ("up", up_pct))
    as_of, candidates = _scan_itm_candidates(symbols, 0, 0)
    all_contracts = []
    for c, spot, base in candidates:
        strike, typ = c.get("strike"), c.get("type")
        ask = c.get("ask")
        delta = c.get("delta")
        spread_pct = base["spread_pct"]
        contract_cost = base["contract_cost"]
        tier = _itm_scan_tier(spread_pct, delta, base["moneyness_pct"])

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

        raw_score = _itm_scan_score(delta, spread_pct, scenario_out["flat"]["pnl_pct"])
        score = _apply_risk_flags(raw_score, base["flags"])

        all_contracts.append({
            "underlying": c.get("underlying"),
            "type": typ,
            "strike": strike,
            "expiry": c.get("expiry"),
            "dte": c.get("dte"),
            "bid": c.get("bid"),
            "ask": ask,
            "volume": c.get("volume"),
            "oi": c.get("oi"),
            "iv": c.get("iv"),
            "delta": delta,
            "spot": spot,
            "mid": round(base["mid"], 4),
            "spread_pct": round(spread_pct, 4) if spread_pct is not None else None,
            "moneyness_pct": round(base["moneyness_pct"], 4),
            "tier": tier,
            "score": score,
            "raw_score": raw_score,  # before risk-flag penalties, so both are visible
            "flags": base["flags"],
            "realized_vol": base["realized_vol"],
            "iv_rv_ratio": base["iv_rv_ratio"],
            "earnings_date": base["earnings_date"],
            "contract_cost": round(contract_cost, 2),
            "max_loss": round(contract_cost, 2),  # 1 long contract: max loss == entry cost
            "breakeven": round(base["breakeven"], 4),
            "breakeven_cushion_pct": round(base["breakeven_cushion_pct"], 4),
            "scenarios": scenario_out,
        })

    # Ranked by Snipe Score (highest first) -- see _itm_scan_score for what
    # that rewards: high probability of finishing ITM, already profitable
    # (or close to it) if the underlying just sits still, and a spread tight
    # enough not to eat the edge on entry+exit. This replaces a plain
    # "tier then spread" sort with something that actually targets the
    # strategy's goal (small, repeatable wins) rather than just liquidity.
    all_contracts.sort(key=lambda c: c["score"], reverse=True)
    out = {
        "as_of": as_of,
        "delayed": True,
        "note": ("CBOE delayed ~15min. Ranked by Snipe Score (probability + "
                 "banked flat-scenario profit + spread cost). Screening tool, "
                 "not a trade recommendation. No trades are placed automatically."),
        "contracts": all_contracts[:top],
    }
    cache_put(key, out, ttl=90)
    return out


# Snipe Weekly Score weights/ranges -- named here for the same reason as the
# 0DTE _SCORE_* constants above: one place to retune instead of magic
# numbers in the formula. Deliberately a DIFFERENT set of weights/floors
# from the 0DTE Snipe Score, not a parameterization of it -- a week-long
# hold has a different risk shape (real theta bleed, weekend gap risk) and
# a different goal (biggest attainable profit, not "small and repeatable").
_WEEKLY_PROB_WEIGHT = 0.30     # |delta| -- probability of finishing ITM
_WEEKLY_MAGNITUDE_WEIGHT = 0.40  # P&L% at the contract's own IV-implied move
_WEEKLY_SPREAD_WEIGHT = 0.15   # same spread-cost signal as the 0DTE score
_WEEKLY_THETA_WEIGHT = 0.15    # extrinsic (time) value as a fraction of price
_WEEKLY_DELTA_FLOOR = 0.55     # |delta| at/below this scores 0 on probability
_WEEKLY_DELTA_CEIL = 0.90      # |delta| at/above this scores 1 on probability
_WEEKLY_MAGNITUDE_CEIL = 1.50  # P&L% at/above this (150%) scores 1 on magnitude
_WEEKLY_SPREAD_CEIL = 0.20     # spread% at/above this scores 0 on spread cost
_WEEKLY_EXTRINSIC_CEIL = 0.50  # extrinsic-ratio at/above this scores 0 on theta


def _weekly_itm_scan_score(delta, spread_pct, magnitude_pnl_pct, extrinsic_ratio):
    """0-100 "Weekly Score" -- ranks ~7-DTE ITM contracts by probability of
    finishing ITM blended with the SIZE of the profit if the contract's own
    IV-implied move actually happens, unlike the 0DTE Snipe Score (which
    explicitly excludes raw payoff by design -- see _itm_scan_score). Four
    signals, each clamped to [0,1] before weighting so one extreme input
    can't swamp the other three:

      - probability (30%): |delta|, scaled 0.55 (score 0) to 0.90 (score 1).
        Lower floor/ceiling than the 0DTE score's 0.70/1.00 -- a contract a
        full week from expiry rarely trades at 0.85+ delta even when solidly
        ITM today.
      - profit magnitude (40%): P&L% if the underlying moves, by expiry, the
        amount this contract's own IV implies (see expected_move_pct in
        get_itm_scan_weekly) -- scaled 0% (score 0) to 150% (score 1). This
        is the "biggest profit" signal the 0DTE score deliberately omits.
      - spread cost (15%): identical formula to the 0DTE score.
      - theta/extrinsic exposure (15%): extrinsic_ratio (time value as a
        fraction of the ask) inverted and scaled 0%->1, 50%->0. A week-long
        hold actually bleeds theta if the move doesn't happen, unlike 0DTE
        where it barely matters intraday -- a contract that's mostly time
        premium is a worse bet here even at the same delta and spread.
    """
    prob = _clamp01((abs(delta or 0) - _WEEKLY_DELTA_FLOOR) /
                     (_WEEKLY_DELTA_CEIL - _WEEKLY_DELTA_FLOOR))
    magnitude = _clamp01(magnitude_pnl_pct / _WEEKLY_MAGNITUDE_CEIL)
    spread = _clamp01(1 - (spread_pct if spread_pct is not None else 1.0) / _WEEKLY_SPREAD_CEIL)
    theta = _clamp01(1 - (extrinsic_ratio if extrinsic_ratio is not None else 1.0)
                      / _WEEKLY_EXTRINSIC_CEIL)
    return round(100 * (_WEEKLY_PROB_WEIGHT * prob +
                         _WEEKLY_MAGNITUDE_WEIGHT * magnitude +
                         _WEEKLY_SPREAD_WEIGHT * spread +
                         _WEEKLY_THETA_WEIGHT * theta), 1)


def get_itm_scan_weekly(symbols=None, target_dte=7, window=_DEFAULT_DTE_WINDOW, top=20):
    """Scan SNIPE_SCAN underlyings for ~week-out deep-ITM candidates ranked
    by probability blended with profit magnitude, not the 0DTE board's
    "small and repeatable" goal -- see _weekly_itm_scan_score.

    Sibling to get_itm_scan(), reusing the same chain-fetch/quote-fetch
    plumbing and per-contract execution-model shape (breakeven,
    contract_cost, max_loss) so the two boards' contract dicts stay
    interchangeable in the UI. Diverges where the strategy actually
    differs: the DTE filter is a window around target_dte (not `== 0`,
    since not every name lands an expiry on exactly day 7), and the
    "scenarios" moves are derived per-contract from its own IV instead of
    one fixed spot-move percentage for every contract -- a week is long
    enough that a flat, near-zero IV name and a wild, high-IV name
    shouldn't be priced against the same fixed bump.

    Args:
        symbols (list): Underlyings to scan. Defaults to SNIPE_SCAN.
        target_dte (int): Center of the DTE window, in calendar days.
        window (int): Contracts with dte in [target_dte-window,
            target_dte+window] are included.
        top (int): Max contracts to return across all symbols, ranked by
            Weekly Score descending. Defaults to 20.

    Returns:
        dict: {as_of, delayed, note, contracts}.
    """
    if symbols is None:
        symbols = SNIPE_SCAN
    symbols = symbols[:MAX_API_SYMBOLS]  # see get_itm_scan's identical guard
    top = max(top, 0)  # see get_active_options' identical guard
    # Clamped here, not only in the HTTP handler, so a direct caller can't
    # bypass the outbound fan-out bound either (see MAX_TARGET_DTE).
    target_dte = _clamp_int(target_dte, 0, MAX_TARGET_DTE)
    window = _clamp_int(window, 0, MAX_DTE_WINDOW)
    dte_lo, dte_hi = target_dte - window, target_dte + window
    key = f"itm-weekly:{','.join(sorted(symbols))}:{dte_lo}:{dte_hi}:{top}"
    cached = cache_get(key)
    if cached is not None:
        return cached

    as_of, candidates = _scan_itm_candidates(symbols, dte_lo, dte_hi)
    all_contracts = []
    for c, spot, base in candidates:
        dte = c.get("dte")
        strike, typ = c.get("strike"), c.get("type")
        ask = c.get("ask")
        delta = c.get("delta")
        iv = c.get("iv") or 0.0  # already a percent, e.g. 23.4 == 23.4%
        spread_pct = base["spread_pct"]
        extrinsic_ratio = base["extrinsic_ratio"]
        contract_cost = base["contract_cost"]

        # Per-contract expected move from its own IV, not a fixed spot-move
        # scenario -- standard vol-scaling: annualized vol * sqrt(time).
        expected_move_pct = (iv / 100.0) * math.sqrt(max(dte, 0) / 365.0)
        favorable_price = (spot * (1 + expected_move_pct) if typ == "C"
                           else spot * (1 - expected_move_pct))
        favorable_intrinsic = (max(0.0, favorable_price - strike) if typ == "C"
                               else max(0.0, strike - favorable_price))

        magnitude_pnl_dollars = (favorable_intrinsic - ask) * 100
        magnitude_pnl_pct = magnitude_pnl_dollars / contract_cost if contract_cost else 0.0
        flat_pnl_dollars = (base["intrinsic"] - ask) * 100

        raw_score = _weekly_itm_scan_score(delta, spread_pct, magnitude_pnl_pct,
                                           extrinsic_ratio)
        score = _apply_risk_flags(raw_score, base["flags"])

        all_contracts.append({
            "underlying": c.get("underlying"),
            "type": typ,
            "strike": strike,
            "expiry": c.get("expiry"),
            "dte": dte,
            "bid": c.get("bid"),
            "ask": ask,
            "volume": c.get("volume"),
            "oi": c.get("oi"),
            "iv": c.get("iv"),
            "delta": delta,
            "theta": c.get("theta"),
            "spot": spot,
            "mid": round(base["mid"], 4),
            "spread_pct": round(spread_pct, 4) if spread_pct is not None else None,
            "moneyness_pct": round(base["moneyness_pct"], 4),
            "extrinsic_ratio": round(extrinsic_ratio, 4) if extrinsic_ratio is not None else None,
            "expected_move_pct": round(expected_move_pct, 4),
            "weekly_score": score,
            "raw_weekly_score": raw_score,  # before risk-flag penalties
            "flags": base["flags"],
            "realized_vol": base["realized_vol"],
            "iv_rv_ratio": base["iv_rv_ratio"],
            "earnings_date": base["earnings_date"],
            "contract_cost": round(contract_cost, 2),
            "max_loss": round(contract_cost, 2),  # 1 long contract: max loss == entry cost
            "breakeven": round(base["breakeven"], 4),
            "breakeven_cushion_pct": round(base["breakeven_cushion_pct"], 4),
            "scenarios": {
                "flat": {"scenario_price": round(spot, 2),
                         "pnl_dollars": round(flat_pnl_dollars, 2),
                         "pnl_pct": round(flat_pnl_dollars / contract_cost, 4)
                                    if contract_cost else 0.0},
                "favorable": {"scenario_price": round(favorable_price, 2),
                              "move_pct": round(expected_move_pct, 4),
                              "pnl_dollars": round(magnitude_pnl_dollars, 2),
                              "pnl_pct": round(magnitude_pnl_pct, 4)},
            },
        })

    # Ranked by Weekly Score (highest first) -- probability + profit
    # magnitude + spread + theta exposure. Two contracts can legitimately
    # trade probability against magnitude differently; weekly_score is the
    # blended default sort, but abs(delta) and scenarios.favorable.pnl_pct
    # are both returned unscaled so a client can sort by either axis alone
    # instead of only trusting one composite number.
    all_contracts.sort(key=lambda c: c["weekly_score"], reverse=True)
    out = {
        "as_of": as_of,
        "delayed": True,
        "note": (f"CBOE delayed ~15min. ~{target_dte}-DTE ITM contracts (window "
                 f"+/-{window} days). Ranked by Weekly Score (probability + "
                 "IV-implied profit magnitude + spread + theta cost). Screening "
                 "tool, not a trade recommendation. No trades are placed automatically."),
        "contracts": all_contracts[:top],
    }
    cache_put(key, out, ttl=300)  # a week-out decision doesn't need 0DTE-grade freshness
    return out


# ---------------------------------------------------------------------------
# Debit spreads -- a separate board, not a column on the single-leg ones.
#
# A vertical debit spread has a fundamentally different payoff shape from a
# naked long: defined max loss AND capped max gain, versus unlimited-ish
# upside for the full premium at risk. Ranking the two in one list would
# compare numbers that don't mean the same thing (a 400% single-leg move
# scenario is not comparable to a spread's fixed max gain), so this gets its
# own scan, its own score, and its own endpoint.
# ---------------------------------------------------------------------------
_SPREAD_PROB_WEIGHT = 0.40    # |short-leg delta| -- odds of capturing MAX profit
_SPREAD_REWARD_WEIGHT = 0.40  # max gain per dollar risked
_SPREAD_COST_WEIGHT = 0.20    # combined execution cost of both legs
_SPREAD_DELTA_FLOOR = 0.40    # |short delta| at/below this scores 0 on probability
_SPREAD_DELTA_CEIL = 0.85     # |short delta| at/above this scores 1 on probability
_SPREAD_REWARD_CEIL = 2.0     # max_gain/max_loss at/above this scores 1 on reward
_SPREAD_COST_CEIL = 0.25      # combined spread% at/above this scores 0 on cost
_SPREAD_MAX_WIDTH_STEPS = 6   # how many strikes above/below the long leg to pair

# Minimum max_gain/max_loss for a pairing to be worth listing at all.
#
# This is a VALIDITY guard, not a risk preference -- the same category as the
# net_debit >= width rejection below, which drops spreads that mathematically
# cannot profit. A pairing can also be structurally pointless without being
# impossible: measured against the live chain, the worst real example was IWM
# 200/284 offering a $2 max gain on $8,398 of risk, and six pairings had a max
# gain under a realistic round-trip commission on two legs (~$2.60), so they
# could not clear costs even when everything went right.
#
# 0.05 keeps ~92% of generated pairings (measured, not guessed) while dropping
# that tail. The Spread Score already penalizes weak reward through its 40%
# reward term, but score-based demotion alone doesn't help here: the "Safer"
# sort ranks on short-leg delta only, which floats exactly these
# near-certain/near-worthless pairings to the top.
_SPREAD_MIN_REWARD_RISK = 0.05


def _spread_score(short_delta, reward_risk, cost_pct):
    """0-100 score for one vertical debit spread.

    Ranks on the question a spread actually poses -- "how likely am I to
    collect the FULL width, and how much do I collect per dollar risked" --
    rather than the single-leg boards' question of how far one option might
    run.

      - probability of max profit (40%): |delta| of the SHORT leg, not the
        long one. Max gain requires the underlying to finish beyond the
        short strike, so the short leg's delta is the market's own estimate
        of hitting the best case. Scaled 0.40 -> 0.85.
      - reward per dollar risked (40%): max_gain / max_loss, scaled 0 -> 2.0.
        A spread risking $300 to make $600 scores full marks here.
      - execution cost (20%): the COMBINED bid/ask cost of both legs, since
        a spread is two fills on the way in and (usually) two on the way
        out. Scaled so 0% -> 1 and 25% -> 0.
    """
    prob = _clamp01((abs(short_delta or 0) - _SPREAD_DELTA_FLOOR) /
                     (_SPREAD_DELTA_CEIL - _SPREAD_DELTA_FLOOR))
    reward = _clamp01((reward_risk or 0) / _SPREAD_REWARD_CEIL)
    cost = _clamp01(1 - (cost_pct if cost_pct is not None else 1.0) / _SPREAD_COST_CEIL)
    return round(100 * (_SPREAD_PROB_WEIGHT * prob +
                         _SPREAD_REWARD_WEIGHT * reward +
                         _SPREAD_COST_WEIGHT * cost), 1)


def get_debit_spreads(symbols=None, target_dte=7, window=_DEFAULT_DTE_WINDOW, top=20):
    """Build and rank vertical debit spreads (long ITM leg + short leg
    further out) across the scanned underlyings.

    For calls the short leg sits at a HIGHER strike, for puts a LOWER one --
    in both cases further from the money than the long leg, which is what
    makes the position a debit (you pay more for the long than you collect
    for the short) with a defined max loss and a capped max gain.

    Priced conservatively: the long leg fills at its ask and the short leg
    at its bid, i.e. both sides against you. That is the worst realistic
    fill, so the reported net debit is an upper bound on cost and the
    reported max gain a lower bound on payoff -- the honest direction to err
    for a screening tool.

    Screening only: it never places or simulates placing an order.

    Args:
        symbols (list): Underlyings to scan. Defaults to SNIPE_SCAN.
        target_dte (int), window (int): DTE window, as on the weekly board.
        top (int): Max spreads returned across all symbols.

    Returns:
        dict: {as_of, delayed, note, spreads}.
    """
    if symbols is None:
        symbols = SNIPE_SCAN
    symbols = symbols[:MAX_API_SYMBOLS]
    top = max(top, 0)
    target_dte = _clamp_int(target_dte, 0, MAX_TARGET_DTE)  # see get_itm_scan_weekly
    window = _clamp_int(window, 0, MAX_DTE_WINDOW)
    dte_lo, dte_hi = target_dte - window, target_dte + window
    key = f"spreads:{','.join(sorted(symbols))}:{dte_lo}:{dte_hi}:{top}"
    cached = cache_get(key)
    if cached is not None:
        return cached

    quotes, chains = _fetch_chains_and_quotes(symbols)
    earnings = earnings_map(dte_hi)

    all_spreads = []
    as_of = None
    for sym in symbols:
        res = chains.get(sym) or {}
        as_of = as_of or res.get("ts")
        spot = (quotes.get(sym) or {}).get("price")
        if not spot:
            continue
        rvol = realized_vol(sym)
        earnings_date_iso = earnings.get(sym.upper())

        # Group the tradeable rows by (expiry, type) so legs are only ever
        # paired within one expiry and one option type -- pairing across
        # either would not be a vertical spread.
        by_leg = {}
        for c in res.get("contracts", []):
            dte = c.get("dte")
            if dte is None or not (dte_lo <= dte <= dte_hi):
                continue
            bid, ask = c.get("bid"), c.get("ask")
            if bid is None or ask is None or bid <= 0 or ask <= 0:
                continue
            by_leg.setdefault((c.get("expiry"), c.get("type")), []).append(c)

        for (expiry, typ), legs in by_leg.items():
            # Ascending strike for calls, descending for puts, so "further
            # out of the money" is always forward in this list and the same
            # pairing loop works for both.
            legs.sort(key=lambda c: c["strike"], reverse=(typ == "P"))
            for i, long_leg in enumerate(legs):
                long_strike = long_leg["strike"]
                is_itm = ((typ == "C" and long_strike < spot) or
                          (typ == "P" and long_strike > spot))
                if not is_itm:
                    continue  # long leg anchors the position; keep it ITM
                for short_leg in legs[i + 1:i + 1 + _SPREAD_MAX_WIDTH_STEPS]:
                    short_strike = short_leg["strike"]
                    width = abs(short_strike - long_strike)
                    if width <= 0:
                        continue
                    # Worst realistic fill: pay the ask, receive the bid.
                    net_debit = long_leg["ask"] - short_leg["bid"]
                    if net_debit <= 0 or net_debit >= width:
                        # <=0 would be a credit (not a debit spread); >=width
                        # means paying more than the position can ever return.
                        continue

                    max_loss = net_debit * 100
                    max_gain = (width - net_debit) * 100
                    reward_risk = max_gain / max_loss if max_loss else None
                    if reward_risk is None or reward_risk < _SPREAD_MIN_REWARD_RISK:
                        # Structurally not worth listing -- see
                        # _SPREAD_MIN_REWARD_RISK. Same family as the
                        # rejection above: that one drops pairings that
                        # cannot profit, this one drops pairings whose best
                        # case doesn't justify the capital or clear costs.
                        continue
                    breakeven = (long_strike + net_debit if typ == "C"
                                 else long_strike - net_debit)
                    breakeven_cushion_pct = ((spot - breakeven) / spot if typ == "C"
                                             else (breakeven - spot) / spot)
                    combined_cost = ((long_leg["ask"] - long_leg["bid"]) +
                                     (short_leg["ask"] - short_leg["bid"]))
                    cost_pct = combined_cost / net_debit if net_debit else None

                    # Liquidity from the WEAKER leg (a spread is only as
                    # fillable as its thinner side); IV from the long leg,
                    # which is the one being paid for.
                    flags, iv_rv_ratio = _build_risk_flags(
                        min(long_leg.get("volume") or 0, short_leg.get("volume") or 0),
                        min(long_leg.get("oi") or 0, short_leg.get("oi") or 0),
                        long_leg.get("iv"), rvol, earnings_date_iso, expiry)

                    raw_score = _spread_score(short_leg.get("delta"),
                                              reward_risk, cost_pct)
                    all_spreads.append({
                        "underlying": sym,
                        "type": typ,
                        "expiry": expiry,
                        "dte": long_leg.get("dte"),
                        "spot": spot,
                        "long_strike": long_strike,
                        "short_strike": short_strike,
                        "width": round(width, 4),
                        "long_ask": long_leg["ask"],
                        "short_bid": short_leg["bid"],
                        "long_delta": long_leg.get("delta"),
                        "short_delta": short_leg.get("delta"),
                        "net_debit": round(net_debit * 100, 2),
                        "max_loss": round(max_loss, 2),
                        "max_gain": round(max_gain, 2),
                        "reward_risk": round(reward_risk, 3) if reward_risk else None,
                        "breakeven": round(breakeven, 4),
                        "breakeven_cushion_pct": round(breakeven_cushion_pct, 4),
                        "cost_pct": round(cost_pct, 4) if cost_pct is not None else None,
                        "spread_score": _apply_risk_flags(raw_score, flags),
                        "raw_spread_score": raw_score,
                        "flags": flags,
                        "realized_vol": round(rvol, 2) if rvol else None,
                        "iv_rv_ratio": iv_rv_ratio,
                        "earnings_date": earnings_date_iso,
                    })

    all_spreads.sort(key=lambda s: s["spread_score"], reverse=True)
    out = {
        "as_of": as_of,
        "delayed": True,
        "note": (f"CBOE delayed ~15min. Vertical debit spreads, ~{target_dte}-DTE "
                 f"(window +/-{window} days), long leg ITM. Priced at the worst "
                 "realistic fill (buy the ask, sell the bid), so net debit is an "
                 "upper bound and max gain a lower bound. Pairings returning under "
                 f"{_SPREAD_MIN_REWARD_RISK:.0%} of the capital at risk are excluded "
                 "— their best case doesn't clear transaction costs. Ranked by "
                 "Spread Score (probability of max profit + reward per dollar risked "
                 "+ execution cost). Screening tool, not a trade recommendation. "
                 "No trades are placed automatically."),
        "spreads": all_spreads[:top],
    }
    cache_put(key, out, ttl=300)
    return out


# ---------------------------------------------------------------------------
# Gamma concentration by strike.
#
# WHAT THIS IS NOT: a dealer-positioning model. The common "GEX" treatment
# signs call gamma positive and put gamma negative on the assumption that
# dealers are long calls and short puts, then concludes where they'll be
# forced to hedge. That assumption is not in this data -- open interest says
# how many contracts exist at a strike, never who holds which side. Publishing
# an inferred dealer position as fact would be the most overconfident thing in
# this app.
#
# So this reports the measurable thing only: how much gamma sits at each
# strike, calls and puts kept separate rather than netted. A strike with a
# large concentration is where the most option-price convexity is anchored,
# which is a real and useful observation; whether anyone is hedging it, and in
# which direction, is left to the reader rather than asserted.
# ---------------------------------------------------------------------------
GAMMA_TOP_STRIKES = 12


def get_gamma_exposure(symbol, target_dte=30, window=None, top=GAMMA_TOP_STRIKES):
    """Gamma concentration per strike for one underlying.

    Uses the FULL chain including untraded strikes -- open interest is the
    quantity being measured, and untraded rows hold a sixth of it (see
    _parse_chain's require_volume note).

    Per strike, per side, the reported figure is:

        gamma * open_interest * 100 * spot^2 * 0.01

    i.e. the dollar change in aggregate delta for a 1% move in the
    underlying. gamma is per-share, a contract covers 100 shares, and
    spot^2 * 0.01 converts "delta per $1" into "dollars per 1%" -- so the
    number is directly comparable across underlyings at different prices.

    Args:
        symbol (str): underlying to analyze.
        target_dte (int): include expiries out to this many days. Gamma is
            concentrated in near-dated contracts, and far-dated OI mostly
            adds noise, so this defaults to a month rather than the whole
            chain.
        window (int): unused placeholder kept out of the signature's way --
            the DTE filter here is one-sided (0..target_dte), unlike the
            boards' centered window.
        top (int): how many strikes to return, ranked by total concentration.

    Returns:
        dict: {symbol, spot, as_of, delayed, note, strikes: [...],
               peak_strike, total_call, total_put}.
    """
    target_dte = _clamp_int(target_dte, 0, MAX_TARGET_DTE)
    top = max(top, 0)
    key = f"gamma:{symbol}:{target_dte}:{top}"
    cached = cache_get(key)
    if cached is not None:
        return cached

    quotes = get_quotes([symbol])
    spot = (quotes.get(symbol) or {}).get("price")
    res = _fetch_chain(symbol, require_volume=False)
    contracts = res.get("contracts") or []

    by_strike = {}
    for c in contracts:
        dte = c.get("dte")
        if dte is None or dte < 0 or dte > target_dte:
            continue
        gamma, oi = c.get("gamma"), c.get("oi")
        if not gamma or not oi:
            continue
        # dollars of delta per 1% move -- see the docstring
        exposure = abs(gamma) * oi * 100 * (spot ** 2) * 0.01 if spot else 0.0
        row = by_strike.setdefault(c["strike"], {
            "strike": c["strike"], "call": 0.0, "put": 0.0,
            "call_oi": 0, "put_oi": 0,
        })
        if c.get("type") == "C":
            row["call"] += exposure
            row["call_oi"] += oi
        else:
            row["put"] += exposure
            row["put_oi"] += oi

    rows = []
    for row in by_strike.values():
        row["total"] = row["call"] + row["put"]
        # Kept separate rather than netted: netting encodes an assumption
        # about who is long which side, which this data cannot support.
        rows.append({k: (round(v, 2) if isinstance(v, float) else v)
                     for k, v in row.items()})
    rows.sort(key=lambda r: r["total"], reverse=True)
    peak = rows[0]["strike"] if rows else None

    out = {
        "symbol": symbol,
        "spot": spot,
        "as_of": res.get("ts"),
        "delayed": True,
        "target_dte": target_dte,
        "peak_strike": peak,
        "total_call": round(sum(r["call"] for r in rows), 2),
        "total_put": round(sum(r["put"] for r in rows), 2),
        "strikes": sorted(rows[:top], key=lambda r: r["strike"]),
        "note": ("CBOE delayed ~15min. Gamma concentration = gamma x open interest, "
                 "expressed as dollars of delta per 1% move, for expiries within "
                 f"{target_dte} days. Calls and puts are reported SEPARATELY, not "
                 "netted: open interest says how many contracts exist at a strike, "
                 "never who holds which side, so any dealer-positioning reading is "
                 "an assumption this data cannot support. A high-concentration "
                 "strike is where option convexity is anchored -- not a prediction "
                 "that price will pin there. Screening tool, not a trade "
                 "recommendation."),
    }
    cache_put(key, out, ttl=300)
    return out


# ---------------------------------------------------------------------------
# Modeled historical simulation of the weekly ITM strategy.
#
# READ THIS BEFORE TRUSTING ANY NUMBER IT RETURNS. This is NOT a backtest.
# There is no historical intraday options data on this free feed -- that is
# precisely why the Snipe Log records forward, one trade at a time, instead.
# What follows prices hypothetical contracts with Black-Scholes from daily
# underlying bars, so every result inherits the model's assumptions rather
# than reporting what could actually have been filled.
#
# Three specific ways it flatters the strategy, all of them structural:
#
#   1. Volatility. Entry is priced off REALIZED vol from trailing bars,
#      because implied vol history doesn't exist here. Implied normally
#      trades above realized (that gap IS the variance risk premium), so
#      modeled entries are systematically CHEAPER than real ones, which
#      inflates returns. get_modeled_backtest reports a vol-premium
#      sensitivity row precisely so this isn't buried in prose.
#   2. Spread. A modeled price has no bid/ask. Live weekly ITM spreads on
#      these names measured 2.7% median / 3.6% mean of premium, so entry
#      gets a haircut -- without one the results are materially better than
#      anything fillable.
#   3. Model shape. Black-Scholes assumes constant vol, no skew, and
#      European exercise. Real ITM equity options are American and skewed.
#
# It is included because a forward log accumulates one trade per day and
# calibrating a scoring formula needs a sample far larger than that. Treat
# the output as "does this strategy shape have any edge at all", never as a
# performance claim.
# ---------------------------------------------------------------------------
_BS_RATE = 0.04             # flat risk-free rate; the app has no yield-curve feed
_MODELED_SPREAD_PCT = 0.03  # measured live median 2.7% / mean 3.6% of premium
_VOL_PREMIUM_SENSITIVITY = (1.0, 1.1, 1.25)

# Implied-vol proxies. Pricing entries off realized vol was the single
# largest source of optimism in this simulation (implied normally trades
# above realized, so modeled entries came out too cheap); these indices are
# actual traded implied vol, which removes the guess rather than scaling it.
#
# Only symbols with a REAL proxy appear here. ^RVX (the Russell 2000 vol
# index, the natural match for IWM) 404s on this feed, and ^VXD carries one
# usable bar -- so IWM has no proxy and falls back to realized rather than
# borrowing SPY's volatility, which would be a different underlying's risk
# dressed up as IWM's.
#
# Two approximations remain, and they're documented rather than hidden:
# these track the INDEX (SPX/NDX), not the ETF, and they quote 30-day
# implied vol while the simulated contracts are ~7-day, so term structure
# is ignored.
_IV_PROXY = {"SPY": "^VIX", "QQQ": "^VXN"}


def _norm_cdf(x):
    """Standard normal CDF via math.erf -- keeps this stdlib-only."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black_scholes_price(spot, strike, years, sigma, rate=_BS_RATE, is_call=True):
    """Black-Scholes price for a European option. Returns intrinsic value at
    (or past) expiry, or when volatility is degenerate, rather than dividing
    by zero."""
    intrinsic = max(0.0, spot - strike) if is_call else max(0.0, strike - spot)
    if years <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return intrinsic
    sqrt_t = math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma ** 2) * years) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    discount = math.exp(-rate * years)
    if is_call:
        return spot * _norm_cdf(d1) - strike * discount * _norm_cdf(d2)
    return strike * discount * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def _realized_vol_from(closes):
    """Annualized realized vol from a list of closes, or None if too short.
    Separate from realized_vol() (which fetches) so the simulation can
    compute it over a trailing slice with no lookahead."""
    rets = [math.log(closes[i] / closes[i - 1])
            for i in range(1, len(closes))
            if closes[i - 1] > 0 and closes[i] > 0]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252)


def implied_vol_series(symbol, rng):
    """{date: sigma} from `symbol`'s implied-vol proxy index, or None when no
    proxy exists for it.

    The index quotes annualized implied vol in percent (VIX 15.4 -> 0.154).
    Returning None rather than an empty dict lets callers distinguish "this
    symbol has no proxy" from "the proxy fetch came back empty", which
    matter differently: the first is permanent, the second is retryable.
    """
    proxy = _IV_PROXY.get(symbol.upper())
    if not proxy:
        return None
    hist = get_history(proxy, rng)
    ts, closes = hist.get("t") or [], hist.get("c") or []
    return {_et_date_from_ts(t): c / 100.0 for t, c in zip(ts, closes) if c}


def _simulate_weekly_itm(bars, hold_days, moneyness, vol_lookback,
                         vol_premium, spread_pct, is_call=True, iv_by_date=None):
    """One pass of the simulation. `bars` is [(date, close), ...] ascending.

    Buys a hypothetical ITM option `moneyness` in the money, holds to
    expiry, settles at intrinsic.

    Volatility comes from `iv_by_date` (real traded implied vol) when a
    date is available there, otherwise from realized vol over the trailing
    window. Either way it uses only data at or before the entry bar, so
    there is no lookahead.
    """
    trades = []
    for i in range(vol_lookback, len(bars) - hold_days):
        entry_date, spot = bars[i]
        exit_date, exit_spot = bars[i + hold_days]
        sigma = (iv_by_date or {}).get(entry_date)
        if not sigma:
            sigma = _realized_vol_from([c for _d, c in bars[i - vol_lookback:i + 1]])
        if not sigma:
            continue
        sigma *= vol_premium
        # Strike placed `moneyness` in the money, rounded to a plausible
        # $1 grid so strikes look like ones that would really list.
        strike = round(spot * (1 - moneyness)) if is_call else round(spot * (1 + moneyness))
        years = hold_days / 365.0
        theo = black_scholes_price(spot, strike, years, sigma, is_call=is_call)
        if theo <= 0:
            continue
        entry = theo * (1 + spread_pct / 2)  # pay the modeled ask, not the mid
        payoff = (max(0.0, exit_spot - strike) if is_call
                  else max(0.0, strike - exit_spot))
        pnl = (payoff - entry) * 100
        trades.append({
            "entry_date": entry_date, "exit_date": exit_date,
            "spot": round(spot, 2), "strike": strike, "exit_spot": round(exit_spot, 2),
            "sigma": round(sigma, 4), "entry": round(entry, 4),
            "payoff": round(payoff, 4),
            "pnl_dollars": round(pnl, 2),
            "pnl_pct": round(pnl / (entry * 100), 4),
        })
    return trades


def _summarize_trades(trades):
    n = len(trades)
    if not n:
        return {"trades": 0, "wins": 0, "win_rate": None,
                "total_pnl_dollars": 0.0, "avg_pnl_pct": 0.0,
                "best_pnl_pct": None, "worst_pnl_pct": None}
    wins = sum(1 for t in trades if t["pnl_dollars"] > 0)
    return {
        "trades": n,
        "wins": wins,
        "win_rate": round(wins / n, 4),
        "total_pnl_dollars": round(sum(t["pnl_dollars"] for t in trades), 2),
        "avg_pnl_pct": round(sum(t["pnl_pct"] for t in trades) / n, 4),
        "best_pnl_pct": max(t["pnl_pct"] for t in trades),
        "worst_pnl_pct": min(t["pnl_pct"] for t in trades),
    }


def get_modeled_backtest(symbol, rng="2y", hold_days=7, moneyness=0.01,
                         vol_lookback=30, spread_pct=_MODELED_SPREAD_PCT,
                         option_type="C", vol_source="implied"):
    """Model the weekly ITM strategy over historical daily bars.

    See the module comment above this function for why the result is a
    model rather than a backtest, and the three structural ways it flatters
    the strategy.

    Returns a headline result plus a `sensitivity` block pricing the same
    trades at higher volatility assumptions. That block is the point: the
    single biggest unknown is how far implied would have sat above the
    realized vol used for entry, and showing the result collapse (or not)
    as that gap widens is more honest than one number with a disclaimer.
    """
    is_call = option_type.upper() == "C"
    hold_days = _clamp_int(hold_days, 1, MAX_TARGET_DTE)
    vol_lookback = _clamp_int(vol_lookback, 5, 250)
    key = (f"mbt:{symbol}:{rng}:{hold_days}:{moneyness}:{vol_lookback}:"
           f"{spread_pct}:{option_type}:{vol_source}")
    cached = cache_get(key)
    if cached is not None:
        return cached

    hist = get_history(symbol, rng)
    ts, closes = hist.get("t") or [], hist.get("c") or []
    bars = [(_et_date_from_ts(t), c) for t, c in zip(ts, closes)]

    iv_by_date = implied_vol_series(symbol, rng) if vol_source == "implied" else None
    proxy = _IV_PROXY.get(symbol.upper())
    if vol_source == "implied" and iv_by_date:
        vol_source_used = f"implied ({proxy})"
    elif vol_source == "implied":
        # Asked for implied, none exists for this symbol. Say so rather than
        # silently reporting realized-vol results under an "implied" label.
        vol_source_used = f"realized (no implied-vol proxy for {symbol})"
    else:
        vol_source_used = f"realized ({vol_lookback}d trailing)"

    def _run(premium, spread):
        return _simulate_weekly_itm(bars, hold_days, moneyness, vol_lookback,
                                    premium, spread, is_call, iv_by_date)

    headline = _run(1.0, spread_pct)
    sensitivity = []
    for premium in _VOL_PREMIUM_SENSITIVITY:
        s = _summarize_trades(_run(premium, spread_pct))
        s["vol_premium"] = premium
        sensitivity.append(s)
    # Same trades with no spread haircut, to size how much execution costs
    # actually matter rather than asserting that they do.
    no_spread = _summarize_trades(_run(1.0, 0.0))
    # And the same trades priced off realized vol, so the difference the
    # implied-vol input makes is a visible number rather than a claim.
    realized_only = _summarize_trades(_simulate_weekly_itm(
        bars, hold_days, moneyness, vol_lookback, 1.0, spread_pct, is_call, None))

    out = {
        "symbol": symbol,
        "range": rng,
        "hold_days": hold_days,
        "moneyness": moneyness,
        "option_type": "C" if is_call else "P",
        "spread_pct": spread_pct,
        "bars": len(bars),
        "vol_source": vol_source_used,
        "summary": _summarize_trades(headline),
        "sensitivity": sensitivity,
        "no_spread_summary": no_spread,
        "realized_vol_summary": realized_only,
        "trades": headline[-20:],  # a tail sample, not the whole series
        "modeled": True,
        "note": ("MODELED, NOT A BACKTEST. No historical options prices exist on "
                 "this feed, so entries are priced with Black-Scholes from daily "
                 f"underlying bars. Entry volatility: {vol_source_used}. "
                 + ("A traded implied-vol index removes the largest guess here, but "
                    "it tracks the INDEX rather than the ETF and quotes 30-day vol "
                    "against ~7-day contracts, so term structure is ignored. "
                    if iv_by_date else
                    "Realized vol is NOT what an option would really have cost -- "
                    "implied normally trades above it, so these returns are "
                    "optimistic. ")
                 + "`sensitivity` reprices the same trades at higher volatility, "
                 f"`no_spread_summary` drops the {spread_pct:.0%} execution "
                 "haircut, and `realized_vol_summary` prices them off realized "
                 "vol for comparison. Black-Scholes also assumes constant vol, no "
                 "skew and European exercise; real ITM equity options are American "
                 "and skewed. Held to expiry, settled at intrinsic. Use this to ask "
                 "whether the strategy shape has any edge at all, never as a "
                 "performance claim."),
    }
    cache_put(key, out, ttl=900)
    return out


# ---------------------------------------------------------------------------
# Snipe Log -- forward paper-trading track record for the Snipe board's
# top-scored pick each day.
#
# This is intentionally NOT a backtest: there is no historical intraday
# options data available from the free CBOE feed, so retroactively pricing
# "what would this contract have cost yesterday at 3:30pm" would mean
# fabricating option prices. Instead this logs the real top-ranked candidate
# going forward (at ~30min before close) and settles it against the
# underlying's REAL closing price the next time the log is read on a later
# day -- an honest, if slower-to-accumulate, track record.
#
# 100% read-only bookkeeping: entry price is the screen's own ask, exit value
# is computed intrinsic value at the close. Nothing here places, simulates
# placing, or connects to anything capable of placing a real order.
# ---------------------------------------------------------------------------
_snipe_log_lock = threading.Lock()


def _load_snipe_log():
    """Read the persisted Snipe Log as a list of entry dicts.

    Returns [] if the file doesn't exist yet (first run) or is unreadable/
    corrupt -- a bad log file should degrade to "no history yet", not crash
    every endpoint that touches the Snipe tab.
    """
    with _snipe_log_lock:
        try:
            with open(SNIPE_LOG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except FileNotFoundError:
            return []
        except (json.JSONDecodeError, OSError, ValueError):
            return []


def _save_snipe_log(entries):
    """Persist the Snipe Log via write-temp-then-atomic-replace, so a crash
    or power loss mid-write can't leave a half-written/corrupt JSON file
    behind (os.replace is atomic on both POSIX and Windows, unlike writing
    the target path directly)."""
    with _snipe_log_lock:
        tmp_path = f"{SNIPE_LOG_PATH}.tmp-{os.getpid()}-{threading.get_ident()}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2)
        os.replace(tmp_path, SNIPE_LOG_PATH)


# The boards that keep a forward paper-trading record, and how to scan each.
# Adding a board here is all it takes for the scheduler, the resolver and the
# API to cover it -- the settlement rule below is horizon-independent.
SNIPE_LOG_BOARDS = {
    "0dte": {"scan": lambda: get_itm_scan(), "score_key": "score"},
    "weekly": {"scan": lambda: get_itm_scan_weekly(), "score_key": "weekly_score"},
}
DEFAULT_SNIPE_BOARD = "0dte"


def _entry_board(entry):
    """The board an entry belongs to, defaulting to 0DTE.

    Entries written before the weekly board existed have no `board` field;
    they were all 0DTE picks, so that's the correct reading of a missing
    value rather than a reason to migrate the file.
    """
    return entry.get("board") or DEFAULT_SNIPE_BOARD


def _entry_settle_date(entry):
    """The date an entry can first be settled against: the contract's expiry.

    This is what makes one resolver serve both boards. A 0DTE pick expires
    the same day it's logged, so its expiry EQUALS its date and the rule is
    identical to the original "settle the day after it was logged". A weekly
    pick expires 5-10 days later, and the same rule waits for that date
    instead -- no per-board branching needed.

    Falls back to the entry date if `expiry` is somehow absent, which keeps
    a malformed or hand-edited entry resolvable rather than stuck open.
    """
    return entry.get("expiry") or entry.get("date")


def snapshot_snipe_pick(late=False, board=DEFAULT_SNIPE_BOARD):
    """Log a board's top-scored candidate as a new open paper-trade entry.

    Idempotent per calendar day PER BOARD: if this board already has an
    entry dated today, that entry is returned unchanged rather than
    duplicated (so the startup catch-up, the scheduler's own daily call, and
    repeated clicks of the manual "snapshot now" button are all safe). If the
    scan currently has zero candidates (e.g. no live two-sided market yet),
    nothing is logged and None is returned -- there's nothing honest to
    record.

    Args:
        late (bool): True when this is a startup catch-up (the server wasn't
            running at the scheduled 15:30 ET time) rather than the regular
            on-schedule snapshot. Stored for visibility; doesn't change the
            logging logic.
        board (str): which board to snapshot -- see SNIPE_LOG_BOARDS.

    Returns:
        dict or None: the (new or pre-existing) log entry, or None if there
        was nothing to log.
    """
    cfg = SNIPE_LOG_BOARDS.get(board)
    if cfg is None:
        return None
    scan = cfg["scan"]()
    contracts = scan.get("contracts") or []
    today = _today_et().isoformat()  # ET: this is the entry's TRADING day

    entries = _load_snipe_log()
    for existing in entries:
        if existing.get("date") == today and _entry_board(existing) == board:
            return existing

    if not contracts:
        return None

    c = contracts[0]
    logged_at = _now_et().isoformat(timespec="seconds")
    entry = {
        # board is part of the id so the two boards' same-day picks on the
        # same underlying stay distinguishable
        "id": f"{today}-{board}-{c.get('underlying')}",
        "board": board,
        "date": today,
        "logged_at": logged_at,
        "underlying": c.get("underlying"),
        "type": c.get("type"),
        "strike": c.get("strike"),
        "expiry": c.get("expiry"),
        "dte": c.get("dte"),
        "entry_ask": c.get("ask"),
        "entry_delta": c.get("delta"),
        "entry_spread_pct": c.get("spread_pct"),
        "entry_score": c.get(cfg["score_key"]),
        "entry_flags": [f.get("code") for f in (c.get("flags") or [])],
        "contract_cost": c.get("contract_cost"),
        "breakeven": c.get("breakeven"),
        "late_snapshot": bool(late),
        "status": "open",
        "close_price": None,
        "exit_value": None,
        "pnl_dollars": None,
        "pnl_pct": None,
        "correct": None,
        "resolved_at": None,
    }
    entries.append(entry)
    _save_snipe_log(entries)
    return entry


def _closing_price_on(symbol, date_str):
    """Find `symbol`'s closing price on a specific calendar trading day
    (YYYY-MM-DD, America/New_York) from its recent daily-bar history.

    Returns None if that date's bar can't be found (feed hiccup, thin/stale
    cache, symbol delisted, etc.) rather than raising -- callers must treat
    "can't find it" as "try again later," not a crash.
    """
    hist = get_history(symbol, rng="1mo")
    ts_list = hist.get("t") or []
    closes = hist.get("c") or []
    for t, c in zip(ts_list, closes):
        if _et_date_from_ts(t) == date_str:
            return c
    return None


def resolve_snipe_log():
    """Settle every open Snipe Log entry whose contract has expired, against
    the underlying's real closing price on that expiry date.

    Settling on EXPIRY (not on the day the pick was logged) is what lets one
    resolver serve every board: a 0DTE pick expires the same day it's logged,
    so this is identical to the original "settle it the next day" rule, while
    a weekly pick simply waits the 5-10 days until its own expiry. An entry
    whose expiry is today or later stays open -- the contract hasn't settled
    yet. If the close price for that date can't be found, the entry is left
    open and retried later rather than resolved with bad data.

    Returns:
        list: the full (possibly updated) log, unsorted.
    """
    entries = _load_snipe_log()
    today = _today_et().isoformat()  # ET: "has this contract's expiry passed?"
    changed = False
    for e in entries:
        if e.get("status") != "open":
            continue
        settle_date = _entry_settle_date(e)
        if not settle_date or settle_date >= today:
            continue  # not expired yet (or a future date) -- not settled
        close = _closing_price_on(e.get("underlying"), settle_date)
        if close is None:
            continue  # can't resolve yet (feed hiccup / stale cache) -- try again later
        strike = e.get("strike")
        if e.get("type") == "C":
            exit_value = max(0.0, close - strike)
        else:
            exit_value = max(0.0, strike - close)
        entry_ask = e.get("entry_ask") or 0.0
        contract_cost = e.get("contract_cost")
        pnl_dollars = round((exit_value - entry_ask) * 100, 2)
        e["close_price"] = close
        e["exit_value"] = round(exit_value, 4)
        e["pnl_dollars"] = pnl_dollars
        e["pnl_pct"] = round(pnl_dollars / contract_cost, 4) if contract_cost else None
        e["correct"] = pnl_dollars > 0
        e["resolved_at"] = _now_et().isoformat(timespec="seconds")
        e["status"] = "closed"
        changed = True
    if changed:
        _save_snipe_log(entries)
    return entries


def get_snipe_log(board=DEFAULT_SNIPE_BOARD):
    """Return one board's Snipe Log, freshly resolved, most-recent-first,
    plus a summary computed only over that board's closed (settled) trades.

    Scoped per board deliberately: the whole point of the log is to say
    whether a given board's scoring picks winners, and pooling two boards'
    records would produce a number that describes neither. Resolution still
    runs across every entry regardless of the board asked for -- settling is
    independent of who's reading.

    Args:
        board (str): which board's record to return. Pass None for all
            boards pooled (the entries, at least; the summary then spans
            them and should be read with that in mind).

    Returns:
        dict: {board, entries: [...], summary: {trades, wins, win_rate,
            total_pnl_dollars, avg_pnl_dollars, avg_pnl_pct}}. summary
            fields are all 0/None-ish when there are zero closed trades yet.
    """
    all_entries = resolve_snipe_log()
    entries = ([e for e in all_entries if _entry_board(e) == board]
               if board else all_entries)
    ordered = sorted(entries, key=lambda e: e.get("date", ""), reverse=True)
    closed = [e for e in entries if e.get("status") == "closed"]
    n = len(closed)
    if n == 0:
        summary = {
            "trades": 0, "wins": 0, "win_rate": None,
            "total_pnl_dollars": 0.0, "avg_pnl_dollars": 0.0, "avg_pnl_pct": 0.0,
        }
    else:
        wins = sum(1 for e in closed if e.get("correct"))
        total_pnl = sum(e.get("pnl_dollars") or 0.0 for e in closed)
        avg_pct = sum(e.get("pnl_pct") or 0.0 for e in closed) / n
        summary = {
            "trades": n,
            "wins": wins,
            "win_rate": round(wins / n, 4),
            "total_pnl_dollars": round(total_pnl, 2),
            "avg_pnl_dollars": round(total_pnl / n, 2),
            "avg_pnl_pct": round(avg_pct, 4),
        }
    return {"board": board, "entries": ordered, "summary": summary}


def _next_snipe_snapshot_time(now=None):
    """Next weekday occurrence of 15:30 America/New_York (30min before the
    16:00 ET equity close), strictly after `now`.

    No market-holiday awareness -- a real exchange holiday still produces a
    scheduled attempt. Known/accepted gap: get_itm_scan() naturally returns
    zero candidates on a day the market never opened, so snapshot_snipe_pick()
    just logs nothing for that date rather than misbehaving.
    """
    now = now or _now_et()
    candidate = now.replace(hour=15, minute=30, second=0, microsecond=0)
    if candidate <= now:
        candidate += dt.timedelta(days=1)
    while candidate.weekday() >= 5:  # 5=Saturday, 6=Sunday
        candidate += dt.timedelta(days=1)
    return candidate


def _snipe_scheduler_loop():
    """Daemon loop: snapshot the day's top Snipe pick ~30min before the US
    equity close, every weekday, forever.

    Runs a one-time startup catch-up first (if the server happens to start
    on a weekday after 15:30 ET, e.g. it wasn't running at the scheduled
    time), then loops sleeping to the next 15:30 ET and snapshotting again.
    Each snapshot attempt is wrapped in its own try/except: a bad snapshot
    (network hiccup, CBOE outage, etc.) is logged to stderr and the loop
    keeps running rather than the whole background thread dying silently.
    """
    def _snapshot_all(late=False):
        """Snapshot every board, isolating failures per board: a CBOE hiccup
        on one must not cost the other its record for the day."""
        for board in SNIPE_LOG_BOARDS:
            try:
                snapshot_snipe_pick(late=late, board=board)
            except Exception as e:  # noqa: BLE001
                print(f"[snipe scheduler] {board} snapshot failed: {e}", file=sys.stderr)

    now = _now_et()
    if now.weekday() < 5 and now.time() >= dt.time(15, 30):
        _snapshot_all(late=True)
    while True:
        try:
            target = _next_snipe_snapshot_time()
            sleep_s = (target - _now_et()).total_seconds()
            if sleep_s > 0:
                time.sleep(sleep_s)
            _snapshot_all()
        except Exception as e:  # noqa: BLE001 -- keep the daemon loop alive across bad runs
            print(f"[snipe scheduler] scheduling failed: {e}", file=sys.stderr)
            time.sleep(60)  # avoid a tight crash loop if something's persistently broken


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
        """0DTE board by default. `target_dte`/`window` route to the weekly
        board's scoring instead of the 0DTE score, so one endpoint can serve
        any horizon -- the two boards optimize for genuinely different things
        (see _itm_scan_score vs _weekly_itm_scan_score), so the horizon picks
        the formula rather than one formula being stretched across both.
        /api/options/itm-scan-weekly stays live as its own path."""
        syms = self._qsymbols(query, default=SNIPE_SCAN)
        top = int(self._qparam(query, "top", "20"))
        target_dte = int(self._qparam(query, "target_dte", "0"))
        if target_dte > 0:
            window = int(self._qparam(query, "window", str(_DEFAULT_DTE_WINDOW)))
            return get_itm_scan_weekly(syms, target_dte, window, top)
        down = float(self._qparam(query, "down", "-0.005"))
        flat = float(self._qparam(query, "flat", "0.0"))
        up = float(self._qparam(query, "up", "0.005"))
        return get_itm_scan(syms, down, flat, up, top)

    def _api_options_itm_scan_weekly(self, query):
        syms = self._qsymbols(query, default=SNIPE_SCAN)
        top = int(self._qparam(query, "top", "20"))
        target_dte = int(self._qparam(query, "target_dte", "7"))
        window = int(self._qparam(query, "window", str(_DEFAULT_DTE_WINDOW)))
        return get_itm_scan_weekly(syms, target_dte, window, top)

    def _api_options_debit_spreads(self, query):
        syms = self._qsymbols(query, default=SNIPE_SCAN)
        top = int(self._qparam(query, "top", "20"))
        target_dte = int(self._qparam(query, "target_dte", "7"))
        window = int(self._qparam(query, "window", str(_DEFAULT_DTE_WINDOW)))
        return get_debit_spreads(syms, target_dte, window, top)

    def _api_options_gamma(self, query):
        sym = self._qparam(query, "symbol")
        if not sym:
            return {"_error": "no symbol"}
        target_dte = int(self._qparam(query, "target_dte", "30"))
        top = int(self._qparam(query, "top", str(GAMMA_TOP_STRIKES)))
        return get_gamma_exposure(sym, target_dte=target_dte, top=top)

    def _api_options_modeled_backtest(self, query):
        sym = self._qparam(query, "symbol")
        if not sym:
            return {"_error": "no symbol"}
        return get_modeled_backtest(
            sym,
            rng=self._qparam(query, "range", "2y"),
            hold_days=int(self._qparam(query, "hold_days", "7")),
            moneyness=float(self._qparam(query, "moneyness", "0.01")),
            option_type=self._qparam(query, "type", "C"),
            vol_source=self._qparam(query, "vol_source", "implied"),
        )

    def _api_snipe_log(self, query):
        board = self._qparam(query, "board", DEFAULT_SNIPE_BOARD)
        if board not in SNIPE_LOG_BOARDS:
            return {"_error": f"unknown board {board!r}"}
        return get_snipe_log(board)

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
        "/api/options/itm-scan-weekly": _api_options_itm_scan_weekly,
        "/api/options/debit-spreads": _api_options_debit_spreads,
        "/api/options/gamma": _api_options_gamma,
        "/api/options/modeled-backtest": _api_options_modeled_backtest,
        "/api/snipe-log": _api_snipe_log,
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

    def do_POST(self):
        # Deliberately tiny: exactly one POST route exists (a manual "snapshot
        # today's pick now" trigger for the Snipe Log), so this stays a small,
        # dedicated method rather than growing do_GET's routing table/pattern
        # for a single endpoint. Any other path 404s.
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)  # drain any body so HTTP/1.1 keep-alive stays in sync
        parsed_url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed_url.query)
        try:
            if parsed_url.path == "/api/snipe-log/snapshot":
                board = self._qparam(query, "board", DEFAULT_SNIPE_BOARD)
                if board not in SNIPE_LOG_BOARDS:
                    self._send_json({"_error": f"unknown board {board!r}"}, status=400)
                    return
                entry = snapshot_snipe_pick(board=board)
                if entry is None:
                    self._send_json({"_error": "No live two-sided market to snapshot right now"})
                else:
                    self._send_json(entry)
            else:
                self.send_error(404)
        except BrokenPipeError:
            pass
        except Exception as e:
            print(f"[server error] Unhandled exception in POST: {e}", file=sys.stderr)
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
    # Daemon thread: snapshots the day's top Snipe pick ~30min before the US
    # equity close every weekday (plus a startup catch-up), so it doesn't
    # block process exit and a snapshot failure can never take the HTTP
    # server down with it (see _snipe_scheduler_loop's own try/except).
    threading.Thread(target=_snipe_scheduler_loop, daemon=True, name="snipe-scheduler").start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
