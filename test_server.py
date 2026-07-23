#!/usr/bin/env python3
"""
Unit tests for server.py's pure logic — no network access required.

Pure stdlib (unittest), matching server.py's own no-pip-install ethos.
Run:  python test_server.py
"""

import json
import os
import time
import unittest
import urllib.parse

import server


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
        self.assertFalse(server.is_within_dir("/base", "/other/file.txt"))

    def test_parent_dir_traversal(self):
        base = os.path.normpath("/base/cascade")
        target = os.path.normpath(os.path.join(base, "..", "server.py"))
        self.assertFalse(server.is_within_dir(base, target))


class PearsonTests(unittest.TestCase):
    """Tests for _pearson() Pearson correlation coefficient calculation.

    Verifies that the pairwise correlation function correctly computes
    correlation between two numeric series, handling edge cases like
    zero variance and perfectly correlated/uncorrelated data.
    """
    def test_perfect_positive_correlation(self):
        xs = [1.0, 2.0, 3.0, 4.0]
        ys = [2.0, 4.0, 6.0, 8.0]
        self.assertAlmostEqual(server._pearson(xs, ys), 1.0, places=9)

    def test_perfect_negative_correlation(self):
        xs = [1.0, 2.0, 3.0, 4.0]
        ys = [-1.0, -2.0, -3.0, -4.0]
        self.assertAlmostEqual(server._pearson(xs, ys), -1.0, places=9)

    def test_zero_variance_returns_none(self):
        xs = [1.0, 1.0, 1.0, 1.0]
        ys = [1.0, 2.0, 3.0, 4.0]
        self.assertIsNone(server._pearson(xs, ys))

    def test_uncorrelated_ish(self):
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
        hist = {"t": [100, 200, 300], "c": [10.0, 11.0, 9.9]}
        out = server._returns_by_t(hist)
        self.assertAlmostEqual(out[200], 0.1, places=9)
        self.assertAlmostEqual(out[300], -0.1, places=9)

    def test_skips_zero_previous_close(self):
        hist = {"t": [100, 200, 300], "c": [0.0, 5.0, 10.0]}
        out = server._returns_by_t(hist)
        # k=1 (t=200) skipped: c[0]==0 is falsy; k=2 (t=300) kept.
        self.assertNotIn(200, out)
        self.assertAlmostEqual(out[300], 1.0, places=9)

    def test_empty_history(self):
        self.assertEqual(server._returns_by_t({"t": [], "c": []}), {})

    def test_missing_keys_default_empty(self):
        self.assertEqual(server._returns_by_t({}), {})


class CboeChainUrlTests(unittest.TestCase):
    """Tests for _cboe_chain_url() CBOE API URL construction.

    Ensures that CBOE option chain URLs are properly formatted with
    correct escaping of client-controlled symbol input to prevent
    path injection attacks.
    """
    def test_plain_symbol(self):
        self.assertEqual(
            server._cboe_chain_url("SPY"),
            "https://cdn.cboe.com/api/global/delayed_quotes/options/SPY.json",
        )

    def test_path_segment_injection_is_escaped(self):
        # Regression test: symbol is client-controlled via
        # /api/options/active?symbols=... — a bare f-string interpolation
        # (the pre-fix behavior) would let "/" and ".." pass straight into
        # the URL path unescaped.
        url = server._cboe_chain_url("../other/path")
        self.assertNotIn("/../", url)
        self.assertIn(urllib.parse.quote("../other/path", safe=""), url)


class ParseChainTests(unittest.TestCase):
    """Tests for _parse_chain() CBOE option chain JSON parsing.

    Validates that CBOE JSON responses are correctly parsed into contract
    objects, extracting OCC symbols, volumes, greeks, and handling invalid
    or missing data gracefully.
    """
    def _raw(self, options):
        """Wrap options list in CBOE JSON response structure for testing.

        Args:
            options (list): List of option contract dicts (option, volume, etc).

        Returns:
            str: JSON-encoded CBOE response with wrapped options data.
        """
        return json.dumps({"data": {"options": options}})

    def test_parses_valid_call_and_put(self):
        """Valid call and put contracts should be parsed with correct data.

        Verifies that OCC symbols are parsed, strike prices are computed,
        and volume is aggregated separately for calls and puts.
        """
        raw = self._raw([
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
        raw = self._raw([{"option": "SPY260117C00500000", "volume": 0}])
        contracts, call_vol, put_vol = server._parse_chain("SPY", raw)
        self.assertEqual(contracts, [])
        self.assertEqual((call_vol, put_vol), (0.0, 0.0))

    def test_skips_unparseable_option_symbol(self):
        """Invalid OCC symbols should be skipped without crashing."""
        raw = self._raw([{"option": "not-an-occ-symbol", "volume": 50}])
        contracts, call_vol, put_vol = server._parse_chain("SPY", raw)
        self.assertEqual(contracts, [])

    def test_skips_missing_option_field(self):
        # o.get("option", "") returns "" only when the key is absent — make
        # sure a genuinely missing key (not an explicit None) doesn't crash.
        raw = self._raw([{"volume": 50}])
        contracts, call_vol, put_vol = server._parse_chain("SPY", raw)
        self.assertEqual(contracts, [])


class CacheTests(unittest.TestCase):
    """Tests for cache_put() and cache_get() TTL-based caching.

    Validates that cache entries expire correctly after their TTL,
    that expired entries return None on retrieval, and that the cache
    opportunistically purges expired entries on write.
    """
    def test_put_then_get_within_ttl(self):
        server.cache_put("test:key", {"v": 1}, ttl=60)
        self.assertEqual(server.cache_get("test:key"), {"v": 1})

    def test_get_expired_returns_none(self):
        server.cache_put("test:expired", {"v": 1}, ttl=-1)
        self.assertIsNone(server.cache_get("test:expired"))

    def test_get_missing_key_returns_none(self):
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
    def _raw(self, options):
        return json.dumps({"data": {"options": options}})

    def test_iv_scaled_to_percent(self):
        """Raw IV (0.25) is surfaced as a rounded percentage (25.0)."""
        raw = self._raw([{"option": "SPY260117C00500000", "volume": 10,
                          "iv": 0.2543, "open_interest": 3,
                          "last_trade_price": 2.0, "bid": 1.9, "ask": 2.1}])
        contracts, _, _ = server._parse_chain("SPY", raw)
        self.assertEqual(len(contracts), 1)
        c = contracts[0]
        self.assertEqual(c["iv"], 25.4)

    def test_contract_fields_passed_through_with_types(self):
        """oi/last/bid/ask/dte/expiry are carried through with expected types."""
        raw = self._raw([{"option": "SPY260117C00500000", "volume": 7,
                          "open_interest": 42, "last_trade_price": 3.3,
                          "bid": 3.2, "ask": 3.4, "iv": 0.1}])
        c = server._parse_chain("SPY", raw)[0][0]
        self.assertIsInstance(c["oi"], int)
        self.assertEqual(c["oi"], 42)
        self.assertIsInstance(c["volume"], int)
        self.assertEqual(c["last"], 3.3)
        self.assertEqual(c["expiry"], "2026-01-17")
        self.assertIsInstance(c["dte"], int)

    def test_impossible_expiry_date_is_skipped(self):
        """An OCC symbol with month 13 hits the except ValueError path and is dropped."""
        # Regex-valid (ROOT + 6 digits + C + 8 digits) but strptime rejects
        # month 13, so the contract must be skipped, not raised on.
        raw = self._raw([{"option": "AAPL231301C00150000", "volume": 99}])
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


class GetHistoryErrorTests(unittest.TestCase):
    """Tests that get_history() degrades gracefully on bad upstream data.

    Uses a monkeypatched fetch_yahoo so no network access is needed: a
    malformed or unexpectedly-shaped payload must surface as an {_error: ...}
    dict, never an exception that would crash the request handler.
    """
    def setUp(self):
        self._orig = server.fetch_yahoo
        server._cache.clear()  # bypass the 900s history cache so the patch is hit

    def tearDown(self):
        server.fetch_yahoo = self._orig
        server._cache.clear()

    def test_malformed_json_returns_error(self):
        """A non-JSON upstream body yields an _error dict, not a raised exception."""
        server.fetch_yahoo = lambda url, timeout=15: b"<html>not json</html>"
        out = server.get_history("AAPL", "6mo")
        self.assertIn("_error", out)

    def test_unexpected_shape_returns_error(self):
        """Valid JSON missing the chart/result rows yields an _error dict."""
        server.fetch_yahoo = lambda url, timeout=15: b'{"chart": {"result": []}}'
        out = server.get_history("AAPL", "1y")
        self.assertIn("_error", out)


class UrlInjectionTests(unittest.TestCase):
    """Confirms client-controlled symbol strings can't inject extra query
    parameters or smuggle a second ticker into outbound Yahoo requests.

    symbols come straight from ?symbols=... / ?symbol=..., so a bare
    f-string interpolation into a URL would let "&", "=", or "," in a
    symbol reinterpret the request. Both call sites are network-free here:
    fetch_yahoo is monkeypatched to capture the built URL instead of
    hitting the real API.
    """

    def setUp(self):
        self._orig_fetch_yahoo = server.fetch_yahoo
        server._cache.clear()

    def tearDown(self):
        server.fetch_yahoo = self._orig_fetch_yahoo
        server._cache.clear()

    def test_lookup_symbol_cannot_inject_extra_query_param(self):
        """A symbol containing '&name=' must not add a second, unintended
        query parameter to the outbound Yahoo search URL."""
        captured = {}

        def fake_fetch_yahoo(url, timeout=15):
            captured["url"] = url
            return b'{"quotes": []}'

        server.fetch_yahoo = fake_fetch_yahoo
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

        server.fetch_yahoo = fake_fetch_yahoo
        server.get_quotes(["AAA,XYZ"])
        query = urllib.parse.urlparse(captured["url"]).query
        self.assertIn("AAA%2CXYZ", query)
        self.assertNotIn("symbols=AAA,XYZ", query)


if __name__ == "__main__":
    unittest.main()
