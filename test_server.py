#!/usr/bin/env python3
"""
Unit tests for server.py's pure logic — no network access required.

Pure stdlib (unittest), matching server.py's own no-pip-install ethos.
Run:  python test_server.py
"""

import json
import os
import unittest

import server


class IsWithinDirTests(unittest.TestCase):
    def test_exact_match(self):
        self.assertTrue(server.is_within_dir("/base", "/base"))

    def test_nested_path(self):
        self.assertTrue(server.is_within_dir("/base", os.path.join("/base", "index.html")))

    def test_sibling_prefix_is_not_within(self):
        # Regression test: a bare `.startswith(base)` would wrongly accept
        # this, since "/base_evil" shares "/base" as a string prefix without
        # a separator boundary.
        self.assertFalse(server.is_within_dir("/base", "/base_evil/secret.txt"))

    def test_unrelated_dir(self):
        self.assertFalse(server.is_within_dir("/base", "/other/file.txt"))

    def test_parent_dir_traversal(self):
        base = os.path.normpath("/base/cascade")
        target = os.path.normpath(os.path.join(base, "..", "server.py"))
        self.assertFalse(server.is_within_dir(base, target))


class PearsonTests(unittest.TestCase):
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


class ParseChainTests(unittest.TestCase):
    def _raw(self, options):
        return json.dumps({"data": {"options": options}})

    def test_parses_valid_call_and_put(self):
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
        raw = self._raw([{"option": "SPY260117C00500000", "volume": 0}])
        contracts, call_vol, put_vol = server._parse_chain("SPY", raw)
        self.assertEqual(contracts, [])
        self.assertEqual((call_vol, put_vol), (0.0, 0.0))

    def test_skips_unparseable_option_symbol(self):
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
    def test_put_then_get_within_ttl(self):
        server.cache_put("test:key", {"v": 1}, ttl=60)
        self.assertEqual(server.cache_get("test:key"), {"v": 1})

    def test_get_expired_returns_none(self):
        server.cache_put("test:expired", {"v": 1}, ttl=-1)
        self.assertIsNone(server.cache_get("test:expired"))

    def test_get_missing_key_returns_none(self):
        self.assertIsNone(server.cache_get("test:does-not-exist"))


if __name__ == "__main__":
    unittest.main()
