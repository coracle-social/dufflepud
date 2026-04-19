# Dufflepud

A media server for the Coracle nostr client.

# Installation Guide

First, get the repository and install dependencies. You'll need to have [poetry](https://python-poetry.org/) installed.

```
git clone https://github.com/coracle-social/dufflepud.git
cd dufflepud
poetry install
```

Next, fill out the environment file by running `cp env.template env.local` and adding values for the linkpreview api and database url. If you're running this on a PaaS, you'll want to use their environment settings since env.local is not committed to version control.

Finally, enter `poetry run ./start` to start the server.

## Namecoin NIP-05 &mdash; opt-in

Dufflepud's `/handle/info` endpoint can transparently resolve NIP-05 handles
rooted in the [Namecoin](https://www.namecoin.org/) blockchain instead of
HTTPS. No DNS or registrar in the trust path.

Supported identifier forms (all routed to Namecoin when detected):

- `alice@example.bit` &mdash; standard NIP-05 `<name>@<domain>.bit`
- `example.bit` &mdash; bare domain (resolves the `_` root name)
- `d/example` &mdash; direct Namecoin `d/` namespace reference
- `id/alice` &mdash; Namecoin `id/` namespace (identity-first records)

Two resolution backends are supported. Configuring either one enables the
feature; when both are unset, Namecoin identifiers fail to resolve (same
observable behavior as an unreachable DNS host). When both are set, RPC is
tried first and ElectrumX is the fallback. Regular DNS-based NIP-05 is
unaffected either way.

### Backend 1: namecoind JSON-RPC (trustless)

Set `NAMECOIN_RPC_URL` to a [namecoind](https://www.namecoin.org/) JSON-RPC
endpoint, e.g. `http://user:pass@127.0.0.1:8336`. Dufflepud calls `name_show`
directly, so resolution is trustless against the blockchain you're syncing.
Requires ~10 GB of disk and a one-time chain sync.

### Backend 2: ElectrumX (SPV-style, no full node)

Set `NAMECOIN_ELECTRUMX_SERVERS` to a comma-separated list of server URLs.
Schemes:

- `tcp+tls://host:port` &mdash; raw TCP + TLS (standard Electrum, port 50002)
- `wss://host:port` &mdash; Secure WebSocket (typically port 50004)
- `tcp://host:port` &mdash; plaintext (discouraged)

A bare `host:port` is treated as `tcp+tls`. Use the literal value `default`
to expand to the built-in list of well-known public Namecoin ElectrumX
servers; it may be mixed with explicit URLs:

```
NAMECOIN_ELECTRUMX_SERVERS=default
NAMECOIN_ELECTRUMX_SERVERS=tcp+tls://electrumx.testls.space:50002,wss://my-host.example:50004
NAMECOIN_ELECTRUMX_SERVERS=default,tcp+tls://my-private.example:50002
```

Dufflepud ships pinned self-signed certificates for the public servers
(same pin set as [Amethyst](https://github.com/vitorpamplona/amethyst)),
so TLS succeeds without requiring operators to manage a custom trust store.
For private servers with self-signed certs, provide the PEM-encoded
certificate(s) via `NAMECOIN_ELECTRUMX_PINS` (literal newlines or `\n`
escapes between certs).

Name resolution uses the standard Electrum scripthash protocol
(`blockchain.scripthash.get_history` + `blockchain.transaction.get`), so any
Namecoin-indexed ElectrumX server works &mdash; no special RPC methods required.

### Tuning

All optional, with defaults shown:

```
NAMECOIN_CONNECT_TIMEOUT=10     # seconds
NAMECOIN_READ_TIMEOUT=15        # seconds
NAMECOIN_LOOKUP_TIMEOUT=20      # total per resolve, across retries
NAMECOIN_CACHE_TTL=3600         # in-process resolver cache TTL
NAMECOIN_CACHE_MAX_ENTRIES=500  # in-process resolver cache size
```

The in-process cache is separate from (and complements) Dufflepud's existing
Redis `handle:` cache: it short-circuits repeat lookups for the _same_
identifier within a process, including during the Redis cache miss window.

On-chain record format (stored in the `value` of `d/<label>`):

```json
{
  "ip": "...",
  "nostr": {
    "names": { "_": "<hex-pubkey>", "alice": "<hex-pubkey>" },
    "relays": { "<hex-pubkey>": ["wss://..."] },
    "nip46": { "<hex-pubkey>": ["wss://..."] }
  }
}
```

Dufflepud returns the same `{pubkey, relays, nip46}` shape as the existing
handle resolver, so no client changes are required.
