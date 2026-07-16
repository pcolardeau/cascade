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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# Underlyings scanned for the "most active options" board. Kept to the genuinely
# option-liquid names (the real leaders) so a cold scan stays responsive.
OPTION_SCAN = [
    "SPY", "QQQ", "IWM", "NVDA", "TSLA", "AAPL", "AMZN", "MSFT", "META", "AMD",
    "GOOGL", "MU", "JPM", "BAC", "XOM", "UNH", "LLY", "BA", "WMT", "GE", "AVGO",
    "COIN", "PLTR", "SMCI",
]

# ---------------------------------------------------------------------------
# tiny TTL cache
# ---------------------------------------------------------------------------
_cache = {}
_clock = threading.Lock()


def cache_get(key):
    with _clock:
        hit = _cache.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
    return None


def cache_put(key, value, ttl):
    with _clock:
        _cache[key] = (time.time() + ttl, value)


def fetch(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# Yahoo spark throttles rapid successive calls from one IP (returns 400), so
# serialize all Yahoo requests behind a minimum inter-call gap, with one retry.
_yahoo_lock = threading.Lock()
_yahoo_last = [0.0]


def fetch_yahoo(url, timeout=15, gap=0.6):
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
    key = "q:" + ",".join(symbols)
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
    except Exception as e:  # noqa: BLE001
        out["_error"] = str(e)
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
    key = "corr:" + ",".join(sorted(symbols)) + f":{rng}"
    cached = cache_get(key)
    if cached is not None:
        return cached
    # Sequential, like get_quotes — reuses get_history's own Yahoo pacing
    # (fetch_yahoo) and its 900s cache, so a repeat sync within 15min of a
    # quote/chart fetch for the same symbol costs nothing extra.
    rmap, missing = {}, []
    for sym in symbols:
        r = _returns_by_t(get_history(sym, rng))
        if len(r) < 20:
            missing.append(sym)
        rmap[sym] = r
    # Pairwise date-intersection: a gappy or newly-listed symbol only
    # degrades its own edges, not the whole matrix's common window.
    pairs = []
    for i in range(len(symbols)):
        for j in range(i + 1, len(symbols)):
            si, sj = rmap[symbols[i]], rmap[symbols[j]]
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
    key = f"lu:{symbol.upper()}"
    cached = cache_get(key)
    if cached is not None:
        return cached
    url = (f"https://query1.finance.yahoo.com/v1/finance/search?q={urllib.parse.quote(symbol)}"
           f"&quotesCount=8&newsCount=0")
    out = {"symbol": symbol, "matches": []}
    try:
        data = json.loads(fetch_yahoo(url))
        for q in data.get("quotes", []):
            if q.get("quoteType") != "EQUITY":
                continue
            out["matches"].append({
                "symbol": q.get("symbol"),
                "name": q.get("longname") or q.get("shortname"),
                "sector": q.get("sector"),
                "industry": q.get("industry"),
                "exchange": q.get("exchDisp"),
            })
    except Exception as e:  # noqa: BLE001
        out["_error"] = str(e)
    cache_put(key, out, ttl=3600)
    return out


# ---------------------------------------------------------------------------
# options  (CBOE delayed chains -> most active by volume)
# ---------------------------------------------------------------------------
_OCC = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


def _parse_chain(symbol, raw):
    """Return (contracts, call_vol, put_vol) for one underlying's CBOE chain."""
    contracts = []
    call_vol = put_vol = 0.0
    data = json.loads(raw)
    body = data.get("data") or {}
    for o in body.get("options", []):
        vol = o.get("volume") or 0
        if not vol:
            continue
        m = _OCC.match(o.get("option", ""))
        if not m:
            continue
        root, ymd, cp, strike8 = m.groups()
        try:
            exp = dt.datetime.strptime(ymd, "%y%m%d").date()
        except ValueError:
            continue
        strike = int(strike8) / 1000.0
        dte = (exp - dt.date.today()).days
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
            "oi": int(o.get("open_interest") or 0),
            "last": o.get("last_trade_price"),
            "iv": round((o.get("iv") or 0) * 100, 1),
            "bid": o.get("bid"),
            "ask": o.get("ask"),
        })
    return contracts, call_vol, put_vol


def _fetch_chain(symbol):
    key = f"o:{symbol}"
    cached = cache_get(key)
    if cached is not None:
        return cached
    url = f"https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json"
    result = None
    try:
        raw = fetch(url, timeout=20)
        # capture the feed's own timestamp for the "as of" line
        ts = json.loads(raw).get("timestamp")
        contracts, cv, pv = _parse_chain(symbol, raw)
        result = {"contracts": contracts, "call_vol": cv, "put_vol": pv, "ts": ts}
    except Exception:  # noqa: BLE001 — a dead symbol shouldn't kill the board
        result = {"contracts": [], "call_vol": 0.0, "put_vol": 0.0, "ts": None}
    cache_put(key, result, ttl=300)
    return result


def get_active_options(symbols, top=25):
    key = "oa:" + ",".join(symbols) + f":{top}"
    cached = cache_get(key)
    if cached is not None:
        return cached
    all_contracts = []
    unders = {}
    as_of = None
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        for sym, res in zip(symbols, ex.map(_fetch_chain, symbols)):
            all_contracts.extend(res["contracts"])
            tot = res["call_vol"] + res["put_vol"]
            if tot > 0:
                unders[sym] = {
                    "underlying": sym,
                    "callVol": int(res["call_vol"]),
                    "putVol": int(res["put_vol"]),
                    "totalVol": int(tot),
                    "pcr": round(res["put_vol"] / res["call_vol"], 2) if res["call_vol"] else None,
                }
            as_of = as_of or res["ts"]
    all_contracts.sort(key=lambda c: c["volume"], reverse=True)
    board = sorted(unders.values(), key=lambda u: u["totalVol"], reverse=True)
    total_call = sum(u["callVol"] for u in board)
    total_put = sum(u["putVol"] for u in board)
    out = {
        "as_of": as_of,
        "delayed": True,
        "note": "Most ACTIVE by traded volume (CBOE, ~15-min delayed). Not buy/sell classified.",
        "market_pcr": round(total_put / total_call, 2) if total_call else None,
        "total_call": total_call,
        "total_put": total_put,
        "underlyings": board,
        "contracts": all_contracts[:top],
    }
    cache_put(key, out, ttl=120)
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
        if not full.startswith(BASE_DIR) or not os.path.isfile(full):
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

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        try:
            if u.path == "/api/quotes":
                syms = [s for s in (qs.get("symbols", [""])[0]).split(",") if s]
                self._send_json(get_quotes(syms) if syms else {})
            elif u.path == "/api/history":
                sym = (qs.get("symbol", [""])[0])
                rng = (qs.get("range", ["6mo"])[0])
                self._send_json(get_history(sym, rng) if sym else {"_error": "no symbol"})
            elif u.path == "/api/correlation":
                syms = [s for s in (qs.get("symbols", [""])[0]).split(",") if s]
                rng = (qs.get("range", ["6mo"])[0])
                self._send_json(get_correlation(syms, rng) if len(syms) >= 2
                                 else {"_error": "need >=2 symbols"})
            elif u.path == "/api/lookup":
                sym = (qs.get("symbol", [""])[0])
                self._send_json(get_lookup(sym) if sym else {"_error": "no symbol"})
            elif u.path == "/api/options/active":
                syms = [s for s in (qs.get("symbols", [""])[0]).split(",") if s] or OPTION_SCAN
                top = int(qs.get("top", ["25"])[0])
                self._send_json(get_active_options(syms, top))
            elif u.path in ("/", ""):
                self._send_file("index.html")
            else:
                self._send_file(u.path)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            self._send_json({"_error": str(e)}, status=500)

    def log_message(self, fmt, *args):  # quieter console
        if "/api/" in (args[0] if args else ""):
            return


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8474)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"CASCADE proxy on http://{args.host}:{args.port}  (Ctrl+C to stop)")
    print(f"  static : {BASE_DIR}")
    print(f"  quotes : Yahoo spark   |  options : CBOE delayed  |  data is DELAYED")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
