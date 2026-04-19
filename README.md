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

## Namecoin NIP-05 (`.bit` handles) &mdash; opt-in

Dufflepud's `/handle/info` endpoint can transparently resolve NIP-05 handles
whose domain ends in `.bit` from the [Namecoin](https://www.namecoin.org/)
blockchain instead of HTTPS. Users then get handles like `alice@alice.bit` or
`m@testls.bit` that are resolved via Namecoin's `d/<label>` namespace
convention, with no DNS or registrar in the trust path.

This is **opt-in**: until `NAMECOIN_RPC_URL` is set, `.bit` handles simply
fail to resolve (the same observable behavior as an unreachable DNS host).
Regular DNS-based NIP-05 is unaffected either way.

Set `NAMECOIN_RPC_URL` to a [namecoind](https://www.namecoin.org/) JSON-RPC
endpoint, e.g. `http://user:pass@127.0.0.1:8336`. Dufflepud calls `name_show`
directly, so resolution is trustless against the blockchain you're syncing.

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
