"""Stdlib-only smoke tests for dufflepud.namecoin.

Run with:

    python -m unittest tests.test_namecoin

These cover the offline portions of the Namecoin resolver (identifier
parsing, value extraction, server-URL parsing, env-var config, push-data
script codec) and do not require network access or a running namecoind
/ ElectrumX server. The live-blockchain validation lives outside the
test suite because it needs external services.
"""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from unittest.mock import patch

# NOTE: the module under test only needs `aiohttp` at import time for
# the network backends. If aiohttp is unavailable in the test env,
# skip the whole module with a clear message.
try:
    from dufflepud import namecoin as nc
except Exception as exc:  # pragma: no cover - import-time skip
    raise unittest.SkipTest(f"dufflepud.namecoin unavailable: {exc}")


VALID_PUB_A = "a" * 64
VALID_PUB_B = "b" * 64


class ParseIdentifierTests(unittest.TestCase):
    def _assert(self, raw, expected):
        got = nc.parse_identifier(raw)
        if expected is None:
            self.assertIsNone(got, raw)
        else:
            self.assertIsNotNone(got, raw)
            self.assertEqual(got.namecoin_name, expected[0])
            self.assertEqual(got.local_part, expected[1])
            self.assertEqual(got.namespace, expected[2])

    def test_nip05_style(self):
        self._assert("alice@example.bit", ("d/example", "alice", "d"))

    def test_nip05_root(self):
        self._assert("_@example.bit", ("d/example", "_", "d"))

    def test_bare_domain(self):
        self._assert("example.bit", ("d/example", "_", "d"))

    def test_case_insensitive(self):
        self._assert("EXAMPLE.BIT", ("d/example", "_", "d"))

    def test_whitespace_tolerant(self):
        self._assert("  m@testls.bit  ", ("d/testls", "m", "d"))

    def test_d_namespace(self):
        self._assert("d/example", ("d/example", "_", "d"))

    def test_id_namespace(self):
        self._assert("id/alice", ("id/alice", "_", "id"))

    def test_empty(self):
        self._assert("", None)

    def test_non_bit(self):
        self._assert("alice@example.com", None)

    def test_just_text(self):
        self._assert("just-text", None)


class IsNamecoinIdentifierTests(unittest.TestCase):
    def test_bit_domain(self):
        self.assertTrue(nc.is_namecoin_identifier("alice.bit"))

    def test_nip05_bit(self):
        self.assertTrue(nc.is_namecoin_identifier("alice@example.bit"))

    def test_d_namespace(self):
        self.assertTrue(nc.is_namecoin_identifier("d/foo"))

    def test_id_namespace(self):
        self.assertTrue(nc.is_namecoin_identifier("id/foo"))

    def test_non_bit(self):
        self.assertFalse(nc.is_namecoin_identifier("alice@example.com"))


class ParseServersTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(nc.parse_servers(""), [])

    def test_default_token(self):
        servers = nc.parse_servers("default")
        self.assertEqual(len(servers), len(nc.DEFAULT_ELECTRUMX_SERVERS))

    def test_tor_token(self):
        servers = nc.parse_servers("tor")
        self.assertEqual(len(servers), len(nc.TOR_ELECTRUMX_SERVERS))
        self.assertTrue(servers[0].host.endswith(".onion"))

    def test_explicit_mix(self):
        servers = nc.parse_servers(
            "tcp+tls://a:50002,wss://b:50004,c:50002"
        )
        self.assertEqual(len(servers), 3)
        self.assertEqual(servers[0].scheme, "tcp+tls")
        self.assertEqual(servers[1].scheme, "wss")
        self.assertEqual(servers[2].scheme, "tcp+tls")  # bare => tcp+tls

    def test_dedup(self):
        servers = nc.parse_servers(
            "tcp+tls://h:50002,tcp+tls://h:50002"
        )
        self.assertEqual(len(servers), 1)

    def test_mixed_tokens_and_explicit(self):
        servers = nc.parse_servers(
            "tor,default,tcp+tls://custom:50002"
        )
        hosts = {s.host for s in servers}
        self.assertIn("custom", hosts)
        self.assertIn("electrumx.testls.space", hosts)
        self.assertTrue(any(h.endswith(".onion") for h in hosts))

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            nc.parse_servers("not-a-url-with-no-port")


class ConfigFromEnvTests(unittest.TestCase):
    def _getter(self, mapping):
        return lambda k: mapping.get(k)

    def test_empty_disabled(self):
        cfg = nc.NamecoinConfig.from_env(self._getter({}))
        self.assertFalse(cfg.enabled)

    def test_rpc_only(self):
        cfg = nc.NamecoinConfig.from_env(self._getter({
            "NAMECOIN_RPC_URL": "http://u:p@127.0.0.1:8336",
        }))
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.rpc_url, "http://u:p@127.0.0.1:8336")
        self.assertEqual(cfg.electrumx_servers, [])

    def test_electrumx_default(self):
        cfg = nc.NamecoinConfig.from_env(self._getter({
            "NAMECOIN_ELECTRUMX_SERVERS": "default",
        }))
        self.assertTrue(cfg.enabled)
        self.assertEqual(len(cfg.electrumx_servers), 3)

    def test_timeouts(self):
        cfg = nc.NamecoinConfig.from_env(self._getter({
            "NAMECOIN_CONNECT_TIMEOUT": "2.5",
            "NAMECOIN_LOOKUP_TIMEOUT": "30",
        }))
        self.assertEqual(cfg.connect_timeout, 2.5)
        self.assertEqual(cfg.lookup_timeout, 30.0)

    def test_invalid_numeric_ignored(self):
        cfg = nc.NamecoinConfig.from_env(self._getter({
            "NAMECOIN_CONNECT_TIMEOUT": "not-a-number",
        }))
        self.assertEqual(cfg.connect_timeout, 10.0)  # default

    def test_socks5_explicit(self):
        cfg = nc.NamecoinConfig.from_env(self._getter({
            "NAMECOIN_SOCKS5_PROXY": "socks5://127.0.0.1:9050",
        }))
        self.assertEqual(cfg.socks5_proxy, "socks5://127.0.0.1:9050")

    def test_socks5_all_proxy_fallback(self):
        cfg = nc.NamecoinConfig.from_env(self._getter({
            "ALL_PROXY": "socks5://tor:9050",
        }))
        self.assertEqual(cfg.socks5_proxy, "socks5://tor:9050")

    def test_socks5_explicit_wins(self):
        cfg = nc.NamecoinConfig.from_env(self._getter({
            "NAMECOIN_SOCKS5_PROXY": "socks5://a:1",
            "ALL_PROXY": "socks5://b:2",
        }))
        self.assertEqual(cfg.socks5_proxy, "socks5://a:1")


class DomainValueExtractionTests(unittest.TestCase):
    """Cover every value layout Amethyst accepts."""

    def _parsed(self, local="_"):
        return nc._ParsedIdentifier("d/x", local, "d")

    def test_simple_root(self):
        r = nc._extract_from_domain_value({"nostr": VALID_PUB_A}, self._parsed())
        self.assertIsNotNone(r)
        self.assertEqual(r.pubkey, VALID_PUB_A)
        self.assertEqual(r.local_part, "_")

    def test_simple_requires_root(self):
        # "alice" cannot come from a scalar nostr field.
        r = nc._extract_from_domain_value(
            {"nostr": VALID_PUB_A}, self._parsed("alice")
        )
        self.assertIsNone(r)

    def test_extended_named(self):
        r = nc._extract_from_domain_value(
            {"nostr": {"names": {"_": VALID_PUB_A, "alice": VALID_PUB_B}}},
            self._parsed("alice"),
        )
        self.assertEqual(r.pubkey, VALID_PUB_B)

    def test_extended_root(self):
        r = nc._extract_from_domain_value(
            {"nostr": {"names": {"_": VALID_PUB_A}}},
            self._parsed("_"),
        )
        self.assertEqual(r.pubkey, VALID_PUB_A)

    def test_extended_unknown_local_part_no_fallback(self):
        # Dufflepud intentionally differs from Amethyst here: a wrong
        # local-part fails resolution instead of silently resolving to root.
        r = nc._extract_from_domain_value(
            {"nostr": {"names": {"_": VALID_PUB_A}}},
            self._parsed("bob"),
        )
        self.assertIsNone(r)

    def test_relays_and_nip46(self):
        r = nc._extract_from_domain_value(
            {"nostr": {
                "names": {"alice": VALID_PUB_B},
                "relays": {VALID_PUB_B: ["wss://r1", "wss://r2"]},
                "nip46": {VALID_PUB_B: ["wss://bunker"]},
            }},
            self._parsed("alice"),
        )
        self.assertEqual(r.relays, ("wss://r1", "wss://r2"))
        self.assertEqual(r.nip46, ("wss://bunker",))

    def test_flat_layout(self):
        r = nc._extract_from_domain_value(
            {"names": {"alice": VALID_PUB_B}}, self._parsed("alice")
        )
        self.assertEqual(r.pubkey, VALID_PUB_B)

    def test_invalid_pubkey_rejected(self):
        r = nc._extract_from_domain_value(
            {"nostr": {"names": {"alice": "not-a-pubkey"}}},
            self._parsed("alice"),
        )
        self.assertIsNone(r)


class IdentityValueExtractionTests(unittest.TestCase):
    def _parsed(self):
        return nc._ParsedIdentifier("id/x", "_", "id")

    def test_simple(self):
        r = nc._extract_from_identity_value({"nostr": VALID_PUB_A}, self._parsed())
        self.assertEqual(r.pubkey, VALID_PUB_A)

    def test_object_pubkey(self):
        r = nc._extract_from_identity_value(
            {"nostr": {"pubkey": VALID_PUB_A, "relays": ["wss://r"]}},
            self._parsed(),
        )
        self.assertEqual(r.pubkey, VALID_PUB_A)
        self.assertEqual(r.relays, ("wss://r",))

    def test_names_map(self):
        r = nc._extract_from_identity_value(
            {"nostr": {"names": {"_": VALID_PUB_A}}}, self._parsed()
        )
        self.assertEqual(r.pubkey, VALID_PUB_A)

    def test_no_nostr(self):
        r = nc._extract_from_identity_value({"other": "thing"}, self._parsed())
        self.assertIsNone(r)


class Nip05RecordShapeTests(unittest.TestCase):
    def test_shape(self):
        r = nc.NamecoinNostrResult(
            pubkey=VALID_PUB_A,
            namecoin_name="d/x",
            local_part="alice",
            relays=("wss://r1",),
            nip46=("wss://n1",),
        )
        rec = r.to_nip05_record()
        self.assertEqual(rec["names"], {"alice": VALID_PUB_A})
        self.assertEqual(rec["relays"], {VALID_PUB_A: ["wss://r1"]})
        self.assertEqual(rec["nip46"], {VALID_PUB_A: ["wss://n1"]})

    def test_shape_no_relays(self):
        r = nc.NamecoinNostrResult(
            pubkey=VALID_PUB_A, namecoin_name="d/x", local_part="_"
        )
        rec = r.to_nip05_record()
        self.assertEqual(rec, {"names": {"_": VALID_PUB_A}})


class NameIndexScriptTests(unittest.TestCase):
    """Sanity-check the Bitcoin push-data codec used for scripthash queries."""

    def test_round_trip_short(self):
        script = nc._build_name_index_script(b"d/example")
        # First byte: OP_NAME_UPDATE (0x53)
        self.assertEqual(script[0], 0x53)
        # Parse it back out
        name, _ = nc._parse_name_script(
            script[:1 + 1 + len(b"d/example")] + nc._push_data(b"")
        )
        self.assertEqual(name, "d/example")

    def test_push_data_boundary_short(self):
        # <0x4c -> direct push
        data = b"x" * 75
        encoded = nc._push_data(data)
        self.assertEqual(encoded[0], 75)
        self.assertEqual(encoded[1:], data)

    def test_push_data_boundary_pushdata1(self):
        # 0x4c..0xff -> OP_PUSHDATA1
        data = b"x" * 200
        encoded = nc._push_data(data)
        self.assertEqual(encoded[0], nc._OP_PUSHDATA1)
        self.assertEqual(encoded[1], 200)
        self.assertEqual(encoded[2:], data)

    def test_push_data_boundary_pushdata2(self):
        # >0xff -> OP_PUSHDATA2, little-endian length
        data = b"x" * 300
        encoded = nc._push_data(data)
        self.assertEqual(encoded[0], nc._OP_PUSHDATA2)
        self.assertEqual(encoded[1] | (encoded[2] << 8), 300)

    def test_script_hash_is_reversed_sha256(self):
        import hashlib
        data = b"d/example"
        script = nc._build_name_index_script(data)
        expected = hashlib.sha256(script).digest()[::-1].hex()
        self.assertEqual(nc._electrum_script_hash(script), expected)


class CacheTests(unittest.TestCase):
    def test_put_get_roundtrip(self):
        cache = nc._TtlLruCache(max_entries=4)
        cache.put("alice@x.bit", "value-alice")
        self.assertEqual(cache.get("alice@x.bit", ttl=60), "value-alice")

    def test_ttl_expiry(self):
        cache = nc._TtlLruCache(max_entries=4)
        cache.put("k", "v")
        self.assertIsNone(cache.get("k", ttl=-1))  # already expired

    def test_lru_eviction(self):
        cache = nc._TtlLruCache(max_entries=2)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.put("c", 3)  # evicts 'a'
        self.assertIsNone(cache.get("a", ttl=60))
        self.assertEqual(cache.get("b", ttl=60), 2)
        self.assertEqual(cache.get("c", ttl=60), 3)


class CircuitBreakerTests(unittest.TestCase):
    def setUp(self):
        nc._server_states.clear()

    def tearDown(self):
        nc._server_states.clear()

    def test_arms_after_threshold(self):
        srv = nc.ElectrumxServer("tcp+tls", "nope.invalid", 50002)
        cfg = nc.NamecoinConfig(
            server_error_threshold=3, server_cooldown=60.0
        )
        for _ in range(3):
            nc._record_server_error(srv, cfg)
        self.assertTrue(nc._server_in_cooldown(srv))

    def test_success_resets(self):
        srv = nc.ElectrumxServer("tcp+tls", "nope.invalid", 50002)
        cfg = nc.NamecoinConfig(
            server_error_threshold=3, server_cooldown=60.0
        )
        nc._record_server_error(srv, cfg)
        nc._record_server_success(srv)
        st = nc._server_state(srv)
        self.assertEqual(st.consecutive_errors, 0)
        self.assertFalse(nc._server_in_cooldown(srv))

    def test_threshold_zero_disables(self):
        srv = nc.ElectrumxServer("tcp+tls", "nope.invalid", 50002)
        cfg = nc.NamecoinConfig(
            server_error_threshold=0, server_cooldown=60.0
        )
        for _ in range(10):
            nc._record_server_error(srv, cfg)
        self.assertFalse(nc._server_in_cooldown(srv))


class ResolveIdentifierDetailedTests(unittest.TestCase):
    """Confirm structured outcomes without needing network access."""

    def test_invalid_identifier(self):
        async def run():
            out = await nc.resolve_identifier_detailed(
                "definitely not valid", nc.NamecoinConfig()
            )
            return out
        out = asyncio.run(run())
        self.assertIsInstance(out, nc.NamecoinResolveOutcome.InvalidIdentifier)

    def test_no_config_returns_unreachable(self):
        async def run():
            return await nc.resolve_identifier_detailed(
                "alice@example.bit", nc.NamecoinConfig()
            )
        out = asyncio.run(run())
        self.assertIsInstance(out, nc.NamecoinResolveOutcome.ServersUnreachable)


class SplitPemBlobsTests(unittest.TestCase):
    def test_single(self):
        pem = (
            "-----BEGIN CERTIFICATE-----\n"
            "MIIABC...\n"
            "-----END CERTIFICATE-----\n"
        )
        blobs = nc._split_pem_blobs(pem)
        self.assertEqual(len(blobs), 1)

    def test_multiple_with_newline_separator(self):
        pem = (
            "-----BEGIN CERTIFICATE-----\n"
            "AAA...\n"
            "-----END CERTIFICATE-----\n"
            "-----BEGIN CERTIFICATE-----\n"
            "BBB...\n"
            "-----END CERTIFICATE-----\n"
        )
        blobs = nc._split_pem_blobs(pem)
        self.assertEqual(len(blobs), 2)

    def test_escaped_newlines(self):
        pem = (
            "-----BEGIN CERTIFICATE-----\\n"
            "AAA...\\n"
            "-----END CERTIFICATE-----\\n"
            "-----BEGIN CERTIFICATE-----\\n"
            "BBB...\\n"
            "-----END CERTIFICATE-----\\n"
        )
        blobs = nc._split_pem_blobs(pem)
        self.assertEqual(len(blobs), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
