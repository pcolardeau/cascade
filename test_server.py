#!/usr/bin/env python3
"""
Unit tests for server.py's pure logic — no network access required.

Pure stdlib (unittest), matching server.py's own no-pip-install ethos.
Run:  python test_server.py
"""

import contextlib
import io
import json
import os
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


class NetworkFreeTestCase(unittest.TestCase):
    """Base for tests that monkeypatch one of server.py's network-touching
    functions (fetch_yahoo, _fetch_chain) to stay off the real network.

    GetActiveOptionsTopClampTests, GetHistoryErrorTests, and UrlInjectionTests
    each used to hand-roll the same setUp/tearDown pair: save the original
    attribute, clear the shared TTL cache so the patched call is guaranteed
    to actually run, and restore the original afterward. Centralizing it here
    means a new network-free test class gets that behavior by inheriting
    instead of copying five lines of boilerplate a fourth time.
    """
    def setUp(self):
        server._cache.clear()

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

    def test_tier_a_sorts_before_tier_b_regardless_of_spread(self):
        """Sort is tier-A-first, then spread ascending -- a tighter-spread
        tier-B contract must still rank behind a wider-spread tier-A one."""
        tier_b = _snipe_contract(strike=497.0, bid=2.9, ask=3.0, delta=0.5)  # tight spread, low delta/moneyness -> B
        tier_a = _snipe_contract(strike=480.0, bid=19.0, ask=21.0, delta=0.95)  # wider spread, high delta -> A
        self._patch(500.0, [tier_b, tier_a])
        out = server.get_itm_scan(["SPY"])
        self.assertEqual([c["tier"] for c in out["contracts"]], ["A", "B"])

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


if __name__ == "__main__":
    unittest.main()
