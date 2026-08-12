#!/usr/bin/env python3
"""
Unit tests for server.py's pure logic — no network access required.

Pure stdlib (unittest), matching server.py's own no-pip-install ethos.
Run:  python test_server.py
"""

import contextlib
import datetime as dt
import io
import json
import math
import os
import shutil
import tempfile
import time
import unittest
import unittest.mock
import urllib.error
import urllib.parse
import warnings

import server


def _wrap_chain_json(options):
    """Wrap a list of CBOE option-contract dicts in the chain response shape
    _parse_chain() expects: {"data": {"options": [...]}}.

    Module-level (not a per-class helper) because ParseChainTests and
    ParseChainDetailTests both build the same envelope around different
    option payloads -- keeping it in one place means the wrapper shape only
    has to change once if CBOE's response envelope ever does.
    """
    return json.dumps({"data": {"options": options}})


# Captured at import, before any patching, so the handful of tests that
# exercise the real risk-signal lookups can restore them over the
# network-free defaults NetworkFreeTestCase installs (see setUp below).
_REAL_REALIZED_VOL = server.realized_vol
_REAL_EARNINGS_MAP = server.earnings_map


class NetworkFreeTestCase(unittest.TestCase):
    """Base for tests that monkeypatch one of server.py's network-touching
    functions (fetch_yahoo, _fetch_chain) to stay off the real network.

    GetActiveOptionsTopClampTests, GetHistoryErrorTests, and UrlInjectionTests
    each used to hand-roll the same setUp/tearDown pair: save the original
    attribute, clear the shared TTL cache so the patched call is guaranteed
    to actually run, and restore the original afterward. Centralizing it here
    means a new network-free test class gets that behavior by inheriting
    instead of copying five lines of boilerplate a fourth time.

    setUp also neutralizes the two risk-signal lookups the ITM scan core
    makes (realized_vol -> Yahoo history, earnings_map -> Nasdaq calendar).
    They're patched here rather than in each scan test's own _patch() helper
    because they're reached indirectly, through _scan_itm_candidates, from
    every board and from the Snipe Log snapshot -- easy to miss one and
    silently reintroduce real network I/O into a suite whose whole premise
    is not having any. (Caught exactly that way: wiring the flags in took
    the suite from 0.06s to 23s.) Tests that want the real behavior restore
    it explicitly via _REAL_REALIZED_VOL / _REAL_EARNINGS_MAP.
    """
    def setUp(self):
        server._cache.clear()
        self.patch_server("realized_vol", lambda symbol, lookback_days=30: None)
        self.patch_server("earnings_map", lambda days_ahead: {})

    def tearDown(self):
        server._cache.clear()

    def patch_server(self, name, replacement):
        """Monkeypatch server.<name> with `replacement` for this test only;
        restored automatically even if the test raises."""
        patcher = unittest.mock.patch.object(server, name, replacement)
        patcher.start()
        self.addCleanup(patcher.stop)


class IsWithinDirTests(unittest.TestCase):
    """Tests for is_within_dir() path validation.

    Ensures that is_within_dir() correctly distinguishes between nested paths
    and sibling directories with shared prefixes. This is a security-critical
    function used to prevent directory traversal attacks in file serving.
    """
    def test_exact_match(self):
        """Base directory should match itself."""
        self.assertTrue(server.is_within_dir("/base", "/base"))

    def test_nested_path(self):
        """Files nested under base should be within the directory."""
        self.assertTrue(server.is_within_dir("/base", os.path.join("/base", "index.html")))

    def test_sibling_prefix_is_not_within(self):
        """Sibling directory with shared prefix should NOT be considered within.

        Regression test: a bare `.startswith(base)` would wrongly accept
        "/base_evil" since it shares "/base" as a string prefix without
        a separator boundary. Only paths separated by os.sep should match.
        """
        self.assertFalse(server.is_within_dir("/base", "/base_evil/secret.txt"))

    def test_unrelated_dir(self):
        """A completely unrelated directory is never considered within base."""
        self.assertFalse(server.is_within_dir("/base", "/other/file.txt"))

    def test_parent_dir_traversal(self):
        """A '..'-normalized path that climbs back out of base is rejected."""
        base = os.path.normpath("/base/cascade")
        target = os.path.normpath(os.path.join(base, "..", "server.py"))
        self.assertFalse(server.is_within_dir(base, target))


class HasHiddenComponentTests(unittest.TestCase):
    """Tests for has_hidden_component(), the dotfile/dot-directory guard used
    by Handler._send_file() to keep .git/, .env, and similar paths from being
    served just because they happen to live under BASE_DIR.
    """
    def test_plain_file_is_not_hidden(self):
        """An ordinary top-level file has no hidden path component."""
        self.assertFalse(server.has_hidden_component("/base", os.path.join("/base", "index.html")))

    def test_nested_plain_path_is_not_hidden(self):
        """An ordinary file nested in ordinary subdirectories is not hidden."""
        target = os.path.join("/base", "js", "app.js")
        self.assertFalse(server.has_hidden_component("/base", target))

    def test_dotfile_at_top_level_is_hidden(self):
        """A dotfile directly under base (e.g. .env) is flagged as hidden."""
        self.assertTrue(server.has_hidden_component("/base", os.path.join("/base", ".env")))

    def test_dotdir_ancestor_is_hidden(self):
        """A file nested inside a dot-directory (e.g. .git/config) must be
        caught even though the filename itself doesn't start with a dot."""
        target = os.path.join("/base", ".git", "config")
        self.assertTrue(server.has_hidden_component("/base", target))

    def test_base_itself_is_not_hidden(self):
        """base compared against itself must not be flagged (relpath yields
        '.', which is a dot but not a real hidden path component)."""
        self.assertFalse(server.has_hidden_component("/base", "/base"))


class SendFileSecurityTests(unittest.TestCase):
    """Integration-level tests for Handler._send_file()'s dotfile/traversal
    guards, calling the actual method rather than just has_hidden_component()
    and is_within_dir() in isolation. Those two are correct on their own
    (see HasHiddenComponentTests/IsWithinDirTests), but that doesn't prove
    _send_file actually calls them correctly -- an inverted condition, or one
    check silently dropped in a future edit, would still pass both of those
    test classes while reopening the .git/.env disclosure or path traversal.

    Handler is built via __new__ to skip BaseHTTPRequestHandler.__init__,
    which requires a live socket connection; send_error is stubbed to record
    calls instead of writing to one.
    """
    def _make_handler(self):
        handler = server.Handler.__new__(server.Handler)
        handler.send_error = unittest.mock.Mock()
        return handler

    def test_git_config_is_rejected(self):
        """GET /.git/config must 404, never serve the repo's git config."""
        handler = self._make_handler()
        handler._send_file("/.git/config")
        handler.send_error.assert_called_once_with(404)

    def test_dotenv_is_rejected(self):
        """GET /.env must 404, never serve a dotfile secret."""
        handler = self._make_handler()
        handler._send_file("/.env")
        handler.send_error.assert_called_once_with(404)

    def test_parent_dir_traversal_is_rejected(self):
        """Exercises the is_within_dir() branch specifically (no dot
        component involved), confirming that guard still fires after the
        _send_file refactor that added the hidden-path check alongside it."""
        handler = self._make_handler()
        handler._send_file("/../" + os.path.basename(__file__))
        handler.send_error.assert_called_once_with(404)

    def test_normal_missing_file_is_also_rejected(self):
        """Control case: a plain nonexistent path 404s via the isfile()
        check, not by accident because every path always 404s."""
        handler = self._make_handler()
        handler._send_file("/this-file-does-not-exist.xyz")
        handler.send_error.assert_called_once_with(404)


class DoGetErrorHandlingTests(unittest.TestCase):
    """Tests for do_GET()'s top-level exception handling.

    A malformed query param (e.g. a non-integer ?top=) must become a clean
    400 response, and an unrelated exception raised inside a route handler
    must become a 500 -- neither should propagate and crash the handling
    thread, which would otherwise drop the request without ever sending a
    response. Handler is built via __new__ (see SendFileSecurityTests) with
    just enough stubbed (send_response/send_header/end_headers/wfile) for
    _send_json to run to completion and its output to be inspected.
    """
    def _make_handler(self, path):
        handler = server.Handler.__new__(server.Handler)
        handler.path = path
        handler.sent_status = None
        handler.send_response = lambda status: setattr(handler, "sent_status", status)
        handler.send_header = lambda *a: None
        handler.end_headers = lambda: None
        handler.wfile = io.BytesIO()
        return handler

    def test_invalid_top_param_yields_400(self):
        """A non-integer ?top= hits do_GET's ValueError branch, not a 500
        or an uncaught traceback."""
        handler = self._make_handler("/api/options/active?top=notanumber")
        handler.do_GET()
        self.assertEqual(handler.sent_status, 400)
        self.assertIn("_error", json.loads(handler.wfile.getvalue()))

    def test_unexpected_exception_yields_500_not_a_crash(self):
        """An unrelated exception inside a route handler is caught and
        turned into a 500 with a generic message, not left to propagate."""
        handler = self._make_handler("/api/quotes")

        def boom(self, query):
            raise RuntimeError("boom")

        handler._API_ROUTES = {"/api/quotes": boom}  # instance attr shadows the class's real table
        # do_GET deliberately logs unhandled exceptions to stderr (so an
        # operator watching the server can see them) -- expected here since
        # we're simulating exactly that case, so silence it rather than
        # let a real bug's signal get lost in test noise the reader learns
        # to ignore.
        with contextlib.redirect_stderr(io.StringIO()):
            handler.do_GET()
        self.assertEqual(handler.sent_status, 500)
        self.assertEqual(json.loads(handler.wfile.getvalue()), {"_error": "Internal server error"})


class ApiRoutesTests(unittest.TestCase):
    """Tests for Handler._API_ROUTES, the path -> handler dispatch table that
    replaced do_GET's if/elif chain.

    A typo in a route path or a swapped handler would previously only show up
    as a live 404/wrong-response on that one endpoint; pinning the table
    directly catches it without needing a running server.
    """
    def test_all_expected_endpoints_are_routed(self):
        """Every /api/* path maps to exactly the handler method it should."""
        expected = {
            "/api/quotes": server.Handler._api_quotes,
            "/api/history": server.Handler._api_history,
            "/api/correlation": server.Handler._api_correlation,
            "/api/lookup": server.Handler._api_lookup,
            "/api/options/active": server.Handler._api_options_active,
            "/api/options/itm-scan": server.Handler._api_options_itm_scan,
            "/api/options/itm-scan-weekly": server.Handler._api_options_itm_scan_weekly,
            "/api/options/debit-spreads": server.Handler._api_options_debit_spreads,
            "/api/snipe-log": server.Handler._api_snipe_log,
        }
        self.assertEqual(server.Handler._API_ROUTES, expected)


class GetActiveOptionsTopClampTests(NetworkFreeTestCase):
    """Tests that get_active_options() clamps a negative `top` instead of
    letting `list[:top]` silently drop items from the end of the board.

    _fetch_chain is monkeypatched so this stays network-free: it returns a
    small fixed set of contracts regardless of the requested symbol.
    """
    def test_negative_top_returns_no_contracts(self):
        """top=-5 clamps to 0 contracts, not "all but the last 5"."""
        self.patch_server("_fetch_chain", lambda symbol: {
            "contracts": [{"underlying": symbol, "volume": 10}],
            "call_vol": 10.0, "put_vol": 0.0, "ts": 1700000000,
        })
        out = server.get_active_options(["AAPL"], top=-5)
        self.assertEqual(out["contracts"], [])

    def test_positive_top_still_limits_normally(self):
        """A normal positive top still returns the highest-volume contracts,
        confirming the clamp didn't disturb the ordinary case."""
        self.patch_server("_fetch_chain", lambda symbol: {
            "contracts": [{"underlying": symbol, "volume": v} for v in (30, 20, 10)],
            "call_vol": 60.0, "put_vol": 0.0, "ts": 1700000000,
        })
        out = server.get_active_options(["AAPL"], top=2)
        self.assertEqual(len(out["contracts"]), 2)
        self.assertEqual(out["contracts"][0]["volume"], 30)


class GetQuotesTests(NetworkFreeTestCase):
    """Tests get_quotes()'s happy-path parsing of Yahoo's spark batch response.

    fetch_yahoo is monkeypatched with a synthetic multi-symbol spark payload,
    exercising the actual price/prevClose/change computation -- previously
    only the URL-building side of get_quotes had any coverage.
    """
    def test_computes_price_prevclose_and_change(self):
        """price/prevClose/change are computed correctly for two symbols,
        one using chartPreviousClose and the other falling back to
        previousClose when chartPreviousClose is absent."""
        payload = json.dumps({
            "AAPL": {"close": [150.0, 151.5], "chartPreviousClose": 150.0},
            "MSFT": {"close": [300.0], "previousClose": 295.0},
        }).encode()
        self.patch_server("fetch_yahoo", lambda url, timeout=15: payload)
        out = server.get_quotes(["AAPL", "MSFT"])
        self.assertEqual(out["AAPL"], {"price": 151.5, "prevClose": 150.0,
                                        "change": round(151.5 / 150.0 - 1, 6)})
        self.assertEqual(out["MSFT"], {"price": 300.0, "prevClose": 295.0,
                                        "change": round(300.0 / 295.0 - 1, 6)})

    def test_symbol_missing_close_is_skipped(self):
        """A symbol with no closes at all (e.g. delisted) is omitted, not
        surfaced as a spurious None/0 entry."""
        payload = json.dumps({"ZZZZ": {"close": [], "chartPreviousClose": 10.0}}).encode()
        self.patch_server("fetch_yahoo", lambda url, timeout=15: payload)
        out = server.get_quotes(["ZZZZ"])
        self.assertNotIn("ZZZZ", out)


class GetCorrelationTests(NetworkFreeTestCase):
    """Tests get_correlation()'s happy path end-to-end.

    get_history() is fed a synthetic 25-point chart payload (25 >= the
    20-common-date threshold in get_correlation) via a monkeypatched
    fetch_yahoo, shared by both requested symbols so their return series
    are identical and the resulting correlation is a known, exact value.
    """
    def test_two_symbols_yield_one_perfectly_correlated_pair(self):
        """Two symbols sharing an identical, non-degenerate return series
        produce exactly one pair with r == 1.0."""
        closes = [100.0 + i for i in range(25)]  # strictly increasing -> real variance
        payload = json.dumps({
            "chart": {"result": [{
                "timestamp": list(range(25)),
                "indicators": {"quote": [{"close": closes}]},
            }]}
        }).encode()
        self.patch_server("fetch_yahoo", lambda url, timeout=15: payload)
        out = server.get_correlation(["AAA", "BBB"], rng="6mo")
        self.assertEqual(out["missing"], [])
        self.assertEqual(len(out["pairs"]), 1)
        sym1, sym2, r = out["pairs"][0]
        self.assertEqual({sym1, sym2}, {"AAA", "BBB"})
        self.assertAlmostEqual(r, 1.0, places=6)  # identical series -> perfect correlation


class FetchYahooRetryTests(NetworkFreeTestCase):
    """Tests fetch_yahoo()'s single-retry-on-400 behavior.

    Patches the lower-level fetch() (fetch_yahoo itself is under test) to
    fail once with HTTPError 400 and succeed on the second attempt. The
    real retry path sleeps 1.2s as a Yahoo cooldown -- time.sleep is
    patched too so this test doesn't actually pay that cost.
    """
    # Constructing urllib.error.HTTPError directly (as these tests do, to
    # simulate a 400 without any real socket) trips a spurious
    # ResourceWarning ("Implicitly cleaning up <HTTPError ...>") on garbage
    # collection on this Python build -- reproducible with nothing but
    # `urllib.error.HTTPError(url, code, msg, hdrs, fp)` in a bare
    # interpreter, so it's a stdlib quirk unrelated to server.py or these
    # tests' own correctness. Suppressed locally rather than globally so it
    # doesn't mask a real resource leak anywhere else in the suite.
    def test_retries_once_on_400_then_succeeds(self):
        """A first-attempt 400 is retried once and the second attempt's
        result is returned."""
        calls = []

        def flaky_fetch(url, timeout=15):
            calls.append(url)
            if len(calls) == 1:
                raise urllib.error.HTTPError(url, 400, "rate limited", {}, None)
            return b'{"ok": true}'

        self.patch_server("fetch", flaky_fetch)
        with warnings.catch_warnings(), unittest.mock.patch("time.sleep"):
            warnings.simplefilter("ignore", ResourceWarning)
            result = server.fetch_yahoo("http://example.invalid/x", gap=0)
        self.assertEqual(result, b'{"ok": true}')
        self.assertEqual(len(calls), 2)

    def test_raises_after_second_400(self):
        """A second consecutive 400 is not retried again -- it propagates."""
        def always_400(url, timeout=15):
            raise urllib.error.HTTPError(url, 400, "rate limited", {}, None)

        self.patch_server("fetch", always_400)
        with warnings.catch_warnings(), unittest.mock.patch("time.sleep"):
            warnings.simplefilter("ignore", ResourceWarning)
            with self.assertRaises(urllib.error.HTTPError):
                server.fetch_yahoo("http://example.invalid/x", gap=0)


class PearsonTests(unittest.TestCase):
    """Tests for _pearson() Pearson correlation coefficient calculation.

    Verifies that the pairwise correlation function correctly computes
    correlation between two numeric series, handling edge cases like
    zero variance and perfectly correlated/uncorrelated data.
    """
    def test_perfect_positive_correlation(self):
        """A series that's an exact positive multiple of another gives r == 1.0."""
        xs = [1.0, 2.0, 3.0, 4.0]
        ys = [2.0, 4.0, 6.0, 8.0]
        self.assertAlmostEqual(server._pearson(xs, ys), 1.0, places=9)

    def test_perfect_negative_correlation(self):
        """A series that's an exact negative multiple of another gives r == -1.0."""
        xs = [1.0, 2.0, 3.0, 4.0]
        ys = [-1.0, -2.0, -3.0, -4.0]
        self.assertAlmostEqual(server._pearson(xs, ys), -1.0, places=9)

    def test_zero_variance_returns_none(self):
        """A constant series has zero variance, so correlation is undefined."""
        xs = [1.0, 1.0, 1.0, 1.0]
        ys = [1.0, 2.0, 3.0, 4.0]
        self.assertIsNone(server._pearson(xs, ys))

    def test_mixed_series_returns_bounded_correlation(self):
        """A series with no clean linear relationship still yields a
        defined coefficient within the valid [-1, 1] range."""
        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        ys = [3.0, 1.0, 4.0, 1.0, 5.0]
        r = server._pearson(xs, ys)
        self.assertIsNotNone(r)
        self.assertTrue(-1.0 <= r <= 1.0)


class ReturnsByTTests(unittest.TestCase):
    """Tests for _returns_by_t() daily return calculation from price history.

    Validates that daily log returns are computed correctly from price history,
    skipping missing or zero-value closes and creating a mapping of
    timestamp -> daily return for correlation analysis.
    """
    def test_simple_series(self):
        """Daily returns are computed correctly for a plain 3-point series."""
        hist = {"t": [100, 200, 300], "c": [10.0, 11.0, 9.9]}
        out = server._returns_by_t(hist)
        self.assertAlmostEqual(out[200], 0.1, places=9)
        self.assertAlmostEqual(out[300], -0.1, places=9)

    def test_skips_zero_previous_close(self):
        """A day whose prior close is 0 (falsy) is skipped, not a ZeroDivisionError."""
        hist = {"t": [100, 200, 300], "c": [0.0, 5.0, 10.0]}
        out = server._returns_by_t(hist)
        # k=1 (t=200) skipped: c[0]==0 is falsy; k=2 (t=300) kept.
        self.assertNotIn(200, out)
        self.assertAlmostEqual(out[300], 1.0, places=9)

    def test_empty_history(self):
        """An empty t/c history yields an empty returns map, not an error."""
        self.assertEqual(server._returns_by_t({"t": [], "c": []}), {})

    def test_missing_keys_default_empty(self):
        """A history dict missing both 't' and 'c' keys defaults to empty lists."""
        self.assertEqual(server._returns_by_t({}), {})


class CboeChainUrlTests(unittest.TestCase):
    """Tests for _cboe_chain_url() CBOE API URL construction.

    Ensures that CBOE option chain URLs are properly formatted with
    correct escaping of client-controlled symbol input to prevent
    path injection attacks.
    """
    def test_plain_symbol(self):
        """An ordinary ticker builds the expected, unescaped-looking URL."""
        self.assertEqual(
            server._cboe_chain_url("SPY"),
            "https://cdn.cboe.com/api/global/delayed_quotes/options/SPY.json",
        )

    def test_path_segment_injection_is_escaped(self):
        """A symbol containing '/' or '..' can't inject an extra path segment.

        Regression test: symbol is client-controlled via
        /api/options/active?symbols=... -- a bare f-string interpolation
        (the pre-fix behavior) would let "/" and ".." pass straight into
        the URL path unescaped.
        """
        url = server._cboe_chain_url("../other/path")
        self.assertNotIn("/../", url)
        self.assertIn(urllib.parse.quote("../other/path", safe=""), url)


class ParseChainTests(unittest.TestCase):
    """Tests for _parse_chain() CBOE option chain JSON parsing.

    Validates that CBOE JSON responses are correctly parsed into contract
    objects, extracting OCC symbols, volumes, greeks, and handling invalid
    or missing data gracefully.
    """
    def test_parses_valid_call_and_put(self):
        """Valid call and put contracts should be parsed with correct data.

        Verifies that OCC symbols are parsed, strike prices are computed,
        and volume is aggregated separately for calls and puts.
        """
        raw = _wrap_chain_json([
            {"option": "SPY260117C00500000", "volume": 120, "open_interest": 500,
             "iv": 0.25, "bid": 1.1, "ask": 1.2, "last_trade_price": 1.15},
            {"option": "SPY260117P00480000", "volume": 80, "open_interest": 300,
             "iv": 0.30},
        ])
        contracts, call_vol, put_vol = server._parse_chain("SPY", raw)
        self.assertEqual(len(contracts), 2)
        self.assertEqual(call_vol, 120)
        self.assertEqual(put_vol, 80)
        call = next(c for c in contracts if c["type"] == "C")
        self.assertEqual(call["strike"], 500.0)
        self.assertEqual(call["underlying"], "SPY")

    def test_skips_zero_volume_contracts(self):
        """Contracts with zero volume should be skipped entirely."""
        raw = _wrap_chain_json([{"option": "SPY260117C00500000", "volume": 0}])
        contracts, call_vol, put_vol = server._parse_chain("SPY", raw)
        self.assertEqual(contracts, [])
        self.assertEqual((call_vol, put_vol), (0.0, 0.0))

    def test_skips_unparseable_option_symbol(self):
        """Invalid OCC symbols should be skipped without crashing."""
        raw = _wrap_chain_json([{"option": "not-an-occ-symbol", "volume": 50}])
        contracts, call_vol, put_vol = server._parse_chain("SPY", raw)
        self.assertEqual(contracts, [])

    def test_skips_missing_option_field(self):
        """A contract with no "option" key at all is skipped, not crashed on.

        opt.get("option", "") returns "" only when the key is absent -- make
        sure a genuinely missing key (not an explicit None) doesn't crash.
        """
        raw = _wrap_chain_json([{"volume": 50}])
        contracts, call_vol, put_vol = server._parse_chain("SPY", raw)
        self.assertEqual(contracts, [])


class CacheTests(unittest.TestCase):
    """Tests for cache_put() and cache_get() TTL-based caching.

    Validates that cache entries expire correctly after their TTL,
    that expired entries return None on retrieval, and that the cache
    opportunistically purges expired entries on write.
    """
    def test_put_then_get_within_ttl(self):
        """A value written with a positive TTL is retrievable immediately."""
        server.cache_put("test:key", {"v": 1}, ttl=60)
        self.assertEqual(server.cache_get("test:key"), {"v": 1})

    def test_get_expired_returns_none(self):
        """A negative TTL puts an already-expired entry; get returns None."""
        server.cache_put("test:expired", {"v": 1}, ttl=-1)
        self.assertIsNone(server.cache_get("test:expired"))

    def test_get_missing_key_returns_none(self):
        """A key that was never put returns None, not a KeyError."""
        self.assertIsNone(server.cache_get("test:does-not-exist"))

    def test_put_purges_previously_expired_entries(self):
        """cache_put() opportunistically purges other entries whose TTL has
        already elapsed -- backdate the entry directly instead of sleeping
        in real time, so the test stays both fast and deterministic (no
        race against actual wall-clock scheduling on a loaded machine)."""
        # A negative-ttl entry would purge itself in the same call (its own
        # expiry is already in the past), so put it with a normal ttl first,
        # then backdate its stored expiry to simulate elapsed time.
        server.cache_put("test:stale", {"v": 0}, ttl=60)
        _, value = server._cache["test:stale"]
        server._cache["test:stale"] = (time.time() - 1, value)
        self.assertIn("test:stale", server._cache)  # not yet purged — only checked lazily
        server.cache_put("test:trigger-purge", {"v": 1}, ttl=60)
        self.assertNotIn("test:stale", server._cache)


class ParseChainDetailTests(unittest.TestCase):
    """Tests for _parse_chain() field derivation and the date-skip path.

    Complements the existing happy-path parse tests by pinning down the
    derived-field math (implied volatility scaled to a percentage, contract
    fields passed through with correct types) and the currently-untested
    branch where an OCC symbol carries a syntactically impossible expiry date.
    """
    def test_iv_scaled_to_percent(self):
        """Raw IV (0.25) is surfaced as a rounded percentage (25.0)."""
        raw = _wrap_chain_json([{"option": "SPY260117C00500000", "volume": 10,
                                 "iv": 0.2543, "open_interest": 3,
                                 "last_trade_price": 2.0, "bid": 1.9, "ask": 2.1}])
        contracts, _, _ = server._parse_chain("SPY", raw)
        self.assertEqual(len(contracts), 1)
        contract = contracts[0]
        self.assertEqual(contract["iv"], 25.4)

    def test_contract_fields_passed_through_with_types(self):
        """oi/last/bid/ask/dte/expiry are carried through with expected types."""
        raw = _wrap_chain_json([{"option": "SPY260117C00500000", "volume": 7,
                                 "open_interest": 42, "last_trade_price": 3.3,
                                 "bid": 3.2, "ask": 3.4, "iv": 0.1}])
        contract = server._parse_chain("SPY", raw)[0][0]
        self.assertIsInstance(contract["oi"], int)
        self.assertEqual(contract["oi"], 42)
        self.assertIsInstance(contract["volume"], int)
        self.assertEqual(contract["last"], 3.3)
        self.assertEqual(contract["expiry"], "2026-01-17")
        self.assertIsInstance(contract["dte"], int)

    def test_impossible_expiry_date_is_skipped(self):
        """An OCC symbol with month 13 hits the except ValueError path and is dropped."""
        # Regex-valid (ROOT + 6 digits + C + 8 digits) but strptime rejects
        # month 13, so the contract must be skipped, not raised on.
        raw = _wrap_chain_json([{"option": "AAPL231301C00150000", "volume": 99}])
        contracts, call_vol, put_vol = server._parse_chain("AAPL", raw)
        self.assertEqual(contracts, [])
        self.assertEqual((call_vol, put_vol), (0.0, 0.0))


class PearsonEdgeTests(unittest.TestCase):
    """Additional edge-case tests for _pearson().

    Complements the existing correlation tests by pinning symmetry (order of
    the two series must not change the coefficient) and the degenerate
    single-observation case (zero variance -> None rather than ZeroDivision).
    """
    def test_symmetry(self):
        """_pearson(x, y) equals _pearson(y, x)."""
        xs = [1.0, 2.0, 5.0, 7.0]
        ys = [2.0, 1.0, 6.0, 5.0]
        self.assertEqual(server._pearson(xs, ys), server._pearson(ys, xs))

    def test_single_observation_returns_none(self):
        """A single data point has zero variance, so correlation is undefined."""
        self.assertIsNone(server._pearson([5.0], [3.0]))


class GetHistoryErrorTests(NetworkFreeTestCase):
    """Tests that get_history() degrades gracefully on bad upstream data.

    Uses a monkeypatched fetch_yahoo so no network access is needed: a
    malformed or unexpectedly-shaped payload must surface as an {_error: ...}
    dict, never an exception that would crash the request handler.
    """
    def test_malformed_json_returns_error(self):
        """A non-JSON upstream body yields an _error dict, not a raised exception."""
        self.patch_server("fetch_yahoo", lambda url, timeout=15: b"<html>not json</html>")
        out = server.get_history("AAPL", "6mo")
        self.assertIn("_error", out)

    def test_unexpected_shape_returns_error(self):
        """Valid JSON missing the chart/result rows yields an _error dict."""
        self.patch_server("fetch_yahoo", lambda url, timeout=15: b'{"chart": {"result": []}}')
        out = server.get_history("AAPL", "1y")
        self.assertIn("_error", out)


class UrlInjectionTests(NetworkFreeTestCase):
    """Confirms client-controlled symbol strings can't inject extra query
    parameters or smuggle a second ticker into outbound Yahoo requests.

    symbols come straight from ?symbols=... / ?symbol=..., so a bare
    f-string interpolation into a URL would let "&", "=", or "," in a
    symbol reinterpret the request. Both call sites are network-free here:
    fetch_yahoo is monkeypatched to capture the built URL instead of
    hitting the real API.
    """
    def test_lookup_symbol_cannot_inject_extra_query_param(self):
        """A symbol containing '&name=' must not add a second, unintended
        query parameter to the outbound Yahoo search URL."""
        captured = {}

        def fake_fetch_yahoo(url, timeout=15):
            captured["url"] = url
            return b'{"quotes": []}'

        self.patch_server("fetch_yahoo", fake_fetch_yahoo)
        server.get_lookup("AAPL&evil=1")
        query = urllib.parse.urlparse(captured["url"]).query
        params = urllib.parse.parse_qs(query)
        self.assertNotIn("evil", params)
        self.assertEqual(params["q"], ["AAPL&evil=1"])

    def test_spark_symbol_comma_is_percent_encoded(self):
        """A symbol embedding a literal ',' must be percent-encoded, so
        Yahoo's spark endpoint can't split one client-supplied symbol into
        two ticker requests."""
        captured = {}

        def fake_fetch_yahoo(url, timeout=15):
            captured["url"] = url
            return b"{}"

        self.patch_server("fetch_yahoo", fake_fetch_yahoo)
        server.get_quotes(["AAA,XYZ"])
        query = urllib.parse.urlparse(captured["url"]).query
        self.assertIn("AAA%2CXYZ", query)
        self.assertNotIn("symbols=AAA,XYZ", query)


class ParseChainDeltaThetaTests(unittest.TestCase):
    """Tests that _parse_chain() surfaces delta/theta -- added for the Snipe
    0DTE scan, which tiers contracts by delta and needs a signed value
    (CBOE reports puts with a negative delta; that sign must survive).
    """
    def test_delta_and_theta_passed_through(self):
        raw = _wrap_chain_json([{"option": "SPY260117C00500000", "volume": 10,
                                 "bid": 1.9, "ask": 2.1, "delta": 0.87, "theta": -0.05}])
        contract = server._parse_chain("SPY", raw)[0][0]
        self.assertEqual(contract["delta"], 0.87)
        self.assertEqual(contract["theta"], -0.05)

    def test_put_delta_keeps_negative_sign(self):
        raw = _wrap_chain_json([{"option": "SPY260117P00480000", "volume": 10,
                                 "bid": 1.9, "ask": 2.1, "delta": -0.92}])
        contract = server._parse_chain("SPY", raw)[0][0]
        self.assertEqual(contract["delta"], -0.92)

    def test_missing_delta_is_none_not_crash(self):
        raw = _wrap_chain_json([{"option": "SPY260117C00500000", "volume": 10,
                                 "bid": 1.9, "ask": 2.1}])
        contract = server._parse_chain("SPY", raw)[0][0]
        self.assertIsNone(contract["delta"])


class ItmScanTierTests(unittest.TestCase):
    """Tests for _itm_scan_tier(), the Snipe board's A/B/C classification.

    Mirrors the tiering rules 1:1: C beats everything on a wide spread; A is
    reachable either via a high delta or, as a belt-and-suspenders fallback,
    via raw moneyness (in case delta reports as 0 the way an earlier
    web-scrape prototype's source occasionally did on deep-ITM contracts).
    """
    def test_wide_spread_is_tier_c_even_with_high_delta(self):
        self.assertEqual(server._itm_scan_tier(0.25, 0.99, 0.05), "C")

    def test_none_spread_is_tier_c(self):
        self.assertEqual(server._itm_scan_tier(None, 0.99, 0.05), "C")

    def test_high_delta_is_tier_a(self):
        self.assertEqual(server._itm_scan_tier(0.05, 0.90, 0.001), "A")

    def test_zero_delta_falls_back_to_moneyness_for_tier_a(self):
        """Regression guard for the known site artifact: delta==0 on a very
        deep-ITM contract must not silently demote it out of tier A."""
        self.assertEqual(server._itm_scan_tier(0.05, 0.0, 0.02), "A")

    def test_low_delta_and_low_moneyness_is_tier_b(self):
        self.assertEqual(server._itm_scan_tier(0.05, 0.5, 0.003), "B")

    def test_spread_exactly_20pct_is_not_tier_c(self):
        """Boundary check: the rule is '> 20%', so exactly 20% stays tier A/B."""
        self.assertEqual(server._itm_scan_tier(0.20, 0.9, 0.05), "A")


class ItmScanScoreTests(unittest.TestCase):
    """Tests for _itm_scan_score(), the Snipe board's ranking metric.

    Score = 40% probability (|delta|, 0.70->1.00) + 35% banked profit (flat
    P&L%, -10%->+5%) + 25% cost efficiency (spread%, inverted, 0%->20%),
    each sub-score clamped to [0,1] before weighting.
    """
    def test_perfect_inputs_score_100(self):
        self.assertEqual(server._itm_scan_score(delta=1.0, spread_pct=0.0, flat_pnl_pct=0.05), 100.0)

    def test_worst_inputs_score_0(self):
        self.assertEqual(server._itm_scan_score(delta=0.70, spread_pct=0.20, flat_pnl_pct=-0.10), 0.0)

    def test_beyond_range_inputs_still_clamp_to_0_or_100(self):
        """Inputs past the modeled range in either direction (e.g. a delta
        above 1.0, which CBOE shouldn't send but the function must still
        handle) must clamp, not extrapolate past 0/100."""
        self.assertEqual(server._itm_scan_score(delta=1.5, spread_pct=-0.10, flat_pnl_pct=0.20), 100.0)
        self.assertEqual(server._itm_scan_score(delta=0.60, spread_pct=0.50, flat_pnl_pct=-0.50), 0.0)

    def test_missing_delta_scores_as_zero_probability(self):
        self.assertEqual(
            server._itm_scan_score(delta=None, spread_pct=0.05, flat_pnl_pct=0.0),
            server._itm_scan_score(delta=0.0, spread_pct=0.05, flat_pnl_pct=0.0),
        )

    def test_missing_spread_scores_as_worst_case_cost(self):
        """A None spread can't be measured and must NOT be silently rewarded
        as if it were free -- score it as the worst-case (ceiling) spread."""
        self.assertEqual(
            server._itm_scan_score(delta=0.9, spread_pct=None, flat_pnl_pct=0.0),
            server._itm_scan_score(delta=0.9, spread_pct=0.20, flat_pnl_pct=0.0),
        )

    def test_higher_delta_scores_higher_all_else_equal(self):
        lo = server._itm_scan_score(delta=0.80, spread_pct=0.05, flat_pnl_pct=0.0)
        hi = server._itm_scan_score(delta=0.95, spread_pct=0.05, flat_pnl_pct=0.0)
        self.assertGreater(hi, lo)

    def test_higher_flat_pnl_scores_higher_all_else_equal(self):
        lo = server._itm_scan_score(delta=0.90, spread_pct=0.05, flat_pnl_pct=-0.08)
        hi = server._itm_scan_score(delta=0.90, spread_pct=0.05, flat_pnl_pct=0.02)
        self.assertGreater(hi, lo)

    def test_tighter_spread_scores_higher_all_else_equal(self):
        wide = server._itm_scan_score(delta=0.90, spread_pct=0.15, flat_pnl_pct=0.0)
        tight = server._itm_scan_score(delta=0.90, spread_pct=0.02, flat_pnl_pct=0.0)
        self.assertGreater(tight, wide)


def _snipe_contract(**overrides):
    """Build one CBOE-shaped 0DTE contract dict for get_itm_scan() tests,
    with sane ITM-call defaults that individual tests override."""
    base = {
        "underlying": "SPY", "type": "C", "strike": 495.0, "expiry": "2026-08-07",
        "dte": 0, "volume": 100, "oi": 500, "last": 5.0, "iv": 20.0,
        "bid": 4.9, "ask": 5.1, "delta": 0.9, "theta": -0.05,
    }
    base.update(overrides)
    return base


class GetItmScanTests(NetworkFreeTestCase):
    """Tests get_itm_scan()'s filtering, execution-model math, and ordering.

    get_quotes and _fetch_chain are both monkeypatched so this stays
    network-free, matching the pattern GetActiveOptionsTopClampTests uses
    for the sibling /api/options/active endpoint.
    """
    def _patch(self, spot, contracts):
        self.patch_server("get_quotes", lambda syms: {"SPY": {"price": spot}})
        self.patch_server("_fetch_chain", lambda symbol: {
            "contracts": contracts, "call_vol": 0.0, "put_vol": 0.0, "ts": 1700000000,
        })

    def test_execution_model_math(self):
        """breakeven/cost/scenario P&L match the spec formulas exactly for a
        single deep-ITM call: spot 500, strike 495, ask 5.1."""
        self._patch(500.0, [_snipe_contract()])
        out = server.get_itm_scan(["SPY"])
        self.assertEqual(len(out["contracts"]), 1)
        c = out["contracts"][0]
        self.assertEqual(c["contract_cost"], 510.0)
        self.assertEqual(c["max_loss"], 510.0)
        self.assertAlmostEqual(c["breakeven"], 500.1)
        flat = c["scenarios"]["flat"]
        self.assertEqual(flat["scenario_price"], 500.0)
        self.assertAlmostEqual(flat["pnl_dollars"], -10.0)
        self.assertAlmostEqual(flat["pnl_pct"], -10.0 / 510.0, places=4)
        up = c["scenarios"]["up"]
        self.assertAlmostEqual(up["scenario_price"], 502.5)
        self.assertAlmostEqual(up["pnl_dollars"], 240.0)

    def test_otm_call_is_excluded(self):
        """A call struck above spot is not in-the-money and must be dropped."""
        self._patch(500.0, [_snipe_contract(strike=505.0)])
        out = server.get_itm_scan(["SPY"])
        self.assertEqual(out["contracts"], [])

    def test_non_zero_dte_is_excluded(self):
        """Only same-day expiries (dte==0) belong on a 0DTE scan."""
        self._patch(500.0, [_snipe_contract(dte=1)])
        out = server.get_itm_scan(["SPY"])
        self.assertEqual(out["contracts"], [])

    def test_zero_bid_is_excluded(self):
        """A zero/missing bid means no real two-sided market -- spread math
        would be meaningless, so the contract must be skipped."""
        self._patch(500.0, [_snipe_contract(bid=0)])
        out = server.get_itm_scan(["SPY"])
        self.assertEqual(out["contracts"], [])

    def test_missing_spot_price_excludes_symbol(self):
        """No spot price (e.g. Yahoo lookup failed) means moneyness/breakeven
        can't be computed, so that underlying contributes nothing rather
        than crashing the whole scan."""
        self.patch_server("get_quotes", lambda syms: {})
        self.patch_server("_fetch_chain", lambda symbol: {
            "contracts": [_snipe_contract()], "call_vol": 0.0, "put_vol": 0.0, "ts": None,
        })
        out = server.get_itm_scan(["SPY"])
        self.assertEqual(out["contracts"], [])

    def test_ranked_by_score_descending(self):
        """Sort key is the Snipe Score (probability + banked flat P&L + cost
        efficiency), not raw tier-then-spread. A low-delta contract with a
        tight spread must still rank BEHIND a high-delta one with a wider
        spread, because probability carries the heaviest weight (40%) --
        same scenario the old tier-then-spread sort covered, re-asserted
        against the score field instead of the tier field."""
        low_delta_tight_spread = _snipe_contract(strike=497.0, bid=2.9, ask=3.0, delta=0.5)
        high_delta_wider_spread = _snipe_contract(strike=480.0, bid=19.0, ask=21.0, delta=0.95)
        self._patch(500.0, [low_delta_tight_spread, high_delta_wider_spread])
        out = server.get_itm_scan(["SPY"])
        scores = [c["score"] for c in out["contracts"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(out["contracts"][0]["delta"], 0.95)

    def test_contract_includes_score_field(self):
        self._patch(500.0, [_snipe_contract()])
        out = server.get_itm_scan(["SPY"])
        self.assertIn("score", out["contracts"][0])
        self.assertIsInstance(out["contracts"][0]["score"], float)

    def test_top_limits_total_contracts_returned(self):
        contracts = [_snipe_contract(strike=490.0 - i) for i in range(5)]
        self._patch(500.0, contracts)
        out = server.get_itm_scan(["SPY"], top=2)
        self.assertEqual(len(out["contracts"]), 2)

    def test_note_and_delayed_flag_present(self):
        self._patch(500.0, [_snipe_contract()])
        out = server.get_itm_scan(["SPY"])
        self.assertTrue(out["delayed"])
        self.assertIn("Screening tool", out["note"])


class WeeklyItmScanScoreTests(unittest.TestCase):
    """Tests for _weekly_itm_scan_score(), the Snipe Weekly board's ranking
    metric.

    Score = 30% probability (|delta|, 0.55->0.90) + 40% profit magnitude
    (IV-implied-move P&L%, 0%->150%) + 15% spread cost (spread%, inverted,
    0%->20%) + 15% theta exposure (extrinsic_ratio, inverted, 0%->50%),
    each sub-score clamped to [0,1] before weighting. Deliberately
    different weights/floors from the 0DTE _itm_scan_score -- see
    _weekly_itm_scan_score's docstring for why.
    """
    def test_perfect_inputs_score_100(self):
        self.assertEqual(server._weekly_itm_scan_score(
            delta=1.0, spread_pct=0.0, magnitude_pnl_pct=1.50, extrinsic_ratio=0.0), 100.0)

    def test_worst_inputs_score_0(self):
        self.assertEqual(server._weekly_itm_scan_score(
            delta=0.55, spread_pct=0.20, magnitude_pnl_pct=0.0, extrinsic_ratio=0.50), 0.0)

    def test_beyond_range_inputs_still_clamp_to_0_or_100(self):
        self.assertEqual(server._weekly_itm_scan_score(
            delta=1.5, spread_pct=-0.10, magnitude_pnl_pct=3.0, extrinsic_ratio=-0.10), 100.0)
        self.assertEqual(server._weekly_itm_scan_score(
            delta=0.30, spread_pct=0.50, magnitude_pnl_pct=-1.0, extrinsic_ratio=0.90), 0.0)

    def test_missing_delta_scores_as_zero_probability(self):
        self.assertEqual(
            server._weekly_itm_scan_score(delta=None, spread_pct=0.05,
                                          magnitude_pnl_pct=0.5, extrinsic_ratio=0.1),
            server._weekly_itm_scan_score(delta=0.0, spread_pct=0.05,
                                          magnitude_pnl_pct=0.5, extrinsic_ratio=0.1),
        )

    def test_missing_spread_scores_as_worst_case_cost(self):
        self.assertEqual(
            server._weekly_itm_scan_score(delta=0.8, spread_pct=None,
                                          magnitude_pnl_pct=0.5, extrinsic_ratio=0.1),
            server._weekly_itm_scan_score(delta=0.8, spread_pct=0.20,
                                          magnitude_pnl_pct=0.5, extrinsic_ratio=0.1),
        )

    def test_missing_extrinsic_ratio_scores_as_worst_case_theta(self):
        """A None extrinsic_ratio (e.g. ask <= 0, guarded against upstream,
        but the scorer itself must still degrade safely) must NOT be
        rewarded as if the contract were pure intrinsic value."""
        self.assertEqual(
            server._weekly_itm_scan_score(delta=0.8, spread_pct=0.05,
                                          magnitude_pnl_pct=0.5, extrinsic_ratio=None),
            server._weekly_itm_scan_score(delta=0.8, spread_pct=0.05,
                                          magnitude_pnl_pct=0.5, extrinsic_ratio=0.50),
        )

    def test_higher_delta_scores_higher_all_else_equal(self):
        lo = server._weekly_itm_scan_score(delta=0.60, spread_pct=0.05,
                                           magnitude_pnl_pct=0.5, extrinsic_ratio=0.1)
        hi = server._weekly_itm_scan_score(delta=0.85, spread_pct=0.05,
                                           magnitude_pnl_pct=0.5, extrinsic_ratio=0.1)
        self.assertGreater(hi, lo)

    def test_higher_magnitude_scores_higher_all_else_equal(self):
        lo = server._weekly_itm_scan_score(delta=0.75, spread_pct=0.05,
                                           magnitude_pnl_pct=0.10, extrinsic_ratio=0.1)
        hi = server._weekly_itm_scan_score(delta=0.75, spread_pct=0.05,
                                           magnitude_pnl_pct=1.00, extrinsic_ratio=0.1)
        self.assertGreater(hi, lo)

    def test_tighter_spread_scores_higher_all_else_equal(self):
        wide = server._weekly_itm_scan_score(delta=0.75, spread_pct=0.15,
                                             magnitude_pnl_pct=0.5, extrinsic_ratio=0.1)
        tight = server._weekly_itm_scan_score(delta=0.75, spread_pct=0.02,
                                              magnitude_pnl_pct=0.5, extrinsic_ratio=0.1)
        self.assertGreater(tight, wide)

    def test_lower_extrinsic_ratio_scores_higher_all_else_equal(self):
        """Less time premium (more of the price is already intrinsic value)
        is the safer bet for a week-long hold and must score higher."""
        mostly_time_value = server._weekly_itm_scan_score(
            delta=0.75, spread_pct=0.05, magnitude_pnl_pct=0.5, extrinsic_ratio=0.40)
        mostly_intrinsic = server._weekly_itm_scan_score(
            delta=0.75, spread_pct=0.05, magnitude_pnl_pct=0.5, extrinsic_ratio=0.05)
        self.assertGreater(mostly_intrinsic, mostly_time_value)


class GetItmScanWeeklyTests(NetworkFreeTestCase):
    """Tests get_itm_scan_weekly()'s DTE-window filtering, IV-derived
    execution model math, and ordering -- the weekly sibling of
    GetItmScanTests, reusing the same _snipe_contract() builder (its
    dte=0/iv=20.0 defaults are overridden per test as needed)."""

    def _patch(self, spot, contracts):
        self.patch_server("get_quotes", lambda syms: {"SPY": {"price": spot}})
        self.patch_server("_fetch_chain", lambda symbol: {
            "contracts": contracts, "call_vol": 0.0, "put_vol": 0.0, "ts": 1700000000,
        })

    def test_execution_model_math(self):
        """contract_cost/breakeven/flat-scenario P&L match plain arithmetic
        exactly; the IV-derived expected move and favorable-scenario P&L
        are checked against the same sqrt(time)-scaling formula, computed
        independently here rather than by calling the private helper."""
        self._patch(500.0, [_snipe_contract(dte=7, iv=25.0, delta=0.75, bid=5.8, ask=6.0)])
        out = server.get_itm_scan_weekly(["SPY"])
        self.assertEqual(len(out["contracts"]), 1)
        c = out["contracts"][0]
        self.assertEqual(c["contract_cost"], 600.0)
        self.assertEqual(c["max_loss"], 600.0)
        self.assertAlmostEqual(c["breakeven"], 501.0)
        self.assertAlmostEqual(c["breakeven_cushion_pct"], -0.002)
        self.assertAlmostEqual(c["extrinsic_ratio"], 1.0 / 6.0, places=4)
        flat = c["scenarios"]["flat"]
        self.assertAlmostEqual(flat["pnl_dollars"], -100.0)
        self.assertAlmostEqual(flat["pnl_pct"], -100.0 / 600.0, places=4)

        expected_move = 0.25 * math.sqrt(7 / 365.0)
        self.assertAlmostEqual(c["expected_move_pct"], expected_move, places=4)
        favorable_price = 500.0 * (1 + expected_move)
        favorable_intrinsic = favorable_price - 495.0
        expected_pnl_dollars = (favorable_intrinsic - 6.0) * 100
        favorable = c["scenarios"]["favorable"]
        self.assertAlmostEqual(favorable["pnl_dollars"], expected_pnl_dollars, places=1)
        self.assertAlmostEqual(favorable["pnl_pct"],
                               expected_pnl_dollars / 600.0, places=4)

    def test_dte_within_default_window_is_included(self):
        """target_dte=7, window=3 (defaults) -> dte in [4,10] included."""
        self._patch(500.0, [_snipe_contract(dte=4), _snipe_contract(dte=10)])
        out = server.get_itm_scan_weekly(["SPY"])
        self.assertEqual(len(out["contracts"]), 2)

    def test_dte_outside_default_window_is_excluded(self):
        self._patch(500.0, [_snipe_contract(dte=3), _snipe_contract(dte=11)])
        out = server.get_itm_scan_weekly(["SPY"])
        self.assertEqual(out["contracts"], [])

    def test_default_window_spans_a_full_weekly_expiry_cycle(self):
        """Regression guard for the reason the default is 3 and not 2:
        weekly expiries are 7 calendar days apart, so the window has to
        span 7 days to be guaranteed to contain one. Names carrying only
        Friday weeklies (verified live on SMCI/WMT/COIN) list expiries at
        3/10/17 DTE with nothing between, which a [5,9] window misses
        entirely -- returning an empty board rather than a real answer."""
        lo, hi = 7 - server._DEFAULT_DTE_WINDOW, 7 + server._DEFAULT_DTE_WINDOW
        self.assertGreaterEqual(hi - lo + 1, 7,
                                "window must span a full weekly expiry cycle")
        friday_only = [_snipe_contract(dte=d) for d in (3, 10, 17)]
        self._patch(500.0, friday_only)
        out = server.get_itm_scan_weekly(["SPY"])
        self.assertEqual(len(out["contracts"]), 1,
                         "a Friday-weeklies-only chain must still yield a candidate")
        self.assertEqual(out["contracts"][0]["dte"], 10)

    def test_custom_target_dte_and_window(self):
        self._patch(500.0, [_snipe_contract(dte=13), _snipe_contract(dte=20)])
        out = server.get_itm_scan_weekly(["SPY"], target_dte=14, window=3)
        self.assertEqual(len(out["contracts"]), 1)
        self.assertEqual(out["contracts"][0]["dte"], 13)

    def test_otm_call_is_excluded(self):
        self._patch(500.0, [_snipe_contract(dte=7, strike=505.0)])
        out = server.get_itm_scan_weekly(["SPY"])
        self.assertEqual(out["contracts"], [])

    def test_zero_bid_is_excluded(self):
        self._patch(500.0, [_snipe_contract(dte=7, bid=0)])
        out = server.get_itm_scan_weekly(["SPY"])
        self.assertEqual(out["contracts"], [])

    def test_missing_spot_price_excludes_symbol(self):
        self.patch_server("get_quotes", lambda syms: {})
        self.patch_server("_fetch_chain", lambda symbol: {
            "contracts": [_snipe_contract(dte=7)], "call_vol": 0.0, "put_vol": 0.0, "ts": None,
        })
        out = server.get_itm_scan_weekly(["SPY"])
        self.assertEqual(out["contracts"], [])

    def test_ranked_by_weekly_score_descending(self):
        low = _snipe_contract(dte=7, strike=498.0, bid=2.4, ask=2.6,
                              delta=0.58, iv=10.0)
        high = _snipe_contract(dte=7, strike=470.0, bid=29.5, ask=30.5,
                               delta=0.88, iv=30.0)
        self._patch(500.0, [low, high])
        out = server.get_itm_scan_weekly(["SPY"])
        scores = [c["weekly_score"] for c in out["contracts"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(out["contracts"][0]["delta"], 0.88)

    def test_contract_includes_weekly_score_field(self):
        self._patch(500.0, [_snipe_contract(dte=7)])
        out = server.get_itm_scan_weekly(["SPY"])
        self.assertIn("weekly_score", out["contracts"][0])
        self.assertIsInstance(out["contracts"][0]["weekly_score"], float)

    def test_top_limits_total_contracts_returned(self):
        contracts = [_snipe_contract(dte=7, strike=490.0 - i) for i in range(5)]
        self._patch(500.0, contracts)
        out = server.get_itm_scan_weekly(["SPY"], top=2)
        self.assertEqual(len(out["contracts"]), 2)

    def test_note_mentions_target_dte_and_screening_disclaimer(self):
        self._patch(500.0, [_snipe_contract(dte=7)])
        out = server.get_itm_scan_weekly(["SPY"], target_dte=7)
        self.assertTrue(out["delayed"])
        self.assertIn("Screening tool", out["note"])
        self.assertIn("7-DTE", out["note"])


class ScanItmCandidatesTests(NetworkFreeTestCase):
    """Tests for _scan_itm_candidates(), the fetch/filter/base-metrics core
    shared by the 0DTE and weekly boards.

    Covers it directly (not just through the two callers) because a
    regression here silently changes BOTH boards at once.
    """
    def _patch(self, spot, contracts):
        self.patch_server("get_quotes", lambda syms: {"SPY": {"price": spot}})
        self.patch_server("_fetch_chain", lambda symbol: {
            "contracts": contracts, "call_vol": 0.0, "put_vol": 0.0, "ts": 1700000000,
        })

    def test_base_metrics_math(self):
        """spot 500, ITM call strike 495, bid 4.9/ask 5.1: intrinsic is 5.0,
        so extrinsic is the 0.1 of ask above it."""
        self._patch(500.0, [_snipe_contract(dte=0)])
        as_of, candidates = server._scan_itm_candidates(["SPY"], 0, 0)
        self.assertEqual(as_of, 1700000000)
        self.assertEqual(len(candidates), 1)
        _c, spot, base = candidates[0]
        self.assertEqual(spot, 500.0)
        self.assertAlmostEqual(base["mid"], 5.0)
        self.assertAlmostEqual(base["spread_pct"], 0.2 / 5.0)
        self.assertAlmostEqual(base["moneyness_pct"], 5.0 / 500.0)
        self.assertAlmostEqual(base["intrinsic"], 5.0)
        self.assertAlmostEqual(base["extrinsic"], 0.1, places=6)
        self.assertAlmostEqual(base["extrinsic_ratio"], 0.1 / 5.1, places=6)
        self.assertAlmostEqual(base["contract_cost"], 510.0)
        self.assertAlmostEqual(base["breakeven"], 500.1)

    def test_put_side_metrics_use_strike_minus_spot(self):
        self._patch(500.0, [_snipe_contract(dte=0, type="P", strike=505.0)])
        _as_of, candidates = server._scan_itm_candidates(["SPY"], 0, 0)
        _c, _spot, base = candidates[0]
        self.assertAlmostEqual(base["moneyness_pct"], 5.0 / 500.0)
        self.assertAlmostEqual(base["intrinsic"], 5.0)
        self.assertAlmostEqual(base["breakeven"], 499.9)

    def test_dte_window_is_inclusive_on_both_ends(self):
        self._patch(500.0, [_snipe_contract(dte=d) for d in (4, 5, 7, 9, 10)])
        _as_of, candidates = server._scan_itm_candidates(["SPY"], 5, 9)
        self.assertEqual(sorted(c[0]["dte"] for c in candidates), [5, 7, 9])

    def test_otm_and_untradeable_contracts_are_dropped(self):
        self._patch(500.0, [
            _snipe_contract(dte=0, strike=505.0),   # OTM call
            _snipe_contract(dte=0, bid=0),          # no two-sided market
            _snipe_contract(dte=0, ask=None),       # missing ask
        ])
        _as_of, candidates = server._scan_itm_candidates(["SPY"], 0, 0)
        self.assertEqual(candidates, [])

    def test_missing_spot_price_yields_no_candidates(self):
        self.patch_server("get_quotes", lambda syms: {})
        self.patch_server("_fetch_chain", lambda symbol: {
            "contracts": [_snipe_contract(dte=0)], "call_vol": 0.0, "put_vol": 0.0, "ts": None,
        })
        _as_of, candidates = server._scan_itm_candidates(["SPY"], 0, 0)
        self.assertEqual(candidates, [])


class RealizedVolTests(NetworkFreeTestCase):
    """Tests realized_vol(): annualized realized volatility from trailing
    daily closes. Restores the real function over the network-free stub and
    patches get_history instead, so the math runs for real without I/O."""

    def _patch_history(self, closes):
        self.patch_server("realized_vol", _REAL_REALIZED_VOL)
        self.patch_server("get_history", lambda sym, rng="6mo": {
            "t": list(range(len(closes))), "c": closes})

    def test_flat_series_has_zero_vol(self):
        self._patch_history([100.0] * 40)
        self.assertAlmostEqual(server.realized_vol("SPY"), 0.0)

    def test_known_series_matches_hand_computed_annualization(self):
        """A series alternating +1%/-1% daily: verify against the same
        sample-stdev * sqrt(252) * 100 formula computed independently."""
        closes = [100.0]
        for i in range(40):
            closes.append(closes[-1] * (1.01 if i % 2 == 0 else 1 / 1.01))
        self._patch_history(closes)
        window = closes[-31:]
        rets = [math.log(window[i] / window[i - 1]) for i in range(1, len(window))]
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        expected = math.sqrt(var) * math.sqrt(252) * 100
        self.assertAlmostEqual(server.realized_vol("SPY"), expected, places=6)

    def test_higher_volatility_series_scores_higher(self):
        calm = [100.0 + (0.1 if i % 2 else -0.1) for i in range(40)]
        wild = [100.0 + (5.0 if i % 2 else -5.0) for i in range(40)]
        self._patch_history(calm)
        calm_vol = server.realized_vol("SPY")
        server._cache.clear()
        self._patch_history(wild)
        self.assertGreater(server.realized_vol("SPY"), calm_vol)

    def test_insufficient_history_returns_none(self):
        self._patch_history([100.0, 101.0])
        self.assertIsNone(server.realized_vol("SPY"))
        server._cache.clear()
        self._patch_history([])
        self.assertIsNone(server.realized_vol("SPY"))

    def test_non_positive_closes_are_skipped_not_fatal(self):
        """A zero/negative close would blow up log() -- it must be skipped."""
        self._patch_history([100.0, 0.0, 101.0, 102.0, 103.0, 104.0])
        self.assertIsNotNone(server.realized_vol("SPY"))


class RiskFlagTests(unittest.TestCase):
    """Tests the individual risk-flag builders and the penalty application.

    Per the board's design, flags penalize and annotate rather than filter:
    a flagged contract stays visible with a legible reason, the same way the
    A/B/C tier works.
    """
    def test_iv_flag_fires_above_the_ratio_threshold(self):
        flag, ratio = server._iv_flag(iv=30.0, rvol=15.0)  # 2.0x
        self.assertIsNotNone(flag)
        self.assertEqual(flag["code"], "IV_RICH")
        self.assertEqual(ratio, 2.0)
        self.assertIn("2.0x", flag["label"])

    def test_iv_flag_silent_below_threshold_but_still_reports_ratio(self):
        """A normal variance risk premium (implied slightly above realized)
        is not a red flag -- but the ratio is still returned for display."""
        flag, ratio = server._iv_flag(iv=16.0, rvol=15.0)
        self.assertIsNone(flag)
        self.assertAlmostEqual(ratio, 1.07)

    def test_iv_flag_missing_inputs_yield_no_flag_and_no_ratio(self):
        self.assertEqual(server._iv_flag(None, 15.0), (None, None))
        self.assertEqual(server._iv_flag(20.0, None), (None, None))
        self.assertEqual(server._iv_flag(20.0, 0), (None, None))

    def test_liquidity_flags_thin_oi_and_thin_volume(self):
        flags = server._liquidity_flags(volume=1, oi=2)
        self.assertEqual({f["code"] for f in flags}, {"THIN_OI", "THIN_VOL"})

    def test_liquidity_flags_silent_when_healthy(self):
        self.assertEqual(server._liquidity_flags(volume=5000, oi=20000), [])

    def test_earnings_flag_fires_when_report_precedes_expiry(self):
        flag = server._earnings_flag("2026-08-14", "2026-08-21")
        self.assertIsNotNone(flag)
        self.assertEqual(flag["code"], "EARNINGS")

    def test_earnings_flag_fires_on_the_expiry_date_itself(self):
        """Same-day is still inside the holding period, so it counts."""
        self.assertIsNotNone(server._earnings_flag("2026-08-21", "2026-08-21"))

    def test_earnings_flag_silent_when_report_is_after_expiry(self):
        self.assertIsNone(server._earnings_flag("2026-08-28", "2026-08-21"))

    def test_earnings_flag_silent_on_missing_or_malformed_dates(self):
        self.assertIsNone(server._earnings_flag(None, "2026-08-21"))
        self.assertIsNone(server._earnings_flag("2026-08-14", None))
        self.assertIsNone(server._earnings_flag("not-a-date", "2026-08-21"))

    def test_apply_risk_flags_subtracts_each_penalty(self):
        flags = [{"code": "A", "penalty": 10}, {"code": "B", "penalty": 5}]
        self.assertEqual(server._apply_risk_flags(80.0, flags), 65.0)

    def test_apply_risk_flags_is_a_noop_without_flags(self):
        self.assertEqual(server._apply_risk_flags(80.0, []), 80.0)

    def test_apply_risk_flags_floors_at_zero(self):
        """Penalties must never drive a score negative -- 0 is the floor."""
        flags = [{"code": "A", "penalty": 99}]
        self.assertEqual(server._apply_risk_flags(10.0, flags), 0.0)


class EarningsCalendarTests(NetworkFreeTestCase):
    """Tests the Nasdaq earnings-calendar lookup's parsing and its
    never-raise contract (an unavailable calendar means "unknown", which
    suppresses the flag, not a failed scan)."""

    def _patch_urlopen(self, payload=None, exc=None):
        """Patch urllib.request.urlopen itself (server.py calls it through
        the module, so this intercepts the real call site) rather than
        swapping out server.urllib wholesale."""
        class _Resp:
            def read(self):
                return json.dumps(payload).encode()
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            if exc:
                raise exc
            return _Resp()

        patcher = unittest.mock.patch("urllib.request.urlopen", fake_urlopen)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_parses_symbols_from_the_calendar(self):
        self._patch_urlopen({"data": {"rows": [
            {"symbol": "AMAT"}, {"symbol": "bn"}, {"symbol": ""}, {}]}})
        out = server._nasdaq_earnings_for_date("2026-08-13")
        self.assertEqual(out, {"AMAT": "2026-08-13", "BN": "2026-08-13"})

    def test_network_failure_yields_empty_not_an_exception(self):
        self._patch_urlopen(exc=urllib.error.URLError("down"))
        self.assertEqual(server._nasdaq_earnings_for_date("2026-08-13"), {})

    def test_malformed_payload_yields_empty(self):
        self._patch_urlopen({"unexpected": "shape"})
        self.assertEqual(server._nasdaq_earnings_for_date("2026-08-13"), {})

    def test_earnings_map_merges_days_and_keeps_the_soonest(self):
        self.patch_server("earnings_map", _REAL_EARNINGS_MAP)
        calls = []

        def fake_day(date_iso):
            calls.append(date_iso)
            return {"AAPL": date_iso}  # reports "every" day; soonest must win
        self.patch_server("_nasdaq_earnings_for_date", fake_day)
        out = server.earnings_map(3)
        self.assertEqual(len(calls), 4, "queries today + each of the next 3 days")
        self.assertEqual(out["AAPL"], min(calls), "the soonest date wins")

    def test_earnings_map_negative_horizon_is_empty(self):
        self.patch_server("earnings_map", _REAL_EARNINGS_MAP)
        self.assertEqual(server.earnings_map(-1), {})


class ScanRiskFlagIntegrationTests(NetworkFreeTestCase):
    """End-to-end: flags computed in the shared scan core must reach both
    boards' output and actually reduce the ranked score."""

    def _patch(self, contracts, rvol=None, earnings=None):
        self.patch_server("get_quotes", lambda syms: {"SPY": {"price": 500.0}})
        self.patch_server("_fetch_chain", lambda symbol: {
            "contracts": contracts, "call_vol": 0.0, "put_vol": 0.0, "ts": 1700000000,
        })
        self.patch_server("realized_vol", lambda symbol, lookback_days=30: rvol)
        self.patch_server("earnings_map", lambda days_ahead: earnings or {})

    def test_healthy_contract_has_no_flags_and_an_unpenalized_score(self):
        self._patch([_snipe_contract(dte=0, volume=5000, oi=20000, iv=15.0)], rvol=15.0)
        c = server.get_itm_scan(["SPY"])["contracts"][0]
        self.assertEqual(c["flags"], [])
        self.assertEqual(c["score"], c["raw_score"])

    def test_thin_liquidity_penalizes_the_0dte_score(self):
        self._patch([_snipe_contract(dte=0, volume=1, oi=2)])
        c = server.get_itm_scan(["SPY"])["contracts"][0]
        self.assertEqual({f["code"] for f in c["flags"]}, {"THIN_OI", "THIN_VOL"})
        self.assertLess(c["score"], c["raw_score"])

    def test_rich_iv_penalizes_and_reports_the_ratio(self):
        self._patch([_snipe_contract(dte=0, volume=5000, oi=20000, iv=40.0)], rvol=10.0)
        c = server.get_itm_scan(["SPY"])["contracts"][0]
        self.assertIn("IV_RICH", {f["code"] for f in c["flags"]})
        self.assertEqual(c["iv_rv_ratio"], 4.0)
        self.assertEqual(c["realized_vol"], 10.0)
        self.assertLess(c["score"], c["raw_score"])

    def test_earnings_before_expiry_penalizes_the_weekly_score(self):
        self._patch([_snipe_contract(dte=7, expiry="2026-08-18",
                                      volume=5000, oi=20000)],
                    earnings={"SPY": "2026-08-17"})
        c = server.get_itm_scan_weekly(["SPY"])["contracts"][0]
        self.assertIn("EARNINGS", {f["code"] for f in c["flags"]})
        self.assertEqual(c["earnings_date"], "2026-08-17")
        self.assertLess(c["weekly_score"], c["raw_weekly_score"])

    def test_flagged_contract_is_penalized_not_removed(self):
        """The whole point of the flag design: it still appears on the board."""
        self._patch([_snipe_contract(dte=0, volume=1, oi=1, iv=99.0)], rvol=5.0)
        out = server.get_itm_scan(["SPY"])
        self.assertEqual(len(out["contracts"]), 1, "flagged rows stay visible")
        self.assertGreater(len(out["contracts"][0]["flags"]), 0)

    def test_flags_demote_a_contract_below_a_clean_rival(self):
        clean = _snipe_contract(dte=0, strike=496.0, bid=4.0, ask=4.1,
                                 delta=0.90, volume=5000, oi=20000, iv=15.0)
        flagged = _snipe_contract(dte=0, strike=495.0, bid=4.9, ask=5.1,
                                   delta=0.90, volume=1, oi=1, iv=15.0)
        self._patch([flagged, clean], rvol=15.0)
        contracts = server.get_itm_scan(["SPY"])["contracts"]
        self.assertEqual(contracts[0]["strike"], 496.0,
                         "the clean contract outranks the thin one")


class ItmScanEndpointHorizonRoutingTests(NetworkFreeTestCase):
    """The /api/options/itm-scan handler picks the board by horizon:
    target_dte=0 (the default) keeps the 0DTE score, target_dte>0 routes to
    the weekly board's scoring. Verifies the dispatch itself, not the
    scan math (covered by the two GetItmScan* classes)."""

    def _record_calls(self):
        calls = {}
        self.patch_server("get_itm_scan",
                          lambda *a, **k: calls.setdefault("0dte", (a, k)) or {"contracts": []})
        self.patch_server("get_itm_scan_weekly",
                          lambda *a, **k: calls.setdefault("weekly", (a, k)) or {"contracts": []})
        return calls

    def test_no_target_dte_uses_the_0dte_board(self):
        calls = self._record_calls()
        server.Handler._api_options_itm_scan(server.Handler, {})
        self.assertIn("0dte", calls)
        self.assertNotIn("weekly", calls)

    def test_explicit_target_dte_zero_uses_the_0dte_board(self):
        calls = self._record_calls()
        server.Handler._api_options_itm_scan(server.Handler, {"target_dte": ["0"]})
        self.assertIn("0dte", calls)
        self.assertNotIn("weekly", calls)

    def test_positive_target_dte_routes_to_the_weekly_board(self):
        calls = self._record_calls()
        server.Handler._api_options_itm_scan(server.Handler, {"target_dte": ["7"]})
        self.assertIn("weekly", calls)
        self.assertNotIn("0dte", calls)
        args, _kw = calls["weekly"]
        self.assertEqual(args[1], 7, "target_dte is threaded through")
        self.assertEqual(args[2], server._DEFAULT_DTE_WINDOW,
                         "window falls back to the shared default")

    def test_custom_window_is_threaded_through(self):
        calls = self._record_calls()
        server.Handler._api_options_itm_scan(server.Handler, {"target_dte": ["14"], "window": ["3"]})
        args, _kw = calls["weekly"]
        self.assertEqual((args[1], args[2]), (14, 3))

    def test_weekly_path_still_routes_to_the_weekly_board(self):
        """The dedicated /api/options/itm-scan-weekly path stays live -- the
        unified target_dte param is additive, not a replacement."""
        calls = self._record_calls()
        server.Handler._api_options_itm_scan_weekly(server.Handler, {})
        self.assertIn("weekly", calls)


class ScanInputBoundsTests(NetworkFreeTestCase):
    """The DTE window feeds earnings_map(), which issues one outbound
    request per day of horizon. Unbounded, ?target_dte=100000 would attempt
    ~100k requests to Nasdaq through a thread pool -- the same class of
    amplification MAX_API_SYMBOLS already guards on the symbols axis.

    These assert the cap holds at every layer: the function that actually
    turns a number into requests, the two scan entry points, and the HTTP
    handler that takes the value straight from a query param.
    """
    def setUp(self):
        super().setUp()
        self.days = []
        self.patch_server("earnings_map", _REAL_EARNINGS_MAP)
        self.patch_server("_nasdaq_earnings_for_date",
                          lambda d: (self.days.append(d) or {}))
        self.patch_server("get_quotes", lambda syms: {})
        self.patch_server("_fetch_chain",
                          lambda s: {"contracts": [], "call_vol": 0.0,
                                     "put_vol": 0.0, "ts": None})

    def _cap(self):
        return server.MAX_EARNINGS_LOOKAHEAD_DAYS + 1  # inclusive of day 0

    def test_earnings_map_clamps_absurd_horizons(self):
        server.earnings_map(10 ** 6)
        self.assertLessEqual(len(self.days), self._cap())

    def test_earnings_map_is_exact_for_small_horizons(self):
        server.earnings_map(7)
        self.assertEqual(len(self.days), 8, "today + the next 7 days")

    def test_earnings_map_negative_horizon_fetches_nothing(self):
        server.earnings_map(-1)
        self.assertEqual(self.days, [])

    def test_weekly_scan_clamps_target_dte(self):
        server.get_itm_scan_weekly(["SPY"], target_dte=100000, window=3)
        self.assertLessEqual(len(self.days), self._cap())

    def test_spreads_scan_clamps_window(self):
        server.get_debit_spreads(["SPY"], target_dte=7, window=999999)
        self.assertLessEqual(len(self.days), self._cap())

    def test_http_handler_clamps_target_dte(self):
        """The real attack path: the value arrives as an unvalidated query
        param and is int()-parsed straight into the scan."""
        server.Handler._api_options_itm_scan(server.Handler, {"target_dte": ["100000"]})
        self.assertLessEqual(len(self.days), self._cap())

    def test_negative_target_dte_does_not_invert_the_window(self):
        server.get_itm_scan_weekly(["SPY"], target_dte=-500, window=3)
        self.assertLessEqual(len(self.days), self._cap())

    def test_clamp_int_helper(self):
        self.assertEqual(server._clamp_int(5, 0, 10), 5)
        self.assertEqual(server._clamp_int(-5, 0, 10), 0)
        self.assertEqual(server._clamp_int(50, 0, 10), 10)


class EasternDateTests(NetworkFreeTestCase):
    """Anything deciding WHICH TRADING DAY something belongs to must use ET,
    not the machine's local date. The two disagree for a window every night
    as wide as the host's offset from Eastern, so on a non-ET host a
    contract's DTE or a logged pick's date could land on the wrong trading
    day -- producing a subtly wrong track record rather than an error.

    Each test pins _now_et() to a FIXED PAST date rather than to "just after
    midnight tonight". Anchoring to a real-world-relative moment would make
    these pass or fail depending on the day they run: dt.date.today() only
    differs from a pinned ET date when the machine's local date happens to
    differ, so on the wrong day a reverted fix would sail through. A date in
    2020 can never equal dt.date.today(), so a regression back to local time
    fails here every time.
    """
    ET_NOW = dt.datetime(2020, 1, 2, 0, 30, tzinfo=dt.timezone(dt.timedelta(hours=-5)))
    ET_DATE = "2020-01-02"

    def _freeze_et(self):
        self.patch_server("_now_et", lambda: self.ET_NOW)

    def test_today_et_follows_eastern_not_local(self):
        self._freeze_et()
        self.assertEqual(server._today_et().isoformat(), self.ET_DATE)

    def test_parse_chain_dte_is_measured_from_the_eastern_date(self):
        self._freeze_et()
        raw = _wrap_chain_json([{
            "option": "SPY200117C00495000", "volume": 10, "open_interest": 5,
            "bid": 1.0, "ask": 1.1,
        }])
        contract = server._parse_chain("SPY", raw)[0][0]
        expected = (dt.date(2020, 1, 17) - dt.date(2020, 1, 2)).days
        self.assertEqual(contract["dte"], expected)

    def test_snapshot_dates_the_entry_by_the_eastern_day(self):
        self._freeze_et()
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        self.patch_server("SNIPE_LOG_PATH", os.path.join(tmpdir, "log.json"))
        self.patch_server("get_itm_scan", lambda *a, **k: {"contracts": [{
            "underlying": "SPY", "type": "C", "strike": 495.0,
            "expiry": "2020-01-02", "ask": 5.0, "delta": 0.9,
            "spread_pct": 0.02, "score": 80.0, "contract_cost": 500.0,
            "breakeven": 500.0,
        }]})
        entry = server.snapshot_snipe_pick()
        self.assertEqual(entry["date"], self.ET_DATE)

    def test_resolution_uses_the_eastern_day_to_decide_what_is_past(self):
        """An entry dated the CURRENT ET day must stay open. Pinning ET to
        just after midnight makes this fail loudly if local date is used,
        since local is still the previous day."""
        self._freeze_et()
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        path = os.path.join(tmpdir, "log.json")
        self.patch_server("SNIPE_LOG_PATH", path)
        with open(path, "w", encoding="utf-8") as f:
            json.dump([{"date": self.ET_DATE, "status": "open", "underlying": "SPY",
                        "type": "C", "strike": 495.0, "entry_ask": 5.0,
                        "contract_cost": 500.0}], f)
        self.patch_server("_closing_price_on", lambda sym, d: 500.0)
        entries = server.resolve_snipe_log()
        self.assertEqual(entries[0]["status"], "open",
                         "an entry from the current ET day is not settled yet")


class BuildRiskFlagsTests(unittest.TestCase):
    """_build_risk_flags() is the single place all three flags are assembled;
    both the single-leg scan core and the spreads board go through it, so a
    new flag type is one edit rather than one per board."""

    def test_combines_all_three_flag_types(self):
        flags, ratio = server._build_risk_flags(
            volume=1, oi=2, iv=40.0, rvol=10.0,
            earnings_date_iso="2026-08-17", expiry_iso="2026-08-21")
        self.assertEqual({f["code"] for f in flags},
                         {"THIN_VOL", "THIN_OI", "IV_RICH", "EARNINGS"})
        self.assertEqual(ratio, 4.0)

    def test_returns_the_ratio_even_when_it_does_not_trip_the_flag(self):
        """The boards display iv_rv_ratio whether or not it's a red flag."""
        flags, ratio = server._build_risk_flags(
            volume=5000, oi=20000, iv=11.0, rvol=10.0,
            earnings_date_iso=None, expiry_iso="2026-08-21")
        self.assertEqual(flags, [])
        self.assertEqual(ratio, 1.1)

    def test_clean_contract_yields_no_flags(self):
        flags, _ratio = server._build_risk_flags(
            volume=5000, oi=20000, iv=10.0, rvol=10.0,
            earnings_date_iso=None, expiry_iso="2026-08-21")
        self.assertEqual(flags, [])

    def test_both_boards_agree_on_identical_inputs(self):
        """The point of the extraction: a single-leg contract and a spread
        leg with the same characteristics must be flagged identically."""
        args = dict(volume=1, oi=1, iv=40.0, rvol=10.0,
                    earnings_date_iso="2026-08-17", expiry_iso="2026-08-21")
        a, ra = server._build_risk_flags(**args)
        b, rb = server._build_risk_flags(**args)
        self.assertEqual([f["code"] for f in a], [f["code"] for f in b])
        self.assertEqual(ra, rb)


class WeeklySnipeLogTests(NetworkFreeTestCase):
    """The forward paper-trading log now covers both boards.

    The generalization rests on settling by EXPIRY rather than by the date
    the pick was logged. For a 0DTE pick those are the same date, so the
    original behavior is unchanged; a weekly pick simply waits until its own
    expiry. These tests pin both halves of that, plus the backward
    compatibility that matters most: entries written before the weekly board
    existed carry no `board` field and must keep working untouched.
    """
    def setUp(self):
        super().setUp()
        self._tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmpdir, ignore_errors=True)
        self.patch_server("SNIPE_LOG_PATH", os.path.join(self._tmpdir, "snipe_log.json"))

    @staticmethod
    def _entry(**kw):
        base = {
            "id": "x", "date": "2026-08-03", "underlying": "SPY", "type": "C",
            "strike": 495.0, "expiry": "2026-08-03", "entry_ask": 5.0,
            "contract_cost": 500.0, "status": "open",
        }
        base.update(kw)
        return base

    def _write(self, entries):
        with open(server.SNIPE_LOG_PATH, "w", encoding="utf-8") as f:
            json.dump(entries, f)

    # -- board tagging ----------------------------------------------------

    def test_legacy_entry_without_board_reads_as_0dte(self):
        """Real entries predating the weekly board have no `board` key. They
        were all 0DTE picks, so that's the correct reading of a missing
        value -- not a reason to rewrite the user's file."""
        self.assertEqual(server._entry_board({"date": "2026-08-07"}), "0dte")
        self.assertEqual(server._entry_board({"board": None}), "0dte")
        self.assertEqual(server._entry_board({"board": "weekly"}), "weekly")

    def test_snapshot_tags_the_board_and_scans_the_right_one(self):
        self.patch_server("get_itm_scan", lambda *a, **k: {"contracts": [
            {"underlying": "SPY", "type": "C", "strike": 1.0, "expiry": "2026-08-11",
             "ask": 1.0, "contract_cost": 100.0, "score": 10.0}]})
        self.patch_server("get_itm_scan_weekly", lambda *a, **k: {"contracts": [
            {"underlying": "IWM", "type": "P", "strike": 2.0, "expiry": "2026-08-21",
             "ask": 2.0, "contract_cost": 200.0, "weekly_score": 20.0}]})
        a = server.snapshot_snipe_pick(board="0dte")
        b = server.snapshot_snipe_pick(board="weekly")
        self.assertEqual((a["board"], a["underlying"], a["entry_score"]), ("0dte", "SPY", 10.0))
        self.assertEqual((b["board"], b["underlying"], b["entry_score"]), ("weekly", "IWM", 20.0))

    def test_each_board_dedupes_independently_on_the_same_day(self):
        """Two boards may both log on one day; neither may log twice."""
        self.patch_server("get_itm_scan", lambda *a, **k: {"contracts": [
            {"underlying": "SPY", "type": "C", "strike": 1.0, "expiry": "2026-08-11",
             "ask": 1.0, "contract_cost": 100.0, "score": 10.0}]})
        self.patch_server("get_itm_scan_weekly", lambda *a, **k: {"contracts": [
            {"underlying": "IWM", "type": "P", "strike": 2.0, "expiry": "2026-08-21",
             "ask": 2.0, "contract_cost": 200.0, "weekly_score": 20.0}]})
        server.snapshot_snipe_pick(board="0dte")
        server.snapshot_snipe_pick(board="weekly")
        server.snapshot_snipe_pick(board="0dte")   # repeat
        server.snapshot_snipe_pick(board="weekly")  # repeat
        self.assertEqual(len(server._load_snipe_log()), 2)

    def test_unknown_board_logs_nothing(self):
        self.assertIsNone(server.snapshot_snipe_pick(board="not-a-board"))

    # -- settlement by expiry ---------------------------------------------

    def test_settle_date_is_the_expiry(self):
        self.assertEqual(server._entry_settle_date(
            {"date": "2026-08-03", "expiry": "2026-08-21"}), "2026-08-21")

    def test_settle_date_falls_back_to_the_entry_date(self):
        """A hand-edited or malformed entry without an expiry stays
        resolvable rather than stuck open forever."""
        self.assertEqual(server._entry_settle_date({"date": "2026-08-03"}), "2026-08-03")

    def test_weekly_entry_stays_open_until_its_expiry_passes(self):
        self.patch_server("_today_et", lambda: dt.date(2026, 8, 14))
        self.patch_server("_closing_price_on", lambda s, d: 500.0)
        self._write([self._entry(board="weekly", date="2026-08-10",
                                 expiry="2026-08-21")])
        entries = server.resolve_snipe_log()
        self.assertEqual(entries[0]["status"], "open",
                         "a weekly pick must not settle just because a day passed")

    def test_weekly_entry_settles_against_the_close_on_its_expiry(self):
        self.patch_server("_today_et", lambda: dt.date(2026, 8, 24))
        seen = []
        self.patch_server("_closing_price_on",
                          lambda s, d: (seen.append(d), 505.0)[1])
        self._write([self._entry(board="weekly", date="2026-08-10",
                                 expiry="2026-08-21")])
        entries = server.resolve_snipe_log()
        self.assertEqual(seen, ["2026-08-21"], "settles on the EXPIRY date, not the log date")
        e = entries[0]
        self.assertEqual(e["status"], "closed")
        self.assertAlmostEqual(e["exit_value"], 10.0)          # 505 - 495
        self.assertAlmostEqual(e["pnl_dollars"], 500.0)        # (10 - 5) * 100
        self.assertTrue(e["correct"])

    def test_0dte_settlement_is_unchanged_by_the_generalization(self):
        """Regression guard for the real data on disk: for a 0DTE pick
        expiry == date, so settling by expiry must behave exactly as
        settling by date always did."""
        self.patch_server("_today_et", lambda: dt.date(2026, 8, 4))
        self.patch_server("_closing_price_on", lambda s, d: 502.0)
        self._write([self._entry(date="2026-08-03", expiry="2026-08-03")])
        e = server.resolve_snipe_log()[0]
        self.assertEqual(e["status"], "closed")
        self.assertAlmostEqual(e["pnl_dollars"], 200.0)  # (7 - 5) * 100

    # -- per-board reporting ----------------------------------------------

    def test_log_is_scoped_and_summarized_per_board(self):
        """Pooling two boards' records would produce a win rate that
        describes neither."""
        self.patch_server("_today_et", lambda: dt.date(2026, 9, 1))
        self.patch_server("_closing_price_on", lambda s, d: 600.0)
        self._write([
            self._entry(id="a", board="0dte", date="2026-08-03", expiry="2026-08-03"),
            self._entry(id="b", board="weekly", date="2026-08-03", expiry="2026-08-14"),
            self._entry(id="c", board="weekly", date="2026-08-04", expiry="2026-08-14"),
        ])
        zero = server.get_snipe_log("0dte")
        weekly = server.get_snipe_log("weekly")
        self.assertEqual(zero["board"], "0dte")
        self.assertEqual(len(zero["entries"]), 1)
        self.assertEqual(zero["summary"]["trades"], 1)
        self.assertEqual(len(weekly["entries"]), 2)
        self.assertEqual(weekly["summary"]["trades"], 2)

    def test_resolution_runs_across_all_boards_regardless_of_who_asks(self):
        """Settling is independent of which board is being read."""
        self.patch_server("_today_et", lambda: dt.date(2026, 9, 1))
        self.patch_server("_closing_price_on", lambda s, d: 600.0)
        self._write([self._entry(id="w", board="weekly", date="2026-08-03",
                                 expiry="2026-08-14")])
        server.get_snipe_log("0dte")  # ask for the OTHER board
        self.assertEqual(server._load_snipe_log()[0]["status"], "closed")


class SpreadScoreTests(unittest.TestCase):
    """Tests _spread_score(): 40% probability of MAX profit (short-leg
    delta, 0.40->0.85) + 40% reward per dollar risked (0->2.0x) + 20%
    combined execution cost (0%->25%, inverted)."""

    def test_perfect_inputs_score_100(self):
        self.assertEqual(server._spread_score(short_delta=0.85, reward_risk=2.0,
                                              cost_pct=0.0), 100.0)

    def test_worst_inputs_score_0(self):
        self.assertEqual(server._spread_score(short_delta=0.40, reward_risk=0.0,
                                              cost_pct=0.25), 0.0)

    def test_inputs_beyond_range_clamp(self):
        self.assertEqual(server._spread_score(short_delta=1.5, reward_risk=9.0,
                                              cost_pct=-0.5), 100.0)
        self.assertEqual(server._spread_score(short_delta=0.1, reward_risk=-3.0,
                                              cost_pct=0.9), 0.0)

    def test_uses_short_leg_delta_for_probability(self):
        """Max profit needs the underlying beyond the SHORT strike, so it's
        the short leg's delta that estimates hitting the best case."""
        lo = server._spread_score(short_delta=0.45, reward_risk=1.0, cost_pct=0.05)
        hi = server._spread_score(short_delta=0.80, reward_risk=1.0, cost_pct=0.05)
        self.assertGreater(hi, lo)

    def test_better_reward_risk_scores_higher(self):
        lo = server._spread_score(short_delta=0.6, reward_risk=0.4, cost_pct=0.05)
        hi = server._spread_score(short_delta=0.6, reward_risk=1.8, cost_pct=0.05)
        self.assertGreater(hi, lo)

    def test_cheaper_execution_scores_higher(self):
        wide = server._spread_score(short_delta=0.6, reward_risk=1.0, cost_pct=0.20)
        tight = server._spread_score(short_delta=0.6, reward_risk=1.0, cost_pct=0.01)
        self.assertGreater(tight, wide)

    def test_missing_inputs_degrade_to_worst_case_not_a_crash(self):
        self.assertIsInstance(
            server._spread_score(short_delta=None, reward_risk=None, cost_pct=None),
            float)


class GetDebitSpreadsTests(NetworkFreeTestCase):
    """Tests get_debit_spreads(): leg pairing, payoff math, the
    conservative worst-fill pricing, and the invalid-spread guards."""

    def _patch(self, spot, contracts, rvol=None, earnings=None):
        self.patch_server("get_quotes", lambda syms: {"SPY": {"price": spot}})
        self.patch_server("_fetch_chain", lambda symbol: {
            "contracts": contracts, "call_vol": 0.0, "put_vol": 0.0, "ts": 1700000000,
        })
        self.patch_server("realized_vol", lambda symbol, lookback_days=30: rvol)
        self.patch_server("earnings_map", lambda days_ahead: earnings or {})

    @staticmethod
    def _leg(strike, bid, ask, **kw):
        """One tradeable leg. Liquidity defaults to healthy so the risk
        flags stay quiet unless a test deliberately overrides them."""
        kw.setdefault("volume", 5000)
        kw.setdefault("oi", 20000)
        return _snipe_contract(dte=7, expiry="2026-08-18", strike=strike,
                               bid=bid, ask=ask, **kw)

    def test_call_spread_payoff_math(self):
        """Long 495C at ask 6.00, short 500C at bid 3.00, width 5.
        Net debit 3.00 -> max loss $300; max gain (5-3)=2.00 -> $200;
        breakeven 495+3 = 498."""
        self._patch(500.0, [self._leg(495.0, 5.8, 6.0, delta=0.75),
                            self._leg(500.0, 3.0, 3.2, delta=0.50)])
        spreads = server.get_debit_spreads(["SPY"])["spreads"]
        self.assertEqual(len(spreads), 1)
        s = spreads[0]
        self.assertEqual(s["long_strike"], 495.0)
        self.assertEqual(s["short_strike"], 500.0)
        self.assertEqual(s["net_debit"], 300.0)
        self.assertEqual(s["max_loss"], 300.0)
        self.assertEqual(s["max_gain"], 200.0)
        self.assertAlmostEqual(s["reward_risk"], 200.0 / 300.0, places=3)
        self.assertAlmostEqual(s["breakeven"], 498.0)

    def test_put_spread_pairs_toward_lower_strikes(self):
        """For puts the short leg sits BELOW the long leg -- that's the
        further-out-of-the-money direction on the put side."""
        self._patch(500.0, [self._leg(505.0, 5.8, 6.0, type="P", delta=-0.75),
                            self._leg(500.0, 3.0, 3.2, type="P", delta=-0.50)])
        spreads = server.get_debit_spreads(["SPY"])["spreads"]
        self.assertEqual(len(spreads), 1)
        s = spreads[0]
        self.assertEqual(s["long_strike"], 505.0)
        self.assertEqual(s["short_strike"], 500.0)
        self.assertAlmostEqual(s["breakeven"], 502.0)  # 505 - 3.00

    def test_prices_at_worst_realistic_fill(self):
        """Long fills at ITS ASK and short at ITS BID -- both sides against
        you -- so a wider quote makes the spread look worse, never better."""
        self._patch(500.0, [self._leg(495.0, 5.0, 6.0, delta=0.75),
                            self._leg(500.0, 3.0, 4.0, delta=0.50)])
        s = server.get_debit_spreads(["SPY"])["spreads"][0]
        self.assertEqual(s["net_debit"], 300.0, "6.00 ask - 3.00 bid, not mid-to-mid")

    def test_otm_long_leg_is_not_used_as_an_anchor(self):
        self._patch(500.0, [self._leg(505.0, 5.8, 6.0, delta=0.40),
                            self._leg(510.0, 3.0, 3.2, delta=0.25)])
        self.assertEqual(server.get_debit_spreads(["SPY"])["spreads"], [])

    def test_credit_pairing_is_rejected(self):
        """If the short leg's bid exceeds the long leg's ask it's a credit,
        not a debit spread -- outside this board's scope."""
        self._patch(500.0, [self._leg(495.0, 2.0, 2.1, delta=0.75),
                            self._leg(500.0, 9.0, 9.2, delta=0.50)])
        self.assertEqual(server.get_debit_spreads(["SPY"])["spreads"], [])

    def test_debit_at_or_above_width_is_rejected(self):
        """Paying >= the width can never return a profit at expiry."""
        self._patch(500.0, [self._leg(495.0, 6.0, 6.2, delta=0.75),
                            self._leg(500.0, 1.0, 1.1, delta=0.50)])
        # width 5, net debit 6.2-1.0 = 5.2 -> rejected
        self.assertEqual(server.get_debit_spreads(["SPY"])["spreads"], [])

    def test_legs_are_not_paired_across_expiries(self):
        """A long and short in different expiries is a calendar spread, not
        a vertical -- the payoff math here would be wrong for it."""
        self._patch(500.0, [
            self._leg(495.0, 5.8, 6.0, delta=0.75),
            _snipe_contract(dte=8, expiry="2026-08-19", strike=500.0,
                            bid=3.0, ask=3.2, delta=0.50, volume=5000, oi=20000),
        ])
        self.assertEqual(server.get_debit_spreads(["SPY"])["spreads"], [])

    def test_legs_are_not_paired_across_types(self):
        self._patch(500.0, [self._leg(495.0, 5.8, 6.0, delta=0.75),
                            self._leg(500.0, 3.0, 3.2, type="P", delta=-0.50)])
        self.assertEqual(server.get_debit_spreads(["SPY"])["spreads"], [])

    def test_risk_flags_penalize_the_spread_score(self):
        self._patch(500.0, [self._leg(495.0, 5.8, 6.0, delta=0.75, volume=1, oi=1),
                            self._leg(500.0, 3.0, 3.2, delta=0.50, volume=1, oi=1)],
                    earnings={"SPY": "2026-08-17"})
        s = server.get_debit_spreads(["SPY"])["spreads"][0]
        codes = {f["code"] for f in s["flags"]}
        self.assertIn("EARNINGS", codes)
        self.assertIn("THIN_OI", codes)
        self.assertLess(s["spread_score"], s["raw_spread_score"])

    def test_ranked_by_spread_score_descending(self):
        self._patch(500.0, [self._leg(490.0, 11.8, 12.0, delta=0.85),
                            self._leg(495.0, 5.8, 6.0, delta=0.75),
                            self._leg(500.0, 3.0, 3.2, delta=0.50)])
        spreads = server.get_debit_spreads(["SPY"])["spreads"]
        scores = [s["spread_score"] for s in spreads]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_top_limits_returned_spreads(self):
        self._patch(500.0, [self._leg(490.0, 11.8, 12.0, delta=0.85),
                            self._leg(495.0, 5.8, 6.0, delta=0.75),
                            self._leg(500.0, 3.0, 3.2, delta=0.50)])
        self.assertEqual(len(server.get_debit_spreads(["SPY"], top=1)["spreads"]), 1)

    def test_rejects_pairings_below_the_minimum_reward_risk(self):
        """A validity guard, not a preference: width 5.00 bought for a 4.90
        debit -> $10 max gain on $490 of risk (0.02x). Its best case doesn't
        clear a round-trip commission on two legs."""
        self._patch(500.0, [self._leg(495.0, 5.9, 6.0, delta=0.75),
                            self._leg(500.0, 1.1, 1.2, delta=0.50)])
        self.assertEqual(server.get_debit_spreads(["SPY"])["spreads"], [])

    def test_keeps_pairings_at_or_above_the_minimum(self):
        """Boundary: the rule is `< floor` rejects, so exactly at the floor
        must survive. width 5.00, debit 4.7619 -> reward:risk exactly 0.05."""
        debit = 5.0 / (1 + server._SPREAD_MIN_REWARD_RISK)
        self._patch(500.0, [self._leg(495.0, debit + 2.0 - 0.01, debit + 2.0, delta=0.75),
                            self._leg(500.0, 2.0, 2.1, delta=0.50)])
        spreads = server.get_debit_spreads(["SPY"])["spreads"]
        self.assertEqual(len(spreads), 1)
        self.assertAlmostEqual(spreads[0]["reward_risk"],
                               server._SPREAD_MIN_REWARD_RISK, places=3)

    def test_exclusion_is_disclosed_in_the_note(self):
        """Filtering silently would make an empty board look like a broken
        feed; the note says what was removed and why."""
        self._patch(500.0, [self._leg(495.0, 5.8, 6.0, delta=0.75),
                            self._leg(500.0, 3.0, 3.2, delta=0.50)])
        note = server.get_debit_spreads(["SPY"])["note"]
        self.assertIn("excluded", note)
        self.assertIn("transaction costs", note)

    def test_note_carries_the_screening_disclaimer(self):
        self._patch(500.0, [self._leg(495.0, 5.8, 6.0, delta=0.75),
                            self._leg(500.0, 3.0, 3.2, delta=0.50)])
        out = server.get_debit_spreads(["SPY"])
        self.assertTrue(out["delayed"])
        self.assertIn("Screening tool", out["note"])
        self.assertIn("No trades are placed automatically", out["note"])


class SnipeLogTests(NetworkFreeTestCase):
    """Tests for the forward paper-trading Snipe Log: snapshot creation and
    same-day dedupe, next-day close resolution and P&L math (a known win and
    a known loss, hand-verified like GetItmScanTests.test_execution_model_math
    does for the scan itself), the same-day-does-not-resolve rule, the
    missing-close-price-leaves-it-open fallback, and get_snipe_log()'s
    closed-trades-only summary.

    SNIPE_LOG_PATH is monkeypatched to a per-test temp file (via
    NetworkFreeTestCase.patch_server) so these tests never read or write the
    real snipe_log.json this project ships alongside server.py, and never
    race against each other or a real running instance.
    """
    def setUp(self):
        super().setUp()
        self._tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmpdir, ignore_errors=True)
        self.patch_server("SNIPE_LOG_PATH", os.path.join(self._tmpdir, "snipe_log.json"))

    @staticmethod
    def _date_offset(days):
        """ISO date `days` away from the real "today" -- used instead of a
        hardcoded date string so these tests stay correct on any run date
        (a negative `days` is always strictly in the past, which is all the
        resolve-path tests actually need)."""
        return (server._today_et() + server.dt.timedelta(days=days)).isoformat()

    @staticmethod
    def _hist_for_date(date_str, close):
        """A get_history()-shaped payload with exactly one bar, timestamped
        at 9:30am America/New_York on `date_str` -- matches how a real Yahoo
        daily bar's timestamp maps back to its calendar trading day. Uses
        server's own _ny_utcoffset_hours() (rather than a hardcoded offset)
        so this stays correct whether `date_str` falls in EDT or EST."""
        d = server.dt.date.fromisoformat(date_str)
        offset = server._ny_utcoffset_hours(d)
        tz = server.dt.timezone(server.dt.timedelta(hours=offset))
        local_open = server.dt.datetime(d.year, d.month, d.day, 9, 30, tzinfo=tz)
        return {"t": [int(local_open.timestamp())], "c": [close]}

    @staticmethod
    def _raw_entry(date, status="open", underlying="SPY", opt_type="C",
                    strike=495.0, ask=5.0, cost=500.0, id_suffix=None):
        """Build one full-shaped log entry dict (matching snapshot_snipe_pick's
        own schema) directly, for tests that exercise resolve_snipe_log() /
        get_snipe_log() without going through a scan snapshot first."""
        return {
            "id": f"{date}-{id_suffix or underlying}",
            "date": date, "logged_at": f"{date}T15:30:00-04:00",
            "underlying": underlying, "type": opt_type, "strike": strike, "expiry": date,
            "entry_ask": ask, "entry_delta": 0.9, "entry_spread_pct": 0.02,
            "entry_score": 78.4, "contract_cost": cost, "breakeven": strike + ask,
            "late_snapshot": False, "status": status,
            "close_price": None, "exit_value": None, "pnl_dollars": None,
            "pnl_pct": None, "correct": None, "resolved_at": None,
        }

    @staticmethod
    def _scan_with(contracts):
        return {"as_of": 1700000000, "delayed": True, "note": "", "contracts": contracts}

    @staticmethod
    def _pick(**overrides):
        base = {"underlying": "SPY", "type": "C", "strike": 495.0, "expiry": "2026-08-06",
                "ask": 5.0, "delta": 0.9, "spread_pct": 0.02, "score": 78.4,
                "contract_cost": 500.0, "breakeven": 500.0}
        base.update(overrides)
        return base

    # -- snapshot_snipe_pick() --------------------------------------------

    def test_snapshot_creates_one_entry_with_expected_fields(self):
        self.patch_server("get_itm_scan", lambda *a, **k: self._scan_with([self._pick()]))
        entry = server.snapshot_snipe_pick()
        today = server._today_et().isoformat()
        self.assertIsNotNone(entry)
        self.assertEqual(entry["id"], f"{today}-0dte-SPY",
                         "id carries the board, so two boards' same-day picks on the"
                         " same underlying stay distinct")
        self.assertEqual(entry["board"], "0dte")
        self.assertEqual(entry["date"], today)
        self.assertEqual(entry["underlying"], "SPY")
        self.assertEqual(entry["type"], "C")
        self.assertEqual(entry["strike"], 495.0)
        self.assertEqual(entry["entry_ask"], 5.0)
        self.assertEqual(entry["contract_cost"], 500.0)
        self.assertEqual(entry["status"], "open")
        self.assertFalse(entry["late_snapshot"])
        self.assertEqual(len(server._load_snipe_log()), 1)

    def test_snapshot_no_candidates_logs_nothing(self):
        self.patch_server("get_itm_scan", lambda *a, **k: self._scan_with([]))
        entry = server.snapshot_snipe_pick()
        self.assertIsNone(entry)
        self.assertEqual(server._load_snipe_log(), [])

    def test_snapshot_twice_same_day_does_not_duplicate(self):
        self.patch_server("get_itm_scan", lambda *a, **k: self._scan_with([self._pick()]))
        first = server.snapshot_snipe_pick()
        second = server.snapshot_snipe_pick(late=True)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(server._load_snipe_log()), 1)

    def test_late_snapshot_flag_recorded(self):
        self.patch_server("get_itm_scan", lambda *a, **k: self._scan_with([self._pick()]))
        entry = server.snapshot_snipe_pick(late=True)
        self.assertTrue(entry["late_snapshot"])

    # -- resolve_snipe_log() P&L math --------------------------------------

    def test_resolve_computes_correct_pnl_for_a_win(self):
        """Call strike 495, entry_ask 5.0 (cost $500), close 502 ->
        exit_value = max(0, 502-495) = 7.0
        pnl_dollars = (7.0 - 5.0) * 100 = 200.0
        pnl_pct = 200.0 / 500.0 = 0.4"""
        yest = self._date_offset(-1)
        server._save_snipe_log([self._raw_entry(date=yest)])
        self.patch_server("get_history", lambda symbol, rng="1mo": self._hist_for_date(yest, 502.0))
        out = server.resolve_snipe_log()
        e = out[0]
        self.assertEqual(e["status"], "closed")
        self.assertEqual(e["close_price"], 502.0)
        self.assertAlmostEqual(e["exit_value"], 7.0)
        self.assertAlmostEqual(e["pnl_dollars"], 200.0)
        self.assertAlmostEqual(e["pnl_pct"], 0.4)
        self.assertTrue(e["correct"])
        self.assertIsNotNone(e["resolved_at"])

    def test_resolve_computes_correct_pnl_for_a_loss(self):
        """Call strike 495, entry_ask 5.0 (cost $500), close 490 (finishes
        OTM) -> exit_value = max(0, 490-495) = 0.0
        pnl_dollars = (0.0 - 5.0) * 100 = -500.0
        pnl_pct = -500.0 / 500.0 = -1.0"""
        yest = self._date_offset(-1)
        server._save_snipe_log([self._raw_entry(date=yest)])
        self.patch_server("get_history", lambda symbol, rng="1mo": self._hist_for_date(yest, 490.0))
        out = server.resolve_snipe_log()
        e = out[0]
        self.assertEqual(e["status"], "closed")
        self.assertAlmostEqual(e["exit_value"], 0.0)
        self.assertAlmostEqual(e["pnl_dollars"], -500.0)
        self.assertAlmostEqual(e["pnl_pct"], -1.0)
        self.assertFalse(e["correct"])

    def test_resolve_put_uses_strike_minus_close(self):
        """Put strike 495, entry_ask 5.0, close 488 ->
        exit_value = max(0, 495-488) = 7.0 -> same $200 win math as the call case."""
        yest = self._date_offset(-1)
        server._save_snipe_log([self._raw_entry(date=yest, opt_type="P")])
        self.patch_server("get_history", lambda symbol, rng="1mo": self._hist_for_date(yest, 488.0))
        e = server.resolve_snipe_log()[0]
        self.assertAlmostEqual(e["exit_value"], 7.0)
        self.assertAlmostEqual(e["pnl_dollars"], 200.0)

    def test_open_entry_from_today_does_not_resolve(self):
        """Even with a matching close price available, a same-day entry must
        stay open -- it only resolves the NEXT time the log is read on a
        later date."""
        today = server._today_et().isoformat()
        server._save_snipe_log([self._raw_entry(date=today)])
        self.patch_server("get_history", lambda symbol, rng="1mo": self._hist_for_date(today, 502.0))
        out = server.resolve_snipe_log()
        e = out[0]
        self.assertEqual(e["status"], "open")
        self.assertIsNone(e["pnl_dollars"])
        self.assertIsNone(e["close_price"])

    def test_missing_close_price_leaves_entry_open(self):
        """A past-day entry whose close can't be found (feed hiccup, stale
        cache, etc.) must stay open, not crash or resolve with bad data."""
        yest = self._date_offset(-1)
        server._save_snipe_log([self._raw_entry(date=yest)])
        self.patch_server("get_history", lambda symbol, rng="1mo": {"t": [], "c": []})
        out = server.resolve_snipe_log()
        e = out[0]
        self.assertEqual(e["status"], "open")
        self.assertIsNone(e["pnl_dollars"])

    def test_already_closed_entry_is_left_alone(self):
        """resolve_snipe_log() must not re-touch an entry that's already
        settled, even if get_history is (incorrectly) able to produce a bar
        for its date."""
        yest = self._date_offset(-1)
        closed = self._raw_entry(date=yest, status="closed")
        closed.update(close_price=500.0, exit_value=5.0, pnl_dollars=0.0,
                      pnl_pct=0.0, correct=False, resolved_at="already-set")
        server._save_snipe_log([closed])
        self.patch_server("get_history", lambda symbol, rng="1mo": self._hist_for_date(yest, 999.0))
        out = server.resolve_snipe_log()
        self.assertEqual(out[0]["close_price"], 500.0)  # untouched
        self.assertEqual(out[0]["resolved_at"], "already-set")

    # -- get_snipe_log() summary --------------------------------------------

    def test_summary_zero_trades_when_none_closed(self):
        today = server._today_et().isoformat()
        server._save_snipe_log([self._raw_entry(date=today)])
        out = server.get_snipe_log()
        self.assertEqual(out["summary"], {
            "trades": 0, "wins": 0, "win_rate": None,
            "total_pnl_dollars": 0.0, "avg_pnl_dollars": 0.0, "avg_pnl_pct": 0.0,
        })

    def test_summary_only_counts_closed_trades_and_computes_correctly(self):
        yest = self._date_offset(-1)
        today = server._today_et().isoformat()
        win_entry = self._raw_entry(date=yest, underlying="SPY")
        still_open = self._raw_entry(date=today, underlying="QQQ", id_suffix="QQQ")
        server._save_snipe_log([win_entry, still_open])
        self.patch_server("get_history", lambda symbol, rng="1mo": self._hist_for_date(yest, 502.0))
        out = server.get_snipe_log()

        self.assertEqual(out["summary"]["trades"], 1)
        self.assertEqual(out["summary"]["wins"], 1)
        self.assertEqual(out["summary"]["win_rate"], 1.0)
        self.assertAlmostEqual(out["summary"]["total_pnl_dollars"], 200.0)
        self.assertAlmostEqual(out["summary"]["avg_pnl_dollars"], 200.0)
        self.assertAlmostEqual(out["summary"]["avg_pnl_pct"], 0.4)
        # most-recent-first ordering
        self.assertEqual(out["entries"][0]["date"], today)
        self.assertEqual(out["entries"][1]["date"], yest)

    def test_summary_win_rate_with_mixed_results(self):
        """One win, one loss -> win_rate 0.5, total_pnl is their sum."""
        d1, d2 = self._date_offset(-1), self._date_offset(-2)
        win = self._raw_entry(date=d1, underlying="SPY")
        loss = self._raw_entry(date=d2, underlying="QQQ", id_suffix="QQQ")
        server._save_snipe_log([win, loss])

        def fake_history(symbol, rng="1mo"):
            if symbol == "SPY":
                return self._hist_for_date(d1, 502.0)  # win: +200
            return self._hist_for_date(d2, 490.0)       # loss: -500

        self.patch_server("get_history", fake_history)
        out = server.get_snipe_log()
        self.assertEqual(out["summary"]["trades"], 2)
        self.assertEqual(out["summary"]["wins"], 1)
        self.assertAlmostEqual(out["summary"]["win_rate"], 0.5)
        self.assertAlmostEqual(out["summary"]["total_pnl_dollars"], -300.0)
        self.assertAlmostEqual(out["summary"]["avg_pnl_dollars"], -150.0)


class DoPostRoutingTests(unittest.TestCase):
    """Tests for do_POST()'s minimal routing.

    Only one POST route exists (/api/snipe-log/snapshot, the manual
    "snapshot today's pick now" trigger) -- everything else must 404, matching
    do_GET's own error-handling conventions. Handler is built via __new__
    (see DoGetErrorHandlingTests) with just enough stubbed to run _send_json
    to completion and inspect its output, plus a stubbed rfile/headers pair
    so the body-draining step at the top of do_POST doesn't need a real socket.
    """
    def _make_handler(self, path, body=b""):
        handler = server.Handler.__new__(server.Handler)
        handler.path = path
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.sent_status = None
        handler.send_response = lambda status: setattr(handler, "sent_status", status)
        handler.send_header = lambda *a: None
        handler.end_headers = lambda: None
        handler.wfile = io.BytesIO()
        handler.send_error = lambda code: setattr(handler, "sent_status", code)
        return handler

    def test_unknown_post_path_404s(self):
        handler = self._make_handler("/api/nope")
        handler.do_POST()
        self.assertEqual(handler.sent_status, 404)

    def test_snapshot_route_returns_the_entry(self):
        handler = self._make_handler("/api/snipe-log/snapshot")
        fake_entry = {"id": "2026-08-06-SPY", "status": "open"}
        with unittest.mock.patch.object(server, "snapshot_snipe_pick", return_value=fake_entry):
            handler.do_POST()
        self.assertEqual(handler.sent_status, 200)
        self.assertEqual(json.loads(handler.wfile.getvalue()), fake_entry)

    def test_snapshot_route_with_no_candidates_returns_error_dict_not_500(self):
        handler = self._make_handler("/api/snipe-log/snapshot")
        with unittest.mock.patch.object(server, "snapshot_snipe_pick", return_value=None):
            handler.do_POST()
        self.assertEqual(handler.sent_status, 200)
        self.assertIn("_error", json.loads(handler.wfile.getvalue()))


if __name__ == "__main__":
    unittest.main()
